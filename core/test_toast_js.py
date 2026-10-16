"""
Console toasts (static/js/toast.js) — 2.0 release test §19.04/§19.02 (F67).

Every Django message is shown through ``Toast.show``. Two defects:

* The message was interpolated into ``innerHTML``. ``escapejs`` in base.html
  only makes it a safe JS string; once evaluated, ``<img onerror=…>`` in a
  script, store or brain name ran in the browser of whoever saw the message
  (e.g. an admin archiving a member's script).
* With five toasts showing, a sixth looped forever: ``dismiss()`` only removes
  the element after its 200 ms exit animation, so ``children.length`` never
  shrank inside the ``while``. A restore with several warnings froze the tab.

There is no JS test runner in this repo; these tests pin the two properties
in the source (the behaviour was checked in Chromium for the fix).
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

TOAST_JS = Path(settings.BASE_DIR) / "static" / "js" / "toast.js"


class ToastScriptTests(SimpleTestCase):
    def setUp(self):
        self.src = TOAST_JS.read_text(encoding="utf-8")

    def test_message_is_set_as_text_not_html(self):
        self.assertNotIn("${message}", self.src)
        self.assertRegex(self.src, r"\[data-toast-message\]'\)\.textContent\s*=\s*String\(message\)")

    def test_the_max_toasts_loop_counts_only_toasts_still_showing(self):
        loop = re.search(r"while \((.+?)\) \{(.+?)\}", self.src, re.S)
        self.assertIsNotNone(loop)
        condition, body = loop.group(1), loop.group(2)
        self.assertNotIn("children.length", condition)
        self.assertIn("showing", condition)
        # the body must re-read what is still showing, or the loop cannot end
        self.assertIn("this.showing()", body)
        self.assertIn("toast.dataset.dismissing = '1'", self.src)
