"""Login lockout and reCAPTCHA use the proxy-aware client IP (core.ratelimit.client_ip)."""

from unittest import mock

from axes.utils import reset
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from core.models import GlobalSettings, User
from core.ratelimit import lockout_client_ip
from core.services import EncryptionService
from core.test_plugin_api import _mock_setup

PASSWORD = "Correct-Horse-91!"
PROXY = {"REMOTE_ADDR": "10.0.1.5"}  # every request arrives from the reverse proxy


@override_settings(AXES_ENABLED=True)
class LockoutBehindProxyTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        User.objects.create_superuser(username="admin", email="admin@ip.test", password=PASSWORD)
        self.addCleanup(reset)

    def _login(self, password, forwarded_for):
        return self.client.post(
            "/auth/login/",
            {"email": "admin@ip.test", "password": password},
            HTTP_X_FORWARDED_FOR=forwarded_for,
            **PROXY,
        )

    def _attacker_fails_six_times(self):
        for _ in range(6):
            self._login("wrong-password", "203.0.113.66")

    @override_settings(RATELIMIT_TRUSTED_PROXY_DEPTH=1)
    def test_attacker_cannot_lock_out_the_admin_from_another_address(self):
        self._attacker_fails_six_times()
        self.assertEqual(self._login(PASSWORD, "203.0.113.66").status_code, 429)
        response = self._login(PASSWORD, "198.51.100.7")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/cpanel/")

    @override_settings(RATELIMIT_TRUSTED_PROXY_DEPTH=1)
    def test_a_spoofed_leftmost_forwarded_for_does_not_escape_the_lockout(self):
        self._attacker_fails_six_times()
        # The proxy appends the real address on the right; the left is caller-controlled.
        self.assertEqual(self._login(PASSWORD, "198.51.100.7, 203.0.113.66").status_code, 429)


class RecaptchaClientIpTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        settings = GlobalSettings.get_settings()
        settings.recaptcha_enabled = True
        settings.recaptcha_site_key = "site"
        settings.recaptcha_secret_key_encrypted = EncryptionService.encrypt("secret")
        settings.save()

    @override_settings(RATELIMIT_TRUSTED_PROXY_DEPTH=1)
    def test_verification_gets_the_proxy_appended_address(self):
        with mock.patch("core.views.auth.RecaptchaService.verify", return_value=False) as verify:
            self.client.post(
                "/auth/login/",
                {"email": "a@ip.test", "password": "x", "g-recaptcha-response": "t"},
                HTTP_X_FORWARDED_FOR="1.2.3.4, 198.51.100.7",
                **PROXY,
            )
        self.assertEqual(verify.call_args.args[2], "198.51.100.7")


class LockoutClientIpTests(SimpleTestCase):
    """Axes stores this in an inet column on PostgreSQL: only valid IPs or None."""

    def test_valid_addresses_pass_through(self):
        request = RequestFactory().get("/", REMOTE_ADDR="10.0.1.5", HTTP_X_FORWARDED_FOR="2001:db8::1")
        self.assertEqual(lockout_client_ip(request), "10.0.1.5")
        with override_settings(RATELIMIT_TRUSTED_PROXY_DEPTH=1):
            self.assertEqual(lockout_client_ip(request), "2001:db8::1")

    def test_missing_or_malformed_address_is_none(self):
        no_peer = RequestFactory().get("/")
        no_peer.META.pop("REMOTE_ADDR", None)
        self.assertIsNone(lockout_client_ip(no_peer))
        garbage = RequestFactory().get("/", REMOTE_ADDR="10.0.1.5", HTTP_X_FORWARDED_FOR="not-an-ip")
        with override_settings(RATELIMIT_TRUSTED_PROXY_DEPTH=1):
            self.assertIsNone(lockout_client_ip(garbage))
