"""
M4 polish batch (2.0 release test) — Logs and workspace members.

U76   Logs: a message opens in full (with logger + exception), pager links URL-encode the
      filters, auto-refresh redraws the table from the API, Clear confirms inline
U78   Members: owner -> admin -> member, "(you)", a self-removal warning
"""

from datetime import datetime
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.models import Environment, GlobalSettings, User, Workspace, WorkspaceMembership
from core.services.log_service import LogEntry


class AdminBase(TestCase):
    def setUp(self):
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
            user=self.admin, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_ADMIN}
        )
        self.client.force_login(self.admin)


def _entries(n=150):
    long = "x" * 900 + " END-OF-MESSAGE"
    return [
        LogEntry(datetime(2026, 10, 17, 9, 0, i % 60), "ERROR", "core.tasks", long, exception="Traceback: boom")
        for i in range(n)
    ]


class LogsPageTests(AdminBase):
    def test_full_message_pager_encoding_and_inline_clear(self):
        def fake_read(**kwargs):
            return _entries()[kwargs["offset"]:kwargs["offset"] + kwargs["limit"]], 150

        with mock.patch("core.views.logs.LogService.read_logs", side_effect=fake_read):
            html = self.client.get(reverse("cpanel:logs") + "?search=a%26b+C%2B%2B%23").content.decode()
        self.assertIn('<details class="log-entry">', html)
        self.assertIn("END-OF-MESSAGE</pre>", html)
        self.assertIn("Logger: core.tasks", html)
        self.assertIn("?page=2&search=a%26b+C%2B%2B%23", html)
        self.assertNotIn("&search=a&b", html)
        self.assertNotIn("confirm(", html)
        self.assertNotIn("alert(", html)
        self.assertNotIn("window.location.reload();\n        }, 5000)", html)
        self.assertIn("setInterval(refreshLogs, 5000)", html)

    def test_api_page_view_applies_the_filters(self):
        with mock.patch("core.views.logs.LogService.read_logs", return_value=(_entries(3), 3)) as read:
            data = self.client.get(
                reverse("cpanel:logs_api") + "?view=page&page=1&level=ERROR&search=boom"
            ).json()
        self.assertTrue(data["success"])
        self.assertEqual(data["total_count"], 3)
        self.assertEqual(len(data["entries"]), 3)
        self.assertEqual(read.call_args.kwargs["level_filter"], "ERROR")
        self.assertEqual(read.call_args.kwargs["search_query"], "boom")


class MembersPageTests(AdminBase):
    def test_owner_first_you_marker_and_self_removal_warning(self):
        owner = User.objects.create_user(username="zed@example.com", email="zed@example.com")
        member = User.objects.create_user(username="amy@example.com", email="amy@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=owner, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_OWNER}
        )
        WorkspaceMembership.objects.update_or_create(
            user=member, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_MEMBER}
        )
        resp = self.client.get(reverse("cpanel:workspace_members", args=[self.ws.pk]))
        emails = [m.user.email for m in resp.context["memberships"]]
        self.assertLess(emails.index("zed@example.com"), emails.index("root@example.com"))
        self.assertLess(emails.index("root@example.com"), emails.index("amy@example.com"))
        html = resp.content.decode()
        self.assertIn('root@example.com <span class="text-xs font-normal text-faint">(you)</span>', html)
        self.assertIn("You will lose access to it.", html)
