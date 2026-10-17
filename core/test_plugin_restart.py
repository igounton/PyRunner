"""The plugin "Restarting…" page must wait for the restarted process (2.0 release test, 22.02).

The controlled restart is a SIGTERM sent a few seconds after "Restart now". The
interstitial used to poll the plugin list and go back on the first complete page,
which the OLD process still served (measured on rc17: back after 3.5 s, SIGTERM
at 4.6 s, new process up at 50 s), so the user landed on a stale "Restart
required" page. The page now polls a status endpoint that says whether the
answering process loaded its plugins after the restart was requested.
"""

import time
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import User


class _Base(TestCase):
    def setUp(self):
        for target in (
            "core.services.setup_service.SetupService.is_setup_needed",
            "core.services.setup_service.SetupService.needs_admin_setup",
        ):
            patch = mock.patch(target, return_value=False)
            patch.start()
            self.addCleanup(patch.stop)
        self.admin = User.objects.create(email="admin@example.com", is_superuser=True, is_staff=True)
        self.client.force_login(self.admin)


class RestartRedirectTests(_Base):
    def test_restart_redirects_with_the_request_time(self):
        before = time.time()
        with mock.patch("core.views.plugins.PluginService.trigger_restart") as trigger:
            resp = self.client.post(reverse("cpanel:plugin_restart"))
        trigger.assert_called_once()
        self.assertEqual(resp.status_code, 302)
        path, _, query = resp["Location"].partition("?")
        self.assertEqual(path, reverse("cpanel:plugin_restarting"))
        since = float(query.split("since=", 1)[1])
        self.assertGreaterEqual(since, before - 0.01)
        self.assertLessEqual(since, time.time() + 0.01)

    def test_interstitial_polls_the_status_endpoint_with_since(self):
        resp = self.client.get(reverse("cpanel:plugin_restarting") + "?since=123.5")
        self.assertContains(resp, reverse("cpanel:plugin_restart_status") + "?since=123.500")
        self.assertContains(resp, "data.restarted")


class RestartStatusTests(_Base):
    @override_settings(PLUGINS_LOADED_AT=1000.0)
    def test_old_process_is_not_restarted(self):
        resp = self.client.get(reverse("cpanel:plugin_restart_status") + "?since=2000")
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.json()["restarted"], False)
        self.assertEqual(resp["Cache-Control"], "no-store")

    @override_settings(PLUGINS_LOADED_AT=3000.0)
    def test_new_process_is_restarted(self):
        resp = self.client.get(reverse("cpanel:plugin_restart_status") + "?since=2000")
        self.assertIs(resp.json()["restarted"], True)
        self.assertIn("pending_restart", resp.json())

    @override_settings(PLUGINS_LOADED_AT=3000.0)
    def test_missing_or_bad_since_counts_as_restarted(self):
        for query in ("", "?since=", "?since=abc", "?since=nan", "?since=inf"):
            resp = self.client.get(reverse("cpanel:plugin_restart_status") + query)
            self.assertIs(resp.json()["restarted"], True, query)

    def test_non_superuser_gets_json_403(self):
        member = User.objects.create(email="member@example.com")
        self.client.force_login(member)
        resp = self.client.get(reverse("cpanel:plugin_restart_status") + "?since=1")
        self.assertEqual(resp.status_code, 403)
        self.assertIn("error", resp.json())
