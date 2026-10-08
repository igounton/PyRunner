"""Tool-free plugin completions; reuse provider credentials and URL policy."""

import time
from urllib.parse import urlsplit

import requests

from core.api.exceptions import ApiError
from core.models import AIProvider, GlobalSettings
from core.netguard import is_safe_endpoint_url
from core.services.claude_service import ClaudeService, ClaudeServiceError
from core.vectors.budget import BudgetSession


def complete(provider_name, *, system, prompt, timeout):
    provider = AIProvider.objects.filter(name=provider_name).exclude(auth_method="subscription").first()
    if not GlobalSettings.get_settings().claude_enabled or provider is None or not provider.default_model:
        raise ApiError("not_configured", "Enable an API-key AI profile with a default model in Services.", 409)
    if not isinstance(system, str) or not isinstance(prompt, str) or len(system) + len(prompt) > 100000:
        raise ApiError("invalid_input", "The completion prompt is too large.")
    base = provider.base_url or provider.preset.get("base_url") or "https://api.anthropic.com"
    url = base.rstrip("/") + "/v1/messages"
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not is_safe_endpoint_url(url)[0]:
        raise ApiError("not_configured", "The AI endpoint is not allowed.", 409)
    try:
        credential = ClaudeService._decrypt_credential(provider)
    except ClaudeServiceError:
        raise ApiError("not_configured", "Update the AI profile credential in Services.", 409) from None
    headers = {"anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    headers["x-api-key" if provider.is_anthropic else "Authorization"] = credential if provider.is_anthropic else f"Bearer {credential}"
    try:
        with BudgetSession(time.monotonic() + min(30, max(1, float(timeout)))) as session:
            response = session.post(url, headers=headers, json={
                "model": provider.default_model, "max_tokens": 1800,
                "system": system, "messages": [{"role": "user", "content": prompt}],
            })
            response.raise_for_status()
            data = response.json()
        text = "".join(block.get("text", "") for block in data["content"] if block.get("type") == "text")
        if not text or len(text) > 30000:
            raise ValueError
        usage = data.get("usage") or {}
        return {"text": text, "model": str(data.get("model", provider.default_model))[:100],
                "usage": {key: max(0, int(usage.get(key, 0))) for key in ("input_tokens", "output_tokens")}}
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
        raise ApiError("provider_error", "The answer provider is unavailable. Retry later.", 503) from None
