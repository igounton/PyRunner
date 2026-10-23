"""What templates and static JS send to browsers carries no internal notes.

Internal ticket ids (``U58``, ``F22``) and "release test" references are fine in
Django comments, which never render; anywhere else in a template or in
static/js they reach every visitor's browser (F135).
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase

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


class NoHtmlCommentsTests(SimpleTestCase):
    # F136: developer notes were <!-- --> comments, sent in every page's source.
    def test_templates_use_django_comments(self):
        hits = [
            f"{path.relative_to(ROOT)}"
            for path in ROOT.glob("templates/**/*.html")
            if "<!--" in shipped_text(path)
        ]
        self.assertEqual(hits, [])


class RenderedPagesHaveNoHtmlCommentsTests(TestCase):
    def setUp(self):
        from unittest import mock

        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            patcher = mock.patch(target, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)
        from core.models import User

        self.client.force_login(
            User.objects.create_superuser(username="su", email="su@x.test", password="pw-12345678!")
        )

    def test_console_pages(self):
        from django.urls import reverse

        for name in ("dashboard", "script_list", "settings", "services"):
            with self.subTest(page=name):
                response = self.client.get(reverse(f"cpanel:{name}"))
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("<!--", response.content.decode())


class NoPrivateDocLinksTests(SimpleTestCase):
    # F137: tracked code pointed readers at gitignored planning/audit docs.
    def test_tracked_files_only_reference_published_docs(self):
        import subprocess

        try:
            tracked = subprocess.run(
                ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
            ).stdout.split()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout")
        published = {p for p in tracked if p.startswith("docs/")}
        reference = re.compile(r"docs/[A-Za-z0-9_.-]+\.md")
        hits = []
        for name in tracked:
            if name.startswith("docs/") or name in {"CHANGELOG.md", ".dockerignore"}:
                continue
            if name == "core/test_docker_context.py" or name.endswith((".png", ".zip", ".ico")):
                continue
            try:
                text = (ROOT / name).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            hits += [f"{name}: {ref}" for ref in reference.findall(text) if ref not in published]
        self.assertEqual(hits, [])


class AnonymousPagesHideVersionTests(TestCase):
    # F138: forgot/reset password, invite, 404 and CSRF pages showed vX.Y.Z.
    def setUp(self):
        from unittest import mock

        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            patcher = mock.patch(target, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_anonymous_pages_do_not_show_the_version(self):
        from django.urls import reverse

        from pyrunner.version import __version__

        for url in (reverse("auth:forgot_password"), reverse("auth:login"), "/no-such-page/"):
            with self.subTest(url=url):
                self.assertNotIn(f"v{__version__}", self.client.get(url).content.decode())

    def test_signed_in_pages_keep_it(self):
        from django.urls import reverse

        from core.models import User
        from pyrunner.version import __version__

        self.client.force_login(
            User.objects.create_superuser(username="v", email="v@x.test", password="pw-12345678!")
        )
        self.assertIn(f"v{__version__}", self.client.get(reverse("cpanel:dashboard")).content.decode())
