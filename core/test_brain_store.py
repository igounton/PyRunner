"""Store integration and BrainOutside indexer parity on revision manifests."""

import io
import stat
import zipfile
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, SimpleTestCase
from django.utils import timezone

from core.api.errors import ApiError
from core.brain.contract import contract_version, harvest_descriptions, version_warning
from core.brain.imports import export_zip, import_zip, read_zip, github_zip_url, fetch_github
from core.brain.indexer import parse_files, parse_frontmatter, _resolve_visibility
from core.brain.paths import validate_files
from core.brain.service import BrainService
from core.brain.visibility import strip_agents_only_spans, raw_paths
from core.models import Brain, BrainBlob, BrainEntity, BrainRevision, Workspace, User, DataStore
from core.services.backup_service import BackupService


def contract_files():
    return {
        "CLAUDE.md": '---\ncontract-version: "1.0"\n---\n# Contract',
        "identity/core.md": "# Identity",
        "knowledge/takes/_TEMPLATE.md": "# Template",
        "lenses/default.md": "# Lens",
    }


def note(entity_id, visibility="agents-only", extra=""):
    return f"---\nid: {entity_id}\nvisibility: {visibility}\n{extra}---\n# {entity_id}\nText"


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        for name, value in files.items():
            info = zipfile.ZipInfo(name)
            info.filename = name  # Preserve malicious separators on Windows too.
            zipped.writestr(info, value)
    return output.getvalue()


class BrainStoreTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Brain workspace")
        self.other = Workspace.objects.create(name="Other workspace")
        self.files = contract_files()
        self.files["knowledge/takes/a.md"] = note("take-a")
        self.brain = BrainService.create(self.workspace, "test", files=self.files)

    def commit(self, files):
        self.brain.refresh_from_db()
        return BrainService.commit(
            self.workspace,
            self.brain.pk,
            files,
            expected_head=self.brain.head_revision_id,
            message="Edit",
        )

    def test_seed_uses_shipped_template_only_at_creation(self):
        brain = BrainService.create(self.workspace, "seed")
        self.assertEqual(brain.contract_version, "1.1")
        self.assertEqual(brain.head_revision.number, 1)
        self.assertTrue(brain.entities.filter(kind="identity").exists())
        with patch("pathlib.Path.read_text", side_effect=AssertionError("Runtime read of seed")):
            self.assertIn("CLAUDE.md", BrainService.files(self.workspace, brain.pk))

    def test_blob_dedup_and_noop(self):
        count = self.brain.blobs.count()
        files = {**self.files, "raw/duplicate.md": self.files["identity/core.md"]}
        revision = self.commit(files)
        self.assertEqual(revision.number, 2)
        self.assertEqual(self.brain.blobs.count(), count)
        self.assertEqual(self.commit(files).pk, revision.pk)
        self.assertEqual(self.brain.revisions.count(), 2)

    def test_one_changed_blob_and_old_revision_unchanged(self):
        original = self.brain.head_revision_id
        count = self.brain.blobs.count()
        self.commit({**self.files, "identity/core.md": "# Updated"})
        self.assertEqual(self.brain.blobs.count(), count + 1)
        self.assertEqual(BrainService.files(self.workspace, self.brain.pk, original), self.files)

    def test_rename_keeps_id_and_cross_folder_changes_kind(self):
        files = dict(self.files)
        files["knowledge/lessons/renamed.md"] = files.pop("knowledge/takes/a.md")
        self.commit(files)
        obj = self.brain.entities.get(entity_id="take-a")
        self.assertEqual(obj.path, "knowledge/lessons/renamed.md")
        self.assertEqual(obj.kind, "lesson")

    def test_id_swap_and_adoption(self):
        files = {**self.files, "knowledge/takes/b.md": note("take-b")}
        self.commit(files)
        files["knowledge/takes/a.md"], files["knowledge/takes/b.md"] = (
            files["knowledge/takes/b.md"],
            files["knowledge/takes/a.md"],
        )
        revision = self.commit(files)
        self.assertEqual(self.brain.entities.get(entity_id="take-a").path, "knowledge/takes/b.md")
        self.assertEqual(
            set(self.brain.entities.values_list("revision_id", flat=True)), {revision.pk}
        )
        files["knowledge/takes/b.md"] = note("take-c")
        files["knowledge/takes/new.md"] = note("take-a")
        self.commit(files)
        self.assertEqual(self.brain.entities.get(entity_id="take-a").path, "knowledge/takes/new.md")

    def test_duplicate_id_rolls_back_all_tables(self):
        before = self.brain.head_revision_id, self.brain.blobs.count(), self.brain.entities.count()
        with self.assertRaisesMessage(ApiError, "duplicate frontmatter id"):
            self.commit({**self.files, "knowledge/takes/b.md": note("take-a", "public")})
        self.brain.refresh_from_db()
        self.assertEqual(
            (self.brain.head_revision_id, self.brain.blobs.count(), self.brain.entities.count()),
            before,
        )
        self.assertEqual(self.brain.revisions.count(), 1)

    def test_delete_prunes_index(self):
        files = dict(self.files)
        del files["knowledge/takes/a.md"]
        self.commit(files)
        self.assertFalse(self.brain.entities.filter(entity_id="take-a").exists())

    def test_full_rebuild_recovers_index_and_counts_noop(self):
        unchanged = BrainService.rebuild_index(self.workspace, self.brain.pk)
        self.assertEqual((unchanged.added, unchanged.changed, unchanged.removed), (0, 0, 0))
        self.brain.entities.all().delete()
        rebuilt = BrainService.rebuild_index(self.workspace, self.brain.pk)
        self.assertEqual(rebuilt.added, 3)

    def test_diff_and_stale_edit(self):
        old = self.brain.head_revision_id
        revision = self.commit({**self.files, "identity/core.md": "# Changed\n"})
        diff = BrainService.diff(self.workspace, self.brain.pk, revision.pk)
        self.assertEqual([d["path"] for d in diff], ["identity/core.md"])
        self.assertIn("+# Changed", diff[0]["diff"])
        with self.assertRaises(ApiError) as caught:
            BrainService.edit(
                self.workspace,
                self.brain.pk,
                "identity/core.md",
                "Lost edit",
                expected_head=old,
                message="Conflict",
            )
        self.assertEqual(caught.exception.status, 409)

    def test_diff_separates_lines_without_final_newline(self):
        revision = self.commit({**self.files, "identity/core.md": "# Changed"})
        diff = BrainService.diff(self.workspace, self.brain.pk, revision.pk)[0]["diff"]
        self.assertIn("-# Identity\n", diff)
        self.assertIn("\n+# Changed", diff)

    def test_cross_workspace_reads_edits_and_revision_ids_are_unknown(self):
        foreign = BrainService.create(self.other, "other", files=contract_files())
        calls = [
            lambda: BrainService.files(self.other, self.brain.pk),
            lambda: BrainService.files(self.workspace, self.brain.pk, foreign.head_revision_id),
            lambda: BrainService.diff(self.workspace, self.brain.pk, foreign.head_revision_id),
            lambda: BrainService.edit(
                self.other, self.brain.pk, "identity/core.md", "x", expected_head=None, message="x"
            ),
        ]
        for call in calls:
            with self.subTest(call=call), self.assertRaises(ApiError) as caught:
                call()
            self.assertEqual(caught.exception.status, 404)

    def test_immutable_rows_and_brain_cascade(self):
        revision = self.brain.head_revision
        blob = self.brain.blobs.first()
        for obj in (revision, blob):
            with self.assertRaises(ValidationError):
                obj.save()
            with self.assertRaises(ValidationError):
                type(obj).objects.filter(pk=obj.pk).update(
                    **({"message": "changed"} if obj == revision else {"content": "changed"})
                )
            with self.assertRaises(ValidationError):
                obj.delete()
        self.brain.delete()
        self.assertFalse(BrainRevision.objects.filter(pk=revision.pk).exists())
        self.assertFalse(BrainBlob.objects.filter(pk=blob.pk).exists())

    def test_author_deletion_preserves_revision(self):
        user = User.objects.create_user(username="author", email="author@example.test")
        revision = BrainService.edit(
            self.workspace,
            self.brain.pk,
            "identity/core.md",
            "# Edited",
            expected_head=self.brain.head_revision_id,
            message="Author",
            author=user,
        )
        user.delete()
        revision.refresh_from_db()
        self.assertIsNone(revision.author_id)

    def test_new_object_cannot_overwrite_an_existing_immutable_pk(self):
        from django.db import IntegrityError, transaction

        blob = self.brain.blobs.first()
        clone = BrainBlob(
            pk=blob.pk, brain=blob.brain, sha256=blob.sha256, content=blob.content, size=blob.size
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            clone.save()

    def test_unknown_contract_version_imports_with_warning(self):
        files = {**contract_files(), "CLAUDE.md": "# Contract without version"}
        brain = BrainService.create(self.workspace, "unknown-version", files=files)
        self.assertEqual(brain.contract_version, "")
        self.assertIn("unknown", version_warning(brain.contract_version))

    def test_import_10_is_byte_preserved_and_descriptions_survive_rebuild(self):
        files = dict(self.files)
        files["INDEX.md"] = "- `take-a` — Old description [current] [public]\n"
        brain = import_zip(self.workspace, "import", archive(files))
        self.assertEqual(brain.contract_version, "1.0")
        self.assertEqual(BrainService.files(self.workspace, brain.pk), files)
        self.assertEqual(read_zip(export_zip(self.workspace, brain.pk)), files)
        BrainService.rebuild_index(self.workspace, brain.pk)
        self.assertEqual(brain.entities.get(entity_id="take-a").description, "Old description")
        self.assertEqual(brain.entities.get(entity_id="take-a").visibility, "agents-only")

    def test_staleness_boundary(self):
        today = timezone.localdate()
        obj = BrainEntity(kind="project", last_verified=today - timedelta(days=45))
        self.assertFalse(obj.is_stale)
        obj.last_verified -= timedelta(days=1)
        self.assertTrue(obj.is_stale)
        obj.last_verified = None
        self.assertTrue(obj.is_stale)
        obj.kind = "take"
        self.assertFalse(obj.is_stale)


class BrainIndexerParityTests(SimpleTestCase):
    def test_all_dialects_defaults_and_explicit_tiers(self):
        defaults = {
            "identity/a.md": "agents-only",
            "projects/a.md": "agents-only",
            "knowledge/takes/a.md": "agents-only",
            "lenses/a.md": "public",
            "content-catalog/a.md": "public",
        }
        for path, default in defaults.items():
            for tier in (None, "invalid", "public", "agents-only", "private"):
                with self.subTest(path=path, tier=tier):
                    text = "# Note" if tier is None else f"---\nvisibility: {tier}\n---\n# Note"
                    self.assertEqual(
                        parse_files({path: text})[0].visibility,
                        tier if tier in {"public", "agents-only", "private"} else default,
                    )
        self.assertEqual(
            parse_files(
                {
                    "unknown.md": "# Note",
                    "knowledge/takes/_TEMPLATE.md": "# Template",
                    "content-catalog/README.md": "# Readme",
                }
            ),
            [],
        )

    def test_frontmatter_parity_and_bom(self):
        for block in ('visibility: "unterminated', "\tvisibility: public", "- public", "hello"):
            text = f"---\n{block}\n---\n# Body"
            self.assertTrue(parse_frontmatter(text)[2])
            for default in ("public", "agents-only", "private"):
                self.assertEqual(_resolve_visibility("public", default, malformed=True), "private")
        text = "\ufeff---\nvisibility: private\n---\n# Body"
        self.assertEqual(parse_files({"lenses/a.md": text})[0].visibility, "private")
        self.assertFalse(parse_frontmatter("# No frontmatter")[2])

    def test_recursive_alias_and_deep_yaml_fail_closed(self):
        for block in ("value: &a [*a]\nvisibility: public", "value: " + "[" * 600 + "]" * 600):
            with self.subTest(block=block[:25]):
                self.assertTrue(parse_frontmatter(f"---\n{block}\n---\n# Body")[2])

    def test_schema_field_limits_and_skip_one_bad(self):
        text = (
            note("take-a", extra=f"type: {'x' * 20}\nsource_url: {'s' * 510}\ndate: January 2026\n")
            + "\n# "
            + "T" * 400
        )
        entity = parse_files({"knowledge/takes/a.md": text})[0]
        self.assertEqual(len(entity.kind), 16)
        self.assertEqual(len(entity.source_url), 500)
        self.assertEqual(entity.date, "January 20")
        long_title = parse_files({"identity/a.md": "# " + "T" * 400})[0]
        self.assertEqual(len(long_title.title), 300)
        parsed = parse_files(
            {
                "knowledge/takes/a.md": note("x" * 201),
                "identity/good.md": "# Good",
                "projects/" + "x" * 300 + ".md": "# Too long",
            }
        )
        self.assertEqual([e.path for e in parsed], ["identity/good.md"])

    def test_span_port_nested_multiple_multiline_unterminated(self):
        for text, expected in [
            ("before (agents-only: secret (nested)) after", "before  after"),
            ("(agents-only: one) middle (agents-only: two)", " middle "),
            ("before (agents-only: secret\nmore)", "before "),
            ("before (agents-only: secret (nested)", "before "),
            ("normal (parentheses)", "normal (parentheses)"),
            ("agents-only: plain", "agents-only: plain"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(strip_agents_only_spans(text, "fixture.md"), expected)

    def test_raw_links_contained_and_private_includes_nested(self):
        files = {
            "knowledge/takes/a.md": note("a", "public")
            + "\nraw/a.md raw/nested/b.md raw/../secret.md raw/missing.md",
            "raw/a.md": "A",
            "raw/nested/b.md": "B",
            "raw/unlinked.md": "C",
            "raw/README.md": "Readme",
            "secret.md": "Secret",
        }
        parsed = parse_files(files)
        self.assertEqual(raw_paths(files, parsed, "public"), {"raw/a.md", "raw/nested/b.md"})
        self.assertEqual(
            raw_paths(files, parsed, "private"), {"raw/a.md", "raw/nested/b.md", "raw/unlinked.md"}
        )
        self.assertEqual(raw_paths({}, [], "public"), set())

    def test_contract_versions_are_advisory_and_all_description_formats(self):
        files = contract_files()
        for version in ("1.0", "1.1", "99.0", "unparseable"):
            files["CLAUDE.md"] = f'---\ncontract-version: "{version}"\n---\n'
            self.assertEqual(contract_version(files), version)
            self.assertEqual(bool(version_warning(version)), version != "1.1")
        self.assertEqual(contract_version({}), "")
        for line in (
            "- `identity-core` — Description [current] [public]",
            "- [identity] Core — Description | path: identity/core.md",
            "- [Core](identity/core.md) — Description",
        ):
            files["INDEX.md"] = line
            self.assertEqual(harvest_descriptions(files), {"identity/core.md": "Description"})


class BrainArchiveTests(SimpleTestCase):
    def test_wrapper_directory_and_no_disk_extraction(self):
        files = contract_files()
        self.assertEqual(read_zip(archive({"repo-main/" + p: c for p, c in files.items()})), files)

    def test_traversal_absolute_windows_and_case_collisions(self):
        for path in (
            "../x.md",
            "/x.md",
            "raw/../../x.md",
            "raw\\x.md",
            "C:/x.md",
            "con.md",
            "x:stream",
            "raw//x.md",
            "raw/x. ",
        ):
            with self.subTest(path=path), self.assertRaises(ApiError):
                read_zip(archive({**contract_files(), path: "Bad"}))
        with self.assertRaises(ApiError):
            read_zip(archive({**contract_files(), "RAW/a.md": "a", "raw/A.md": "b"}))

    def test_symlink_and_special_file_rejected(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as zipped:
                info = zipfile.ZipInfo("identity/linked.md")
                info.create_system = 3
                info.external_attr = (mode | 0o777) << 16
                zipped.writestr(info, "../../outside")
            with self.subTest(mode=mode), self.assertRaises(ApiError):
                read_zip(output.getvalue())

    def test_limits_and_binary(self):
        for files in (
            {**contract_files(), "identity/large.md": "x" * (1024 * 1024 + 1)},
            {**contract_files(), "raw/binary.md": b"\xff"},
            {**contract_files(), "raw/nul.md": "\x00"},
        ):
            with self.assertRaises(ApiError):
                read_zip(archive(files))
        with self.assertRaises(ApiError):
            read_zip(b"not zip")
        with self.assertRaises(ApiError):
            validate_files({f"raw/{n}.md": "" for n in range(20_001)})

    def test_github_https_and_netguard(self):
        self.assertEqual(
            github_zip_url("https://github.com/owner/repo.git"),
            "https://api.github.com/repos/owner/repo/zipball",
        )
        for url in (
            "http://github.com/o/r",
            "https://evil.example/o/r",
            "https://user@github.com/o/r",
            "https://github.com/o/r/tree/main",
        ):
            with self.subTest(url=url), self.assertRaises(ApiError):
                github_zip_url(url)
        with (
            patch("core.brain.imports.is_safe_endpoint_url", return_value=(False, "Blocked")),
            patch("urllib.request.OpenerDirector.open") as opened,
        ):
            with self.assertRaises(ApiError):
                fetch_github("https://github.com/o/r")
            opened.assert_not_called()


class RestoreRollbackRegression(TestCase):
    def test_non_database_error_rolls_back_full_replace(self):
        ws = Workspace.objects.create(name="Rollback")
        item = DataStore.objects.create(workspace=ws, name="Preserve")
        with patch.object(
            BackupService, "_import_global_settings", side_effect=ValueError("Invalid backup")
        ):
            result = BackupService.restore_backup({})
        self.assertFalse(result["success"])
        self.assertTrue(DataStore.objects.filter(pk=item.pk).exists())
