"""One declaration, handler and validation path for REST, MCP and internal tools."""

import copy
import json
import logging
import re
import time
from dataclasses import dataclass

from django.core.serializers.json import DjangoJSONEncoder

from core.ratelimit import rate_limit_exceeded, seconds_to_window_reset
from .errors import ApiError
from .schema import check_schema, validate

logger = logging.getLogger(__name__)
SLUG = re.compile(r"^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_-]*$")


@dataclass(frozen=True)
class Endpoint:
    slug: str
    handler: object
    input: dict
    title: str
    description: str
    access: str
    permission: str
    rate: tuple[int, int]

    @property
    def mcp_name(self):
        return self.slug.replace(".", "-")

    def catalog_entry(self):
        return {
            "slug": self.slug,
            "mcp_name": self.mcp_name,
            "title": self.title,
            "description": self.description,
            "inputSchema": copy.deepcopy(self.input),
            "access": self.access,
            "permission": self.permission,
        }


class Registry:
    def __init__(self):
        self._endpoints = {}

    def register(self, spec):
        if not SLUG.fullmatch(spec.slug) or len(spec.slug) > 64:
            raise ValueError("Endpoint slug must be a namespace.name of at most 64 characters")
        if spec.access not in {"read", "write"} or not spec.permission:
            raise ValueError("Endpoint access and permission are required")
        if len(spec.rate) != 2 or any(type(v) is not int or v <= 0 for v in spec.rate):
            raise ValueError("Endpoint rate must be (positive calls, positive seconds)")
        check_schema(spec.input)
        if spec.input["type"] != "object":
            raise ValueError("Endpoint input must be an object")
        if any(e.mcp_name == spec.mcp_name for e in self._endpoints.values()):
            raise ValueError("Duplicate endpoint or MCP tool name")
        self._endpoints[spec.slug] = spec

    def get(self, slug):
        return self._endpoints.get(slug)

    def visible(self, ctx):
        return [
            e
            for e in sorted(self._endpoints.values(), key=lambda e: e.slug)
            if e.permission in ctx.permissions
        ]

    def call(self, slug, ctx, params):
        started, ok = time.monotonic(), False
        try:
            ctx.check_budget()
            spec = self.get(slug)
            if spec is None:
                raise ApiError("not_found", "Unknown endpoint.", status=404)
            if ctx.workspace is None or spec.permission not in ctx.permissions:
                raise ApiError("forbidden", "Permission denied.", status=403)
            if ctx.transport in {"rest", "mcp"}:
                key = f"api_call_{ctx.principal}_{ctx.principal_id}_{slug}"
                if rate_limit_exceeded(key, *spec.rate):
                    raise ApiError(
                        "rate_limited",
                        "Rate limit exceeded.",
                        status=429,
                        retry_after=seconds_to_window_reset(spec.rate[1]),
                    )
            validate(params, spec.input)
            result = spec.handler(ctx, params)
            ctx.check_budget()
            if not isinstance(result, dict):
                raise TypeError("Endpoint must return a dict")
            json.dumps(result, cls=DjangoJSONEncoder, allow_nan=False)
            ok = True
            return result
        except ApiError:
            raise
        except Exception as exc:
            logger.exception("api.error request_id=%s slug=%s", ctx.request_id, slug)
            raise ApiError(
                "internal_error", "The request could not be completed.", status=500
            ) from exc
        finally:
            logger.info(
                "api.call slug=%s principal=%s:%s ms=%d ok=%s request_id=%s",
                slug,
                ctx.principal,
                ctx.principal_id,
                int((time.monotonic() - started) * 1000),
                ok,
                ctx.request_id,
            )


registry = Registry()


def endpoint(slug, *, title, description="", input, access="read", permission, rate=(60, 60)):
    def decorate(handler):
        schema = copy.deepcopy(input)
        schema.setdefault("additionalProperties", False)
        registry.register(
            Endpoint(slug, handler, schema, title, description, access, permission, rate)
        )
        return handler

    return decorate
