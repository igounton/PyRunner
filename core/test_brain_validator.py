"""BrainOutside rule parity plus DB-manifest and contract 1.1 gates."""

from django.test import SimpleTestCase

from core.brain import validator as v


def note_file(identifier="take-example", **changes):
    metadata = {
        "id": identifier,
        "type": "take",
        "status": "current",
        "visibility": "agents-only",
        "date": "2026-10",
        "source": "raw/source.md",
        "source_url": "https://example.com/source",
        "topics": "[ai]",
        "description": "A description",
    }
    metadata.update(changes)
    content = "---\n" + "\n".join(f"{key}: {value}" for key, value in metadata.items())
    content += "\n---\n# Example\n> VERBATIM: The source's words.\n"
    return {"path": f"knowledge/takes/{identifier}.md", "action": "create", "content": content}


class ValidatorTests(SimpleTestCase):
    def check_rules(self, file, expected=(), **context):
        result = v.validate({"files": [file]}, v.ValidationContext(taxonomy={"ai"}, **context))
        self.assertEqual({e.rule for e in result.violations}, set(expected), str(result.violations))
        return result

    def test_source_rules_one_through_four_and_thought_exception(self):
        self.check_rules(note_file())
        self.check_rules(note_file(id="wrong"), {1})
        self.check_rules(note_file(type="fact"), {1})
        self.check_rules(note_file(status="superseded"), {1})
        self.check_rules(note_file(source=""), {2})
        self.check_rules(note_file(source_url=""), {2})
        self.check_rules(note_file(source_url=""), source_kind="thought")
        self.check_rules(note_file(topics="[unknown]"), {3})
        file = note_file()
        file["content"] = file["content"].replace("> VERBATIM:", "Quote:")
        self.check_rules(file, {4})

    def test_source_private_quote_rule_uses_normalized_long_lines(self):
        private = "This sentence is private and must never be quoted in any new proposal."
        file = note_file()
        file["content"] += private.upper()
        self.check_rules(file, {8}, private_texts=[private])
        self.check_rules(note_file(), private_texts=["Short"])

    def test_source_blocked_scripts_are_owner_policy(self):
        raw = {"path": "raw/source.md", "action": "create", "content": "هذه ملاحظة"}
        self.check_rules(raw)
        self.check_rules(raw, {6}, blocked_scripts=("arabic",))
        self.check_rules(raw, blocked_scripts=("hebrew",))
        for content in ("", "<!-- Scripts: `arabic` -->", "The choices are `arabic`, `hebrew`."):
            self.assertEqual(
                v.blocked_scripts_from_claude_md("## Blocked scripts\n" + content), ((), ())
            )
        self.assertEqual(
            v.blocked_scripts_from_claude_md("## Blocked scripts\nScripts: `arabic, arabik`"),
            (("arabic",), ("arabik",)),
        )
        result = self.check_rules(raw, unknown_scripts=("arabik",))
        self.assertIn("NOT blocked", " ".join(result.warnings))

    def test_unterminated_span_is_a_validation_error(self):
        file = note_file()
        file["content"] += "(agents-only: never closed"
        self.check_rules(file, {10})

    def test_portable_paths_and_duplicate_proposal_paths_are_rejected(self):
        for path in ("raw/evil\\note.md", "raw/CON.md", "raw/note.md."):
            self.check_rules({"path": path, "action": "create", "content": "text"}, {5})
        result = v.validate(
            {"files": [note_file(), note_file()]}, v.ValidationContext(taxonomy={"ai"})
        )
        self.assertIn(5, {e.rule for e in result.violations})

    def test_malformed_shape_is_reported_without_crashing(self):
        for proposal in ([], {"files": [None]}, {"files": "bad"}, {"files": [], "index_lines": []}):
            with self.subTest(proposal=proposal):
                self.assertFalse(v.validate(proposal, v.ValidationContext()).valid)
