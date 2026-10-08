"""BrainOutside visibility regressions ported to manifest-backed parsing."""

from django.test import SimpleTestCase

from core.brain.indexer import parse_files, parse_frontmatter
from core.brain.visibility import raw_paths


class ImportVisibilityRegressions(SimpleTestCase):
    def test_catalog_explicit_private_wins(self):
        parsed = parse_files({"content-catalog/x.md": "---\nvisibility: private\n---\n# Private"})
        self.assertEqual(parsed[0].visibility, "private")

    def test_unclosed_frontmatter_is_private(self):
        text = "---\nvisibility: private\n# Missing delimiter"
        self.assertTrue(parse_frontmatter(text)[2])
        self.assertEqual(parse_files({"lenses/x.md": text})[0].visibility, "private")

    def test_raw_link_inside_private_span_is_not_public(self):
        files = {
            "knowledge/takes/x.md": "---\nvisibility: public\n---\n# Public\n(agents-only: raw/secret.md)",
            "raw/secret.md": "Secret material",
        }
        entities = parse_files(files)
        self.assertEqual(raw_paths(files, entities, "public"), set())
        self.assertEqual(raw_paths(files, entities, "agents-only"), {"raw/secret.md"})
