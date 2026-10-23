"""
Console shell (UI audit UX1 / UX12).

Every console page extends ``cpanel/layout.html``: one sidebar, one wide page
wrapper, one header (breadcrumb, h1, subtitle, actions) and the skip link. These
tests lock that so a new page can't drift back to a hand-copied wrapper with its
own width, and cover the header fixes that came with it.
"""

import re

from django.test import TestCase
from django.urls import reverse

from core.models import Environment, GlobalSettings, User, Workspace, WorkspaceMembership

PW = "correct-horse-battery-staple-91"

# Pages that take no URL arguments; each must render inside the shell.
SHELL_PAGES = [
    "dashboard", "script_list", "run_list", "tasks", "library_list", "library_create",
    "environment_list", "environment_create", "secret_list", "secret_create", "tag_list", "tag_create",
    "datastore_list", "datastore_create", "channel_list", "channel_create", "brain_list", "brain_create",
    "connect", "services", "settings", "workspace_list", "user_list", "logs", "changelog", "plugin_list",
    "plugin_upload", "api_token_list", "api_token_create",
]


class ShellBase(TestCase):
    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.get_or_create(name="default", defaults={"is_default": True, "python_version": "3.12"})
        self.ws = Workspace.get_default()
        self.admin = User.objects.create_user(
            username="admin@example.com", email="admin@example.com", password=PW, is_superuser=True, is_staff=True,
        )
        WorkspaceMembership.ensure(self.admin, self.ws, role=WorkspaceMembership.ROLE_OWNER)
        self.member = User.objects.create_user(username="m@example.com", email="m@example.com", password=PW)
        WorkspaceMembership.ensure(self.member, self.ws, role=WorkspaceMembership.ROLE_MEMBER)


