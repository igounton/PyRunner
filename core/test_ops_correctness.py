"""
Ops-service correctness fixes — regressions for review 7.2 / 4b.3 / 4a.3.

- 7.2  ``pyrunner_datastore._ApiBackend`` ignored the HTTP status, so a 500/401
       (e.g. an expired token on a long run) made ``set()``/``clear()`` a silent
       no-op and ``get()`` surface a cryptic ``KeyError('value')``. It must raise
       ``DataStoreError`` on server/auth failures (404 still maps to KeyError).
- 4b.3 ``SetupService.get_status`` used a raw ``sqlite_master`` query → reported
       database_ready=False + an error on a healthy Postgres. Now engine-agnostic.
- 4a.3 ``TaskService.get_queued_tasks`` used raw ``pickle.loads`` on a SIGNED
       django-q payload → threw for every task, leaving func "Unknown". Now uses
       the shared SignedPackage-aware decoder.
"""

import io
import urllib.error
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.script_helpers.pyrunner_datastore import DataStoreError, _ApiBackend
from core.services.setup_service import SetupService
from core.services.task_service import TaskService


class _Resp:
    """Minimal context-manager stand-in for urlopen()'s response."""

    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body=b'{"error": "boom"}'):
    return urllib.error.HTTPError("http://x/", code, "err", {}, io.BytesIO(body))


class DataStoreApiBackendTests(TestCase):
    """Review 7.2 — the API backend must not swallow server/auth failures."""

    def setUp(self):
        self.backend = _ApiBackend("store", "http://127.0.0.1:8000", "tok")

    def test_set_raises_on_server_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)):
            with self.assertRaises(DataStoreError):
                self.backend.set("k", 1)

    def test_get_raises_on_auth_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(401)):
            with self.assertRaises(DataStoreError):
                self.backend.get("k")

    def test_clear_raises_on_server_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)):
            with self.assertRaises(DataStoreError):
                self.backend.clear()

    def test_unreachable_api_raises(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(DataStoreError):
                self.backend.set("k", 1)

    def test_missing_key_still_maps_to_keyerror(self):
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(404)):
            with self.assertRaises(KeyError):
                self.backend.get("k")
            with self.assertRaises(KeyError):
                self.backend.delete("k")

    def test_successful_get_returns_value(self):
        with mock.patch("urllib.request.urlopen", return_value=_Resp(200, b'{"value": 42}')):
            self.assertEqual(self.backend.get("k"), 42)


class SetupStatusTests(TestCase):
    """Review 4b.3 — the DB-ready check must be engine-agnostic."""

    def test_database_ready_true_when_table_present(self):
        # The migrated test DB has global_settings; no error should be recorded.
        status = SetupService.get_status()
        self.assertTrue(status["database_ready"])
        self.assertFalse(
            any("Database check failed" in e for e in status["errors"]),
            status["errors"],
        )

    def test_uses_introspection_not_sqlite_master(self):
        # Simulate a non-sqlite backend: introspection returns real table names
        # and the raw sqlite_master query would have failed. The check must read
        # the introspection list, not run engine-specific SQL.
        with mock.patch(
            "django.db.connection.introspection.table_names",
            return_value=["global_settings", "scripts"],
        ):
            self.assertTrue(SetupService.get_status()["database_ready"])
        with mock.patch(
            "django.db.connection.introspection.table_names",
            return_value=["scripts"],
        ):
            self.assertFalse(SetupService.get_status()["database_ready"])


class QueuedTasksDecodeTests(TestCase):
    """Review 4a.3 — signed django-q payloads must decode, not fall back to Unknown."""

    def test_signed_payload_decodes_func_and_name(self):
        from django_q.models import OrmQ
        from django_q.signing import SignedPackage

        from django_q.conf import Conf

        # As django-q2's ORM broker writes it: key = the queue name, the task id
        # inside the signed payload (see core/test_task_queue_ids.py).
        payload = SignedPackage.dumps(
            {"id": "demo-123", "func": "core.tasks.demo_task", "name": "demo-123",
             "args": (), "kwargs": {}}
        )
        OrmQ.objects.create(key=Conf.PREFIX, payload=payload, lock=timezone.now())

        tasks = {t["id"]: t for t in TaskService.get_queued_tasks()}
        self.assertIn("demo-123", tasks)
        self.assertEqual(tasks["demo-123"]["func"], "core.tasks.demo_task")
        self.assertEqual(tasks["demo-123"]["name"], "demo-123")
