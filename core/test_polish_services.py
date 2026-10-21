"""
M4 polish batch (2.0 release test) — Services, Vectors, AI usage.

U37   secret providers: one-line network errors, raw text under "Details"
U45   new collection preselects the tested embedding provider; Qdrant samples sorted by id
U46   vector store Test / outage: one-line cause, "Store unreachable" badge, "retry in N s",
      no "first 0 point(s)" line
U47   object storage: Test result under the clicked button, refused save keeps the form,
      bucket cell truncated, HeadBucket 404 named
U48   Services card headers stack on phones; new-provider key fields ask for the key
U49   AI usage: every day on the chart, one "Total" definition per column, no dead links
U59   AI provider Test renders the model's Markdown
U60   a passing row Test updates its Last tested cell
"""

import time
import uuid
from datetime import timedelta
from unittest import mock

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.forms import (
    AIProviderForm, EmbeddingProviderForm, StorageConnectionForm, VectorCollectionForm,
    VectorConnectionForm,
)
from core.models import (
    ClaudeUsage, EmbeddingProvider, Environment, GlobalSettings, Script, SecretProvider, StorageConnection,
    User, VectorCollection, VectorConnection, Workspace, WorkspaceMembership,
)
from core.net_errors import describe_request_error
from core.services import VectorError
from core.services.s3_service import S3Service
from core.services.secret_backends import get_backend
from core.services.secret_backends.base import SecretResolutionError
from core.vectors.errors import BackendUnavailable
from core.vectors.qdrant_store import QdrantStore
from core.vectors.service import VectorService

PW = "correct-horse-battery-staple-91"

NAME_ERR = (
    "HTTPConnectionPool(host='vault-x', port=8200): Max retries exceeded with url: "
    "/v1/auth/token/lookup-self (Caused by NameResolutionError(\"<urllib3.connection."
    "HTTPConnection object at 0x7f>: Failed to resolve 'vault-x' ([Errno -2] Name or "
    "service not known)\"))"
)
REFUSED_ERR = (
    "HTTPConnectionPool(host='10.0.0.5', port=8200): Max retries exceeded with url: / "
    "(Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x7f>: "
    "Failed to establish a new connection: [Errno 111] Connection refused'))"
)
TIMEOUT_ERR = (
    "HTTPConnectionPool(host='10.255.255.1', port=6333): Max retries exceeded with url: "
    "/collections (Caused by ConnectTimeoutError(<urllib3.connection.HTTPConnection object "
    "at 0x7334b79e28b0>, 'Connection to 10.255.255.1 timed out. (connect timeout=3)'))"
)


class NetworkErrorTextTests(SimpleTestCase):
    def test_name_resolution_is_one_line_with_details(self):
        text = describe_request_error(requests.ConnectionError(NAME_ERR), "Vault", 10)
        first, details = text.split("\n", 1)
        self.assertEqual(first, "Could not reach Vault at vault-x:8200 — the host name doesn't resolve.")
        self.assertTrue(details.startswith("Details: HTTPConnectionPool"))

    def test_refused_and_timeout(self):
        refused = describe_request_error(requests.ConnectionError(REFUSED_ERR), "Vault", 10)
        self.assertIn("at 10.0.0.5:8200 — connection refused", refused)
        timed = describe_request_error(
            requests.exceptions.ConnectTimeout(TIMEOUT_ERR), "the Qdrant server", (3, 10)
        )
        self.assertIn("at 10.255.255.1:6333 — timed out after 3 s.", timed)

    def test_unknown_cause_keeps_the_raw_text(self):
        text = describe_request_error(requests.RequestException("odd failure"), "Doppler")
        self.assertEqual(text, "Could not reach Doppler: odd failure")


class SecretProviderErrorTests(TestCase):
    def _vault(self):
        p = SecretProvider(provider_type="vault", name="v", config={"base_url": "http://vault-x:8200"})
        p.set_credentials({"token": "hvs.test"})
        return p

    def test_vault_test_names_the_cause(self):
        with mock.patch("requests.get", side_effect=requests.ConnectionError(NAME_ERR)):
            ok, message = get_backend("vault").test_connection(self._vault())
        self.assertFalse(ok)
        self.assertTrue(message.startswith("Could not reach Vault at vault-x:8200 — the host name"))
        self.assertNotIn("HTTPConnectionPool", message.split("\n")[0])

    def test_vault_fetch_error_in_run_stderr_is_readable(self):
        with mock.patch("requests.get", side_effect=requests.ConnectionError(REFUSED_ERR)):
            with self.assertRaises(SecretResolutionError) as ctx:
                get_backend("vault").fetch(self._vault(), "app#KEY")
        self.assertIn("connection refused", str(ctx.exception).split("\n")[0])


