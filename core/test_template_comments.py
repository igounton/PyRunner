"""Django's ``{# ... #}`` comment is single-line only (D14 item 3).

A ``{#`` whose ``#}`` sits on a later line is not a comment: Django renders the
whole block as page text (it showed on brand_tracker's public report). Multi-line
comments must use ``{% comment %} ... {% endcomment %}``. This scans every
template the repo ships (core, examples, plugins) so the mistake can't return.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
SKIP_DIRS = {"venv", ".venv", "node_modules", ".git", ".git-private", "staticfiles", "dist", "data", "__pycache__"}
TEMPLATE_SUFFIXES = {".html", ".txt", ".xml"}


def _template_files():
    for path in ROOT.rglob("*"):
        if path.suffix not in TEMPLATE_SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(ROOT).parts
        if any(p in SKIP_DIRS for p in rel):
            continue
        if "templates" not in rel:
            continue
        yield path


def unclosed_comment_lines(text):
    """Line numbers where a ``{#`` has no ``#}`` after it on the same line."""
    bad = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in re.finditer(r"\{#", line):
            if "#}" not in line[m.end():]:
                bad.append(n)
                break
    return bad


class SingleLineTemplateCommentTests(SimpleTestCase):
    def test_detector_catches_a_multiline_comment(self):
        self.assertEqual(unclosed_comment_lines("{# one\n two #}\n<p>x</p>"), [1])
        self.assertEqual(unclosed_comment_lines("{# ok #} <p>{# also ok #}</p>"), [])

    def test_no_template_opens_a_comment_it_does_not_close_on_the_same_line(self):
        files = list(_template_files())
        self.assertGreater(len(files), 50)  # the scan really found the templates
        offenders = []
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            for n in unclosed_comment_lines(text):
                offenders.append(f"{path.relative_to(ROOT)}:{n}")
        self.assertEqual(
            offenders, [],
            "Multi-line {# #} renders as text; use {% comment %} … {% endcomment %}: " + ", ".join(offenders),
        )
