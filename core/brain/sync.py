"""Optional queued GitHub mirror. Incoming changes use the existing human gate."""

import hashlib
import logging
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.api.exceptions import ApiError
from core.models import BrainFeed, BrainSync, Workspace, WorkspaceMembership
from .github import GitHub, sha, validate_repository
from .service import BrainService

log = logging.getLogger(__name__)


def manifest(files):
    return {path: hashlib.sha256(text.encode("utf-8")).hexdigest() for path, text in files.items()}


def get(workspace, brain_id):
    value = BrainSync.objects.filter(brain_id=brain_id, brain__workspace=workspace).first()
    if value is None:
        raise ApiError("not_found", "Unknown GitHub sync configuration.", 404)
    return value


@transaction.atomic
def configure(workspace, brain_id, *, repository, branch, secret, poll_enabled=False):
    brain = BrainService.get(workspace, brain_id, lock=True)
    validate_repository(repository, branch)
    value, _ = BrainSync.objects.get_or_create(brain=brain)
    if value.status in {"queued", "running"}:
        raise ApiError("sync_busy", "Wait for the current sync job to finish before changing settings.", 409)
    if (value.repository, value.branch) != (repository, branch):
        value.generation, value.remote_sha, value.remote_manifest = uuid.uuid4(), "", {}
        value.result_feed = None
    value.repository, value.branch, value.secret = repository, branch, secret
    value.poll_enabled, value.status, value.error = poll_enabled, "idle", ""
    value.full_clean()
    value.save()
    return value


def enqueue(workspace_id, brain_id):
    from django_q.tasks import async_task

    try:
        async_task("core.brain.sync.run", str(workspace_id), str(brain_id), q_options={"timeout": 180})
    except Exception:
        log.warning("brain_sync_enqueue_failed brain=%s", brain_id, exc_info=False)
        BrainSync.objects.filter(brain_id=brain_id, brain__workspace_id=workspace_id, status="queued").update(
            status="failed", error="Could not queue sync. Check Workers and retry.", updated_at=timezone.now())


def can_manage(user, workspace):
    return bool(user and user.is_active and (user.is_superuser or WorkspaceMembership.objects.filter(
        workspace=workspace, user=user, role__in=["owner", "admin"]).exists()))


@transaction.atomic
def submit(workspace, brain_id, operation, *, user=None, initial_sha="", acknowledge=False):
    brain = BrainService.get(workspace, brain_id, lock=True)
    value = get(workspace, brain_id)
    if operation not in {"pull", "export"}:
        raise ApiError("invalid_input", "Unknown sync operation.")
    if value.status in {"queued", "running"}:
        raise ApiError("sync_busy", "A sync job is already queued or running.", 409)
    if not value.secret_id:
        raise ApiError("sync_config", "Select the GitHub Secret before syncing.")
    expected = value.remote_sha
    if operation == "export":
        if not can_manage(user, workspace) or not acknowledge:
            raise ApiError("forbidden", "An owner/admin must acknowledge exporting all visibility tiers.", 403)
        if not expected:
            expected = sha(initial_sha)
        if BrainFeed.objects.filter(brain=brain, channel="sync", status="proposed").exists():
            raise ApiError("sync_conflict", "Review or reject incoming sync proposals before exporting.", 409)
    value.status, value.operation, value.error, value.result_feed = "queued", operation, "", None
    value.job = {"revision": str(brain.head_revision_id), "expected": expected,
                 "user": str(user.pk) if user else None}
    value.claim = None
    value.last_polled_at = timezone.now()
    value.save()
    transaction.on_commit(lambda: enqueue(workspace.pk, brain_id))
    return value


def _current(workspace, brain_id, claim):
    value = get(workspace, brain_id)
    if value.status != "running" or value.claim != claim:
        raise ApiError("sync_changed", "The sync job is no longer active.", 409)
    return value


