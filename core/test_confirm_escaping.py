"""
2.0 release test 25.02 (F89): names inside inline confirm() handlers.

Several delete/archive buttons built their confirm text as a JavaScript string inside an
HTML attribute, e.g. onsubmit="return confirm('Archive {{ script.name }}?')". Django's
HTML autoescape turns ' into &#x27;, but the browser decodes the attribute before it runs
the handler, so a quote in a name ended the JS string. Those values now go through
escapejs, which is safe in both contexts.
"""

import re
from pathlib import Path

from django.conf import settings
from django.urls import reverse

from core.models import DataStore, DataStoreEntry, Environment, Script, Tag
from core.test_polish_admin import AdminBase

NAME = "It's \"quoted\" <b>"
HANDLER = re.compile(r'on(?:submit|click)="([^"]*confirm\([^"]*)"')


def handlers(html):
    return [h for h in HANDLER.findall(html)]


class ConfirmHandlerEscapingTests(AdminBase):
    def assert_escaped(self, html, needle):
        found = [h for h in handlers(html) if needle in h]
        self.assertTrue(found, "no confirm() handler mentions the object")
        for h in found:
            # escapejs output: no HTML-escaped quote the browser would turn back into '
            self.assertNotIn("&#x27;", h)
            self.assertIn("\\u0027", h)

    def test_script_detail_archive_and_delete(self):
        script = Script.objects.create(
            name=NAME, code="print(1)", workspace=self.ws, environment=Environment.objects.get(name="default")
        )
        html = self.client.get(reverse("cpanel:script_detail", args=[script.pk])).content.decode()
        self.assert_escaped(html, "Archive It")

    def test_tag_list(self):
        Tag.objects.create(name=NAME)
        html = self.client.get(reverse("cpanel:tag_list")).content.decode()
        self.assert_escaped(html, "delete the tag")

    def test_datastore_list_and_entry(self):
        store = DataStore.objects.create(name="w26_store", workspace=self.ws)
        entry = DataStoreEntry(datastore=store, key=NAME)
        entry.set_value({"v": 1})
        entry.save()
        html = self.client.get(reverse("cpanel:datastore_detail", args=[store.pk])).content.decode()
        self.assert_escaped(html, "Delete entry")


class NoBareNamesInConfirmHandlersTests(AdminBase):
    """Every template: a {{ var }} inside an inline confirm() handler must use escapejs."""

    NUMERIC = {"entries.count", "rev.version", "revision.version", "items|length", "unread"}

    def test_templates(self):
        roots = [Path(d) for d in settings.TEMPLATES[0]["DIRS"]]
        roots += [Path(settings.BASE_DIR) / "examples", Path(settings.BASE_DIR) / "plugins"]
        bad = []
        for root in roots:
            for path in root.rglob("*.html"):
                for h in handlers(path.read_text(encoding="utf-8")):
                    for var in re.findall(r"\{\{\s*([^}]+?)\s*\}\}", h):
                        if "escapejs" not in var and var not in self.NUMERIC:
                            bad.append(f"{path.name}: {var}")
        self.assertEqual(bad, [])
