"""BrainOutside indexer port (MIT): parse manifest text, never a git checkout."""

import hashlib
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import PurePosixPath

import yaml
from django.db import transaction

from core.models import BrainEntity

log = logging.getLogger(__name__)
KNOWLEDGE_TYPES = {"takes": "take", "stories": "story", "lessons": "lesson", "facts": "fact"}
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_MAX_LENGTHS = {
    f.name: f.max_length for f in BrainEntity._meta.get_fields() if getattr(f, "max_length", None)
}
_CLAMPABLE = ("title", "status", "superseded_by", "source", "source_url", "date", "kind")


@dataclass
class ParsedEntity:
    entity_id: str
    kind: str
    path: str  # repo-relative POSIX
    title: str = ""
    description: str = ""
    status: str = "current"
    superseded_by: str = ""
    visibility: str = "agents-only"
    topics: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    source: str = ""
    source_url: str = ""
    date: str = ""
    last_verified: object = None  # datetime.date | None
    content_sha: str = ""
    malformed_frontmatter: bool = False


def parse_frontmatter(text: str) -> tuple[dict, str, bool]:
    """(frontmatter, body, malformed).

    `malformed` is True only when a `---` block IS present but does not
    yield a YAML mapping. That distinction matters: a file with no block at
    all is legitimate (catalogs have none), but a block the author wrote
    and we could not read must not be treated as "no metadata" — see
    `_resolve_visibility`, which fails closed on it.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text, bool(re.match(r"\A---[ \t]*\r?\n", text))
    if len(m.group(1)) > 65536:
        return {}, text[m.end() :], True
    try:
        fm = yaml.safe_load(m.group(1))
        if fm is None:
            fm = {}
        # Bound aliases/nesting before converting metadata values to strings.
        count = 0
        active = set()

        def bounded(value, depth=0):
            nonlocal count
            count += 1
            if count > 4096 or depth > 32 or id(value) in active:
                raise ValueError("Frontmatter structure exceeds limits")
            if isinstance(value, (dict, list)):
                active.add(id(value))
                children = (
                    list(value.keys()) + list(value.values()) if isinstance(value, dict) else value
                )
                for child in children:
                    bounded(child, depth + 1)
                active.remove(id(value))

        bounded(fm)
    except (yaml.YAMLError, RecursionError, ValueError):
        return {}, text[m.end() :], True
    if not isinstance(fm, dict):
        return {}, text[m.end() :], True
    return fm, text[m.end() :], False


def split_frontmatter(text: str) -> tuple[dict, str]:
    fm, body, _malformed = parse_frontmatter(text)
    return fm, body


def _title_of(body: str, fallback: str) -> str:
    m = _H1_RE.search(body)
    return m.group(1).strip() if m else fallback


def as_list(v: object) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str) and v.strip():
        return [s.strip() for s in v.strip("[]").split(",") if s.strip()]
    return []


def _parse_date(v: object):
    import datetime

    if isinstance(v, datetime.date):
        return v
    if isinstance(v, str):
        try:
            return datetime.date.fromisoformat(v.strip())
        except ValueError:
            return None
    return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_visibility(explicit: object, default: str, *, malformed: bool = False) -> str:
    """Explicit frontmatter wins; otherwise the path default.

    When the frontmatter block exists but could not be parsed we cannot
    know what the author asked for, so we fall back to the MOST restrictive
    tier rather than the path default. Previously a YAML typo (or a UTF-8
    BOM, which stopped the block matching at all) silently turned a note
    marked `visibility: private` into the path default `agents-only` — and
    then copied it into the agents-only snapshot and served it. A file that
    fails to parse must never become more visible than it was.
    """
    if malformed:
        return "private"
    v = str(explicit).strip() if explicit else ""
    return v if v in {"public", "agents-only", "private"} else default


def _fit_to_schema(parsed: list[ParsedEntity]) -> list[ParsedEntity]:
    """Clamp over-long frontmatter values instead of failing the whole sync.

    Postgres raises DataError on an over-length value and aborts the entire
    rebuild; SQLite (dev) accepts anything, so this only ever bit in
    production. A 300-char H1, `type: recommendation-notes` (kind is 16),
    `date: January 2026` (10), or a long tracking URL were each enough to
    freeze the index for every note in the repo.
    """
    kept: list[ParsedEntity] = []
    for e in parsed:
        too_long = [
            (f, _MAX_LENGTHS[f])
            for f in ("path", "entity_id")
            if len(getattr(e, f)) > _MAX_LENGTHS[f]
        ]
        if too_long:
            log.error(
                "skipping %s: %s exceeds the schema limit and cannot be truncated safely",
                e.path[:120],
                ", ".join(f"{f} ({_MAX_LENGTHS[f]})" for f, _ in too_long),
            )
            continue
        for f in _CLAMPABLE:
            limit = _MAX_LENGTHS.get(f)
            value = getattr(e, f)
            if limit and isinstance(value, str) and len(value) > limit:
                log.warning("%s: %s truncated to %d chars", e.path, f, limit)
                setattr(e, f, value[:limit])
        kept.append(e)
    return kept


def parse_files(files: dict[str, str], descriptions=None) -> list[ParsedEntity]:
    """Parse the five content dialects from one validated revision's text."""
    entities = []
    descriptions = descriptions or {}
    for path, text in sorted(files.items()):
        p = PurePosixPath(path)
        parts = p.parts
        if p.suffix != ".md" or p.name == "_TEMPLATE.md":
            continue
        if len(parts) == 3 and parts[0] == "knowledge" and parts[1] in KNOWLEDGE_TYPES:
            kind, fallback_id, default = KNOWLEDGE_TYPES[parts[1]], p.stem, "agents-only"
        elif len(parts) == 2 and parts[0] in {"identity", "projects", "lenses", "content-catalog"}:
            kind = {
                "identity": "identity",
                "projects": "project",
                "lenses": "lens",
                "content-catalog": "catalog",
            }[parts[0]]
            if kind == "catalog" and p.name == "README.md":
                continue
            fallback_id = f"{kind}-{p.stem}"
            default = "public" if kind in {"lens", "catalog"} else "agents-only"
        else:
            continue
        fm, body, malformed = parse_frontmatter(text.lstrip("\ufeff"))
        if malformed:
            log.warning("unparseable frontmatter in %s — indexing it as private", path)
        entity_id = (
            f"lens-{fm.get('lens') or p.stem}"
            if kind == "lens"
            else str(fm.get("id") or fallback_id)
        )
        entities.append(
            ParsedEntity(
                entity_id=entity_id,
                kind=str(fm.get("type") or kind) if parts[0] == "knowledge" else kind,
                path=path,
                title=_title_of(body, p.stem),
                description=str(fm.get("description") or descriptions.get(path, "")),
                status=str(fm.get("status") or "current") if parts[0] == "knowledge" else "current",
                superseded_by=str(fm.get("superseded_by") or ""),
                visibility=_resolve_visibility(fm.get("visibility"), default, malformed=malformed),
                topics=as_list(fm.get("topics")),
                projects=as_list(fm.get("projects")),
                source=str(fm.get("source") or ""),
                source_url=str(fm.get("source_url") or ""),
                date=str(fm.get("date") or ""),
                last_verified=_parse_date(fm.get("last-verified")),
                content_sha=_sha256(text.encode("utf-8")),
                malformed_frontmatter=malformed,
            )
        )
    return _fit_to_schema(entities)


