"""One Brain contract across REST, MCP, workers, plugin SDK and agent tools."""

import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.api.context import token_context
from core.api.errors import ApiError
from core.api.registry import registry
from core.brain.access import principal_context
from core.brain.service import BrainService
from core.brain.tools import build_tools
from core.models import APIToken, Brain, BrainGrant, Environment, Run, Script, Workspace
from core.plugins.api import BrainAPI
from core.script_helpers import pyrunner_brain
from core.services.datastore_token import mint_datastore_token
from core.test_brain_store import contract_files, note
from core.test_plugin_api import _mock_setup


class BrainDoorTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.ws = Workspace.get_default()
        self.other = Workspace.objects.create(name="Other")
        self.files = {
            **contract_files(),
            "INDEX.md": "OLD INDEX PRIVATE CANARY",
            "knowledge/takes/public.md": note(
                "take-public",
                "public",
                'title: "Visible (agents-only: TITLE CANARY)"\n'
                'description: "Summary (agents-only: DESCRIPTION CANARY)"\n'
                'topics: [tools, "(agents-only: SECRET TOPIC)"]\nprojects: [demo]\n',
            )
            + "\nPublic text (agents-only: BODY CANARY (nested)) raw/public.md",
            "knowledge/takes/agent.md": note("take-agent") + " raw/agent.md",
            "knowledge/takes/private.md": note("take-private", "private"),
            "knowledge/takes/old.md": note("take-old", "public", "status: superseded\n"),
            "raw/public.md": "Raw (agents-only: RAW CANARY)",
            "raw/agent.md": "Agent raw",
            "raw/unlinked.md": "Private raw",
        }
        self.brain = BrainService.create(self.ws, "mind", files=self.files)
        self.foreign = BrainService.create(self.other, "foreign", files=contract_files())
        self.raw = APIToken.generate_token(scope="api")
        self.token = APIToken.objects.create(
            name="Reader",
            token=self.raw,
            scope="api",
            permissions=["brain:read"],
            brain_tier="public",
            workspace=self.ws,
        )
        self.environment = Environment.objects.create(name="Brain test")
        self.script = Script.objects.create(
            name="Consumer", workspace=self.ws, environment=self.environment
        )
        self.run = Run.objects.create(script=self.script, workspace=self.ws)
        self.signed = mint_datastore_token(self.run.pk)
        for kind, identifier in (
            ("token", self.token.pk),
            ("script", self.script.pk),
            ("plugin", "consumer"),
            ("agent", "pyai"),
        ):
            BrainGrant.objects.create(
                brain=self.brain, subject_kind=kind, subject_id=str(identifier), max_tier="public"
            )

    def call(self, op, **params):
        return registry.call(f"brain.{op}", token_context(self.token), {"brain": "mind", **params})

    def rest(self, op, params):
        return self.client.post(
            f"/api/v1/brain.{op}",
            {"brain": "mind", **params},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.raw,
        )

    def internal(self, op, params=None, **headers):
        headers.setdefault("HTTP_AUTHORIZATION", "Bearer " + self.signed)
        headers.setdefault("REMOTE_ADDR", "127.0.0.1")
        return self.client.post(
            f"/internal/brains/mind/{op}", params or {}, content_type="application/json", **headers
        )

    def rpc(self, op, params):
        return self.client.post(
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": f"brain-{op}", "arguments": {"brain": "mind", **params}},
            },
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.raw,
            HTTP_ACCEPT="application/json, text/event-stream",
            HTTP_MCP_PROTOCOL_VERSION="2025-11-25",
        )

    def test_identical_read_data_on_every_door(self):
        agent_tools = {tool.name: tool for tool in build_tools(self.ws)}
        sdk = BrainAPI("consumer", self.ws).get_brain("mind")
        operations = {
            "ping": {},
            "get-index": {},
            "list-notes": {},
            "get-note": {"entity_id": "take-public"},
            "get-lens": {"name": "default"},
            "get-identity": {},
            "get-raw": {"path": "raw/public.md"},
        }
        # Identity and lenses in the source contract default to agents-only.
        for grant in self.brain.grants.all():
            grant.max_tier = "agents-only"
            grant.save()
        self.token.brain_tier = "agents-only"
        self.token.save()
        for operation, params in operations.items():
            with self.subTest(operation=operation):
                expected = self.call(operation, **params)
                self.assertEqual(self.rest(operation, params).json()["data"], expected)
                self.assertEqual(self.internal(operation, params).json()["data"], expected)
                rpc = self.rpc(operation, params).json()["result"]
                self.assertEqual(json.loads(rpc["content"][0]["text"]), expected)
                self.assertEqual(sdk._call(operation, **params), expected)
                result = async_to_sync(agent_tools[f"brain-{operation}"].handler)(
                    {"brain": "mind", **params}
                )
                self.assertEqual(json.loads(result["content"][0]["text"]), expected)

    def test_public_filters_body_metadata_raw_and_legacy_index(self):
        text = json.dumps(
            [
                self.call("get-index"),
                self.call("list-notes"),
                self.call("get-note", entity_id="take-public"),
                self.call("get-raw", path="raw/public.md"),
            ]
        )
        for secret in ("CANARY", "SECRET TOPIC", "take-agent", "take-private", "OLD INDEX"):
            self.assertNotIn(secret, text)
        self.assertIn("Public text", text)
        self.assertIn("take-public", text)

    def test_note_filters_and_superseded_semantics(self):
        self.assertEqual(self.call("list-notes", topic="tools", project="demo")["count"], 1)
        self.assertEqual(self.call("list-notes", kind="fact")["count"], 0)
        self.assertEqual(
            self.call("list-notes", status="superseded")["notes"][0]["entity_id"], "take-old"
        )
        self.assertEqual(self.call("list-notes", kind="take", status="")["count"], 2)

    def test_filter_cannot_probe_hidden_topic_metadata(self):
        self.assertEqual(self.call("list-notes", topic="(agents-only: SECRET TOPIC)")["count"], 0)

    def test_unknown_shapes_match_above_tier_missing_and_no_grant(self):
        missing = self.rest("get-note", {"entity_id": "missing"})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            self.rest("get-note", {"entity_id": "take-private"}).json(), missing.json()
        )
        self.brain.grants.filter(subject_kind="token").delete()
        self.assertEqual(self.rest("get-note", {"entity_id": "take-public"}).json(), missing.json())

    def test_raw_path_confinement_and_link_visibility(self):
        for path in (
            "raw/../CLAUDE.md",
            "CLAUDE.md",
            "raw/agent.md",
            "raw/unlinked.md",
            "raw\\public.md",
            "raw/missing.md",
            "/raw/public.md",
        ):
            with self.subTest(path=path), self.assertRaises(ApiError) as error:
                self.call("get-raw", path=path)
            self.assertEqual(error.exception.status, 404)

    def test_tier_is_minimum_of_token_and_grant(self):
        self.brain.grants.filter(subject_kind="token").update(max_tier="private")
        with self.assertRaises(ApiError):
            self.call("get-note", entity_id="take-agent")
        self.token.brain_tier = "agents-only"
        self.assertIn("take-agent", self.call("get-note", entity_id="take-agent")["content"])
        with self.assertRaises(ApiError):
            self.call("get-note", entity_id="take-private")
        self.token.brain_tier = "private"
        self.assertEqual(self.call("get-raw", path="raw/unlinked.md")["content"], "Private raw")

    def test_binding_and_workspace_hold_even_with_foreign_grant(self):
        BrainGrant.objects.create(
            brain=self.foreign, subject_kind="token", subject_id=str(self.token.pk)
        )
        self.token.brain = self.brain
        ctx = token_context(self.token)
        self.assertTrue(registry.call("brain.ping", ctx, {})["pong"])
        with self.assertRaises(ApiError):
            registry.call("brain.ping", ctx, {"brain": "foreign"})
        self.assertEqual(
            [b["slug"] for b in registry.call("brain.list", ctx, {})["brains"]], ["mind"]
        )

    def test_list_paginates_accessible_brains_only(self):
        owned = BrainAPI("consumer", self.ws).provision("owned")
        ctx = principal_context(self.ws, "plugin", "consumer")
        first = registry.call("brain.list", ctx, {"limit": 1})
        self.assertEqual(first["brains"][0]["slug"], owned.slug)
        self.assertEqual(first["next_offset"], 1)
        self.assertEqual(
            registry.call("brain.list", ctx, {"offset": 1})["brains"][0]["slug"], "mind"
        )

    def test_list_parity_includes_worker_catalog(self):
        expected = registry.call("brain.list", token_context(self.token), {})
        response = self.client.post(
            "/internal/brains",
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.signed,
        )
        self.assertEqual(response.json()["data"], expected)
        response = self.client.post(
            "/api/v1/brain.list",
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.raw,
        )
        self.assertEqual(response.json()["data"], expected)
        self.assertEqual(BrainAPI("consumer", self.ws).list(), expected)
        response = self.client.post(
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "brain-list", "arguments": {}},
            },
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.raw,
            HTTP_ACCEPT="application/json, text/event-stream",
            HTTP_MCP_PROTOCOL_VERSION="2025-11-25",
        )
        self.assertEqual(json.loads(response.json()["result"]["content"][0]["text"]), expected)
        agent_tool = next(t for t in build_tools(self.ws) if t.name == "brain-list")
        result = async_to_sync(agent_tool.handler)({})
        self.assertEqual(json.loads(result["content"][0]["text"]), expected)

    def test_script_inherits_plugin_grant_and_owned_brain(self):
        self.brain.grants.filter(subject_kind="script").delete()
        self.assertEqual(self.internal("ping").status_code, 404)
        self.script.owner_plugin = "consumer"
        self.script.save()
        self.assertEqual(self.internal("ping").json()["data"]["tier"], "public")
        BrainAPI("consumer", self.ws).provision("owned")
        ctx = principal_context(self.ws, "script", self.script.pk)
        self.assertEqual(
            registry.call("brain.ping", ctx, {"brain": "consumer-owned"})["tier"], "private"
        )

    def test_internal_auth_server_identity_and_payload_bounds(self):
        self.assertEqual(self.internal("ping", REMOTE_ADDR="203.0.113.5").status_code, 403)
        self.assertEqual(
            self.internal("ping", HTTP_AUTHORIZATION="Bearer invalid").status_code, 401
        )
        self.assertEqual(self.internal("ping", {"brain": "foreign"}).status_code, 400)
        self.assertEqual(self.internal("ping", {"tier": "private"}).status_code, 400)
        self.assertEqual(self.internal("ping", {"pad": "x" * 1048576}).status_code, 413)
        self.run.workspace = self.other
        self.run.save()
        self.assertEqual(self.internal("ping").status_code, 404)
        self.run.delete()
        self.assertEqual(self.internal("ping").status_code, 404)

    def test_helper_uses_signed_loopback_with_same_shapes(self):
        def urlopen(request, timeout):
            self.assertEqual(timeout, 60)
            self.assertEqual(request.headers["Authorization"], "Bearer " + self.signed)
            response = self.client.post(
                request.full_url.split("18201", 1)[1],
                request.data,
                content_type="application/json",
                HTTP_AUTHORIZATION=request.headers["Authorization"],
            )
            self.assertEqual(response.status_code, 200)
            return io.BytesIO(response.content)

        with (
            patch.dict(
                os.environ,
                {
                    "PYRUNNER_INTERNAL_URL": "http://127.0.0.1:18201",
                    "PYRUNNER_INTERNAL_TOKEN": self.signed,
                },
            ),
            patch("urllib.request.urlopen", side_effect=urlopen),
        ):
            brain = pyrunner_brain.get_brain("mind")
            self.assertEqual(
                pyrunner_brain.list_brains(),
                registry.call("brain.list", token_context(self.token), {}),
            )
            self.assertEqual(brain.index(), self.call("get-index"))
            self.assertEqual(
                brain.note("take-public"), self.call("get-note", entity_id="take-public")
            )

    def test_pyai_tools_filtered_and_recheck_revoked_grants(self):
        self.assertEqual(len(build_tools(self.ws)), 8)
        self.assertEqual(build_tools(self.other), [])
        tool = next(t for t in build_tools(self.ws) if t.name == "brain-ping")
        self.brain.grants.filter(subject_kind="agent").delete()
        self.assertEqual(build_tools(self.ws), [])
        result = async_to_sync(tool.handler)({"brain": "mind"})
        self.assertTrue(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["error"]["code"], "not_found")

    def test_sdk_provision_is_idempotent_and_content_is_not_reseeded(self):
        api = BrainAPI("consumer", self.ws)
        owned = api.provision("owned")
        brain = Brain.objects.get(workspace=self.ws, slug=owned.slug)
        revision = brain.head_revision_id
        self.assertEqual(api.provision("owned").slug, owned.slug)
        brain.refresh_from_db()
        self.assertEqual(brain.head_revision_id, revision)
        self.assertEqual(owned.ping()["tier"], "private")
        with self.assertRaises(ApiError):
            BrainAPI("different", self.ws).get_brain(owned.slug)
        with self.assertRaises(ApiError):
            api.provision("owned", slug="changed")
        with self.assertRaises(ValidationError):
            BrainAPI("different", self.ws).provision("owned", slug=owned.slug)

    def test_sdk_grant_only_owned_and_subject_scoped(self):
        api = BrainAPI("consumer", self.ws)
        api.provision("owned")
        grant = api.grant(
            "owned",
            subject_kind="script",
            subject_id=self.script.pk,
            max_tier="agents-only",
            can_propose=True,
        )
        self.assertTrue(grant.can_propose)
        with self.assertRaises(ValidationError):
            api.grant("owned", subject_kind="script", subject_id=self.script.pk, can_propose=True)
        foreign = Script.objects.create(
            name="Foreign", workspace=self.other, environment=self.environment
        )
        with self.assertRaises(ValidationError):
            api.grant("owned", subject_kind="script", subject_id=foreign.pk)
        with self.assertRaises(ApiError):
            api.grant("mind", subject_kind="agent", subject_id="pyai")

    def test_doctor_brain_declarations_and_numeric_versions(self):
        from core.services.plugin_doctor import run_doctor

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "sample"
            folder.mkdir()
            for version, count, valid in (
                ("2.6", 1, False),
                ("2.7", 1, True),
                ("2.10", 1, True),
                ("2.7", -1, False),
            ):
                (folder / "plugin.json").write_text(
                    json.dumps({"slug": "sample", "api": version, "provisions": {"brains": count}}),
                    encoding="utf-8",
                )
                findings = [f for f in run_doctor(folder).findings if f.rule == "metadata"]
                self.assertEqual(not any(f.severity == "fail" for f in findings), valid)

    def test_plugin_remove_data_counts_and_cascades_owned_brain_only(self):
        from core.services.plugin_service import PluginService

        BrainAPI("consumer", self.ws).provision("owned")
        self.assertEqual(PluginService.owned_resource_counts("consumer")["brains"], 1)
        with patch("core.plugins.api.StorageAPI.delete_all", return_value=0):
            PluginService._cleanup_owned_resources("consumer")
        self.assertFalse(Brain.objects.filter(owner_plugin="consumer").exists())
        self.assertTrue(Brain.objects.filter(pk=self.brain.pk).exists())

    def test_index_and_listing_do_not_load_blobs(self):
        with patch(
            "core.brain.reads.BrainService.files", side_effect=AssertionError("Unneeded blob read")
        ):
            self.call("get-index")
            self.call("list-notes")

    def test_agent_calls_have_distinct_audit_request_ids(self):
        tool = next(t for t in build_tools(self.ws) if t.name == "brain-ping")
        with self.assertLogs("core.api.registry", level="INFO") as log:
            async_to_sync(tool.handler)({"brain": "mind"})
            async_to_sync(tool.handler)({"brain": "mind"})
        identifiers = [line.split("request_id=")[1] for line in log.output]
        self.assertEqual(len(set(identifiers)), 2)

    def test_unseeded_identity_has_unknown_raw_not_internal_error(self):
        empty = Brain.objects.create(workspace=self.ws, slug="empty", name="Empty")
        BrainGrant.objects.create(brain=empty, subject_kind="token", subject_id=str(self.token.pk))
        with self.assertRaises(ApiError) as error:
            registry.call(
                "brain.get-raw", token_context(self.token), {"brain": "empty", "path": "raw/no.md"}
            )
        self.assertEqual(error.exception.status, 404)
