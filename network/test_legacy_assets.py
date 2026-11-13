"""The legacy front-panel and onboarding-template assets live inside the network app (network/legacy/).

They used to sit at the repo root and under backend/, which the Docker image has to exclude, so the app now owns them.
The old locations are a fallback only when the app's own directory is missing (whole directory, not per file).
"""
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from network import legacy_panels

APP_LEGACY = Path(legacy_panels.__file__).resolve().parent / "legacy"


class LegacyAssetLocationTests(SimpleTestCase):
    def test_panels_and_templates_are_read_from_the_app(self):
        self.assertEqual(legacy_panels._iframe_dir(), APP_LEGACY / "iframeGraphs")
        self.assertEqual(legacy_panels._template_direct_dir(), APP_LEGACY / "template" / "DIRECT")

    def test_the_assets_are_there(self):
        self.assertGreater(len(list((APP_LEGACY / "iframeGraphs").glob("*.j2"))), 50)
        self.assertGreater(len(list((APP_LEGACY / "template" / "DIRECT").glob("*.yaml"))), 100)

    def test_the_old_repo_root_location_is_a_fallback_when_the_app_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "iframeGraphs").mkdir()
            (Path(tmp) / "template" / "DIRECT").mkdir(parents=True)
            with mock.patch.object(legacy_panels, "LEGACY_DIR", Path(tmp) / "absent"), override_settings(BASE_DIR=tmp):
                self.assertEqual(legacy_panels._iframe_dir(), Path(tmp) / "iframeGraphs")
                self.assertEqual(legacy_panels._template_direct_dir(), Path(tmp) / "template" / "DIRECT")

    def test_the_old_backend_location_is_the_last_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend" / "iframeGraphs").mkdir(parents=True)
            (root / "backend" / "template" / "DIRECT").mkdir(parents=True)
            with mock.patch.object(legacy_panels, "LEGACY_DIR", root / "absent"), override_settings(BASE_DIR=tmp):
                # backend/ alone: it is used
                self.assertEqual(legacy_panels._iframe_dir(), root / "backend" / "iframeGraphs")
                self.assertEqual(legacy_panels._template_direct_dir(), root / "backend" / "template" / "DIRECT")
                # repo root and backend/ both present: the repo root wins, so backend/ really is last
                (root / "iframeGraphs").mkdir()
                (root / "template" / "DIRECT").mkdir(parents=True)
                self.assertEqual(legacy_panels._iframe_dir(), root / "iframeGraphs")
                self.assertEqual(legacy_panels._template_direct_dir(), root / "template" / "DIRECT")

    def test_the_legacy_project_keeps_its_own_copies(self):
        # backend/ is a separate Django project that reads these by relative path from its own directory
        # (backend/app/views.py, backend/lib/LinkedEyeEntity/Node.py: iframeGraphs/; backend/addservice/views.py,
        # backend/vault/views.py: template/DIRECT/). Moving them into this app once broke those pages.
        backend = Path(legacy_panels.__file__).resolve().parents[1] / "backend"
        if not backend.is_dir():
            self.skipTest("no legacy backend/ in this checkout")
        self.assertTrue((backend / "iframeGraphs" / "neochart-query.json").is_file())
        self.assertTrue((backend / "template" / "DIRECT" / "HOSTS").is_dir())
        self.assertTrue((backend / "template" / "DIRECT" / "SERVICES").is_dir())

    def test_the_app_wins_over_the_old_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "iframeGraphs").mkdir()   # a stale copy at the repo root must not shadow the app's own
            with override_settings(BASE_DIR=tmp):
                self.assertEqual(legacy_panels._iframe_dir(), APP_LEGACY / "iframeGraphs")
