"""
Logs → Clear (2.0 release test §20.01, F68).

``LogService.clear_logs`` deleted ``pyrunner.log``. Every PyRun process keeps
that file open through its logging handler, so after a clear the web and task
workers went on writing to the deleted file: the Logs page stayed empty until
the container restarted and the disk space was never freed. The current file
is now truncated in place; rotated files are removed.
"""

import json
import logging
import shutil
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from core.services.log_service import LogService

_TMP = Path(tempfile.mkdtemp(prefix="pyrun-logclear-"))


def _line(message):
    return json.dumps({"timestamp": "2026-10-16T13:00:00", "level": "INFO", "logger": "t", "message": message})


@override_settings(LOGS_DIR=_TMP)
class ClearLogsTests(SimpleTestCase):
    def setUp(self):
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP.mkdir(parents=True)
        # a handler holding the file open, as every PyRun process does
        self.handler = logging.FileHandler(_TMP / "pyrunner.log", encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.log = logging.getLogger("pyrun.test.logclear")
        self.log.propagate = False
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.INFO)
        self.addCleanup(self._close)

    def _close(self):
        self.log.removeHandler(self.handler)
        self.handler.close()

    def test_lines_logged_after_a_clear_are_shown(self):
        for i in range(3):
            self.log.info(_line(f"before {i}"))
        (_TMP / "pyrunner.log.1").write_text(_line("rotated") + "\n", encoding="utf-8")

        freed = LogService.clear_logs()

        self.assertGreater(freed, 0)
        self.assertFalse((_TMP / "pyrunner.log.1").exists())
        self.assertTrue((_TMP / "pyrunner.log").exists())
        self.log.info(_line("after the clear"))
        entries, total = LogService.read_logs(limit=10, offset=0)
        self.assertEqual(total, 1)
        self.assertEqual([e.message for e in entries], ["after the clear"])
