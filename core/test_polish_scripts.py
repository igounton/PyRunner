"""
M4 polish batch (2.0 release test) — scripts, runs and tasks pages.

U21   a refused script save shows an error summary at the top
U22   truncated output is badged; counts are formatted
U24   repeat forks are numbered
U26   the task-history filter sits in the history header
U29   next run in the schedule's own timezone; yearly dates as "3 Mar"
U32   stderr of a successful run is shown as warnings, not as a failure
U33   the schedule control says Pause/Resume; held while schedules are paused
U57   the script page names its working folder
U70   a run stopped by a resource limit says which (also F30's CPU kill)
U77   a run whose policy wants the sandbox says the host can't provide it
"""

import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.executor import limit_hint
from core.models import (
    Environment, GlobalSettings, Run, Script, ScriptSchedule, User, Workspace,
)
from core.services.script_service import fork_script
from core.templatetags.time_tags import in_tz, thousands

PW = "correct-horse-battery-staple-91"


class ScriptsBase(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        self.ws = Workspace.get_default()
        Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        self.env = Environment.objects.create(name="penv", path=f"penv{uuid.uuid4().hex[:6]}")
        self.admin = User.objects.create_user(
            username="admin@example.com", email="admin@example.com", password=PW,
            is_superuser=True, is_staff=True,
        )
        self.script = Script.objects.create(
            name="w22-script", code="print(1)", environment=self.env,
            workspace=self.ws, created_by=self.admin,
        )
        self.client.force_login(self.admin)

    def run_with(self, **kw):
        defaults = dict(script=self.script, workspace=self.ws, status=Run.Status.SUCCESS, exit_code=0)
        defaults.update(kw)
        return Run.objects.create(**defaults)


class ForkNameTests(ScriptsBase):
    def test_repeat_forks_are_numbered(self):
        first = fork_script(self.script, user=self.admin)
        second = fork_script(self.script, user=self.admin)
        third = fork_script(self.script, user=self.admin)
        self.assertEqual(first.name, "Copy of w22-script")
        self.assertEqual(second.name, "Copy of w22-script (2)")
        self.assertEqual(third.name, "Copy of w22-script (3)")


class LimitHintTests(TestCase):
    LIMITS = {"memory_bytes": 256 * 1024 * 1024, "cpu_seconds": 5, "fsize_bytes": 10 * 1024 * 1024}

    def test_memory_error_names_the_memory_limit(self):
        hint = limit_hint(1, "Traceback...\nMemoryError\n", self.LIMITS)
        self.assertIn("256 MB memory limit", hint)

    def test_sigkill_names_the_cpu_limit(self):
        self.assertIn("5 s CPU-time limit", limit_hint(-9, "", self.LIMITS))

    def test_no_hint_without_limits_or_on_success(self):
        self.assertIsNone(limit_hint(-9, "", None))
        self.assertIsNone(limit_hint(0, "MemoryError", self.LIMITS))
        self.assertIsNone(limit_hint(1, "ValueError: x", self.LIMITS))


class RunDetailTests(ScriptsBase):
    def detail(self, run):
        return self.client.get(reverse("cpanel:run_detail", kwargs={"pk": run.pk})).content.decode()

    def test_truncated_output_is_badged_and_counts_formatted(self):
        run = self.run_with(stdout="x" * 1200 + "\n[OUTPUT TRUNCATED: 5 bytes cut from the middle.]\n")
        html = self.detail(run)
        self.assertIn("Truncated — start and end kept", html)
        self.assertIn("1,2", html)
        self.assertEqual(thousands(1000000), "1,000,000")

    def test_successful_run_stderr_is_warnings_not_failure(self):
        ok = self.detail(self.run_with(stderr="DeprecationWarning: old"))
        self.assertIn("warnings / log output", ok)
        failed = self.detail(self.run_with(status=Run.Status.FAILED, exit_code=1, stderr="Traceback"))
        self.assertNotIn("warnings / log output", failed)

    def test_isolation_note_when_the_host_cant_sandbox(self):
        run = self.run_with()
        decision = mock.Mock(sandbox=True)
        with mock.patch("core.executor.resolve_isolation", return_value=decision), \
                mock.patch("core.views.runs.host_sandbox_tier", return_value="rlimits_only"):
            html = self.detail(run)
        self.assertIn("this host can&#x27;t provide the sandbox", html)
        with mock.patch("core.executor.resolve_isolation", return_value=decision), \
                mock.patch("core.views.runs.host_sandbox_tier", return_value="full"):
            self.assertNotIn("provide the sandbox", self.detail(run))


class ScriptDetailTests(ScriptsBase):
    def test_working_folder_and_schedule_controls(self):
        sched, _ = ScriptSchedule.objects.get_or_create(
            script=self.script, defaults={"workspace": self.ws, "created_by": self.admin}
        )
        sched.run_mode = "interval"
        sched.interval_minutes = 30
        sched.is_active = True
        sched.save()
        gs = GlobalSettings.get_settings()
        gs.schedules_paused = True
        gs.save()
        html = self.client.get(reverse("cpanel:script_detail", kwargs={"pk": self.script.pk})).content.decode()
        self.assertIn("Working folder", html)
        self.assertIn(str(self.script.pk), html)
        self.assertIn("Pause this schedule", html)
        self.assertIn("Held — all schedules paused", html)

    def test_next_run_in_the_schedule_zone_with_its_name(self):
        moment = datetime(timezone.now().year, 3, 2, 22, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(in_tz(moment, "Asia/Tokyo"), "3 Mar, 07:00 (Asia/Tokyo)")
        later = moment + timedelta(days=400)
        self.assertIn(str(later.year), in_tz(later, "UTC"))

    def test_yearly_display_is_unambiguous(self):
        sched = ScriptSchedule(script=self.script, run_mode="yearly", yearly_month=3,
                               yearly_day=3, yearly_time="07:00", timezone="Asia/Tokyo")
        self.assertIn("Yearly on 3 Mar at 07:00", sched.schedule_display)


class EditAndTasksTests(ScriptsBase):
    def test_refused_save_has_an_error_summary(self):
        resp = self.client.post(
            reverse("cpanel:script_edit", kwargs={"pk": self.script.pk}),
            {"name": "", "code": "print(1)", "run_mode": "cron", "cron_expression": "nope"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Not saved — fix the highlighted fields.")

    def test_history_filter_sits_in_the_history_header(self):
        html = self.client.get(reverse("cpanel:tasks")).content.decode()
        head = html.index("Task history</h2>")
        self.assertGreater(html.index("?status=failed"), head)
