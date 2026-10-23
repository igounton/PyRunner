"""F128: the agent CLI must not reach files or instance secrets outside its sandbox.

The Brain reader guard is a PreToolUse hook; the CLI expands a leading ``~``
to HOME, Python's Path does not, so ``~/../proc/self/environ`` used to pass the
containment check. Every claude-agent-sdk launch in core also blanks the
instance secrets (the SDK merges os.environ into the CLI's environment).
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, override_settings

from core.brain.snapshots import reader_hooks
from core.services.claude_service import ClaudeService, agent_env

SECRETS = {
    "SECRET_KEY": "s-key",
    "ENCRYPTION_KEY": "e-key",
    "DATABASE_URL": "postgres://u:p@db/x",
    "PYRUNNER_DATA_DB_URL": "postgres://u:p@data/x",
    "REDIS_URL": "redis://:p@redis:6379/0",
    "RESEND_API_KEY": "re-key",
    "OPERATOR_EXTRA": "op",
}


def _decision(hook, tool, inputs):
    result = async_to_sync(hook)({"tool_name": tool, "tool_input": inputs}, "id", None)
    return result.get("hookSpecificOutput", {}).get("permissionDecision", "allow")


class ReaderGuardHomeExpansionTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "snapshot"
        self.root.mkdir()
        (self.root / "note.md").write_text("hello", encoding="utf-8")
        self.hook = reader_hooks(self.root, set())["PreToolUse"][0].hooks[0]

    def tearDown(self):
        self.tmp.cleanup()

    def test_tilde_paths_are_denied_for_every_path_key(self):
        for raw in (
            "~/../proc/self/environ",
            "~/../etc/passwd",
            "~/.claude/.credentials.json",
            "~root/.ssh/id_rsa",
            "~",
            " ~/x",
        ):
            for tool, key in (
                ("Read", "file_path"),
                ("Grep", "path"),
                ("Glob", "path"),
                ("Read", "notebook_path"),
            ):
                with self.subTest(raw=raw, tool=tool, key=key):
                    self.assertEqual(_decision(self.hook, tool, {key: raw}), "deny")

    def test_glob_and_grep_patterns_cannot_escape(self):
        for pattern in ("~/**", "~/.claude/*", "/etc/*", "../**", "a/../../*", "\\x\\*"):
            with self.subTest(pattern=pattern):
                self.assertEqual(
                    _decision(self.hook, "Glob", {"path": ".", "pattern": pattern}), "deny"
                )
                self.assertEqual(
                    _decision(self.hook, "Grep", {"path": ".", "pattern": "x", "glob": pattern}),
                    "deny",
                )

    def test_symlink_out_of_the_snapshot_is_denied(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            os.symlink(outside, self.root / "link.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertEqual(_decision(self.hook, "Read", {"file_path": "link.md"}), "deny")

    def test_snapshot_reads_still_allowed(self):
        self.assertEqual(_decision(self.hook, "Read", {"file_path": "note.md"}), "allow")
        self.assertEqual(
            _decision(self.hook, "Read", {"file_path": str(self.root / "note.md")}), "allow"
        )
        self.assertEqual(
            _decision(self.hook, "Grep", {"path": ".", "pattern": "x", "glob": "*.md"}), "allow"
        )
        self.assertEqual(_decision(self.hook, "Glob", {"path": ".", "pattern": "**/*.md"}), "allow")


@override_settings(PYRUNNER_RUN_ENV_DENYLIST=["OPERATOR_EXTRA"])
class AgentEnvBlanksInstanceSecretsTests(SimpleTestCase):
    def assertBlanked(self, env):
        for name in SECRETS:
            with self.subTest(name=name):
                self.assertEqual(env.get(name), "", name)

    def test_agent_env_blanks_and_keeps_provider_keys(self):
        env = agent_env({"ANTHROPIC_API_KEY": "k", "SECRET_KEY": "leak"})
        self.assertBlanked(env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "k")

    def test_brain_run_agent_launch_env(self):
        from core.brain import runtime

        seen = {}

        async def drive(prompt, options, result, partial, timeout):
            seen["env"] = options.env

        with (
            patch.dict(os.environ, SECRETS),
            patch.object(
                runtime, "prepare", return_value=(Mock(), {"ANTHROPIC_API_KEY": "k"}, "", None)
            ),
            patch.object(runtime, "_drive", drive),
        ):
            try:
                runtime.run_agent(
                    None, 1, kind="reader", prompt="p", system="s", schema={"type": "object"}
                )
            except Exception:
                pass
        self.assertBlanked(seen["env"])

    def test_pyai_launch_env_and_no_builtin_tools(self):
        import claude_agent_sdk as sdk
        from core.services.pyai import runtime

        seen = {}

        async def query(prompt, options):
            seen["options"] = options
            if False:
                yield None

        with (
            patch.dict(os.environ, SECRETS),
            patch.object(runtime, "build_tools", return_value=[]),
            patch.object(sdk, "query", query),
        ):
            async_to_sync(runtime.PyAIService._drive)(
                "p", "s", "", {"ANTHROPIC_API_KEY": "k"}, None
            )
        self.assertBlanked(seen["options"].env)
        self.assertEqual(seen["options"].tools, [])

    def test_claude_service_launch_env(self):
        import claude_agent_sdk as sdk

        seen = {}

        async def query(prompt, options):
            seen["env"] = options.env
            if False:
                yield None

        with patch.dict(os.environ, SECRETS), patch.object(sdk, "query", query):
            try:
                ClaudeService._run_query(
                    {"ANTHROPIC_API_KEY": "k"}, "", prompt="p", kwargs_extra={}
                )
            except Exception:
                pass
        self.assertBlanked(seen["env"])
