"""Brain Grants pickers for plugins and agents + the Py AI brains panel (D14 item 2)."""

import json
import re

from django.test import TestCase
from django.urls import reverse

from core.brain.service import BrainService
from core.models import BrainGrant, Channel, Plugin, User, Workspace, WorkspaceMembership
from core.test_brain_store import contract_files
from core.test_plugin_api import _mock_setup


class GrantPickerTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.objects.create(name="Pickers")
        self.other = Workspace.objects.create(name="Elsewhere")
        self.owner = User.objects.create_user(username="pick-owner", email="owner@pick.test")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.owner, role="owner")
        self.brain = BrainService.create(self.ws, "pickers", files=contract_files())
        self.client.force_login(self.owner)
        Plugin.objects.create(slug="brand_tracker", name="Brand Tracker", status="active")
        Plugin.objects.create(slug="old_thing", name="Old Thing", status="inactive")
        self.bot = Channel.objects.create(workspace=self.ws, name="Ops bot", provider="telegram", inbound_handler="pyai")
        self.script_bot = Channel.objects.create(workspace=self.ws, name="Runner bot", provider="telegram", inbound_handler="script")
        Channel.objects.create(workspace=self.other, name="Foreign bot", provider="telegram", inbound_handler="pyai")

    def url(self, view="grants", *args):
        return reverse("cpanel_ws:brain_" + view, args=[self.ws.pk, self.brain.pk, *args])

    def choices(self, response):
        raw = re.search(r'<script id="grant-subject-choices" type="application/json">(.*?)</script>',
                        response.content.decode(), re.S).group(1)
        return json.loads(raw)

    def test_plugin_and_agent_choices_use_friendly_names_and_principal_ids(self):
        response = self.client.get(self.url())
        choices = self.choices(response)
        self.assertEqual(
            choices["plugin"],
            [{"id": "brand_tracker", "label": "Brand Tracker"}, {"id": "old_thing", "label": "Old Thing · inactive"}],
        )
        self.assertEqual(choices["agent"], [
            {"id": "pyai", "label": "Py AI (console)"},
            {"id": f"telegram:{self.bot.pk}", "label": "Py AI on Ops bot (Telegram)"},
        ])  # the script-handler bot and the other workspace's bot are not offered
        html = response.content.decode()
        self.assertNotIn("UUID", html)
        self.assertNotIn("Foreign bot", html)

    def test_saving_and_listing_agent_and_plugin_grants_shows_labels(self):
        for kind, ident in (("agent", f"telegram:{self.bot.pk}"), ("agent", "pyai"), ("plugin", "brand_tracker")):
            response = self.client.post(self.url(), {"subject_kind": kind, "subject_id": ident, "max_tier": "agents-only"})
            self.assertEqual(response.status_code, 302)
        self.assertEqual(self.brain.grants.count(), 3)
        # A channel moved off Py AI keeps its name on the existing grant.
        BrainGrant.objects.create(brain=self.brain, subject_kind="agent", subject_id=f"telegram:{self.script_bot.pk}")
        response = self.client.get(self.url())
        for label in ("Py AI on Ops bot (Telegram)", "Py AI (console)", "Brand Tracker", "Py AI on Runner bot (Telegram)"):
            self.assertContains(response, f'<p class="break-words">{label}</p>', html=True)

    def test_server_side_validation_still_applies(self):
        response = self.client.post(self.url(), {"subject_kind": "plugin", "subject_id": "Not A Slug!", "max_tier": "public"})
        self.assertContains(response, "Enter a valid subject identifier")
        response = self.client.post(self.url(), {"subject_kind": "agent", "subject_id": "pyai", "max_tier": "public", "can_propose": "on"})
        self.assertContains(response, "Proposals require agents-only or private tier")
        self.assertFalse(self.brain.grants.exists())


class PyAIBrainsPanelTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.get_default()
        self.admin = User.objects.create_user(username="pyai-admin", email="a@pyai.test", is_superuser=True, is_staff=True)
        WorkspaceMembership.ensure(self.admin, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(self.admin)

    def test_lists_readable_brains_with_tier_and_propose_and_links_to_grants(self):
        readable = BrainService.create(self.ws, "ops-notes", files=contract_files())
        hidden = BrainService.create(self.ws, "hr-notes", files=contract_files())
        channel_only = BrainService.create(self.ws, "bot-notes", files=contract_files())
        BrainGrant.objects.create(brain=readable, subject_kind="agent", subject_id="pyai", max_tier="private", can_propose=True)
        BrainGrant.objects.create(brain=channel_only, subject_kind="agent", subject_id="telegram:123")
        html = self.client.get(reverse("cpanel:pyai")).content.decode()
        self.assertIn("Brains Py AI can read", html)
        self.assertIn(readable.name, html)
        self.assertIn("Private · can propose", html)
        self.assertIn(f"/brains/{readable.pk}/grants/", html)
        self.assertNotIn(hidden.name, html)
        self.assertNotIn(channel_only.name, html)
        self.assertIn("1 brain", html)

    def test_no_grants_explains_how_to_grant(self):
        BrainService.create(self.ws, "lonely", files=contract_files())
        html = self.client.get(reverse("cpanel:pyai")).content.decode()
        self.assertIn("Agent → Py AI (console)", html)
        self.assertIn("Grant access to a brain", html)
        self.assertIn("No brains", html)
