"""
Step 2 (Postgres-readiness) — datastore + Claude-usage helper cutover.

Proves the engine-aware paths that make Postgres work without changing SQLite:
- the executor injects the internal API URL + signed token (and keeps
  PYRUNNER_DB_PATH on SQLite);
- the rewritten datastore helper's API backend round-trips the full public
  surface against the real internal endpoint (via a live server);
- the Claude-usage endpoint records a row (the ORM equivalent of the raw-sqlite
  write pyrunner_ai used).
"""

import json
import os
import sys
import uuid
import unittest
from unittest import mock

from django.conf import settings
from django.db import connection
from django.test import LiveServerTestCase, TestCase
from django.urls import reverse

from core.models import ClaudeUsage, DataStore, Environment, Run, Script
from core.services.datastore_token import mint_datastore_token, verify_datastore_token

# The script helpers import as top-level modules (the executor puts this dir on
# PYTHONPATH for the subprocess); do the same in-process for the tests.
sys.path.insert(0, str(settings.BASE_DIR / "core" / "script_helpers"))


def _auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


class ClaudeUsageEndpointTests(TestCase):
    def setUp(self):
        self.token = mint_datastore_token(uuid.uuid4())
        self.url = reverse("internal:claude_usage")

    def test_records_a_row(self):
        run_id = uuid.uuid4()
        body = {
            "run_id": run_id.hex,
            "script_name": "demo",
            "source": "script",
            "model": "claude-opus-4-8",
            "input_tokens": 10,
            "output_tokens": 5,
            "num_turns": 1,
            "duration_ms": 1234,
            "cost_usd": 0.01,
        }
        resp = self.client.post(
            self.url, data=json.dumps(body), content_type="application/json", **_auth(self.token)
        )
        self.assertEqual(resp.status_code, 200)
        row = ClaudeUsage.objects.get()
        self.assertEqual(row.script_name, "demo")
        self.assertEqual(row.input_tokens, 10)
        self.assertEqual(row.model, "claude-opus-4-8")

    def test_requires_token(self):
        self.assertEqual(self.client.post(self.url, data="{}", content_type="application/json").status_code, 401)


class ExecutorInjectionTests(TestCase):
    @unittest.skipUnless(connection.vendor == "sqlite", "the DB path is only injected on SQLite")
    def test_sqlite_run_env_has_db_path_and_signed_token(self):
        from core.executor import _build_script_environment

        env = Environment.objects.create(name="e", path="injenv")
        script = Script.objects.create(name="s", code="x", environment=env)
        run = Run.objects.create(script=script)

        built = _build_script_environment(run=run)

        # SQLite: direct path stays (byte-for-byte).
        self.assertIn("PYRUNNER_DB_PATH", built)
        # Internal API path is also provided, with a token bound to this run.
        self.assertEqual(built["PYRUNNER_INTERNAL_URL"], settings.PYRUNNER_INTERNAL_BASE_URL)
        payload = verify_datastore_token(built["PYRUNNER_INTERNAL_TOKEN"])
        self.assertEqual(payload, {"run_id": str(run.id)})


class BackendSelectionTests(TestCase):
    def test_api_backend_when_only_url_and_token(self):
        import pyrunner_datastore as pds

        with mock.patch.dict(os.environ, {
            "PYRUNNER_INTERNAL_URL": "http://127.0.0.1:8000",
            "PYRUNNER_INTERNAL_TOKEN": "tok",
        }, clear=False):
            os.environ.pop("PYRUNNER_DB_PATH", None)
            backend = pds._make_backend("x")
        self.assertIsInstance(backend, pds._ApiBackend)

    def test_runtime_error_when_unconfigured(self):
        import pyrunner_datastore as pds

        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("PYRUNNER_DB_PATH", "PYRUNNER_INTERNAL_URL", "PYRUNNER_INTERNAL_TOKEN"):
                os.environ.pop(k, None)
            with self.assertRaises(RuntimeError):
                pds._make_backend("x")


class HelperApiRoundTripTests(LiveServerTestCase):
    """Drive the rewritten helper's API backend against a real internal endpoint."""

    def setUp(self):
        DataStore.objects.create(name="hs")
        self._saved = {
            k: os.environ.get(k)
            for k in ("PYRUNNER_DB_PATH", "PYRUNNER_INTERNAL_URL", "PYRUNNER_INTERNAL_TOKEN")
        }
        os.environ.pop("PYRUNNER_DB_PATH", None)  # force the API backend
        os.environ["PYRUNNER_INTERNAL_URL"] = self.live_server_url
        os.environ["PYRUNNER_INTERNAL_TOKEN"] = mint_datastore_token(uuid.uuid4())

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_full_public_surface_round_trips(self):
        import pyrunner_datastore as pds

        store = pds.DataStore("hs")

        # set / get / type fidelity
        store["a"] = {"n": 1, "nested": [True, None]}
        self.assertEqual(store["a"], {"n": 1, "nested": [True, None]})

        # contains / len / get-default
        self.assertIn("a", store)
        self.assertNotIn("missing", store)
        self.assertEqual(store.get("missing", 42), 42)
        self.assertEqual(len(store), 1)

        # keys / values / items (ordered by key) + update
        store.update({"b": 2, "c": 3})
        self.assertEqual(store.keys(), ["a", "b", "c"])
        self.assertEqual([k for k, _ in store.items()], ["a", "b", "c"])

        # setdefault
        self.assertEqual(store.setdefault("b", 999), 2)
        self.assertEqual(store.setdefault("d", 4), 4)

        # del + KeyError parity
        del store["a"]
        with self.assertRaises(KeyError):
            _ = store["a"]
        with self.assertRaises(KeyError):
            del store["a"]

        # pop
        self.assertEqual(store.pop("b"), 2)
        self.assertEqual(store.pop("nope", "fallback"), "fallback")

        # clear
        store.clear()
        self.assertEqual(len(store), 0)

    def test_missing_store_raises_value_error(self):
        import pyrunner_datastore as pds

        with self.assertRaises(ValueError):
            pds.DataStore("does_not_exist")
