"""
Secret model for encrypted credential storage.
"""

import uuid

from django.conf import settings
from django.db import models

from .workspace import WorkspaceScopedManager


class Secret(models.Model):
    """
    Stores encrypted secrets (API keys, credentials) that are injected
    as environment variables when scripts run.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Tenancy: nullable + backfilled to the default workspace for upgrade-safety;
    # queries scope through WorkspaceScopedManager (.for_workspace).
    workspace = models.ForeignKey(
        "core.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="secrets",
        help_text="Workspace this resource belongs to (tenancy seam; nullable).",
    )

    objects = WorkspaceScopedManager()

    # Key name - must be uppercase with underscores (e.g., API_KEY, DATABASE_URL)
    # Tenancy Stage 3: unique PER WORKSPACE (not globally), so two workspaces can
    # each own an API_KEY. Plugin Platform v2 adds owner_plugin as a SECOND
    # scoping axis (see the constraints below): two plugins can each own an
    # R2_BUCKET in the same workspace; each injects under the clean name R2_BUCKET
    # into that owner's scripts.
    key = models.CharField(
        max_length=100,
        help_text="Environment variable name (uppercase, underscores allowed)",
    )

    # Plugin ownership (Plugin Platform v2, WS3). Both nullable: NULL = a normal
    # user/system secret = today's semantics. ``owner_plugin`` is a slug STRING
    # (not an FK) so it survives plugin-row deletion; ``owner_key`` is the SDK's
    # stable logical handle for idempotent upsert on (owner_plugin, owner_key).
    owner_plugin = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Slug of the plugin that owns this secret (NULL = user/system).",
    )
    owner_key = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        help_text="Stable per-owner handle for idempotent upsert (NULL = unmanaged).",
    )

    # Where this secret's VALUE comes from (External Secret Providers). "local"
    # (default) = today's path, byte-for-byte: Fernet-decrypt ``encrypted_value``.
    # "external" = fetch live at run time from ``provider`` using ``external_ref``.
    class Source(models.TextChoices):
        LOCAL = "local", "Stored value"
        EXTERNAL = "external", "External provider"

    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.LOCAL,
        help_text="Where the value comes from: a locally-stored encrypted value, "
        "or a live fetch from an external secrets provider.",
    )

    # The external provider profile this row resolves through (external rows only).
    # PROTECT, not SET_NULL: deleting a profile that secrets still reference must be
    # an explicit, guided act — SET_NULL would silently orphan rows into
    # unresolvable secrets.
    provider = models.ForeignKey(
        "core.SecretProvider",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="secrets",
        help_text="External provider profile (external rows only).",
    )

    # Provider-specific reference to the value, e.g. "kv/myapp#API_KEY" for Vault.
    external_ref = models.CharField(
        max_length=500,
        blank=True,
        help_text="Reference to the value within the provider (external rows only).",
    )

    # Encrypted value - stores the Fernet-encrypted bytes as base64 string.
    # Blank for external rows (they store no value locally).
    encrypted_value = models.TextField(
        blank=True,
        help_text="Fernet-encrypted secret value (blank for external rows)",
    )

    # Optional description to help remember what this secret is for
    description = models.TextField(
        blank=True,
        help_text="Optional description of what this secret is used for",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Who created this secret
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_secrets",
    )

    class Meta:
        db_table = "secrets"
        verbose_name = "secret"
        verbose_name_plural = "secrets"
        ordering = ["key"]
        constraints = [
            # USER secrets (owner_plugin NULL): unique PER WORKSPACE — the exact
            # tenancy rule, now scoped to user rows so owned rows don't collide
            # with it. (NULLs are SQL-distinct, hence the second partial.)
            models.UniqueConstraint(
                fields=["workspace", "key"],
                condition=models.Q(owner_plugin__isnull=True),
                name="uniq_secret_ws_key_user",
            ),
            # USER secrets without a workspace: globally unique by key (reproduces
            # today's rule; closes the SQLite NULL-distinct loophole).
            models.UniqueConstraint(
                fields=["key"],
                condition=models.Q(workspace__isnull=True)
                & models.Q(owner_plugin__isnull=True),
                name="uniq_secret_key_global_user",
            ),
            # PLUGIN-OWNED secrets: unique per (workspace, owner_plugin) — two
            # plugins may both define R2_BUCKET in one workspace.
            models.UniqueConstraint(
                fields=["workspace", "owner_plugin", "key"],
                condition=models.Q(owner_plugin__isnull=False),
                name="uniq_secret_ws_owner_key",
            ),
            # PLUGIN-OWNED secrets without a workspace: unique per (owner_plugin,
            # key) — symmetric loophole closure for un-scoped owned rows.
            models.UniqueConstraint(
                fields=["owner_plugin", "key"],
                condition=models.Q(workspace__isnull=True)
                & models.Q(owner_plugin__isnull=False),
                name="uniq_secret_owner_key_global",
            ),
        ]

    def __str__(self):
        return f"{self.key} [{self.owner_plugin}]" if self.owner_plugin else self.key

    def get_clean_name(self) -> str:
        """The environment-variable name this secret injects under.

        Namespacing lives at the model (the (owner_plugin, key) uniqueness), not
        at injection time — an owner-scoped R2_BUCKET still injects as the clean
        ``R2_BUCKET`` into that owner's scripts, so plugin code stays portable.
        """
        return self.key

    def get_masked_value(self) -> str:
        """
        Return a masked preview of the value.

        External rows show the REFERENCE ("{provider_type}: {external_ref}"), never
        the fetched value — the secrets list must never trigger N live HTTP calls
        on a page load. Local rows show first 3 / last 3 chars of the decrypted
        value, e.g. "sk-abc123xyz789" -> "sk-...789".
        """
        if self.source == self.Source.EXTERNAL:
            ptype = self.provider.provider_type if self.provider_id else "external"
            return f"{ptype}: {self.external_ref}"

        from core.services import EncryptionService

        try:
            value = EncryptionService.decrypt(self.encrypted_value)
            if len(value) <= 8:
                return "*" * len(value)
            return f"{value[:3]}...{value[-3:]}"
        except Exception:
            return "[decryption error]"

    def get_decrypted_value(self) -> str:
        """Return the secret value, dispatching on ``source``.

        Local rows Fernet-decrypt the stored value (today's path). External rows
        fetch live from the provider; a failure raises ``SecretResolutionError``
        wrapped with this secret's name + provider so the run fails pre-exec with a
        clear, named error (fail-closed).
        """
        if self.source == self.Source.EXTERNAL:
            from core.services.secret_backends import (
                SecretResolutionError,
                resolve_secret_ref,
            )

            if self.provider_id is None:
                raise SecretResolutionError(
                    f"Secret {self.key} is marked external but has no provider profile"
                )
            try:
                return resolve_secret_ref(self.provider, self.external_ref)
            except SecretResolutionError as e:
                raise SecretResolutionError(
                    f"Secret {self.key} could not be resolved from provider "
                    f"'{self.provider.name}' ({self.provider.provider_type}): {e}"
                ) from e

        from core.services import EncryptionService

        return EncryptionService.decrypt(self.encrypted_value)

    def set_value(self, plaintext: str) -> None:
        """Encrypt and store a new value."""
        from core.services import EncryptionService

        self.encrypted_value = EncryptionService.encrypt(plaintext)


class SecretGrant(models.Model):
    """A per-secret grant attaching one Secret to one Script (Plugin Platform v2).

    Opt-in scoped injection: a Script with ``injection_mode='selected'`` receives
    only its granted secrets (a plugin-owned script also gets same-owner and
    explicitly-global secrets). The
    default ``injection_mode='all'`` never consults this table, so existing
    scripts are byte-for-byte unaffected — the grant layer is purely additive.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    script = models.ForeignKey(
        "core.Script",
        on_delete=models.CASCADE,
        related_name="secret_grants",
    )
    secret = models.ForeignKey(
        "core.Secret",
        on_delete=models.CASCADE,
        related_name="grants",
    )
    # An inactive grant is retained (for UI/history) but never injected — the
    # resolver filters on active=True.
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "secret_grants"
        verbose_name = "secret grant"
        verbose_name_plural = "secret grants"
        constraints = [
            models.UniqueConstraint(
                fields=["script", "secret"], name="uniq_secret_grant"
            ),
        ]

    def __str__(self):
        return f"{self.script_id} → {self.secret_id}{'' if self.active else ' (inactive)'}"
