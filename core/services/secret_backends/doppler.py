"""
Doppler adapter — service-token auth.

A Doppler service token is already scoped to one project + config, so there is no
config to enter and no "path": ``fetch`` downloads the whole config's secrets in
one call (cached per profile), and the reference is simply the secret name.
"""

import requests

from core.net_errors import describe_request_error

from .base import SecretBackend, SecretResolutionError, register

DOPPLER_TIMEOUT = 10  # seconds
DOWNLOAD_URL = "https://api.doppler.com/v3/configs/config/secrets/download"


class DopplerBackend(SecretBackend):
    provider_key = "doppler"
    label = "Doppler"
    docs_url = "https://docs.doppler.com/reference/auth-token-formats"

    fields = [
        {
            "name": "service_token",
            "label": "Service token",
            "kind": "credential",
            "required": True,
            "placeholder": "dp.st.…",
            "help": "A Doppler service token — it already scopes to a project + config.",
        },
    ]
    ref_placeholder = "SECRET_NAME"
    ref_help = "The secret's name. The service token already scopes the project + config."

    def split_ref(self, ref: str) -> tuple[str, str]:
        # One config = one secret set: a single cache path per profile, the ref is
        # the key. Doppler names can't contain '#', so take the whole ref.
        return "", (ref or "").strip()

    def _token(self, profile) -> str:
        token = (profile.get_credentials() or {}).get("service_token", "").strip()
        if not token:
            raise SecretResolutionError("Doppler service token is not configured")
        return token

    def _download(self, token: str):
        return requests.get(
            DOWNLOAD_URL,
            params={"format": "json", "include_dynamic_secrets": "false"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=DOPPLER_TIMEOUT,
        )

    def test_connection(self, profile) -> tuple[bool, str]:
        try:
            token = self._token(profile)
        except SecretResolutionError as e:
            return False, str(e)
        try:
            resp = self._download(token)
        except requests.RequestException as e:
            return False, describe_request_error(e, "Doppler", DOPPLER_TIMEOUT)
        if resp.status_code == 200:
            return True, "Doppler service token is valid."
        if resp.status_code in (401, 403):
            return False, "Doppler rejected the service token (401/403)."
        return False, f"Doppler returned HTTP {resp.status_code}."

    def fetch(self, profile, path: str) -> dict[str, str]:
        token = self._token(profile)
        try:
            resp = self._download(token)
        except requests.RequestException as e:
            raise SecretResolutionError(describe_request_error(e, "Doppler", DOPPLER_TIMEOUT)) from e

        if resp.status_code in (401, 403):
            raise SecretResolutionError("Doppler denied access (401/403) — check the token")
        if resp.status_code != 200:
            raise SecretResolutionError(
                f"Doppler returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise SecretResolutionError(f"Unexpected Doppler response: {e}") from e
        if not isinstance(data, dict):
            raise SecretResolutionError("Doppler response was not a JSON object")
        return {str(k): str(v) for k, v in data.items()}


register(DopplerBackend())
