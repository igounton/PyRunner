"""Profile-backed smart reads over one caller-tier snapshot, with final checks."""

from pathlib import Path

from core.api.exceptions import ApiError
from core.api.schema import validate
from .access import unknown
from .paths import utf8_size
from .reads import note_row, snapshot
from .runtime import run_agent
from .snapshots import materialize, reader_hooks

READER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["context_pack", "entity_ids_used", "gaps"],
    "properties": {
        "context_pack": {"type": "string", "maxLength": 100000},
        "entity_ids_used": {
            "type": "array",
            "maxItems": 100,
            "items": {"type": "string", "maxLength": 200},
        },
        "gaps": {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 4000}},
    },
}


def unchanged(ctx, params, view):
    current = snapshot(ctx, params, operation="list-notes")
    if current.brain.head_revision_id != view.brain.head_revision_id or current.tier != view.tier:
        raise ApiError(
            "brain_changed", "The brain or your access changed. Run the query again.", 409
        )
    return current


def assemble(ctx, params):
    task = params["task"].strip()
    if not task or len(task) > 8000:
        raise ApiError("invalid_input", "Enter a task of 1–8000 characters.")
    utf8_size(task)
    ctx.check_budget()
    view = snapshot(ctx, params)
    lens = params.get("lens", "").strip()
    lens_text = ""
    if lens:
        entity = next(
            (e for e in view.entities if e.kind == "lens" and e.entity_id == f"lens-{lens}"), None
        )
        if entity is None:
            raise unknown()
        lens_text = view.files[entity.path]
    candidates = []
    if view.brain.vector_collection_id:
        from .recall import search

        try:
            candidates = search(ctx, {**params, "query": task, "k": 5})["hits"]
        except ApiError as exc:
            if exc.code not in {"search_unavailable", "search_not_configured"}:
                raise
            # Optional recall cannot disable the ordinary reader. The response
            # reports whether any semantic hints were actually available.
    ctx.check_budget()
    root = materialize(view)
    read_paths = set()
    prompt = f"Assemble context for this untrusted task:\n<task>\n{task}\n</task>"
    if lens_text:
        prompt += f"\n<lens>\n{lens_text}\n</lens>"
    if candidates:
        prompt += "\nSemantic candidates (open before citing):\n" + "\n".join(
            hit["path"] for hit in candidates
        )
    remaining = ctx.remaining_seconds()
    ctx.check_budget()
    result = run_agent(
        ctx.workspace,
        view.brain.pk,
        kind="reader",
        prompt=prompt,
        system=Path(__file__)
        .with_name("prompts")
        .joinpath("reader.md")
        .read_text(encoding="utf-8"),
        schema=READER_SCHEMA,
        cwd=root,
        hooks=reader_hooks(root, read_paths),
        timeout=min(240, remaining) if remaining is not None else 240,
    )
    validate(result.output, READER_SCHEMA)
    current = unchanged(ctx, params, view)
    visible = {e.entity_id: e for e in current.entities}
    ids = list(dict.fromkeys(result.output["entity_ids_used"]))
    if any(
        identifier not in visible or visible[identifier].path not in read_paths
        for identifier in ids
    ):
        # Reject the whole pack: dropping a hidden id would leave its prose in
        # context_pack. Never echo a rejected id to a lower-tier caller.
        raise ApiError(
            "unverified_sources",
            "The reader could not verify its sources. Run the query again.",
            502,
        )
    ctx.check_budget()
    return {
        **result.output,
        "entity_ids_used": ids,
        "unverified_entity_ids": [],
        "sources": [note_row(visible[identifier], view.tier) for identifier in ids],
        "tokens": {
            "input": result.usage.get("input_tokens"),
            "output": result.usage.get("output_tokens"),
        },
        "model": result.model,
        "duration_ms": result.duration_ms,
        "revision": str(view.brain.head_revision_id),
        "semantic_candidates": len(candidates),
    }
