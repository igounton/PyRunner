"""
Deleting a user must not leave a workspace without an owner (F73, 2.0 release test).

The member pages already refuse to demote or remove a workspace's last owner;
Users → Delete cascaded the memberships away and left the workspace ownerless.
The Users list now also shows each user's workspaces and roles (U79).
"""

from django.test import TestCase
from django.urls import reverse

from core.models import (
    Environment,
    GlobalSettings,
    User,
    Workspace,
    WorkspaceMembership,
)


class UserDeleteLastOwnerGuardTests(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.create(name="env", path="p", is_active=True, is_default=True)
        self.root = User.objects.create(
            email="root@example.com", is_superuser=True, is_staff=True
        )
        WorkspaceMembership.ensure(
            self.root, Workspace.get_default(), role=WorkspaceMembership.ROLE_OWNER
        )
        self.client.force_login(self.root)
        self.ws = Workspace.objects.create(name="Team Six")
        self.owner = User.objects.create(email="owner@example.com")
        WorkspaceMembership.ensure(self.owner, self.ws, role=WorkspaceMembership.ROLE_OWNER)

    def _delete(self, user):
        return self.client.post(reverse("cpanel:delete_user", kwargs={"pk": user.pk}), follow=True)

    def test_sole_owner_cannot_be_deleted(self):
        member = User.objects.create(email="member@example.com")
        WorkspaceMembership.ensure(member, self.ws, role=WorkspaceMembership.ROLE_MEMBER)

        resp = self._delete(self.owner)

        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())
        self.assertTrue(
            WorkspaceMembership.objects.filter(
                workspace=self.ws, user=self.owner, role=WorkspaceMembership.ROLE_OWNER
            ).exists()
        )
        text = " ".join(str(m) for m in resp.context["messages"])
        self.assertIn("only owner of Team Six", text)

    def test_owner_with_a_co_owner_can_be_deleted(self):
        other = User.objects.create(email="co@example.com")
        WorkspaceMembership.ensure(other, self.ws, role=WorkspaceMembership.ROLE_OWNER)

        self._delete(self.owner)

        self.assertFalse(User.objects.filter(pk=self.owner.pk).exists())
        self.assertEqual(
            WorkspaceMembership.objects.filter(
                workspace=self.ws, role=WorkspaceMembership.ROLE_OWNER
            ).count(),
            1,
        )

    def test_plain_member_can_be_deleted(self):
        member = User.objects.create(email="member@example.com")
        WorkspaceMembership.ensure(member, self.ws, role=WorkspaceMembership.ROLE_MEMBER)

        self._delete(member)

        self.assertFalse(User.objects.filter(pk=member.pk).exists())

    def test_users_list_shows_workspaces_and_roles(self):
        resp = self.client.get(reverse("cpanel:user_list"))

        self.assertContains(resp, "Team Six (Owner)")
        self.assertContains(resp, "They are the only owner of: Team Six")
