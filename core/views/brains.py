"""Workspace Brain console: browsing for members, writes for owners/admins."""

from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.api.errors import ApiError
from core.brain.contract import version_warning
from core.brain.imports import export_zip, import_github, import_zip
from core.brain.paths import MAX_IMPORT_BYTES, MAX_NOTE_BYTES, MAX_RAW_BYTES, validate_path
from core.brain.service import BrainService
from core.models import Brain, BrainGrant, WorkspaceMembership
from core.views.workspaces import _require_manage


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
        max_length=64, help_text="A stable name used by connectors in this workspace."
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
        }


class BrainGrantForm(StyledForm, forms.ModelForm):
    class Meta:
        model = BrainGrant
        fields = ["subject_kind", "subject_id", "max_tier", "can_propose"]
        help_texts = {
            "subject_id": "Script or Unified API token UUID, plugin slug, or agent identifier.",
            "can_propose": "Allows proposals at agents-only or private tier. Every feed still needs approval.",
        }

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


def _brain(request, brain_id):
    return get_object_or_404(
        Brain.objects.for_workspace(request.workspace).select_related("head_revision"), pk=brain_id
    )


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
            return redirect("cpanel:brain_detail", brain_id=brain.pk)
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
            revision = BrainService.edit(request.workspace, brain.pk, author=request.user, **data)
        except (ApiError, ValidationError) as exc:
            _error(form, exc)
            status = getattr(exc, "status", 400)
        else:
            messages.success(request, f"Saved revision {revision.number}.")
            return redirect(
                reverse("cpanel:brain_detail", kwargs={"brain_id": brain.pk})
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
        brain.save(update_fields=list(form.changed_data) + ["updated_at"])
        messages.success(request, "Brain settings saved.")
        return redirect("cpanel:brain_settings", brain_id=brain.pk)
    return render(
        request,
        "cpanel/brains/form.html",
        _context(request, brain, form=form, heading="Brain settings", submit="Save settings"),
    )


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
        return redirect("cpanel:brain_grants", brain_id=brain.pk)
    return render(
        request,
        "cpanel/brains/grants.html",
        _context(
            request,
            brain,
            form=form,
            grants=brain.grants.filter(brain__workspace=request.workspace).order_by(
                "subject_kind", "subject_id"
            ),
        ),
    )


@login_required
@require_POST
def brain_grant_delete_view(request, brain_id, grant_id):
    brain = _brain(request, brain_id)
    _require_manage(request, request.workspace)
    get_object_or_404(brain.grants.filter(brain__workspace=request.workspace), pk=grant_id).delete()
    messages.success(request, "Grant revoked.")
    return redirect("cpanel:brain_grants", brain_id=brain.pk)


@login_required
def brain_export_view(request, brain_id):
    brain = _brain(request, brain_id)
    response = HttpResponse(export_zip(request.workspace, brain.pk), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{brain.slug}.zip"'
    response["Cache-Control"] = "private, no-store"
    return response
