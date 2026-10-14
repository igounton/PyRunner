"""
Password policy tests — AUTH_PASSWORD_VALIDATORS must actually run.

Regression for review 6.1: settings declared the four standard validators
(similarity, min-length, common-password, numeric), but SetPasswordForm and
AdminSetupForm only checked min_length=8 + match — "12345678" and "password"
were accepted everywhere passwords are set, including for the instance's
first superuser. The forms now call validate_password(), so the
settings-declared policy is the single source of truth.
"""

from django.test import TestCase
from django.urls import reverse

from core.forms import AdminSetupForm, SetPasswordForm
from core.models import Environment, GlobalSettings, User, Workspace, WorkspaceMembership

STRONG = "correct-horse-battery-staple-91"


def _pw_data(password, confirm=None):
    return {"password": password, "password_confirm": confirm or password}


class SetPasswordFormPolicyTests(TestCase):
    def test_common_password_rejected(self):
        form = SetPasswordForm(_pw_data("password"))
        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)

    def test_all_numeric_password_rejected(self):
        form = SetPasswordForm(_pw_data("12345678"))
        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)

    def test_email_similar_password_rejected_when_user_given(self):
        user = User(email="hasan.aboulhasan@example.com")
        form = SetPasswordForm(_pw_data("hasan.aboulhasan"), user=user)
        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)

    def test_strong_password_accepted(self):
        form = SetPasswordForm(_pw_data(STRONG))
        self.assertTrue(form.is_valid(), form.errors)

    def test_mismatch_still_rejected(self):
        form = SetPasswordForm(_pw_data(STRONG, confirm=STRONG + "x"))
        self.assertFalse(form.is_valid())


class AdminSetupFormPolicyTests(TestCase):
    def _data(self, password, email="admin@example.com"):
        return {"email": email, "password": password, "password_confirm": password}

    def test_common_password_rejected(self):
        form = AdminSetupForm(self._data("password"))
        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)

    def test_email_similar_password_rejected(self):
        form = AdminSetupForm(
            self._data("first.admin.setup", email="first.admin.setup@example.com")
        )
        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)

    def test_strong_password_accepted(self):
        form = AdminSetupForm(self._data(STRONG))
        self.assertTrue(form.is_valid(), form.errors)


class ChangePasswordViewTests(TestCase):
    """The view must pass the user into the form — the seam that would
    silently disable the similarity check if it regressed."""

    def setUp(self):
        gs = GlobalSettings.get_settings()
        gs.setup_completed = True
        gs.save()
        Environment.objects.get_or_create(
            name="default", defaults={"is_default": True, "python_version": "3.12"}
        )
        # Superuser: without one, SetupWizardMiddleware 302s everything to
        # the admin-setup page and the view under test never runs.
        self.user = User.objects.create(
            email="worker.honey.bee@example.com", is_superuser=True
        )
        self.user.set_password(STRONG)
        self.user.save()
        WorkspaceMembership.ensure(self.user, Workspace.get_default())
        self.client.force_login(self.user)

    def test_weak_password_rejected_and_unchanged(self):
        resp = self.client.post(reverse("auth:change_password"), {**_pw_data("12345678"), "current_password": STRONG})

        self.assertEqual(resp.status_code, 200)  # re-rendered with errors
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(STRONG))  # unchanged

    def test_email_similar_password_rejected(self):
        resp = self.client.post(
            reverse("auth:change_password"), {**_pw_data("worker.honey.bee"), "current_password": STRONG}
        )

        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(STRONG))

    def test_strong_password_accepted_and_session_survives(self):
        # Also guards the success path itself: it used to call login()
        # without a backend argument, which 500s with multiple auth backends
        # (axes + ModelBackend) — found by this test, fixed with
        # update_session_auth_hash().
        new = "another-long-sturdy-passphrase-42"
        resp = self.client.post(reverse("auth:change_password"), {**_pw_data(new), "current_password": STRONG})

        self.assertEqual(resp.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new))
        # Session must still be authenticated (no forced logout).
        follow_up = self.client.get(reverse("auth:change_password"))
        self.assertEqual(follow_up.status_code, 200)

    def test_wrong_current_password_is_refused(self):
        new = "another-long-sturdy-passphrase-42"
        for current in ("", "not-my-password"):
            with self.subTest(current=current):
                resp = self.client.post(
                    reverse("auth:change_password"), {**_pw_data(new), "current_password": current}
                )
                self.assertEqual(resp.status_code, 200)
                self.user.refresh_from_db()
                self.assertTrue(self.user.check_password(STRONG))

    def test_member_returns_to_the_dashboard_and_sees_the_header_link(self):
        User.objects.create(email="root@example.com", is_superuser=True)  # keeps setup complete
        self.user.is_superuser = False
        self.user.save(update_fields=["is_superuser"])
        dashboard = self.client.get(reverse("cpanel:dashboard"))
        self.assertContains(dashboard, f'href="{reverse("auth:change_password")}"')
        page = self.client.get(reverse("auth:change_password"))
        self.assertContains(page, f'href="{reverse("cpanel:dashboard")}"')  # Cancel
        new = "another-long-sturdy-passphrase-42"
        resp = self.client.post(
            reverse("auth:change_password"), {**_pw_data(new), "current_password": STRONG}
        )
        self.assertRedirects(resp, reverse("cpanel:dashboard"), fetch_redirect_response=False)

    def test_account_without_a_password_sets_one_without_the_current_field(self):
        self.user.set_unusable_password()
        self.user.save()
        self.client.force_login(self.user)  # the hash change ended the old session
        self.assertNotContains(self.client.get(reverse("auth:change_password")), "id_current_password")
        new = "another-long-sturdy-passphrase-42"
        resp = self.client.post(reverse("auth:change_password"), _pw_data(new))
        self.assertEqual(resp.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new))
