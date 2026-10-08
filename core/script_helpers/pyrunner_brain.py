"""Grant-checked Brain reads and proposals. Python standard library only."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


class BrainError(Exception):
    def __init__(self, message, code="error", status=None):
        super().__init__(message)
        self.code, self.status = code, status


def _request(slug, operation, params):
    base = os.environ.get("PYRUNNER_INTERNAL_URL", "").rstrip("/")
    token = os.environ.get("PYRUNNER_INTERNAL_TOKEN", "")
    if not base or not token:
        raise BrainError("Run this script through PyRun to access Brains.", "unavailable")
    path = "brains" if slug is None else f"brains/{urllib.parse.quote(slug, safe='')}/{operation}"
    url = f"{base}/internal/{path}"
    request = urllib.request.Request(
        url,
        data=json.dumps(params).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)["data"]
    except urllib.error.HTTPError as exc:
        try:
            error = json.load(exc).get("error", {})
        except (ValueError, TypeError):
            error = {}
        raise BrainError(
            error.get("message", "Brain request failed."), error.get("code", "error"), exc.code
        ) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise BrainError("PyRun's Brain service is unavailable.", "unavailable") from exc


class Brain:
    """All methods return the same data object as REST and MCP."""

    def __init__(self, slug, *, _call=None):
        self.slug = slug
        self._caller = _call or _request

    def _call(self, operation, **params):
        return self._caller(self.slug, operation, params)

    def ping(self):
        return self._call("ping")

    def index(self):
        return self._call("get-index")

    def notes(self, *, kind="", topic="", project="", status="current"):
        return self._call("list-notes", kind=kind, topic=topic, project=project, status=status)

    def note(self, entity_id):
        return self._call("get-note", entity_id=entity_id)

    def lens(self, name):
        return self._call("get-lens", name=name)

    def identity(self):
        return self._call("get-identity")

    def raw(self, path):
        return self._call("get-raw", path=path)

    def assemble_context(self, *, task, lens=""):
        return self._call("assemble-context", task=task, lens=lens)

    def search(self, query, *, k=5):
        return self._call("search", query=query, k=k)

    def propose(self, *, title="", url="", content="", notes="", source_kind="doc"):
        return self._call("propose-feed", title=title, url=url, content=content, notes=notes, source_kind=source_kind)


def get_brain(slug):
    brain = Brain(slug)
    brain.ping()
    return brain


def list_brains(*, offset=0, limit=1000):
    return _request(None, "list", {"offset": offset, "limit": limit})
