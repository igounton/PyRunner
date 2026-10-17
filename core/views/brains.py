"""Workspace Brain console: browsing for members, writes for owners/admins."""

from collections import Counter
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.api.errors import ApiError
from core.brain.contract import version_warning
from core.brain.imports import export_zip, import_github, import_zip
from core.brain.paths import MAX_IMPORT_BYTES, MAX_NOTE_BYTES, MAX_RAW_BYTES, validate_path
from core.brain.service import BrainService
from core.models import Brain, BrainGrant, WorkspaceMembership
from core.views.workspaces import _require_manage, _require_owner
from core.templatetags.workspace_tags import ws_url
from core.brain import approval, feeder, intake


class StyledForm:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if not isinstance(field.widget, (forms.CheckboxInput, forms.HiddenInput)):
                field.widget.attrs["class"] = (
                    "w-full bg-ink border border-line rounded-lg p-2 text-sm"
                )


class CreateBrainForm(StyledForm, forms.Form):
    name = forms.CharField(max_length=150)
    slug = forms.SlugField(
        max_length=64,
        help_text="A stable name used by connectors in this workspace. Saved in lowercase.",
    )
    source = forms.ChoiceField(
        choices=[
            ("template", "New from template"),
            ("zip", "Import ZIP"),
            ("github", "Import public GitHub repository"),
        ]
    )
    archive = forms.FileField(
        required=False, help_text="UTF-8 text ZIP, up to 50 MB compressed and expanded."
    )
    github_url = forms.URLField(required=False, label="GitHub repository URL")

    def clean_slug(self):
        # F55: slugs are lowercase so connectors can't miss a brain by case.
        return self.cleaned_data["slug"].lower()

    def clean(self):
        data = super().clean()
        if data.get("source") == "zip" and not data.get("archive"):
            self.add_error("archive", "Choose a ZIP archive.")
        if data.get("source") == "github" and not data.get("github_url"):
            self.add_error("github_url", "Enter a repository URL.")
        return data


class EditBrainForm(StyledForm, forms.Form):
    path = forms.CharField(max_length=300)
    content = forms.CharField(
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 22}),
        max_length=MAX_NOTE_BYTES,
    )
    content_file = forms.FileField(
        required=False,
        allow_empty_file=True,
        label="Upload replacement (optional)",
        help_text="UTF-8 text; replaces the editor content. Notes: 1 MB, raw files: 4 MB.",
    )
    message = forms.CharField(max_length=500)
    expected_head = forms.UUIDField(widget=forms.HiddenInput, required=False)
    delete = forms.BooleanField(required=False, label="Delete this file")


