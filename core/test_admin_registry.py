"""The Django admin exposes only the Plugin model (F69, 2.0 release test)."""

from django.contrib import admin
from django.test import SimpleTestCase

from core.models import Plugin


class AdminRegistryTests(SimpleTestCase):
    def test_only_plugin_is_registered(self):
        self.assertEqual(set(admin.site._registry), {Plugin})
