"""
Py AI dashboard — chat with the built-in read-only assistant.

Beta access: superuser, or an owner/admin of the active workspace. Config
(enable/model/system prompt) is instance-global, so superuser-only. Conversation
memory for the web surface is session-backed (last few turns).
"""

import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core.forms import PyAISettingsForm
from core.models import GlobalSettings, WorkspaceMembership
from core.services.claude_service import ClaudeService
from core.services.pyai import PyAIError, PyAIService

logger = logging.getLogger(__name__)

_HISTORY_KEY = "pyai_history"
_MAX_HISTORY = 12


def _allowed(request) -> bool:
    """Beta gate: superuser, or owner/admin of the active workspace."""
    if request.user.is_superuser:
        return True
    return WorkspaceMembership.objects.filter(
        user=request.user,
        workspace=request.workspace,
        role__in=WorkspaceMembership.MANAGE_ROLES,
    ).exists()


def _readable_brains(workspace):
    """Brains console Py AI may read (its ``agent:pyai`` grants), with tier and
    propose right — the same check its brain tools make at call time."""
    from core.api.errors import ApiError
    from core.brain.access import access_for, accessible_brains, principal_context
    from core.models.brain import TIERS

    if workspace is None:
        return []
    labels = dict(TIERS)
    ctx = principal_context(workspace, "agent", "pyai")
    rows = []
    for brain in accessible_brains(ctx).order_by("name"):
        try:
            access = access_for(ctx, brain)
        except ApiError:
            continue
        rows.append({"brain": brain, "tier": labels.get(access.tier, access.tier), "can_propose": access.can_propose})
    return rows


@login_required
def pyai_view(request: HttpRequest) -> HttpResponse:
    if not _allowed(request):
        raise PermissionDenied("Py AI is available to workspace owners and admins.")

    settings = GlobalSettings.get_settings()
    return render(
        request,
        "cpanel/pyai/chat.html",
        {
            "available": PyAIService.is_available(),
            "claude_configured": settings.claude_enabled and ClaudeService.is_configured(),
            "pyai_enabled": settings.pyai_enabled,
            "history": request.session.get(_HISTORY_KEY, []),
            "readable_brains": _readable_brains(request.workspace),
            "is_superuser": request.user.is_superuser,
            "settings_form": PyAISettingsForm(instance=settings) if request.user.is_superuser else None,
        },
    )


@login_required
@require_POST
def pyai_send_view(request: HttpRequest) -> JsonResponse:
    if not _allowed(request):
        return JsonResponse({"error": "Not allowed."}, status=403)

    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid request."}, status=400)
    message = (data.get("message") or "").strip()
    if not message:
        return JsonResponse({"error": "Message is empty."}, status=400)

    if not PyAIService.is_available():
        return JsonResponse(
            {"error": "Py AI is not enabled. Configure Claude and enable Py AI below."},
            status=400,
        )

    history = request.session.get(_HISTORY_KEY, [])
    try:
        result = PyAIService.respond(message, workspace=request.workspace, history=history)
    except PyAIError as e:
        return JsonResponse({"error": str(e)}, status=502)
    except Exception as e:  # noqa: BLE001
        logger.exception("Py AI respond failed")
        return JsonResponse({"error": f"Py AI failed: {e}"}, status=502)

    history = history + [
        {"role": "user", "text": message},
        {"role": "assistant", "text": result.text},
    ]
    request.session[_HISTORY_KEY] = history[-_MAX_HISTORY:]
    request.session.modified = True

    return JsonResponse({"text": result.text, "tools_used": result.tools_used})


@login_required
@require_POST
def pyai_clear_view(request: HttpRequest) -> JsonResponse:
    request.session.pop(_HISTORY_KEY, None)
    request.session.modified = True
    return JsonResponse({"ok": True})


@login_required
@require_POST
def pyai_settings_view(request: HttpRequest) -> HttpResponse:
    if not request.user.is_superuser:
        messages.error(request, "Only an administrator can change Py AI settings.")
        return redirect("cpanel:pyai")

    settings = GlobalSettings.get_settings()
    form = PyAISettingsForm(request.POST, instance=settings)
    if form.is_valid():
        if form.cleaned_data.get("pyai_enabled") and not (
            settings.claude_enabled and ClaudeService.is_configured()
        ):
            messages.error(request, "Configure an AI provider (Services → AI Provider) before enabling Py AI.")
        else:
            form.save(settings)
            messages.success(request, "Py AI settings saved.")
    else:
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, f"{field}: {error}")
    return redirect("cpanel:pyai")
