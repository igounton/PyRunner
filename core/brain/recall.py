"""Optional vector recall. Every hit is re-derived from canonical tier text."""

import hashlib
import json
import logging
import time
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.api.exceptions import ApiError
from core.models import Brain, Workspace
from core.vectors.budget import BudgetSession
from core.vectors.errors import VectorError
from core.vectors.service import VectorService
from .reads import Snapshot, at_tier, note_row, snapshot
from .service import BrainService
from .visibility import TIERS, visible_at

log = logging.getLogger(__name__)
CHUNK_SIZE, CHUNK_OVERLAP = 2400, 200


def chunks(text):
    return [
        text[start : start + CHUNK_SIZE]
        for start in range(0, len(text), CHUNK_SIZE - CHUNK_OVERLAP)
        if text[start : start + CHUNK_SIZE].strip()
    ]


def points(view):
    for entity in view.entities:
        if entity.status == "superseded":
            continue
        payload = {
            "brain_id": str(view.brain.pk),
            "entity_id": entity.entity_id,
            "tier": view.tier,
            "kind": entity.kind,
            "topics": [at_tier(topic, view.tier) for topic in entity.topics],
        }
        for index, text in enumerate(chunks(view.files[entity.path])):
            chunk_payload = {**payload, "chunk": index}
            digest = hashlib.sha256(
                (json.dumps(chunk_payload, sort_keys=True) + text).encode()
            ).hexdigest()
            yield {
                "id": f"brain:{view.brain.pk}:{view.tier}:{digest}",
                "text": text,
                "payload": {**chunk_payload, "digest": digest},
            }


def _collection(brain):
    collection = brain.vector_collection
    if collection is None or collection.workspace_id != brain.workspace_id:
        raise ApiError(
            "search_not_configured",
            "Choose a vector collection in Brain settings to enable semantic search.",
            409,
        )
    return collection


def search(ctx, params):
    from .reader import unchanged

    if not params["query"].strip():
        raise ApiError("invalid_input", "Enter a search query.")
    view = snapshot(ctx, params)
    collection = _collection(view.brain)
    deadline = min(ctx.deadline or time.monotonic() + 30, time.monotonic() + 30)
    try:
        with BudgetSession(deadline) as session:
            handle = VectorService.handle(collection, session=session)
            hits = handle.search(
                params["query"],
                k=min(1000, params.get("k", 5) * 4),
                filter={"brain_id": str(view.brain.pk), "tier": view.tier},
            )
    except VectorError:
        ctx.check_budget()
        raise ApiError(
            "search_unavailable",
            "Semantic search is unavailable. Check the vector collection and embedding provider.",
            503,
        ) from None
    current = unchanged(ctx, params, view)
    if current.brain.vector_collection_id != collection.pk:
        raise ApiError("brain_changed", "Search settings changed. Run the query again.", 409)
    canonical = {p["id"]: p for p in points(view)}
    entities = {e.entity_id: e for e in view.entities}
    result, seen = [], set()
    for hit in hits:
        point = canonical.get(hit["id"])
        # Stale/deleted/retiered data, shared-collection data and arbitrary payload
        # text never leave the server. Content is read from canonical blobs.
        if point is None or hit.get("payload") != point["payload"]:
            continue
        identifier = point["payload"]["entity_id"]
        if identifier in seen:
            continue
        seen.add(identifier)
        result.append(
            {
                **note_row(entities[identifier], view.tier),
                "text": point["text"],
                "score": hit["score"],
            }
        )
        if len(result) == params.get("k", 5):
            break
    ctx.check_budget()
    return {
        "tier": view.tier,
        "hits": result,
        "index_current": view.brain.vector_revision_id == view.brain.head_revision_id,
    }


def enqueue(workspace_id, brain_id):
    from django_q.tasks import async_task

    try:
        async_task("core.brain.recall.reindex", str(workspace_id), str(brain_id))
    except Exception:
        Brain.objects.filter(pk=brain_id, workspace_id=workspace_id).update(
            vector_error="Index queue unavailable. Save Brain settings to retry."
        )
        log.exception("brain_vector_queue_failed brain=%s", brain_id)


def reindex(workspace_id, brain_id):
    workspace = Workspace.objects.filter(pk=workspace_id).first()
    if workspace is None:
        return
    claim = uuid.uuid4()
    with transaction.atomic():
        brain = (
            Brain.objects.for_workspace(workspace).select_for_update().filter(pk=brain_id).first()
        )
        if brain is None or brain.vector_collection_id is None:
            return
        if (
            brain.vector_claim
            and brain.vector_claimed_at
            and brain.vector_claimed_at > timezone.now() - timedelta(minutes=15)
        ):
            return
        collection = _collection(brain)
        brain.vector_claim, brain.vector_claimed_at = claim, timezone.now()
        brain.save(update_fields=["vector_claim", "vector_claimed_at"])
        files = BrainService.files(workspace, brain.pk)
        entities = list(brain.entities.filter(brain__workspace=workspace))
        revision = brain.head_revision_id
    error = ""
    try:
        handle = VectorService.handle(collection)
        wanted = {}
        for tier in TIERS:
            visible = [entity for entity in entities if visible_at(entity.visibility, tier)]
            view = Snapshot(
                brain, tier, visible, {e.path: at_tier(files[e.path], tier) for e in visible}
            )
            wanted.update({point["id"]: point for point in points(view)})
        existing, cursor = set(), None
        while True:
            rows, cursor = handle.scroll(
                cursor=cursor, limit=1000, filter={"brain_id": str(brain.pk)}
            )
            existing.update(row["id"] for row in rows)
            if cursor is None:
                break
        additions = [point for identifier, point in wanted.items() if identifier not in existing]
        for start in range(0, len(additions), 100):
            handle.add_texts(additions[start : start + 100])
        # A lease owner is the only index writer; changed chunks have immutable
        # ids so stale external writes cannot replace another revision's bytes.
        with transaction.atomic():
            current = BrainService.get(workspace, brain.pk, lock=True)
            if current.vector_claim != claim or current.vector_collection_id != collection.pk:
                return
            if current.head_revision_id == revision:
                stale = list(existing - set(wanted))
                for start in range(0, len(stale), 1000):
                    handle.delete(ids=stale[start : start + 1000])
                current.vector_revision_id = revision
                current.save(update_fields=["vector_revision"])
    except Exception as exc:
        error = "Semantic indexing failed. Check the vector collection/provider and save Brain settings to retry."
        log.warning(
            "brain_vector_index_failed brain=%s error_type=%s", brain_id, type(exc).__name__
        )
    finally:
        with transaction.atomic():
            current = (
                Brain.objects.for_workspace(workspace)
                .select_for_update()
                .filter(pk=brain_id, vector_claim=claim)
                .first()
            )
            if current:
                current.vector_claim, current.vector_claimed_at, current.vector_error = (
                    None,
                    None,
                    error,
                )
                current.save(update_fields=["vector_claim", "vector_claimed_at", "vector_error"])
                if not error and current.head_revision_id != revision:
                    transaction.on_commit(lambda: enqueue(workspace_id, brain_id))
