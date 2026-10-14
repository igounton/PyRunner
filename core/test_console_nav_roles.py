"""Navigation shows each role only what it can open (sidebar, drawer, environment actions)."""

from django.test import TestCase
from django.urls import reverse

from core.models import Environment, User, Workspace, WorkspaceMembership
from core.test_plugin_api import _mock_setup

SUPERUSER_ONLY = ("/cpanel/services/", "/cpanel/settings/", "/cpanel/logs/")


class SidebarByRoleTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.default = Workspace.get_default() or Workspace.objects.create(name="Default", is_default=True)
        self.env = Environment.objects.create(name="shared-env", path="nav-env")

    def _user(self, email, role=None, superuser=False):
        user = User.objects.create(email=email, is_superuser=superuser)
        if role:  # a signal already made them a plain member of the default workspace
            WorkspaceMembership.objects.update_or_create(
                user=user, workspace=self.default, defaults={"role": role}
            )
        self.client.force_login(user)
        return user

    def test_member_sees_no_superuser_links_or_environment_actions(self):
        self._user("member@nav.test", role=WorkspaceMembership.ROLE_MEMBER)
        html = self.client.get(reverse("cpanel:dashboard")).content.decode()
        for link in SUPERUSER_ONLY + ("/cpanel/workspaces/",):
            self.assertNotIn(f'href="{link}"', html)
        self.assertNotIn(reverse("cpanel:environment_create"), self.client.get(reverse("cpanel:environment_list")).content.decode())
        detail = self.client.get(reverse("cpanel:environment_detail", args=[self.env.pk])).content.decode()
        self.assertNotIn(reverse("cpanel:environment_edit", args=[self.env.pk]), detail)
        packages = self.client.get(reverse("cpanel:environment_packages", args=[self.env.pk])).content.decode()
        self.assertNotIn(reverse("cpanel:package_install", args=[self.env.pk]), packages)
        self.assertIn("only an administrator can install", packages)

    def test_workspace_admin_gets_a_workspaces_link(self):
        self._user("admin@nav.test", role=WorkspaceMembership.ROLE_ADMIN)
        html = self.client.get(reverse("cpanel:dashboard")).content.decode()
        self.assertIn(f'href="{reverse("cpanel:workspace_list")}"', html)
        for link in SUPERUSER_ONLY:
            self.assertNotIn(f'href="{link}"', html)

    def test_superuser_keeps_every_link_and_action(self):
        self._user("root@nav.test", superuser=True)
        html = self.client.get(reverse("cpanel:dashboard")).content.decode()
        for link in SUPERUSER_ONLY + ("/cpanel/workspaces/",):
            self.assertIn(f'href="{link}"', html)
        packages = self.client.get(reverse("cpanel:environment_packages", args=[self.env.pk])).content.decode()
        self.assertIn(reverse("cpanel:package_install", args=[self.env.pk]), packages)

    def test_drawer_lists_workspaces_for_phones(self):
        user = self._user("two@nav.test", role=WorkspaceMembership.ROLE_MEMBER)
        team = Workspace.objects.create(name="Team Two")
        WorkspaceMembership.ensure(user, team, role=WorkspaceMembership.ROLE_MEMBER)
        html = self.client.get(reverse("cpanel:dashboard")).content.decode()
        drawer = html.split('<div class="lg:hidden pb-3 mb-2 border-b border-line">', 1)[1].split("</div>\n        <div", 1)[0]
        self.assertIn(reverse("cpanel_ws:dashboard", kwargs={"workspace_id": team.id}), drawer)
        self.assertIn(reverse("cpanel_ws:dashboard", kwargs={"workspace_id": self.default.id}), drawer)
