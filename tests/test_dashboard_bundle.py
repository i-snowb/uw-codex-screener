from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_dashboard_bundle.py"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("build_dashboard_bundle", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DashboardBundleTests(unittest.TestCase):
    def test_local_only_cli_updates_shell_and_latest_without_portable_export(self) -> None:
        run = {
            "run_id": "2026-08-31T06:45:00-04:00",
            "cutoff_at": "2026-08-31T06:45:00-04:00",
            "generated_at": "2026-08-31T06:46:00-04:00",
            "mode": "SHADOW",
            "watchlist": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "run.json"
            source.write_text(json.dumps(run), encoding="utf-8")
            self.assertEqual(
                0,
                MODULE.main([
                    "--input", str(source),
                    "--app-root", str(root / "app"),
                    "--local-only",
                ]),
            )
            self.assertTrue((root / "app" / "index.html").is_file())
            self.assertTrue((root / "app" / "assets" / "app.js").is_file())
            latest = json.loads((root / "app" / "data" / "latest.json").read_text())
            self.assertEqual(run["run_id"], latest["dataVersion"])
            self.assertFalse((root / "portable.html").exists())

    def test_bundle_separates_data_and_keeps_portable_export(self) -> None:
        run = {
            "run_id": "2026-08-28T06:45:00-04:00",
            "cutoff_at": "2026-08-28T06:45:00-04:00",
            "generated_at": "2026-08-28T06:46:00-04:00",
            "mode": "SHADOW",
            "watchlist": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive.html"
            archive.write_text("frozen", encoding="utf-8")
            before = hashlib.sha256(archive.read_bytes()).hexdigest()
            manifest = MODULE.build_bundle(
                run=run,
                app_root=root / "app",
                portable_output=root / "portable.html",
                archive_reference=archive,
            )

            index = (root / "app" / "index.html").read_text(encoding="utf-8")
            script = (root / "app" / "assets" / "app.js").read_text(encoding="utf-8")
            daily = json.loads((root / "app" / "data" / "2026-08-28" / "run.json").read_text())
            portable = (root / "portable.html").read_text(encoding="utf-8")

            self.assertIn("./assets/app.css", index)
            self.assertIn("./assets/app.js", index)
            self.assertIn("fetch('./data/latest.json'", script)
            self.assertNotIn('const DATA={"', script)
            self.assertEqual([], daily["entries"])
            self.assertIn("const DATA=", portable)
            self.assertEqual(0o700, stat.S_IMODE((root / "app" / "data").stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE((root / "app" / "data" / "latest.json").stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE((root / "portable.html").stat().st_mode))
            self.assertEqual(before, hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertFalse(manifest["credentials_embedded"])
            self.assertTrue(manifest["research_only"])
            catalog = json.loads((root / "app" / "data" / "publications.json").read_text())
            self.assertEqual("codex-screener-publications-v1", catalog["schema_version"])
            self.assertEqual(1, len(catalog["entries"]))

            live_run = dict(run)
            live_run["run_id"] = "live-version"
            live_run["generated_at"] = "2026-08-28T10:00:00-04:00"
            published = MODULE.publish_latest_data(run=live_run, app_root=root / "app")
            latest = json.loads((root / "app" / "data" / "latest.json").read_text())
            self.assertEqual("live-version", latest["dataVersion"])
            self.assertEqual("live-version", published["data_version"])

            MODULE.build_bundle(
                run=run,
                app_root=root / "app",
                portable_output=root / "portable.html",
                archive_reference=archive,
            )
            changed = dict(run)
            changed["generated_at"] = "2026-08-28T06:47:00-04:00"
            with self.assertRaises(FileExistsError):
                MODULE.build_bundle(
                    run=changed,
                    app_root=root / "app",
                    portable_output=root / "portable.html",
                    archive_reference=archive,
                )

            revision = MODULE.build_bundle(
                run=changed,
                app_root=root / "app",
                portable_output=root / "portable-revision.html",
                archive_reference=archive,
            )
            self.assertEqual("CONTENT_ADDRESSED_REVISION", revision["publication_kind"])
            revision_path = Path(revision["files"]["daily_data"]["path"])
            self.assertIn("publications/2026-08-28", revision_path.as_posix())
            self.assertTrue(revision_path.is_file())
            catalog = json.loads((root / "app" / "data" / "publications.json").read_text())
            self.assertEqual(2, len(catalog["entries"]))
            canonical = json.loads((root / "app" / "data" / "2026-08-28" / "run.json").read_text())
            self.assertEqual("2026-08-28T06:46:00-04:00", canonical["generatedAt"])


if __name__ == "__main__":
    unittest.main()
