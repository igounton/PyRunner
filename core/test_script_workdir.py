"""Per-script working folders (F28, 2.0).

Each script runs in ``SCRIPTS_WORKDIR/<script id>/``: files a run writes are
there for the script's next run, other scripts never see them, and the folder is
removed when the script is deleted — but not when a backup restore deletes and
re-creates the same script id.
"""

import sys
from pathlib import Path
from unittest import mock

from django.db import transaction
from django.test import TestCase

from core.executor import execute_run, remove_script_workdir, script_workdir
from core.models import Environment, Run, Script, Workspace

_WRITE_AND_LIST = (
    "import os, pathlib\n"
    "print('CWD=' + os.getcwd())\n"
    "print('SEEN=' + ','.join(sorted(p for p in os.listdir('.') if not p.startswith('.'))))\n"
    "pathlib.Path('state-' + os.environ['PYRUNNER_SCRIPT_TAG'] + '.txt').write_text('x')\n"
)


def _stub_python(func):
    """Run the real script with the test runner's own Python."""
    return mock.patch(
        "core.executor._validate_environment", return_value=sys.executable
    )(func)


class ScriptWorkdirTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        self.env = Environment.objects.create(name="wd-env", path="wd-env")
        self.a = Script.objects.create(
            name="wd-a", code=_WRITE_AND_LIST, environment=self.env, workspace=self.ws
        )
        self.b = Script.objects.create(
            name="wd-b", code=_WRITE_AND_LIST, environment=self.env, workspace=self.ws
        )
        for s in (self.a, self.b):
            self.addCleanup(remove_script_workdir, s.pk)

    def _run(self, script, tag):
        run = Run.objects.create(script=script, workspace=self.ws)
        with mock.patch.dict("os.environ", {"PYRUNNER_SCRIPT_TAG": tag}):
            execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCESS, run.stderr)
        return run.stdout

    @_stub_python
    def test_each_script_runs_in_its_own_folder_kept_between_runs(self, _val):
        out = self._run(self.a, "a1")
        folder_a = script_workdir(self.a.pk)
        self.assertIn("CWD=" + str(folder_a), out)
        self.assertEqual(folder_a.name, str(self.a.pk))

        # Script B starts in its own folder and does not see A's file.
        out = self._run(self.b, "b1")
        self.assertIn("CWD=" + str(script_workdir(self.b.pk)), out)
        self.assertIn("SEEN=\n", out + "\n")
        self.assertNotIn("state-a1.txt", out)

        # A's next run finds what its previous run wrote.
        out = self._run(self.a, "a2")
        self.assertIn("SEEN=state-a1.txt", out)
        # The temp script file is gone after the run; the state file stays.
        self.assertEqual(
            sorted(p.name for p in folder_a.iterdir()), ["state-a1.txt", "state-a2.txt"]
        )

    @_stub_python
    def test_deleting_a_script_removes_its_folder(self, _val):
        self._run(self.a, "a1")
        self._run(self.b, "b1")
        folder_a, folder_b = script_workdir(self.a.pk), script_workdir(self.b.pk)
        self.assertTrue(folder_a.is_dir())

        with self.captureOnCommitCallbacks(execute=True):
            self.a.delete()
        self.assertFalse(folder_a.exists())
        self.assertTrue(folder_b.is_dir())  # other scripts untouched

    @_stub_python
    def test_restore_style_delete_and_recreate_keeps_the_folder(self, _val):
        # A backup restore deletes every script and re-creates it under the same
        # id in one transaction: the folder must survive.
        self._run(self.a, "a1")
        folder_a = script_workdir(self.a.pk)
        fields = {"id": self.a.pk, "name": self.a.name, "code": self.a.code,
                  "environment": self.env, "workspace": self.ws}
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                Script.objects.filter(pk=self.a.pk).delete()
                Script.objects.create(**fields)
        self.assertTrue((folder_a / "state-a1.txt").exists())

    def test_rolled_back_delete_keeps_the_folder(self):
        folder_a = script_workdir(self.a.pk)
        folder_a.mkdir(parents=True, exist_ok=True)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    self.a.delete()
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.assertEqual(callbacks, [])
        self.assertTrue(folder_a.is_dir())

    def test_remove_refuses_paths_outside_the_workdir(self):
        self.assertFalse(remove_script_workdir("../environments"))
        self.assertFalse(remove_script_workdir(".."))
        self.assertFalse(remove_script_workdir("does-not-exist"))
        self.assertTrue(Path(script_workdir("x")).parent.is_dir())
