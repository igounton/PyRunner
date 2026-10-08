"""Owner-reviewed repository changes, distinct from agent-generated proposals."""

from copy import deepcopy
import json

from core.api.exceptions import ApiError
from core.api.schema import validate as validate_schema
from .contract import validate_contract
from .indexer import DuplicateEntityId, parse_files, parse_frontmatter
from .paths import MAX_IMPORT_BYTES, utf8_size, validate_files, validate_path
from .proposal import PROPOSAL_SCHEMA
from .validator import ValidationResult, Violation
from .visibility import _span_close

SCHEMA = deepcopy(PROPOSAL_SCHEMA)
SCHEMA["properties"]["files"]["items"]["properties"]["action"]["enum"].append("delete")


def shape(proposal):
    validate_schema(proposal, SCHEMA)
    utf8_size(json.dumps(proposal, ensure_ascii=False))
    seen, size = set(), 0
    for row in proposal["files"]:
        validate_path(row["path"])
        if row["path"].casefold() in seen:
            raise ApiError("invalid_proposal", "Sync proposals cannot repeat a path.")
        seen.add(row["path"].casefold())
        size += utf8_size(row["content"])
        if row["action"] == "delete" and row["content"]:
            raise ApiError("invalid_proposal", "Deleted files must have empty content.")
    if size > MAX_IMPORT_BYTES:
        raise ApiError("too_large", "Sync proposals may contain at most 50 MB.", 413)
    if proposal.get("supersedes") or proposal.get("taxonomy_additions"):
        raise ApiError("invalid_proposal", "Sync proposals contain exact file changes only.")


def materialize(before, proposal):
    shape(proposal)
    after = dict(before)
    for row in proposal["files"]:
        if row["action"] == "delete":
            after.pop(row["path"], None)
        else:
            after[row["path"]] = row["content"]
    return after


def validate(before, proposal):
    result = ValidationResult(warnings=[
        "Repository sync can change the contract, identity and visibility, and delete files. Review every diff; no AI extraction is performed."
    ])
    try:
        after = materialize(before, proposal)
        if validate_files(after) > MAX_IMPORT_BYTES:
            raise ApiError("too_large", "The resulting brain exceeds 50 MB.")
        validate_contract(after)
        entities = parse_files(after)
        if len({entity.entity_id for entity in entities}) != len(entities):
            raise ApiError("invalid_proposal", "The resulting brain has duplicate entity IDs.")
        for row in proposal["files"]:
            if row["action"] == "delete":
                continue
            _, _, malformed = parse_frontmatter(row["content"].lstrip("\ufeff"))
            if malformed:
                raise ApiError("invalid_proposal", f"Invalid frontmatter in {row['path']}.")
            start = 0
            while (start := row["content"].find("(agents-only:", start)) >= 0:
                end = _span_close(row["content"], start)
                if end is None:
                    raise ApiError("invalid_proposal", f"Unterminated agents-only span in {row['path']}.")
                start = end + 1
    except (ApiError, DuplicateEntityId) as exc:
        result.violations.append(Violation(0, "sync", getattr(exc, "message", str(exc))))
    return result
