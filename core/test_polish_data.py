"""
M4 polish batch (2.0 release test) — environments, libraries, tags, data stores, databases.

U5    pip/setuptools can't be uninstalled from an environment
U36   the Python picker lists one entry per real interpreter
F35   "1 script still uses it"
U34   library module remove controls are real, labelled buttons
U35   version history: short "Diff" action
U38   tag colour swatches are named; members get no empty Actions column
U39   a plugin-owned data store says so; superusers get a force delete
U44   the slow-query list hides PyRun's own explorer statements
"""

import pathlib
import uuid
from unittest import mock

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from core.models import (
    DataStore, Environment, GlobalSettings, Library, PackageOperation, Script, User,
    Workspace, WorkspaceMembership,
)
from core.services.environment_service import EnvironmentService

PW = "correct-horse-battery-staple-91"


class DataBase(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        self.ws = Workspace.get_default()
        Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        self.env = Environment.objects.create(name="denv", path=f"denv{uuid.uuid4().hex[:6]}")
        self.admin = User.objects.create_user(
            username="admin@example.com", email="admin@example.com", password=PW,
            is_superuser=True, is_staff=True,
        )
        self.member = User.objects.create_user(
            username="member@example.com", email="member@example.com", password=PW,
        )
        WorkspaceMembership.objects.update_or_create(
            user=self.member, workspace=self.ws, defaults={"role": WorkspaceMembership.ROLE_MEMBER}
        )
        self.client.force_login(self.admin)


class EnvironmentTests(DataBase):
    def test_pip_and_setuptools_are_protected(self):
        self.assertTrue(EnvironmentService.is_protected_package("pip"))
        self.assertTrue(EnvironmentService.is_protected_package("Setuptools"))
        self.assertFalse(EnvironmentService.is_protected_package("requests"))
        before = PackageOperation.objects.count()
        resp = self.client.post(
            reverse("cpanel:package_uninstall", kwargs={"pk": self.env.pk}),
            {"package_name": "pip"}, follow=True,
        )
        self.assertEqual(PackageOperation.objects.count(), before)
        texts = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("can't be uninstalled here" in t for t in texts), texts)

    def test_python_picker_deduplicates_by_real_path(self):
        real = "/usr/local/bin/python3.13"
        entries = [{"path": "/usr/local/bin/python3", "version": "3.13.16", "display": "Python 3.13.16"}]
        with mock.patch("core.services.environment_service.os.name", "posix"), \
                mock.patch("core.services.environment_service.sys.executable", "/usr/local/bin/python"), \
                mock.patch("core.services.environment_service.os.path.isfile", return_value=True), \
                mock.patch("core.services.environment_service.os.path.realpath", return_value=real), \
                mock.patch.object(EnvironmentService, "_get_python_version", return_value="3.13.16"), \
                mock.patch.object(EnvironmentService, "_discover_in_path", return_value=entries), \
                mock.patch.object(EnvironmentService, "_supports_venv", return_value=True):
            found = EnvironmentService.discover_python_versions()
        self.assertEqual(len(found), 1, found)

    def test_delete_blocked_copy_agrees_with_the_count(self):
        Script.objects.create(name="uses-env", code="print(1)", environment=self.env,
                              workspace=self.ws, created_by=self.admin)
        resp = self.client.get(reverse("cpanel:environment_detail", kwargs={"pk": self.env.pk}))
        self.assertContains(resp, "1 script still uses it.")


class LibraryAndTagTests(DataBase):
    def test_module_remove_control_is_a_labelled_button(self):
        js = (pathlib.Path(settings.BASE_DIR) / "static/js/library_form.js").read_text(encoding="utf-8")
        self.assertIn("x.setAttribute('aria-label', 'Remove ' + filename);", js)
        self.assertNotIn("var x = document.createElement('span');", js)

    def test_version_history_uses_a_short_diff_action(self):
        lib = Library.objects.create(key="w22lib", name="W22 lib", workspace=self.ws)
        lib.save_revision({"a.py": "x = 1\n"})
        lib.save_revision({"a.py": "x = 2\n"})
        resp = self.client.get(reverse("cpanel:library_detail", kwargs={"pk": lib.pk}))
        self.assertRegex(resp.content.decode(), r">\s*Diff\s*</a>")
        self.assertNotContains(resp, "View &amp; diff\n")

    def test_swatches_are_named_and_members_get_no_actions_column(self):
        create = self.client.get(reverse("cpanel:tag_create")).content.decode()
        self.assertIn('aria-label="Red"', create)
        self.assertIn('aria-pressed="false"', create)
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(reverse("cpanel:tag_list")), ">Actions</th>")


class OwnedDataStoreTests(DataBase):
    def setUp(self):
        super().setUp()
        self.store = DataStore.objects.create(
            name="brand_tracker:mentions", workspace=self.ws, owner_plugin="brand_tracker",
        )

    def test_detail_says_who_manages_it(self):
        resp = self.client.get(reverse("cpanel:datastore_detail", kwargs={"pk": self.store.pk}))
        self.assertContains(resp, "Managed by the “brand_tracker” plugin")

    def test_list_offers_superusers_a_force_delete_only(self):
        html = self.client.get(reverse("cpanel:datastore_list")).content.decode()
        self.assertIn("Force delete", html)
        self.assertIn('name="force" value="1"', html)
        self.client.force_login(self.member)
        html = self.client.get(reverse("cpanel:datastore_list")).content.decode()
        self.assertNotIn("Force delete", html)
        self.assertIn("Plugin-managed", html)


class SlowQueryFilterTests(TestCase):
    def test_explorer_statements_are_recognised(self):
        from core.services.database_explorer import is_viewer_statement

        for q in ("BEGIN READ ONLY", "SELECT set_config($1, $2, $3)",
                  'SELECT * FROM "leads" LIMIT $1 OFFSET $2',
                  "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"):
            self.assertTrue(is_viewer_statement(q), q)
        for q in ("SELECT * FROM leads WHERE id = $1", "INSERT INTO leads (name) VALUES ($1)",
                  'SELECT * FROM "leads" WHERE score > $1'):
            self.assertFalse(is_viewer_statement(q), q)
