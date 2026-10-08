"""BrainOutside validator port (MIT): pure proposal rules 1–6 and 8.

Generated indexes remove rule 7. Callers supply a workspace-scoped canonical
revision context; validation never writes content or touches a filesystem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.brain.indexer import KNOWLEDGE_TYPES, as_list, parse_frontmatter, parse_files
from core.api.exceptions import ApiError
from .paths import validate_files
from .proposal import validate_shape
from .visibility import _span_close

VISIBILITIES = {"public", "agents-only", "private"}
NOTE_STATUSES = {"current", "superseded"}
# Proposals may only touch content surfaces. INDEX.md and CLAUDE.md are
# edited by trusted code (taxonomy_additions); identity/,
# lenses/, eval/ and infrastructure are never feed-written.
ALLOWED_PREFIXES = ("knowledge/", "projects/", "content-catalog/", "raw/")


#: Rule 6's vocabulary: scripts a brain may ask the validator to reject,
#: by name. **Empty by default** — this used to be a hardcoded Arabic
#: ban, which is a fact about one owner's corpus, not about the engine,
#: and it silently rejected a stranger's notes on a rule they could not
#: see or change. (The template's contract text was generalised for
#: exactly this reason; the server never followed.) A brain opts in via
#: its own CLAUDE.md — see `blocked_scripts_from_claude_md`.
#:
#: Ranges are deliberately coarse. The question is "does this file
#: contain text in that script at all", not "what language is this".
BLOCKABLE_SCRIPTS: dict[str, str] = {
    # Arabic, Arabic Supplement, Arabic Extended-A, presentation forms A+B
    # — byte-for-byte the ranges the hardcoded rule used.
    "arabic": "[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]",
    "hebrew": "[֐-׿יִ-ﭏ]",
    "cyrillic": "[Ѐ-ԯ]",
    "greek": "[Ͱ-Ͽἀ-῿]",
    "devanagari": "[ऀ-ॿ]",
    "thai": "[฀-๿]",
    "han": "[㐀-䶿一-鿿]",
    "hiragana": "[぀-ゟ]",
    "katakana": "[゠-ヿ]",
    "hangul": "[ᄀ-ᇿ가-힯]",
}
_SCRIPT_RES = {name: re.compile(pattern) for name, pattern in BLOCKABLE_SCRIPTS.items()}

_DATE_RE = re.compile(r"^\d{4}-\d{2}$")
_WS_RE = re.compile(r"\s+")
# Minimum normalized-line length considered "quoting" for rule 8 — short
# strings collide by chance, whole sentences don't.
PRIVATE_QUOTE_MIN_CHARS = 30


@dataclass(frozen=True)
class Violation:
    rule: int
    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - repr only
        return f"rule {self.rule} [{self.path}]: {self.message}"


@dataclass
class ValidationResult:
    violations: list[Violation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.violations


@dataclass
class ValidationContext:
    """Everything the pure rules need from the outside world."""

    taxonomy: set[str] = field(default_factory=set)
    # entity_id -> {"path", "visibility", "status"} for the CURRENT repo state.
    entities: dict[str, dict] = field(default_factory=dict)
    # Full text of every `visibility: private` entity (rule 8).
    private_texts: list[str] = field(default_factory=list)
    # The feed's source kind — rule 2 allows a null source_url for
    # "thought" (contract §5 rule 2 amendment, 2026-07-30).
    source_kind: str = ""
    # Rule 6, opted into by the brain's CLAUDE.md. Empty = nothing blocked.
    blocked_scripts: tuple[str, ...] = ()
    # Names the brain asked for that this server does not know. Carried so
    # the approval UI can say so — a typo here silently disarms the rule,
    # and a silently disarmed rule is the failure this whole change is
    # about.
    unknown_scripts: tuple[str, ...] = ()
    files: dict[str, str] | None = None
    source_visibility: str = "agents-only"


def context_from_files(files, *, source_kind="", source_visibility="agents-only"):
    """Build rules from the exact manifest being reviewed, never the live index."""
    entities = parse_files(files)
    contract = files.get("CLAUDE.md", "")
    known, unknown = blocked_scripts_from_claude_md(contract)
    return ValidationContext(
        taxonomy=taxonomy_from_claude_md(contract),
        entities={
            e.entity_id: {
                "path": e.path,
                "visibility": e.visibility,
                "status": e.status,
                "kind": e.kind,
            }
            for e in entities
        },
        private_texts=[files[e.path] for e in entities if e.visibility == "private"],
        source_kind=source_kind,
        blocked_scripts=known,
        unknown_scripts=unknown,
        files=files,
        source_visibility=source_visibility,
    )


def description_from_content(content):
    fm, body, malformed = parse_frontmatter(content.lstrip("\ufeff"))
    if malformed:
        return ""
    explicit = fm.get("description")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()[:500]
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", ">", "```", "---", "<!--")):
            return line[:500]
    return ""


def taxonomy_from_claude_md(text: str) -> set[str]:
    """Parse §7's backtick-quoted tag list. Anchored to the heading so a
    stray inline-code span elsewhere never widens the vocabulary."""
    m = re.search(r"^##.*Topic taxonomy.*$", text, re.MULTILINE)
    if not m:
        return set()
    section = text[m.end() :]
    nxt = re.search(r"^## ", section, re.MULTILINE)
    if nxt:
        section = section[: nxt.start()]
    tags: set[str] = set()
    for span in re.findall(r"`([^`]+)`", section):
        for tok in span.split(","):
            tok = tok.strip()
            if re.fullmatch(r"[a-z0-9][a-z0-9-]*", tok):
                tags.add(tok)
    return tags


#: Only a line that opens with this carries the list. See the docstring.
_SCRIPTS_LINE_RE = re.compile(r"^[ \t]*Scripts:[ \t]*(.+)$", re.MULTILINE | re.IGNORECASE)


def blocked_scripts_from_claude_md(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Parse a `## Blocked scripts` section. Returns (known, unknown).

    Anchored to its own heading like the taxonomy parser above, but the
    list must sit on a line that STARTS with `Scripts:`:

        ## Blocked scripts

        Scripts: `arabic, hebrew`

    Not "any backticks in the section", which is how this was first
    written and which the template immediately broke: that section has
    to name every script the server knows in order to document them, and
    it shows a commented-out example. Harvesting backticks from prose
    would have turned a page of documentation into a blocklist of
    everything, and left `<!-- Scripts: `arabic` -->` switched ON. A
    commented-out line does not start with `Scripts:`, so it stays off.

    Absent section, absent file, or an empty list all mean *block
    nothing* — the right default for a server somebody else is about to
    install. Names this server does not recognise come back separately
    rather than being dropped, so a typo surfaces instead of quietly
    turning the rule off.
    """
    m = re.search(r"^##.*Blocked scripts.*$", text, re.MULTILINE)
    if not m:
        return (), ()
    section = text[m.end() :]
    nxt = re.search(r"^## ", section, re.MULTILINE)
    if nxt:
        section = section[: nxt.start()]
    known: list[str] = []
    unknown: list[str] = []
    for line in _SCRIPTS_LINE_RE.findall(section):
        for span in re.findall(r"`([^`]+)`", line):
            for tok in span.split(","):
                tok = tok.strip().lower()
                if not tok:
                    continue
                bucket = known if tok in _SCRIPT_RES else unknown
                if tok not in bucket:
                    bucket.append(tok)
    return tuple(known), tuple(unknown)


