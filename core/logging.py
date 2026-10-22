"""
Custom logging formatters for PyRun.
"""
import json
import logging
import logging.handlers
import os
from datetime import datetime

try:  # POSIX only; the image is Linux
    import fcntl
except ImportError:  # pragma: no cover - Windows dev box
    fcntl = None


class JsonFormatter(logging.Formatter):
    """
    Format log records as JSON for easier parsing.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry)


class MultiProcessRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """``RotatingFileHandler`` for one log file shared by several processes.

    Gunicorn's workers and the django-q cluster each hold their own handler on
    ``pyrunner.log``. With the stock handler the first process to reach
    ``maxBytes`` renames the file away while the others keep writing to the
    renamed file (``pyrunner.log.1``, then ``.2`` after their own rollover), so
    the Logs page, which reads ``pyrunner.log``, misses their lines, the next
    rollovers delete files still in use, and concurrent renames race
    (``FileNotFoundError`` in ``doRollover``, release test F114).

    This handler:

    * reopens ``pyrunner.log`` before writing whenever the file on disk is no
      longer the one it holds open (another process rotated it), so every
      process follows the current file;
    * rotates under an exclusive lock file (POSIX ``flock``) and only when the
      current file on disk is still over the limit, so one process rotates and
      the others just reopen.
    """

    def _lock_path(self) -> str:
        directory, name = os.path.split(self.baseFilename)
        # dot-prefixed: LogService globs ``pyrunner.log*`` and must not see it
        return os.path.join(directory, f".{name}.lock")

    def _reopen_if_moved(self) -> None:
        if self.stream is None:
            return
        try:
            on_disk = os.stat(self.baseFilename)
        except FileNotFoundError:
            on_disk = None
        held = os.fstat(self.stream.fileno())
        if on_disk is None or (on_disk.st_dev, on_disk.st_ino) != (held.st_dev, held.st_ino):
            self.stream.close()
            self.stream = self._open()

    def shouldRollover(self, record) -> bool:
        self._reopen_if_moved()
        return super().shouldRollover(record)

    def doRollover(self) -> None:
        if fcntl is None:  # Windows dev box: single-process behaviour
            super().doRollover()
            return
        with open(self._lock_path(), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    size = os.stat(self.baseFilename).st_size
                except FileNotFoundError:
                    size = 0
                if size < self.maxBytes:
                    # another process rotated (or Logs → Clear truncated the
                    # file) while we waited: follow the current file
                    if self.stream is not None:
                        self.stream.close()
                    self.stream = self._open()
                    return
                super().doRollover()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
