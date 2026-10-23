"""
Tenancy Stage 2 — per-workspace datastores (Decision 2B: composite uniqueness).

Two workspaces can each own a store named "shared", and every by-name resolver
(model, the internal loopback API, the public REST API, the SQLite helper's env
injection) scopes to the right workspace — so a run/token in one workspace can
never read another's store by name. A transitional NULL fallback keeps a
single-workspace instance byte-for-byte.
"""

import json
import uuid
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from core.executor import _build_script_environment
from core.models import (
    DataStore,
    DataStoreAPIToken,
    DataStoreEntry,
    Environment,
    Run,
    Script,
    Workspace,
)
from core.services.datastore_token import mint_datastore_token


def _auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _entry(store, key, value):
    e = DataStoreEntry(datastore=store, key=key)
    e.set_value(value)
    e.save()
    return e


class CompositeUniquenessTests(TestCase):
    def setUp(self):
        self.ws_a = Workspace.objects.create(name="A")
        self.ws_b = Workspace.objects.create(name="B")

    def test_same_name_allowed_across_workspaces(self):
        DataStore.objects.create(name="shared", workspace=self.ws_a)
        DataStore.objects.create(name="shared", workspace=self.ws_b)  # no collision
        self.assertEqual(DataStore.objects.filter(name="shared").count(), 2)

    def test_duplicate_name_within_workspace_rejected(self):
        DataStore.objects.create(name="shared", workspace=self.ws_a)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DataStore.objects.create(name="shared", workspace=self.ws_a)

    def test_null_workspace_name_globally_unique(self):
        DataStore.objects.create(name="legacy", workspace=None)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DataStore.objects.create(name="legacy", workspace=None)


class ResolveForWorkspaceTests(TestCase):
    def setUp(self):
        self.default = Workspace.get_default()
        self.ws_a = Workspace.objects.create(name="A")
        self.ws_b = Workspace.objects.create(name="B")
        self.a = DataStore.objects.create(name="shared", workspace=self.ws_a)
        self.b = DataStore.objects.create(name="shared", workspace=self.ws_b)

    def test_resolves_within_workspace(self):
        self.assertEqual(DataStore.resolve_for_workspace("shared", self.ws_a.id), self.a)
        self.assertEqual(DataStore.resolve_for_workspace("shared", self.ws_b.id), self.b)

    def test_cross_workspace_name_not_found(self):
        # No "shared" store in the default workspace and none NULL -> not found.
        with self.assertRaises(DataStore.DoesNotExist):
            DataStore.resolve_for_workspace("shared", self.default.id)

    def test_none_never_resolves_the_default_workspace(self):
        # F129: no workspace sees only unassigned stores.
        DataStore.objects.create(name="cfg", workspace=self.default)
        with self.assertRaises(DataStore.DoesNotExist):
            DataStore.resolve_for_workspace("cfg", None)
        legacy = DataStore.objects.create(name="cfg", workspace=None)
        self.assertEqual(DataStore.resolve_for_workspace("cfg", None), legacy)

    def test_null_store_fallback(self):
        legacy = DataStore.objects.create(name="legacy", workspace=None)
        # A workspace with no own "legacy" falls back to the unassigned store.
        self.assertEqual(DataStore.resolve_for_workspace("legacy", self.ws_a.id), legacy)


