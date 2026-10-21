"""Owner/admin configuration and queued GitHub operations."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render

from core.api.exceptions import ApiError
from core.brain import sync
from core.models import BrainSync, Secret
from .brains import StyledForm, _brain, _context, _error, _url
from .workspaces import _require_manage


class SyncForm(StyledForm, forms.Form):
    repository = forms.CharField(max_length=200, help_text="A dedicated private GitHub repository: owner/name.")
    branch = forms.CharField(max_length=200, initial="main", help_text="An existing branch. Initialize the repository on GitHub first.")
    secret = forms.ModelChoiceField(queryset=Secret.objects.none(), help_text="A workspace Secret with repository Contents read/write permission.")
    poll_enabled = forms.BooleanField(required=False, label="Check for incoming changes every five minutes")

    def __init__(self, *args, workspace, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["secret"].queryset = Secret.objects.for_workspace(workspace).filter(owner_plugin__isnull=True)
        self.fields["secret"].empty_label = "Choose a secret…"


@login_required
def brain_sync_view(request, brain_id):
    brain = _brain(request, brain_id)
    _require_manage(request, request.workspace)
    value = BrainSync.objects.filter(brain=brain, brain__workspace=request.workspace).first()
    initial = {key: getattr(value, key) for key in ("repository", "branch", "secret", "poll_enabled")} if value else {}
    action = request.POST.get("action") if request.method == "POST" else None
    form = SyncForm(request.POST if action == "save" else None, initial=initial, workspace=request.workspace)
    if action:
        try:
            if action == "save" and form.is_valid():
                sync.configure(request.workspace, brain_id, **form.cleaned_data)
                messages.success(request, "GitHub sync settings saved.")
                return redirect(_url(request, "brain_sync", brain_id=brain_id))
            if action in {"pull", "export"}:
                sync.submit(request.workspace, brain_id, action, user=request.user,
                            initial_sha=request.POST.get("initial_sha", "").strip(),
                            acknowledge=request.POST.get("acknowledge") == "on")
                messages.success(request, "Sync queued. Refresh this page to see the result.")
                return redirect(_url(request, "brain_sync", brain_id=brain_id))
            if action not in {"save", "pull", "export"}:
                raise ApiError("invalid_input", "Unknown sync action.")
        except (ApiError, ValidationError) as exc:
            _error(form, exc)
    return render(request, "cpanel/brains/sync.html", _context(request, brain, form=form, sync=value))
