"""Tier-confined reader, asynchronous console and canonical vector recall."""

import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from asgiref.sync import async_to_sync
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.api.context import token_context
from core.api.exceptions import ApiError
from core.api.registry import registry
from core.brain import jobs, recall, reader
from core.brain.backup import export_brains, import_brains
from core.brain.reads import snapshot
from core.brain.runtime import AgentResult
from core.brain.service import BrainService
from core.brain.snapshots import materialize, reader_hooks
from core.models import (
    APIToken,
    BrainGrant,
    BrainReaderJob,
    EmbeddingProvider,
    User,
    VectorConnection,
    Workspace,
    WorkspaceMembership,
)
from core.test_brain_store import contract_files, note
from core.test_plugin_api import _mock_setup
from core.vectors.embeddings import EmbeddingResult
from core.vectors.service import VectorService


class ReaderFixture:
    def setUp(self):
        _mock_setup(self)
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.enterContext(self.settings(DATA_DIR=self.root, VECTORS_DIR=self.root / "vectors"))
        (self.root / "vectors").mkdir()
        self.ws = Workspace.objects.create(name="Reader tests")
        self.other = Workspace.objects.create(name="Other")
        self.user = User.objects.create_user(username="reader", email="reader@example.test")
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.user, role="owner")
        self.files = {
            **contract_files(),
            "knowledge/takes/public.md": note("take-public", "public", "topics: [tools]\n")
            + " visible text (agents-only: SECRET SPAN)",
            "knowledge/takes/private.md": note("take-private", "private") + " PRIVATE SENTINEL",
            "raw/hidden.md": "HIDDEN RAW",
        }
        self.files["CLAUDE.md"] += "\nPRIVATE CONTRACT"
        self.files["lenses/default.md"] = "---\nvisibility: agents-only\n---\n# Lens"
        self.brain = BrainService.create(self.ws, "mind", files=self.files)
        self.token = APIToken.objects.create(
            name="Reader",
            token=APIToken.generate_token(scope="api"),
            scope="api",
            permissions=["brain:read"],
            brain_tier="public",
            workspace=self.ws,
        )
        self.grant = BrainGrant.objects.create(
            brain=self.brain, subject_kind="token", subject_id=str(self.token.pk), max_tier="public"
        )
        self.ctx = token_context(self.token)
        self.params = {"brain": "mind", "task": "Use the public take"}

    def agent(self, *args, **kwargs):
        path = Path(kwargs["cwd"]) / "knowledge/takes/public.md"
        observed = kwargs["hooks"]["PostToolUse"][0].hooks[0]
        async_to_sync(observed)(
            {"tool_name": "Read", "tool_input": {"file_path": str(path)}}, "id", None
        )
        return AgentResult(
            output={
                "context_pack": "Visible context",
                "entity_ids_used": ["take-public"],
                "gaps": [],
            },
            model="stub",
        )

    def url(self, name, *args):
        return reverse("cpanel_ws:brain_" + name, args=[self.ws.pk, self.brain.pk, *args])

    def vectors(self):
        connection = VectorConnection.objects.create(
            name="Reader local", provider_type="local", enabled=True
        )
        provider = EmbeddingProvider.objects.create(
            name="Reader embeddings",
            provider_type="openai",
            model="stub",
            dimension=2,
            enabled=True,
        )
        collection = VectorService.create_collection(
            workspace=self.ws, name="brain-test", connection=connection, embedding_provider=provider
        )
        self.brain.vector_collection = collection
        self.brain.save(update_fields=["vector_collection"])
        self.embedding = self.enterContext(
            patch(
                "core.vectors.embeddings.EmbeddingClient.embed",
                side_effect=lambda texts, **kw: EmbeddingResult(
                    vectors=[[1.0, 0.0] for text in texts], tokens=len(texts), calls=1
                ),
            )
        )
        return collection


