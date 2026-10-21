"""
Console shell (UI audit UX1 / UX12).

Every console page extends ``cpanel/layout.html``: one sidebar, one wide page
wrapper, one header (breadcrumb, h1, subtitle, actions) and the skip link. These
tests lock that so a new page can't drift back to a hand-copied wrapper with its
own width, and cover the header fixes that came with it.
"""

import re

from django.test import TestCase
from django.urls import reverse

from core.models import Environment, GlobalSettings, User, Workspace, WorkspaceMembership

PW = "correct-horse-battery-staple-91"

# Pages that take no URL arguments; each must render inside the shell.
SHELL_PAGES = [
    "dashboard", "script_list", "run_list", "tasks", "library_list", "library_create",
    "environment_list", "environment_create", "secret_list", "secret_create", "tag_list", "tag_create",
    "datastore_list", "datastore_create", "channel_list", "channel_create", "brain_list", "brain_create",
    "connect", "services", "settings", "workspace_list", "user_list", "logs", "changelog", "plugin_list",
    "plugin_upload", "api_token_list", "api_token_create",
]


class ShellBase(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.get_or_create(name="default", defaults={"is_default": True, "python_version": "3.12"})
        self.ws = Workspace.get_default()
        self.admin = User.objects.create_user(
            username="admin@example.com", email="admin@example.com", password=PW, is_superuser=True, is_staff=True,
        )
        WorkspaceMembership.ensure(self.admin, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.member = User.objects.create_user(username="m@example.com", email="m@example.com", password=PW)
        WorkspaceMembership.ensure(self.member, self.ws, role=WorkspaceMembership.ROLE_MEMBER)


class ShellPagesTests(ShellBase):
    def test_every_page_renders_in_the_one_shell(self):
        self.client.force_login(self.admin)
        for name in SHELL_PAGES:
            with self.subTest(page=name):
                resp = self.client.get(reverse(f"cpanel:{name}"))
                self.assertEqual(resp.status_code, 200)
                html = resp.content.decode()
                self.assertEqual(html.count('id="app-sidebar"'), 1)
                self.assertEqual(html.count('id="main"'), 1)
                self.assertIn('href="#main"', html)  # skip link
                self.assertIn("max-w-[1440px] mx-auto w-full", html)
                # no page re-centres itself in a narrow column any more
                self.assertNotRegex(html, r'py-7 max-w-(2xl|3xl|4xl|\[840px\]) mx-auto')
                self.assertEqual(len(re.findall(r"<h1\b", html)), 1)

    def test_editor_keeps_its_full_height_layout_inside_the_shell(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:script_create")).content.decode()
        self.assertEqual(html.count('id="main"'), 1)
        self.assertIn("sticky top-16", html)
        self.assertNotIn("max-w-[1440px] mx-auto w-full space-y-5", html)

    def test_header_actions_get_their_own_row_on_phones(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:secret_list")).content.decode()
        self.assertIn('<header class="flex flex-col sm:flex-row sm:items-start sm:justify-between', html)
        self.assertIn('<nav aria-label="Breadcrumb" class="crumbs', html)


class HeaderFixTests(ShellBase):
    def test_dashboard_has_a_title_and_health_is_a_pill(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:dashboard")).content.decode()
        self.assertRegex(html, r"<h1 [^>]*>Dashboard</h1>")
        self.assertIn("rounded-full bg-warn/10 text-warn", html)  # no worker in tests -> not "operational"

    def test_members_page_uses_the_breadcrumb_not_a_back_chevron(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:workspace_members", kwargs={"pk": self.ws.pk})).content.decode()
        self.assertIn(f'<a href="{reverse("cpanel:workspace_list")}">Workspaces</a>', html)
        self.assertNotIn('title="Back to workspaces"', html)

    def test_token_breadcrumb_only_links_settings_for_admins(self):
        settings_crumb = f'<a href="{reverse("cpanel:settings")}">Settings</a>'
        self.client.force_login(self.member)
        self.assertNotIn(settings_crumb, self.client.get(reverse("cpanel:api_token_list")).content.decode())
        self.client.force_login(self.admin)
        self.assertIn(settings_crumb, self.client.get(reverse("cpanel:api_token_list")).content.decode())

    def test_brains_list_names_the_section_once(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:brain_list")).content.decode()
        self.assertNotIn('class="text-sm text-muted">Brains</a>', html)
        self.assertRegex(html, r"<h1 [^>]*>Brains</h1>")
