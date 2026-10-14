"""Per-workspace URLconf, so a request under ``/cpanel/w/<id>/`` stays there.

The console is mounted twice (``pyrunner/urls.py``): canonical ``/cpanel/`` and
the tenancy prefix ``/cpanel/w/<id>/``. Templates and views reverse the
canonical ``cpanel:`` names, which under the prefix pointed back at the user's
*default* workspace: a sidebar click or a post-save redirect silently switched
workspaces, and a redirect to an object created in the prefixed workspace 404'd.

For a prefixed request whose workspace the user may access,
``ActiveWorkspaceMiddleware`` installs the URLconf built here as
``request.urlconf``. It is the root URLconf with the canonical ``cpanel`` mount
moved to that literal prefix, so every ``{% url 'cpanel:…' %}`` and
``redirect("cpanel:…")`` reverses into the same workspace with no call-site
change. Unprefixed requests keep the root URLconf, byte-for-byte as before.
"""

import re
import types
import uuid
from functools import lru_cache
from importlib import import_module

from django.conf import settings
from django.urls import include, path
from django.urls.resolvers import URLResolver

_PREFIX = re.compile(
    r"^/cpanel/w/(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/"
)


def requested_workspace_id(path_info):
    """The workspace id in a ``/cpanel/w/<id>/`` path, or None."""
    match = _PREFIX.match(path_info)
    return uuid.UUID(match.group("id")) if match else None


def urlconf_for(workspace_id):
    """The URLconf module for ``workspace_id`` (one per workspace, reused).

    Only call this for a workspace the user may access: Django caches a
    resolver per URLconf object for the life of the process, so the number of
    modules must stay bounded by real workspaces, never by request input.
    """
    root = import_module(settings.ROOT_URLCONF)
    return _build(workspace_id, settings.ROOT_URLCONF, id(root.urlpatterns))


@lru_cache(maxsize=None)
def _build(workspace_id, root_name, _root_identity):
    # ``_root_identity`` keys the cache on the root pattern list, so a reloaded
    # root URLconf (tests) never reuses modules built from the old patterns.
    patterns = []
    for entry in import_module(root_name).urlpatterns:
        if (
            isinstance(entry, URLResolver)
            and entry.namespace == "cpanel"
            and str(entry.pattern) == "cpanel/"
        ):
            entry = path(f"cpanel/w/{workspace_id}/", include("core.urls.cpanel"))
        patterns.append(entry)
    module = types.ModuleType(f"pyrunner_workspace_urls_{workspace_id.hex}")
    module.urlpatterns = patterns
    return module
