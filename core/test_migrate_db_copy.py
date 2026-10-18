"""
migrate_db copies every table with its keys (F98).

Before 2.0 migrate_db ran through backup/restore, which leaves out users,
memberships, API tokens, AI providers, channels and plugin rows and keeps only
each library's head revision, so a migrated instance came up with no admin.
The CLI only accepts a postgres:// target; the copy itself is engine-agnostic,
so these tests run it into a throwaway SQLite file and read that file back.
"""

import io
import os
import sqlite3
import tempfile

from django.contrib.auth import get_user_model

from django.test import TestCase

from core.management.commands.migrate_db import Command
from core.models import (
    AIProvider,
    APIToken,
    Channel,
    DataStore,
    DataStoreEntry,
    Environment,
    GlobalSettings,
    Library,
    Plugin,
    Script,
    Secret,
    Workspace,
    WorkspaceMembership,
)
from core.services import EncryptionService


class MigrateDbCopyTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser(
            username="mig-admin@example.invalid", email="mig-admin@example.invalid", password="mig-pass-1"
        )
        self.member = User.objects.create_user(
            username="mig-member@example.invalid", email="mig-member@example.invalid", password="mig-pass-2"
        )
        ws = Workspace.get_default()
        ws2 = Workspace.objects.create(name="mig-ws2")
        WorkspaceMembership.objects.create(user=self.member, workspace=ws2, role="admin")
        env = Environment.objects.create(name="mig-env", path="migenv")
        sc = Script.objects.create(name="mig-script", code="print(1)", environment=env, workspace=ws)
        Script.objects.filter(pk=sc.pk).update(created_at=sc.created_at.replace(microsecond=123457))
        s = Secret(key="MIG_SECRET", workspace=ws)
        s.set_value("mig-value")
        s.save()
        ds = DataStore.objects.create(name="mig-store", workspace=ws)
        DataStoreEntry.objects.create(datastore=ds, key="k", value_json='{"a": 1}')
        lib = Library.objects.create(key="miglib", name="miglib", workspace=ws)
        lib.save_revision({"m.py": "A = 1\n"})
        lib.save_revision({"m.py": "A = 2\n"})
        tok = APIToken(token=APIToken.generate_token(scope="datastore"), name="mig-token",
                       scope="datastore", datastore=ds, workspace=ws)
        tok.save()
        self.token_digest = APIToken.objects.get(pk=tok.pk).token
        prov = AIProvider.objects.create(
            provider_type="openrouter", name="mig-provider", base_url="https://openrouter.ai/api",
            credential_encrypted=EncryptionService.encrypt("mig-key"), default_model="m",
        )
        gs = GlobalSettings.get_settings()
        gs.active_ai_provider = prov
        gs.save()
        ch = Channel(workspace=ws, provider="telegram", name="mig-channel")
        ch.set_credentials({"bot_token": "1:mig"}, identity="migbot")
        ch.save()
        Plugin.objects.create(slug="mig_plugin", name="Mig plugin", version="1.0.0", status="inactive")

    def _copy(self):
        tmp = tempfile.mkdtemp(prefix="migdb-test-")
        target = os.path.join(tmp, "target.sqlite3")
        out = io.StringIO()
        checks = Command(stdout=out, stderr=io.StringIO()).copy("sqlite:///" + target.replace("\\", "/"))
        return target, checks, out.getvalue()

    def test_copy_keeps_users_tokens_providers_channels_plugins_and_history(self):
        target, checks, out = self._copy()
        self.assertIn("0 mismatched", out)
        self.assertTrue(all(src == tgt for src, tgt in checks.values()), checks)

        con = sqlite3.connect(target)
        try:
            q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
            users = dict(q("SELECT email, password FROM users"))
            self.assertEqual(users["mig-admin@example.invalid"], get_user_model().objects.get(pk=self.admin.pk).password)
            self.assertIn("mig-member@example.invalid", users)
            self.assertEqual(
                q("SELECT COUNT(*) FROM workspace_memberships")[0][0], WorkspaceMembership.objects.count()
            )
            self.assertEqual(q("SELECT token FROM datastore_api_tokens WHERE name='mig-token'")[0][0], self.token_digest)
            self.assertEqual(q("SELECT COUNT(*) FROM library_revisions")[0][0], 2)
            prov = q("SELECT id, credential_encrypted FROM ai_providers WHERE name='mig-provider'")[0]
            self.assertEqual(EncryptionService.decrypt(prov[1]), "mig-key")
            self.assertEqual(q("SELECT active_ai_provider_id FROM global_settings")[0][0], prov[0])
            self.assertEqual(q("SELECT COUNT(*) FROM channels WHERE name='mig-channel'")[0][0], 1)
            self.assertEqual(q("SELECT COUNT(*) FROM plugins WHERE slug='mig_plugin'")[0][0], 1)
            self.assertEqual(q("SELECT COUNT(*) FROM secrets WHERE key='MIG_SECRET'")[0][0], 1)
            # Times keep their microseconds (Django's JSON encoder rounds to ms).
            script = Script.objects.get(name="mig-script")
            self.assertNotEqual(script.created_at.microsecond % 1000, 0)
            got = q("SELECT created_at FROM scripts WHERE name='mig-script'")[0][0]
            self.assertIn(script.created_at.strftime("%H:%M:%S.%f"), got)
        finally:
            con.close()


class MigrateDbLoadGuardTests(TestCase):
    def test_load_flag_refuses_outside_migrate_db(self):
        """The internal load step empties its database; it must not run by hand."""
        from django.core.management import call_command
        from django.core.management.base import CommandError

        get_user_model().objects.create_user(username="keep@example.invalid", email="keep@example.invalid")
        with self.assertRaisesMessage(CommandError, "internal"):
            call_command("migrate_db", load="anything.json.gz")
        self.assertTrue(get_user_model().objects.filter(username="keep@example.invalid").exists())


class MembershipSignalRawTests(TestCase):
    def test_fixture_load_does_not_add_a_membership(self):
        """A raw save (loaddata) must not create the default-workspace membership."""
        User = get_user_model()
        u = User(username="raw@example.invalid", email="raw@example.invalid")
        u.save_base(raw=True)
        self.assertFalse(WorkspaceMembership.objects.filter(user=u).exists())

    def test_normal_create_still_adds_a_membership(self):
        u = get_user_model().objects.create_user(username="n@example.invalid", email="n@example.invalid", password="x")
        self.assertTrue(WorkspaceMembership.objects.filter(user=u, workspace=Workspace.get_default()).exists())

