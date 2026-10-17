"""
Datastore and plugin API tokens are stored hashed, like Unified API tokens (F53).

1.x kept the raw value of datastore (single/all stores) and plugin tokens in the
database, so a copy of the SQLite file or a dump was enough to use them. They
are now stored as a SHA-256 digest with a "first 8...last 4" hint, and
migration 0065 hashes the plaintext rows an upgraded instance still holds —
the tokens keep working after the upgrade.
"""

import importlib

from django.apps import apps as django_apps
from django.test import TestCase
from django.urls import reverse

from core.models import (
    APIToken,
    DataStore,
    Environment,
    GlobalSettings,
    User,
    Workspace,
    WorkspaceMembership,
)

migration_0065 = importlib.import_module("core.migrations.0065_hash_datastore_plugin_tokens")


def _auth(raw):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


class DatastoreTokenHashingTests(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.create(name="env", path="p", is_active=True, is_default=True)
        self.ws = Workspace.get_default()
        self.user = User.objects.create(email="root@example.com", is_superuser=True, is_staff=True)
        WorkspaceMembership.ensure(self.user, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.store = DataStore.objects.create(name="alpha", workspace=self.ws)

    def test_console_created_datastore_token_is_stored_as_digest(self):
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("cpanel:api_token_create"), {"name": "all", "scope": "datastores"}
        )
        self.assertEqual(resp.status_code, 302)
        token = APIToken.objects.get(name="all")
        raw = self.client.session["new_api_token"]

        self.assertNotEqual(token.token, raw)
        self.assertEqual(token.token, APIToken.digest(raw))
        self.assertEqual(token.token_hint, f"{raw[:8]}...{raw[-4:]}")
        # Shown once on the created page, never in the list.
        self.assertContains(self.client.get(resp.url), raw)
        listing = self.client.get(reverse("cpanel:api_token_list"))
        self.assertNotContains(listing, raw)
        self.assertContains(listing, token.token_hint)
        # The raw value authenticates; the stored digest does not.
        self.assertEqual(self.client.get("/api/v1/datastores/", **_auth(raw)).status_code, 200)
        self.assertEqual(
            self.client.get("/api/v1/datastores/", **_auth(token.token)).status_code, 401
        )

    def test_status_update_keeps_the_digest(self):
        token = APIToken.objects.create(
            name="s", token=APIToken.generate_token(), datastore=self.store
        )
        raw, digest = token.raw_token, token.token
        token.is_active = False
        token.save(update_fields=["is_active"])
        token.refresh_from_db()
        self.assertEqual(token.token, digest)
        token.is_active = True
        token.save(update_fields=["is_active"])
        self.assertEqual(self.client.get("/api/v1/datastores/", **_auth(raw)).status_code, 200)

    def test_migration_hashes_plaintext_rows_and_they_keep_working(self):
        raw_store = APIToken.generate_token()
        raw_plugin = APIToken.generate_token()
        store_tok = APIToken.objects.create(name="old-store", token=raw_store, datastore=self.store)
        plugin_tok = APIToken.objects.create(
            name="old-plugin", token=raw_plugin, scope="plugin",
            plugin_slug="brand_tracker", workspace=self.ws,
        )
        # Put the rows back into their 1.x shape: plaintext token, no hint.
        APIToken.objects.filter(pk=store_tok.pk).update(token=raw_store, token_hint="")
        APIToken.objects.filter(pk=plugin_tok.pk).update(token=raw_plugin, token_hint="")

        migration_0065.hash_plaintext_tokens(django_apps, None)
        migration_0065.hash_plaintext_tokens(django_apps, None)  # idempotent

        store_tok.refresh_from_db()
        plugin_tok.refresh_from_db()
        self.assertEqual(store_tok.token, APIToken.digest(raw_store))
        self.assertEqual(store_tok.token_hint, f"{raw_store[:8]}...{raw_store[-4:]}")
        self.assertEqual(plugin_tok.token, APIToken.digest(raw_plugin))
        self.assertEqual(
            self.client.get("/api/v1/datastores/alpha/", **_auth(raw_store)).status_code, 200
        )
        # The plugin token still authenticates (then is refused for scope, not identity).
        resp = self.client.get("/api/v1/datastores/", **_auth(raw_plugin))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "SCOPE_MISMATCH")
