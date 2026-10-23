"""F132: every escapeHtml helper shipped to the browser escapes quotes too.

The div.textContent/innerHTML idiom escapes only & < >; its result was also
placed inside HTML attributes, where a " or ' ends the attribute.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
DEFINITION = re.compile(r"function escapeHtml\s*\([^)]*\)\s*\{(.*?)\n\s*\}", re.S)


class EscapeHelperTests(SimpleTestCase):
    def test_every_escape_helper_escapes_both_quotes(self):
        found = 0
        for path in [*ROOT.glob("templates/**/*.html"), *ROOT.glob("static/js/*.js")]:
            for body in DEFINITION.findall(path.read_text(encoding="utf-8")):
                found += 1
                with self.subTest(path=str(path.relative_to(ROOT))):
                    self.assertIn("&quot;", body)
                    self.assertIn("&#39;", body)
                    self.assertNotIn("innerHTML", body)
        self.assertGreaterEqual(found, 5)
