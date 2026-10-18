"""
Brain AI calls take their row lock inside a transaction (F99).

PostgreSQL refuses ``SELECT ... FOR UPDATE`` outside a transaction; SQLite
ignores row locks, and every TestCase runs inside one, so the suite never saw
it: on PostgreSQL every feeder extraction and reader run failed with
TransactionManagementError. ``postgres_lock_rules`` makes SQLite enforce the
PostgreSQL rule for the duration of a test.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from django.db import connection
from django.db.transaction import TransactionManagementError
from django.test import TransactionTestCase

from core.api.exceptions import ApiError
from core.brain import feeder, runtime
from core.brain.service import BrainService
from core.models import AIProvider, Brain, BrainFeed, GlobalSettings, Workspace
from core.services.encryption_service import EncryptionService
from core.test_brain_store import contract_files


@contextmanager
def postgres_lock_rules():
    """select_for_update raises outside a transaction, as on PostgreSQL."""
    with (
        patch.object(connection.features, "has_select_for_update", True),
        patch.object(connection.ops, "for_update_sql", lambda *a, **k: ""),
    ):
        yield


class BrainRuntimeLockTests(TransactionTestCase):
    def setUp(self):
        self.ws = Workspace.objects.create(name="pg locks")
        self.brain = BrainService.create(self.ws, "pglocks", files=contract_files())
        provider = AIProvider.objects.create(
            name="pg-locks", provider_type="anthropic",
            credential_encrypted=EncryptionService.encrypt("e2e-only-dummy"), default_model="m",
        )
        settings = GlobalSettings.get_settings()
        settings.claude_enabled, settings.active_ai_provider = True, provider
        settings.save()

    def test_helper_enforces_the_postgres_rule(self):
        with postgres_lock_rules(), self.assertRaises(TransactionManagementError):
            Brain.objects.select_for_update().first()

    def test_prepare_locks_inside_a_transaction(self):
        with postgres_lock_rules():
            usage, _env, model, _budget = runtime.prepare(self.ws, self.brain.pk, "feeder")
        self.assertEqual(model, "m")
        self.assertIsNotNone(usage.pk)

    def test_feeder_reaches_the_provider(self):
        feed = BrainFeed.objects.create(
            brain=self.brain, base_revision=self.brain.head_revision, source_id="thought-pg",
            channel="console", principal="user:1",
            raw_payload={"source_kind": "thought", "content": "Source words"},
        )
        drive = AsyncMock(side_effect=ApiError("provider_error", "stub provider down", 502))
        with postgres_lock_rules(), patch("core.brain.runtime._drive", drive):
            feeder.extract(self.ws.pk, feed.pk)
        feed.refresh_from_db()
        self.assertEqual(drive.await_count, 1)
        self.assertEqual(feed.error, "stub provider down")
