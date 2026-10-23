"""
Guard the Docker build context.

`COPY . .` in the Dockerfile copies everything the build context contains,
including gitignored files — `.gitignore` does not protect the image, only
`.dockerignore` does. The v1.17 e2e pass found the private-overlay git dir
(`.git-private`, with the history of the private plan docs and plugins) and
23 host `__pycache__` dirs inside the image because those entries were
missing or not recursive. This test pins the local-only paths that must never
ship, so adding a new local-only dir without an ignore rule fails CI.
"""

from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent


def _dockerignore_rules() -> set[str]:
    text = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


class DockerignoreTests(SimpleTestCase):
    # Every path that exists locally on a dev machine but must not enter the
    # image. Keep in sync with docs/PRIVATE_OVERLAY.md and the release notes.
    REQUIRED_RULES = {
        ".git",
        ".git-private",
        ".privateignore",
        "private.ps1",
        "private.sh",
        ".claude/",
        ".env",
        ".env.*",
        "data/",
        "docs/",
        "dist/",
        "landing/",
        "client_secret.json",
        "token.json",
        "plugins/*",
        "!plugins/__init__.py",
        "!plugins/README.md",
        "**/node_modules/",
        # Docker's ignore grammar only matches bare patterns at the context
        # root; caches need the recursive form.
        "**/__pycache__",
        "**/*.pyc",
    }

    def test_local_only_paths_are_excluded_from_the_build_context(self):
        missing = self.REQUIRED_RULES - _dockerignore_rules()
        self.assertFalse(
            missing,
            f".dockerignore is missing rules for local-only paths: {sorted(missing)}",
        )

    def test_python_cache_rules_are_recursive(self):
        rules = _dockerignore_rules()
        for bare in ("__pycache__", "*.pyc", "*.pyo", "*.pyd"):
            self.assertNotIn(
                bare, rules,
                f"'{bare}' only matches at the context root; use '**/{bare}'",
            )


class DockerignorePluginsTests(SimpleTestCase):
    # F139: a per-plugin private list let any new private plugin into the image.
    def test_only_tracked_plugin_files_are_re_included(self):
        import subprocess

        try:
            tracked = subprocess.run(
                ["git", "ls-files", "plugins/"], cwd=REPO_ROOT, capture_output=True,
                text=True, check=True,
            ).stdout.split()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout")
        rules = _dockerignore_rules()
        reincluded = {rule[1:] for rule in rules if rule.startswith("!plugins/")}
        self.assertEqual(set(tracked), reincluded)
        self.assertIn("plugins/*", rules)
