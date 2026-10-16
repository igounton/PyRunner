"""
A restore keeps the backup's timestamps (2.0 release test §19.02, F70).

The import passes ``created_at``/``updated_at`` to ``create()``, but Django
overwrites ``auto_now``/``auto_now_add`` fields on insert, so every restored
run, script, environment, secret, store entry … was stamped with the restore
time: the dashboard showed the whole history as "0 minutes ago", "Runs today"
counted every restored run, and age-based retention started over.
"""

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from core.models import DataStore, DataStoreEntry, Environment, Run, Script, Secret, User, Workspace
from core.services.backup_service import BackupService
from core.services.encryption_service import EncryptionService

OLD = datetime(2025, 1, 2, 3, 4, 5, tzinfo=dt_timezone.utc)
OLDER = datetime(2024, 6, 7, 8, 9, 10, tzinfo=dt_timezone.utc)


class RestoreKeepsTimestampsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(email="admin@example.com", is_superuser=True)
        ws = Workspace.get_default() or Workspace.objects.create(name="D", is_default=True)
        env = Environment.objects.create(name="ts-env", path="ts-env", workspace=ws)
        script = Script.objects.create(name="ts-script", code="print(1)", environment=env, workspace=ws)
        Run.objects.create(script=script, workspace=ws, status="success")
        store = DataStore.objects.create(name="ts_store", workspace=ws)
        DataStoreEntry.objects.create(datastore=store, key="k", value_json="1")
        Secret.objects.create(key="TS_SECRET", workspace=ws, encrypted_value=EncryptionService.encrypt("x"))
        for model in (Environment, Script, DataStore, DataStoreEntry, Secret):
            model.objects.update(created_at=OLDER, updated_at=OLD)
        Run.objects.update(created_at=OLD)

    def test_restore_writes_back_created_and_updated_times(self):
        data = BackupService.create_backup(include_runs=True)
        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)

        self.assertEqual(Run.objects.get().created_at, OLD)
        for model in (Environment, Script, DataStore, DataStoreEntry, Secret):
            with self.subTest(model=model.__name__):
                row = model.objects.get()
                self.assertEqual(row.created_at, OLDER)
                self.assertEqual(row.updated_at, OLD)

    def test_rows_without_a_time_in_the_backup_keep_the_restore_time(self):
        data = BackupService.create_backup(include_runs=True)
        data["runs"][0]["created_at"] = None
        before = datetime.now(dt_timezone.utc)
        result = BackupService.restore_backup(data, restore_runs=True, current_user=self.user)
        self.assertTrue(result["success"], result)
        self.assertGreaterEqual(Run.objects.get().created_at, before)
