"""
The restore preview names the API tokens a restore will delete (F61) and lists
every kind of data in the backup, counting only real schedules (U43).

A restore deletes every Brain and DataStore before re-creating them, and the
tokens bound to a brain or scoped to one store go with them by FK cascade —
a same-instance rollback silently broke connectors (measured on A, 17.16).
The behaviour is kept; the preview now says which tokens go.
"""

from django.test import TestCase

from core.brain.service import BrainService
from core.models import APIToken, DataStore, Workspace
from core.services.backup_service import BackupService
from core.test_brain_store import contract_files


class RestorePreviewTokenTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.brain = BrainService.create(self.ws, "mind", files=contract_files())
        self.store = DataStore.objects.create(name="leads", workspace=self.ws)
        APIToken.objects.create(
            name="brain-bound", token=APIToken.generate_token(scope="api"), scope="api",
            permissions=["brain:read"], workspace=self.ws, brain=self.brain,
        )
        APIToken.objects.create(
            name="one-store", token=APIToken.generate_token(), datastore=self.store
        )
        APIToken.objects.create(
            name="all-stores", token=APIToken.generate_token(), workspace=self.ws
        )

    def _preview(self, **data):
        return BackupService.get_backup_preview({"backup_metadata": {}, **data})

    def test_preview_lists_tokens_bound_to_a_brain_or_one_store(self):
        removed = self._preview()["tokens_removed"]
        self.assertEqual(
            removed,
            [
                {"name": "brain-bound", "bound_to": "brain mind"},
                {"name": "one-store", "bound_to": "data store leads"},
            ],
        )

    def test_counts_cover_every_kind_and_only_real_schedules(self):
        preview = self._preview(
            script_schedules=[
                {"run_mode": "manual", "is_active": True},
                {"run_mode": "daily", "is_active": True},
                {"run_mode": "weekly", "is_active": False},
            ],
            libraries=[{}, {}],
            databases=[{}],
        )
        counts = preview["counts"]
        self.assertEqual(counts["schedules"], 1)
        self.assertEqual(counts["libraries"], 2)
        self.assertEqual(counts["databases"], 1)
