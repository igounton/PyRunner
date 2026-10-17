"""
M4 polish batch (2.0 release test) — Channels and Py AI.

U50   Add channel asks for the @BotFather token; Telegram's bare "Unauthorized" is explained
      (and a network error never echoes the bot token from the request URL)
F46   the Channels list no longer says inbound chat "arrives in a later phase"
U53   Py AI talks about the current workspace; the model hint follows the active provider
U55   Py AI replies sent to a chat channel are plain text, not raw Markdown
"""

from unittest import mock

import requests
from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.urls import reverse

from core.forms import ChannelForm, PyAISettingsForm
from core.models import (
    AIProvider, Channel, Environment, GlobalSettings, User, Workspace, WorkspaceMembership,
)
from core.services import EncryptionService
from core.services.channels import ChannelError, InboundMessage, get_provider
from core.services.channels.handlers import _pyai_handler, markdown_to_plain
from core.services.claude_service import ClaudeService
from core.services.pyai import PyAIResult, PyAIService

_TEST_KEY = Fernet.generate_key().decode()
BAD_TOKEN = "Telegram rejected this bot token (Unauthorized) — copy it again from @BotFather."
TOKEN = "123456:SECRET-bot-token"


def _tg(status, body):
    resp = mock.Mock(status_code=status)
    resp.json.return_value = body
    return resp


@override_settings(ENCRYPTION_KEY=_TEST_KEY)
class ChannelBase(TestCase):
    def setUp(self):
        EncryptionService.reset()
        self.addCleanup(EncryptionService.reset)
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

    def _channel(self, **extra):
        ch = Channel(workspace=self.ws, provider="telegram", name="Ops", **extra)
        ch.set_credentials({"bot_token": TOKEN}, identity=TOKEN)
        ch.config = {"default_target": "999"}
        ch.save()
        return ch


class BotTokenTests(ChannelBase):
    def test_add_form_asks_for_the_token_and_edit_keeps_it(self):
        add = ChannelForm(workspace=self.ws).fields["bot_token"].widget.attrs["placeholder"]
        self.assertEqual(add, "Paste the token from @BotFather")
        edit = ChannelForm(instance=self._channel(), workspace=self.ws)
        self.assertEqual(edit.fields["bot_token"].widget.attrs["placeholder"], "Leave blank to keep current")
        html = self.client.get(reverse("cpanel:channel_create")).content.decode()
        self.assertIn("Paste the token from @BotFather", html)
        self.assertNotIn("Leave blank to keep current", html)

    def test_unauthorized_is_explained_in_test_and_find_chat_id(self):
        ch = self._channel()
        body = {"ok": False, "error_code": 401, "description": "Unauthorized"}
        with mock.patch("core.services.channels.telegram.requests.post", return_value=_tg(401, body)):
            ok, message = get_provider("telegram").test_connection(ch)
            with self.assertRaises(ChannelError) as ctx:
                get_provider("telegram").discover_chat_ids(ch)
            test = self.client.post(reverse("cpanel:channel_test", args=[ch.pk])).json()
        self.assertFalse(ok)
        self.assertEqual(message, BAD_TOKEN)
        self.assertEqual(str(ctx.exception), BAD_TOKEN)
        self.assertIn("@BotFather", test.get("error") or test.get("message", ""))

    def test_other_errors_keep_telegrams_description(self):
        body = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        with mock.patch("core.services.channels.telegram.requests.post", return_value=_tg(400, body)):
            ok, message = get_provider("telegram").test_connection(self._channel())
        self.assertEqual(message, "Bad Request: chat not found")

    def test_network_error_never_shows_the_token(self):
        err = requests.ConnectionError(
            f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
            f"with url: /bot{TOKEN}/getMe (Caused by NameResolutionError(\"Failed to resolve "
            f"'api.telegram.org' ([Errno -3] Temporary failure in name resolution)\"))"
        )
        with mock.patch("core.services.channels.telegram.requests.post", side_effect=err):
            ok, message = get_provider("telegram").test_connection(self._channel())
        self.assertFalse(ok)
        self.assertNotIn(TOKEN, message)
        self.assertTrue(message.startswith("Could not reach Telegram at api.telegram.org:443"))

    def test_list_copy_says_inbound_is_built(self):
        self._channel()
        html = self.client.get(reverse("cpanel:channel_list")).content.decode()
        self.assertNotIn("arrives in a later phase", html)
        self.assertIn("is set up on each channel", html)


