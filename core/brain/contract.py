"""The shipped contract is a seed; imported content is never rewritten."""

import re
from .indexer import parse_files, parse_frontmatter
from core.api.errors import ApiError

CONTRACT_VERSION = "1.1"


def contract_version(files):
    metadata, _, malformed = parse_frontmatter(files.get("CLAUDE.md", "").lstrip("\ufeff"))
    return "" if malformed else str(metadata.get("contract-version") or "")[:32]


def validate_contract(files):
    missing = [path for path in ("CLAUDE.md",) if path not in files]
    missing += [
        prefix
        for prefix in ("identity/", "knowledge/", "lenses/")
        if not any(path.startswith(prefix) for path in files)
    ]
    if missing:
        raise ApiError("invalid_contract", "Missing brain contract paths: " + ", ".join(missing))
    return contract_version(files)


def version_warning(version):
    if version == CONTRACT_VERSION:
        return ""
    return f"This brain uses contract {version or 'unknown'}; PyRun ships {CONTRACT_VERSION}. Imported notes are preserved."


def harvest_descriptions(files):
    """One-time compatibility with the three historical INDEX line dialects."""
    descriptions = {}
    entities = parse_files(files)
    by_id = {entity.entity_id: entity.path for entity in entities}
    by_path = {entity.path: entity.path for entity in entities}
    for line in files.get("INDEX.md", "").splitlines():
        historical = re.match(r"^- \[[a-z]+\] [^—|]+—\s*([^|]+)", line)
        if historical and "|" in line:
            path = line.rsplit("|", 1)[-1].strip().split(":", 1)[-1].strip()
            if path in by_path:
                descriptions[path] = historical.group(1).strip()
                continue
        match = re.match(r"\s*[-*]\s+`([^`]+)`\s*(?:[—–-]|:)\s*(.+)", line)
        if not match:
            match = re.match(r"\s*[-*]\s+\[([^]]+)\]\(([^)]+)\)\s*(?:[—–-]|:)\s*(.+)", line)
            if match:
                key, description = match.group(2), match.group(3)
            else:
                continue
        else:
            key, description = match.groups()
        path = by_id.get(key) or by_path.get(key.removeprefix("./"))
        if path:
            descriptions[path] = re.sub(r"\s*\[[^]]+\]", "", description).strip()
    return descriptions
