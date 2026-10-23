"""Bounded text-only archives and HTTPS GitHub imports. Never extracts to disk."""

import io
import stat
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from core.api.errors import ApiError
from core.netguard import guarded_opener, is_safe_endpoint_url
from .contract import validate_contract
from .paths import (
    MAX_FILES,
    MAX_IMPORT_BYTES,
    MAX_NOTE_BYTES,
    MAX_RAW_BYTES,
    validate_files,
    validate_path,
)
from .service import BrainService


def read_zip(data):
    if len(data) > MAX_IMPORT_BYTES:
        raise ApiError("too_large", "The archive exceeds 50 MB.", 413)
    files = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES * 2:
                raise ApiError("too_large", "The archive contains too many entries.", 413)
            names = []
            for entry in entries:
                # ZipInfo normalizes Windows separators in filename on Windows.
                validate_path(
                    entry.orig_filename.rstrip("/") if entry.is_dir() else entry.orig_filename
                )
                validate_path(entry.filename.rstrip("/") if entry.is_dir() else entry.filename)
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ApiError(
                        "invalid_archive",
                        "Archives may contain only regular files and directories.",
                    )
                if entry.flag_bits & 1:
                    raise ApiError("invalid_archive", "Encrypted archives are not supported.")
                if not entry.is_dir():
                    names.append(entry.filename)
            # GitHub zipballs wrap the entire tree in a repository directory.
            prefix = ""
            roots = {name.split("/", 1)[0] for name in names}
            if "CLAUDE.md" not in names and len(roots) == 1:
                candidate = next(iter(roots)) + "/"
                if candidate + "CLAUDE.md" in names:
                    prefix = candidate
            total = 0
            folded = set()
            for entry in entries:
                if entry.is_dir():
                    continue
                path = entry.filename.removeprefix(prefix) if prefix else entry.filename
                validate_path(path)
                if path.casefold() in folded:
                    raise ApiError("invalid_archive", "The archive contains duplicate paths.")
                folded.add(path.casefold())
                limit = MAX_RAW_BYTES if path.startswith("raw/") else MAX_NOTE_BYTES
                if entry.file_size > limit or total + entry.file_size > MAX_IMPORT_BYTES:
                    raise ApiError(
                        "too_large", "Expanded archive or file exceeds its size limit.", 413
                    )
                with archive.open(entry) as stream:
                    raw = stream.read(limit + 1)
                if len(raw) > limit:
                    raise ApiError("too_large", "Expanded file exceeds its size limit.", 413)
                total += len(raw)
                if total > MAX_IMPORT_BYTES:
                    raise ApiError("too_large", "Expanded archive exceeds 50 MB.", 413)
                files[path] = raw.decode("utf-8")
    except (zipfile.BadZipFile, UnicodeError, RuntimeError, NotImplementedError) as exc:
        raise ApiError(
            "invalid_archive", "Upload a valid ZIP containing unencrypted UTF-8 text files."
        ) from exc
    validate_files(files)
    validate_contract(files)
    return files


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_zip_url(url):
    parsed = urllib.parse.urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
        or any(not part or part in {".", ".."} for part in parts)
    ):
        raise ApiError(
            "invalid_url",
            "Enter a public HTTPS GitHub repository URL: https://github.com/owner/repo.",
        )
    owner, repo = parts
    return f"https://api.github.com/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo.removesuffix('.git'), safe='')}/zipball"


def fetch_github(url):
    url = github_zip_url(url)
    opener = guarded_opener(_NoRedirect())
    try:
        for redirects in range(4):
            parsed = urllib.parse.urlsplit(url)
            safe, _reason = is_safe_endpoint_url(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"api.github.com", "codeload.github.com"}
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or not safe
            ):
                raise ApiError("invalid_url", "GitHub import destination is not permitted.")
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "PyRun-Brain-Import",
                    "Accept": "application/vnd.github+json",
                },
            )
            try:
                response = opener.open(request, timeout=30)
            except urllib.error.HTTPError as exc:
                if (
                    exc.code in {301, 302, 303, 307, 308}
                    and redirects < 3
                    and exc.headers.get("Location")
                ):
                    url = urllib.parse.urljoin(url, exc.headers["Location"])
                    exc.close()
                    continue
                exc.close()
                raise ApiError(
                    "import_failed", "GitHub could not provide that public repository archive.", 502
                ) from exc
            with response:
                data = response.read(MAX_IMPORT_BYTES + 1)
            if len(data) > MAX_IMPORT_BYTES:
                raise ApiError("too_large", "The downloaded archive exceeds 50 MB.", 413)
            return data
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ApiError(
            "import_failed",
            "Could not download the GitHub archive. Check the repository URL and try again.",
            502,
        ) from exc
    raise ApiError("import_failed", "GitHub redirected too many times.", 502)


def import_zip(workspace, slug, data, **kwargs):
    return BrainService.create(workspace, slug, files=read_zip(data), **kwargs)


def import_github(workspace, slug, url, **kwargs):
    return import_zip(workspace, slug, fetch_github(url), **kwargs)


def export_zip(workspace, brain_id, revision_id=None):
    files = BrainService.files(workspace, brain_id, revision_id)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            validate_path(path)
            archive.writestr(path, content.encode("utf-8"))
    return output.getvalue()
