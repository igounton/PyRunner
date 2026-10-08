"""Consistent, tier-filtered DB snapshots and BrainOutside read response shapes."""

from dataclasses import dataclass

from django.db import transaction

from .access import resolve, unknown
from .service import BrainService
from .visibility import raw_paths, strip_agents_only_spans, visible_at


def at_tier(value, tier):
    if tier == "public":
        return strip_agents_only_spans(value)
    return value


@dataclass
class Snapshot:
    brain: object
    tier: str
    entities: list
    files: dict


@transaction.atomic
def snapshot(ctx, params, *, operation=None):
    # Serialize with apply, so entity visibility and blob bytes share one head.
    # The transaction ends before a caller can start a slow reader/model call.
    brain, access = resolve(ctx, params, lock=True)
    entities = list(brain.entities.filter(brain__workspace=ctx.workspace))
    visible = [e for e in entities if visible_at(e.visibility, access.tier)]
    paths = {e.path for e in visible}
    if operation in {"get-index", "list-notes"}:
        return Snapshot(brain, access.tier, visible, {})
    if operation == "get-note":
        paths = {e.path for e in visible if e.entity_id == params["entity_id"]}
    elif operation == "get-lens":
        paths = {
            e.path for e in visible if e.entity_id == f"lens-{params['name']}" and e.kind == "lens"
        }
    elif operation == "get-identity":
        paths = {e.path for e in visible if e.kind == "identity"}
    files = BrainService.files(ctx.workspace, brain.pk, paths=paths)
    if operation is None or operation == "get-raw":
        # Membership is from the immutable manifest; link text comes only from
        # visible entities. Never load unrelated raw/private blob contents.
        manifest = brain.head_revision.manifest if brain.head_revision_id else {}
        raw = {p: "" for p in manifest if p.startswith("raw/")}
        raw.update(files)
        allowed = raw_paths(raw, visible, access.tier)
        if operation == "get-raw":
            allowed &= {params["path"]}
        files.update(BrainService.files(ctx.workspace, brain.pk, paths=allowed))
        paths.update(allowed)
    return Snapshot(
        brain,
        access.tier,
        visible,
        {path: at_tier(files[path], access.tier) for path in paths if path in files},
    )


def note_row(entity, tier):
    return {
        "entity_id": at_tier(entity.entity_id, tier),
        "kind": entity.kind,
        "title": at_tier(entity.title, tier),
        "description": at_tier(entity.description, tier),
        "status": at_tier(entity.status, tier),
        "date": entity.date,
        "last_verified": entity.last_verified.isoformat() if entity.last_verified else "",
        "path": at_tier(entity.path, tier),
        "topics": [at_tier(topic, tier) for topic in entity.topics],
    }


def generated_index(view):
    """Port the source's generated format; all fields honor inline spans."""
    lines = [
        f"# INDEX — generated view for tier `{view.tier}` (HEAD {str(view.brain.head_revision_id)[:12]})",
        "",
        "One line per entity visible at this tier. Format:",
        "`- [kind] title — description | status | last-verified | path`",
        "",
    ]
    sections = {
        "Identity": {"identity"},
        "Projects": {"project"},
        "Knowledge": {"take", "story", "lesson", "fact"},
        "Catalogs & lenses": {"catalog", "lens"},
    }
    for section, kinds in sections.items():
        rows = [e for e in view.entities if e.kind in kinds]
        if not rows:
            continue
        lines.append(f"## {section}")
        for entity in sorted(rows, key=lambda e: (e.kind, e.entity_id)):
            row = note_row(entity, view.tier)
            title = row["title"].replace("\n", " ")
            desc = (row["description"] or title).replace("\n", " ")
            parts = [f"- [{entity.kind}] {title} — {desc}"]
            if row["status"] and row["status"] != "current":
                parts.append(f"status: {row['status']}")
            if row["last_verified"]:
                parts.append(f"last-verified: {row['last_verified']}")
            parts.append(row["path"])
            lines.append(" | ".join(parts))
        lines.append("")
    return "\n".join(lines)


def read(ctx, params, operation):
    view = snapshot(ctx, params, operation=operation)
    tier = view.tier
    if operation == "get-index":
        return {"tier": tier, "index": generated_index(view)}
    if operation == "list-notes":
        rows = view.entities
        for field in ("kind", "topic", "project"):
            value = params.get(field, "")
            if value:
                attr = {"topic": "topics", "project": "projects"}.get(field, field)
                rows = (
                    [e for e in rows if value in [at_tier(v, tier) for v in getattr(e, attr)]]
                    if field != "kind"
                    else [e for e in rows if e.kind == value]
                )
        status = params.get("status", "current")
        if status == "current":
            rows = [e for e in rows if e.status != "superseded"]
        elif status == "superseded":
            rows = [e for e in rows if e.status == status]
        notes = [note_row(e, tier) for e in sorted(rows, key=lambda e: (e.kind, e.entity_id))]
        return {"tier": tier, "count": len(notes), "notes": notes}
    if operation == "get-identity":
        return {
            "tier": tier,
            "files": [
                {"entity_id": e.entity_id, "path": e.path, "content": view.files[e.path]}
                for e in sorted(view.entities, key=lambda e: e.path)
                if e.kind == "identity"
            ],
        }
    if operation == "get-raw":
        path = params["path"]
        if not path.startswith("raw/") or path not in view.files:
            raise unknown()
        return {"tier": tier, "path": path, "content": view.files[path]}
    identifier = params.get("entity_id") if operation == "get-note" else f"lens-{params['name']}"
    entity = next((e for e in view.entities if e.entity_id == identifier), None)
    if entity is None or (operation == "get-lens" and entity.kind != "lens"):
        raise unknown()
    if operation == "get-lens":
        return {"tier": tier, "name": params["name"], "content": view.files[entity.path]}
    return {
        "tier": tier,
        "entity_id": entity.entity_id,
        "path": entity.path,
        "content": view.files[entity.path],
    }
