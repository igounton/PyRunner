"""Template helpers for console forms (U69 inline errors)."""

from django import template

register = template.Library()


@register.filter
def error_fields(form):
    """The visible bound fields of ``form`` that carry errors, in form order."""
    if not form or not getattr(form, "errors", None):
        return []
    return [field for field in form.visible_fields() if field.errors]
