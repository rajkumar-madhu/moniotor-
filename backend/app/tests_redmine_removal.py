from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import Resolver404, resolve


PROJECT_DIR = Path(__file__).resolve().parents[1]


class RedmineRemovalContractTests(SimpleTestCase):
    def _read_project_file(self, relative_path):
        return (PROJECT_DIR / relative_path).read_text(encoding="utf-8")

    def test_redmine_settings_and_ticket_app_are_removed(self):
        redmine_settings = [name for name in dir(settings) if name.startswith("REDMINE_")]

        self.assertEqual([], redmine_settings)
        self.assertNotIn("ticket", settings.INSTALLED_APPS)

    def test_ticket_routes_are_removed(self):
        with self.assertRaises(Resolver404):
            resolve("/ticket/")

        with self.assertRaises(Resolver404):
            resolve("/useronboard/gettickets")

    def test_runtime_code_has_no_redmine_coupling(self):
        files_to_check = [
            "app/views.py",
            "app/adapter.py",
            "login/views.py",
            "useronboard/views.py",
            "lesites/models.py",
            "lesites/views.py",
            "sitehealth/views.py",
            "LinkedEyeWebProject/settings.py",
            "LinkedEyeWebProject/urls.py",
            "requirements.txt",
            "requirements_win.txt",
        ]
        forbidden = [
            "REDMINE_",
            "redmine.",
            "redmine_url",
            "Redmine(",
            "from redminelib",
            "python-redmine",
        ]

        failures = []
        for relative_path in files_to_check:
            content = self._read_project_file(relative_path)
            for token in forbidden:
                if token in content:
                    failures.append(f"{relative_path}: {token}")

        self.assertEqual([], failures)
