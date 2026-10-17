"""
Infisical adapter (cloud or self-hosted) — universal-auth, KV secrets.

Auth is a machine-identity **universal-auth** client id + secret: a login call
exchanges them for a short-lived access token, then the raw-secrets endpoint lists
a folder. ``fetch`` is folder-level (the resolver caches per folder), so all keys
in one folder cost one login + list per TTL window.

Reference is ``/folder/path#SECRET_KEY``; the folder is optional (root ``/``), so a
bare ``SECRET_KEY`` or ``#SECRET_KEY`` reads from the root folder.
"""

import requests

from core.net_errors import describe_request_error

from .base import SecretBackend, SecretResolutionError, guard_endpoint, register

INFISICAL_TIMEOUT = 10  # seconds
DEFAULT_BASE_URL = "https://app.infisical.com"


class InfisicalBackend(SecretBackend):
    provider_key = "infisical"
    label = "Infisical"
    docs_url = "https://infisical.com/docs/documentation/platform/identities/universal-auth"

    fields = [
        {
            "name": "base_url",
            "label": "Base URL",
            "kind": "config",
            "required": False,
            "placeholder": DEFAULT_BASE_URL,
            "help": "Blank for Infisical Cloud; set it for a self-hosted instance.",
        },
        {
            "name": "project_id",
            "label": "Project ID",
            "kind": "config",
            "required": True,
            "placeholder": "a1b2c3d4-…",
            "help": "The project (workspace) ID from Infisical.",
        },
        {
            "name": "environment",
            "label": "Environment",
            "kind": "config",
            "required": True,
            "placeholder": "prod",
            "help": "Environment slug, e.g. dev / staging / prod.",
        },
        {
            "name": "client_id",
            "label": "Client ID",
            "kind": "credential",
            "required": True,
            "placeholder": "…",
            "help": "Universal-auth machine-identity client ID.",
        },
        {
            "name": "client_secret",
            "label": "Client secret",
            "kind": "credential",
            "required": True,
            "placeholder": "…",
            "help": "Universal-auth machine-identity client secret.",
        },
    ]
    ref_placeholder = "/folder/path#SECRET_KEY"
    ref_help = (
        "Secret key, optionally prefixed with a folder path and '#'. At the root: "
        "SECRET_KEY or #SECRET_KEY."
    )

    def split_ref(self, ref: str) -> tuple[str, str]:
        ref = (ref or "").strip()
        if "#" in ref:
            folder, key = ref.rsplit("#", 1)
            return (folder.strip() or "/"), key.strip()
        # No '#': the whole ref is the key, read from the root folder.
        return "/", ref

    def _base(self, profile) -> str:
        base = (profile.config or {}).get("base_url", "").strip().rstrip("/")
        return guard_endpoint(base or DEFAULT_BASE_URL)

    def _login(self, profile) -> str:
        creds = profile.get_credentials() or {}
        client_id = (creds.get("client_id") or "").strip()
        client_secret = (creds.get("client_secret") or "").strip()
        if not client_id or not client_secret:
            raise SecretResolutionError("Infisical client ID / secret are not configured")
        try:
            resp = requests.post(
                f"{self._base(profile)}/api/v1/auth/universal-auth/login",
                json={"clientId": client_id, "clientSecret": client_secret},
                timeout=INFISICAL_TIMEOUT,
            )
        except requests.RequestException as e:
            raise SecretResolutionError(
                describe_request_error(e, "Infisical", INFISICAL_TIMEOUT)
            ) from e
        if resp.status_code in (401, 403):
            raise SecretResolutionError("Infisical rejected the client credentials (401/403)")
        if resp.status_code != 200:
            raise SecretResolutionError(f"Infisical login returned HTTP {resp.status_code}")
        try:
            token = resp.json()["accessToken"]
        except (ValueError, KeyError) as e:
            raise SecretResolutionError(f"Unexpected Infisical login response: {e}") from e
        if not token:
            raise SecretResolutionError("Infisical login returned an empty access token")
        return token

    def test_connection(self, profile) -> tuple[bool, str]:
        config = profile.config or {}
        if not (config.get("project_id") or "").strip():
            return False, "Project ID is not configured."
        if not (config.get("environment") or "").strip():
            return False, "Environment is not configured."
        try:
            self._login(profile)
        except SecretResolutionError as e:
            return False, str(e)
        return True, "Authenticated with Infisical."

    def fetch(self, profile, path: str) -> dict[str, str]:
        config = profile.config or {}
        project_id = (config.get("project_id") or "").strip()
        environment = (config.get("environment") or "").strip()
        if not project_id or not environment:
            raise SecretResolutionError("Infisical project ID / environment are not configured")

        token = self._login(profile)
        try:
            resp = requests.get(
                f"{self._base(profile)}/api/v3/secrets/raw",
                params={
                    "workspaceId": project_id,
                    "environment": environment,
                    "secretPath": path or "/",
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=INFISICAL_TIMEOUT,
            )
        except requests.RequestException as e:
            raise SecretResolutionError(
                describe_request_error(e, "Infisical", INFISICAL_TIMEOUT)
            ) from e

        if resp.status_code == 404:
            raise SecretResolutionError(f"folder {path!r} not found in Infisical")
        if resp.status_code in (401, 403):
            raise SecretResolutionError("Infisical denied access (401/403)")
        if resp.status_code != 200:
            raise SecretResolutionError(
                f"Infisical returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            secrets = resp.json()["secrets"]
        except (ValueError, KeyError, TypeError) as e:
            raise SecretResolutionError(f"Unexpected Infisical response shape: {e}") from e
        return {str(s["secretKey"]): str(s.get("secretValue", "")) for s in secrets}


register(InfisicalBackend())
