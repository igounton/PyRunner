"""
Restoring a backup rebuilds environments (2.0 release test §19.02, F64).

A backup carries environments as rows plus their saved ``requirements`` (the
``pip freeze`` PyRun keeps after every package operation), never the venv
folders. Before this fix a restore left any environment whose folder was gone
(a new server, or an environment deleted after the backup) unusable — every
script using it failed with "Environment directory not found" — while the
restore message pointed to a "Recreate" action that didn't exist. Now the
restore queues a rebuild (missing venvs recreated at the same path, missing
saved packages reinstalled) and the environment page offers Recreate.
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.messages import get_messages
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Environment, PackageOperation, User
from core.services.backup_service import BackupService
from core.services.environment_service import EnvironmentService
from core.tasks import rebuild_environments_task

_TMP = Path(tempfile.mkdtemp(prefix="pyrun-envrebuild-"))


def _setup_wizard_off(test):
    for target in (
        "core.services.setup_service.SetupService.is_setup_needed",
        "core.services.setup_service.SetupService.needs_admin_setup",
    ):
        p = mock.patch(target, return_value=False)
        p.start()
        test.addCleanup(p.stop)


def _make_python(env):
    """Give ``env`` a folder with an interpreter file (a healthy venv, as far as PyRun checks)."""
    python = Path(env.get_python_executable())
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("")


@override_settings(ENVIRONMENTS_ROOT=str(_TMP / "envs"))
class RecreateEnvironmentServiceTests(TestCase):
    def setUp(self):
        shutil.rmtree(_TMP / "envs", ignore_errors=True)
        (_TMP / "envs").mkdir(parents=True)
        self.env = Environment.objects.create(name="w-env", path="w-env", python_version="3.99.1")

    def test_a_missing_folder_is_recreated_at_the_same_path(self):
        with mock.patch.object(EnvironmentService, "discover_python_versions", return_value=[]), \
                mock.patch.object(EnvironmentService, "create_environment", return_value=(True, "ok")) as create:
            ok, _ = EnvironmentService.recreate_environment(self.env)
        self.assertTrue(ok)
        create.assert_called_once()
        self.assertEqual(create.call_args.args[1], "w-env")

    def test_the_same_python_version_is_preferred_when_the_host_has_it(self):
        found = [{"path": "/opt/py399/bin/python", "version": "3.99.1", "display": "Python 3.99.1"}]
        with mock.patch.object(EnvironmentService, "discover_python_versions", return_value=found), \
                mock.patch.object(EnvironmentService, "create_environment", return_value=(True, "ok")) as create:
            EnvironmentService.recreate_environment(self.env)
        self.assertEqual(create.call_args.args, ("/opt/py399/bin/python", "w-env"))

    def test_a_healthy_venv_is_left_alone(self):
        _make_python(self.env)
        with mock.patch.object(EnvironmentService, "create_environment") as create:
            ok, message = EnvironmentService.recreate_environment(self.env)
        self.assertTrue(ok)
        self.assertIn("already exists", message)
        create.assert_not_called()

    def test_a_folder_without_an_interpreter_is_replaced(self):
        folder = Path(self.env.get_full_path())
        folder.mkdir(parents=True)
        (folder / "leftover.txt").write_text("x")
        with mock.patch.object(EnvironmentService, "discover_python_versions", return_value=[]), \
                mock.patch.object(EnvironmentService, "create_environment", return_value=(True, "ok")) as create:
            ok, _ = EnvironmentService.recreate_environment(self.env)
        self.assertTrue(ok)
        self.assertFalse(folder.exists())  # removed, so create_environment can build it again
        create.assert_called_once()

    def test_missing_requirements_compares_names_and_pins(self):
        self.env.requirements = "humanize==4.16.0\nSix==1.17.0\nrequests>=2\n# comment\n\nzope.interface==7.0\n"
        installed = [
            {"name": "humanize", "version": "4.16.0"},
            {"name": "six", "version": "1.16.0"},  # other version → missing
            {"name": "zope-interface", "version": "7.0"},  # normalized name
        ]
        with mock.patch.object(EnvironmentService, "get_installed_packages", return_value=installed):
            self.assertEqual(EnvironmentService.missing_requirements(self.env), ["Six==1.17.0", "requests>=2"])


@override_settings(ENVIRONMENTS_ROOT=str(_TMP / "envs-task"))
class RebuildEnvironmentsTaskTests(TestCase):
    def setUp(self):
        shutil.rmtree(_TMP / "envs-task", ignore_errors=True)
        (_TMP / "envs-task").mkdir(parents=True)

    def test_missing_venv_is_recreated_and_saved_packages_queued(self):
        env = Environment.objects.create(name="gone", path="gone", requirements="humanize==4.16.0\n")

        def fake_create(python, path):
            _make_python(env)
            return True, "created"

        with mock.patch.object(EnvironmentService, "discover_python_versions", return_value=[]), \
                mock.patch.object(EnvironmentService, "create_environment", side_effect=fake_create), \
                mock.patch.object(EnvironmentService, "get_installed_packages", return_value=[]), \
                mock.patch("django_q.tasks.async_task", return_value="tid") as queued:
            report = rebuild_environments_task()
        self.assertEqual(report["environments"], [{"environment": "gone", "recreated": True, "install_queued": True}])
        op = PackageOperation.objects.get(environment=env)
        self.assertEqual(op.operation, PackageOperation.Operation.BULK_INSTALL)
        self.assertEqual(op.package_spec, "humanize==4.16.0\n")
        self.assertEqual(queued.call_args.args[:2], ("core.tasks.execute_package_operation", str(op.id)))

    def test_a_complete_environment_is_untouched(self):
        env = Environment.objects.create(name="fine", path="fine", requirements="humanize==4.16.0\n")
        _make_python(env)
        with mock.patch.object(EnvironmentService, "create_environment") as create, \
                mock.patch.object(EnvironmentService, "get_installed_packages",
                                  return_value=[{"name": "humanize", "version": "4.16.0"}]), \
                mock.patch("django_q.tasks.async_task") as queued:
            report = rebuild_environments_task()
        self.assertEqual(report["environments"], [{"environment": "fine", "recreated": False, "install_queued": False}])
        create.assert_not_called()
        queued.assert_not_called()
        self.assertFalse(PackageOperation.objects.exists())

    def test_a_failed_recreate_is_reported_and_the_rest_continue(self):
        Environment.objects.create(name="a-broken", path="a-broken", requirements="x==1\n")
        ok_env = Environment.objects.create(name="b-ok", path="b-ok")
        _make_python(ok_env)
        with mock.patch.object(EnvironmentService, "recreate_environment", return_value=(False, "no python")), \
                mock.patch("django_q.tasks.async_task"):
            report = rebuild_environments_task()
        self.assertEqual(report["environments"][0], {"environment": "a-broken", "recreated": False,
                                                     "install_queued": False, "error": "no python"})
        self.assertEqual(report["environments"][1]["environment"], "b-ok")


@override_settings(ENVIRONMENTS_ROOT=str(_TMP / "envs-view"), BASE_DIR=_TMP / "base")
class RestoreQueuesTheRebuildTests(TestCase):
    def setUp(self):
        _setup_wizard_off(self)
        self.admin = User.objects.create(email="admin@example.com", is_superuser=True)
        self.member = User.objects.create(email="member@example.com")

    def test_console_restore_queues_the_rebuild_and_says_so(self):
        data = BackupService.create_backup(include_runs=False)
        self.client.force_login(self.admin)
        session = self.client.session
        session["pending_backup"] = data
        session.save()
        with mock.patch("core.tasks.async_task", return_value="tid") as queued:
            resp = self.client.post(reverse("cpanel:backup_restore"), {"confirm_delete": "on"})
        self.assertEqual(resp.status_code, 302)
        queued.assert_called_once_with("core.tasks.rebuild_environments_task", task_name="restore-rebuild-environments")
        texts = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertTrue(any("rebuilt in the background" in t for t in texts), texts)
        self.assertFalse(any("from the Environments page" in t for t in texts), texts)

    def test_cli_restore_queues_the_rebuild(self):
        import io
        import json

        data = BackupService.create_backup(include_runs=False)
        path = _TMP / "cli-backup.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        out = io.StringIO()
        with mock.patch("core.tasks.async_task", return_value="tid") as queued:
            call_command("restore_backup", str(path), "--yes", "--no-runs", stdout=out)
        queued.assert_called_once_with("core.tasks.rebuild_environments_task", task_name="restore-rebuild-environments")
        self.assertIn("Environment rebuild queued", out.getvalue())

    def test_recreate_button_rebuilds_a_missing_environment(self):
        env = Environment.objects.create(name="restored", path="restored", requirements="humanize==4.16.0\n")
        self.client.force_login(self.admin)
        page = self.client.get(reverse("cpanel:environment_detail", args=[env.pk])).content.decode()
        self.assertIn("folder is missing on disk", page)
        self.assertIn(reverse("cpanel:environment_recreate", args=[env.pk]), page)
        op = PackageOperation(environment=env)
        with mock.patch.object(EnvironmentService, "recreate_environment", return_value=(True, "ok")) as rec, \
                mock.patch.object(EnvironmentService, "queue_requirements_install", return_value=op) as inst:
            resp = self.client.post(reverse("cpanel:environment_recreate", args=[env.pk]))
        self.assertRedirects(resp, reverse("cpanel:environment_packages", args=[env.pk]), fetch_redirect_response=False)
        rec.assert_called_once()
        inst.assert_called_once()
        self.assertTrue(any("Reinstalling its saved packages" in str(m) for m in get_messages(resp.wsgi_request)))

    def test_recreate_is_superuser_only_and_a_no_op_for_a_healthy_env(self):
        env = Environment.objects.create(name="healthy", path="healthy")
        _make_python(env)
        self.client.force_login(self.member)
        with mock.patch.object(EnvironmentService, "recreate_environment") as rec:
            resp = self.client.post(reverse("cpanel:environment_recreate", args=[env.pk]))
        self.assertNotEqual(resp.status_code, 200)
        rec.assert_not_called()
        self.client.force_login(self.admin)
        page = self.client.get(reverse("cpanel:environment_detail", args=[env.pk])).content.decode()
        self.assertNotIn("folder is missing on disk", page)
        with mock.patch.object(EnvironmentService, "recreate_environment") as rec:
            resp = self.client.post(reverse("cpanel:environment_recreate", args=[env.pk]))
        rec.assert_not_called()
        self.assertTrue(any("nothing to recreate" in str(m) for m in get_messages(resp.wsgi_request)))
        self.assertTrue(os.path.isfile(env.get_python_executable()))
