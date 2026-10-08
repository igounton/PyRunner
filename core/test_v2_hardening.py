"""Regressions from the vector live pass: SQLite locking and Qdrant outages."""

from unittest import mock, skipUnless

import requests
from django.core.cache import cache
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from core.models import VectorConnection
from core.vectors.errors import BackendUnavailable
from core.vectors.qdrant_store import QdrantStore
from core.vectors.service import VectorService


class SQLiteTransactionTests(TransactionTestCase):
    @skipUnless(connection.vendor == "sqlite", "SQLite transaction mode")
    def test_atomic_acquires_the_write_lock_at_begin(self):
        with CaptureQueriesContext(connection) as queries, transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        self.assertEqual(queries[0]["sql"], "BEGIN IMMEDIATE")


@override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
class QdrantOutageTests(TestCase):
    url = "http://127.0.0.1:16336"

    def setUp(self):
        self.session = mock.Mock()
        self.session.request.return_value = mock.Mock(
            status_code=200, json=lambda: {"result": {"count": 0, "collections": []}},
        )
        sleep = mock.patch("core.vectors.qdrant_store.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def store(self, name="docs", **kwargs):
        return QdrantStore(self.url, name, 3, "cosine", session=self.session, **kwargs)

    def trip(self, store=None):
        self.session.request.side_effect = requests.ConnectTimeout("private endpoint URL")
        with self.assertRaises(BackendUnavailable):
            (store or self.store()).count()
        self.session.request.side_effect = None

    def test_separate_connect_and_read_timeouts(self):
        self.store().count()
        self.assertEqual(self.session.request.call_args.kwargs["timeout"], (3, 10))

    def test_other_collection_and_new_adapter_fail_fast_via_shared_cache(self):
        self.trip()
        self.session.reset_mock()
        with self.assertRaises(BackendUnavailable) as raised:
            self.store("another").count()
        self.session.request.assert_not_called()
        self.assertNotIn(self.url, str(raised.exception))
        self.assertNotIn("private endpoint", str(raised.exception))

    def test_failure_is_shared_but_credentials_and_connections_are_isolated(self):
        self.trip(self.store(connection_id="first"))
        self.assertEqual(self.store(connection_id="second").count(), 0)
        self.assertEqual(self.store(connection_id="first", api_key="rotated").count(), 0)

    def test_open_window_skips_dns_in_new_adapter(self):
        self.trip()
        with mock.patch("core.vectors.qdrant_store.is_safe_endpoint_url", return_value=(True, "")) as guard:
            with self.assertRaises(BackendUnavailable):
                self.store().count()
        guard.assert_not_called()

    def test_repeated_fast_failures_do_not_extend_the_window(self):
        with mock.patch.object(cache, "add", wraps=cache.add) as add:
            self.trip()
            self.assertEqual(add.call_count, 1)
            self.assertEqual(add.call_args.kwargs["timeout"], 30)
            for _ in range(3):
                with self.assertRaises(BackendUnavailable):
                    self.store().count()
            self.assertEqual(add.call_count, 1)
            cache.delete(add.call_args.args[0])  # simulate expiry, without wall-clock sleeps
            self.assertEqual(self.store().count(), 0)

    def test_services_test_bypasses_and_clears_the_window(self):
        conn = VectorConnection.objects.create(
            name="outage-test", provider_type="qdrant", config={"url": self.url},
        )
        self.trip(self.store(connection_id=conn.pk))
        response = mock.Mock(status_code=200, json=lambda: {"result": {"collections": []}})
        with mock.patch("core.vectors.service.requests.get", return_value=response) as get:
            self.assertTrue(VectorService.test_connection(conn)[0])
        self.assertEqual(get.call_args.kwargs["timeout"], (3, 10))
        self.assertEqual(self.store(connection_id=conn.pk).count(), 0)

    def test_server_error_never_echoes_a_url(self):
        self.session.request.return_value = mock.Mock(
            status_code=503,
            json=lambda: {"status": {"error": f"cannot proxy {self.url}/secret"}},
        )
        with self.assertRaises(BackendUnavailable) as raised:
            self.store().count()
        self.assertNotIn(self.url, str(raised.exception))
