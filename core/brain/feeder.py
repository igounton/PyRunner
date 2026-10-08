"""Claimed q2 extraction, bounded retries and recovery after a worker disappears."""

import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from core.api.exceptions import ApiError
from core.models import Brain, BrainFeed, BrainRevision, Workspace
from .approval import get_feed
from .indexer import parse_files
from .intake import enqueue
from .proposal import PROPOSAL_SCHEMA, validate_shape
from .reads import Snapshot, generated_index
from .runtime import run_agent
from .service import BrainService
from .visibility import raw_paths, visible_at

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


def _view(brain, files):
    entities = [e for e in parse_files(files) if visible_at(e.visibility, "agents-only")]
    paths = {e.path for e in entities} | raw_paths(files, entities, "agents-only")
    return Snapshot(brain, "agents-only", entities, {path: files[path] for path in paths})


def _tools(view):
    from claude_agent_sdk import tool

    @tool(
        "read-file",
        "Read one file listed in the agents-only index.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    async def read_file(params):
        content = view.files.get(params.get("path"))
        return {
            "isError": content is None,
            "content": [
                {"type": "text", "text": content if content is not None else "Unknown note."}
            ],
        }

    return [read_file]


def extract(workspace_id, feed_id):
    workspace = Workspace.objects.filter(pk=workspace_id).first()
    if workspace is None:
        return
    initial = BrainFeed.objects.filter(pk=feed_id, brain__workspace=workspace).first()
    if initial is None:
        return
    with transaction.atomic():
        brain = BrainService.get(workspace, initial.brain_id, lock=True)
        feed = get_feed(workspace, brain.pk, feed_id, lock=True)
        if feed.channel == "sync" or feed.status != "pending" or feed.retries >= MAX_ATTEMPTS:
            return
        if not feed.raw_payload.get("content", "").strip():
            feed.status, feed.error = "failed", "Paste source content before retrying extraction."
            feed.save()
            return
        feed.status, feed.claim_id, feed.claimed_at = "extracting", uuid.uuid4(), timezone.now()
        feed.base_revision = brain.head_revision
        feed.retries += 1
        feed.error = ""
        feed.save()
        files = BrainService.files(workspace, brain.pk)
    result, error, retryable = None, "", False
    try:
        view = _view(brain, files)
        system = (
            Path(__file__).with_name("prompts").joinpath("feeder.md").read_text(encoding="utf-8")
        )
        system += "\n\n# Brain contract\n" + files.get("CLAUDE.md", "")
        prompt = json.dumps(
            {
                "source_id": feed.source_id,
                "source": feed.raw_payload,
                "index": generated_index(view),
            },
            ensure_ascii=False,
        )
        result = run_agent(
            workspace,
            brain.pk,
            kind="feeder",
            prompt=prompt,
            system=system,
            schema=PROPOSAL_SCHEMA,
            tools=_tools(view),
        )
        validate_shape(result.output)
    except ApiError as exc:
        error, retryable = (
            exc.message,
            exc.code not in {"not_configured", "daily_cap", "unsafe_url"},
        )
    except Exception as exc:
        log.warning("brain_feeder_failed feed=%s error_type=%s", feed_id, type(exc).__name__)
        error, retryable = "Extraction failed. Check the worker and retry.", True
    with transaction.atomic():
        BrainService.get(workspace, brain.pk, lock=True)
        current = get_feed(workspace, brain.pk, feed_id, lock=True)
        if current.status != "extracting" or current.claim_id != feed.claim_id:
            return
        current.claim_id, current.claimed_at = None, None
        if result and result.cost_usd is not None:
            current.cost_usd = (current.cost_usd or Decimal(0)) + Decimal(str(result.cost_usd))
        if not error:
            current.proposal, current.status = result.output, "proposed"
        elif retryable and current.retries < MAX_ATTEMPTS:
            from django_q.models import Schedule

            current.status, current.error = "pending", error
            current.queued_at = timezone.now() + timedelta(seconds=180 * current.retries)
            Schedule.objects.create(
                name=f"brain-feed-{current.pk}-{current.retries}",
                func="core.brain.feeder.extract",
                args=f"{str(workspace.pk)!r}, {str(current.pk)!r}",
                schedule_type=Schedule.ONCE,
                next_run=current.queued_at,
                repeats=1,
            )
        else:
            current.status, current.error = "failed", error
        current.save()


@transaction.atomic
def retry(workspace, brain_id, feed_id, *, content=None):
    BrainService.get(workspace, brain_id, lock=True)
    feed = get_feed(workspace, brain_id, feed_id, lock=True)
    if feed.channel == "sync":
        raise ApiError("invalid_input", "Sync proposals come from GitHub. Pull again from GitHub sync.")
    if feed.status not in {"pending", "failed"} or feed.claim_id:
        raise ApiError("feed_changed", "This feed is already processing or reviewed.", 409)
    if content is not None:
        from .intake import MAX_PAYLOAD
        from .paths import utf8_size

        if (
            sum(
                utf8_size(value)
                for key, value in {**feed.raw_payload, "content": content}.items()
                if key != "source_kind"
            )
            > MAX_PAYLOAD
        ):
            raise ApiError("too_large", "Source exceeds 512 KB.", 413)
        feed.raw_payload = {**feed.raw_payload, "content": content}
    feed.status, feed.retries, feed.error, feed.queued_at = "pending", 0, "", timezone.now()
    feed.save()
    transaction.on_commit(lambda: enqueue(workspace.pk, feed.pk))
    return feed


def reconcile():
    """Heartbeat task: scope each recovery to its own workspace and brain lock."""
    cutoff = timezone.now() - timedelta(minutes=15)
    rows = BrainFeed.objects.filter(
        status__in=["approving", "extracting", "pending"], updated_at__lt=cutoff
    ).values_list("brain__workspace_id", "brain_id", "pk")[:200]
    count = 0
    for workspace_id, brain_id, feed_id in list(rows):
        with transaction.atomic():
            workspace = Workspace.objects.filter(pk=workspace_id).first()
            if (
                not workspace
                or not Brain.objects.for_workspace(workspace).filter(pk=brain_id).exists()
            ):
                continue
            BrainService.get(workspace, brain_id, lock=True)
            feed = get_feed(workspace, brain_id, feed_id, lock=True)
            if feed.updated_at >= cutoff or feed.status not in {
                "approving",
                "extracting",
                "pending",
            }:
                continue
            applied = BrainRevision.objects.filter(
                brain_id=brain_id, brain__workspace=workspace, feed=feed
            ).first()
            if applied:
                feed.status, feed.revision = "applied", applied
            elif feed.status == "approving":
                feed.status, feed.needs_review, feed.error = (
                    "proposed",
                    True,
                    "Interrupted approval recovered. Review before applying.",
                )
            elif feed.retries >= MAX_ATTEMPTS:
                feed.status, feed.error = (
                    "failed",
                    "The worker did not finish after three attempts. Retry from the inbox.",
                )
            elif feed.queued_at is not None:
                feed.status, feed.queued_at = "pending", timezone.now()
                transaction.on_commit(lambda ws=workspace_id, pk=feed_id: enqueue(ws, pk))
            feed.claim_id, feed.claimed_at = None, None
            feed.save()
            count += 1
    return count
