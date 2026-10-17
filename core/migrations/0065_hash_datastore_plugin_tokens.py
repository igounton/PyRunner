"""Hash the datastore and plugin API tokens that 1.x stored in plaintext (F53).

Unified API tokens (scope="api") were already stored as a SHA-256 digest with a
short display hint. Datastore (single store / all stores) and plugin tokens kept
their raw value, so a copy of the database or a dump was enough to use them.
This migration replaces each such raw value with its SHA-256 digest and keeps
"first 8...last 4" as the display hint. Existing tokens keep working: requests
are authenticated by hashing the presented token.

Irreversible by nature (a digest can't be turned back into the token): the
reverse step does nothing, and code older than this migration can't
authenticate a hashed row.
"""

import hashlib

from django.db import migrations


def _hint(raw):
    if len(raw) <= 12:
        return "*" * len(raw)
    return f"{raw[:8]}...{raw[-4:]}"


def hash_plaintext_tokens(apps, schema_editor):
    APIToken = apps.get_model("core", "APIToken")
    rows = (
        APIToken.objects.exclude(scope="api")
        .filter(token_hint="")
        .exclude(token="")
        .only("id", "token")
    )
    for row in rows.iterator():
        raw = row.token
        APIToken.objects.filter(pk=row.pk).update(
            token=hashlib.sha256(raw.encode()).hexdigest(),
            token_hint=_hint(raw),
        )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0064_rate_limit_counter"),
    ]

    operations = [
        migrations.RunPython(hash_plaintext_tokens, migrations.RunPython.noop),
    ]
