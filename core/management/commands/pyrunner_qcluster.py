"""
Start the django-q cluster with PyRun's DB-backed Workers settings applied.

This wraps django-q2's ``qcluster`` command because the values saved on
Settings → Workers cannot take effect any other way: Q_CLUSTER is computed at
settings-module import, inside ``django.setup()``, before the app registry —
and thus the database — exists, in every process. By the time a management
command runs, apps are ready, so this command reads the GlobalSettings row
(plus the fleet's largest script timeout, for the retry safety floor), patches
``django_q.conf.Conf``, and only then starts the cluster.

A plain ``manage.py qcluster`` still works but runs on the env defaults only —
production (entrypoint.sh) and the restart-workers flow use this command.

``--reconcile-orphans`` (passed by entrypoint.sh, the all-in-one container)
first fails RUNNING runs whose script process is gone — see
``Run.reconcile_orphans_at_worker_start``. Workers started separately (split
worker containers) leave the flag off and keep the deadline rule.
"""

import os

import psutil
from django_q.management.commands.qcluster import Command as QClusterCommand

from core.services.worker_config import apply_db_worker_settings


class Command(QClusterCommand):
    help = (
        "Starts a Django Q Cluster with PyRun's DB-backed worker settings "
        "(Settings → Workers) applied."
    )

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--reconcile-orphans",
            action="store_true",
            help="Before starting, fail runs still marked running whose script "
            "process no longer exists in this container. Only for a worker that "
            "shares its process namespace with every run it started (the "
            "standard single container).",
        )

    def handle(self, *args, **options):
        if options.get("reconcile_orphans"):
            self._reconcile_orphans()
        try:
            config = apply_db_worker_settings()
            self.stdout.write(
                "Worker settings applied: workers={workers} timeout={timeout} "
                "retry={retry} queue_limit={queue_limit}".format(**config)
            )
        except Exception as e:
            # A fresh install (migrations not run yet) must still boot a
            # cluster; env defaults are the safe fallback and the next restart
            # picks the DB values up.
            self.stderr.write(
                f"Could not apply DB worker settings ({e}); "
                "starting with environment defaults."
            )
        super().handle(*args, **options)

    def _reconcile_orphans(self):
        """Fail runs orphaned by a container or worker restart. Never fatal."""
        from core.models import Run

        try:
            started = psutil.Process(os.getpid()).create_time()
            count = Run.reconcile_orphans_at_worker_start(started)
            if count:
                self.stdout.write(
                    f"Marked {count} run(s) failed: they were running when the "
                    "worker stopped and their process is gone."
                )
        except Exception as e:
            self.stderr.write(f"Could not reconcile orphaned runs ({e}); continuing.")
