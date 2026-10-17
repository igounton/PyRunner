"""
2.0 release test §26 (responsive pass) and §27 (accessibility quick pass).

F94   the header overflowed at the 640 / 768 / 1024 px breakpoints (page 28 / 26 / 16 px wider than
      the screen, Logout cut off): the changelog icon and version label now appear from md, the
      Discord / Docs / GitHub icons from xl, and the update pill is icon-only below xl
F95   Workspaces list at 390 px: each row was 526 px wide and its actions covered the name; the
      actions now wrap under the name below xl. Users → Pending invites: the inviter's email ran
      under the buttons; the row stacks below sm and the line breaks anywhere
F96   axe-core: the five Services card toggles had no accessible name (critical "button-name"), and
      the header and sidebar navs were two unnamed nav landmarks ("landmark-unique")
"""

from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.models import Environment, GlobalSettings, User, UserInvite, Workspace

PW = "correct-horse-battery-staple-91"


class ResponsiveBase(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        self.admin = User.objects.create_user(
            username="admin@example.com", email="admin@example.com", password=PW,
            is_superuser=True, is_staff=True,
        )
        self.client.force_login(self.admin)


class HeaderBreakpointTests(ResponsiveBase):
    def html(self):
        return self.client.get(reverse("cpanel:dashboard")).content.decode()

    def test_external_icons_only_from_xl(self):
        html = self.html()
        for title in ("Discord", "Docs", "GitHub"):
            self.assertRegex(html, r'class="w-9 h-9 hidden xl:grid [^"]*" title="%s"' % title)
        self.assertNotIn("hidden md:grid place-items-center rounded-lg border border-line", html)

    def test_changelog_icon_and_version_label_from_md(self):
        html = self.html()
        self.assertIn('title="What\'s new"\n                       class="w-9 h-9 hidden md:grid', html)
        self.assertIn("hidden md:inline whitespace-nowrap\">v", html)
        self.assertNotIn("hidden sm:inline\">v", html)

    def test_update_pill_is_icon_only_below_xl_and_hidden_on_phones(self):
        req = RequestFactory().get("/")
        req.user = self.admin
        html = render_to_string("base.html", {
            "update_available": True, "update_latest_version": "9.9.9", "pyrunner_version": "2.0.0",
        }, request=req)
        self.assertIn("hidden sm:inline-flex items-center gap-1 rounded-full bg-warn/10", html)
        self.assertIn('<span class="hidden xl:inline">Update to 9.9.9</span>', html)


class WorkspaceAndInviteRowTests(ResponsiveBase):
    def test_workspace_rows_stack_below_xl(self):
        Workspace.objects.create(name="Second workspace")
        html = self.client.get(reverse("cpanel:workspace_list")).content.decode()
        self.assertEqual(html.count("flex flex-col xl:flex-row xl:items-center justify-between"), 2)
        self.assertIn("flex flex-wrap items-center gap-2 xl:flex-shrink-0", html)

    def test_invite_row_stacks_and_inviter_email_wraps(self):
        UserInvite.create_invite(email="someone@example.invalid", created_by=self.admin)
        html = self.client.get(reverse("cpanel:user_list")).content.decode()
        self.assertIn("flex flex-col sm:flex-row sm:items-center justify-between gap-3", html)
        self.assertIn('<p class="text-xs text-faint [overflow-wrap:anywhere]">Invited', html)


class AccessibleNamesTests(ResponsiveBase):
    def test_services_card_toggles_have_names(self):
        html = self.client.get(reverse("cpanel:services")).content.decode()
        for what in ("object storage", "AI provider", "secret provider", "embedding provider", "vector store"):
            self.assertIn('aria-label="Show or hide the %s settings"' % what, html)

    def test_nav_landmarks_are_named(self):
        html = self.client.get(reverse("cpanel:dashboard")).content.decode()
        self.assertIn('<nav class="flex items-center gap-1.5" aria-label="Account and tools">', html)
        self.assertIn('aria-label="Main">', html)
