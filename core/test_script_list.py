"""Scripts list: pagination, name/description search and combinable filters (D14, U114)."""

from django.urls import reverse

from core.models import Environment, Script, Tag
from core.test_console_layout import ShellBase
from core.views.scripts import SCRIPTS_PER_PAGE


class ScriptListPaginationSearchTests(ShellBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.env = Environment.objects.get(name="default")

    def _make(self, name, **kw):
        return Script.objects.create(name=name, workspace=self.ws, environment=self.env, **kw)

    def _get(self, **params):
        return self.client.get(reverse("cpanel:script_list"), params)

    def test_paginates_at_25_with_the_runs_pager(self):
        for i in range(SCRIPTS_PER_PAGE + 5):
            self._make(f"bulk-{i:02d}")
        r = self._get()
        self.assertEqual(SCRIPTS_PER_PAGE, 25)
        self.assertEqual(len(r.context["scripts"]), 25)
        html = r.content.decode()
        self.assertIn('aria-label="Pagination"', html)
        self.assertIn("?page=2", html)
        self.assertIn("30", html)  # subtitle + pager show the full count, not the page length
        r2 = self._get(page=2)
        self.assertEqual(len(r2.context["scripts"]), 5)
        # An out-of-range page falls back to the last page instead of 404.
        self.assertEqual(self._get(page=99).context["page_obj"].number, 2)

    def test_search_matches_name_and_description(self):
        self._make("Invoice mailer")
        self._make("Nightly job", description="Sends the INVOICE digest")
        self._make("Unrelated")
        names = {s.name for s in self._get(q="invoice").context["scripts"]}
        self.assertEqual(names, {"Invoice mailer", "Nightly job"})
        html = self._get(q="invoice").content.decode()
        self.assertIn('value="invoice"', html)

    def test_filters_search_and_page_combine_in_the_url(self):
        tag = Tag.objects.create(name="reports")
        for i in range(SCRIPTS_PER_PAGE + 2):
            s = self._make(f"report-{i:02d}", is_enabled=(i % 2 == 0))
            s.tags.add(tag)
        self._make("report-untagged")
        self._make("report-plugin", owner_plugin="brand_tracker").tags.add(tag)

        r = self._get(q="report", tag=tag.pk, status="enabled")
        enabled_tagged = 14 + 1  # 14 even of 27 + the plugin one (is_enabled default True)
        self.assertEqual(r.context["page_obj"].paginator.count, enabled_tagged)
        r = self._get(q="report", tag=tag.pk, owner_plugin="brand_tracker")
        self.assertEqual([s.name for s in r.context["scripts"]], ["report-plugin"])

        r = self._get(q="report", tag=tag.pk)
        html = r.content.decode()
        # Pager links keep every filter; status tabs keep search + tag but reset the page.
        self.assertIn(f"?page=2&q=report&amp;tag={tag.pk}", html)
        self.assertIn(f"?status=enabled&q=report&amp;tag={tag.pk}", html)
        # The search form carries the other filters as hidden fields.
        self.assertIn(f'name="tag" value="{tag.pk}"', html)
        page2 = self._get(q="report", tag=tag.pk, page=2)
        self.assertEqual(page2.context["page_obj"].number, 2)
        self.assertEqual(page2.context["querystring"], f"q=report&tag={tag.pk}")

    def test_empty_result_state_names_the_search(self):
        self._make("Alpha")
        html = self._get(q="zzz-nothing").content.decode()
        self.assertIn("No scripts match these filters", html)
        self.assertIn("Nothing matches “zzz-nothing”", html)
        self.assertIn("Clear filters", html)
        self.assertNotIn('aria-label="Pagination"', html)

    def test_empty_workspace_still_invites_the_first_script(self):
        html = self._get().content.decode()
        self.assertIn("No scripts yet", html)
        self.assertIn("Create script", html)

    def test_search_is_workspace_scoped_and_hides_archived_by_default(self):
        from django.utils import timezone

        from core.models import Workspace

        other = Workspace.objects.create(name="Other")
        Script.objects.create(name="secret-other", workspace=other, environment=self.env)
        self._make("secret-archived", archived_at=timezone.now())
        self.assertEqual(list(self._get(q="secret").context["scripts"]), [])
        archived = self._get(q="secret", status="archived").context["scripts"]
        self.assertEqual([s.name for s in archived], ["secret-archived"])
