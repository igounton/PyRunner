"""One bounded proposal shape for the feeder, editor and final approval gate."""

from core.api.schema import validate
from core.api.exceptions import ApiError
from .paths import MAX_FILES, MAX_IMPORT_BYTES, MAX_RAW_BYTES, utf8_size


def obj(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def text(limit):
    return {"type": "string", "maxLength": limit}


def array(items, limit=MAX_FILES):
    return {"type": "array", "items": items, "maxItems": limit}


PROPOSAL_SCHEMA = obj(
    {
        "source_id": text(120),
        "summary": text(4000),
        "files": array(
            obj(
                {
                    "path": text(300),
                    "action": {"type": "string", "enum": ["create", "update"]},
                    "content": text(MAX_RAW_BYTES),
                },
                ("path", "action", "content"),
            )
        ),
        "supersedes": array(
            obj(
                {"old_entity_id": text(200), "new_entity_id": text(200)},
                ("old_entity_id", "new_entity_id"),
            )
        ),
        "taxonomy_additions": array(text(100), 100),
        "issues": array(text(4000), 100),
    },
    ("files",),
)


def validate_shape(proposal):
    import json

    validate(proposal, PROPOSAL_SCHEMA)
    utf8_size(json.dumps(proposal, ensure_ascii=False))
    size = sum(utf8_size(row["content"]) for row in proposal["files"])
    if size > MAX_IMPORT_BYTES:
        raise ApiError("too_large", "A proposal may contain at most 50 MB of file content.", 413)
    return proposal
