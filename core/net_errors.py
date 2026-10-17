"""
One readable line for a failed outbound HTTP request (U37, U46).

``requests`` wraps urllib3's errors in text like "HTTPConnectionPool(host='vault',
port=8200): Max retries exceeded with url: /v1/auth/token/lookup-self (Caused by
NameResolutionError(...))", which is what the console showed after a failed Test
and what landed in a run's stderr. :func:`describe_request_error` turns the
common causes into one line that names the service, the host and port and the
cause ("the host name doesn't resolve", "connection refused", "timed out after
10 s"), and keeps the raw text on a second line that starts with ``Details:`` —
the Services page folds that line under a "Details" toggle.
"""

import re
from urllib.parse import urlsplit

import requests

DETAILS_PREFIX = "Details: "

_POOL_RE = re.compile(r"host='([^']+)', port=(\d+)")


def _endpoint(exc: BaseException) -> str:
    """``host:port`` the request was for, from the request or the message."""
    request = getattr(exc, "request", None)
    url = getattr(request, "url", None)
    if url:
        parts = urlsplit(url)
        if parts.hostname:
            try:
                port = parts.port
            except ValueError:
                port = None
            port = port or (443 if parts.scheme == "https" else 80)
            return f"{parts.hostname}:{port}"
    match = _POOL_RE.search(str(exc))
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    return ""


def _seconds(timeout, part: int) -> str:
    if isinstance(timeout, (tuple, list)):
        timeout = timeout[part] if len(timeout) > part else timeout[0]
    if timeout is None:
        return ""
    value = float(timeout)
    return f"{value:g} s"


def network_cause(exc: BaseException, timeout=None) -> str:
    """Plain-words cause of a connection failure, or ``""`` when unknown."""
    text = f"{exc.__class__.__name__}: {exc}".lower()
    if isinstance(exc, requests.exceptions.SSLError) or "certificate verify failed" in text:
        return "the TLS handshake failed (certificate or protocol problem)"
    if any(
        marker in text
        for marker in (
            "nameresolutionerror",
            "name or service not known",
            "nodename nor servname",
            "getaddrinfo failed",
            "temporary failure in name resolution",
            "no address associated with hostname",
            "failed to resolve",
        )
    ):
        return "the host name doesn't resolve"
    if any(
        marker in text
        for marker in ("connection refused", "connectionrefusederror", "errno 111", "10061")
    ):
        return "connection refused (nothing is listening on that port)"
    if isinstance(exc, requests.exceptions.ConnectTimeout) or "connecttimeout" in text:
        after = _seconds(timeout, 0)
        return f"timed out after {after}" if after else "timed out"
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in text:
        after = _seconds(timeout, 1)
        return f"no answer within {after}" if after else "timed out"
    if any(
        marker in text
        for marker in ("connection reset", "connection aborted", "remote end closed")
    ):
        return "the server closed the connection"
    return ""


def describe_request_error(exc: BaseException, service: str, timeout=None) -> str:
    """``Could not reach <service> at host:port — <cause>.`` plus a Details line."""
    where = _endpoint(exc)
    head = f"Could not reach {service}" + (f" at {where}" if where else "")
    raw = str(exc) or exc.__class__.__name__
    cause = network_cause(exc, timeout)
    if not cause:
        return f"{head}: {raw}"
    return f"{head} — {cause}.\n{DETAILS_PREFIX}{raw}"