# ---- helpers -------------------------------------------------------------


def _split_frontmatter(text):
    fm, body, malformed = parse_frontmatter(text.lstrip("\ufeff"))
    return (None if malformed or not text.lstrip("\ufeff").startswith("---") else fm), body


def _norm(s: str) -> str:
    return _WS_RE.sub(" ", s).strip().lower()


# ---- the rules -----------------------------------------------------------


def validate(proposal: dict, ctx: ValidationContext) -> ValidationResult:
    res = ValidationResult()
    try:
        validate_shape(proposal)
    except ApiError as exc:
        res.violations.append(Violation(0, "", exc.message))
        return res
    files = proposal.get("files") or []
    supersedes = proposal.get("supersedes") or []
    taxonomy_additions = [str(t) for t in (proposal.get("taxonomy_additions") or [])]
    allowed_topics = ctx.taxonomy | set(taxonomy_additions)
    proposed_note_ids: set[str] = set()
    proposed_paths: set[str] = set()
    existing_paths = (
        set(ctx.files) if ctx.files is not None else {e["path"] for e in ctx.entities.values()}
    )
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]*", tag) for tag in taxonomy_additions):
        res.violations.append(
            Violation(
                3, "CLAUDE.md", "taxonomy tags must use lowercase letters, digits and hyphens"
            )
        )

    if taxonomy_additions:
        res.warnings.append(
            "proposes NEW taxonomy tags (contract §7 — extend deliberately): "
            + ", ".join(taxonomy_additions)
        )
    if ctx.unknown_scripts:
        # Said out loud where the operator already is. A misspelled name
        # under "Blocked scripts" disarms rule 6 for that script, and a
        # silently disarmed rule is worse than no rule.
        res.warnings.append(
            "CLAUDE.md lists scripts this server does not know, so they are "
            f"NOT blocked: {', '.join(ctx.unknown_scripts)}. Known names: "
            + ", ".join(sorted(BLOCKABLE_SCRIPTS))
        )

    for f in files:
        path = str(f.get("path", ""))
        action = str(f.get("action", ""))
        content = str(f.get("content", ""))

        try:
            validate_files({path: content})
        except ApiError as exc:
            res.violations.append(Violation(5, path, exc.message))
            continue
        if path.casefold() in proposed_paths:
            res.violations.append(Violation(5, path, "duplicate proposal path"))
            continue
        proposed_paths.add(path.casefold())
        start = 0
        while (start := content.find("(agents-only:", start)) >= 0:
            end = _span_close(content, start)
            if end is None:
                res.violations.append(Violation(10, path, "unterminated agents-only span"))
                break
            start = end + 1

        # -- rule 5: no deletions (and only content surfaces are writable) --
        if action not in {"create", "update"}:
            res.violations.append(
                Violation(5, path, f"unknown action {action!r} — only create/update exist")
            )
            continue
        if ".." in path or path.startswith(("/", "\\")) or ":" in path.split("/")[0]:
            res.violations.append(Violation(5, path, "path escapes the repo"))
            continue
        if not path.startswith(ALLOWED_PREFIXES):
            res.violations.append(
                Violation(5, path, f"proposals may only touch {', '.join(ALLOWED_PREFIXES)}")
            )
            continue
        if not content.strip():
            res.violations.append(
                Violation(5, path, "empty content — emptying a file is a deletion")
            )
            continue
        if action == "update" and path not in existing_paths:
            res.violations.append(Violation(5, path, "update targets a file that does not exist"))
        if action == "create" and path in existing_paths:
            res.violations.append(
                Violation(5, path, "create targets a file that already exists — use update")
            )

        # -- rule 6: no content in a script this brain blocks --
        for script in ctx.blocked_scripts:
            if _SCRIPT_RES[script].search(content):
                res.violations.append(
                    Violation(
                        6,
                        path,
                        f"{script}-script content — your CLAUDE.md lists "
                        f"{script} under Blocked scripts",
                    )
                )

        # -- per-kind frontmatter (rules 1-4) --
        if path.startswith("knowledge/"):
            _validate_note(path, content, allowed_topics, res, proposed_note_ids, ctx)
        elif path.startswith("projects/"):
            _validate_project_card(path, content, res)
        if path.startswith(("knowledge/", "projects/")) and not description_from_content(content):
            res.violations.append(
                Violation(
                    9, path, "add a description or a body paragraph from which one can be derived"
                )
            )
        for entity in parse_files({path: content}):
            previous = ctx.entities.get(entity.entity_id)
            if previous and previous["path"] != path:
                res.violations.append(
                    Violation(1, path, "entity id already belongs to another path")
                )
            default = previous["visibility"] if previous else ctx.source_visibility
            if entity.visibility != default and not any(
                issue.strip() for issue in proposal.get("issues", [])
            ):
                res.violations.append(
                    Violation(11, path, "explain the visibility change in issues before review")
                )
        # content-catalog/ and raw/ carry no frontmatter schema.

    # -- supersedes are declarative; check both ends --
    for s in supersedes:
        old = str(s.get("old_entity_id", ""))
        new = str(s.get("new_entity_id", ""))
        if old not in ctx.entities:
            res.violations.append(Violation(1, old, "supersedes a note that does not exist"))
        if new not in proposed_note_ids:
            res.violations.append(
                Violation(1, new, "superseded_by points at a note this proposal does not create")
            )
        if (
            old == new
            or ctx.entities.get(old, {}).get("kind", "take") not in KNOWLEDGE_TYPES.values()
        ):
            res.violations.append(
                Violation(1, old, "only another knowledge note may be superseded")
            )
    old_ids = [s["old_entity_id"] for s in supersedes]
    if len(set(old_ids)) != len(old_ids):
        res.violations.append(Violation(1, "", "a note may be superseded only once per proposal"))

    # -- rule 8: no quoting from private entities --
    all_content = _norm("\n".join(str(f.get("content", "")) for f in files))
    for text in ctx.private_texts:
        for line in text.splitlines():
            n = _norm(line)
            if len(n) >= PRIVATE_QUOTE_MIN_CHARS and n in all_content:
                res.violations.append(
                    Violation(8, "", f"proposal quotes private content: “{line.strip()[:80]}…”")
                )
                break  # one violation per private file is enough signal

    return res