class BrainReaderTests(ReaderFixture, TestCase):
    def test_materialized_view_has_only_visible_bytes_and_generated_index(self):
        view = snapshot(self.ctx, self.params)
        path = materialize(view)
        self.assertEqual(
            path,
            self.root / "brains" / str(self.brain.pk) / str(self.brain.head_revision_id) / "public",
        )
        text = "\n".join(p.read_text(encoding="utf-8") for p in path.rglob("*") if p.is_file())
        for secret in ("SECRET", "PRIVATE", "HIDDEN RAW", "take-private"):
            self.assertNotIn(secret, text)
        self.assertFalse((path / "CLAUDE.md").exists())
        self.assertIn("generated view", text)
        self.assertEqual(materialize(view), path)

    def test_corrupt_snapshot_is_rejected_before_model_reads(self):
        view = snapshot(self.ctx, self.params)
        path = materialize(view)
        (path / "private.md").write_text("CORRUPT CANARY")
        with patch.object(reader, "run_agent") as run, self.assertRaises(ApiError) as error:
            reader.assemble(self.ctx, self.params)
        self.assertEqual(error.exception.code, "snapshot_error")
        run.assert_not_called()

    def test_path_guard_denies_escape_absolute_and_nonstring_inputs(self):
        view = materialize(snapshot(self.ctx, self.params))
        hook = reader_hooks(view, set())["PreToolUse"][0].hooks[0]
        for tool, inputs in (
            ("Read", {"file_path": "../private/a.md"}),
            ("Read", {"file_path": str(self.root / "secret.md")}),
            ("Grep", {"path": ["../private"]}),
            ("Glob", {"pattern": "../../**"}),
            ("Glob", {"path": "..", "pattern": "*"}),
            ("Read", {"file_path": "bad\x00file"}),
        ):
            with self.subTest(tool=tool, inputs=inputs):
                result = async_to_sync(hook)({"tool_name": tool, "tool_input": inputs}, "id", None)
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        for tool in ("Read", "Grep", "Glob"):
            self.assertEqual(
                async_to_sync(hook)({"tool_name": tool, "tool_input": {"path": "."}}, "id", None),
                {},
            )

    def test_reader_sources_require_actual_read_and_do_not_include_contract(self):
        with patch.object(reader, "run_agent", side_effect=self.agent) as run:
            result = reader.assemble(self.ctx, self.params)
        self.assertEqual(result["entity_ids_used"], ["take-public"])
        self.assertEqual(result["sources"][0]["entity_id"], "take-public")
        self.assertNotIn("PRIVATE CONTRACT", run.call_args.kwargs["system"])

    def test_visible_but_unopened_citation_is_dropped_and_the_answer_kept(self):
        # F58: one visible note the reader never opened no longer discards the
        # whole answer; it is left out of the sources and named in a note.
        output = {"context_pack": "ANSWER", "entity_ids_used": ["take-public"], "gaps": []}
        with patch.object(reader, "run_agent", return_value=AgentResult(output=output)):
            result = reader.assemble(self.ctx, self.params)
        self.assertEqual(result["context_pack"], "ANSWER")
        self.assertEqual(result["entity_ids_used"], [])
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["unverified_entity_ids"], ["take-public"])
        self.assertIn("1 cited note was not opened", result["note"])

    def test_unverified_hidden_sources_reject_entire_pack(self):
        for identifier in ("missing", "take-private"):
            output = {"context_pack": "DO NOT RETURN", "entity_ids_used": [identifier], "gaps": []}
            with (
                patch.object(reader, "run_agent", return_value=AgentResult(output=output)),
                self.assertRaises(ApiError) as error,
            ):
                reader.assemble(self.ctx, self.params)
            self.assertEqual(error.exception.code, "unverified_sources")
            self.assertNotIn(identifier, error.exception.message)

    def test_revoked_grant_while_reader_runs_discards_pack(self):
        def run(*args, **kwargs):
            result = self.agent(*args, **kwargs)
            self.grant.delete()
            return result

        with (
            patch.object(reader, "run_agent", side_effect=run),
            self.assertRaises(ApiError) as error,
        ):
            reader.assemble(self.ctx, self.params)
        self.assertEqual(error.exception.code, "not_found")

    def test_disabled_token_while_reader_runs_discards_pack(self):
        def run(*args, **kwargs):
            result = self.agent(*args, **kwargs)
            self.token.is_active = False
            self.token.save(update_fields=["is_active"])
            return result

        with (
            patch.object(reader, "run_agent", side_effect=run),
            self.assertRaises(ApiError) as error,
        ):
            reader.assemble(self.ctx, self.params)
        self.assertEqual(error.exception.code, "not_found")

    def test_changed_head_while_reader_runs_discards_pack(self):
        def run(*args, **kwargs):
            result = self.agent(*args, **kwargs)
            BrainService.edit(
                self.ws,
                self.brain.pk,
                "knowledge/takes/public.md",
                note("take-public", "private"),
                expected_head=self.brain.head_revision_id,
                message="Tighten visibility",
            )
            return result

        with (
            patch.object(reader, "run_agent", side_effect=run),
            self.assertRaises(ApiError) as error,
        ):
            reader.assemble(self.ctx, self.params)
        self.assertEqual(error.exception.code, "brain_changed")

    def test_lens_hidden_and_missing_are_identical(self):
        with patch.object(reader, "run_agent") as run:
            shapes = []
            for lens in ("default", "missing"):
                with self.assertRaises(ApiError) as error:
                    reader.assemble(self.ctx, {**self.params, "lens": lens})
                self.assertEqual(error.exception.code, "invalid_input")
                # U68: the message names only the lenses visible at this tier.
                shapes.append(error.exception.message.replace(f"'{lens}'", "'x'"))
            self.assertEqual(shapes[0], shapes[1])
            self.assertNotIn("default", shapes[0])
            run.assert_not_called()

    def test_call_budget_bounds_sdk_and_prevents_expired_launch(self):
        with patch.object(reader, "run_agent", side_effect=self.agent) as run:
            reader.assemble(replace(self.ctx, deadline=time.monotonic() + 7), self.params)
            self.assertLessEqual(run.call_args.kwargs["timeout"], 7)
            with self.assertRaises(ApiError) as error:
                reader.assemble(replace(self.ctx, deadline=time.monotonic() - 1), self.params)
            self.assertEqual(error.exception.code, "timeout")
            self.assertEqual(run.call_count, 1)

    def test_foreign_workspace_unknown_before_model(self):
        with patch.object(reader, "run_agent") as run, self.assertRaises(ApiError) as error:
            reader.assemble(replace(self.ctx, workspace=self.other), self.params)
        self.assertEqual(error.exception.code, "not_found")
        run.assert_not_called()

    def test_console_queues_and_poll_is_user_scoped(self):
        self.client.force_login(self.user)
        with patch("django_q.tasks.async_task") as queued, patch.object(reader, "run_agent") as run:
            response = self.client.post(self.url("reader"), {"task": "Find context"}, follow=True)
        self.assertEqual(response.status_code, 200)
        job = BrainReaderJob.objects.get(brain=self.brain)
        queued.assert_called_once()
        run.assert_not_called()
        self.assertEqual(
            self.client.get(self.url("reader_status", job.pk)).json()["status"], "queued"
        )
        stranger = User.objects.create_user(username="colleague", email="colleague@example.test")
        WorkspaceMembership.objects.create(workspace=self.ws, user=stranger, role="member")
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(self.url("reader_status", job.pk)).status_code, 404)

    def test_console_brain_links_keep_nondefault_workspace(self):
        import re

        self.client.force_login(self.user)
        job = BrainReaderJob.objects.create(brain=self.brain, user=self.user, task="test")
        page = self.client.get(self.url("reader") + f"?job={job.pk}").content.decode()
        links = re.findall(r'(?:href|action|data-poll|data-note)="([^"]*/brains/[^"]*)"', page)
        self.assertGreater(len(links), 5)
        self.assertTrue(
            all(link.startswith(f"/cpanel/w/{self.ws.pk}/brains/") for link in links), links
        )

    def test_job_claim_is_idempotent_and_membership_is_rechecked(self):
        with patch("django_q.tasks.async_task"):
            job = jobs.submit(self.ws, self.brain, self.user, "Find context")
        with patch.object(reader, "run_agent", side_effect=self.agent) as run:
            jobs.run(self.ws.pk, job.pk)
            jobs.run(self.ws.pk, job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, "complete")
        self.assertEqual(run.call_count, 1)
        with patch("django_q.tasks.async_task"):
            revoked = jobs.submit(self.ws, self.brain, self.user, "Find context")
        WorkspaceMembership.objects.filter(user=self.user, workspace=self.ws).delete()
        with patch.object(reader, "run_agent") as run:
            jobs.run(self.ws.pk, revoked.pk)
        revoked.refresh_from_db()
        self.assertEqual(revoked.status, "failed")
        run.assert_not_called()

    def test_interrupted_jobs_expire_without_restarting_paid_work(self):
        from datetime import timedelta
        from django.utils import timezone

        job = BrainReaderJob.objects.create(
            brain=self.brain, user=self.user, task="test", status="running"
        )
        BrainReaderJob.objects.filter(pk=job.pk).update(
            updated_at=timezone.now() - timedelta(minutes=11)
        )
        with patch.object(reader, "run_agent") as run:
            jobs.reconcile()
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        run.assert_not_called()

    def test_poll_drops_result_when_revision_changes(self):
        job = BrainReaderJob.objects.create(
            brain=self.brain,
            user=self.user,
            task="test",
            status="complete",
            revision=self.brain.head_revision,
            result={"context_pack": "OLD SECRET"},
        )
        BrainService.edit(
            self.ws,
            self.brain.pk,
            "knowledge/takes/public.md",
            note("take-public", "private"),
            expected_head=self.brain.head_revision_id,
            message="Change",
        )
        self.client.force_login(self.user)
        response = self.client.get(self.url("reader_status", job.pk))
        self.assertEqual(response.json()["status"], "failed")
        self.assertNotIn("OLD SECRET", response.content.decode())

    def test_rest_and_mcp_use_reader_handler(self):
        raw = APIToken.generate_token(scope="api")
        self.token.token = raw
        self.token.save()
        headers = {"HTTP_AUTHORIZATION": "Bearer " + raw}
        with patch.object(reader, "run_agent", side_effect=self.agent):
            rest = self.client.post(
                "/api/v1/brain.assemble-context",
                self.params,
                content_type="application/json",
                **headers,
            )
            mcp = self.client.post(
                "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "brain-assemble-context", "arguments": self.params},
                },
                content_type="application/json",
                HTTP_ACCEPT="application/json, text/event-stream",
                HTTP_MCP_PROTOCOL_VERSION="2025-11-25",
                **headers,
            )
        self.assertEqual(rest.status_code, 200)
        self.assertEqual(
            json.loads(mcp.json()["result"]["content"][0]["text"]), rest.json()["data"]
        )


