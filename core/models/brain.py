"""Workspace identity for a brain; content is stored in immutable revisions."""

import uuid

from django.db import models
from .workspace import WorkspaceScopedManager


class Brain(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("Workspace", on_delete=models.CASCADE, related_name="brains")
    slug = models.SlugField(max_length=64)
    name = models.CharField(max_length=150)
    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "brains"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "slug"], name="uniq_brain_ws_slug")
        ]

    def __str__(self):
        return self.name
