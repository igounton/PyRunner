"""
Tenancy Stage 0 — plumbing (membership + active-workspace resolution + switcher).

Proves the plumbing is correct and, above all, ADDITIVE: a single-workspace
instance is byte-for-byte unchanged (no prefix, switcher hidden), while the
URL-scoped prefix is membership-validated (a non-member 404s — the web tier's
primary new IDOR surface). No scoped query filters by workspace yet (that is the
Stage 3 sweep); these tests cover only the resolution/plumbing layer.
"""

import importlib
import uuid

from django.contrib.auth.models import AnonymousUser
from django.db.models.signals import post_save
from django.http import Http404, HttpResponse
from django.template import Context, Template
from django.test import RequestFactory, TestCase
from django.urls import reverse
from unittest import mock

from core.context_processors import workspaces as workspaces_ctx
from core.middleware import ActiveWorkspaceMiddleware
from core.models import (
    Environment,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)

# Migration module starts with a digit — import dynamically (mirrors the seam test).
_mig = importlib.import_module("core.migrations.0031_membership_backfill")
backfill_memberships = _mig.backfill_memberships
unbackfill_memberships = _mig.unbackfill


def _dummy_view(request):
    return HttpResponse("ok")


class MembershipModelTests(TestCase):
    """WorkspaceMembership.ensure + Workspace.resolve_for / for_user."""

    def setUp(self):
        self.default = Workspace.get_default()
        self.ws_b = Workspace.objects.create(name="B")

    def test_new_user_signal_creates_default_membership(self):
        # The post_save hook lands every new user in the default workspace.
        u = User.objects.create(email="member@example.com")
        m = WorkspaceMembership.objects.get(user=u, workspace=self.default)
        self.assertEqual(m.role, WorkspaceMembership.ROLE_MEMBER)

    def test_new_superuser_signal_is_owner(self):
        u = User.objects.create(email="root@example.com", is_superuser=True)
        m = WorkspaceMembership.objects.get(user=u, workspace=self.default)
        self.assertEqual(m.role, WorkspaceMembership.ROLE_OWNER)

    def test_ensure_is_idempotent_and_upgrades_only(self):
        u = User.objects.create(email="u@example.com")  # member of default via signal
        # Re-ensure as member: no-op.
        WorkspaceMembership.ensure(u, self.default, role=WorkspaceMembership.ROLE_MEMBER)
        self.assertEqual(WorkspaceMembership.objects.filter(user=u).count(), 1)
        # Upgrade to owner sticks; never downgrades back.
        WorkspaceMembership.ensure(u, self.default, role=WorkspaceMembership.ROLE_OWNER)
        WorkspaceMembership.ensure(u, self.default, role=WorkspaceMembership.ROLE_MEMBER)
        self.assertEqual(
            WorkspaceMembership.objects.get(user=u, workspace=self.default).role,
            WorkspaceMembership.ROLE_OWNER,
        )

    def test_for_user_member_vs_superuser(self):
        member = User.objects.create(email="m@example.com")  # default only
        self.assertEqual(set(Workspace.for_user(member)), {self.default})
        root = User.objects.create(email="s@example.com", is_superuser=True)
        # Superuser sees ALL workspaces regardless of membership rows.
        self.assertEqual(set(Workspace.for_user(root)), {self.default, self.ws_b})

    def test_for_user_anonymous_is_empty(self):
        self.assertEqual(list(Workspace.for_user(AnonymousUser())), [])

    def test_resolve_for_requested_member_ok(self):
        u = User.objects.create(email="m@example.com")
        WorkspaceMembership.ensure(u, self.ws_b, role=WorkspaceMembership.ROLE_MEMBER)
        ws, ok = Workspace.resolve_for(u, requested_id=self.ws_b.id)
        self.assertTrue(ok)
        self.assertEqual(ws, self.ws_b)

    def test_resolve_for_requested_non_member_denied(self):
        u = User.objects.create(email="m@example.com")  # NOT in ws_b
        ws, ok = Workspace.resolve_for(u, requested_id=self.ws_b.id)
        self.assertFalse(ok)
        self.assertIsNone(ws)

    def test_resolve_for_requested_unknown_denied(self):
        u = User.objects.create(email="m@example.com")
        ws, ok = Workspace.resolve_for(u, requested_id=uuid.uuid4())
        self.assertFalse(ok)
        self.assertIsNone(ws)

    def test_resolve_for_superuser_any_workspace(self):
        root = User.objects.create(email="s@example.com", is_superuser=True)
        ws, ok = Workspace.resolve_for(root, requested_id=self.ws_b.id)
        self.assertTrue(ok)
        self.assertEqual(ws, self.ws_b)

    def test_resolve_for_bare_returns_default(self):
        u = User.objects.create(email="m@example.com")
        ws, ok = Workspace.resolve_for(u, requested_id=None)
        self.assertTrue(ok)
        self.assertEqual(ws, self.default)