class QdrantOutageTests(TestCase):
    def test_retry_in_says_the_seconds_left(self):
        store = QdrantStore.__new__(QdrantStore)
        store._outage_key = "qdrant_outage_test_" + uuid.uuid4().hex
        cache.set(store._outage_key, time.time() + 12, timeout=30)
        try:
            with self.assertRaises(BackendUnavailable) as ctx:
                store._check_outage()
        finally:
            cache.delete(store._outage_key)
        self.assertRegex(str(ctx.exception), r"retry in 1[12] s")
        self.assertNotIn("30 seconds", str(ctx.exception))

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
    def test_vector_store_test_names_the_timeout(self):
        conn = VectorConnection(provider_type="qdrant", name="q", config={"url": "http://10.255.255.1:6333"})
        conn.set_credentials({})
        with mock.patch("requests.get", side_effect=requests.exceptions.ConnectTimeout(TIMEOUT_ERR)):
            ok, message = VectorService.test_connection(conn)
        self.assertFalse(ok)
        self.assertEqual(
            message.split("\n")[0],
            "Could not reach the Qdrant server at 10.255.255.1:6333 — timed out after 3 s.",
        )


class ServicesBase(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        self.ws = Workspace.get_default()
        self.env, _ = Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        self.admin = User.objects.create_user(
            username="admin@example.com", email="admin@example.com", password=PW,
            is_superuser=True, is_staff=True,
        )
        WorkspaceMembership.objects.update_or_create(
            user=self.admin, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )
        self.client.force_login(self.admin)


class VectorPageTests(ServicesBase):
    def _provider(self, name, tested_minutes_ago=None):
        p = EmbeddingProvider(
            provider_type=EmbeddingProvider.ProviderType.OPENAI, name=name,
            base_url="https://api.openai.com", model="text-embedding-3-small", max_batch=4,
            dimension=8 if tested_minutes_ago is not None else None,
        )
        if tested_minutes_ago is not None:
            p.last_tested_at = timezone.now() - timedelta(minutes=tested_minutes_ago)
        p.set_credential("sk-test")
        p.save()
        return p

    def test_new_collection_preselects_the_latest_tested_provider(self):
        self._provider("never-tested")
        self._provider("old", tested_minutes_ago=90)
        recent = self._provider("recent", tested_minutes_ago=5)
        form = VectorCollectionForm(workspace=self.ws)
        self.assertEqual(form.fields["embedding_provider"].initial, recent.pk)

    def _qdrant_collection(self):
        conn = VectorConnection.objects.create(
            provider_type="qdrant", name="Q", config={"url": "http://q:6333"}
        )
        return VectorCollection.objects.create(
            workspace=self.ws, name="qcol", connection=conn, dimension=3, metric="cosine",
            backend_ref="pyrun_qcol", points_count=8,
        )

    def _detail(self, col, handle):
        with mock.patch("core.views.vectors.VectorService.handle", return_value=handle):
            return self.client.get(reverse("cpanel:vector_detail", args=[col.pk]))

    def test_outage_shows_store_unreachable_not_ready(self):
        col = self._qdrant_collection()
        handle = mock.Mock()
        handle.scroll.side_effect = VectorError("Qdrant is temporarily unavailable; retry in 9 s")
        html = self._detail(col, handle).content.decode()
        self.assertIn("Store unreachable", html)
        self.assertIn("(last known)", html)
        self.assertNotIn(">Ready</span>", html)
        self.assertNotIn("point(s) by id", html)

    def test_qdrant_sample_is_sorted_by_user_id(self):
        col = self._qdrant_collection()
        handle = mock.Mock()
        handle.scroll.return_value = (
            [{"id": i, "text": "", "payload": {}} for i in ("p8", "p2", "p5")], None
        )
        html = self._detail(col, handle).content.decode()
        self.assertLess(html.index(">p2<"), html.index(">p5<"))
        self.assertLess(html.index(">p5<"), html.index(">p8<"))
        self.assertIn("3 points from the server, sorted by id.", html)


class StorageCardTests(ServicesBase):
    def test_head_bucket_404_names_the_bucket(self):
        message = S3Service._map_boto3_error(
            "An error occurred (404) when calling the HeadBucket operation: Not Found", "media"
        )
        self.assertEqual(message, "Bucket 'media' not found at this endpoint")

    def test_refused_save_reopens_the_form_with_values(self):
        StorageConnection.objects.create(name="taken", provider_type="aws", bucket="b1")
        resp = self.client.post(
            reverse("cpanel:storage_connection_save"),
            {
                "provider_type": "aws", "name": "taken", "region": "us-east-1",
                "bucket": "new-bucket", "access_key": "AKIATYPED", "secret_key": "typed-secret",
                "use_ssl": "on", "enabled": "on",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["storage_form_open"])
        html = resp.content.decode()
        self.assertIn("Not saved", html)
        self.assertIn("A connection with this name already exists.", html)
        self.assertIn('value="new-bucket"', html)
        self.assertIn('value="AKIATYPED"', html)
        self.assertEqual(StorageConnection.objects.filter(name__iexact="taken").count(), 1)

    def test_page_markup(self):
        StorageConnection.objects.create(
            name="spaces", provider_type="aws", bucket="cf-pyrun-v2-test-backups"
        )
        html = self.client.get(reverse("cpanel:services")).content.decode()
        # U47(1): one shared renderer that moves the box under the clicked button.
        self.assertIn("function placeTestResult(div, btn)", html)
        self.assertIn("storageShowResult(data, btn)", html)
        # U47(3)
        self.assertIn('max-w-[14rem] truncate" title="cf-pyrun-v2-test-backups"', html)
        # U48(1): five card headers stack below sm and drop the icon on phones.
        self.assertEqual(html.count("flex flex-col-reverse sm:flex-row sm:items-center"), 5)
        self.assertGreaterEqual(html.count("hidden sm:grid place-items-center"), 5)
        # U59: the AI test answer goes through the shared Markdown renderer.
        self.assertIn("js/markdown.", html)
        self.assertIn("window.PyRunMarkdown.render(detail)", html)
        # U60: every saved-row table marks its Last tested cell.
        self.assertIn("data-last-tested", html)
        self.assertIn("function markRowTested(btn)", html)


class AIOffBannerTests(ServicesBase):
    """U109: a saved provider with the AI switch off says "AI is off" and offers the switch."""

    def _provider(self):
        from core.models import AIProvider

        return AIProvider.objects.create(name="router", provider_type="openrouter", default_model="m")

    def test_banner_shows_when_providers_exist_and_ai_is_off(self):
        provider = self._provider()
        gs = GlobalSettings.get_settings()
        gs.active_ai_provider = provider
        gs.claude_enabled = False
        gs.save()
        html = self.client.get(reverse("cpanel:services")).content.decode()
        self.assertIn('id="ai-off-banner"', html)
        self.assertIn("AI is off", html)
        banner = html[html.index('id="ai-off-banner"'):]
        banner = banner[:banner.index("</form>")]
        self.assertIn(f'value="{provider.pk}"', banner)
        self.assertIn('name="claude_enabled" value="on"', banner)
        # the banner's form is the one the master switch already posts to
        resp = self.client.post(reverse("cpanel:claude_settings"),
                                {"claude_enabled": "on", "active_provider": str(provider.pk)})
        self.assertEqual(resp.status_code, 302)
        gs.refresh_from_db()
        self.assertTrue(gs.claude_enabled)
        self.assertEqual(gs.active_ai_provider_id, provider.pk)
        html = self.client.get(reverse("cpanel:services")).content.decode()
        self.assertNotIn('id="ai-off-banner"', html)

    def test_no_banner_without_providers(self):
        html = self.client.get(reverse("cpanel:services")).content.decode()
        self.assertNotIn('id="ai-off-banner"', html)

    def test_banner_falls_back_to_the_first_provider_when_none_is_active(self):
        provider = self._provider()
        html = self.client.get(reverse("cpanel:services")).content.decode()
        banner = html[html.index('id="ai-off-banner"'):]
        banner = banner[:banner.index("</form>")]
        self.assertIn(f'name="active_provider" value="{provider.pk}"', banner)


class NewProviderPlaceholderTests(SimpleTestCase):
    def test_add_forms_ask_for_the_key(self):
        def ph(form, name):
            return form.fields[name].widget.attrs.get("placeholder", "")

        self.assertEqual(ph(AIProviderForm(), "credential"), "Paste the API key or token")
        self.assertEqual(ph(EmbeddingProviderForm(), "credential"), "Paste the API key")
        self.assertIn("Paste the API key", ph(VectorConnectionForm(), "api_key"))
        self.assertEqual(ph(StorageConnectionForm(), "access_key"), "Paste the access key ID")
        self.assertEqual(ph(StorageConnectionForm(), "secret_key"), "Paste the secret access key")


class UsagePageTests(ServicesBase):
    def test_chart_has_every_day_and_dead_scripts_are_not_linked(self):
        live = Script.objects.create(
            name="alive", code="print(1)", workspace=self.ws, environment=self.env
        )
        ClaudeUsage.objects.create(script_id=live.pk, script_name="alive", input_tokens=10, output_tokens=5)
        old = ClaudeUsage.objects.create(
            script_id=uuid.uuid4(), script_name="gone", input_tokens=7, output_tokens=1,
            cache_read_tokens=100,
        )
        ClaudeUsage.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=4))

        resp = self.client.get(reverse("cpanel:claude_usage") + "?period=7")
        chart = resp.context["chart_json"]
        self.assertEqual(len(chart["labels"]), 7)
        self.assertEqual(sum(1 for n in chart["input"] if n), 2)
        html = resp.content.decode()
        self.assertIn(reverse("cpanel:script_detail", args=[live.pk]), html)
        self.assertNotIn(f"/scripts/{old.script_id}/", html)
        self.assertIn("(deleted)", html)
        self.assertIn("Total incl. cache", html)
        self.assertIn("Input + output", html)
