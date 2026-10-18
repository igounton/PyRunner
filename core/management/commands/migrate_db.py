"""
Copy this PyRun instance's database to PostgreSQL (SQLite -> Postgres).

A guided, maintenance-window operation. Every table is copied row for row with
its original primary keys: users and their passwords, workspaces and members,
API tokens, AI providers, channels, plugin records, full library and brain
history, schedules and the task queue. (Plugins never have tables of their own:
the plugin doctor refuses models and migrations.) Nothing is re-keyed or
re-provisioned: encrypted values stay readable because the instance keeps its
ENCRYPTION_KEY, and data-server databases, vector stores and environments on
the volume are left as they are.

    manage.py migrate_db --to postgres://user:pass@host:5432/db --dry-run
    manage.py migrate_db --to postgres://user:pass@host:5432/db --yes

Steps: dump every table (this process, the source) -> ``migrate`` the target ->
replace the target's rows with the dump (a subprocess with DATABASE_URL at the
target and plugins off) -> compare every model's row count with the source.

Everything already in the target database is replaced. Sessions are not copied,
so everyone signs in again. Stop writes first (a maintenance window) and take a
backup; after success, set DATABASE_URL to the target and restart.

Before 2.0 this ran through backup/restore. A backup is portable by design, so
it leaves out users, API tokens, AI providers, channels and plugin rows, and a
migrated instance came up with no admin account (F98).
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core import serializers
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers import json as json_serializer

# Not copied: sessions (everyone signs in again).
_EXCLUDE_APPS = ["sessions"]
_COUNTS_PREFIX = "MIGRATE_DB_COUNTS="
_LOAD_ENV = "PYRUNNER_MIGRATE_DB_LOAD"
# Dump format: Django's JSON with full-precision times (registered on use).
_FORMAT = "pyrunjson"

# The familiar subset printed before the copy (every model is verified after).
_SUMMARY = [
    ("Users", "core.user"),
    ("Workspaces", "core.workspace"),
    ("Scripts", "core.script"),
    ("Runs", "core.run"),
    ("Schedules", "core.scriptschedule"),
    ("Secrets", "core.secret"),
    ("Data stores", "core.datastore"),
    ("Data store entries", "core.datastoreentry"),
    ("Library revisions", "core.libraryrevision"),
    ("Brains", "core.brain"),
    ("API tokens", "core.apitoken"),
    ("AI providers", "core.aiprovider"),
    ("Channels", "core.channel"),
    ("Plugins", "core.plugin"),
]


class Serializer(json_serializer.Serializer):
    """Django's JSON serializer, but times keep their microseconds.

    DjangoJSONEncoder rounds datetimes to milliseconds; the copy must be exact.
    """

    def handle_field(self, obj, field):
        value = field.value_from_object(obj)
        if isinstance(value, (datetime.datetime, datetime.time)):
            self._current[field.name] = value.isoformat()
        else:
            super().handle_field(obj, field)


Deserializer = json_serializer.Deserializer


def _register_format():
    serializers.register_serializer(_FORMAT, __name__)


def _models():
    """Concrete models of every app except sessions.

    Includes auto-created many-to-many tables so their rows are verified too.
    Proxy models share their parent's table and are skipped, as dumpdata does.
    """
    return [
        m
        for c in apps.get_app_configs()
        if c.label not in _EXCLUDE_APPS
        for m in c.get_models(include_auto_created=True)
        if not m._meta.proxy and m._meta.managed
    ]


def _counts(models) -> dict:
    return {m._meta.label_lower: m._base_manager.count() for m in models}


class Command(BaseCommand):
    help = "Copy this instance's database (every table, keys kept) into a PostgreSQL database."

    def add_arguments(self, parser):
        parser.add_argument("--to", help="Target DATABASE_URL (postgres://...)")
        parser.add_argument("--dry-run", action="store_true", help="Report only; make no changes")
        parser.add_argument("--yes", action="store_true", help="Confirm the real migration")
        # Internal: the load step, run in a subprocess whose DATABASE_URL is the target.
        parser.add_argument("--load", help=argparse.SUPPRESS)

    def handle(self, *args, **opts):
        if opts["load"]:
            # The load step empties the database it runs against; only the
            # parent migrate_db, which points it at the target, may start it.
            if os.environ.get(_LOAD_ENV) != "1":
                raise CommandError("--load is internal to migrate_db.")
            self._load(opts["load"])
            return

        target = opts["to"] or ""
        if not target.startswith(("postgres://", "postgresql://")):
            raise CommandError("--to must be a postgres:// URL")

        self._check_target_reachable(target)
        source = _counts(_models())

        self.stdout.write("Source row counts:")
        for name, label in _SUMMARY:
            self.stdout.write(f"  {name}: {source.get(label, 0)}")
        self.stdout.write(
            f"  All tables: {len(source)} tables, {sum(source.values())} rows"
            " (sessions are not copied)"
        )

        if opts["dry_run"]:
            self.stdout.write(self.style.SUCCESS("Dry run complete — no changes made."))
            return

        if not opts["yes"]:
            raise CommandError(
                "This REPLACES everything in the target database. Stop writes "
                "(maintenance window) and take a backup first, then re-run with --yes."
            )

        self.copy(target)
        self.stdout.write(self.style.SUCCESS("Migration complete and verified."))
        self.stdout.write(
            "Next: set DATABASE_URL to the target, then restart PyRun so it "
            "runs on Postgres. Everyone signs in again (sessions are not copied)."
        )

    # --- the copy (also called directly by the tests) ---------------------

    def copy(self, target: str) -> dict:
        """Copy every table into ``target``; return {model: (source, target)} rows.

        Raises CommandError if any model's row count differs afterwards.
        """
        checks = {}
        with tempfile.TemporaryDirectory(prefix="pyrun-migrate-") as tmp:
            dump_file = str(Path(tmp) / f"dump.{_FORMAT}.gz")
            self.stdout.write("Dumping every table (keys kept)...")
            source = _counts(_models())
            _register_format()
            call_command(
                "dumpdata",
                format=_FORMAT,
                exclude=_EXCLUDE_APPS,
                use_base_manager=True,
                output=dump_file,
                verbosity=0,
            )

            self.stdout.write("Creating schema on target (migrate)...")
            self._subprocess(["migrate", "--noinput"], target)

            self.stdout.write("Loading rows into target...")
            loaded = self._subprocess(["migrate_db", "--load", dump_file], target)
            for label, rows in source.items():
                checks[label] = (rows, loaded.get(label, -1))

        self.stdout.write("Verifying row counts...")
        mismatches = {k: v for k, v in checks.items() if v[0] != v[1]}
        for name, label in _SUMMARY:
            src, tgt = checks.get(label, (0, 0))
            flag = "OK" if src == tgt else "MISMATCH"
            self.stdout.write(f"  {name}: source={src} target={tgt} [{flag}]")
        self.stdout.write(
            f"  All tables: {len(checks)} checked, {len(mismatches)} mismatched"
        )
        if mismatches:
            raise CommandError(f"Row-count mismatch after migration: {mismatches}")
        return checks

    # --- helpers ---------------------------------------------------------

    def _load(self, path: str) -> None:
        """Subprocess side: replace the target's rows with the dump, print counts."""
        # Empty every table, content types and permissions included, so the
        # source's rows load with their own keys.
        call_command(
            "flush", interactive=False, inhibit_post_migrate=True, allow_cascade=True, verbosity=0
        )
        _register_format()
        call_command("loaddata", path, verbosity=0)
        self.stdout.write(_COUNTS_PREFIX + json.dumps(_counts(_models())))

    def _check_target_reachable(self, target: str) -> None:
        try:
            import psycopg

            conn = psycopg.connect(target, connect_timeout=10)
            conn.close()
        except Exception as e:
            raise CommandError(f"Cannot connect to target database: {e}")

    def _subprocess(self, args: list, target: str) -> dict:
        """Run ``manage.py <args>`` against the target; return its printed counts."""
        env = dict(os.environ)
        env["DATABASE_URL"] = target
        # Mirror the entrypoint: migrations never need plugins, and a broken
        # active plugin must not derail the migration.
        env["PYRUNNER_DISABLE_PLUGINS"] = "1"
        env[_LOAD_ENV] = "1"
        cmd = [sys.executable, str(Path(settings.BASE_DIR) / "manage.py")] + args
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            raise CommandError(
                f"Step `{' '.join(args[:2])}` failed:\n{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
            )
        for line in result.stdout.splitlines():
            if line.startswith(_COUNTS_PREFIX):
                return json.loads(line[len(_COUNTS_PREFIX):])
        return {}
