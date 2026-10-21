"""
Shared ``superuser_required`` decorator — regression for review 5.2.

Six view modules each defined an identical ``superuser_required`` decorator and
users.py / workspaces.py hand-rolled ``@user_passes_test(is_admin/is_superuser)``
equivalents. They are now one shared ``core.views.decorators.superuser_required``.
These tests lock the gate's behavior (signed-out users go to login, signed-in
non-superusers get the console 403 page, superusers pass)
and prove the previously-divergent surfaces (user management) still gate.
"""

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.models import (
    Environment,
    GlobalSettings,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.views.decorators import ADMIN_ONLY_MESSAGE, superuser_required


@superuser_required
def _protected(request):
    return HttpResponse("ok")


class SuperuserRequiredUnitTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_superuser_passes_through(self):
        request = self.factory.get("/x/")
        request.user = User.objects.create(email="su@example.com", is_superuser=True)
        resp = _protected(request)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"ok")

    def test_plain_member_is_denied_not_redirected(self):
        # UX6: a signed-in non-admin gets the console 403 page (PermissionDenied),
        # not a silent bounce through the login page to the dashboard.
        request = self.factory.get("/x/")
        request.user = User.objects.create(email="member@example.com", is_superuser=False)
        with self.assertRaises(PermissionDenied) as ctx:
            _protected(request)
        self.assertEqual(str(ctx.exception), ADMIN_ONLY_MESSAGE)

    def test_anonymous_redirected_to_login(self):
        request = self.factory.get("/x/")
        request.user = AnonymousUser()
        resp = _protected(request)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("auth:login") + "?next=/x/")


class UserManagementGateTests(TestCase):
    """users.py switched from a local is_admin to the shared decorator — prove the
    gate survived the swap (a non-superuser member must not reach user management)."""

    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.create(name="env", path="p", is_active=True, is_default=True)
        self.ws = Workspace.get_default()
        # A superuser must exist or SetupMiddleware 302s everyone to /setup/admin/
        # before the view's gate runs. This one is never logged in here.
        User.objects.create(email="root@example.com", is_superuser=True, is_staff=True)

    def _login(self, *, superuser):
        user = User.objects.create(
            email=f"{'su' if superuser else 'mem'}@example.com",
            is_superuser=superuser,
            is_staff=superuser,
        )
        WorkspaceMembership.ensure(user, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(user)
        return user

    def test_member_cannot_reach_user_list(self):
        self._login(superuser=False)
        resp = self.client.get(reverse("cpanel:user_list"))
        self.assertContains(resp, ADMIN_ONLY_MESSAGE, status_code=403)

    def test_superuser_reaches_user_list(self):
        self._login(superuser=True)
        resp = self.client.get(reverse("cpanel:user_list"))
        self.assertEqual(resp.status_code, 200)
