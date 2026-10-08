"""Consumer SDK boundaries stay safe when private plugins are absent."""

import json
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from core.api.exceptions import ApiError
from core.brain.service import BrainService
from core.models import AIProvider, BrainGrant, Environment, GlobalSettings, Run, Script, Workspace
from core.plugins.api import AIProviderAPI, BrainAPI, PublicPageAPI, VectorAPI
from core.script_helpers import pyrunner_brain, pyrunner_netguard
from core.services.datastore_token import mint_datastore_token
from core.test_brain_store import contract_files, note
from core.test_plugin_api import _mock_setup


class RagSDKTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.get_default()
        self.other = Workspace.objects.create(name="Foreign")

    def test_capability_resolves_only_live_owner_page_and_workspace(self):
        api = PublicPageAPI("rag_chatbot", self.ws)
        path = api.share("chat")
        token = path.strip("/").split("/")[-1]
        self.assertEqual(api.resolve_token(token, page_name="chat").workspace, self.ws)
        self.assertIsNone(PublicPageAPI("other").resolve_token(token, page_name="chat"))
        self.assertIsNone(PublicPageAPI("rag_chatbot", self.other).resolve_token(token, page_name="chat"))
        self.assertIsNone(api.resolve_token(token, page_name="other"))
        api.revoke("chat")
        self.assertIsNone(api.resolve_token(token, page_name="chat"))
        self.assertNotEqual(api.share("chat"), path)
        api.share("chat", expires_at=timezone.now() - timezone.timedelta(seconds=1))
        self.assertIsNone(api.resolve_token(token, page_name="chat"))

    def test_sdk_tier_only_lowers_an_existing_grant(self):
        brain = BrainService.create(self.ws, "rag", files={**contract_files(),
            "knowledge/takes/a.md": note("public", "public") + " text (agents-only: HIDDEN)",
            "knowledge/takes/b.md": note("private", "private")})
        grant = BrainGrant.objects.create(brain=brain, subject_kind="plugin", subject_id="rag_chatbot", max_tier="private")
        api = BrainAPI("rag_chatbot", self.ws, tier="public").get_brain("rag")
        self.assertNotIn("HIDDEN", api.note("public")["content"])
        self.assertNotIn("private", [n["entity_id"] for n in api.notes()["notes"]])
        grant.max_tier = "public"
        grant.save()
        self.assertEqual(BrainAPI("rag_chatbot", self.ws, tier="private").get_brain("rag").ping()["tier"], "public")
        with self.assertRaises(ApiError):
            BrainAPI("rag_chatbot", self.other, tier="public").get_brain("rag")
        grant.delete()
        with self.assertRaises(ApiError):
            api.note("public")

    def test_loopback_tier_and_helper_propagate_without_escalation(self):
        brain = BrainService.create(self.ws, "rag", files=contract_files())
        script = Script.objects.create(workspace=self.ws, name="Consumer", environment=Environment.objects.create(name="RAG"))
        BrainGrant.objects.create(brain=brain, subject_kind="script", subject_id=str(script.pk), max_tier="agents-only")
        run = Run.objects.create(script=script, workspace=self.ws, code_snapshot="pass")
        headers = {"HTTP_AUTHORIZATION": "Bearer " + mint_datastore_token(run.id), "REMOTE_ADDR": "127.0.0.1"}
        for tier, expected in (("public", "public"), ("private", "agents-only")):
            response = self.client.post("/internal/brains/rag/ping", json.dumps({"_tier": tier}), content_type="application/json", **headers)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()["data"]["tier"], expected)
        response = self.client.post("/internal/brains/rag/ping", '{"_tier": []}', content_type="application/json", **headers)
        self.assertEqual(response.status_code, 400)
        caller = Mock(return_value={})
        pyrunner_brain.Brain("rag", tier="public", _call=caller).notes()
        self.assertEqual(caller.call_args.args[2]["_tier"], "public")

    def test_choices_expose_names_without_credentials(self):
        from core.models import EmbeddingProvider, VectorConnection
        EmbeddingProvider.objects.create(name="on", enabled=True, dimension=3, model="m")
        EmbeddingProvider.objects.create(name="off", enabled=False, dimension=3, model="m")
        VectorConnection.objects.create(name="off", enabled=False)
        self.assertEqual(VectorAPI.choices()["providers"], ["on"])
        self.assertNotIn("off", VectorAPI.choices()["connections"])

    def provider(self):
        from core.services.encryption_service import EncryptionService
        provider = AIProvider.objects.create(name="RAG AI", provider_type="custom", base_url="https://example.com", default_model="stub",
                                             credential_encrypted=EncryptionService.encrypt("TEST_ONLY_CREDENTIAL"))
        settings = GlobalSettings.get_settings()
        settings.claude_enabled = True
        settings.save()
        return provider

    def test_completion_uses_profile_no_tools_and_bounded_session(self):
        self.provider()
        response = Mock()
        response.json.return_value = {"content": [{"type": "text", "text": "answer"}], "usage": {"input_tokens": 3, "output_tokens": 4}}
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=None)
        session.post.return_value = response
        with patch("core.plugins.completion.BudgetSession", return_value=session), patch("core.plugins.completion.is_safe_endpoint_url", return_value=(True, "")):
            result = AIProviderAPI.complete("RAG AI", system="system", prompt="question", timeout=2)
        self.assertEqual(result["text"], "answer")
        payload = session.post.call_args.kwargs
        self.assertEqual(payload["headers"]["Authorization"], "Bearer TEST_ONLY_CREDENTIAL")
        self.assertNotIn("tools", payload["json"])

    def test_completion_refuses_disabled_subscription_and_unsafe_profile(self):
        provider = self.provider()
        provider.auth_method = "subscription"
        provider.save()
        self.assertEqual(AIProviderAPI.list(), [])
        with self.assertRaises(ApiError):
            AIProviderAPI.complete(provider.name, system="", prompt="q")
        provider.auth_method = "api_key"
        provider.base_url = "http://169.254.169.254"
        provider.save()
        with self.assertRaises(ApiError):
            AIProviderAPI.complete(provider.name, system="", prompt="q")

    def test_stdlib_network_policy_preserves_metadata_fence(self):
        for flag in ("true", "false"):
            with patch.dict("os.environ", {"PYRUNNER_ALLOW_PRIVATE_NETWORK": flag}):
                self.assertEqual(pyrunner_netguard.is_safe_endpoint_url("http://localhost")[0], flag == "true")
                self.assertFalse(pyrunner_netguard.is_safe_endpoint_url("http://169.254.169.254")[0])
