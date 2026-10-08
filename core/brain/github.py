"""GitHub Git Data API, bounded and credential-scoped. No git subprocess."""

import re
import time
from urllib.parse import quote, urljoin, urlsplit

import requests
from django.conf import settings

from core.api.exceptions import ApiError
from core.netguard import is_safe_endpoint_url
from core.vectors.budget import BudgetSession
from .imports import read_zip
from .paths import MAX_IMPORT_BYTES


def validate_repository(repository, branch):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or any(p in {".", ".."} for p in repository.split("/")):
        raise ApiError("invalid_input", "Use a GitHub repository name: owner/repository.")
    if (not branch or len(branch) > 200 or branch.startswith(("/", "-")) or branch.endswith(("/", "."))
            or any(s in branch for s in ("..", "//", "@{", "\\"))
            or any(c.isspace() or ord(c) < 32 or c in "~^:?*[" for c in branch)
            or any(p.startswith(".") or p.endswith(".lock") for p in branch.split("/"))):
        raise ApiError("invalid_input", "Use an existing GitHub branch name.")


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ApiError("sync_failed", "GitHub returned an invalid commit identifier.", 502)
    return value


class GitHub:
    def __init__(self, sync):
        validate_repository(sync.repository, sync.branch)
        secret = sync.secret
        if not secret or secret.workspace_id != sync.brain.workspace_id or secret.owner_plugin:
            raise ApiError("sync_config", "Select a user Secret in this workspace.")
        try:
            self.token = secret.get_decrypted_value()
        except Exception:
            raise ApiError("sync_config", "The GitHub Secret could not be resolved.") from None
        if not self.token or any(c.isspace() for c in self.token):
            raise ApiError("sync_config", "The GitHub Secret is empty or invalid.")
        # Operator-only override for isolated test siblings, never a console URL.
        self.base = getattr(settings, "PYRUNNER_GITHUB_API_URL", "https://api.github.com").rstrip("/")
        parsed = urlsplit(self.base)
        if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.scheme not in {"http", "https"}:
            raise ApiError("sync_config", "Invalid GitHub API origin.")
        self.origin = (parsed.scheme, parsed.netloc)
        self.root = self.base + "/repos/" + "/".join(quote(p, safe="") for p in sync.repository.split("/"))
        self.branch = quote(sync.branch, safe="")
        self.session = BudgetSession(time.monotonic() + 120, max_response_bytes=MAX_IMPORT_BYTES, reject_redirects=False)
        self.session.trust_env = False

    def close(self):
        self.session.close()

    def _request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        origin = (parsed.scheme, parsed.netloc)
        same = origin == self.origin
        archive = method == "GET" and parsed.scheme == "https" and parsed.hostname == "codeload.github.com" and parsed.port in (None, 443)
        safe, _ = is_safe_endpoint_url(url)
        if not safe or parsed.username or parsed.password or not (same or archive):
            raise ApiError("sync_failed", "GitHub destination is not permitted.", 502)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "PyRun-Brain-Sync"}
        if same:
            headers["Authorization"] = "Bearer " + self.token
        try:
            response = self.session.request(method, url, headers=headers, **kwargs)
        except requests.RequestException:
            raise ApiError("sync_failed", "GitHub request failed or exceeded its time/size limit. Retry after checking the connection.", 502) from None
        if response.status_code in {409, 422}:
            raise ApiError("sync_conflict", "GitHub refused the update. Pull and review remote changes; check branch protection.", 409)
        if response.status_code >= 400:
            raise ApiError("sync_failed", "GitHub request failed. Check the repository, branch and Secret permissions.", 502)
        return response

    def json(self, method, path, body=None):
        response = self._request(method, self.root + path, **({"json": body} if body is not None else {}))
        if not 200 <= response.status_code < 300:
            raise ApiError("sync_failed", "GitHub API redirects are not supported.", 502)
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except ValueError:
            raise ApiError("sync_failed", "GitHub returned an invalid response.", 502) from None

    def head(self):
        data = self.json("GET", "/git/ref/heads/" + self.branch)
        return sha(data.get("object", {}).get("sha"))

    def files(self, commit):
        url = self.root + "/zipball/" + sha(commit)
        for attempt in range(4):
            response = self._request("GET", url)
            if 300 <= response.status_code < 400:
                if attempt == 3 or not response.headers.get("Location"):
                    break
                url = urljoin(url, response.headers["Location"])
            else:
                return read_zip(response.content)
        raise ApiError("sync_failed", "GitHub archive redirected too many times.", 502)

    def export(self, files, expected, message):
        expected = sha(expected)
        if self.head() != expected:
            raise ApiError("sync_conflict", "The GitHub branch changed. Pull and review it before exporting.", 409)
        # A complete tree intentionally mirrors the selected DB revision, including deletions.
        tree = self.json("POST", "/git/trees", {"tree": [
            {"path": path, "mode": "100644", "type": "blob", "content": content}
            for path, content in sorted(files.items())
        ]})
        commit = self.json("POST", "/git/commits", {"message": message, "tree": sha(tree.get("sha")), "parents": [expected]})
        new_sha = sha(commit.get("sha"))
        # Never force. A concurrent commit makes this new sibling non-fast-forward.
        self.json("PATCH", "/git/refs/heads/" + self.branch, {"sha": new_sha, "force": False})
        return new_sha
