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
