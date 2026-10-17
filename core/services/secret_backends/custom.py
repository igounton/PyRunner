"""
Custom adapter — a generic HTTP JSON endpoint.

GETs a fixed URL that returns a flat JSON object of key/value pairs; the reference
picks one key out of it. An optional bearer token is sent as ``Authorization``.
The escape hatch for anything not covered by a dedicated adapter (an internal
secrets service, a wrapper in front of another store, …).
"""

import requests

from core.net_errors import describe_request_error

from .base import SecretBackend, SecretResolutionError, register

CUSTOM_TIMEOUT = 10  # seconds


class CustomHTTPBackend(SecretBackend):
    provider_key = "custom"
    label = "Custom HTTP endpoint"
    docs_url = ""

    fields = [
        {
            "name": "url",
            "label": "Endpoint URL",
            "kind": "config",
            "required": True,
            "placeholder": "https://secrets.internal/api/values",
            "help": "GET this URL; it must return a flat JSON object of key/value pairs.",
        },
        {
            "name": "token",
            "label": "Bearer token",
            "kind": "credential",
            "required": False,
            "placeholder": "…",
            "help": "Optional — sent as an Authorization: Bearer header.",
        },
    ]
    ref_placeholder = "json_key"
    ref_help = "A key in the JSON object the endpoint returns."

    def split_ref(self, ref: str) -> tuple[str, str]:
        # One URL per profile: a single cache path, the ref is the key to pick.
        return "", (ref or "").strip()

    def _url(self, profile) -> str:
        url = (profile.config or {}).get("url", "").strip()
        if not url:
            raise SecretResolutionError("Custom endpoint URL is not configured")
        return url

    def _headers(self, profile) -> dict:
        token = (profile.get_credentials() or {}).get("token", "").strip()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _get(self, profile):
        return requests.get(
            self._url(profile), headers=self._headers(profile), timeout=CUSTOM_TIMEOUT
        )

    def test_connection(self, profile) -> tuple[bool, str]:
        try:
            self._url(profile)
        except SecretResolutionError as e:
            return False, str(e)
        try:
            resp = self._get(profile)
        except requests.RequestException as e:
            return False, describe_request_error(e, "the endpoint", CUSTOM_TIMEOUT)
        if resp.status_code != 200:
            return False, f"Endpoint returned HTTP {resp.status_code}."
        try:
            data = resp.json()
        except ValueError:
            return False, "Endpoint did not return JSON."
        if not isinstance(data, dict):
            return False, "Endpoint JSON was not an object of key/value pairs."
        return True, f"Endpoint reachable ({len(data)} key(s))."

    def fetch(self, profile, path: str) -> dict[str, str]:
        try:
            resp = self._get(profile)
        except requests.RequestException as e:
            raise SecretResolutionError(
                describe_request_error(e, "the custom endpoint", CUSTOM_TIMEOUT)
            ) from e

        if resp.status_code in (401, 403):
            raise SecretResolutionError("Custom endpoint denied access (401/403)")
        if resp.status_code != 200:
            raise SecretResolutionError(
                f"Custom endpoint returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise SecretResolutionError(f"Custom endpoint did not return JSON: {e}") from e
        if not isinstance(data, dict):
            raise SecretResolutionError("Custom endpoint JSON was not an object")
        return {str(k): str(v) for k, v in data.items()}


register(CustomHTTPBackend())
