"""
``manage.py recaptcha`` — show or switch off the login reCAPTCHA from the shell.

The way back in when a wrong site or secret key blocks every sign-in (the
widget fails, so no one can reach Settings → Security to fix it). Takes effect
on the next page load; no restart needed.

    python manage.py recaptcha status
    python manage.py recaptcha off
"""

from django.core.management.base import BaseCommand

from core.models.settings import GlobalSettings


class Command(BaseCommand):
    help = "Show the login reCAPTCHA state, or switch it off (status | off)."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["status", "off"])

    def handle(self, *args, **options):
        settings = GlobalSettings.get_settings()
        if options["action"] == "off":
            if not settings.recaptcha_enabled:
                self.stdout.write("reCAPTCHA is already off.")
                return
            settings.recaptcha_enabled = False
            settings.save(update_fields=["recaptcha_enabled"])
            self.stdout.write(self.style.SUCCESS(
                "reCAPTCHA switched off. Sign in, then fix the keys under Settings → Security "
                "before switching it on again."
            ))
            return

        state = "on" if settings.recaptcha_enabled else "off"
        self.stdout.write(f"reCAPTCHA is {state}.")
        self.stdout.write(f"  Site key set:   {'yes' if settings.recaptcha_site_key else 'no'}")
        self.stdout.write(
            f"  Secret key set: {'yes' if settings.recaptcha_secret_key_encrypted else 'no'}"
        )
        self.stdout.write(
            f"  Active on the login page: {'yes' if settings.recaptcha_active() else 'no'}"
        )
