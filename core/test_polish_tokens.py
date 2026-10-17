"""
M4 polish batch (2.0 release test) — API tokens list, Connect, brain grants.

U62   tokens list: bound brain shown, expired = "Expired" (not "Active"), inactive plugin
      vs not installed, phone cards instead of a sideways-scrolling table
U63   Connect: Copy buttons, wrapped snippets, "(disabled)" not "(revoked)", brain.ping text
U65   brain grants: a revoked token's grants go with it, "Deleted API token" for old
      orphans, "Choose an API token…", tier labels
"""

import uuid
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.api.registry import registry
from core.brain.service import BrainService
from core.models import (
    APIToken, BrainGrant, Environment, GlobalSettings, Plugin, User, Workspace,
    WorkspaceMembership,
)
from core.test_brain_store import contract_files


class TokenBase(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        self.ws = Workspace.get_default()
        self.admin = User.objects.create_user(
            username="root@example.com", email="root@example.com", password="x-Long-pass-91",
            is_superuser=True, is_staff=True,
        )
        WorkspaceMembership.objects.update_or_create(
            user=self.admin, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )
        self.client.force_login(self.admin)
        self.brain = BrainService.create(self.ws, "w16-brain", files=contract_files())

    def _api_token(self, name, **extra):
        return APIToken.objects.create(
            name=name, workspace=self.ws, scope="api", token=APIToken.generate_token(scope="api"),
            permissions=["brain:read"], **extra,
        )


class TokenListTests(TokenBase):
    def test_brain_expired_and_plugin_state(self):
        self._api_token("bound", brain=self.brain)
        self._api_token("old", expires_at=timezone.now() - timedelta(days=2))
        Plugin.objects.create(slug="sleepy_plugin", name="Sleepy")
        APIToken.objects.create(
            name="inactive-plugin", workspace=self.ws, scope="plugin", plugin_slug="sleepy_plugin",
            token=APIToken.generate_token(),
        )
        APIToken.objects.create(
            name="gone-plugin", workspace=self.ws, scope="plugin", plugin_slug="gone_plugin",
            token=APIToken.generate_token(),
        )
        html = self.client.get(reverse("cpanel:api_token_list")).content.decode()
        self.assertIn(f'Brain: <span class="text-text">{self.brain.name}</span>', html)
        self.assertIn(">Expired</span>", html)
        self.assertIn("Plugin inactive", html)
        self.assertIn("Plugin not installed", html)
        # Phones get cards; the table is for sm and up.
        self.assertIn('<ul class="sm:hidden divide-y divide-line-soft">', html)
        self.assertIn('<div class="hidden sm:block overflow-x-auto">', html)


class ConnectTests(TokenBase):
    def test_copy_buttons_and_disabled_label(self):
        token = self._api_token("off", is_active=False)
        html = self.client.get(reverse("cpanel:connect") + f"?token={token.pk}").content.decode()
        self.assertGreaterEqual(html.count('class="copy-btn'), 3)
        self.assertIn("off (disabled)", html)
        self.assertNotIn("(revoked)", html)
        self.assertIn("whitespace-pre-wrap break-all", html)

    def test_brain_ping_has_a_description(self):
        endpoint = registry.get("brain.ping")
        self.assertTrue(endpoint.description.strip())


class GrantTests(TokenBase):
    def _grants_url(self):
        return reverse("cpanel:brain_grants", args=[self.brain.pk])

    def test_revoking_a_token_drops_its_grants(self):
        token = self._api_token("connector")
        BrainGrant.objects.create(brain=self.brain, subject_kind="token", subject_id=str(token.pk))
        self.client.post(reverse("cpanel:api_token_revoke", args=[token.pk]))
        self.assertFalse(BrainGrant.objects.filter(subject_id=str(token.pk)).exists())

    def test_orphan_grant_label_tiers_and_article(self):
        orphan = str(uuid.uuid4())
        BrainGrant.objects.create(
            brain=self.brain, subject_kind="token", subject_id=orphan, max_tier="agents-only"
        )
        html = self.client.get(self._grants_url()).content.decode()
        self.assertIn("Deleted API token", html)
        self.assertIn("Agents only", html)
        self.assertNotIn("· agents-only", html)
        self.assertIn("Choose ${articles[kind.value]} ${noun}", html)
