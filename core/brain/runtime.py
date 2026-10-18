"""Profile-backed, bounded SDK calls with a durable per-brain usage ledger."""

import asyncio
import logging
import math
import tempfile
import time
from dataclasses import dataclass, field
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.api.exceptions import ApiError
from core.models import ClaudeUsage, GlobalSettings
from core.services.claude_service import ClaudeService, ClaudeServiceError
from .intake import safe_url
from .service import BrainService

log = logging.getLogger(__name__)
DENIED_TOOLS = [
    "Bash",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "Agent",
    "TodoWrite",
    "KillShell",
    "BashOutput",
]


@dataclass
class AgentResult:
    output: object = None
    model: str = ""
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    num_turns: int = 0
    duration_ms: int = 0


class PartialUsage:
    """Source-port accounting: output deltas are cumulative within each turn."""

    def __init__(self):
        self.totals = {}
        self.output = 0

    def observe(self, event):
        if event.get("type") == "message_start":
            self.totals["output_tokens"] = self.totals.get("output_tokens", 0) + self.output
            usage = (event.get("message") or {}).get("usage") or {}
            self.output = int(usage.get("output_tokens") or 0)
            for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                self.totals[key] = self.totals.get(key, 0) + int(usage.get(key) or 0)
        elif event.get("type") == "message_delta":
            self.output = int((event.get("usage") or {}).get("output_tokens", self.output))

    def value(self):
        return {**self.totals, "output_tokens": self.totals.get("output_tokens", 0) + self.output}


@transaction.atomic
def daily_spend(brain):
    """``(spent today in USD, any call today with unknown cost)`` for the brain's AI runs."""
    today = ClaudeUsage.objects.filter(
        source="brain", brain_id=brain.pk, created_at__date=timezone.localdate()
    )
    unknown = today.filter(cost_usd__isnull=True).exists()
    spent = Decimal(str(today.aggregate(total=Sum("cost_usd"))["total"] or 0))
    return spent, unknown


def cap_refusal(brain) -> str:
    """Why the daily cap refuses an AI run right now, or ``""`` (U64: checked before queueing)."""
    if brain.daily_cost_cap_usd is None:
        return ""
    spent, unknown = daily_spend(brain)
    if unknown:
        return (
            "An AI call today is still running or has unknown cost. Retry tomorrow or remove "
            "the cap explicitly."
        )
    if brain.daily_cost_cap_usd - spent <= 0:
        return "The brain's daily AI cap is reached. Raise it or retry tomorrow."
    return ""


@transaction.atomic
def prepare(workspace, brain_id, kind):
    # The brain row lock serializes the daily-cap check and the usage reservation;
    # it needs a transaction (PostgreSQL refuses SELECT ... FOR UPDATE without one).
    # The AI call itself runs after this commits.
    brain = BrainService.get(workspace, brain_id, lock=True)
    settings = GlobalSettings.get_settings()
    provider = brain.ai_provider or settings.active_ai_provider
    if not settings.claude_enabled or provider is None:
        raise ApiError(
            "not_configured", "Configure and enable an AI provider in Services before retrying."
        )
    try:
        credential = ClaudeService._decrypt_credential(provider)
    except ClaudeServiceError:
        raise ApiError(
            "not_configured", "The AI provider credential is unavailable. Update it in Services."
        ) from None
    env = {
        key: ""
        for key in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_MODEL",
        )
    }
    env.update(ClaudeService._env_for_provider(provider, credential))
    env.update(
        {
            "CLAUDE_CODE_USE_BEDROCK": "0",
            "CLAUDE_CODE_USE_VERTEX": "0",
            "CLAUDE_CODE_USE_FOUNDRY": "0",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
        }
    )
    if env.get("ANTHROPIC_BASE_URL"):
        safe_url(env["ANTHROPIC_BASE_URL"])
    model = getattr(brain, f"{kind}_model") or provider.default_model
    budget = None
    if brain.daily_cost_cap_usd is not None:
        refusal = cap_refusal(brain)
        if refusal:
            raise ApiError("daily_cap", refusal)
        spent, _ = daily_spend(brain)
        budget = float(brain.daily_cost_cap_usd - spent)
    usage = ClaudeUsage.objects.create(
        source="brain", brain_id=brain.pk, provider=provider.provider_type, model=model
    )
    return usage, env, model, budget


