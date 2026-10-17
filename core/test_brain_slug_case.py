"""
Brain slugs are lowercase and unique regardless of case (F55, 2.0 release test).

`W16-Upper` and `w16-upper` could both be created in one workspace, and a slug
typed in another case reached the other brain or none. New slugs are saved in
lowercase, a case-only twin is refused, and a pre-2.0 mixed-case slug is found
when typed in another case (unless case twins make that ambiguous).
"""

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.api.errors import ApiError
from core.brain.access import principal_context, resolve
from core.brain.service import BrainService
from core.models import (
    Brain,
    BrainGrant,
    Environment,
    GlobalSettings,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.test_brain_store import contract_files


class BrainSlugCaseTests(TestCase):
    def setUp(self):
        self.ws = Workspace.get_default()

    def _ctx(self):
        return principal_context(self.ws, "plugin", "reader")

    def _grant(self, brain):
        BrainGrant.objects.create(
            brain=brain, subject_kind="plugin", subject_id="reader", max_tier="public"
        )

    def test_service_lowercases_and_refuses_case_twins(self):
        brain = BrainService.create(self.ws, "Team-Notes", files=contract_files())
        self.assertEqual(brain.slug, "team-notes")
        with self.assertRaises(ValidationError):
            BrainService.create(self.ws, "TEAM-notes", files=contract_files())
        self.assertEqual(Brain.objects.filter(workspace=self.ws).count(), 1)

    def test_legacy_mixed_case_slug_resolves_in_any_case(self):
        brain = BrainService.create(self.ws, "legacy", files=contract_files())
        self._grant(brain)
        Brain.objects.filter(pk=brain.pk).update(slug="Legacy-Mind")  # a pre-2.0 row
        found, _ = resolve(self._ctx(), {"brain": "legacy-mind"})
        self.assertEqual(found.pk, brain.pk)
        found, _ = resolve(self._ctx(), {"brain": "Legacy-Mind"}, lock=True)
        self.assertEqual(found.pk, brain.pk)

    def test_ambiguous_case_twins_need_the_exact_slug(self):
        a = BrainService.create(self.ws, "twin-a", files=contract_files())
        b = BrainService.create(self.ws, "twin-b", files=contract_files())
        self._grant(a)
        self._grant(b)
        Brain.objects.filter(pk=a.pk).update(slug="Twin")
        Brain.objects.filter(pk=b.pk).update(slug="twin")
        self.assertEqual(resolve(self._ctx(), {"brain": "Twin"})[0].pk, a.pk)
        self.assertEqual(resolve(self._ctx(), {"brain": "twin"})[0].pk, b.pk)
        with self.assertRaises(ApiError):
            resolve(self._ctx(), {"brain": "TWIN"})


class BrainCreateFormSlugTests(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.create(name="env", path="p", is_active=True, is_default=True)
        self.ws = Workspace.get_default()
        user = User.objects.create(email="root@example.com", is_superuser=True, is_staff=True)
        WorkspaceMembership.ensure(user, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.client.force_login(user)

    def _create(self, name, slug):
        return self.client.post(
            reverse("cpanel:brain_create"), {"name": name, "slug": slug, "source": "template"}
        )

    def test_console_saves_lowercase_and_refuses_a_case_twin(self):
        self.assertEqual(self._create("Upper", "W16-Upper").status_code, 302)
        self.assertEqual(Brain.objects.get(workspace=self.ws).slug, "w16-upper")
        resp = self._create("Lower", "w16-UPPER")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already exists")
        self.assertEqual(Brain.objects.filter(workspace=self.ws).count(), 1)
