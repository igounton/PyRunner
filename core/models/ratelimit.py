"""
RateLimitCounter — the shared fixed-window hit counter behind core.ratelimit.

Used when the default cache is Django's DatabaseCache (PyRun's default). That
backend's ``incr()`` is a ``get()`` followed by a ``set()``, so concurrent
requests read the same value and overwrite each other's increment: a parallel
burst of 70 requests against a 60/min limit was counted as 30 and all 70 got
through. Here every hit is one ``UPDATE … SET count = count + 1`` inside a
transaction, which the database serialises per row on SQLite and PostgreSQL.
"""

from django.db import models


class RateLimitCounter(models.Model):
    """Hits counted against one rate-limit key in one fixed time window."""

    # "<key>:<window bucket>", the same string the cache path uses as its key.
    bucket = models.CharField(max_length=255, unique=True)
    count = models.PositiveIntegerField(default=0)
    # Garbage collection only: rows past this are deleted when a new window
    # opens. The window itself is the bucket number inside ``bucket``.
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "rate_limit_counters"

    def __str__(self):
        return f"{self.bucket} = {self.count}"