class ForWorkspaceManagerTests(TestCase):
    """The unused-but-added .for_workspace() ergonomic primitive filters correctly."""

    def test_for_workspace_filters(self):
        default = Workspace.get_default()
        other = Workspace.objects.create(name="Other")
        env = Environment.objects.create(name="e", path="wsenv_fw")
        a = Script.objects.create(name="a", code="x", environment=env, workspace=default)
        Script.objects.create(name="b", code="x", environment=env, workspace=other)
        result = list(Script.objects.for_workspace(default))
        self.assertEqual(result, [a])


class MembershipBackfillMigrationTests(TestCase):
    """The 0031 backfill (migration-time path for pre-existing users)."""

    def test_backfill_assigns_roles(self):
        u1 = User.objects.create(email="a@b.com")
        u2 = User.objects.create(email="admin@b.com", is_superuser=True)
        # Clear the signal-created memberships so we exercise the backfill itself.
        WorkspaceMembership.objects.all().delete()

        backfill_memberships(__import__("django.apps", fromlist=["apps"]).apps, None)

        default = Workspace.get_default()
        self.assertEqual(
            WorkspaceMembership.objects.get(user=u1, workspace=default).role, "member"
        )
        self.assertEqual(
            WorkspaceMembership.objects.get(user=u2, workspace=default).role, "owner"
        )

    def test_backfill_is_idempotent_and_reversible(self):
        from django.apps import apps as global_apps

        User.objects.create(email="a@b.com")
        WorkspaceMembership.objects.all().delete()
        backfill_memberships(global_apps, None)
        backfill_memberships(global_apps, None)  # second run is a no-op
        default = Workspace.get_default()
        self.assertEqual(
            WorkspaceMembership.objects.filter(workspace=default).count(), 1
        )
        unbackfill_memberships(global_apps, None)
        self.assertEqual(
            WorkspaceMembership.objects.filter(workspace=default).count(), 0
        )


class ActiveWorkspaceMiddlewareTests(TestCase):
    """Unit tests of the resolution hook (no full stack / setup gate needed)."""

    def setUp(self):
        self.mw = ActiveWorkspaceMiddleware(lambda r: HttpResponse())
        self.rf = RequestFactory()
        self.default = Workspace.get_default()
        self.ws_b = Workspace.objects.create(name="B")
        self.member = User.objects.create(email="m@example.com")  # default only

    def _req(self, user):
        req = self.rf.get("/cpanel/scripts/")
        req.user = user
        return req

    def test_bare_url_resolves_default_in_place(self):
        req = self._req(self.member)
        kwargs = {}
        res = self.mw.process_view(req, _dummy_view, [], kwargs)
        self.assertIsNone(res)  # no redirect — resolve-in-place
        self.assertEqual(req.workspace, self.default)

    def test_prefixed_member_ok_and_kwarg_stripped(self):
        req = self._req(self.member)
        kwargs = {"workspace_id": self.default.id}
        self.mw.process_view(req, _dummy_view, [], kwargs)
        self.assertEqual(req.workspace, self.default)
        # The kwarg is popped so the wrapped view's signature is untouched.
        self.assertNotIn("workspace_id", kwargs)

    def test_prefixed_non_member_404(self):
        req = self._req(self.member)  # not a member of ws_b
        with self.assertRaises(Http404):
            self.mw.process_view(req, _dummy_view, [], {"workspace_id": self.ws_b.id})

    def test_prefixed_unknown_workspace_404(self):
        req = self._req(self.member)
        with self.assertRaises(Http404):
            self.mw.process_view(req, _dummy_view, [], {"workspace_id": uuid.uuid4()})

    def test_prefixed_superuser_any_workspace_ok(self):
        root = User.objects.create(email="s@example.com", is_superuser=True)
        req = self._req(root)
        self.mw.process_view(req, _dummy_view, [], {"workspace_id": self.ws_b.id})
        self.assertEqual(req.workspace, self.ws_b)

    def test_anonymous_not_404_kwarg_still_stripped(self):
        req = self._req(AnonymousUser())
        kwargs = {"workspace_id": self.ws_b.id}
        res = self.mw.process_view(req, _dummy_view, [], kwargs)
        self.assertIsNone(res)  # let @login_required redirect, do not 404
        self.assertIsNone(req.workspace)
        self.assertNotIn("workspace_id", kwargs)


class WsUrlTagTests(TestCase):
    def setUp(self):
        self.ws_b = Workspace.objects.create(name="B")
        self.rf = RequestFactory()

    def _render(self, template_str, show_switcher, **extra):
        req = self.rf.get("/cpanel/scripts/")
        req.workspace = self.ws_b
        ctx = {"request": req, "show_workspace_switcher": show_switcher}
        ctx.update(extra)
        return Template("{% load workspace_tags %}" + template_str).render(Context(ctx))

    def test_unprefixed_when_switcher_hidden(self):
        out = self._render("{% ws_url 'cpanel:script_list' %}", show_switcher=False)
        self.assertEqual(out, "/cpanel/scripts/")

    def test_prefixed_when_switcher_active(self):
        out = self._render("{% ws_url 'cpanel:script_list' %}", show_switcher=True)
        self.assertEqual(out, f"/cpanel/w/{self.ws_b.id}/scripts/")

    def test_prefixed_with_kwargs(self):
        pk = uuid.uuid4()
        out = self._render(
            "{% ws_url 'cpanel:script_detail' pk=pk %}", show_switcher=True, pk=pk
        )
        self.assertEqual(out, f"/cpanel/w/{self.ws_b.id}/scripts/{pk}/")