class ShellPagesTests(ShellBase):
    def test_every_page_renders_in_the_one_shell(self):
        self.client.force_login(self.admin)
        for name in SHELL_PAGES:
            with self.subTest(page=name):
                resp = self.client.get(reverse(f"cpanel:{name}"))
                self.assertEqual(resp.status_code, 200)
                html = resp.content.decode()
                self.assertEqual(html.count('id="app-sidebar"'), 1)
                self.assertEqual(html.count('id="main"'), 1)
                self.assertIn('href="#main"', html)  # skip link
                self.assertIn("max-w-[1440px] mx-auto w-full", html)
                # no page re-centres itself in a narrow column any more
                self.assertNotRegex(html, r'py-7 max-w-(2xl|3xl|4xl|\[840px\]) mx-auto')
                self.assertEqual(len(re.findall(r"<h1\b", html)), 1)

    def test_editor_keeps_its_full_height_layout_inside_the_shell(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:script_create")).content.decode()
        self.assertEqual(html.count('id="main"'), 1)
        self.assertIn("sticky top-16", html)
        self.assertNotIn("max-w-[1440px] mx-auto w-full space-y-5", html)

    def test_header_actions_get_their_own_row_on_phones(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:secret_list")).content.decode()
        self.assertIn('<header class="flex flex-col sm:flex-row sm:items-start sm:justify-between', html)
        self.assertIn('<nav aria-label="Breadcrumb" class="crumbs', html)


class HeaderFixTests(ShellBase):
    def test_dashboard_has_a_title_and_health_is_a_pill(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:dashboard")).content.decode()
        self.assertRegex(html, r"<h1 [^>]*>Dashboard</h1>")
        self.assertIn("rounded-full bg-warn/10 text-warn", html)  # no worker in tests -> not "operational"

    def test_members_page_uses_the_breadcrumb_not_a_back_chevron(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:workspace_members", kwargs={"pk": self.ws.pk})).content.decode()
        self.assertIn(f'<a href="{reverse("cpanel:workspace_list")}">Workspaces</a>', html)
        self.assertNotIn('title="Back to workspaces"', html)

    def test_token_breadcrumb_only_links_settings_for_admins(self):
        settings_crumb = f'<a href="{reverse("cpanel:settings")}">Settings</a>'
        self.client.force_login(self.member)
        self.assertNotIn(settings_crumb, self.client.get(reverse("cpanel:api_token_list")).content.decode())
        self.client.force_login(self.admin)
        self.assertIn(settings_crumb, self.client.get(reverse("cpanel:api_token_list")).content.decode())

    def test_brains_list_names_the_section_once(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:brain_list")).content.decode()
        self.assertNotIn('class="text-sm text-muted">Brains</a>', html)
        self.assertRegex(html, r"<h1 [^>]*>Brains</h1>")


class PagePatternTests(ShellBase):
    """UX2 / UX3 / UX7 / UX14: forms with a side panel, wide detail pages, Brain tabs, footers."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_form_pages_put_help_in_a_side_panel(self):
        html = self.client.get(reverse("cpanel:secret_create")).content.decode()
        self.assertIn('class="lg:col-span-2 max-w-3xl w-full rounded-2xl', html)
        self.assertRegex(html, r'<aside class="space-y-5">\s*<div class="rounded-2xl border border-line bg-panel p-5')

    def test_channel_edit_is_two_columns_with_cancel(self):
        from core.models import Channel

        channel = Channel.objects.create(workspace=self.ws, provider="telegram", name="ops", config={}, created_by=self.admin)
        html = self.client.get(reverse("cpanel:channel_edit", kwargs={"pk": channel.pk})).content.decode()
        self.assertIn('<aside class="space-y-5 min-w-0">', html)  # inbound + approval side column
        self.assertIn(">Cancel</a>", html)
        self.assertNotIn(">Back</a>", html)

    def test_plugin_detail_title_is_the_page_header(self):
        from core.models import Plugin

        Plugin.objects.create(slug="demo_plugin", name="Demo Plugin", status="errored", error_message="boom", manifest={})
        html = self.client.get(reverse("cpanel:plugin_detail", kwargs={"slug": "demo_plugin"})).content.decode()
        self.assertRegex(html, r'<h1 class="text-\[22px\][^"]*">Demo Plugin</h1>')
        self.assertIn('<aside class="space-y-5 min-w-0">', html)  # about / metadata side column

    def test_connect_has_a_side_panel(self):
        html = self.client.get(reverse("cpanel:connect")).content.decode()
        self.assertRegex(html, r'<aside class="space-y-5">\s*<div[^>]*>\s*<h2 class="font-semibold">Token catalog</h2>')


class BrainSectionTests(ShellBase):
    def setUp(self):
        super().setUp()
        from core.brain.service import BrainService

        self.brain = BrainService.create(self.ws, "team-notes", name="Team notes", author=self.admin)
        self.client.force_login(self.admin)

    def test_tabs_mark_the_current_section_and_export_is_a_header_action(self):
        html = self.client.get(reverse("cpanel:brain_settings", args=[self.brain.pk])).content.decode()
        self.assertRegex(html, r'bg-panel-hi text-text font-medium"\s*aria-current="page">Settings</a>')
        self.assertEqual(html.count('aria-current="page"'), 1)
        nav = html[html.index('aria-label="Brain sections"'):]
        nav = nav[:nav.index("</nav>")]
        self.assertNotIn("Export ZIP", nav)
        self.assertIn("Export ZIP</a>", html)

    def test_settings_form_names_blank_choices_and_has_a_footer(self):
        html = self.client.get(reverse("cpanel:brain_settings", args=[self.brain.pk])).content.decode()
        self.assertIn(">Use the active provider</option>", html)
        self.assertIn(">No vector collection</option>", html)
        self.assertNotIn(">---------</option>", html)
        self.assertIn(">Cancel</a>", html)

    def test_reader_uses_the_console_fields_not_as_p(self):
        html = self.client.get(reverse("cpanel:brain_reader", args=[self.brain.pk])).content.decode()
        self.assertIn('data-field="task"', html)
        self.assertNotIn("<p>\n    <label", html)

    def test_create_form_shows_archive_and_url_per_source(self):
        html = self.client.get(reverse("cpanel:brain_create")).content.decode()
        self.assertIn('data-field="archive"', html)
        self.assertIn('data-field="github_url"', html)
        self.assertIn("source.value !== 'zip'", html)


class NoAccessPageTests(ShellBase):
    """UX6: one in-console "no access" page instead of silent dashboard bounces."""

    ADMIN_PAGES = ["settings", "services", "logs", "user_list", "plugin_list", "plugin_upload", "environment_create"]

    def test_member_on_an_admin_page_gets_the_403_inside_the_shell(self):
        self.client.force_login(self.member)
        for name in self.ADMIN_PAGES:
            with self.subTest(page=name):
                resp = self.client.get(reverse(f"cpanel:{name}"))
                self.assertContains(resp, "Only instance admins can open this page.", status_code=403)
                self.assertContains(resp, 'id="app-sidebar"', status_code=403)
                self.assertTemplateUsed(resp, "cpanel/error_shell.html")

    def test_signed_out_visitor_still_goes_to_login(self):
        resp = self.client.get(reverse("cpanel:settings"))
        self.assertRedirects(resp, reverse("auth:login") + "?next=" + reverse("cpanel:settings"),
                             fetch_redirect_response=False)

    def test_member_denied_py_ai_sees_why(self):
        self.client.force_login(self.member)
        resp = self.client.get(reverse("cpanel:pyai"))
        self.assertContains(resp, "Py AI is available to workspace owners and admins.", status_code=403)
        self.assertContains(resp, 'id="app-sidebar"', status_code=403)

    def test_404_keeps_the_sidebar_when_signed_in_only(self):
        self.client.force_login(self.member)
        resp = self.client.get("/cpanel/does-not-exist/")
        self.assertEqual(resp.status_code, 404)
        self.assertTemplateUsed(resp, "cpanel/error_shell.html")
        self.assertContains(resp, 'id="app-sidebar"', status_code=404)
        self.client.logout()
        resp = self.client.get("/cpanel/does-not-exist/")
        if resp.status_code == 404:  # (cpanel may send anonymous visitors to login first)
            self.assertTemplateUsed(resp, "error_base.html")
            self.assertNotContains(resp, 'id="app-sidebar"', status_code=404)


class ServicesLandingTests(ShellBase):
    """UX5: a fresh Services page opens at the top with only one card expanded."""

    def test_fresh_instance_opens_one_card_and_focuses_nothing(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("cpanel:services")).content.decode()
        init = html[html.index("document.addEventListener('DOMContentLoaded'"):]
        self.assertIn("toggleS3Config();", init)  # storage is the first unconfigured card
        self.assertNotIn("toggleClaudeConfig();", init)
        self.assertNotIn("toggleEmbeddingConfig();", init)
        self.assertIn("if (focus) aiFormField('name').focus();", html)
        self.assertIn('aria-label="Services on this page"', html)
        for anchor in ("storage", "ai", "secret-providers", "embeddings", "vector-stores"):
            self.assertIn(f'href="#{anchor}"', html)
            self.assertIn(f'id="{anchor}"', html)


class ListAndDetailPolishTests(ShellBase):
    """UX10 / UX11 / UX13 / UX17-UX20."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_scripts_list_has_a_name_filter_and_phone_columns(self):
        from core.models import Script

        Script.objects.create(name="Nightly sync", workspace=self.ws, environment=Environment.objects.get(name="default"))
        html = self.client.get(reverse("cpanel:script_list")).content.decode()
        self.assertIn('id="script-search"', html)  # server-side search (D14) replaced the client filter
        self.assertIn("bg-panel-hi text-muted whitespace-nowrap", html)  # env chip stays on one line
        self.assertIn('<table class="table-cards w-full sm:min-w-[820px]">', html)
        self.assertIn('data-label="Environment"', html)

    def test_environments_list_is_a_table(self):
        html = self.client.get(reverse("cpanel:environment_list")).content.decode()
        self.assertIn('<table class="table-cards w-full sm:min-w-[760px]">', html)
        self.assertIn(">Packages</a>", html)

    def test_users_page_is_a_filterable_table(self):
        html = self.client.get(reverse("cpanel:user_list")).content.decode()
        self.assertIn('id="user-filter"', html)
        self.assertIn('data-user-email="m@example.com"', html)
        self.assertIn(">Workspaces</th>", html)

    def test_changelog_stacks_the_change_type_on_phones(self):
        html = self.client.get(reverse("cpanel:changelog")).content.decode()
        self.assertIn("flex flex-col sm:flex-row items-start", html)
        self.assertIn('<div class="relative max-w-[72ch]">', html)

    def test_data_store_detail_is_compact(self):
        from core.models import DataStore

        store = DataStore.objects.create(name="orders", workspace=self.ws, created_by=self.admin)
        html = self.client.get(reverse("cpanel:datastore_detail", kwargs={"pk": store.pk})).content.decode()
        self.assertIn("grid grid-cols-3 divide-x", html)
        self.assertIn("How to use this data store in scripts", html)
        self.assertRegex(html, r'<details class="rounded-2xl border border-line bg-panel p-5 group">')

    def test_sidebar_footer_moves_developer_links_to_settings(self):
        html = self.client.get(reverse("cpanel:dashboard")).content.decode()
        sidebar = html[html.index('id="app-sidebar"'):html.index("</aside>")]
        self.assertNotIn("Developer:", sidebar)
        self.assertIn("xl:hidden flex-shrink-0 border-t", sidebar)
        self.assertIn(">Discord</a>", sidebar)
        settings_html = self.client.get(reverse("cpanel:settings")).content.decode()
        self.assertIn("About PyRun", settings_html)
        self.assertIn("https://x.com/hasan_ab_hasan", settings_html)

    def test_member_role_select_is_labelled(self):
        html = self.client.get(reverse("cpanel:workspace_members", kwargs={"pk": self.ws.pk})).content.decode()
        self.assertIn('aria-label="Role for m@example.com"', html)


class PhoneCardRowsTests(ShellBase):
    """UX10 / U104 / U84 / U86: list tables become card rows below sm; Logs too."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def _seed(self):
        from core.models import DataStore, Run, Script, Secret, Tag

        env = Environment.objects.get(name="default")
        script = Script.objects.create(name="Nightly sync", workspace=self.ws, environment=env)
        Run.objects.create(script=script, workspace=self.ws, status="failed", stderr="boom", code_snapshot="")
        Tag.objects.create(name="etl", color="blue", created_by=self.admin)
        secret = Secret(key="API_KEY", workspace=self.ws, created_by=self.admin)
        secret.set_value("local-dummy")
        secret.save()
        DataStore.objects.create(name="orders", workspace=self.ws, created_by=self.admin)

    def test_list_tables_render_as_cards_on_phones(self):
        self._seed()
        cases = {
            "script_list": ['data-label="Status"', 'data-label="Updated"'],
            "run_list": ['data-label="Status"', 'data-label="Started"', 'class="tc-hide'],
            "secret_list": ['data-label="Value"', 'class="tc-actions'],
            "tag_list": ['data-label="Scripts"', 'class="tc-actions'],
            "datastore_list": ['data-label="Entries"', 'class="tc-actions'],
            "environment_list": ['data-label="Python"', 'class="tc-actions'],
            "user_list": ['data-label="Joined"', 'class="tc-full'],
            "tasks": [],
            "library_list": [],
            "channel_list": [],
            "plugin_list": [],
        }
        for name, needles in cases.items():
            with self.subTest(page=name):
                html = self.client.get(reverse(f"cpanel:{name}")).content.decode()
                for needle in needles:
                    self.assertIn(needle, html)
                if needles:
                    self.assertIn('<table class="table-cards ', html)
                    # every column is in the card; none is just hidden on phones
                    self.assertNotIn("hidden sm:table-cell", html)

    def test_logs_rows_are_cards_and_open_once(self):
        html = self.client.get(reverse("cpanel:logs")).content.decode()
        # the server template and the auto-refresh JS build the same row
        self.assertNotIn("min-w-[760px]\">", html.replace("sm:min-w-[760px]", ""))
        self.assertGreaterEqual(html.count('data-label="Logger"'), 1)
        self.assertIn('class="log-preview block truncate', html)
        self.assertIn('<span class="log-hide text-xs text-faint">Hide</span>', html)

    def test_card_css_is_compiled(self):
        from pathlib import Path

        from django.conf import settings

        css = (Path(settings.BASE_DIR) / "theme" / "static" / "css" / "dist" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("table.table-cards>tbody>tr:not([hidden])", css)
        self.assertIn("details.log-entry[open] .log-preview{", css)

    def test_member_rows_stack_on_phones(self):
        """U83: the role select and Remove go under the email below sm."""
        html = self.client.get(reverse("cpanel:workspace_members", kwargs={"pk": self.ws.pk})).content.decode()
        self.assertIn("flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 p-4", html)
        self.assertIn("break-all sm:truncate", html)


class InlineFormErrorTests(ShellBase):
    """U69 / UX14: refused console forms show each error under its field (the
    widget marked aria-invalid) and a summary above the form."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_partials_are_used_by_every_console_form(self):
        from pathlib import Path

        from django.conf import settings

        root = Path(settings.BASE_DIR) / "templates" / "cpanel"
        for name in ("secrets/create.html", "tags/create.html", "datastores/entry_create.html",
                     "environments/create.html", "libraries/create.html", "channels/create.html",
                     "vectors/create.html", "api_tokens/create.html", "plugins/upload.html", "brains/fields.html"):
            with self.subTest(template=name):
                text = (root / name).read_text(encoding="utf-8")
                self.assertIn('{% include "cpanel/_form_errors.html"', text)
                self.assertNotIn(".errors.0 }}", text)

    def test_bad_json_entry_shows_field_error_and_summary(self):
        from core.models import DataStore

        store = DataStore.objects.create(name="orders", workspace=self.ws, created_by=self.admin)
        resp = self.client.post(reverse("cpanel:datastore_entry_create", kwargs={"pk": store.pk}),
                                {"key": "order_1", "value": "{not json"})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('id="id_value_error"', html)
        self.assertRegex(html, r'<textarea[^>]*name="value"[^>]*aria-invalid="true"|<textarea[^>]*aria-invalid="true"[^>]*name="value"')
        self.assertIn("data-form-errors", html)
        self.assertIn('href="#id_value"', html)
        self.assertIn('value="order_1"', html)

    def test_non_field_error_is_in_the_summary(self):
        from django import forms as djforms
        from django.template.loader import render_to_string

        class F(djforms.Form):
            name = djforms.CharField()

            def clean(self):
                raise djforms.ValidationError("These two fields clash.")

        form = F({"name": ""})
        form.is_valid()
        html = render_to_string("cpanel/_form_errors.html", {"form": form})
        self.assertIn("These two fields clash.", html)
        self.assertIn('href="#id_name"', html)
        self.assertEqual(render_to_string("cpanel/_form_errors.html", {"form": F()}).strip(), "")

    def test_refused_ai_provider_save_reopens_the_form(self):
        resp = self.client.post(reverse("cpanel:ai_provider_save"),
                                {"provider_type": "custom", "name": "", "base_url": "", "default_model": "typed-model"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["ai_form_open"])
        html = resp.content.decode()
        self.assertIn('<div id="ai-provider-form-wrap" class="border', html)
        self.assertIn('id="id_name_error"', html)
        self.assertIn('value="typed-model"', html)
        self.assertNotIn("name: ", "".join(str(m) for m in resp.wsgi_request._messages))

    def test_refused_ai_settings_shows_the_error_under_active_provider(self):
        import uuid

        resp = self.client.post(reverse("cpanel:claude_settings"),
                                {"claude_enabled": "on", "active_provider": str(uuid.uuid4())})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('id="id_active_provider_error"', html)
        self.assertIn("That provider no longer exists", html)


class WideScreenAsideTests(ShellBase):
    """§26 re-run on rc28 (F115): at 640-1280 px the Users table's sr-only header
    escaped its scroll box and widened the page, and the narrow side column of
    the detail pages (from lg up) kept the phone/tablet grids and fixed-width
    confirm inputs, so they ran past the page edge or drew over each other."""

    def setUp(self):
        super().setUp()
        from core.brain.service import BrainService

        self.brain = BrainService.create(self.ws, "team-notes", name="Team notes", author=self.admin)
        self.client.force_login(self.admin)

    def test_users_table_scroll_box_contains_its_sr_only_header(self):
        html = self.client.get(reverse("cpanel:user_list")).content.decode()
        self.assertRegex(html, r'<div class="relative overflow-x-auto">\s*<table class="table-cards')

    def test_expired_invite_badge_stays_whole_next_to_a_long_email(self):
        from datetime import timedelta

        from django.utils import timezone

        from core.models import UserInvite

        email = "a-very-long-invitee-address-for-phones@example.invalid"
        inv = UserInvite.create_invite(email, self.admin)
        UserInvite.objects.filter(pk=inv.pk).update(expires_at=timezone.now() - timedelta(days=1))
        html = self.client.get(reverse("cpanel:user_list")).content.decode()
        # the email truncates on its own; the badge sits outside the truncated span
        self.assertIn(f'<span class="truncate">{email}</span><span class="flex-shrink-0 ', html)

    def test_brain_delete_confirm_fits_the_side_column(self):
        html = self.client.get(reverse("cpanel:brain_settings", args=[self.brain.pk])).content.decode()
        self.assertIn('class="block text-sm w-full sm:w-auto lg:w-full"', html)
        self.assertIn('name="confirm_slug" autocomplete="off" required class="w-full sm:w-64 lg:w-full ', html)

    def test_detail_asides_use_side_column_grids(self):
        from pathlib import Path

        from django.conf import settings

        tpl = Path(settings.BASE_DIR) / "templates" / "cpanel"
        db = (tpl / "databases" / "detail.html").read_text(encoding="utf-8")
        self.assertIn('grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-1 gap-x-6', db)
        self.assertEqual(db.count('<dd class="font-mono text-text mt-0.5 break-all">'), 4)
        self.assertIn('class="w-full sm:w-80 lg:w-full px-3', db)
        vec = (tpl / "vectors" / "detail.html").read_text(encoding="utf-8")
        self.assertIn('grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-2 gap-x-6', vec)
        self.assertIn('class="w-full sm:w-80 lg:w-full px-3', vec)
        plug = (tpl / "plugins" / "detail.html").read_text(encoding="utf-8")
        self.assertIn('grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-2 gap-x-4', plug)

    def test_side_column_utilities_are_compiled(self):
        from pathlib import Path

        from django.conf import settings

        css = (Path(settings.BASE_DIR) / "theme" / "static" / "css" / "dist" / "styles.css").read_text(encoding="utf-8")
        for rule in ("lg\:grid-cols-1{", "lg\:grid-cols-2{", "lg\:w-full{", "sm\:w-auto{"):
            self.assertIn(rule, css)
