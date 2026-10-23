"""What templates and static JS send to browsers carries no internal notes.

Internal ticket ids (``U58``, ``F22``) and "release test" references are fine in
Django comments, which never render; anywhere else in a template or in
static/js they reach every visitor's browser (F135).
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
DJANGO_COMMENT = re.compile(r"\{#.*?#\}|\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", re.S)
TICKET = re.compile(r"\b[UF]\d{1,3}\b|release test")


def shipped_files():
    for path in [*ROOT.glob("templates/**/*.html"), *ROOT.glob("static/js/*.js")]:
        if ".min." not in path.name:
            yield path


def shipped_text(path):
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".html":
        text = DJANGO_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    return text


class ShippedTextTests(SimpleTestCase):
    def test_no_internal_ticket_ids_reach_the_browser(self):
        hits = []
        for path in shipped_files():
            for number, line in enumerate(shipped_text(path).splitlines(), 1):
                if TICKET.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:80]}")
        self.assertEqual(hits, [])
