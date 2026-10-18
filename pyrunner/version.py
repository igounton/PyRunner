"""
PyRun version information.
"""

__version__ = "2.0.0-rc22"

# Whole-app beta flag. Flip to False to drop every "Beta" badge across the UI
# in one place (badges are rendered via templates/_beta_badge.html, gated on the
# pyrunner_is_beta context flag).
IS_BETA = True
