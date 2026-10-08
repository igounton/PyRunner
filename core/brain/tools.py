"""Grant-filtered, in-process tools; each execution rechecks current grants."""

import json

from asgiref.sync import sync_to_async
from claude_agent_sdk import tool

from core.api.errors import ApiError
from core.api.registry import registry
from .access import accessible_brains, access_for, principal_context


def build_tools(workspace, *, agent="pyai"):
    ctx = principal_context(workspace, "agent", agent)
    if not accessible_brains(ctx).exists():
        return []
    can_propose = any(access_for(ctx, brain).can_propose for brain in accessible_brains(ctx))

    def wrap(spec, name=None):
        @tool(name or spec.mcp_name, spec.description or spec.title, spec.input)
        async def call(params):
            try:
                current = principal_context(workspace, "agent", agent)
                data = await sync_to_async(registry.call)(spec.slug, current, params)
            except ApiError as exc:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps({"error": exc.as_dict()})}],
                }
            return {"content": [{"type": "text", "text": json.dumps(data)}]}

        return call

    result = [
        wrap(spec)
        for spec in registry.visible(ctx)
        if spec.slug.startswith("brain.") and (spec.access == "read" or can_propose)
    ]
    if can_propose:
        result.append(wrap(registry.get("brain.propose-feed"), "remember"))
    return result