class BrainSettingsForm(StyledForm, forms.ModelForm):
    class Meta:
        model = Brain
        fields = [
            "name",
            "description",
            "ai_provider",
            "reader_model",
            "feeder_model",
            "daily_cost_cap_usd",
            "vector_collection",
            "include_in_backup",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        labels = {"ai_provider": "AI provider", "daily_cost_cap_usd": "Daily cost cap (USD)"}
        help_texts = {
            "ai_provider": "Blank uses the active instance provider.",
            "reader_model": "Blank uses the provider default.",
            "feeder_model": "Blank uses the provider default.",
            "daily_cost_cap_usd": "Blank means no daily cap. Zero prevents AI runs.",
            "include_in_backup": "Include this brain and its complete revision history in portable backups.",
            "vector_collection": "Optional: indexes all visibility tiers into this workspace collection. Anyone with direct collection access can read its private chunks. Use a dedicated collection and a trusted embedding provider. Save to rebuild missing chunks.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from core.models import VectorCollection

        self.fields["vector_collection"].queryset = VectorCollection.objects.for_workspace(self.instance.workspace).filter(embedding_provider__isnull=False)


class BrainReaderForm(StyledForm, forms.Form):
    task = forms.CharField(max_length=8000, widget=forms.Textarea(attrs={"rows": 5}))
    lens = forms.CharField(max_length=100, required=False, help_text="Optional lens name, such as building-in-public.")


class BrainGrantForm(StyledForm, forms.ModelForm):
    class Meta:
        model = BrainGrant
        fields = ["subject_kind", "subject_id", "max_tier", "can_propose"]
        labels = {"subject_kind": "Access for", "subject_id": "Recipient"}
        help_texts = {
            "subject_id": "Script or Unified API token UUID, plugin slug, or agent identifier.",
            "can_propose": "Allows proposals at agents-only or private tier. Every feed still needs approval.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from core.models import APIToken, Script

        self.fields["subject_kind"].choices = [
            (value, "API token" if value == "token" else label)
            for value, label in BrainGrant.SUBJECTS
        ]
        self.initial["subject_kind"] = self.initial.get("subject_kind") or "script"
        workspace = self.instance.brain.workspace
        # Only names and identifiers reach the picker, never token credentials.
        subjects = {
            "script": Script.objects.for_workspace(workspace),
            "token": APIToken.objects.filter(workspace=workspace, scope="api"),
        }
        self.subject_choices = {}
        for kind, queryset in subjects.items():
            rows = list(queryset.order_by("name", "pk").values_list("pk", "name"))
            counts = Counter(name for _, name in rows)
            self.subject_choices[kind] = [
                {"id": str(pk), "label": name if counts[name] == 1 else f"{name} ({str(pk)[:8]})"}
                for pk, name in rows
            ]

    def clean(self):
        data = super().clean()
        from core.brain.access import validate_subject

        if all(data.get(field) for field in ("subject_kind", "subject_id", "max_tier")):
            try:
                data["subject_id"] = validate_subject(
                    self.instance.brain.workspace, data["subject_kind"], data["subject_id"],
                    data["max_tier"], data.get("can_propose", False),
                )
            except ValidationError as exc:
                self.add_error(None, exc)
        return data


class BrainFeedForm(StyledForm, forms.Form):
    source_kind = forms.ChoiceField(choices=[(value, value.title()) for value in intake.SOURCE_KINDS])
    title = forms.CharField(max_length=500, required=False)
    url = forms.URLField(max_length=2048, required=False, label="Source URL")
    content = forms.CharField(max_length=intake.MAX_PAYLOAD, required=False, strip=False, widget=forms.Textarea(attrs={"rows": 9}))
    notes = forms.CharField(max_length=intake.MAX_PAYLOAD, required=False, widget=forms.Textarea(attrs={"rows": 3}))


class BrainProposalForm(StyledForm, forms.Form):
    proposal = forms.JSONField(required=False, widget=forms.Textarea(attrs={"rows": 20}))
    proposal_file = forms.FileField(required=False, label="Proposal JSON file (optional)",
                                    help_text="Replaces the editor; at most 50 MB UTF-8 JSON.")
    expected_digest = forms.CharField(widget=forms.HiddenInput)

    def clean(self):
        data = super().clean()
        upload = data.get("proposal_file")
        if upload:
            from core.api.schema import loads
            try:
                raw = upload.read(MAX_IMPORT_BYTES + 1)
                if len(raw) > MAX_IMPORT_BYTES:
                    raise ApiError("too_large", "Proposal JSON exceeds 50 MB.")
                data["proposal"] = loads(raw)
            except ApiError as exc:
                self.add_error("proposal_file", exc.message)
        return data


def _brain(request, brain_id):
    return get_object_or_404(
        Brain.objects.for_workspace(request.workspace).select_related("head_revision"), pk=brain_id
    )


def _url(request, name, **kwargs):
    return ws_url({"request": request, "show_workspace_switcher": request.resolver_match.namespace == "cpanel_ws"},
                  f"cpanel:{name}", **kwargs)


def _context(request, brain=None, **extra):
    membership = WorkspaceMembership.objects.filter(
        user=request.user, workspace=request.workspace
    ).first()
    return {
        "brain": brain,
        "can_manage": request.user.is_superuser or bool(membership and membership.can_manage),
        "contract_warning": version_warning(brain.contract_version) if brain else "",
        **extra,
    }


def _error(form, exc):
    if isinstance(exc, ValidationError):
        form.add_error(None, "; ".join(exc.messages))
    elif isinstance(exc, IntegrityError):
        form.add_error(None, "That name or grant is already in use. Choose another.")
    else:
        form.add_error(None, exc.message)


@login_required
def brain_list_view(request):
    brains = Brain.objects.for_workspace(request.workspace).select_related("head_revision")
    page = Paginator(brains, 100).get_page(request.GET.get("page"))
    return render(request, "cpanel/brains/list.html", _context(request, page=page))


@login_required
def brain_create_view(request):
    _require_manage(request, request.workspace)
    form = CreateBrainForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        kwargs = {"name": data["name"], "author": request.user}
        try:
            if data["source"] == "zip":
                if data["archive"].size > MAX_IMPORT_BYTES:
                    raise ApiError("too_large", "The archive exceeds 50 MB.", 413)
                brain = import_zip(
                    request.workspace,
                    data["slug"],
                    data["archive"].read(MAX_IMPORT_BYTES + 1),
                    **kwargs,
                )
            elif data["source"] == "github":
                brain = import_github(request.workspace, data["slug"], data["github_url"], **kwargs)
            else:
                brain = BrainService.create(request.workspace, data["slug"], **kwargs)
        except (ApiError, ValidationError, IntegrityError) as exc:
            _error(form, exc)
        else:
            messages.success(request, "Brain created.")
            return redirect(_url(request, "brain_detail", brain_id=brain.pk))
    return render(
        request,
        "cpanel/brains/form.html",
        _context(request, form=form, heading="Create or import a brain", submit="Create brain"),
    )


@login_required
def brain_detail_view(request, brain_id):
    brain = _brain(request, brain_id)
    path = request.GET.get("path", "")
    files = BrainService.files(request.workspace, brain.pk)
    if path and path not in files:
        raise Http404("Unknown note.")
    entities = {
        entity.path: entity for entity in brain.entities.filter(brain__workspace=request.workspace)
    }
    folders = {}
    for name in sorted(files):
        folder, _, filename = name.rpartition("/")
        folders.setdefault(folder or "Contract", []).append(
            {"path": name, "name": filename, "entity": entities.get(name)}
        )
    entity = entities.get(path)
    superseded = (
        brain.entities.filter(
            brain__workspace=request.workspace, entity_id=entity.superseded_by
        ).first()
        if entity and entity.superseded_by
        else None
    )
    return render(
        request,
        "cpanel/brains/detail.html",
        _context(
            request,
            brain,
            folders=folders,
            path=path,
            content=files.get(path, ""),
            entity=entity,
            superseded=superseded,
        ),
    )


@login_required
def brain_edit_view(request, brain_id):
    brain = _brain(request, brain_id)
    _require_manage(request, request.workspace)
    path = request.GET.get("path", "")
    files = BrainService.files(request.workspace, brain.pk)
    large = len(files.get(path, "").encode("utf-8")) > MAX_NOTE_BYTES
    form = EditBrainForm(
        request.POST or None,
        request.FILES or None,
        initial={
            "path": path,
            "content": "" if large else files.get(path, ""),
            "expected_head": brain.head_revision_id,
        },
    )
    if large:
        form.fields[
            "content"
        ].help_text = "This file exceeds the text editor limit. Upload a UTF-8 replacement, enter shorter content, or delete it."
    status = 200
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        upload = data.pop("content_file")
        try:
            validate_path(data["path"])
            if upload is not None:
                raw = upload.read(MAX_RAW_BYTES + 1)
                if len(raw) > MAX_RAW_BYTES:
                    raise ApiError("too_large", "The replacement exceeds 4 MB.", 413)
                try:
                    data["content"] = raw.decode("utf-8")
                except UnicodeError as exc:
                    raise ApiError("invalid_input", "Upload UTF-8 text.") from exc
            elif (
                not data["content"]
                and not data["delete"]
                and len(files.get(data["path"], "").encode("utf-8")) > MAX_NOTE_BYTES
            ):
                raise ApiError(
                    "invalid_input",
                    "Upload a UTF-8 replacement or enter shorter content before saving.",
                )
            elif "\r\n" not in files.get(data["path"], ""):
                # Browsers submit textarea text with CRLF; keep LF files LF so
                # exports and GitHub sync don't rewrite every line.
                data["content"] = data["content"].replace("\r\n", "\n")
            revision = BrainService.edit(request.workspace, brain.pk, author=request.user, **data)
        except (ApiError, ValidationError) as exc:
            _error(form, exc)
            status = getattr(exc, "status", 400)
        else:
            messages.success(request, f"Saved revision {revision.number}.")
            return redirect(
                _url(request, "brain_detail", brain_id=brain.pk)
                + ("?" + urlencode({"path": data["path"]}) if not data["delete"] else "")
            )
    return render(
        request,
        "cpanel/brains/form.html",
        _context(request, brain, form=form, heading="Edit brain file", submit="Save revision"),
        status=status,
    )


@login_required
def brain_revisions_view(request, brain_id):
    brain = _brain(request, brain_id)
    revisions = brain.revisions.filter(brain__workspace=request.workspace).select_related("author")
    return render(
        request,
        "cpanel/brains/revisions.html",
        _context(request, brain, page=Paginator(revisions, 50).get_page(request.GET.get("page"))),
    )


@login_required
def brain_diff_view(request, brain_id, revision_id):
    brain = _brain(request, brain_id)
    revision = get_object_or_404(
        brain.revisions.filter(brain__workspace=request.workspace), pk=revision_id
    )
    return render(
        request,
        "cpanel/brains/diff.html",
        _context(
            request,
            brain,
            revision=revision,
            changes=BrainService.diff(request.workspace, brain.pk, revision.pk),
        ),
    )


@login_required
def brain_settings_view(request, brain_id):
    brain = _brain(request, brain_id)
    _require_manage(request, request.workspace)
    form = BrainSettingsForm(request.POST or None, instance=brain)
    if request.method == "POST" and form.is_valid():
        # Only configuration fields: a concurrent writer may advance HEAD.
        changed = list(form.changed_data)
        if "vector_collection" in changed:
            brain.vector_revision, brain.vector_claim, brain.vector_claimed_at = None, None, None
            brain.vector_error = ""
            changed += ["vector_revision", "vector_claim", "vector_claimed_at", "vector_error"]
        brain.save(update_fields=changed + ["updated_at"])
        if brain.vector_collection_id:
            from core.brain.recall import enqueue

            enqueue(request.workspace.pk, brain.pk)
        messages.success(request, "Brain settings saved.")
        return redirect(_url(request, "brain_settings", brain_id=brain.pk))
    is_owner = request.user.is_superuser or WorkspaceMembership.objects.filter(
        user=request.user, workspace=request.workspace, role=WorkspaceMembership.ROLE_OWNER
    ).exists()
    return render(
        request,
        "cpanel/brains/form.html",
        _context(
            request, brain, form=form, heading="Brain settings", submit="Save settings",
            delete_zone=True, can_delete=is_owner and not brain.owner_plugin,
            bound_tokens=brain.api_tokens.count(),
        ),
    )


@login_required
@require_POST
def brain_delete_view(request, brain_id):
    """Owner-only: delete a brain after its slug is typed to confirm (F56)."""
    brain = _brain(request, brain_id)
    _require_owner(request, request.workspace)
    if request.POST.get("confirm_slug", "").strip() != brain.slug:
        messages.error(request, f"Type the brain's slug ({brain.slug}) to confirm the delete.")
        return redirect(_url(request, "brain_settings", brain_id=brain.pk))
    try:
        result = BrainService.delete(request.workspace, brain.pk)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(_url(request, "brain_settings", brain_id=brain.pk))
    note = ""
    if result["tokens"]:
        n = result["tokens"]
        note = f" {n} API token{'s' if n != 1 else ''} bound to it {'were' if n != 1 else 'was'} deleted too."
    messages.success(request, f"Brain '{brain.name}' deleted.{note}")
    if result["warning"]:
        messages.warning(request, result["warning"])
    return redirect(_url(request, "brain_list"))


@login_required
def brain_grants_view(request, brain_id):
    brain = _brain(request, brain_id)
    _require_manage(request, request.workspace)
    form = BrainGrantForm(request.POST or None, instance=BrainGrant(brain=brain))
    if request.method == "POST" and form.is_valid():
        # Upsert lets administrators change an existing grant in the same form.
        BrainGrant.objects.update_or_create(
            brain=brain,
            subject_kind=form.cleaned_data["subject_kind"],
            subject_id=form.cleaned_data["subject_id"],
            defaults={key: form.cleaned_data[key] for key in ("max_tier", "can_propose")},
        )
        messages.success(request, "Grant saved.")
        return redirect(_url(request, "brain_grants", brain_id=brain.pk))
    labels = {
        (kind, item["id"]): item["label"]
        for kind, items in form.subject_choices.items()
        for item in items
    }
    grants = list(brain.grants.filter(brain__workspace=request.workspace).order_by(
        "subject_kind", "subject_id"
    ))
    for grant in grants:
        grant.subject_label = labels.get((grant.subject_kind, grant.subject_id), grant.subject_id)
    return render(
        request,
        "cpanel/brains/grants.html",
        _context(
            request,
            brain,
            form=form,
            grants=grants,
        ),
    )


@login_required
@require_POST
def brain_grant_delete_view(request, brain_id, grant_id):
    brain = _brain(request, brain_id)
    _require_manage(request, request.workspace)
    get_object_or_404(brain.grants.filter(brain__workspace=request.workspace), pk=grant_id).delete()
    messages.success(request, "Grant revoked.")
    return redirect(_url(request, "brain_grants", brain_id=brain.pk))


@login_required
def brain_export_view(request, brain_id):
    brain = _brain(request, brain_id)
    response = HttpResponse(export_zip(request.workspace, brain.pk), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{brain.slug}.zip"'
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def brain_inbox_view(request, brain_id):
    brain = _brain(request, brain_id)
    page = Paginator(brain.feeds.filter(brain__workspace=request.workspace), 50).get_page(request.GET.get("page"))
    return render(request, "cpanel/brains/inbox.html", _context(request, brain, page=page))


@login_required
def brain_reader_view(request, brain_id):
    from core.brain.jobs import submit
    from uuid import UUID

    brain = _brain(request, brain_id)
    form = BrainReaderForm(request.POST or None)
    job = None
    if request.GET.get("job"):
        try:
            identifier = UUID(request.GET["job"])
        except ValueError:
            raise Http404 from None
        job = get_object_or_404(brain.reader_jobs.filter(brain__workspace=request.workspace, user=request.user), pk=identifier)
        if request.method != "POST":
            # Keep the question in the box so it can be refined and re-asked (U67).
            form = BrainReaderForm(initial={"task": job.task, "lens": job.lens})
    if request.method == "POST" and form.is_valid():
        try:
            job = submit(request.workspace, brain, request.user, **form.cleaned_data)
        except ApiError as exc:
            _error(form, exc)
        else:
            return redirect(_url(request, "brain_reader", brain_id=brain.pk) + "?" + urlencode({"job": job.pk}))
    return render(request, "cpanel/brains/reader.html", _context(request, brain, form=form, job=job))


@login_required
def brain_reader_status_view(request, brain_id, job_id):
    brain = _brain(request, brain_id)
    job = get_object_or_404(brain.reader_jobs.filter(brain__workspace=request.workspace, user=request.user), pk=job_id)
    result, error, status = job.result, job.error, job.status
    if status == "complete" and job.revision_id != brain.head_revision_id:
        result, error, status = None, "The brain changed. Run the query again.", "failed"
    response = JsonResponse({"status": status, "result": result, "error": error})
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def brain_feed_create_view(request, brain_id):
    brain = _brain(request, brain_id)
    form = BrainFeedForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            feed = intake.capture(request.workspace, brain.pk, form.cleaned_data,
                                  channel="console", principal=f"user:{request.user.pk}")
        except ApiError as exc:
            _error(form, exc)
        else:
            messages.success(request, "Source captured for human review.")
            return redirect(_url(request, "brain_feed", brain_id=brain.pk, feed_id=feed.pk))
    return render(request, "cpanel/brains/form.html", _context(request, brain, form=form,
                  heading="Add a source", submit="Capture source"))


@login_required
def brain_feed_view(request, brain_id, feed_id):
    import json

    brain = _brain(request, brain_id)
    try:
        feed = approval.get_feed(request.workspace, brain.pk, feed_id)
    except ApiError:
        raise Http404 from None
    form = BrainProposalForm(initial={"proposal": feed.proposal, "expected_digest": approval.digest(feed)})
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in {"approve", "reject"}:
                _require_manage(request, request.workspace)
                decision = approval.approve if action == "approve" else approval.reject
                updated = decision(request.workspace, brain.pk, feed.pk,
                                   expected_digest=request.POST.get("expected_digest", ""), user=request.user,
                                   note=request.POST.get("note", ""))
                if updated.needs_review:
                    messages.warning(request, updated.error)
                else:
                    messages.success(request, f"Feed {updated.get_status_display().lower()}.")
                return redirect(_url(request, "brain_feed", brain_id=brain.pk, feed_id=feed.pk))
            if action == "retry":
                feeder.retry(request.workspace, brain.pk, feed.pk, content=request.POST.get("content"))
                messages.success(request, "Extraction queued.")
                return redirect(_url(request, "brain_feed", brain_id=brain.pk, feed_id=feed.pk))
            if action == "edit":
                if feed.channel == "sync":
                    _require_manage(request, request.workspace)
                form = BrainProposalForm(request.POST, request.FILES)
                if form.is_valid():
                    approval.edit_proposal(request.workspace, brain.pk, feed.pk, form.cleaned_data["proposal"],
                                           expected_digest=form.cleaned_data["expected_digest"])
                    messages.success(request, "Proposal saved for review.")
                    return redirect(_url(request, "brain_feed", brain_id=brain.pk, feed_id=feed.pk))
            elif action not in {"approve", "reject", "retry"}:
                raise ApiError("invalid_input", "Unknown feed action.")
        except ApiError as exc:
            messages.error(request, exc.message)
    current = None
    if feed.proposal is not None:
        try:
            current = approval.review(request.workspace, brain.pk, feed.pk)
        except ApiError as exc:
            messages.error(request, exc.message)
    proposal_text = json.dumps(feed.proposal, ensure_ascii=False, indent=2) if feed.proposal is not None else ""
    if len(proposal_text.encode("utf-8")) > MAX_NOTE_BYTES:
        # The bounded upload supports large proposals without a multi-MB textarea.
        form.initial["proposal"] = None
    return render(request, "cpanel/brains/feed.html", _context(request, brain, feed=feed,
                  review=current, form=form, expected_digest=approval.digest(feed),
                  editable=feed.status in {"pending", "failed", "proposed"} and (feed.channel != "sync" or _context(request)["can_manage"]),
                  retryable=feed.channel != "sync" and feed.status in {"pending", "failed"},
                  decidable=feed.status not in {"applied", "rejected", "approving"}))
