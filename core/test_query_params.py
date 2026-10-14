"""Malformed query strings get a default, never a 500 (core/views/params.py)."""

from django.test import SimpleTestCase, TestCase

from core.models import Environment, Run, Script, User, Workspace
from core.test_plugin_api import _mock_setup
from core.views.params import positive_int


class PositiveIntTests(SimpleTestCase):
    def test_parses_and_falls_back(self):
        self.assertEqual(positive_int("3", 1), 3)
        for bad in (None, "", "abc", "1.5", "0", "-1"):
            with self.subTest(bad=bad):
                self.assertEqual(positive_int(bad, 7), 7)
        self.assertEqual(positive_int("999", 50, maximum=200), 200)


class MalformedQueryPageTests(TestCase):
    def setUp(self):
        _mock_setup(self)
        self.client.raise_request_exception = False
        admin = User.objects.create_superuser(username="qa", email="qa@params.test", password="x")
        self.client.force_login(admin)
        workspace = Workspace.get_default() or Workspace.objects.create(name="Default", is_default=True)
        script = Script.objects.create(
            name="probe", workspace=workspace, environment=Environment.objects.create(name="env")
        )
        Run.objects.create(script=script, workspace=workspace)
        self.script = script

    def test_bad_values_do_not_500(self):
        for url in (
            "/cpanel/tasks/?page=abc",
            "/cpanel/tasks/?page=-1",
            "/cpanel/logs/?page=abc",
            "/cpanel/logs/?page=0",
            "/cpanel/api/logs/?lines=abc",
            "/cpanel/runs/?script=not-a-uuid",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_runs_script_filter_still_filters(self):
        response = self.client.get(f"/cpanel/runs/?script={self.script.pk}")
        self.assertEqual(len(response.context["page_obj"].object_list), 1)
        response = self.client.get("/cpanel/runs/?script=not-a-uuid")
        self.assertEqual(len(response.context["page_obj"].object_list), 0)
