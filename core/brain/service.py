"""Transactional, workspace-scoped operations on the canonical Brain store."""

import hashlib
import logging
from pathlib import Path

from django.db import transaction

from core.api.errors import ApiError
from core.models import Brain, BrainBlob, BrainRevision
from .contract import harvest_descriptions, validate_contract
from .indexer import DuplicateEntityId, rebuild
from .paths import validate_files, validate_path

log = logging.getLogger(__name__)
TEMPLATE = Path(__file__).with_name("template")


class BrainService:
    @staticmethod
    def get(workspace, brain_id, *, lock=False):
        scope = Brain.objects.for_workspace(workspace)
        if lock:
            scope = scope.select_for_update()
        brain = scope.filter(pk=brain_id).first()
        if brain is None:
            raise ApiError("not_found", "Unknown brain.", 404)
        return brain

    @staticmethod
    def files(workspace, brain_id, revision_id=None):
        brain = BrainService.get(workspace, brain_id)
        revision = BrainRevision.objects.filter(
            brain=brain, brain__workspace=workspace, pk=revision_id or brain.head_revision_id
        ).first()
        if revision is None:
            if revision_id is None and brain.head_revision_id is None:
                return {}
            raise ApiError("not_found", "Unknown revision.", 404)
        blobs = dict(
            BrainBlob.objects.filter(
                brain=brain, brain__workspace=workspace, sha256__in=set(revision.manifest.values())
            ).values_list("sha256", "content")
        )
        if set(revision.manifest.values()) - set(blobs):
            raise ApiError(
                "corrupt_revision", "Revision content is missing. Restore a complete backup.", 500
            )
        return {path: blobs[sha] for path, sha in revision.manifest.items()}

    @staticmethod
    @transaction.atomic
    def create(workspace, slug, *, name=None, author=None, files=None, **ownership):
        if set(ownership) - {"owner_plugin", "owner_key"}:
            raise TypeError("Unknown ownership fields")
        if files is None:
            files = {
                path.relative_to(TEMPLATE).as_posix(): path.read_text(encoding="utf-8")
                for path in TEMPLATE.rglob("*")
                if path.is_file()
            }
            source = "seed"
        else:
            source = "import"
        validate_files(files)
        version = validate_contract(files)
        brain = Brain(
            workspace=workspace,
            slug=slug,
            name=name or slug,
            created_by=author,
            contract_version=version,
            **ownership,
        )
        brain.legacy_descriptions = harvest_descriptions(files) if version == "1.0" else {}
        brain.full_clean()
        brain.save()
        BrainService.commit(
            workspace,
            brain.pk,
            files,
            expected_head=None,
            message="Create brain",
            author=author,
            source=source,
        )
        brain.refresh_from_db()
        return brain

    @staticmethod
    @transaction.atomic
    def commit(workspace, brain_id, files, *, expected_head, message, author=None, source="edit"):
        """Replace the manifest, with optimistic concurrency and one row lock."""
        validate_files(files)
        version = validate_contract(files)
        if not isinstance(message, str) or not message.strip() or len(message) > 500:
            raise ApiError("invalid_input", "Enter a revision message of 1–500 characters.")
        if source not in {"seed", "import", "edit", "feed", "sync"}:
            raise ApiError("invalid_input", "Unknown revision source.")
        brain = BrainService.get(workspace, brain_id, lock=True)
        if str(brain.head_revision_id or "") != str(expected_head or ""):
            raise ApiError("head_changed", "The brain changed. Reload and review your edit.", 409)
        parent = brain.head_revision
        previous = parent.manifest if parent else {}
        manifest = {
            path: hashlib.sha256(text.encode("utf-8")).hexdigest() for path, text in files.items()
        }
        if parent and manifest == previous:
            return parent
        existing = set(
            BrainBlob.objects.filter(brain=brain, brain__workspace=workspace).values_list(
                "sha256", flat=True
            )
        )
        new = {}
        for path, sha in manifest.items():
            if sha not in existing:
                text = files[path]
                new[sha] = BrainBlob(
                    brain=brain, sha256=sha, content=text, size=len(text.encode("utf-8"))
                )
        BrainBlob.objects.bulk_create(list(new.values()))
        revision = BrainRevision.objects.create(
            brain=brain,
            parent=parent,
            number=parent.number + 1 if parent else 1,
            manifest=manifest,
            message=message.strip(),
            author=author,
            source=source,
        )
        changed = {
            path
            for path in set(previous) | set(manifest)
            if previous.get(path) != manifest.get(path)
        }
        try:
            rebuild(
                brain,
                revision,
                {path: files[path] for path in changed if path in files},
                changed_paths=changed if parent else None,
            )
        except DuplicateEntityId as exc:
            raise ApiError("duplicate_entity_id", str(exc)) from exc
        brain.head_revision = revision
        brain.contract_version = version
        brain.save(update_fields=["head_revision", "contract_version", "updated_at"])
        log.info(
            "brain_revision brain=%s workspace=%s revision=%s source=%s changed=%d",
            brain.pk,
            workspace.pk,
            revision.number,
            source,
            len(changed),
        )
        return revision

    @staticmethod
    @transaction.atomic
    def edit(
        workspace, brain_id, path, content, *, expected_head, message, author=None, delete=False
    ):
        validate_path(path)
        brain = BrainService.get(workspace, brain_id, lock=True)
        files = BrainService.files(workspace, brain.pk)
        if delete:
            files.pop(path, None)
        else:
            files[path] = content
        return BrainService.commit(
            workspace, brain.pk, files, expected_head=expected_head, message=message, author=author
        )

    @staticmethod
    def diff(workspace, brain_id, revision_id):
        from core.services.library_service import diff_module_maps

        brain = BrainService.get(workspace, brain_id)
        revision = BrainRevision.objects.filter(
            brain=brain, brain__workspace=workspace, pk=revision_id
        ).first()
        if revision is None:
            raise ApiError("not_found", "Unknown revision.", 404)
        after = BrainService.files(workspace, brain.pk, revision.pk)
        before = (
            BrainService.files(workspace, brain.pk, revision.parent_id)
            if revision.parent_id
            else {}
        )
        actions = {"added": "add", "removed": "delete", "changed": "edit"}
        return [
            {
                "path": item["filename"],
                "action": actions[item["status"]],
                "diff": "\n".join(item["diff_lines"]) or "Empty file or line-ending change.",
            }
            for item in diff_module_maps(before, after)
            if item["status"] != "unchanged"
        ]

    @staticmethod
    @transaction.atomic
    def rebuild_index(workspace, brain_id):
        brain = BrainService.get(workspace, brain_id, lock=True)
        if brain.head_revision_id is None:
            return None
        return rebuild(brain, brain.head_revision, BrainService.files(workspace, brain.pk))
