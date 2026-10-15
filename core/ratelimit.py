"""
Shared fixed-window rate limiting.

One helper for every rate-limit site so the pattern can't drift. The previous
per-site pattern — ``cache.set(key, count + 1, WINDOW)`` — had two bugs:

- ``set()`` re-arms the TTL on every hit, so steady traffic NEVER expires the
  window: a legitimate poller below the limit accumulates across windows and
  eventually starves itself with 429s until a fully quiet window passes.
- The read-modify-write races across gunicorn workers (undercounts bursts).

The window is identified by a time bucket IN THE KEY, not by the cache TTL —
this matters because Django's BaseCache.incr (used by the DatabaseCache
default) is itself a get()+set() that re-arms the TTL, so any TTL-based fixed
window silently turns sliding on that backend. With bucketed keys, a new
window is a new key and nothing a backend does to the old key's TTL can
stretch the budget. The TTL on the bucket key is garbage collection only.

That same get()+set() also LOSES hits under concurrency: requests in parallel
read one value and each write value+1. Measured on the 2.0 release test, 70
parallel API calls against a 60/min token limit were counted as 30 and all 70
passed. So when the default cache is DatabaseCache (PyRun's default), the
counter lives in ``RateLimitCounter`` rows incremented by one UPDATE inside a
transaction — serialised by the database on SQLite and PostgreSQL. Backends
whose ``incr()`` is atomic (Redis, LocMem) keep using the cache.
"""

import hashlib
import ipaddress
import logging
import time
from datetime import timedelta

from django.core.cache import cache, caches
from django.core.cache.backends.db import DatabaseCache
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)


def _use_db_counter() -> bool:
    """True when the default cache can't increment atomically (DatabaseCache)."""
    return isinstance(caches["default"], DatabaseCache)


def _db_bucket(bucket_key: str) -> str:
    """The row key for ``bucket_key``; over-long keys are hashed to fit the column."""
    if len(bucket_key) <= 255:
        return bucket_key
    return "sha256:" + hashlib.sha256(bucket_key.encode("utf-8")).hexdigest()


def _db_count_hit(bucket_key: str, ttl_seconds: int) -> int:
    """Add one hit to ``bucket_key``'s row and return the new total.

    The UPDATE and the read run in one transaction: the row stays locked
    (PostgreSQL) or the database write-locked (SQLite) until commit, so the
    value read back is exactly this hit's position in the window.
    """
    from core.models import RateLimitCounter

    bucket_key = _db_bucket(bucket_key)
    rows = RateLimitCounter.objects.filter(bucket=bucket_key)
    with transaction.atomic():
        if rows.update(count=F("count") + 1):
            return rows.values_list("count", flat=True).first() or 1
    now = timezone.now()
    try:
        with transaction.atomic():
            RateLimitCounter.objects.create(
                bucket=bucket_key, count=1, expires_at=now + timedelta(seconds=ttl_seconds)
            )
    except IntegrityError:
        # Another request opened this window first: count on its row.
        with transaction.atomic():
            rows.update(count=F("count") + 1)
            return rows.values_list("count", flat=True).first() or 1
    # A new window opened: drop finished windows (cheap, indexed).
    RateLimitCounter.objects.filter(expires_at__lt=now).delete()
    return 1


def _cache_count_hit(bucket_key: str, ttl_seconds: int) -> int:
    cache.add(bucket_key, 0, ttl_seconds)  # TTL = cleanup only
    try:
        return cache.incr(bucket_key)
    except ValueError:
        # The key vanished between add() and incr() (cull race). Count this
        # hit on a fresh counter rather than dropping the brake entirely.
        cache.set(bucket_key, 1, ttl_seconds)
        return 1


def rate_limit_exceeded(key: str, limit: int, window_seconds: int) -> bool:
    """Count one hit against ``key``; True when the fixed window is over budget.

    Allows exactly ``limit`` hits per window (windows are aligned to epoch
    multiples of ``window_seconds``), also when the hits arrive in parallel;
    further hits return True until the next window starts. Rejected hits cannot
    extend the window.

    Fail-open by design: these limits are abuse brakes in front of endpoints
    that carry their own auth (tokens, signatures) — a misbehaving counter
    store must not take webhooks down with it.
    """
    bucket = int(time.time() // window_seconds)
    bucket_key = f"{key}:{bucket}"
    ttl = window_seconds + 60
    try:
        if _use_db_counter():
            count = _db_count_hit(bucket_key, ttl)
        else:
            count = _cache_count_hit(bucket_key, ttl)
    except DatabaseError:
        logger.warning("Rate-limit counter unavailable for %r; allowing the request", key, exc_info=True)
        return False
    return count > limit


def rate_limit_over(key: str, limit: int, window_seconds: int) -> bool:
    """Peek: is ``key`` already over budget this window, WITHOUT counting a hit?

    Gates expensive work behind a budget that only specific events consume —
    the plugin API checks this before its token DB lookup so an IP that burned
    its auth-fail budget (counted via ``rate_limit_exceeded`` on each failure)
    stops costing a query per guess. Same fail-open stance as the counter.
    """
    bucket = int(time.time() // window_seconds)
    bucket_key = f"{key}:{bucket}"
    try:
        if _use_db_counter():
            from core.models import RateLimitCounter

            count = (
                RateLimitCounter.objects.filter(bucket=_db_bucket(bucket_key))
                .values_list("count", flat=True)
                .first()
                or 0
            )
        else:
            count = cache.get(bucket_key) or 0
    except DatabaseError:
        logger.warning("Rate-limit counter unavailable for %r; allowing the request", key, exc_info=True)
        return False
    return count > limit


def seconds_to_window_reset(window_seconds: int) -> int:
    """Seconds until the current fixed window rolls over (for ``Retry-After``)."""
    return max(1, window_seconds - int(time.time()) % window_seconds)


def client_ip(request) -> str:
    """Best-effort client IP for per-IP rate-limit keys.

    Default: ``REMOTE_ADDR`` (the direct peer). Behind a trusted reverse proxy the
    direct peer is the proxy, so every caller shares one IP and per-IP limits become
    instance-global. Set ``RATELIMIT_TRUSTED_PROXY_DEPTH=N`` (N = trusted proxy hops
    in front of PyRun) to take the client address from the RIGHT of
    ``X-Forwarded-For`` — the entry your outermost trusted proxy appended, which a
    caller cannot forge. Leftmost XFF entries ARE client-controlled and must never
    key a security control, so they are never read here.
    """
    from django.conf import settings

    depth = getattr(settings, "RATELIMIT_TRUSTED_PROXY_DEPTH", 0)
    if depth > 0:
        parts = [
            p.strip()
            for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
            if p.strip()
        ]
        if len(parts) >= depth:
            return parts[-depth]
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def lockout_client_ip(request):
    """``client_ip`` for django-axes: a valid IP address, or None.

    Axes stores the address in a GenericIPAddressField, which PostgreSQL types
    as ``inet``: the "unknown" placeholder (a request with no peer address,
    e.g. ``force_login``) or a malformed X-Forwarded-For entry would make the
    login request fail with a database error. None is what axes stores when
    it can't tell.
    """
    address = client_ip(request)
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        return None
