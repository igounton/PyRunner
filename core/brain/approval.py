"""Human decisions and the single transactional feed writer."""

import hashlib
import json
import re
from dataclasses import dataclass

import yaml
from django.db import transaction
from django.utils import timezone

from core.api.exceptions import ApiError
from core.models import BrainFeed, BrainRevision
from core.services.library_service import diff_module_maps
from .indexer import parse_frontmatter
from .proposal import validate_shape
from .service import BrainService
from .validator import context_from_files, description_from_content, validate


def get_feed(workspace, brain_id, feed_id, *, lock=False):
    scope = BrainFeed.objects.filter(brain_id=brain_id, brain__workspace=workspace)
    if lock:
        scope = scope.select_for_update()
    feed = scope.filter(pk=feed_id).first()
    if feed is None:
        raise ApiError("not_found", "Unknown feed.", 404)
    return feed


def digest(feed):
    value = [str(feed.base_revision_id), feed.proposal, feed.updated_at.isoformat()]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _frontmatter(content, changes):
    metadata, body, malformed = parse_frontmatter(content.lstrip("\ufeff"))
    if malformed or not content.lstrip("\ufeff").startswith("---"):
        raise ApiError("invalid_proposal", "A touched note has invalid frontmatter.")
    metadata.update(changes)
    return (
        "---\n"
        + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).rstrip()
        + "\n---\n"
        + body
    )


def proposed_files(before, proposal, context):
    """Include generated supersession, descriptions and taxonomy in the review diff."""
    after = dict(before)
    for row in proposal["files"]:
        content = row["content"]
        if row["path"].startswith(("knowledge/", "projects/")):
            fm, _, _ = parse_frontmatter(content.lstrip("\ufeff"))
            if not fm.get("description"):
                content = _frontmatter(content, {"description": description_from_content(content)})
        after[row["path"]] = content
    for row in proposal.get("supersedes", []):
        path = context.entities[row["old_entity_id"]]["path"]
        after[path] = _frontmatter(
            after[path], {"status": "superseded", "superseded_by": row["new_entity_id"]}
        )
    additions = sorted(set(proposal.get("taxonomy_additions", [])) - context.taxonomy)
    if additions:
        contract = after.get("CLAUDE.md", "")
        heading = re.search(r"^##.*Topic taxonomy.*$", contract, re.MULTILINE)
        if heading is None:
            raise ApiError(
                "invalid_proposal", "Add a Topic taxonomy section to CLAUDE.md before extending it."
            )
        after["CLAUDE.md"] = (
            contract[: heading.end()]
            + "\n\n"
            + "`"
            + ", ".join(additions)
            + "`\n"
            + contract[heading.end() :]
        )
    return after


@dataclass
class Review:
    feed: BrainFeed
    validation: object
    files: dict
    changes: list
    digest: str


def review(workspace, brain_id, feed_id):
    feed = get_feed(workspace, brain_id, feed_id)
    before = BrainService.files(workspace, brain_id, feed.base_revision_id)
    ctx = context_from_files(before, source_kind=feed.raw_payload.get("source_kind", ""))
    verdict = validate(feed.proposal, ctx)
    after = before
    if verdict.valid:
        after = proposed_files(before, feed.proposal, ctx)
    changes = [item for item in diff_module_maps(before, after) if item["status"] != "unchanged"]
    return Review(feed, verdict, after, changes, digest(feed))


@transaction.atomic
def edit_proposal(workspace, brain_id, feed_id, proposal, *, expected_digest):
    BrainService.get(workspace, brain_id, lock=True)
    feed = get_feed(workspace, brain_id, feed_id, lock=True)
    if feed.status not in {"pending", "proposed", "failed"} or digest(feed) != expected_digest:
        raise ApiError("feed_changed", "The feed changed. Reload before editing.", 409)
    validate_shape(proposal)
    feed.proposal, feed.proposal_edited = proposal, True
    feed.status, feed.error = "proposed", ""
    feed.save()
    return feed


@transaction.atomic
def approve(workspace, brain_id, feed_id, *, expected_digest, user, note=""):
    brain = BrainService.get(workspace, brain_id, lock=True)
    feed = get_feed(workspace, brain_id, feed_id, lock=True)
    applied = BrainRevision.objects.filter(
        brain=brain, brain__workspace=workspace, feed=feed
    ).first()
    if applied:
        feed.status, feed.revision = "applied", applied
        feed.save()
        return feed
    if feed.status != "proposed" or digest(feed) != expected_digest:
        raise ApiError("feed_changed", "The feed changed. Reload and review it again.", 409)
    current = review(workspace, brain_id, feed_id)
    if not current.validation.valid:
        raise ApiError("invalid_proposal", "Fix the validation errors before approving.")
    if not current.changes:
        raise ApiError("empty_proposal", "This proposal changes no files. Edit or reject it.")
    before = BrainService.files(workspace, brain_id, feed.base_revision_id)
    head = BrainService.files(workspace, brain_id)
    touched = {item["filename"] for item in current.changes}
    if brain.head_revision_id != feed.base_revision_id and any(
        before.get(path) != head.get(path) for path in touched
    ):
        feed.base_revision = brain.head_revision
        feed.needs_review = True
        feed.error = "The reviewed files changed. Review the updated diff before approving again."
        feed.save()
        return feed
    ctx = context_from_files(head, source_kind=feed.raw_payload.get("source_kind", ""))
    verdict = validate(feed.proposal, ctx)
    if not verdict.valid:
        feed.base_revision, feed.needs_review = brain.head_revision, True
        feed.error = "The brain's rules or notes changed. Review and fix the new validation errors."
        feed.save()
        return feed
    files = proposed_files(head, feed.proposal, ctx)
    feed.status = "approving"
    feed.save(update_fields=["status", "updated_at"])
    revision = BrainService.commit(
        workspace,
        brain_id,
        files,
        expected_head=brain.head_revision_id,
        message=f"Feed: {feed.source_id}"[:500],
        author=user,
        source="feed",
        feed=feed,
    )
    feed.status, feed.revision = "applied", revision
    feed.decided_by, feed.decided_at, feed.decision_note = user, timezone.now(), note[:4000]
    feed.needs_review, feed.error = False, ""
    feed.save()
    return feed


@transaction.atomic
def reject(workspace, brain_id, feed_id, *, expected_digest, user, note=""):
    BrainService.get(workspace, brain_id, lock=True)
    feed = get_feed(workspace, brain_id, feed_id, lock=True)
    if feed.status in {"applied", "approving", "rejected"} or digest(feed) != expected_digest:
        raise ApiError("feed_changed", "The feed changed. Reload before rejecting.", 409)
    feed.status, feed.claim_id = "rejected", None
    feed.decided_by, feed.decided_at, feed.decision_note = user, timezone.now(), note[:4000]
    feed.save()
    return feed
