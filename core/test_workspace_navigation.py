"""Links and redirects under /cpanel/w/<id>/ stay in that workspace (core/workspace_urls.py)."""

import re
import uuid

from django.test import TestCase

from core.models import DataStore, User, Workspace, WorkspaceMembership
from core.test_plugin_api import _mock_setup
from core.workspace_urls import _build


def _console_links(response):
    return set(re.findall(r'(?:href|action)="(/cpanel/[^"]*)"', response.content.decode()))


class WorkspaceNavigationTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.default = Workspace.get_default() or Workspace.objects.create(name="Default", is_default=True)
        self.team = Workspace.objects.create(name="Team")
        self.owner = User.objects.create_user(username="owner", email="owner@nav.test")
        WorkspaceMembership.ensure(self.owner, self.default, role="owner")
        WorkspaceMembership.ensure(self.owner, self.team, role="owner")
        self.client.force_login(self.owner)
        self.prefix = f"/cpanel/w/{self.team.pk}/"

    def test_every_console_link_under_the_prefix_stays_in_the_workspace(self):
        for page in ("", "scripts/", "datastores/", "secrets/", "runs/"):
            with self.subTest(page=page):
                links = _console_links(self.client.get(self.prefix + page))
                self.assertTrue(links)
                self.assertEqual(
                    {link for link in links if not link.startswith("/cpanel/w/")}, set()
                )
                self.assertIn(self.prefix + "scripts/", links)

    def test_redirect_after_create_stays_in_the_workspace_and_opens(self):
        response = self.client.post(self.prefix + "datastores/create/", {"name": "team_store"})
        store = DataStore.objects.get(name="team_store")
        self.assertEqual(store.workspace, self.team)
        self.assertEqual(response["Location"], f"{self.prefix}datastores/{store.pk}/")
        self.assertEqual(self.client.get(response["Location"]).status_code, 200)

    def test_unprefixed_pages_keep_plain_links(self):
        links = _console_links(self.client.get("/cpanel/scripts/"))
        self.assertIn("/cpanel/scripts/", links)
        response = self.client.post("/cpanel/datastores/create/", {"name": "default_store"})
        store = DataStore.objects.get(name="default_store")
        self.assertEqual(store.workspace, self.default)
        self.assertEqual(response["Location"], f"/cpanel/datastores/{store.pk}/")

    def test_inaccessible_or_unknown_workspace_404s_without_a_new_urlconf(self):
        outsider = User.objects.create_user(username="outsider", email="out@nav.test")
        WorkspaceMembership.ensure(outsider, self.default, role="member")
        self.client.get(self.prefix)  # build the owner's module first
        built = _build.cache_info().currsize
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(self.prefix + "scripts/").status_code, 404)
        for _ in range(3):
            self.assertEqual(self.client.get(f"/cpanel/w/{uuid.uuid4()}/scripts/").status_code, 404)
        self.assertEqual(_build.cache_info().currsize, built)

    def test_anonymous_prefixed_request_goes_to_login(self):
        self.client.logout()
        response = self.client.get(self.prefix + "scripts/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/login/", response["Location"])
