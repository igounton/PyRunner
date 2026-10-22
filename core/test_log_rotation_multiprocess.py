"""
Log rotation with several processes on one file (2.0 release test P6.05, F114).

Gunicorn's workers and the django-q cluster each hold a handler on
``pyrunner.log``. With the stock ``RotatingFileHandler`` the first process to
reach ``maxBytes`` renamed the file away while the others went on writing to
the renamed file, so the Logs page (which reads ``pyrunner.log``) lost their
lines, later rollovers deleted files still in use, and concurrent renames
raised ``FileNotFoundError`` in ``doRollover`` (seen once in the 24 h soak).
``core.logging.MultiProcessRotatingFileHandler`` follows the current file and
rotates under a lock. The file-system tests need POSIX rename semantics and
``fork``; they run inside the Linux image.
"""

import logging
import multiprocessing
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from core.logging import MultiProcessRotatingFileHandler

POSIX = os.name == "posix" and sys.platform != "darwin"


def _handler(path, max_bytes, backups):
    h = MultiProcessRotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


def _record(msg):
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)


def _child(path, tag, n, max_bytes, backups):  # pragma: no cover - runs in a forked child
    h = _handler(path, max_bytes, backups)
    errors = []
    h.handleError = lambda record: errors.append(sys.exc_info()[1])
    for i in range(n):
        h.emit(_record(f"{tag}-{i:05d} " + "x" * 60))
    h.close()
    os._exit(1 if errors else 0)


class LoggingSettingsTests(SimpleTestCase):
    def test_file_handler_is_multiprocess_safe(self):
        self.assertEqual(
            settings.LOGGING["handlers"]["file"]["class"],
            "core.logging.MultiProcessRotatingFileHandler",
        )

    def test_lock_file_is_hidden_from_the_logs_page_glob(self):
        h = MultiProcessRotatingFileHandler(os.path.join(tempfile.gettempdir(), "pyrunner.log"), delay=True)
        self.assertEqual(os.path.basename(h._lock_path()), ".pyrunner.log.lock")
        self.assertFalse(Path(h._lock_path()).match("pyrunner.log*"))


@unittest.skipUnless(POSIX, "needs POSIX rename-while-open and fork (runs in the Linux image)")
class MultiProcessRotationTests(SimpleTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pyrun-logrot-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = str(self.dir / "pyrunner.log")

    def test_every_handler_follows_the_current_file_after_a_rotation(self):
        a, b = _handler(self.path, 2000, 5), _handler(self.path, 2000, 5)
        errors = []
        for h in (a, b):
            h.handleError = lambda record: errors.append(sys.exc_info()[1])
            self.addCleanup(h.close)
        for i in range(120):
            a.emit(_record(f"a-{i:03d} " + "x" * 40))
            b.emit(_record(f"b-{i:03d} " + "x" * 40))
        self.assertEqual(errors, [])
        current = Path(self.path).read_text(encoding="utf-8").splitlines()
        # the newest lines of both "processes" are in pyrunner.log, which the Logs page reads
        self.assertIn("a-119 " + "x" * 40, current)
        self.assertIn("b-119 " + "x" * 40, current)
        self.assertLessEqual(len(list(self.dir.glob("pyrunner.log*"))), 6)

    def test_concurrent_processes_lose_no_line_and_log_no_error(self):
        ctx = multiprocessing.get_context("fork")
        n = 1500
        procs = [ctx.Process(target=_child, args=(self.path, tag, n, 20_000, 500)) for tag in ("p1", "p2", "p3")]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        self.assertEqual([p.exitcode for p in procs], [0, 0, 0])
        lines = []
        for f in self.dir.glob("pyrunner.log*"):
            lines += f.read_text(encoding="utf-8").splitlines()
        self.assertGreater(len(list(self.dir.glob("pyrunner.log.*"))), 5)  # it did rotate
        self.assertEqual(len(lines), 3 * n)
        self.assertEqual(len(set(lines)), 3 * n)