class SwitcherContextTests(TestCase):
    def setUp(self):
        self.default = Workspace.get_default()
        self.rf = RequestFactory()

    def _ctx(self, user):
        req = self.rf.get("/cpanel/")
        req.user = user
        req.workspace = self.default
        return workspaces_ctx(req)

    def test_hidden_for_single_workspace_member(self):
        u = User.objects.create(email="m@example.com")  # default only
        ctx = self._ctx(u)
        self.assertFalse(ctx["show_workspace_switcher"])
        self.assertEqual(list(ctx["user_workspaces"]), [self.default])

    def test_shown_for_multi_workspace_member(self):
        u = User.objects.create(email="m@example.com")
        ws_b = Workspace.objects.create(name="B")
        WorkspaceMembership.ensure(u, ws_b, role=WorkspaceMembership.ROLE_MEMBER)
        ctx = self._ctx(u)
        self.assertTrue(ctx["show_workspace_switcher"])
        self.assertEqual(len(ctx["user_workspaces"]), 2)

    def test_anonymous_hidden(self):
        ctx = self._ctx(AnonymousUser())
        self.assertFalse(ctx["show_workspace_switcher"])
        self.assertEqual(list(ctx["user_workspaces"]), [])


class TenancyIntegrationTests(TestCase):
    """Full request stack: byte-for-byte single-workspace + URL-prefix membership gate."""

    def setUp(self):
        # Bypass the setup-wizard redirect so cpanel views render.
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)

        self.default = Workspace.get_default()
        self.ws_b = Workspace.objects.create(name="B")
        self.member = User.objects.create(email="member@example.com")  # default only
        self.root = User.objects.create(email="root@example.com", is_superuser=True)

    def test_single_workspace_unprefixed_byte_for_byte(self):
        self.client.force_login(self.member)
        resp = self.client.get(reverse("cpanel:script_list"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        # Switcher hidden + no prefixed links for a single-workspace user.
        self.assertNotIn("Switch workspace", body)
        self.assertNotIn("/cpanel/w/", body)

    def test_prefixed_member_ok(self):
        self.client.force_login(self.member)
        url = f"/cpanel/w/{self.default.id}/scripts/"
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_prefixed_non_member_404(self):
        self.client.force_login(self.member)  # not a member of ws_b
        url = f"/cpanel/w/{self.ws_b.id}/scripts/"
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_prefixed_superuser_any_ok(self):
        self.client.force_login(self.root)
        url = f"/cpanel/w/{self.ws_b.id}/scripts/"
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_anonymous_prefixed_redirects_to_login(self):
        url = f"/cpanel/w/{self.ws_b.id}/scripts/"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/auth/", resp["Location"])

    def test_switcher_shown_once_multi_workspace(self):
        WorkspaceMembership.ensure(
            self.member, self.ws_b, role=WorkspaceMembership.ROLE_MEMBER
        )
        self.client.force_login(self.member)
        body = self.client.get(reverse("cpanel:script_list")).content.decode()
        self.assertIn("Switch workspace", body)


class WorkspaceManagementViewTests(TestCase):
    def setUp(self):
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)

        self.root = User.objects.create(email="root@example.com", is_superuser=True)
        self.member = User.objects.create(email="member@example.com")

    def test_superuser_can_create_workspace_as_owner(self):
        self.client.force_login(self.root)
        resp = self.client.post(reverse("cpanel:workspace_create"), {"name": "Acme"})
        self.assertEqual(resp.status_code, 302)
        ws = Workspace.objects.get(name="Acme")
        self.assertTrue(
            WorkspaceMembership.objects.filter(
                user=self.root, workspace=ws, role=WorkspaceMembership.ROLE_OWNER
            ).exists()
        )

    def test_create_rejects_blank_name(self):
        self.client.force_login(self.root)
        self.client.post(reverse("cpanel:workspace_create"), {"name": "   "})
        self.assertFalse(Workspace.objects.filter(name="   ").exists())

    def test_rename_workspace(self):
        self.client.force_login(self.root)
        ws = Workspace.objects.create(name="Old")
        self.client.post(reverse("cpanel:workspace_rename", args=[ws.id]), {"name": "New"})
        ws.refresh_from_db()
        self.assertEqual(ws.name, "New")

    def test_non_superuser_blocked(self):
        self.client.force_login(self.member)
        resp = self.client.post(reverse("cpanel:workspace_create"), {"name": "Nope"})
        self.assertEqual(resp.status_code, 403)  # superuser_required → console 403 page
        self.assertFalse(Workspace.objects.filter(name="Nope").exists())

    def test_members_page_lists_roles(self):
        self.client.force_login(self.root)
        body = self.client.get(
            reverse("cpanel:workspace_members", args=[Workspace.get_default().id])
        ).content.decode()
        self.assertIn("root@example.com", body)
