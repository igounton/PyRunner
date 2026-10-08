"""Grant-filtered, in-process tools; each execution rechecks current grants."""

import json

from asgiref.sync import sync_to_async
from claude_agent_sdk import tool

from core.api.errors import ApiError
from core.api.registry import registry
from .access import accessible_brains, principal_context


def build_tools(workspace, *, agent="pyai"):
    ctx = principal_context(workspace, "agent", agent)
    if not accessible_brains(ctx).exists():
        return []

    def wrap(spec):
        @tool(spec.mcp_name, spec.description or spec.title, spec.input)
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

    return [
        wrap(spec)
        for spec in registry.visible(ctx)
        if spec.slug.startswith("brain.") and spec.access == "read"
    ]
