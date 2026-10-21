"""A run that finishes while the database connection is broken keeps its result (F109, 2.0).

On instance B (PostgreSQL core) the shared PostgreSQL server was restarted while a
scheduled run executed. The script finished normally, but the worker's final save
used the connection the restart had killed ("terminating connection due to
administrator command"): the status and output were lost and the run showed
"running" until reconcile_stale's deadline, then "failed" with a message blaming
a worker restart. The final save now reconnects and retries.
"""

import sys
from unittest import mock

from django.db import OperationalError, connection
from django.db.models.query import QuerySet
from django.test import TestCase

from core import executor
from core.executor import execute_run, remove_script_workdir
from core.models import Environment, Run, Script, Workspace

_CODE = "print('finished fine')\n"


def _failing_final_update(failures):
    """QuerySet.update that raises like a killed PostgreSQL connection for the
    first `failures` final-state writes (the ones carrying stdout)."""
    real_update = QuerySet.update
    state = {"left": failures, "calls": 0}

    def update(self, **kwargs):
        if "stdout" in kwargs and self.model is Run:
            state["calls"] += 1
            if state["left"] > 0:
                state["left"] -= 1
                raise OperationalError("terminating connection due to administrator command")
        return real_update(self, **kwargs)

    return update, state


class FinalSaveRetryTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        env = Environment.objects.create(name="fs-env", path="fs-env")
        self.script = Script.objects.create(name="fs", code=_CODE, environment=env, workspace=self.ws)
        self.addCleanup(remove_script_workdir, self.script.pk)

    def _run(self, failures):
        run = Run.objects.create(script=self.script, workspace=self.ws)
        update, state = _failing_final_update(failures)
        with mock.patch("core.executor._validate_environment", return_value=sys.executable), \
                mock.patch.object(QuerySet, "update", update), \
                mock.patch("time.sleep") as sleep:
            try:
                execute_run(run)
            finally:
                run.refresh_from_db()
        return run, state, sleep

    def test_result_saved_after_the_connection_comes_back(self):
        run, state, sleep = self._run(failures=2)
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        self.assertEqual(run.exit_code, 0)
        self.assertIn("finished fine", run.stdout)
        self.assertIsNotNone(run.ended_at)
        self.assertIsNone(run.pid)
        self.assertEqual(state["calls"], 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])

    def test_gives_up_after_the_last_delay_and_raises(self):
        n = len(executor.FINAL_SAVE_RETRY_DELAYS)
        with self.assertRaises(OperationalError):
            self._run(failures=n + 1)
        run = Run.objects.get(script=self.script)
        self.assertEqual(run.status, Run.Status.RUNNING)  # reconcile_stale settles it later

    def test_external_cancel_is_still_not_clobbered(self):
        run = Run.objects.create(script=self.script, workspace=self.ws, status=Run.Status.CANCELLED)
        run.status = Run.Status.SUCCESS
        run.stdout = "late"
        self.assertEqual(executor._persist_final_state(run), 0)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.CANCELLED)

    def test_closes_the_broken_connection_outside_a_transaction(self):
        run = Run.objects.create(script=self.script, workspace=self.ws, status=Run.Status.RUNNING)
        run.status, run.stdout, run.exit_code = Run.Status.SUCCESS, "ok", 0
        update, state = _failing_final_update(1)
        with mock.patch.object(QuerySet, "update", update), mock.patch("time.sleep"), \
                mock.patch.object(connection, "in_atomic_block", False), \
                mock.patch.object(connection, "close") as close:
            self.assertEqual(executor._persist_final_state(run), 1)
        close.assert_called_once()
        run.refresh_from_db()
        self.assertEqual((run.status, run.stdout), (Run.Status.SUCCESS, "ok"))
