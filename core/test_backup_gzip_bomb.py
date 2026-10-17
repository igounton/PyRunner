"""F92 (2.0 release test 25.06): a gzip backup is decompressed with a bounded read.

``deserialize_backup`` used to call ``GzipFile.read()`` with no size, so a 300 KB
upload of gzipped zeros grew the web worker by ~580 MB before failing as
"invalid JSON"; the 100 MB upload cap allows ~100 GB of expansion. The read now
stops at ``PYRUNNER_BACKUP_MAX_EXPANDED_MB`` and refuses the file.
"""
import gzip
import io
import json
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.models import User
from core.services.backup_service import BackupService

MB = 1024 * 1024


def _gz(payload: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as g:
        g.write(payload)
    return buf.getvalue()


def _mock_setup(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


class BoundedGunzipTests(SimpleTestCase):
    @override_settings(PYRUNNER_BACKUP_MAX_EXPANDED_MB=1)
    def test_expansion_over_the_cap_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            BackupService.deserialize_backup(_gz(b"\0" * (3 * MB)), "b.json.gz")
        self.assertIn("expands to more than 1 MB", str(cm.exception))
        self.assertIn("restore_backup", str(cm.exception))

    @override_settings(PYRUNNER_BACKUP_MAX_EXPANDED_MB=1)
    def test_reading_stops_near_the_cap(self):
        """The guard must stop decompressing at the cap, not inflate everything first."""
        returned = []
        real_read = gzip.GzipFile.read

        def spy(self, size=-1):
            chunk = real_read(self, size)
            returned.append(len(chunk))
            return chunk

        with mock.patch.object(gzip.GzipFile, "read", spy):
            with self.assertRaises(ValueError):
                BackupService.deserialize_backup(_gz(b"\0" * (16 * MB)), "b.json.gz")
        self.assertLessEqual(sum(returned), 2 * MB + 1)

    @override_settings(PYRUNNER_BACKUP_MAX_EXPANDED_MB=1)
    def test_a_backup_under_the_cap_still_loads(self):
        doc = {"version": "1.13.0", "pad": "x" * (MB // 2)}
        self.assertEqual(BackupService.deserialize_backup(_gz(json.dumps(doc).encode()), "b.json.gz"), doc)

    def test_default_cap_covers_the_plain_upload_cap(self):
        """Any backup accepted as plain JSON (<= MAX_BACKUP_SIZE_MB) also loads gzipped."""
        self.assertGreaterEqual(
            BackupService.max_expanded_bytes(), BackupService.MAX_BACKUP_SIZE_MB * MB
        )


class BackupUploadBombViewTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.admin = User.objects.create_superuser(username="bomb-admin", email="bomb-admin@example.com", password="x-Strong-pass-91")
        self.client.force_login(self.admin)

    @override_settings(PYRUNNER_BACKUP_MAX_EXPANDED_MB=1)
    def test_upload_of_a_gzip_bomb_is_refused_with_a_reason(self):
        upload = SimpleUploadedFile("bomb.json.gz", _gz(b"\0" * (4 * MB)), content_type="application/gzip")
        resp = self.client.post(reverse("cpanel:backup_upload"), {"backup_file": upload})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertIn("expands to more than 1 MB", body["error"])
        self.assertNotIn("pending_backup", self.client.session)