class PyAIWordingTests(ChannelBase):
    def _system_for(self, **kwargs):
        s = GlobalSettings.get_settings()
        s.pyai_enabled = True
        s.claude_enabled = True
        s.save()
        drive = mock.AsyncMock(return_value=PyAIResult(text="ok"))
        with mock.patch.object(ClaudeService, "is_configured", return_value=True), \
                mock.patch.object(ClaudeService, "get_script_env", return_value={"X": "y"}), \
                mock.patch.object(PyAIService, "_drive", new=drive):
            PyAIService.respond("how many scripts?", workspace=self.ws, **kwargs)
        return drive.call_args.args[1]

    def test_system_prompt_is_about_the_workspace(self):
        system = self._system_for()
        self.assertIn(f"current workspace ('{self.ws.name}')", system)
        self.assertNotIn("THIS instance", system)
        self.assertNotIn("plain-text chat message", system)
        self.assertIn("plain-text chat message", self._system_for(plain_text=True))

    def test_page_header_and_model_hint(self):
        provider = AIProvider.objects.create(
            provider_type=AIProvider.ProviderType.OPENROUTER, name="OpenRouter",
            default_model="anthropic/claude-haiku-4.5",
        )
        s = GlobalSettings.get_settings()
        s.active_ai_provider = provider
        s.save()
        field = PyAISettingsForm(instance=s).fields["pyai_model"]
        self.assertEqual(field.widget.attrs["placeholder"], "anthropic/claude-haiku-4.5 (optional)")
        self.assertIn("OpenRouter's default model, anthropic/claude-haiku-4.5", field.help_text)
        html = self.client.get(reverse("cpanel:pyai")).content.decode()
        self.assertIn(f"Read-only assistant for the {self.ws.name} workspace", html)
        self.assertNotIn("claude-sonnet-4-6 (optional)", html)


class PyAIChannelPlainTextTests(ChannelBase):
    def test_handler_asks_for_plain_text_and_strips_markdown(self):
        ch = self._channel(inbound_handler="pyai")
        msg = InboundMessage(
            channel_id=str(ch.id), provider="telegram", text="what failed?",
            sender={"id": "7"}, reply_ref={"chat_id": 7}, raw={},
        )
        answer = (
            "## Failures\n"
            "**w14-notify-fail** failed most recently. Check `pyrunner_notify`:\n"
            "* exit code 1\n"
            "- see [the run](https://example.com/runs/1)"
        )
        with mock.patch.object(PyAIService, "is_available", return_value=True), \
                mock.patch.object(PyAIService, "respond", return_value=PyAIResult(text=answer)) as r:
            out = _pyai_handler(ch, msg)
        self.assertTrue(r.call_args.kwargs["plain_text"])
        self.assertEqual(
            out.text,
            "Failures\nw14-notify-fail failed most recently. Check pyrunner_notify:\n"
            "- exit code 1\n- see the run (https://example.com/runs/1)",
        )

    def test_plain_text_keeps_code_and_ordinary_text(self):
        self.assertEqual(markdown_to_plain("use `25 * 1024 * 1024` bytes"), "use 25 * 1024 * 1024 bytes")
        text = "my_script ran 2 * 3 times; __init__ stays; a*b stays"
        self.assertEqual(markdown_to_plain(text), text)
        self.assertEqual(markdown_to_plain("Run:\n```python\nprint('hi')\n```\nDone."), "Run:\nprint('hi')\nDone.")
