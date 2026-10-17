"""Relative-time filters that read naturally under a minute (U27).

``{{ dt|ago }}``   → "just now" / "5 minutes ago"
``{{ dt|until }}`` → "in under a minute" / "in 5 minutes"
"""

from django import template
from django.utils import timezone
from django.utils.timesince import timesince, timeuntil

register = template.Library()


@register.filter
def ago(value):
    if not value:
        return ""
    if (timezone.now() - value).total_seconds() < 60:
        return "just now"
    return f"{timesince(value)} ago"


@register.filter
def until(value):
    if not value:
        return ""
    if (value - timezone.now()).total_seconds() < 60:
        return "in under a minute"
    return f"in {timeuntil(value)}"


@register.filter
def in_tz(value, tzname):
    """``3 Mar, 07:00 (Asia/Tokyo)`` in the schedule's zone; the year only when
    it isn't this year (U29: next run was printed in the instance zone, unlabelled)."""
    if not value:
        return ""
    import zoneinfo

    try:
        zone = zoneinfo.ZoneInfo(tzname) if tzname else timezone.get_current_timezone()
    except Exception:
        zone = timezone.get_current_timezone()
    local = timezone.localtime(value, zone)
    year = "" if local.year == timezone.localtime(timezone.now(), zone).year else f" {local.year}"
    return f"{local.day} {local:%b}{year}, {local:%H:%M} ({tzname or zone})"


@register.filter
def thousands(value):
    """``1000000`` → ``1,000,000`` (U22)."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return value
