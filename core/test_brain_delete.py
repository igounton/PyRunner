"""
A brain can be deleted by a workspace owner who types its slug (F56, 2.0 release test).

Before 2.0 there was no delete at all: a wrong import, a test brain or a case
twin stayed in the list, in backups and in grants forever.
"""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.brain.service import BrainService
from core.models import (
    APIToken,
    Brain,
    BrainGrant,
    BrainRevision,
    Environment,
    GlobalSettings,
    User,
    Workspace,
    WorkspaceMembership,
)
from core.test_brain_store import contract_files


class BrainDeleteTests(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.create(name="env", path="p", is_active=True, is_default=True)
        self.ws = Workspace.get_default()
        self.owner = User.objects.create(email="owner@example.com")
        WorkspaceMembership.ensure(self.owner, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.admin = User.objects.create(email="admin@example.com")
        WorkspaceMembership.objects.update_or_create(
            user=self.admin, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_ADMIN}
        )
        User.objects.create(email="root@example.com", is_superuser=True, is_staff=True)
        self.brain = BrainService.create(self.ws, "doomed", files=contract_files())
        BrainGrant.objects.create(
            brain=self.brain, subject_kind="plugin", subject_id="x", max_tier="public"
        )
        self.token = APIToken.objects.create(
            name="bound", token=APIToken.generate_token(scope="api"), scope="api",
            permissions=["brain:read"], workspace=self.ws, brain=self.brain,
        )

    def _delete(self, user, confirm):
        self.client.force_login(user)
        return self.client.post(
            reverse("cpanel:brain_delete", kwargs={"brain_id": self.brain.pk}),
            {"confirm_slug": confirm},
            follow=True,
        )

    def test_owner_deletes_with_the_slug_and_everything_goes(self):
        resp = self._delete(self.owner, "doomed")
        self.assertFalse(Brain.objects.filter(pk=self.brain.pk).exists())
        self.assertFalse(BrainRevision.objects.filter(brain_id=self.brain.pk).exists())
        self.assertFalse(BrainGrant.objects.filter(brain_id=self.brain.pk).exists())
        self.assertFalse(APIToken.objects.filter(pk=self.token.pk).exists())
        text = " ".join(str(m) for m in resp.context["messages"])
        self.assertIn("Brain 'doomed' deleted.", text)
        self.assertIn("1 API token bound to it was deleted too.", text)

    def test_wrong_confirmation_keeps_the_brain(self):
        resp = self._delete(self.owner, "Doomed")
        self.assertTrue(Brain.objects.filter(pk=self.brain.pk).exists())
        self.assertIn("Type the brain's slug", " ".join(str(m) for m in resp.context["messages"]))

    def test_admin_cannot_delete(self):
        resp = self._delete(self.admin, "doomed")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Brain.objects.filter(pk=self.brain.pk).exists())

    def test_plugin_owned_brain_is_refused(self):
        Brain.objects.filter(pk=self.brain.pk).update(owner_plugin="demo", owner_key="k")
        self._delete(self.owner, "doomed")
        self.assertTrue(Brain.objects.filter(pk=self.brain.pk).exists())

    def test_vector_chunks_are_purged_by_brain_id(self):
        collection = mock.Mock()
        collection.name = "recall"
        handle = mock.Mock()
        with mock.patch.object(Brain, "vector_collection", collection), \
                mock.patch("core.vectors.service.VectorService.handle", return_value=handle):
            result = BrainService.delete(self.ws, self.brain.pk)
        handle.delete.assert_called_once_with(filter={"brain_id": str(self.brain.pk)})
        self.assertEqual(result["warning"], "")

    def test_settings_page_shows_the_delete_zone_to_owners_only(self):
        url = reverse("cpanel:brain_settings", kwargs={"brain_id": self.brain.pk})
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(url), 'name="confirm_slug"')
        self.client.force_login(self.admin)
        page = self.client.get(url)
        self.assertNotContains(page, 'name="confirm_slug"')
        self.assertContains(page, "Only a workspace owner can delete a brain.")