class BrainRecallTests(ReaderFixture, TestCase):
    def test_optional_disabled_and_cross_workspace_binding(self):
        with self.assertRaises(ApiError) as error:
            recall.search(self.ctx, {"brain": "mind", "query": "tools"})
        self.assertEqual(error.exception.code, "search_not_configured")
        collection = self.vectors()
        collection.workspace = self.other
        collection.save()
        self.brain.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.brain.full_clean()
        with self.assertRaises(ApiError) as error:
            recall.search(self.ctx, {"brain": "mind", "query": "tools"})
        self.assertEqual(error.exception.code, "search_not_configured")

    def test_index_three_tiers_and_filter_search_from_canonical_text(self):
        collection = self.vectors()
        recall.reindex(self.ws.pk, self.brain.pk)
        handle = VectorService.handle(collection)
        self.assertGreater(handle.count({"tier": "private"}), handle.count({"tier": "public"}))
        result = recall.search(self.ctx, {"brain": "mind", "query": "tools"})
        self.assertTrue(result["index_current"])
        self.assertEqual([hit["entity_id"] for hit in result["hits"]], ["take-public"])
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_stale_retiered_deleted_chunks_never_return(self):
        self.vectors()
        recall.reindex(self.ws.pk, self.brain.pk)
        BrainService.edit(
            self.ws,
            self.brain.pk,
            "knowledge/takes/public.md",
            note("take-public", "private"),
            expected_head=self.brain.head_revision_id,
            message="Hide",
        )
        self.assertEqual(recall.search(self.ctx, {"brain": "mind", "query": "tools"})["hits"], [])

    def test_reindex_embeds_only_changed_chunks_and_removes_old_ones(self):
        collection = self.vectors()
        recall.reindex(self.ws.pk, self.brain.pk)
        calls = self.embedding.call_count
        recall.reindex(self.ws.pk, self.brain.pk)
        self.assertEqual(self.embedding.call_count, calls)
        old_count = VectorService.handle(collection).count()
        BrainService.edit(
            self.ws,
            self.brain.pk,
            "knowledge/takes/public.md",
            "",
            delete=True,
            expected_head=self.brain.head_revision_id,
            message="Remove",
        )
        recall.reindex(self.ws.pk, self.brain.pk)
        self.assertEqual(self.embedding.call_count, calls)
        self.assertEqual(VectorService.handle(collection).count(), old_count - 3)

    def test_commit_queues_index_only_when_enabled_after_commit(self):
        self.vectors()
        with self.captureOnCommitCallbacks(execute=True), patch.object(recall, "enqueue") as queue:
            BrainService.edit(
                self.ws,
                self.brain.pk,
                "knowledge/takes/public.md",
                note("take-public", "public") + " update",
                expected_head=self.brain.head_revision_id,
                message="Update",
            )
        queue.assert_called_once_with(self.ws.pk, self.brain.pk)

    def test_hybrid_reader_preseeds_candidates_but_still_opens_notes(self):
        self.vectors()
        recall.reindex(self.ws.pk, self.brain.pk)
        with patch.object(reader, "run_agent", side_effect=self.agent) as run:
            result = registry.call("brain.assemble-context", self.ctx, self.params)
        self.assertEqual(result["semantic_candidates"], 1)
        self.assertIn("knowledge/takes/public.md", run.call_args.kwargs["prompt"])
        self.assertNotIn("SECRET", run.call_args.kwargs["prompt"])

    def test_binding_backup_relinks_by_workspace_name_without_paid_reindex(self):
        collection = self.vectors()
        rows = [row for row in export_brains() if row["id"] == str(self.brain.pk)]
        self.assertEqual(rows[0]["vector_collection_name"], collection.name)
        self.assertNotIn("reader_jobs", rows[0])
        self.brain.delete()
        with patch.object(recall, "enqueue") as queue:
            import_brains(
                rows,
                workspace_for=lambda _: self.ws,
                user_map={},
                script_map={},
                current_user=self.user,
            )
        from core.models import Brain

        restored = Brain.objects.for_workspace(self.ws).get(slug="mind")
        self.assertEqual(restored.vector_collection_id, collection.pk)
        self.assertIsNone(restored.vector_revision_id)
        queue.assert_not_called()


