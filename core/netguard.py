"""
One SSRF guard for every user-supplied outbound URL.

Operators type endpoint URLs into console forms — S3 endpoints, notification
webhooks, vector stores, Brain import URLs — and the web or worker process then
makes requests to them. A URL that points back into the deployment's own network
(loopback, RFC-1918, link-local) would let a console user probe or drive services
only the server can reach, so every such URL is checked here, in one place:

* ``PYRUNNER_ALLOW_PRIVATE_NETWORK`` **off (the default)** — loopback, private,
  link-local and reserved addresses are rejected. The hostname is DNS-resolved
  first, so a public name that points at a private IP is caught as well as a
  literal private IP.
* **on** — loopback, private and reserved addresses are allowed, so a Qdrant,
  MinIO, ntfy or Ollama sibling on the compose network (or on localhost during
  development) is reachable. Only the operator can set it (it is an environment
  variable, never a console setting), so the people the guard defends against
  cannot flip it.
* **Always blocked, whatever the flag says** — the link-local / cloud-metadata
  ranges (``169.254.0.0/16``: ``169.254.169.254`` serves instance credentials on
  AWS, GCP and Azure; ``fe80::/10``; AWS IMDS over IPv6 ``fd00:ec2::254``) and the
  unspecified addresses ``0.0.0.0`` / ``::``. Reaching your own MinIO is a
  legitimate operator choice; leaking the host's cloud credentials never is.

``core.services.s3_service.is_safe_endpoint_url`` re-exports the function so the
original import path keeps working.
"""

from django.conf import settings

from core.script_helpers import pyrunner_netguard as _policy
from core.script_helpers.pyrunner_netguard import (  # noqa: F401 - compatibility
    ALWAYS_BLOCKED_NETWORKS, FLAG_HINT, _check_ip, socket,
)


def private_network_allowed() -> bool:
    return bool(getattr(settings, "PYRUNNER_ALLOW_PRIVATE_NETWORK", False))


def is_safe_endpoint_url(url: str, *, allow_private: bool | None = None) -> tuple[bool, str]:
    return _policy.is_safe_endpoint_url(
        url, allow_private=private_network_allowed() if allow_private is None else allow_private,
    )
