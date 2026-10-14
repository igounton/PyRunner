"""Tags are instance-wide: anyone uses and creates them; only superusers rename or delete."""

from django.test import TestCase
from django.urls import reverse

from core.models import Environment, Script, Tag, User, Workspace, WorkspaceMembership
from core.test_plugin_api import _mock_setup


class TagPermissionTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.default = Workspace.get_default() or Workspace.objects.create(name="Default", is_default=True)
        self.member = User.objects.create(email="member@tags.test")
        WorkspaceMembership.ensure(self.member, self.default, role=WorkspaceMembership.ROLE_MEMBER)
        self.root = User.objects.create(email="root@tags.test", is_superuser=True)
        self.tag = Tag.objects.create(name="shared", color="blue")
        env = Environment.objects.create(name="tags-env")
        other = Workspace.objects.create(name="Other team")
        for name, workspace in (("mine", self.default), ("theirs-1", other), ("theirs-2", other)):
            Script.objects.create(name=name, workspace=workspace, environment=env).tags.add(self.tag)

    def test_member_creates_but_cannot_rename_or_delete(self):
        self.client.force_login(self.member)
        self.client.post(reverse("cpanel:tag_create"), {"name": "mine-too", "color": "red"})
        self.assertTrue(Tag.objects.filter(name="mine-too").exists())
        response = self.client.post(reverse("cpanel:tag_edit", args=[self.tag.pk]), {"name": "x", "color": "red"})
        self.assertTrue(response["Location"].startswith(reverse("auth:login")))
        self.client.post(reverse("cpanel:tag_delete", args=[self.tag.pk]))
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.name, "shared")

    def test_member_list_counts_only_their_workspace_and_hides_actions(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("cpanel:tag_list"))
        self.assertContains(response, "1 script<")
        self.assertNotContains(response, reverse("cpanel:tag_edit", args=[self.tag.pk]))

    def test_superuser_renames_and_deletes(self):
        self.client.force_login(self.root)
        self.assertContains(self.client.get(reverse("cpanel:tag_list")), reverse("cpanel:tag_edit", args=[self.tag.pk]))
        self.client.post(reverse("cpanel:tag_edit", args=[self.tag.pk]), {"name": "renamed", "color": "red"})
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.name, "renamed")
        self.client.post(reverse("cpanel:tag_delete", args=[self.tag.pk]))
        self.assertFalse(Tag.objects.filter(pk=self.tag.pk).exists())
