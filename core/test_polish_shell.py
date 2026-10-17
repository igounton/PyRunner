"""
M4 polish batch (2.0 release test) — console shell and dashboard.

F22   header meters, dashboard resources card and their API: superusers only
U52   members don't see the Py AI link they'd be refused from
U15   a fresh instance's empty states lead to "Create your first script"
U27   relative times under a minute read "just now" / "in under a minute"
U28   the run-pulse legend names cancelled runs
U33   while schedules are paused the hero says so and links to the Schedules tab
U56   in a container the worker-down banner doesn't say "manage.py qcluster"
F19   the header Docs link no longer points at the unpublished pyrun.ai/docs
F20   the dashboard body grid has an explicit single mobile column
F16/F21/U18/U80  header/dashboard polling and drawer script behaviour
"""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Environment, GlobalSettings, User, Workspace, WorkspaceMembership
from core.templatetags.time_tags import ago, until

PW = "correct-horse-battery-staple-91"
WORKER = "core.services.system_info_service.SystemInfoService.get_worker_status"


def _worker(status):
    return mock.patch(WORKER, return_value={"status": status, "status_text": status.title(), "queued_tasks": 0})


class ShellBase(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        self.admin = User.objects.create_user(
            username="admin@example.com", email="admin@example.com", password=PW,
            is_superuser=True, is_staff=True,
        )
        self.member = User.objects.create_user(
            username="member@example.com", email="member@example.com", password=PW,
        )
        ws = Workspace.objects.get(is_default=True)
        WorkspaceMembership.objects.update_or_create(
            user=self.member, workspace=ws, defaults={"role": WorkspaceMembership.ROLE_MEMBER}
        )

    def dashboard(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("cpanel:dashboard"))


class ResourceMetersAdminOnlyTests(ShellBase):
    def test_member_gets_no_meters_card_or_api(self):
        resp = self.dashboard(self.member)
        self.assertNotContains(resp, 'id="headerResources"')
        self.assertNotContains(resp, "System resources</h2>")
        api = self.client.get(reverse("cpanel:system_resources_api"))
        self.assertEqual(api.status_code, 403)

    def test_superuser_keeps_meters_card_and_api(self):
        resp = self.dashboard(self.admin)
        self.assertContains(resp, 'id="headerResources"')
        self.assertContains(resp, "System resources</h2>")
        self.assertEqual(self.client.get(reverse("cpanel:system_resources_api")).status_code, 200)


class PyAiLinkTests(ShellBase):
    def test_member_sidebar_has_no_py_ai_link(self):
        self.assertNotContains(self.dashboard(self.member), reverse("cpanel:pyai"))

    def test_superuser_sidebar_keeps_py_ai_link(self):
        self.assertContains(self.dashboard(self.admin), reverse("cpanel:pyai"))


class DashboardCopyTests(ShellBase):
    def test_fresh_instance_points_at_create_script(self):
        resp = self.dashboard(self.admin)
        self.assertContains(resp, "Create your first script")
        self.assertContains(resp, reverse("cpanel:script_create"))

    def test_relative_time_filters_under_a_minute(self):
        now = timezone.now()
        self.assertEqual(ago(now - timedelta(seconds=20)), "just now")
        self.assertEqual(ago(now - timedelta(minutes=5, seconds=2)), "5\xa0minutes ago")
        self.assertEqual(until(now + timedelta(seconds=30)), "in under a minute")

    def test_pulse_legend_names_cancelled_and_grid_has_one_mobile_column(self):
        resp = self.dashboard(self.admin)
        self.assertContains(resp, "Cancelled")
        self.assertContains(resp, "grid grid-cols-1 lg:grid-cols-3")

    def test_paused_schedules_turn_the_hero_amber_and_link_the_tab(self):
        gs = GlobalSettings.get_settings()
        gs.schedules_paused = True
        gs.save()
        with _worker("running"):
            resp = self.dashboard(self.admin)
        self.assertNotContains(resp, "All systems operational")
        self.assertContains(resp, reverse("cpanel:settings") + "#schedules")

    def test_worker_down_banner_in_a_container_names_settings_workers(self):
        with _worker("stopped"), mock.patch("core.views.dashboard._in_container", return_value=True):
            resp = self.dashboard(self.admin)
        self.assertContains(resp, "Settings → Workers")
        self.assertNotContains(resp, "python manage.py qcluster")

    def test_worker_down_banner_on_bare_metal_keeps_the_command(self):
        with _worker("stopped"), mock.patch("core.views.dashboard._in_container", return_value=False):
            resp = self.dashboard(self.admin)
        self.assertContains(resp, "python manage.py qcluster")


class HeaderScriptTests(ShellBase):
    def test_docs_link_points_at_the_readme(self):
        resp = self.dashboard(self.admin)
        self.assertNotContains(resp, "https://pyrun.ai/docs/")
        self.assertContains(resp, "github.com/hassancs91/PyRun#readme")

    def test_polling_and_drawer_scripts(self):
        html = self.dashboard(self.admin).content.decode()
        # U80: a signed-out poll stops and tells the user
        self.assertIn("Your session ended", html)
        # F16: an aborted poll on navigation isn't logged as an error
        self.assertNotIn("console.error('Failed to refresh header resources", html)
        # F21: the dashboard card skips polls while the tab is hidden
        self.assertIn("if (document.hidden) return;", html)
        # U18: crossing into desktop closes the drawer
        self.assertIn("if (e.matches) window.closeSidebar();", html)
