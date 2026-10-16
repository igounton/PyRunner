"""
Py AI (built-in read-only assistant) — tools, runtime, inbound handler, views.

The actual Claude call (``PyAIService._drive``) is always mocked — tests cover the
tool data layer (incl. workspace scoping), usage recording, config gating, the
chat views, and the ``pyai`` inbound handler. No test hits the Claude API.
"""

import json
from unittest import mock

from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import (
    Channel,
    ChannelMessage,
    ClaudeUsage,
    DataStore,
    DataStoreEntry,
    Environment,
    GlobalSettings,
    Run,
    Script,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.services.channels import InboundMessage
from core.services.channels.handlers import _channel_history, _pyai_handler
from core.services.claude_service import ClaudeService
from core.services.pyai import PyAIError, PyAIResult, PyAIService
from core.services.pyai import tools as pyai_tools

_TEST_KEY = Fernet.generate_key().decode()


def _wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


class PyAIToolsTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.other = Workspace.objects.create(name="Other")
        self.env = Environment.objects.create(name="e", path="p")
        self.s1 = Script.objects.create(name="alpha", code="x", environment=self.env, workspace=self.ws)
        self.s2 = Script.objects.create(name="beta", code="x", environment=self.env, workspace=self.other)
        Run.objects.create(script=self.s1, workspace=self.ws, status=Run.Status.SUCCESS)
        self.ds = DataStore.objects.create(name="cfg", workspace=self.ws)
        e = DataStoreEntry(datastore=self.ds, key="token")
        e.set_value({"n": 1})
        e.save()

    def test_count_scoped(self):
        self.assertEqual(pyai_tools._count_scripts(self.ws)["count"], 1)
        self.assertEqual(pyai_tools._count_scripts(self.other)["count"], 1)

    def test_list_scripts_scoped(self):
        names = [s["name"] for s in pyai_tools._list_scripts(self.ws)["scripts"]]
        self.assertIn("alpha", names)
        self.assertNotIn("beta", names)

    def test_get_script_found_and_missing(self):
        self.assertTrue(pyai_tools._get_script(self.ws, "alpha")["found"])
        self.assertTrue(pyai_tools._get_script(self.ws, "alph")["found"])  # icontains
        self.assertFalse(pyai_tools._get_script(self.ws, "beta")["found"])  # other workspace

    def test_recent_runs_scoped(self):
        runs = pyai_tools._recent_runs(self.ws, 10)["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["script"], "alpha")

    def test_datastore_tools(self):
        stores = pyai_tools._list_datastores(self.ws)["datastores"]
        self.assertEqual(stores, [{"name": "cfg", "entry_count": 1}])
        full = pyai_tools._query_datastore(self.ws, "cfg", None)
        self.assertEqual(full["entries"], {"token": {"n": 1}})
        one = pyai_tools._query_datastore(self.ws, "cfg", "token")
        self.assertEqual(one["value"], {"n": 1})
        self.assertFalse(pyai_tools._query_datastore(self.ws, "nope", None)["found"])

    def test_list_schedules_shape(self):
        self.assertEqual(pyai_tools._list_schedules(self.ws), {"schedules": []})

    def test_build_tools_and_allowed(self):
        tools = pyai_tools.build_tools(self.ws)
        self.assertEqual(len(tools), 7)
        self.assertEqual(len(pyai_tools.ALLOWED_TOOLS), 7)
        self.assertTrue(all(n.startswith("mcp__pyai__") for n in pyai_tools.ALLOWED_TOOLS))


class PyAIRuntimeTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.s = GlobalSettings.get_settings()
        self.s.pyai_enabled = True
        self.s.claude_enabled = True
        self.s.save()

    def test_respond_records_usage_and_returns_text(self):
        canned = PyAIResult(text="You have 3 scripts.", model="claude-x", input_tokens=10, output_tokens=4)
        with mock.patch.object(ClaudeService, "is_configured", return_value=True), \
                mock.patch.object(ClaudeService, "get_script_env", return_value={"ANTHROPIC_API_KEY": "x"}), \
                mock.patch.object(PyAIService, "_drive", new=mock.AsyncMock(return_value=canned)):
            result = PyAIService.respond("how many scripts?", workspace=self.ws)
        self.assertEqual(result.text, "You have 3 scripts.")
        self.assertEqual(ClaudeUsage.objects.filter(source=ClaudeUsage.Source.PYAI).count(), 1)

    def test_provider_http_error_is_raised_not_returned_as_answer(self):
        """F49: the CLI puts "API Error: 400 …" in the result text; that must be an error."""
        canned = PyAIResult(text="API Error: 400 anthropic/x is not a valid model ID",
                            is_error=True, error="API error (HTTP 400)", api_error_status=400)
        with mock.patch.object(ClaudeService, "is_configured", return_value=True), \
                mock.patch.object(ClaudeService, "get_script_env", return_value={"ANTHROPIC_API_KEY": "x"}), \
                mock.patch.object(PyAIService, "_drive", new=mock.AsyncMock(return_value=canned)):
            with self.assertRaises(PyAIError) as cm:
                PyAIService.respond("hi", workspace=self.ws)
        self.assertIn("HTTP 400", str(cm.exception))
        self.assertIn("not a valid model ID", str(cm.exception))

    def test_error_without_provider_status_keeps_partial_text(self):
        canned = PyAIResult(text="Partial answer before the turn limit.", is_error=True, error="error_max_turns")
        with mock.patch.object(ClaudeService, "is_configured", return_value=True), \
                mock.patch.object(ClaudeService, "get_script_env", return_value={"ANTHROPIC_API_KEY": "x"}), \
                mock.patch.object(PyAIService, "_drive", new=mock.AsyncMock(return_value=canned)):
            result = PyAIService.respond("hi", workspace=self.ws)
        self.assertEqual(result.text, "Partial answer before the turn limit.")

    def test_respond_raises_when_disabled(self):
        self.s.pyai_enabled = False
        self.s.save()
        with self.assertRaises(PyAIError):
            PyAIService.respond("hi", workspace=self.ws)

    def test_respond_raises_when_claude_unconfigured(self):
        with mock.patch.object(ClaudeService, "is_configured", return_value=False):
            with self.assertRaises(PyAIError):
                PyAIService.respond("hi", workspace=self.ws)

    def test_is_available(self):
        with mock.patch.object(ClaudeService, "is_configured", return_value=True), \
                mock.patch.object(ClaudeService, "cli_available", return_value=True):
            self.assertTrue(PyAIService.is_available())
        with mock.patch.object(ClaudeService, "is_configured", return_value=False):
            self.assertFalse(PyAIService.is_available())


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class PyAIHandlerTests(TestCase):
    def setUp(self):
        from core.services import EncryptionService

        EncryptionService.reset()
        self.addCleanup(EncryptionService.reset)
        self.ws = Workspace.get_default()
        self.ch = Channel(workspace=self.ws, provider="telegram", name="Bot", inbound_handler="pyai")
        self.ch.set_credentials({"bot_token": "1:2"}, identity="1:2")
        self.ch.save()
        self.msg = InboundMessage(
            channel_id=str(self.ch.id), provider="telegram", text="how many scripts?",
            sender={"id": "7"}, reply_ref={"chat_id": 7}, raw={},
        )

    def test_handler_replies_with_answer(self):
        canned = PyAIResult(text="You have 2 scripts.")
        with mock.patch.object(PyAIService, "is_available", return_value=True), \
                mock.patch.object(PyAIService, "respond", return_value=canned) as r:
            out = _pyai_handler(self.ch, self.msg)
        self.assertEqual(out.text, "You have 2 scripts.")
        self.assertEqual(out.reply_ref, {"chat_id": 7})
        self.assertEqual(r.call_args.kwargs["workspace"], self.ws)

    def test_handler_when_unavailable(self):
        with mock.patch.object(PyAIService, "is_available", return_value=False):
            out = _pyai_handler(self.ch, self.msg)
        self.assertIn("not available", out.text)

    def test_channel_history_excludes_current_and_maps_roles(self):
        ChannelMessage.objects.create(channel=self.ch, direction="in", text="hi", reply_ref_json={"chat_id": 7})
        ChannelMessage.objects.create(channel=self.ch, direction="out", text="hello!", reply_ref_json={"chat_id": 7})
        # the current inbound, already logged by dispatch:
        ChannelMessage.objects.create(channel=self.ch, direction="in", text="how many scripts?", reply_ref_json={"chat_id": 7})
        hist = _channel_history(self.ch, self.msg)
        texts = [h["text"] for h in hist]
        self.assertEqual(texts, ["hi", "hello!"])  # current message excluded
        self.assertEqual(hist[0]["role"], "user")
        self.assertEqual(hist[1]["role"], "assistant")


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class PyAIViewTests(TestCase):
    def setUp(self):
        _wizard_off(self)
        self.ws = Workspace.get_default()
        self.admin = User.objects.create(email="a@example.com", is_superuser=True, is_staff=True)
        WorkspaceMembership.ensure(self.admin, self.ws, role=WorkspaceMembership.ROLE_OWNER)

    def _login(self, user):
        self.client.force_login(user)

    def test_view_renders_for_superuser(self):
        self._login(self.admin)
        resp = self.client.get(reverse("cpanel:pyai"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Py AI", resp.content.decode())

    def test_access_gate_blocks_plain_member(self):
        member = User.objects.create(email="m@example.com")
        WorkspaceMembership.objects.filter(user=member).delete()
        WorkspaceMembership.objects.create(user=member, workspace=self.ws, role=WorkspaceMembership.ROLE_MEMBER)
        self._login(member)
        resp = self.client.get(reverse("cpanel:pyai"))
        self.assertEqual(resp.status_code, 302)  # redirected to dashboard

    def test_send_blocked_when_unavailable(self):
        self._login(self.admin)
        with mock.patch.object(PyAIService, "is_available", return_value=False):
            resp = self.client.post(
                reverse("cpanel:pyai_send"),
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 400)

    def test_send_returns_answer_and_stores_history(self):
        self._login(self.admin)
        canned = PyAIResult(text="2 scripts.", tools_used=["mcp__pyai__count_scripts"])
        with mock.patch.object(PyAIService, "is_available", return_value=True), \
                mock.patch.object(PyAIService, "respond", return_value=canned):
            resp = self.client.post(
                reverse("cpanel:pyai_send"),
                data=json.dumps({"message": "how many?"}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["text"], "2 scripts.")
        self.assertEqual(self.client.session["pyai_history"][-1]["text"], "2 scripts.")

    def test_send_provider_error_is_502_and_not_stored(self):
        self._login(self.admin)
        canned = PyAIResult(text="API Error: 400 bad model", is_error=True, api_error_status=400)
        with mock.patch.object(PyAIService, "is_available", return_value=True), \
                mock.patch.object(ClaudeService, "is_configured", return_value=True), \
                mock.patch.object(ClaudeService, "get_script_env", return_value={"ANTHROPIC_API_KEY": "x"}), \
                mock.patch.object(PyAIService, "_drive", new=mock.AsyncMock(return_value=canned)):
            s = GlobalSettings.get_settings()
            s.pyai_enabled = True
            s.claude_enabled = True
            s.save()
            resp = self.client.post(
                reverse("cpanel:pyai_send"),
                data=json.dumps({"message": "hi"}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 502)
        self.assertIn("HTTP 400", resp.json()["error"])
        self.assertNotIn("pyai_history", self.client.session)

    def test_settings_enable_blocked_without_claude(self):
        self._login(self.admin)
        with mock.patch.object(ClaudeService, "is_configured", return_value=False):
            self.client.post(reverse("cpanel:pyai_settings"), {"pyai_enabled": "on"})
        self.assertFalse(GlobalSettings.get_settings().pyai_enabled)

    def test_settings_save_model(self):
        self._login(self.admin)
        self.client.post(reverse("cpanel:pyai_settings"), {"pyai_model": "claude-x"})
        self.assertEqual(GlobalSettings.get_settings().pyai_model, "claude-x")

    def test_settings_blocked_for_non_superuser(self):
        member = User.objects.create(email="m2@example.com")
        WorkspaceMembership.ensure(member, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self._login(member)
        resp = self.client.post(reverse("cpanel:pyai_settings"), {"pyai_model": "x"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(GlobalSettings.get_settings().pyai_model, "")


class ChannelInboundFormPyAITests(TestCase):
    def test_pyai_choice_appears_when_enabled(self):
        from core.forms import ChannelInboundForm

        ws = Workspace.get_default()
        form = ChannelInboundForm(workspace=ws)
        self.assertNotIn("pyai", dict(form.fields["inbound_handler"].choices))

        s = GlobalSettings.get_settings()
        s.pyai_enabled = True
        s.save()
        form = ChannelInboundForm(workspace=ws)
        self.assertIn("pyai", dict(form.fields["inbound_handler"].choices))
