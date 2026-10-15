"""
Tasks page ↔ django-q2 queue: entries are matched by their payload task id.

Regression for the 2.0 release test (§4.04, F31): ``TaskService`` treated
``OrmQ.key`` as the task id, but django-q2's ORM broker stores the *queue name*
(``Conf.PREFIX``) there — every row shares it. So the Tasks page listed every
queued task as the queue name with no linked run, and **Cancel** on any one of
them deleted every queue entry while leaving every Run "pending".
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from django_q.conf import Conf
from django_q.models import OrmQ
from django_q.signing import SignedPackage

from core.models import Environment, Run, Script, Workspace
from core.services.task_service import TaskService


def _enqueue(task_id, *, lock=None, name=None):
    """A queue row exactly as django-q2's ORM broker writes it (key = queue name)."""
    payload = SignedPackage.dumps({
        "id": task_id,
        "name": name or f"run-{task_id}",
        "func": "core.tasks.execute_run_task",
        "args": (),
        "kwargs": {},
    })
    return OrmQ.objects.create(key=Conf.PREFIX, payload=payload, lock=lock or timezone.now())


class QueuedTaskIdTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()
        env = Environment.objects.create(name="qenv", path="qenv")
        self.script = Script.objects.create(
            name="queued_script", code="print(1)", environment=env,
            workspace=self.ws, is_enabled=True,
        )
        self.run1 = Run.objects.create(
            script=self.script, workspace=self.ws, status=Run.Status.PENDING, task_id="task-one"
        )
        self.run2 = Run.objects.create(
            script=self.script, workspace=self.ws, status=Run.Status.PENDING, task_id="task-two"
        )
        _enqueue("task-one")
        _enqueue("task-two")

    def _queued_ids(self):
        return {tid for _q, _p, tid in TaskService._queued_entries()}

    def test_queued_tasks_listed_by_payload_id_with_linked_runs(self):
        tasks = {t["id"]: t for t in TaskService.get_queued_tasks(workspace=self.ws)}
        self.assertEqual(set(tasks), {"task-one", "task-two"})
        self.assertNotIn(Conf.PREFIX, tasks)
        self.assertEqual(tasks["task-one"]["linked_run"], self.run1)
        self.assertEqual(tasks["task-one"]["type"], "script_run")

    def test_cancel_removes_only_that_task_and_cancels_its_run(self):
        ok, _msg = TaskService.cancel_queued_task("task-one", workspace=self.ws)
        self.assertTrue(ok)
        self.assertEqual(self._queued_ids(), {"task-two"})
        self.run1.refresh_from_db()
        self.run2.refresh_from_db()
        self.assertEqual(self.run1.status, Run.Status.CANCELLED)
        self.assertEqual(self.run2.status, Run.Status.PENDING)

    def test_cancel_by_queue_name_deletes_nothing(self):
        ok, msg = TaskService.cancel_queued_task(Conf.PREFIX, workspace=self.ws)
        self.assertFalse(ok)
        self.assertEqual(msg, "Task not found in queue")
        self.assertEqual(OrmQ.objects.count(), 2)

    def test_force_stop_pending_run_drops_only_its_entry(self):
        ok, _msg = TaskService.force_stop_task("task-two", workspace=self.ws)
        self.assertTrue(ok)
        self.assertEqual(self._queued_ids(), {"task-one"})
        self.run2.refresh_from_db()
        self.assertEqual(self.run2.status, Run.Status.CANCELLED)

    def test_task_detail_finds_queued_entry(self):
        detail = TaskService.get_task_detail("task-two", workspace=self.ws)
        self.assertEqual(detail["state"], "queued")
        self.assertEqual(detail["linked_run"], self.run2)

    def test_stale_queued_entry_reported_by_payload_id(self):
        OrmQ.objects.all().delete()
        _enqueue("task-one", lock=timezone.now() - timedelta(minutes=10))
        stuck = TaskService.get_stuck_tasks(workspace=self.ws)
        self.assertEqual([s["id"] for s in stuck], ["task-one"])
        self.assertEqual(stuck[0]["linked_run"], self.run1)

    def test_running_task_row_is_not_queued_stuck_or_cancellable(self):
        # django-q keeps the row until the task finishes; a started run's entry
        # must not show as queued/stuck, and Cancel must not flag it cancelled
        # while its process keeps running.
        OrmQ.objects.all().delete()
        _enqueue("task-one", lock=timezone.now() - timedelta(minutes=10))
        _enqueue("task-two")
        Run.objects.filter(pk=self.run1.pk).update(
            status=Run.Status.RUNNING, started_at=timezone.now() - timedelta(minutes=10)
        )
        self.assertEqual([t["id"] for t in TaskService.get_queued_tasks(workspace=self.ws)], ["task-two"])
        self.assertEqual(TaskService.get_task_statistics(workspace=self.ws)["queued_count"], 1)
        self.assertNotIn("task-one", [s["id"] for s in TaskService.get_stuck_tasks(workspace=self.ws)])
        ok, msg = TaskService.cancel_queued_task("task-one", workspace=self.ws)
        self.assertFalse(ok)
        self.assertIn("already running", msg)
        self.run1.refresh_from_db()
        self.assertEqual(self.run1.status, Run.Status.RUNNING)
        self.assertEqual(self._queued_ids(), {"task-one", "task-two"})

    def test_queued_count_hides_other_workspace_entries(self):
        other = Workspace.objects.create(name="other")
        Run.objects.create(
            script=self.script, workspace=other, status=Run.Status.PENDING, task_id="task-other"
        )
        _enqueue("task-other")
        self.assertEqual(TaskService.get_task_statistics(workspace=self.ws)["queued_count"], 2)
        self.assertEqual(TaskService.get_task_statistics()["queued_count"], 3)
