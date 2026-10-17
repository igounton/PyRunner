"""
Secret-backend seam — a registry of ``SecretBackend`` classes (mirrors the
channels provider seam and the ``RunBackend`` seam). Adding an external secrets
provider is additive: define a subclass with declarative ``fields`` specs,
implement ``test_connection`` + ``fetch``, and register it. No migration, no
model edit, no form/template work — every UI surface renders from the registry.

``fetch`` is path-level on purpose: it returns the WHOLE path's key/value map, and
the resolver (``resolve_secret_ref`` in ``cache.py``) caches on ``(profile, path)``
and extracts ``#key`` uniformly — so ten secrets pointing at ten keys of one Vault
path cost one HTTP call per TTL window.
"""

from __future__ import annotations


class SecretResolutionError(Exception):
    """Raised when an external secret cannot be resolved.

    Propagated by the run resolver (unlike a local decrypt error, which is
    logged-and-skipped) so the run fails BEFORE start with a clear, named cause —
    a silently missing env var produces far worse downstream failures.
    """


def guard_endpoint(url: str) -> str:
    """Return ``url`` if it passes the shared SSRF guard, else raise.

    Backends whose endpoint is operator-supplied (custom, Vault, Infisical) call
    this before every request. Without it, a secret provider pointed at the cloud
    metadata endpoint would fetch instance credentials and hand them back as
    "secret values". The same ``PYRUNNER_ALLOW_PRIVATE_NETWORK`` policy as storage,
    webhooks and vector stores applies; the cloud-metadata range is always blocked.
    """
    from core.netguard import is_safe_endpoint_url

    ok, why = is_safe_endpoint_url(url)
    if not ok:
        raise SecretResolutionError(f"Endpoint URL is not allowed: {why}")
    return url


class SecretBackend:
    """Base class for an external secrets provider adapter.

    Subclasses set ``provider_key`` and declare their credential/config surface in
    ``fields`` (the Stage 2 profile form auto-renders from these). A field spec is
    ``{name, label, kind, required, placeholder, help}`` where ``kind`` is
    ``"config"`` (plain, stored in ``SecretProvider.config``) or ``"credential"``
    (encrypted, stored in the ``credentials_encrypted`` blob).
    """

    provider_key: str = ""
    label: str = ""
    docs_url: str = ""
    fields: list[dict] = []
    ref_placeholder: str = ""  # e.g. "path/to/secret#key"
    ref_help: str = ""

    def test_connection(self, profile) -> tuple[bool, str]:
        """Probe the provider with the profile's credentials. Returns (ok, message)."""
        raise NotImplementedError

    def fetch(self, profile, path: str) -> dict[str, str]:
        """Fetch ONE path and return ALL its key/value pairs (raises on failure)."""
        raise NotImplementedError

    def split_ref(self, ref: str) -> tuple[str, str]:
        """Split a reference into ``(path, key)``.

        Default: ``"path/to/secret#key"`` → ``("path/to/secret", "key")``; a
        ``#``-less ref → ``(ref, "")``. Splits on the LAST ``#`` so a ``#`` inside
        the path survives. Adapters with a different ref grammar override this.
        """
        ref = (ref or "").strip()
        if "#" in ref:
            path, key = ref.rsplit("#", 1)
            return path.strip(), key.strip()
        return ref, ""


_REGISTRY: dict[str, SecretBackend] = {}


def register(backend: SecretBackend) -> SecretBackend:
    """Register a backend instance under its ``provider_key``."""
    _REGISTRY[backend.provider_key] = backend
    return backend


def get_backend(provider_key: str) -> SecretBackend:
    """Return the registered backend, or raise ``SecretResolutionError``."""
    try:
        return _REGISTRY[provider_key]
    except KeyError:
        raise SecretResolutionError(f"Unknown secret provider: {provider_key!r}")


def list_backends() -> list[SecretBackend]:
    """All registered backends, sorted by ``provider_key`` (drives the UI dropdowns)."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]
