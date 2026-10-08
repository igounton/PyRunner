"""Workspace brains: immutable content/revisions and a rebuildable entity index."""

import hashlib
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from .workspace import WorkspaceScopedManager

TIERS = [("public", "Public"), ("agents-only", "Agents only"), ("private", "Private")]


class Brain(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("Workspace", on_delete=models.CASCADE, related_name="brains")
    slug = models.SlugField(max_length=64)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    owner_plugin = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    owner_key = models.CharField(max_length=100, null=True, blank=True)
    contract_version = models.CharField(max_length=32, default="1.1", blank=True)
    legacy_descriptions = models.JSONField(default=dict, blank=True)
    head_revision = models.ForeignKey(
        "BrainRevision", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    ai_provider = models.ForeignKey("AIProvider", null=True, blank=True, on_delete=models.SET_NULL)
    reader_model = models.CharField(max_length=100, blank=True)
    feeder_model = models.CharField(max_length=100, blank=True)
    daily_cost_cap_usd = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    include_in_backup = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "brains"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "slug"], name="uniq_brain_ws_slug"),
            models.UniqueConstraint(
                fields=["workspace", "owner_plugin", "owner_key"], name="uniq_brain_owner_key"
            ),
            models.CheckConstraint(
                condition=models.Q(daily_cost_cap_usd__isnull=True)
                | models.Q(daily_cost_cap_usd__gte=0),
                name="brain_nonnegative_cap",
            ),
        ]

    def __str__(self):
        return self.name


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        # User deletion may anonymize a revision without changing its content.
        if self.model.__name__ == "BrainRevision" and kwargs == {"author": None}:
            return super().update(**kwargs)
        raise ValidationError("Brain content and revisions are immutable.")

    def delete(self):
        raise ValidationError("Delete the brain to remove its revision history.")


class ImmutableRecord(models.Model):
    objects = ImmutableQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Brain content and revisions are immutable.")
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Delete the brain to remove its revision history.")


class BrainBlob(ImmutableRecord):
    brain = models.ForeignKey(Brain, on_delete=models.CASCADE, related_name="blobs")
    sha256 = models.CharField(max_length=64)
    content = models.TextField()
    size = models.PositiveIntegerField()

    class Meta:
        db_table = "brain_blobs"
        constraints = [
            models.UniqueConstraint(fields=["brain", "sha256"], name="uniq_brain_blob_sha")
        ]

    def save(self, *args, **kwargs):
        encoded = self.content.encode("utf-8")
        if self.sha256 != hashlib.sha256(encoded).hexdigest() or self.size != len(encoded):
            raise ValidationError("Brain blob hash or size does not match its content.")
        super().save(*args, **kwargs)


class BrainRevision(ImmutableRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    brain = models.ForeignKey(Brain, on_delete=models.CASCADE, related_name="revisions")
    number = models.PositiveIntegerField()
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    manifest = models.JSONField(default=dict)
    message = models.CharField(max_length=500)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    source = models.CharField(
        max_length=16, choices=[(v, v) for v in ("seed", "import", "edit", "feed", "sync")]
    )
    feed = models.OneToOneField(
        "BrainFeed", null=True, blank=True, on_delete=models.SET_NULL, related_name="applied_revision"
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = "brain_revisions"
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["brain", "number"], name="uniq_brain_revision_number")
        ]


class BrainEntity(models.Model):
    KINDS = [
        (k, k)
        for k in ("identity", "project", "take", "story", "lesson", "fact", "lens", "catalog")
    ]
    brain = models.ForeignKey(Brain, on_delete=models.CASCADE, related_name="entities")
    revision = models.ForeignKey(BrainRevision, on_delete=models.CASCADE)
    entity_id = models.CharField(max_length=200)
    kind = models.CharField(max_length=16, choices=KINDS)
    path = models.CharField(max_length=300)
    title = models.CharField(max_length=300, blank=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=64, default="current", blank=True)
    superseded_by = models.CharField(max_length=200, blank=True)
    visibility = models.CharField(max_length=16, choices=TIERS)
    topics = models.JSONField(default=list, blank=True)
    projects = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=300, blank=True)
    source_url = models.CharField(max_length=500, blank=True)
    date = models.CharField(max_length=10, blank=True)
    last_verified = models.DateField(null=True, blank=True)
    content_sha = models.CharField(max_length=64)
    malformed_frontmatter = models.BooleanField(default=False)
    indexed_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "brain_entities"
        ordering = ["path"]
        constraints = [
            models.UniqueConstraint(fields=["brain", "entity_id"], name="uniq_brain_entity_id"),
            models.UniqueConstraint(fields=["brain", "path"], name="uniq_brain_entity_path"),
        ]
        indexes = [
            models.Index(fields=["brain", "visibility", "kind"], name="brain_entity_tier_kind")
        ]

    @property
    def is_stale(self):
        from datetime import timedelta

        return self.kind == "project" and (
            self.last_verified is None
            or self.last_verified < timezone.localdate() - timedelta(days=45)
        )


class BrainGrant(models.Model):
    SUBJECTS = [(s, s.title()) for s in ("script", "plugin", "agent", "token")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    brain = models.ForeignKey(Brain, on_delete=models.CASCADE, related_name="grants")
    subject_kind = models.CharField(max_length=16, choices=SUBJECTS)
    subject_id = models.CharField(max_length=200)
    max_tier = models.CharField(max_length=16, choices=TIERS, default="public")
    can_propose = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "brain_grants"
        constraints = [
            models.UniqueConstraint(
                fields=["brain", "subject_kind", "subject_id"], name="uniq_brain_grant"
            ),
            models.CheckConstraint(
                condition=models.Q(max_tier__in=[t[0] for t in TIERS]),
                name="brain_grant_valid_tier",
            ),
            models.CheckConstraint(
                condition=models.Q(subject_kind__in=["script", "plugin", "agent", "token"]),
                name="brain_grant_valid_subject",
            ),
        ]


class BrainFeed(models.Model):
    """Captured source and reviewed proposal; only approval creates a revision."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        EXTRACTING = "extracting", "Extracting"
        PROPOSED = "proposed", "Ready for review"
        APPROVING = "approving", "Applying"
        APPLIED = "applied", "Applied"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    brain = models.ForeignKey(Brain, on_delete=models.CASCADE, related_name="feeds")
    source_id = models.CharField(max_length=120)
    channel = models.CharField(max_length=16, choices=[
        (value, value) for value in ("console", "api", "mcp", "script", "plugin", "agent", "telegram")
    ])
    principal = models.CharField(max_length=256)
    raw_payload = models.JSONField(default=dict)
    base_revision = models.ForeignKey(
        BrainRevision, null=True, blank=True, on_delete=models.SET_NULL, related_name="based_feeds"
    )
    proposal = models.JSONField(null=True, blank=True)
    proposal_edited = models.BooleanField(default=False)
    needs_review = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="brain_decisions"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    revision = models.ForeignKey(
        BrainRevision, null=True, blank=True, on_delete=models.SET_NULL, related_name="feeds_applied"
    )
    retries = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True)
    cost_usd = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claim_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "brain_feeds"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["brain", "status", "created_at"], name="brain_feed_queue")]
