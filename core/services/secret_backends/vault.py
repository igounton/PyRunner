"""
HashiCorp Vault / OpenBao adapter — KV v2 secrets engine, token auth.

One adapter covers both: OpenBao is API-compatible with Vault's KV v2. Reads
``{base_url}/v1/{mount}/data/{path}`` with an ``X-Vault-Token`` header and returns
that path's ``data.data`` key/value map. AppRole and other auth methods are a
later, additive extension (another credential field + a login step).
"""

import requests

from core.net_errors import describe_request_error

from .base import SecretBackend, SecretResolutionError, register

# Vault reads are fast; keep the run-start latency bound tight.
VAULT_TIMEOUT = 10  # seconds


class VaultBackend(SecretBackend):
    provider_key = "vault"
    label = "HashiCorp Vault / OpenBao"
    docs_url = "https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2"

    fields = [
        {
            "name": "base_url",
            "label": "Server URL",
            "kind": "config",
            "required": True,
            "placeholder": "https://vault.example.com:8200",
            "help": "Base URL of the Vault / OpenBao server (no trailing /v1).",
        },
        {
            "name": "mount",
            "label": "KV mount",
            "kind": "config",
            "required": False,
            "placeholder": "secret",
            "help": "KV v2 mount point. Defaults to 'secret'.",
        },
        {
            "name": "namespace",
            "label": "Namespace",
            "kind": "config",
            "required": False,
            "placeholder": "admin/team-a",
            "help": "Vault Enterprise / HCP namespace (optional).",
        },
        {
            "name": "token",
            "label": "Token",
            "kind": "credential",
            "required": True,
            "placeholder": "hvs.…",
            "help": "A Vault token with read access to the secret paths.",
        },
    ]
    ref_placeholder = "path/to/secret#key"
    ref_help = (
        "Path within the KV mount, then '#', then the key inside that secret — "
        "e.g. myapp/prod#API_KEY."
    )

    def _base_url(self, profile) -> str:
        base = (profile.config or {}).get("base_url", "").strip().rstrip("/")
        if not base:
            raise SecretResolutionError("Vault server URL (base_url) is not configured")
        return base

    def _mount(self, profile) -> str:
        return (profile.config or {}).get("mount", "").strip() or "secret"

    def _headers(self, profile) -> dict:
        token = (profile.get_credentials() or {}).get("token", "").strip()
        if not token:
            raise SecretResolutionError("Vault token is not configured")
        headers = {"X-Vault-Token": token}
        namespace = (profile.config or {}).get("namespace", "").strip()
        if namespace:
            headers["X-Vault-Namespace"] = namespace
        return headers

    def test_connection(self, profile) -> tuple[bool, str]:
        try:
            base = self._base_url(profile)
            headers = self._headers(profile)
        except SecretResolutionError as e:
            return False, str(e)
        try:
            resp = requests.get(
                f"{base}/v1/auth/token/lookup-self",
                headers=headers,
                timeout=VAULT_TIMEOUT,
            )
        except requests.RequestException as e:
            return False, describe_request_error(e, "Vault", VAULT_TIMEOUT)
        if resp.status_code == 200:
            return True, "Token is valid."
        if resp.status_code in (401, 403):
            return False, "Vault rejected the token (401/403)."
        return False, f"Vault returned HTTP {resp.status_code}."

    def fetch(self, profile, path: str) -> dict[str, str]:
        base = self._base_url(profile)
        headers = self._headers(profile)
        mount = self._mount(profile)
        clean_path = path.strip().strip("/")
        url = f"{base}/v1/{mount}/data/{clean_path}"
        try:
            resp = requests.get(url, headers=headers, timeout=VAULT_TIMEOUT)
        except requests.RequestException as e:
            raise SecretResolutionError(describe_request_error(e, "Vault", VAULT_TIMEOUT)) from e

        if resp.status_code == 404:
            raise SecretResolutionError(
                f"path {clean_path!r} not found in Vault mount {mount!r}"
            )
        if resp.status_code in (401, 403):
            raise SecretResolutionError(
                "Vault denied access (401/403) — check the token's policy"
            )
        if resp.status_code != 200:
            raise SecretResolutionError(
                f"Vault returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            data = resp.json()["data"]["data"]
        except (ValueError, KeyError, TypeError) as e:
            raise SecretResolutionError(f"Unexpected Vault response shape: {e}") from e
        if not isinstance(data, dict):
            raise SecretResolutionError("Vault response 'data.data' was not an object")
        return {str(k): str(v) for k, v in data.items()}


register(VaultBackend())