class DuplicateEntityId(ValueError):
    """Two manifest paths claim the same stable entity id."""


@dataclass
class IndexChanges:
    added: int = 0
    changed: int = 0
    removed: int = 0


@transaction.atomic
def rebuild(brain, revision, files, *, changed_paths=None):
    """Prune before upsert so renames/id swaps remain atomic, including SQLite."""
    parsed = parse_files(files, brain.legacy_descriptions)
    by_id = {}
    for entity in parsed:
        if entity.entity_id in by_id:
            raise DuplicateEntityId(
                f"duplicate frontmatter id {entity.entity_id!r}: {by_id[entity.entity_id]} and {entity.path}"
            )
        by_id[entity.entity_id] = entity.path
    scope = BrainEntity.objects.filter(brain=brain, brain__workspace=brain.workspace)
    existing = {entity.path: entity for entity in scope}
    # A caller may pass only changed file texts. Unchanged index rows still
    # participate in duplicate-id detection and receive the new revision stamp.
    if changed_paths is not None:
        for obj in existing.values():
            if obj.path not in changed_paths and obj.entity_id in by_id:
                raise DuplicateEntityId(
                    f"duplicate frontmatter id {obj.entity_id!r}: {obj.path} and {by_id[obj.entity_id]}"
                )
    affected = set(existing) if changed_paths is None else set(changed_paths)
    parsed_by_path = {e.path: e for e in parsed}
    gone = affected - set(parsed_by_path)
    result = IndexChanges(removed=len(gone & set(existing)))
    # Release both uniqueness keys before inserts (renames and swaps).
    reidentified = {
        path
        for path, e in parsed_by_path.items()
        if path in existing and existing[path].entity_id != e.entity_id
    }
    scope.filter(path__in=gone | reidentified).delete()
    for parsed in parsed_by_path.values():
        values = asdict(parsed)
        path = values.pop("path")
        previous = existing.get(path)
        if previous is None or path in reidentified:
            BrainEntity.objects.create(brain=brain, revision=revision, path=path, **values)
            if previous is None:
                result.added += 1
            else:
                result.changed += 1
        else:
            dirty = [key for key, value in values.items() if getattr(previous, key) != value]
            if dirty:
                for key in dirty:
                    setattr(previous, key, values[key])
                previous.revision = revision
                previous.save(update_fields=dirty + ["revision", "indexed_at"])
                result.changed += 1
    scope.exclude(revision=revision).update(revision=revision)
    return result
