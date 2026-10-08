"""Console reader work on q2, with user/workspace checks before and after it."""

import logging
import time
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.api.exceptions import ApiError
from core.api.registry import registry
from core.models import Brain, BrainReaderJob
from .access import principal_context, resolve

log = logging.getLogger(__name__)


def submit(workspace, brain, user, task, lens=""):
    from django_q.tasks import async_task
    from core.ratelimit import rate_limit_exceeded

    if rate_limit_exceeded(f"brain_reader_user_{workspace.pk}_{user.pk}", 10, 60):
        raise ApiError("rate_limited", "Wait a minute before starting another reader query.", 429)
    ctx = principal_context(workspace, "user", user.pk)
    resolve(ctx, {"brain": brain.slug})
    job = BrainReaderJob.objects.create(brain=brain, user=user, task=task, lens=lens)
    try:
        async_task("core.brain.jobs.run", str(workspace.pk), str(job.pk))
    except Exception:
        log.exception("brain_reader_queue_failed job=%s", job.pk)
        job.status, job.error = (
            "failed",
            "The worker queue is unavailable. Retry after the worker recovers.",
        )
        job.save(update_fields=["status", "error", "updated_at"])
    return job


def run(workspace_id, job_id):
    with transaction.atomic():
        job = (
            BrainReaderJob.objects.select_for_update()
            .filter(pk=job_id, brain__workspace_id=workspace_id, status="queued")
            .first()
        )
        if job is None:
            return
        job.status = "running"
        job.save(update_fields=["status", "updated_at"])
    try:
        ctx = principal_context(
            job.brain.workspace, "user", job.user_id, deadline=time.monotonic() + 240
        )
        result = registry.call(
            "brain.assemble-context",
            ctx,
            {"brain": job.brain.slug, "task": job.task, "lens": job.lens},
        )
        with transaction.atomic():
            brain, _ = resolve(ctx, {"brain": job.brain.slug}, lock=True)
            if str(brain.head_revision_id) != result["revision"]:
                raise ApiError("brain_changed", "The brain changed. Run the query again.", 409)
            BrainReaderJob.objects.filter(
                pk=job.pk, brain__workspace_id=workspace_id, status="running"
            ).update(
                status="complete",
                result=result,
                revision=brain.head_revision,
                updated_at=timezone.now(),
            )
    except Exception as exc:
        message = (
            exc.message
            if isinstance(exc, ApiError)
            else "The reader could not finish. Check the AI provider and retry."
        )
        log.warning("brain_reader_job_failed job=%s error_type=%s", job.pk, type(exc).__name__)
        BrainReaderJob.objects.filter(
            pk=job.pk, brain__workspace_id=workspace_id, status="running"
        ).update(status="failed", error=message, updated_at=timezone.now())


def reconcile():
    """Global maintenance only; jobs never automatically retry paid AI work."""
    now = timezone.now()
    BrainReaderJob.objects.filter(
        status__in=["queued", "running"], updated_at__lt=now - timedelta(minutes=10)
    ).update(
        status="failed", error="The reader job was interrupted. Submit a new query.", updated_at=now
    )
    BrainReaderJob.objects.filter(created_at__lt=now - timedelta(days=1)).delete()
    from .recall import enqueue

    for brain in Brain.objects.filter(vector_claimed_at__lt=now - timedelta(minutes=15)).exclude(
        vector_collection=None
    )[:100]:
        enqueue(brain.workspace_id, brain.pk)