def _validate_note(path, content, allowed_topics, res, proposed_note_ids, ctx):
    parts = path.split("/")
    folder = parts[1] if len(parts) == 3 else ""
    expected_type = KNOWLEDGE_TYPES.get(folder)
    if expected_type is None or not path.endswith(".md"):
        res.violations.append(
            Violation(1, path, "notes live at knowledge/<takes|stories|lessons|facts>/<id>.md")
        )
        return
    fm, body = _split_frontmatter(content)
    if fm is None:
        res.violations.append(Violation(1, path, "missing or unparseable YAML frontmatter"))
        return

    stem = parts[-1][:-3]
    note_id = str(fm.get("id") or "")
    if note_id in proposed_note_ids or len(note_id) > 200:
        res.violations.append(Violation(1, path, "duplicate or overlong entity id"))
    proposed_note_ids.add(note_id)
    if note_id != stem:
        res.violations.append(Violation(1, path, f"id {note_id!r} ≠ filename {stem!r}"))
    ntype = str(fm.get("type") or "")
    if ntype != expected_type:
        res.violations.append(Violation(1, path, f"type {ntype!r} ≠ folder type {expected_type!r}"))
    status = str(fm.get("status") or "")
    if status not in NOTE_STATUSES:
        res.violations.append(
            Violation(1, path, f"status must be current|superseded, got {status!r}")
        )
    if status == "superseded" and not fm.get("superseded_by"):
        res.violations.append(Violation(1, path, "status superseded requires superseded_by"))
    vis = str(fm.get("visibility") or "")
    if vis not in VISIBILITIES:
        res.violations.append(
            Violation(1, path, f"visibility must be one of {sorted(VISIBILITIES)}, got {vis!r}")
        )
    date = str(fm.get("date") or "")
    if not _DATE_RE.match(date):
        res.warnings.append(f"{path}: date should be YYYY-MM, got {date!r}")

    # -- rule 2: provenance is mandatory --
    if not str(fm.get("source") or "").strip():
        res.violations.append(Violation(2, path, "source missing — no note without provenance"))
    if not str(fm.get("source_url") or "").strip() and ctx.source_kind != "thought":
        # Direct thoughts have no URL: `source` (the feed id / raw
        # archive) IS the provenance (contract §5 rule 2, 2026-07-30).
        res.violations.append(Violation(2, path, "source_url missing — no note without provenance"))

    # -- rule 3: topics ⊆ taxonomy --
    topics = as_list(fm.get("topics"))
    if not topics:
        res.violations.append(Violation(3, path, "at least one topic tag required"))
    for t in topics:
        if t not in allowed_topics:
            res.violations.append(
                Violation(3, path, f"topic {t!r} is not in the CLAUDE.md taxonomy")
            )
    if len(topics) > 4:
        res.warnings.append(f"{path}: {len(topics)} topics (contract says 1-4)")

    # -- rule 4: voice is sacred --
    if ntype in {"take", "story"} and "> VERBATIM:" not in body:
        res.violations.append(Violation(4, path, "takes/stories must carry a `> VERBATIM:` quote"))


def _validate_project_card(path, content, res):
    fm, _body = _split_frontmatter(content)
    if fm is None:
        res.violations.append(
            Violation(1, path, "project card missing or unparseable YAML frontmatter")
        )
        return
    if not str(fm.get("id") or "").strip():
        res.violations.append(Violation(1, path, "project card requires an id"))
    lv = str(fm.get("last-verified") or "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", lv):
        res.violations.append(
            Violation(1, path, "touched project cards require last-verified (YYYY-MM-DD)")
        )
