"""Query-string parsing that turns a bad value into a default, never a 500."""


def positive_int(value, default, maximum=None):
    """``value`` as an int of at least 1 (capped at ``maximum``), else ``default``."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < 1:
        return default
    return number if maximum is None else min(number, maximum)
