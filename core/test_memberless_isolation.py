"""F129: a signed-in user with no workspace membership is never "unscoped".

Before the fix, ``Workspace.resolve_for`` gave a memberless non-superuser
``request.workspace = None``; the console let them create and run a script
with no workspace, and ``resolve_secrets_for_run`` applied no filter to an
unscoped run, so the run received every workspace's secrets. The loopback
resolvers also mapped "no workspace" to the default workspace.
"""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.executor import resolve_secrets_for_run
from core.models import (
    DataStore,
    Environment,
    Run,
    Script,
    Secret,
    User,
    Workspace,
    WorkspaceMembership,
)


def _secret(key, value, workspace):
    s = Secret(key=key, workspace=workspace)
    s.set_value(value)
    s.save()
    return s


class MemberlessUserTests(TestCase):
    def setUp(self):
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            patcher = mock.patch(target, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.default = Workspace.get_default()
        self.ws2 = Workspace.objects.create(name="ws2")
        self.env = Environment.objects.create(name="e", path="secenv")
        _secret("DEFAULT_KEY", "dval", self.default)
        _secret("WS2_KEY", "wval", self.ws2)
        self.user = User.objects.create_user(
            username="x", email="x@example.com", password="pw-12345678!"
        )
        WorkspaceMembership.objects.filter(user=self.user).delete()  # an admin removed them
        self.client.force_login(self.user)

    def test_console_pages_answer_the_no_workspace_page(self):
        for name in ("script_list", "secret_list", "script_create", "dashboard", "api_token_list"):
            with self.subTest(page=name):
                response = self.client.get(reverse(f"cpanel:{name}"))
                self.assertEqual(response.status_code, 403)
                self.assertContains(response, "not in any workspace", status_code=403)
                # Sign out is a POST form (logout_view is POST-only).
                self.assertContains(
                    response, f'<form method="post" action="{reverse("auth:logout")}">',
                    status_code=403,
                )

    def test_script_create_and_run_are_refused(self):
        response = self.client.post(
            reverse("cpanel:script_create"),
            {
                "name": "evil",
                "code": "import os; print(os.environ)",
                "environment": str(self.env.pk),
                "timeout_seconds": "60",
                "injection_mode": "all",
                "isolation_mode": "inherit",
                "notify_on": "never",
                "is_enabled": "on",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Script.objects.filter(name="evil").exists())

    def test_auth_pages_still_work(self):
        self.assertEqual(self.client.get(reverse("auth:change_password")).status_code, 200)

    def test_sign_out_from_the_no_workspace_page_works(self):
        response = self.client.post(reverse("auth:logout"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_superuser_without_membership_keeps_the_default_workspace(self):
        admin = User.objects.create_superuser(
            username="root", email="root@example.com", password="pw-12345678!"
        )
        WorkspaceMembership.objects.filter(user=admin).delete()
        self.client.force_login(admin)
        self.assertEqual(self.client.get(reverse("cpanel:script_list")).status_code, 200)

    def test_member_still_works(self):
        WorkspaceMembership.ensure(self.user, self.default)
        self.assertEqual(self.client.get(reverse("cpanel:script_list")).status_code, 200)

    def test_unscoped_run_gets_only_unscoped_secrets(self):
        _secret("GLOBAL_KEY", "gval", None)
        script = Script.objects.create(name="orphan", code="x", environment=self.env)
        Script.objects.filter(pk=script.pk).update(workspace=None)
        script.refresh_from_db()
        run = Run.objects.create(script=script)
        Run.objects.filter(pk=run.pk).update(workspace=None)
        run.refresh_from_db()
        self.assertEqual(sorted(resolve_secrets_for_run(run)), ["GLOBAL_KEY"])

    def test_scoped_run_unchanged(self):
        script = Script.objects.create(
            name="s", code="x", environment=self.env, workspace=self.ws2
        )
        run = Run.objects.create(script=script)
        self.assertEqual(sorted(resolve_secrets_for_run(run)), ["WS2_KEY"])

    def test_loopback_resolver_without_workspace_never_picks_the_default(self):
        DataStore.objects.create(name="store", workspace=self.default)
        with self.assertRaises(DataStore.DoesNotExist):
            DataStore.resolve_for_workspace("store", None)
        self.assertEqual(
            DataStore.resolve_for_workspace("store", self.default.id).workspace, self.default
        )
