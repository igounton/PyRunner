"""Disposable reader views; canonical data remains in revision blobs."""

import logging
import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path, PureWindowsPath

from django.conf import settings

from core.api.exceptions import ApiError
from .paths import validate_path
from .reads import generated_index

log = logging.getLogger(__name__)


def materialize(view):
    root = (Path(settings.DATA_DIR) / "brains").resolve()
    parent = root / str(view.brain.pk) / str(view.brain.head_revision_id)
    target = parent / view.tier
    if target.resolve() != target.absolute():
        raise ApiError(
            "snapshot_error", "The reader cache is unavailable. Rebuild its directory.", 503
        )
    parent.mkdir(parents=True, exist_ok=True)
    expected = {**view.files, "INDEX.md": generated_index(view)}
    if not target.exists():
        temporary = Path(tempfile.mkdtemp(prefix=".building-", dir=parent))
        try:
            for name, text in expected.items():
                validate_path(name)
                path = temporary / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="")
            try:
                temporary.rename(target)
            except OSError:
                # Another worker can finish the same immutable view first.
                if not target.is_dir():
                    raise
        finally:
            if temporary.exists() and temporary.resolve().is_relative_to(root):
                shutil.rmtree(temporary)
    actual = {}
    for path in target.rglob("*"):
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ApiError(
                "snapshot_error", "The reader cache contains links. Rebuild its directory.", 503
            )
        if path.is_file():
            actual[path.relative_to(target).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    if actual != {
        name: hashlib.sha256(text.encode("utf-8")).hexdigest() for name, text in expected.items()
    }:
        raise ApiError(
            "snapshot_error",
            "The reader cache does not match this revision. Rebuild its directory.",
            503,
        )
    os.utime(parent, None)
    # A reader lasts at most four minutes. Keep a full day so another worker's
    # in-flight snapshot is never removed when a revision advances.
    for old in parent.parent.iterdir():
        if old == parent or old.is_symlink() or not old.is_dir():
            continue
        try:
            if old.resolve().is_relative_to(root) and old.stat().st_mtime < time.time() - 86400:
                shutil.rmtree(old)
        except OSError:
            log.warning("brain_snapshot_cleanup_failed brain=%s", view.brain.pk)
    return target


def _deny():
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "File tools may only read this brain snapshot.",
        }
    }


def reader_hooks(root, read_paths):
    """Source-port guard; also reject Glob patterns that escape their path root."""
    from claude_agent_sdk import HookMatcher

    root = root.resolve()

    def allowed(raw):
        if not isinstance(raw, str) or "\x00" in raw:
            return False
        try:
            path = Path(raw)
            return (root / path).resolve().is_relative_to(root)
        except (OSError, ValueError, RuntimeError):
            return False

    async def guard(data, tool_id, context):
        inputs = data.get("tool_input") or {}
        if not isinstance(inputs, dict):
            return _deny()
        for key in ("file_path", "path", "notebook_path"):
            raw = inputs.get(key)
            if raw is not None and not allowed(raw):
                return _deny()
        if data.get("tool_name") == "Glob":
            pattern = inputs.get("pattern", "")
            if (
                not isinstance(pattern, str)
                or pattern.startswith(("/", "\\"))
                or PureWindowsPath(pattern).drive
                or ".." in pattern.replace("\\", "/").split("/")
            ):
                return _deny()
        return {}

    async def observed(data, tool_id, context):
        raw = (data.get("tool_input") or {}).get("file_path")
        if data.get("tool_name") == "Read" and allowed(raw):
            read_paths.add((root / raw).resolve().relative_to(root).as_posix())
        return {}

    return {
        "PreToolUse": [HookMatcher(matcher="Read|Grep|Glob", hooks=[guard])],
        "PostToolUse": [HookMatcher(matcher="Read", hooks=[observed])],
    }
