"""``{% icon "name" %}`` — inline Lucide SVG icons for the console.

The SVGs are vendored, unmodified, from the ``lucide-static`` npm package at a
pinned version (``LUCIDE_VERSION``) in ``core/icons/lucide/`` together with its
ISC ``LICENSE``. Only the icons the console uses are vendored; to add one, copy
``icons/<name>.svg`` from the same package version into that folder.

The tag renders the icon inline (no CDN, no JavaScript, nothing for the CSP to
allow) with ``stroke="currentColor"`` so it follows the text colour in light and
dark mode, and ``aria-hidden="true"`` because every console icon sits next to a
visible label. Plugin sidebar icons keep their own ``icon_svg`` markup.

Usage::

    {% load icon_tags %}
    {% icon "brain" %}                       {# default size w-[18px] h-[18px] #}
    {% icon "brain" class="w-5 h-5 text-ok" %}
"""

import logging
import re
from functools import lru_cache
from pathlib import Path

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

LUCIDE_VERSION = "0.460.0"
ICON_DIR = Path(__file__).resolve().parent.parent / "icons" / "lucide"
DEFAULT_CLASS = "w-[18px] h-[18px]"
# Shapes Lucide icons are drawn with; anything else in a vendored file is refused.
_SHAPE = re.compile(r"<(path|circle|rect|line|polyline|polygon|ellipse)(\s+[a-z][a-z0-9-]*=\"[^\"<>]*\")*\s*/>")
_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")

logger = logging.getLogger(__name__)
register = template.Library()


@lru_cache(maxsize=None)
def icon_body(name: str) -> str:
    """The icon's inner shapes (validated), or "" for an unknown name."""
    if not _NAME.fullmatch(name or ""):
        return ""
    path = ICON_DIR / f"{name}.svg"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    inner = text[text.index(">", text.index("<svg")) + 1:text.rindex("</svg>")]
    shapes = _SHAPE.findall(inner)
    body = "".join(m.group(0) for m in _SHAPE.finditer(inner))
    leftover = _SHAPE.sub("", inner).strip()
    if leftover or not shapes:
        raise ValueError(f"Unexpected markup in vendored icon {name}.svg")
    return body


def available_icons() -> list[str]:
    return sorted(p.stem for p in ICON_DIR.glob("*.svg"))


@register.simple_tag
def icon(name, **kwargs):
    body = icon_body(name)
    if not body:
        logger.warning("Unknown console icon %r", name)
        return ""
    return format_html(
        '<svg class="{}" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="{}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">{}</svg>',
        kwargs.get("class") or DEFAULT_CLASS,
        kwargs.get("stroke_width", 2),
        mark_safe(body),  # validated shapes from a vendored file, never user input
    )