@transaction.atomic
def _capture(workspace, brain_id, claim, remote_sha, files):
    brain = BrainService.get(workspace, brain_id, lock=True)
    value = _current(workspace, brain_id, claim)
    remote = manifest(files)
    existing = BrainFeed.objects.filter(brain=brain, channel="sync", source_id=f"sync:{value.generation}:{remote_sha}",
                                        raw_payload__previous_sha=value.remote_sha).first()
    if existing:
        value.result_feed = existing
    elif remote_sha != value.remote_sha:
        local = BrainService.files(workspace, brain_id)
        changed = {p for p in set(remote) | set(value.remote_manifest) if remote.get(p) != value.remote_manifest.get(p)}
        rows = [{"path": p, "action": "delete" if p not in files else "update" if p in local else "create",
                 "content": files.get(p, "")} for p in sorted(changed) if files.get(p) != local.get(p)]
        if rows:
            value.result_feed = BrainFeed.objects.create(
                brain=brain, source_id=f"sync:{value.generation}:{remote_sha}", channel="sync", principal="github",
                raw_payload={"source_kind": "sync", "title": f"GitHub: {value.repository} / {value.branch}",
                             "source_url": f"https://github.com/{value.repository}/commit/{remote_sha}",
                             "generation": str(value.generation), "remote_sha": remote_sha, "remote_manifest": remote,
                             "previous_sha": value.remote_sha},
                base_revision=brain.head_revision, status="proposed",
                proposal={"summary": "Review incoming GitHub file changes.", "files": rows},
            )
        else:
            value.remote_sha, value.remote_manifest = remote_sha, remote
    value.status, value.claim, value.error = "idle", None, ""
    value.save()


def accept(workspace, brain_id, feed):
    """Called under the Brain lock in the same transaction as approval."""
    value = get(workspace, brain_id)
    if str(value.generation) != feed.raw_payload.get("generation") or value.remote_sha != feed.raw_payload.get("previous_sha"):
        raise ApiError("sync_changed", "Sync settings or its baseline changed. Reject this proposal and pull again.", 409)
    if value.status in {"queued", "running"}:
        raise ApiError("sync_busy", "Wait for the sync job to finish before approving.", 409)
    value.remote_sha = feed.raw_payload["remote_sha"]
    value.remote_manifest = feed.raw_payload["remote_manifest"]
    value.save(update_fields=["remote_sha", "remote_manifest", "updated_at"])


def run(workspace_id, brain_id):
    workspace = Workspace.objects.filter(pk=workspace_id).first()
    if workspace is None:
        return
    claim = uuid.uuid4()
    with transaction.atomic():
        BrainService.get(workspace, brain_id, lock=True)
        value = get(workspace, brain_id)
        if value.status != "queued":
            return
        value.status, value.claim = "running", claim
        value.save()
    client = None
    try:
        client = GitHub(value)
        if value.operation == "pull":
            remote_sha = client.head()
            files = client.files(remote_sha) if remote_sha != value.remote_sha else {}
            _capture(workspace, brain_id, claim, remote_sha, files)
        else:
            from django.contrib.auth import get_user_model

            user = get_user_model().objects.filter(pk=value.job.get("user")).first()
            if not can_manage(user, workspace):
                raise ApiError("forbidden", "The export requester no longer has workspace management access.", 403)
            files = BrainService.files(workspace, brain_id, value.job["revision"])
            remote_sha = client.export(files, value.job["expected"], f"PyRun Brain {value.brain.slug}: revision {value.job['revision']}")
            with transaction.atomic():
                BrainService.get(workspace, brain_id, lock=True)
                current = _current(workspace, brain_id, claim)
                current.remote_sha, current.remote_manifest = remote_sha, manifest(files)
                current.status, current.claim, current.error = "idle", None, ""
                current.save()
    except Exception as exc:
        log.warning("brain_sync_failed brain=%s error_type=%s", brain_id, type(exc).__name__)
        message = exc.message if isinstance(exc, ApiError) else "GitHub sync failed. Check worker logs and retry."
        BrainSync.objects.filter(brain_id=brain_id, brain__workspace=workspace, claim=claim).update(
            status="failed", claim=None, error=message[:500], updated_at=timezone.now())
    finally:
        if client:
            client.close()


def reconcile():
    now = timezone.now()
    # An interrupted export might have reached GitHub. Never repeat it automatically.
    BrainSync.objects.filter(status__in=["queued", "running"], updated_at__lt=now - timedelta(minutes=10)).update(
        status="failed", claim=None, error="Sync was interrupted. Pull to inspect GitHub before retrying export.", updated_at=now)
    due = BrainSync.objects.filter(poll_enabled=True, status="idle", secret__isnull=False).filter(
        Q(last_polled_at__isnull=True) | Q(last_polled_at__lt=now - timedelta(minutes=5)))
    for workspace_id, brain_id in list(due.values_list("brain__workspace_id", "brain_id")[:100]):
        workspace = Workspace.objects.filter(pk=workspace_id).first()
        try:
            submit(workspace, brain_id, "pull")
        except ApiError as exc:
            log.info("brain_sync_poll_skipped brain=%s code=%s", brain_id, exc.code)
