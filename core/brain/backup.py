"""Portable Brain history. Credential-bound token grants are never restored."""

import hashlib
from decimal import Decimal

from django.db import transaction
from django.utils.dateparse import parse_datetime

from core.api.errors import ApiError
from core.models import AIProvider, Brain, BrainBlob, BrainGrant, BrainRevision
from .contract import validate_contract
from .paths import validate_files
from .service import BrainService


@transaction.atomic
def export_brains():
    rows = []
    # Hold each brain across metadata/blobs/history reads. Lock only Brain on
    # PostgreSQL: provider/author joins are nullable and cannot be FOR UPDATE.
    for brain in (
        Brain.objects.filter(include_in_backup=True)
        .select_for_update(of=("self",))
        .select_related("ai_provider", "created_by")
        .order_by("id")
    ):
        rows.append(
            {
                "id": str(brain.pk),
                "workspace_id": str(brain.workspace_id),
                "slug": brain.slug,
                "name": brain.name,
                "description": brain.description,
                "owner_plugin": brain.owner_plugin,
                "owner_key": brain.owner_key,
                "contract_version": brain.contract_version,
                "legacy_descriptions": brain.legacy_descriptions,
                "head_revision_id": str(brain.head_revision_id) if brain.head_revision_id else None,
                "ai_provider_name": brain.ai_provider.name if brain.ai_provider else None,
                "reader_model": brain.reader_model,
                "feeder_model": brain.feeder_model,
                "daily_cost_cap_usd": str(brain.daily_cost_cap_usd)
                if brain.daily_cost_cap_usd is not None
                else None,
                "include_in_backup": brain.include_in_backup,
                "created_by_id": str(brain.created_by_id) if brain.created_by_id else None,
                "created_at": brain.created_at.isoformat(),
                "blobs": list(brain.blobs.values("sha256", "content", "size")),
                "revisions": [
                    {
                        "id": str(rev.pk),
                        "number": rev.number,
                        "parent_id": str(rev.parent_id) if rev.parent_id else None,
                        "manifest": rev.manifest,
                        "message": rev.message,
                        "source": rev.source,
                        "author_id": str(rev.author_id) if rev.author_id else None,
                        "created_at": rev.created_at.isoformat(),
                    }
                    for rev in brain.revisions.order_by("number")
                ],
                # Preserve the cache for inspection; restore rebuilds it from HEAD.
                "entities": [
                    {
                        key: value.isoformat()
                        if hasattr(value, "isoformat")
                        else str(value)
                        if key == "revision_id"
                        else value
                        for key, value in entity.items()
                        if key not in {"id", "brain_id"}
                    }
                    for entity in brain.entities.values()
                ],
                "grants": list(
                    brain.grants.values("subject_kind", "subject_id", "max_tier", "can_propose")
                ),
            }
        )
    return rows


def validate_brain_backup(rows):
    """Check hashes/manifests/ancestry before restore deletes any existing data."""
    if not isinstance(rows, list):
        raise ApiError("invalid_backup", "Brain backup data must be a list.")
    ids = set()
    for row in rows:
        if row["id"] in ids:
            raise ApiError("invalid_backup", "Duplicate brain in backup.")
        ids.add(row["id"])
        blobs = {}
        for blob in row["blobs"]:
            encoded = blob["content"].encode("utf-8")
            sha = hashlib.sha256(encoded).hexdigest()
            if sha != blob["sha256"] or len(encoded) != blob["size"] or sha in blobs:
                raise ApiError("invalid_backup", "Brain blob hash, size or uniqueness is invalid.")
            blobs[sha] = blob["content"]
        parent = None
        for number, revision in enumerate(row["revisions"], 1):
            if revision["number"] != number or revision["parent_id"] != parent:
                raise ApiError("invalid_backup", "Brain revision ancestry is invalid.")
            manifest = revision["manifest"]
            if not isinstance(manifest, dict) or set(manifest.values()) - set(blobs):
                raise ApiError("invalid_backup", "Brain revision references missing blobs.")
            files = {path: blobs[sha] for path, sha in manifest.items()}
            validate_files(files)
            validate_contract(files)
            parent = revision["id"]
        if row["head_revision_id"] != parent:
            raise ApiError("invalid_backup", "Brain head must be the latest revision.")


def import_brains(rows, *, workspace_for, user_map, script_map, current_user):
    warnings = []
    for row in rows:
        workspace = workspace_for(row.get("workspace_id"))
        provider_name = row.get("ai_provider_name")
        provider = AIProvider.objects.filter(name=provider_name).first() if provider_name else None
        if provider_name and provider is None:
            warnings.append(
                f"Brain {row['slug']}: configure AI provider {provider_name!r}; using the active provider until then."
            )
        brain = Brain(
            id=row["id"],
            workspace=workspace,
            slug=row["slug"],
            name=row["name"],
            description=row.get("description", ""),
            owner_plugin=row.get("owner_plugin"),
            owner_key=row.get("owner_key"),
            contract_version=row.get("contract_version", ""),
            legacy_descriptions=row.get("legacy_descriptions", {}),
            ai_provider=provider,
            reader_model=row.get("reader_model", ""),
            feeder_model=row.get("feeder_model", ""),
            daily_cost_cap_usd=Decimal(row["daily_cost_cap_usd"])
            if row.get("daily_cost_cap_usd") is not None
            else None,
            include_in_backup=row.get("include_in_backup", True),
            created_by=user_map.get(str(row.get("created_by_id"))) or current_user,
            created_at=parse_datetime(row["created_at"]),
        )
        brain.full_clean()
        brain.save()
        for blob in row["blobs"]:
            BrainBlob.objects.create(brain=brain, **blob)
        for data in row["revisions"]:
            revision = BrainRevision(
                id=data["id"],
                brain=brain,
                number=data["number"],
                parent_id=data["parent_id"],
                manifest=data["manifest"],
                message=data["message"],
                source=data["source"],
                author=user_map.get(str(data.get("author_id"))),
                created_at=parse_datetime(data["created_at"]),
            )
            revision.full_clean()
            revision.save(force_insert=True)
        brain.head_revision_id = row["head_revision_id"]
        brain.save(update_fields=["head_revision", "updated_at"])
        BrainService.rebuild_index(workspace, brain.pk)
        for data in row.get("grants", []):
            data = dict(data)
            if data["subject_kind"] == "token":
                warnings.append(
                    f"Brain {brain.slug}: re-create token grants after restoring; API tokens are not portable."
                )
                continue
            if data["subject_kind"] == "script":
                script = script_map.get(data["subject_id"])
                if script is None or script.workspace_id != workspace.pk:
                    warnings.append(
                        f"Brain {brain.slug}: omitted a grant to a missing or foreign-workspace script."
                    )
                    continue
                data["subject_id"] = str(script.pk)
            grant = BrainGrant(brain=brain, **data)
            grant.full_clean()
            grant.save()
    return len(rows), warnings