class InternalApiIsolationTests(TestCase):
    """The signed per-run token scopes by-name resolution to the run's workspace."""

    def setUp(self):
        self.ws_a = Workspace.objects.create(name="A")
        self.ws_b = Workspace.objects.create(name="B")
        self.env = Environment.objects.create(name="e", path="s2int")
        self.store_a = DataStore.objects.create(name="shared", workspace=self.ws_a)
        self.store_b = DataStore.objects.create(name="shared", workspace=self.ws_b)
        _entry(self.store_a, "k", "a-value")
        _entry(self.store_b, "k", "b-value")

    def _run_in(self, workspace):
        script = Script.objects.create(
            name="s", code="x", environment=self.env, workspace=workspace
        )
        return Run.objects.create(script=script, workspace=workspace)

    def _get_entry(self, run, key="k"):
        token = mint_datastore_token(run.id)
        url = reverse("internal:entry", args=["shared"]) + f"?key={key}"
        return self.client.get(url, **_auth(token))

    def test_run_reads_its_own_workspace_store(self):
        resp_a = self._get_entry(self._run_in(self.ws_a))
        self.assertEqual(resp_a.status_code, 200)
        self.assertEqual(resp_a.json()["value"], "a-value")

        resp_b = self._get_entry(self._run_in(self.ws_b))
        self.assertEqual(resp_b.status_code, 200)
        self.assertEqual(resp_b.json()["value"], "b-value")  # never a-value

    def test_write_lands_in_run_workspace_store(self):
        run_a = self._run_in(self.ws_a)
        token = mint_datastore_token(run_a.id)
        url = reverse("internal:entry", args=["shared"])
        self.client.put(
            url,
            data=json.dumps({"key": "new", "value": 1}),
            content_type="application/json",
            **_auth(token),
        )
        # The new entry is in WS-A's store only.
        self.assertTrue(self.store_a.entries.filter(key="new").exists())
        self.assertFalse(self.store_b.entries.filter(key="new").exists())


class PublicRestIsolationTests(TestCase):
    def setUp(self):
        # /api/v1/ is not exempt from the setup-wizard redirect; mark setup done.
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)

        self.ws_a = Workspace.objects.create(name="A")
        self.ws_b = Workspace.objects.create(name="B")
        self.store_a = DataStore.objects.create(name="shared", workspace=self.ws_a)
        self.store_b = DataStore.objects.create(name="shared", workspace=self.ws_b)
        _entry(self.store_a, "k", "a-value")
        _entry(self.store_b, "k", "b-value")
        self.token_a = DataStoreAPIToken.objects.create(
            name="ta", token=DataStoreAPIToken.generate_token(), datastore=self.store_a
        )

    def test_scoped_token_resolves_its_workspace_store(self):
        resp = self.client.get(
            "/api/v1/datastores/shared/entries/k/", **_auth(self.token_a.raw_token)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["value"], "a-value")  # WS-A, never WS-B


class ExecutorWorkspaceInjectionTests(TestCase):
    def setUp(self):
        self.default = Workspace.get_default()
        self.ws_a = Workspace.objects.create(name="A")
        self.env = Environment.objects.create(name="e", path="s2inj")

    def test_injects_run_workspace_hex(self):
        script = Script.objects.create(
            name="s", code="x", environment=self.env, workspace=self.ws_a
        )
        run = Run.objects.create(script=script, workspace=self.ws_a)
        env = _build_script_environment(run=run)
        self.assertEqual(env["PYRUNNER_WORKSPACE_ID"], self.ws_a.id.hex)

    def test_null_workspace_run_gets_no_workspace_id(self):
        # F129: an unscoped run is not the default workspace (fail closed); the
        # SQLite helper then resolves only unassigned stores.
        script = Script.objects.create(name="s", code="x", environment=self.env)
        run = Run.objects.create(script=script)  # workspace NULL
        env = _build_script_environment(run=run)
        self.assertNotIn("PYRUNNER_WORKSPACE_ID", env)


class DatastoreCpanelScopingTests(TestCase):
    def setUp(self):
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            p = mock.patch(target, return_value=False)
            p.start()
            self.addCleanup(p.stop)

        from core.models import User

        self.default = Workspace.get_default()
        self.ws_b = Workspace.objects.create(name="B")
        self.root = User.objects.create(email="root@example.com", is_superuser=True)
        self.client.force_login(self.root)

    def test_create_stamps_active_workspace(self):
        self.client.post(reverse("cpanel:datastore_create"), {"name": "results", "description": ""})
        ds = DataStore.objects.get(name="results")
        self.assertEqual(ds.workspace_id, self.default.id)  # superuser's active = default

    def test_list_scoped_to_active_workspace(self):
        DataStore.objects.create(name="mine", workspace=self.default)
        DataStore.objects.create(name="theirs", workspace=self.ws_b)
        body = self.client.get(reverse("cpanel:datastore_list")).content.decode()
        self.assertIn("mine", body)
        self.assertNotIn("theirs", body)

    def test_detail_of_other_workspace_store_404(self):
        other = DataStore.objects.create(name="theirs", workspace=self.ws_b)
        resp = self.client.get(reverse("cpanel:datastore_detail", args=[other.id]))
        self.assertEqual(resp.status_code, 404)