async def _drive(prompt, options, result, partial, timeout):
    import claude_agent_sdk as sdk

    async def drain():
        async for message in sdk.query(prompt=prompt, options=options):
            if isinstance(message, sdk.StreamEvent):
                partial.observe(message.event)
            elif isinstance(message, sdk.AssistantMessage):
                result.model = message.model or result.model
            elif isinstance(message, sdk.ResultMessage):
                result.usage = message.usage or {}
                result.cost_usd = message.total_cost_usd
                result.num_turns, result.duration_ms = message.num_turns, message.duration_ms
                if message.is_error and message.subtype == "error_max_budget_usd":
                    raise ApiError(
                        "daily_cap",
                        "This call reached the brain's daily AI cap before it finished. "
                        "Raise the cap or retry tomorrow.",
                    )
                if message.is_error:
                    raise ApiError(
                        "provider_error",
                        "The AI provider could not finish. Retry or check the provider profile.",
                        502,
                    )
                result.output = message.structured_output

    await asyncio.wait_for(drain(), timeout=timeout)
    if result.output is None:
        raise ApiError(
            "provider_error",
            "The AI provider returned no structured output. Retry or choose another model.",
            502,
        )


def run_agent(
    workspace,
    brain_id,
    *,
    kind,
    prompt,
    system,
    schema,
    tools=None,
    cwd=None,
    hooks=None,
    timeout=240,
):
    import claude_agent_sdk as sdk

    if cwd is not None and not (hooks or {}).get("PreToolUse"):
        raise ApiError("snapshot_error", "The reader's file guard is unavailable.", 503)
    usage, env, model, budget = prepare(workspace, brain_id, kind)
    result, partial, started = AgentResult(model=model), PartialUsage(), time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="pyrunner-brain-ai-") as scratch:
            env["CLAUDE_CONFIG_DIR"] = scratch
            kwargs = dict(
                cwd=str(cwd or scratch),
                permission_mode="dontAsk",
                setting_sources=[],
                strict_mcp_config=True,
                system_prompt=system,
                disallowed_tools=DENIED_TOOLS,
                env=env,
                max_turns=12,
                include_partial_messages=True,
                output_format={"type": "json_schema", "schema": schema},
            )
            if model:
                kwargs["model"] = model
            if budget is not None:
                kwargs["max_budget_usd"] = budget
            if cwd is not None:
                kwargs.update(
                    tools=["Read", "Grep", "Glob"],
                    allowed_tools=["Read", "Grep", "Glob"],
                    hooks=hooks or {},
                )
            else:
                kwargs.update(
                    tools=[],
                    allowed_tools=[f"mcp__brain__{tool.name}" for tool in tools or []],
                    mcp_servers={
                        "brain": sdk.create_sdk_mcp_server(name="brain", tools=tools or [])
                    },
                )
            async_to_sync(_drive)(
                prompt, sdk.ClaudeAgentOptions(**kwargs), result, partial, timeout
            )
            return result
    except TimeoutError:
        raise ApiError(
            "timeout", "The brain AI call timed out. Retry with a smaller source or task.", 504
        ) from None
    except ApiError:
        raise
    except Exception as exc:
        log.warning(
            "brain_ai_failed brain=%s kind=%s error_type=%s", brain_id, kind, type(exc).__name__
        )
        raise ApiError(
            "provider_error", "The brain AI call failed. Check the provider and retry.", 502
        ) from None
    finally:
        counts = result.usage or partial.value()
        for field_name, key in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read_tokens", "cache_read_input_tokens"),
            ("cache_creation_tokens", "cache_creation_input_tokens"),
        ):
            setattr(usage, field_name, max(0, min(2147483647, int(counts.get(key) or 0))))
        cost = result.cost_usd
        usage.cost_usd = (
            cost if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0 else None
        )
        usage.model = (result.model or model)[:100]
        usage.num_turns = max(0, result.num_turns)
        usage.duration_ms = max(0, result.duration_ms or int((time.monotonic() - started) * 1000))
        usage.save()
