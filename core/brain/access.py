"""Grant resolution shared by every Brain door; never infer a public grant."""

from dataclasses import dataclass
import re
import uuid

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from core.api.context import CallContext
from core.api.errors import ApiError
from core.models import APIToken, Brain, BrainGrant, Script
from .visibility import TIER_ORDER, TIERS


def unknown():
    return ApiError("not_found", "Unknown brain or note.", 404)


def subjects(ctx):
    if ctx.principal not in {"script", "plugin", "agent", "token"} or not ctx.principal_id:
        return []
    result = [(ctx.principal, str(ctx.principal_id))]
    if ctx.principal == "script":
        script = Script.objects.for_workspace(ctx.workspace).filter(pk=ctx.principal_id).first()
        if script is None:
            return []
        if script.owner_plugin:
            result.append(("plugin", script.owner_plugin))
    return result


@dataclass(frozen=True)
class Access:
    tier: str
    can_propose: bool


def grants(ctx):
    terms = Q(pk__in=[])
    for kind, identifier in subjects(ctx):
        terms |= Q(subject_kind=kind, subject_id=identifier)
    return BrainGrant.objects.filter(terms, brain__workspace=ctx.workspace)


def accessible_brains(ctx):
    scope = Brain.objects.for_workspace(ctx.workspace)
    owners = [identifier for kind, identifier in subjects(ctx) if kind == "plugin"]
    scope = scope.filter(Q(pk__in=grants(ctx).values("brain_id")) | Q(owner_plugin__in=owners))
    if ctx.resource is not None:
        scope = scope.filter(pk=ctx.resource.pk)
    if ctx.tier is not None and ctx.tier not in TIER_ORDER:
        return scope.none()
    return scope


def access_for(ctx, brain):
    if brain.workspace_id != ctx.workspace.pk:
        raise unknown()
    if ctx.resource is not None and ctx.resource.pk != brain.pk:
        raise unknown()
    if ctx.principal == "user":
        from core.models import User, WorkspaceMembership

        user = User.objects.filter(pk=ctx.principal_id, is_active=True).first()
        if user and (user.is_superuser or WorkspaceMembership.objects.filter(user=user, workspace=ctx.workspace).exists()):
            return Access("private", True)
        raise unknown()
    token = None
    if ctx.principal == "token":
        token = APIToken.objects.filter(pk=ctx.principal_id, workspace=ctx.workspace, scope="api", is_active=True).first()
        if (token is None or (token.expires_at and token.expires_at <= timezone.now())
                or not ctx.permissions.issubset(token.permissions)
                or (token.brain_id and token.brain_id != brain.pk)):
            raise unknown()
    entries = list(grants(ctx).filter(brain=brain))
    owned = bool(brain.owner_plugin and ("plugin", brain.owner_plugin) in subjects(ctx))
    if not entries and not owned:
        raise unknown()
    rank = 2 if owned else max(TIER_ORDER.get(g.max_tier, -1) for g in entries)
    if ctx.tier is not None:
        rank = min(rank, TIER_ORDER.get(ctx.tier, -1))
    if token is not None:
        rank = min(rank, TIER_ORDER.get(token.brain_tier, -1))
    if rank < 0:
        raise unknown()
    return Access(TIERS[rank], rank >= 1 and (owned or any(g.can_propose for g in entries)))


def _with_slug(scope, slug):
    """Narrow ``scope`` to ``slug``: the exact slug, else a unique case-insensitive
    match (F55 — a pre-2.0 mixed-case slug typed in another case). Ambiguous case
    twins match nothing, so the caller gets the same "unknown brain" answer."""
    if scope.filter(slug=slug).exists() or not isinstance(slug, str):
        return scope.filter(slug=slug)
    pks = list(scope.filter(slug__iexact=slug).values_list("pk", flat=True)[:2])
    return scope.filter(pk=pks[0]) if len(pks) == 1 else scope.none()


def resolve(ctx, params, *, lock=False):
    scope = Brain.objects.for_workspace(ctx.workspace)
    slug = params.get("brain")
    if ctx.resource is not None:
        scope = scope.filter(pk=ctx.resource.pk)
        if slug is not None:
            scope = _with_slug(scope, slug)
    elif slug:
        scope = _with_slug(scope, slug)
    else:
        raise unknown()
    if lock:
        scope = scope.select_for_update()
    brain = scope.first()
    if brain is None:
        raise unknown()
    return brain, access_for(ctx, brain)


def principal_context(workspace, kind, identifier, *, transport="in-process", deadline=None):
    return CallContext(
        workspace=workspace,
        principal=kind,
        principal_id=str(identifier),
        permissions=frozenset({"brain:read", "brain:write"}),
        transport=transport,
        deadline=deadline,
    )


def validate_subject(workspace, kind, identifier, tier, can_propose):
    if kind not in dict(BrainGrant.SUBJECTS) or tier not in TIER_ORDER:
        raise ValidationError("Unknown grant subject or tier.")
    identifier = str(identifier).strip()
    if not identifier or len(identifier) > 200:
        raise ValidationError({"subject_id": "Enter a valid subject identifier."})
    if kind in {"script", "token"}:
        try:
            identifier = str(uuid.UUID(identifier))
        except ValueError:
            raise ValidationError({"subject_id": "Enter a valid UUID."}) from None
        query = (
            Script.objects.for_workspace(workspace)
            if kind == "script"
            else APIToken.objects.filter(workspace=workspace, scope="api")
        )
        if not query.filter(pk=identifier).exists():
            raise ValidationError({"subject_id": "Unknown subject in this workspace."})
    elif not re.fullmatch(
        r"[a-z][a-z0-9_]*" if kind == "plugin" else r"[a-zA-Z0-9_.:-]+", identifier
    ):
        raise ValidationError({"subject_id": "Enter a valid subject identifier."})
    if can_propose and tier == "public":
        raise ValidationError({"max_tier": "Proposals require agents-only or private tier."})
    return identifier
