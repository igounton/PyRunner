"""
2.0 release test 25.04 (F90): the operator-URL secret backends (custom, Vault, Infisical)
reached any URL their config named. A provider pointed at the cloud-metadata endpoint
fetched instance credentials and returned them as "secret values". They now pass the shared
SSRF guard before every request, like storage, webhooks and vector stores.
"""

from unittest import mock

from django.test import SimpleTestCase, override_settings

from core.models import SecretProvider
from core.services.secret_backends import get_backend
from core.services.secret_backends.base import SecretResolutionError

META = "http://169.254.169.254/latest/meta-data/"
GCE_IP = "http://169.254.169.254/"


def _provider(ptype, **config):
    p = SecretProvider(provider_type=ptype, name="t", config=config)
    p.set_credentials({"token": "x", "client_id": "a", "client_secret": "b"})
    return p


class SecretBackendSsrfTests(SimpleTestCase):
    def setUp(self):
        # Make a failed guard observable even if a request somehow slipped through.
        self.get = mock.patch("requests.get", side_effect=AssertionError("request not blocked")).start()
        self.post = mock.patch("requests.post", side_effect=AssertionError("request not blocked")).start()
        self.addCleanup(mock.patch.stopall)

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
    def test_custom_metadata_refused_even_with_flag(self):
        ok, msg = get_backend("custom").test_connection(_provider("custom", url=META))
        self.assertFalse(ok)
        self.assertIn("not allowed", msg.lower())
        with self.assertRaises(SecretResolutionError):
            get_backend("custom").fetch(_provider("custom", url=GCE_IP), "x")
        self.get.assert_not_called()

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
    def test_vault_metadata_refused(self):
        ok, msg = get_backend("vault").test_connection(_provider("vault", base_url=GCE_IP))
        self.assertFalse(ok)
        self.assertIn("not allowed", msg.lower())
        self.get.assert_not_called()

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
    def test_infisical_metadata_refused(self):
        ok, msg = get_backend("infisical").test_connection(
            _provider("infisical", base_url=GCE_IP, project_id="p", environment="dev")
        )
        self.assertFalse(ok)
        self.assertIn("not allowed", msg.lower())
        self.post.assert_not_called()

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=False)
    def test_custom_private_refused_without_flag(self):
        ok, msg = get_backend("custom").test_connection(_provider("custom", url="http://127.0.0.1:9000/"))
        self.assertFalse(ok)
        self.assertIn("not allowed", msg.lower())

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
    def test_public_url_still_reaches_the_request(self):
        # With a public URL the guard passes and the (mocked) request is attempted.
        with self.assertRaises(AssertionError):
            get_backend("custom").test_connection(_provider("custom", url="https://secrets.example.com/v"))
        self.get.assert_called_once()
