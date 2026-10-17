"""
M4 polish batch (2.0 release test) — Brain settings, Ask brain, inbox/feed.

U64   settings: re-index toast + index status line, today's spend next to the cap, money
      format; Ask brain refuses a cap-blocked query before queueing a job
U66   an invalid proposal still shows what it would write (blocked preview); a refused JSON
      edit keeps the panel open; URL refusals name the guard's reason; the inbox shows
      feeds that need attention
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from core.api.exceptions import ApiError
from core.brain import approval, intake
from core.brain.service import BrainService
from core.models import (
    BrainFeed, BrainReaderJob, ClaudeUsage, Environment, GlobalSettings, User, Workspace,
    WorkspaceMembership,
)
from core.test_brain_store import contract_files
from core.test_brain_validator import note_file
from core.test_plugin_api import _mock_setup


class BrainBase(TestCase):
    def setUp(self):
        _mock_setup(self)
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        self.ws = Workspace.get_default()
        self.owner = User.objects.create_user(
            username="owner@example.com", email="owner@example.com", password="x-Long-pass-91",
        )
        WorkspaceMembership.objects.update_or_create(
            user=self.owner, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )
        self.client.force_login(self.owner)
        files = contract_files()
        files["CLAUDE.md"] += "\n## Topic taxonomy\n`ai`\n"
        self.brain = BrainService.create(self.ws, "w16-brain", files=files)

    def url(self, name, *args):
        return reverse(f"cpanel_ws:brain_{name}", args=[self.ws.pk, self.brain.pk, *args])


class SettingsAndCapTests(BrainBase):
    def test_settings_show_spend_cap_money_and_index_line(self):
        self.brain.daily_cost_cap_usd = Decimal("0.5000")
        self.brain.save(update_fields=["daily_cost_cap_usd"])
        ClaudeUsage.objects.create(source="brain", brain_id=self.brain.pk, cost_usd=0.0123)
        resp = self.client.get(self.url("settings"))
        html = resp.content.decode()
        self.assertEqual(resp.context["form"].initial["daily_cost_cap_usd"], Decimal("0.50"))
        self.assertIn('AI spend today: <span class="font-mono text-text">$0.0123</span>', html)
        self.assertIn("$0.50</span> daily cap", html)
        self.assertIn("Search index: off", html)

    def test_ask_is_refused_before_queueing_when_the_cap_is_reached(self):
        self.brain.daily_cost_cap_usd = Decimal("0")
        self.brain.save(update_fields=["daily_cost_cap_usd"])
        with patch("django_q.tasks.async_task") as queued:
            resp = self.client.post(self.url("reader"), {"task": "what do I believe?", "lens": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(BrainReaderJob.objects.filter(brain=self.brain).exists())
        queued.assert_not_called()
        self.assertIn("daily AI cap is reached", resp.content.decode())


class FeedReviewTests(BrainBase):
    def _feed(self, files, status="proposed", **extra):
        return BrainFeed.objects.create(
            brain=self.brain, base_revision=self.brain.head_revision, source_id="thought-x",
            channel="console", principal=f"user:{self.owner.pk}",
            raw_payload={"source_kind": "thought", "content": "Source words", "title": "Blocked one"},
            proposal={"files": files}, status=status, **extra,
        )

    def test_invalid_proposal_shows_a_blocked_preview(self):
        feed = self._feed([note_file(source="")])
        review = approval.review(self.ws, self.brain.pk, feed.pk)
        self.assertFalse(review.validation.valid)
        self.assertTrue(review.blocked)
        self.assertEqual([c["filename"] for c in review.changes], ["knowledge/takes/take-example.md"])
        html = self.client.get(self.url("feed", feed.pk)).content.decode()
        self.assertIn("Preview only", html)
        self.assertIn("(blocked)", html)
        self.assertNotIn("No reviewable changes.", html)
        with self.assertRaises(ApiError):
            approval.approve(self.ws, self.brain.pk, feed.pk, expected_digest=approval.digest(feed), user=self.owner)

    def test_bad_json_edit_keeps_the_panel_open(self):
        feed = self._feed([note_file()])
        resp = self.client.post(
            self.url("feed", feed.pk),
            {"action": "edit", "proposal": "{not json", "expected_digest": approval.digest(feed)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('<details class="rounded-xl border border-line bg-panel p-4" open><summary class="text-sm font-medium cursor-pointer">Edit proposal JSON', resp.content.decode())

    def test_url_refusal_names_the_reason(self):
        with self.assertRaises(ApiError) as ctx:
            intake.safe_url("http://169.254.169.254/latest/meta-data/")
        self.assertIn("169.254.169.254", ctx.exception.message)
        self.assertNotEqual(ctx.exception.message, "Enter an allowed HTTP or HTTPS URL without credentials.")

    def test_inbox_flags_feeds_that_need_attention(self):
        self._feed([], status="failed", error="Could not fetch the URL: HTTP 404")
        html = self.client.get(self.url("inbox")).content.decode()
        self.assertIn("Needs attention: Could not fetch the URL: HTTP 404", html)
