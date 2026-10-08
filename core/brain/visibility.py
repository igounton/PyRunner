"""Tier rules ported from BrainOutside (MIT); no canonical disk snapshots."""

import logging
import re

from .paths import validate_path
from core.api.errors import ApiError

log = logging.getLogger(__name__)
TIERS = ("public", "agents-only", "private")
TIER_ORDER = {tier: rank for rank, tier in enumerate(TIERS)}
_SPAN_OPEN = "(agents-only:"
_RAW_REF_RE = re.compile(r"raw/[A-Za-z0-9_\-./]+?\.md")


def _span_close(text: str, start: int) -> int | None:
    """Index of the paren that closes the span opened at `start`, or None.

    None means the span never closes — the input is malformed, and the
    caller must not guess where the secret ended.
    """
    depth = 0
    for k in range(start, len(text)):
        if text[k] == "(":
            depth += 1
        elif text[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return None


def strip_agents_only_spans(text: str, source: str = "") -> str:
    """Remove `(agents-only: ...)` spans, counting nested parens.

    An unterminated span fails CLOSED: everything from the marker to the
    end of the file is dropped and the file is named in a warning. The
    alternative is guessing where the author meant the secret to end, and
    the old regex's guess — "it isn't a span at all" — published the lot.
    A note that reads short in public can be repaired; one that published
    a private aside cannot be unpublished.
    """
    out: list[str] = []
    i = 0
    while True:
        start = text.find(_SPAN_OPEN, i)
        if start < 0:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:start])
        close = _span_close(text, start)
        if close is None:
            log.warning(
                "snapshot: unterminated '(agents-only:' span in %s — dropping "
                "everything from it to end of file rather than risk publishing "
                "it. Close the parenthesis in the brain repo.",
                source or "<unknown file>",
            )
            return "".join(out)
        i = close + 1


def visible_at(visibility, tier):
    return tier in TIER_ORDER and TIER_ORDER.get(visibility, 2) <= TIER_ORDER[tier]


def raw_paths(files, entities, tier):
    """Raw inherits linking entities' visibility; private includes nested raw."""

    def inside(path):
        try:
            validate_path(path)
        except ApiError:
            return False
        return (
            path.startswith("raw/")
            and path in files
            and path.endswith(".md")
            and path.rsplit("/", 1)[-1] != "README.md"
        )

    if tier == "private":
        return {path for path in files if inside(path)}
    found = set()
    for entity in entities:
        if not visible_at(entity.visibility, tier):
            continue
        text = files.get(entity.path, "")
        if tier == "public":
            text = strip_agents_only_spans(text, entity.path)
        candidates = set(_RAW_REF_RE.findall(text))
        if entity.source.startswith("raw/"):
            candidates.add(entity.source)
        found.update(path for path in candidates if inside(path))
    return found
