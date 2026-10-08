"""Signed worker loopback door into the shared Brain endpoint registry."""

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from core.api.errors import ApiError
from core.api.registry import registry
from core.api.schema import loads
from core.brain.access import principal_context, unknown
from core.models import Run
from .decorators import internal_datastore_token_required


@csrf_exempt
@internal_datastore_token_required
def brain_op(request, slug, op):
    try:
        if request.method != "POST":
            raise ApiError("method_not_allowed", "Use POST.", 405)
        if request.content_type != "application/json":
            raise ApiError("invalid_input", "Use application/json.", 415)
        raw = request.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ApiError("too_large", "Request body is too large.", 413)
        params = loads(raw)
        if not isinstance(params, dict) or "brain" in params:
            raise ApiError("invalid_input", "Name the brain only in the URL.")
        run = (
            Run.objects.filter(
                pk=request.datastore_run["run_id"],
                workspace_id=request.datastore_workspace,
                script__workspace_id=request.datastore_workspace,
            )
            .select_related("workspace")
            .first()
        )
        if run is None or run.workspace_id is None or run.script_id is None:
            raise unknown()
        ctx = principal_context(run.workspace, "script", run.script_id, transport="loopback")
        if slug is not None:
            params = {**params, "brain": slug}
        result = registry.call(f"brain.{op}", ctx, params)
        response = JsonResponse({"ok": True, "data": result})
    except ApiError as exc:
        response = JsonResponse({"ok": False, "error": exc.as_dict()}, status=exc.status)
    response["Cache-Control"] = "no-store"
    return response
