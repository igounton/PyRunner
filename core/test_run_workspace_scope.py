"""
F82 (2.0 release test, 23.01): a run saved without a workspace ran unscoped.

The built-in sales_dashboard example creates its Run with the models directly
and sets no workspace. When the "Sales Collector" it picks lives in a second
workspace, that run got the default workspace's data stores and every
workspace's secrets (measured on A, rc18). A run now always takes its script's
workspace: ``Run.save()`` stamps it, and the executor and the loopback API fall
back to the script's workspace for a row saved without one before the fix.
"""

from django.test import TestCase
from django.urls import reverse

from core.executor import _build_script_environment, resolve_secrets_for_run
from core.models import DataStore, DataStoreEntry, Environment, Run, Script, Secret, Workspace
from core.services.datastore_token import mint_datastore_token


def _secret(key, value, workspace):
    s = Secret(key=key, workspace=workspace)
    s.set_value(value)
    s.save()
    return s


def _entry(store, key, value):
    e = DataStoreEntry(datastore=store, key=key)
    e.set_value(value)
    e.save()
    return e


class RunTakesScriptWorkspaceTests(TestCase):
    def setUp(self):
        self.default = Workspace.get_default()
        self.ws2 = Workspace.objects.create(name="ws2")
        self.env = Environment.objects.create(name="e", path="f82env")
        self.script = Script.objects.create(
            name="Sales Collector", code="x", environment=self.env, workspace=self.ws2
        )
        _secret("DEFAULT_KEY", "dval", self.default)
        _secret("WS2_KEY", "wval", self.ws2)
        _secret("NULL_KEY", "nval", None)

    def _legacy_unscoped_run(self):
        """A row saved without a workspace (what plugin code produced before the fix)."""
        run = Run.objects.create(script=self.script, workspace=self.ws2)
        Run.objects.filter(pk=run.pk).update(workspace=None)
        return Run.objects.get(pk=run.pk)

    def test_run_created_without_workspace_gets_the_scripts(self):
        # Exactly what examples/sales_dashboard/views.py does.
        run = Run.objects.create(script=self.script, status=Run.Status.PENDING, code_snapshot="x")
        run.refresh_from_db()
        self.assertEqual(run.workspace_id, self.ws2.id)

    def test_explicit_workspace_is_kept(self):
        run = Run.objects.create(script=self.script, workspace=self.default)
        run.refresh_from_db()
        self.assertEqual(run.workspace_id, self.default.id)

    def test_secrets_of_an_unscoped_row_stay_in_the_scripts_workspace(self):
        out = resolve_secrets_for_run(self._legacy_unscoped_run())
        self.assertEqual(set(out), {"WS2_KEY", "NULL_KEY"})

    def test_env_of_an_unscoped_row_is_the_scripts_workspace(self):
        env = _build_script_environment(run=self._legacy_unscoped_run())
        self.assertEqual(env.get("PYRUNNER_WORKSPACE_ID"), self.ws2.id.hex)
        self.assertNotIn("DEFAULT_KEY", env)

    def test_loopback_api_of_an_unscoped_row_reads_the_scripts_workspace(self):
        _entry(DataStore.objects.create(name="sales_data", workspace=self.default), "orders", "default")
        _entry(DataStore.objects.create(name="sales_data", workspace=self.ws2), "orders", "ws2")
        token = mint_datastore_token(self._legacy_unscoped_run().id)
        url = reverse("internal:entry", args=["sales_data"]) + "?key=orders"
        resp = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["value"], "ws2")

    def test_pre_tenancy_script_without_workspace_stays_unscoped(self):
        # Back-compat: a script with no workspace still runs un-scoped (all secrets).
        legacy = Script.objects.create(name="old", code="x", environment=self.env, workspace=None)
        run = Run.objects.create(script=legacy)
        self.assertIsNone(run.workspace_id)
        self.assertEqual(set(resolve_secrets_for_run(run)), {"DEFAULT_KEY", "WS2_KEY", "NULL_KEY"})
