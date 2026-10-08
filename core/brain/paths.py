"""Portable manifest paths; validation never touches a user-controlled filesystem."""

import re
from pathlib import PurePosixPath

from core.api.errors import ApiError

MAX_FILES = 20_000
MAX_NOTE_BYTES = 1024 * 1024
MAX_RAW_BYTES = 4 * MAX_NOTE_BYTES
MAX_IMPORT_BYTES = 50 * MAX_NOTE_BYTES
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)


def utf8_size(text):
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise ApiError("invalid_input", "Brain content must be valid UTF-8 text.") from None


def validate_path(path):
    if not isinstance(path, str) or not path or len(path) > 300:
        raise ApiError("invalid_path", "Paths must contain 1–300 characters.")
    utf8_size(path)
    parts = path.split("/")
    if (
        "\\" in path
        or ":" in path
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or any(
            p in {"", ".", ".."} or p.endswith((".", " ")) or _WINDOWS_RESERVED.match(p)
            for p in parts
        )
        or PurePosixPath(path).is_absolute()
    ):
        raise ApiError(
            "invalid_path", "Use a relative POSIX path without traversal or reserved names."
        )
    return path


def validate_files(files):
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise ApiError("too_large", f"A brain may contain at most {MAX_FILES} files.", status=413)
    total = 0
    folded = set()
    for path, text in files.items():
        validate_path(path)
        key = path.casefold()
        if key in folded:
            raise ApiError(
                "invalid_path", "Paths must also be unique on case-insensitive filesystems."
            )
        folded.add(key)
        if not isinstance(text, str) or "\x00" in text:
            raise ApiError(
                "invalid_input", "Brain files must contain UTF-8 text without NUL bytes."
            )
        size = utf8_size(text)
        limit = MAX_RAW_BYTES if path.startswith("raw/") else MAX_NOTE_BYTES
        if size > limit:
            raise ApiError(
                "too_large",
                f"File {path} exceeds its {limit // MAX_NOTE_BYTES} MB limit.",
                status=413,
            )
        total += size
    return total
