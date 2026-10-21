"""
Tenancy Stage 4 — RBAC role-gating + member management + workspace-delete-block.

Covers the plan's role matrix (leak rows 16 & 17): a plain Member cannot manage
members or delete a workspace; an Admin can manage members but not delete; an
Owner can delete (but never the default workspace, and never one that still owns
data — SET_NULL would orphan it invisibly); a superuser is cross-workspace. Also
the last-owner guards (a workspace must always keep an owner) and that a
non-member targeting a workspace 404s (no existence disclosure).
"""

from django.contrib.messages import get_messages
from django.db import models
from django.test import TestCase
from django.urls import reverse

from core.models import (
    APIToken,
    Environment,
    Library,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)
from unittest import mock


def _mock_setup(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


def _member(user, workspace, role):
    """Set ``user``'s role in ``workspace`` (overriding the signal's default)."""
    m, _ = WorkspaceMembership.objects.update_or_create(
        user=user, workspace=workspace, defaults={"role": role}
    )
    return m


class _Fixture(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.objects.create(name="Acme")

        self.owner = User.objects.create(email="owner@example.com")
        self.admin = User.objects.create(email="admin@example.com")
        self.plain = User.objects.create(email="member@example.com")
        self.outsider = User.objects.create(email="outsider@example.com")
        self.super = User.objects.create(email="root@example.com", is_superuser=True)

        _member(self.owner, self.ws, WorkspaceMembership.ROLE_OWNER)
        _member(self.admin, self.ws, WorkspaceMembership.ROLE_ADMIN)
        _member(self.plain, self.ws, WorkspaceMembership.ROLE_MEMBER)
        # outsider gets only the signal's default-workspace membership.

    def _login(self, user):
        self.client.force_login(user)


class RenameGatingTests(_Fixture):
    def _rename(self, name="Renamed"):
        return self.client.post(
            reverse("cpanel:workspace_rename", args=[self.ws.id]), {"name": name}
        )

    def test_member_cannot_rename(self):
        self._login(self.plain)
        self.assertEqual(self._rename().status_code, 403)

    def test_admin_can_rename(self):
        self._login(self.admin)
        self.assertEqual(self._rename("ByAdmin").status_code, 302)
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.name, "ByAdmin")

    def test_owner_can_rename(self):
        self._login(self.owner)
        self.assertEqual(self._rename("ByOwner").status_code, 302)

    def test_superuser_can_rename(self):
        self._login(self.super)
        self.assertEqual(self._rename("BySuper").status_code, 302)

    def test_outsider_404(self):
        # Not a member → 404 (no existence disclosure), not 403.
        self._login(self.outsider)
        self.assertEqual(self._rename().status_code, 404)


class MembersPageGatingTests(_Fixture):
    def _members(self):
        return self.client.get(reverse("cpanel:workspace_members", args=[self.ws.id]))

    def test_member_cannot_view(self):
        self._login(self.plain)
        self.assertEqual(self._members().status_code, 403)

    def test_admin_can_view(self):
        self._login(self.admin)
        self.assertEqual(self._members().status_code, 200)

    def test_outsider_404(self):
        self._login(self.outsider)
        self.assertEqual(self._members().status_code, 404)


class MemberManagementTests(_Fixture):
    def test_admin_adds_existing_user(self):
        self._login(self.admin)
        resp = self.client.post(
            reverse("cpanel:workspace_member_add", args=[self.ws.id]),
            {"email": self.outsider.email, "role": "member"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            WorkspaceMembership.objects.filter(
                user=self.outsider, workspace=self.ws
            ).exists()
        )

    def test_member_cannot_add(self):
        self._login(self.plain)
        resp = self.client.post(
            reverse("cpanel:workspace_member_add", args=[self.ws.id]),
            {"email": self.outsider.email, "role": "member"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(
            WorkspaceMembership.objects.filter(
                user=self.outsider, workspace=self.ws
            ).exists()
        )

    def test_add_unknown_email_no_membership(self):
        self._login(self.owner)
        before = WorkspaceMembership.objects.filter(workspace=self.ws).count()
        resp = self.client.post(
            reverse("cpanel:workspace_member_add", args=[self.ws.id]),
            {"email": "ghost@example.com", "role": "member"},
        )
        self.assertEqual(resp.status_code, 302)  # redirect with an error message
        self.assertEqual(
            WorkspaceMembership.objects.filter(workspace=self.ws).count(), before
        )

    def test_change_role(self):
        self._login(self.owner)
        m = WorkspaceMembership.objects.get(user=self.plain, workspace=self.ws)
        resp = self.client.post(
            reverse("cpanel:workspace_member_role", args=[self.ws.id, m.id]),
            {"role": "admin"},
        )
        self.assertEqual(resp.status_code, 302)
        m.refresh_from_db()
        self.assertEqual(m.role, "admin")

    def test_remove_member(self):
        self._login(self.admin)
        m = WorkspaceMembership.objects.get(user=self.plain, workspace=self.ws)
        resp = self.client.post(
            reverse("cpanel:workspace_member_remove", args=[self.ws.id, m.id])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(WorkspaceMembership.objects.filter(pk=m.id).exists())

    def test_cannot_demote_last_owner(self):
        self._login(self.super)
        m = WorkspaceMembership.objects.get(user=self.owner, workspace=self.ws)
        self.client.post(
            reverse("cpanel:workspace_member_role", args=[self.ws.id, m.id]),
            {"role": "member"},
        )
        m.refresh_from_db()
        self.assertEqual(m.role, "owner")  # refused — last owner

    def test_cannot_remove_last_owner(self):
        self._login(self.super)
        m = WorkspaceMembership.objects.get(user=self.owner, workspace=self.ws)
        self.client.post(
            reverse("cpanel:workspace_member_remove", args=[self.ws.id, m.id])
        )
        self.assertTrue(WorkspaceMembership.objects.filter(pk=m.id).exists())

    def test_can_remove_owner_when_another_exists(self):
        self._login(self.super)
        _member(self.admin, self.ws, WorkspaceMembership.ROLE_OWNER)  # 2nd owner
        m = WorkspaceMembership.objects.get(user=self.owner, workspace=self.ws)
        resp = self.client.post(
            reverse("cpanel:workspace_member_remove", args=[self.ws.id, m.id])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(WorkspaceMembership.objects.filter(pk=m.id).exists())


class OwnerRoleGuardTests(_Fixture):
    """Only an Owner (or superuser) grants, changes or removes the Owner role."""

    def _role(self, user, role):
        m = WorkspaceMembership.objects.get(user=user, workspace=self.ws)
        self.client.post(reverse("cpanel:workspace_member_role", args=[self.ws.id, m.id]), {"role": role})
        m.refresh_from_db()
        return m.role

    def test_admin_cannot_promote_themselves_or_others_to_owner(self):
        self._login(self.admin)
        self.assertEqual(self._role(self.admin, "owner"), "admin")
        self.assertEqual(self._role(self.plain, "owner"), "member")
        self.assertEqual(self._role(self.plain, "admin"), "admin")  # other roles still fine

    def test_admin_cannot_add_an_owner(self):
        self._login(self.admin)
        self.client.post(
            reverse("cpanel:workspace_member_add", args=[self.ws.id]),
            {"email": self.outsider.email, "role": "owner"},
        )
        self.assertFalse(
            WorkspaceMembership.objects.filter(user=self.outsider, workspace=self.ws).exists()
        )

    def test_admin_cannot_demote_or_remove_an_owner(self):
        _member(self.plain, self.ws, WorkspaceMembership.ROLE_OWNER)  # not the last owner
        self._login(self.admin)
        self.assertEqual(self._role(self.owner, "member"), "owner")
        m = WorkspaceMembership.objects.get(user=self.owner, workspace=self.ws)
        self.client.post(reverse("cpanel:workspace_member_remove", args=[self.ws.id, m.id]))
        self.assertTrue(WorkspaceMembership.objects.filter(pk=m.id).exists())

    def test_owner_and_superuser_manage_owners(self):
        self._login(self.owner)
        self.assertEqual(self._role(self.admin, "owner"), "owner")
        self._login(self.super)
        self.assertEqual(self._role(self.admin, "admin"), "admin")

    def test_admin_page_offers_no_owner_controls(self):
        self._login(self.admin)
        html = self.client.get(reverse("cpanel:workspace_members", args=[self.ws.id])).content.decode()
        self.assertNotIn('<option value="owner"', html)
        self._login(self.owner)
        html = self.client.get(reverse("cpanel:workspace_members", args=[self.ws.id])).content.decode()
        self.assertIn('<option value="owner"', html)


class DeleteTests(_Fixture):
    def _delete(self):
        return self.client.post(reverse("cpanel:workspace_delete", args=[self.ws.id]))

    def test_member_cannot_delete(self):
        self._login(self.plain)
        self.assertEqual(self._delete().status_code, 403)
        self.assertTrue(Workspace.objects.filter(pk=self.ws.id).exists())

    def test_admin_cannot_delete(self):
        # Delete is Owner-only — an Admin is refused.
        self._login(self.admin)
        self.assertEqual(self._delete().status_code, 403)
        self.assertTrue(Workspace.objects.filter(pk=self.ws.id).exists())

    def test_owner_deletes_empty_workspace(self):
        self._login(self.owner)
        self.assertEqual(self._delete().status_code, 302)
        self.assertFalse(Workspace.objects.filter(pk=self.ws.id).exists())

    def test_delete_blocked_if_non_empty(self):
        # Row 16: a workspace that still owns data cannot be deleted (SET_NULL
        # would orphan the rows where strict scoping makes them invisible).
        env = Environment.objects.create(name="e", path="s4env")
        Script.objects.create(name="s", code="x", environment=env, workspace=self.ws)
        self._login(self.owner)
        self.assertEqual(self._delete().status_code, 302)  # redirect w/ error
        self.assertTrue(Workspace.objects.filter(pk=self.ws.id).exists())

    def test_delete_blocked_by_library_and_token(self):
        # F71: a workspace owning only a library and a datastore token was
        # deleted — both rows went to workspace=NULL (invisible), and the
        # token then read the DEFAULT workspace's stores on the datastore API.
        Library.objects.create(key="f71lib", name="F71", workspace=self.ws)
        APIToken.objects.create(
            name="f71", scope="datastores",
            token=APIToken.generate_token(scope="datastores"), workspace=self.ws,
        )
        self._login(self.owner)
        resp = self._delete()
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Workspace.objects.filter(pk=self.ws.id).exists())
        msgs = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(
            msgs,
            ["Cannot delete 'Acme' — it still owns 1 library, 1 API token. "
             "Move or delete its resources first."],
        )
        self.assertFalse(APIToken.objects.filter(workspace__isnull=True).exists())

    def test_every_set_null_workspace_relation_blocks_delete(self):
        # Any new SET_NULL workspace FK must join the delete-block, or deleting
        # the workspace orphans its rows. Environments are shared infra.
        from core.views.workspaces import _OWNED_RELATIONS

        listed = {rel for rel, _one, _many in _OWNED_RELATIONS}
        set_null = {
            rel.get_accessor_name()
            for rel in Workspace._meta.related_objects
            if rel.on_delete is models.SET_NULL and rel.related_model is not Environment
        }
        self.assertTrue(set_null)
        self.assertEqual(set_null - listed, set())

    def test_cannot_delete_default(self):
        default = Workspace.get_default()
        _member(self.owner, default, WorkspaceMembership.ROLE_OWNER)
        self._login(self.owner)
        resp = self.client.post(
            reverse("cpanel:workspace_delete", args=[default.id])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Workspace.objects.filter(pk=default.id).exists())

    def test_outsider_delete_404(self):
        self._login(self.outsider)
        self.assertEqual(self._delete().status_code, 404)


class CreateAndListTests(_Fixture):
    def test_member_cannot_create(self):
        self._login(self.plain)
        before = Workspace.objects.count()
        resp = self.client.post(
            reverse("cpanel:workspace_create"), {"name": "Sneaky"}
        )
        self.assertEqual(resp.status_code, 403)  # superuser_required → console 403 page
        self.assertEqual(Workspace.objects.count(), before)

    def test_superuser_can_create(self):
        self._login(self.super)
        resp = self.client.post(
            reverse("cpanel:workspace_create"), {"name": "NewCo"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Workspace.objects.filter(name="NewCo").exists())

    def test_list_shows_only_members_workspaces(self):
        other = Workspace.objects.create(name="NotMine")
        self._login(self.plain)
        body = self.client.get(reverse("cpanel:workspace_list")).content.decode()
        self.assertIn("Acme", body)
        self.assertNotIn("NotMine", body)

    def test_member_count_counts_every_member(self):
        # F72: a non-superuser saw "1 member" on every workspace — the count
        # reused for_user()'s join on the caller's own membership.
        self._login(self.plain)
        resp = self.client.get(reverse("cpanel:workspace_list"))
        acme = next(w for w in resp.context["workspaces"] if w.pk == self.ws.pk)
        self.assertEqual(acme.member_count, 3)
        self.assertIn("3 members", resp.content.decode())

    def test_create_button_superuser_only(self):
        self._login(self.plain)
        body = self.client.get(reverse("cpanel:workspace_list")).content.decode()
        self.assertNotIn("New workspace", body)
        self._login(self.super)
        body = self.client.get(reverse("cpanel:workspace_list")).content.decode()
        self.assertIn("New workspace", body)