class BrainBudgetTests(SimpleTestCase):
    def test_repeated_chunks_have_distinct_immutable_payload_ids(self):
        from types import SimpleNamespace

        entity = SimpleNamespace(
            status="current", entity_id="take", kind="take", topics=[], path="note.md"
        )
        view = SimpleNamespace(
            brain=SimpleNamespace(pk="brain"),
            tier="public",
            entities=[entity],
            files={"note.md": "text"},
        )
        with patch.object(recall, "chunks", return_value=["same", "same"]):
            rows = list(recall.points(view))
        self.assertEqual(len({row["id"] for row in rows}), 2)

    def test_glob_rejects_windows_rooted_and_drive_relative_patterns(self):
        with tempfile.TemporaryDirectory() as directory:
            hook = reader_hooks(Path(directory), set())["PreToolUse"][0].hooks[0]
            for pattern in (r"\outside\*.md", r"C:outside\*.md"):
                result = async_to_sync(hook)(
                    {"tool_name": "Glob", "tool_input": {"path": ".", "pattern": pattern}},
                    "id",
                    None,
                )
                self.assertEqual(
                    result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
                )

    def test_stream_reads_shrink_socket_timeout_and_expire(self):
        import requests
        from core.vectors.budget import BudgetSession

        response = Mock(status_code=200)
        response.raw.read1.side_effect = [b"one", b"two", b""]
        with (
            patch("requests.Session.request", return_value=response),
            patch("core.vectors.budget.time.monotonic", side_effect=[0, 0, 9, 11]),
        ):
            with self.assertRaises(requests.Timeout):
                BudgetSession(10).post("https://example.com")
        self.assertEqual(
            [call.args[0] for call in response.raw._fp.fp.raw._sock.settimeout.call_args_list],
            [10, 1],
        )
        response.close.assert_called_once()

    def test_bounded_embedding_has_no_retry_sleep_after_timeout(self):
        import requests
        from core.vectors.embeddings import EmbeddingClient, EmbeddingError

        provider = Mock(endpoint_url="https://example.com", model="model", max_batch=1, preset={})
        provider.get_credential.return_value = ""
        session = Mock(bounded=True)
        session.post.side_effect = requests.Timeout("budget")
        with (
            patch("core.vectors.embeddings.time.sleep") as sleep,
            self.assertRaises(EmbeddingError),
        ):
            EmbeddingClient(provider, session=session).embed(["query"])
        session.post.assert_called_once()
        sleep.assert_not_called()
