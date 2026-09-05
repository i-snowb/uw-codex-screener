from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_public_release.py"
SPEC = importlib.util.spec_from_file_location("build_public_release", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class PublicReleaseTests(unittest.TestCase):
    def test_secret_assignment_pattern_covers_shell_exports(self) -> None:
        assignment = b"export UNUSUAL_WHALES_API_KEY=not-a-real-key\n"
        match = release.SECRET_ASSIGNMENT.search(assignment)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(b"not-a-real-key", match.group(1))

    def test_public_tree_rejects_non_source_provider_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.object(release, "ROOT", Path(temporary)):
            candidate = Path(temporary) / "tests" / "provider-capture.json"
            candidate.parent.mkdir()
            candidate.write_text('{"provider":"unusual_whales"}\n', encoding="utf-8")
            paths = {path.relative_to(release.ROOT).as_posix() for path in release._source_files()}
            self.assertNotIn("tests/provider-capture.json", paths)

    def test_sensitive_literal_pattern_covers_other_secret_names(self) -> None:
        payload = (
            b"ANALYST_ACCESS_" + b'TOKEN = "' + b"live_" + b'0123456789abcdef"\n'
        )
        match = release.SENSITIVE_LITERAL.search(payload)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertNotIn(match.group(2), release.SAFE_TEST_SECRET_LITERALS)

    def test_public_fixture_must_be_explicitly_synthetic(self) -> None:
        payload = json.dumps({
            "purpose": "synthetic test",
            "snapshots": [{"provider": "unusual_whales"}],
        }).encode()
        with self.assertRaisesRegex(ValueError, "non-fixture provider"):
            release._validate_synthetic_json(Path("fixtures/demo_morning_snapshot.json"), payload)

    def test_release_contains_source_shell_and_synthetic_fixture_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "public"
            result = release.build_release(output)
            paths = {item["path"] for item in result["files"]}

            self.assertIn("src/morning_edge/config.py", paths)
            self.assertIn("dashboard-app/index.html", paths)
            self.assertIn("fixtures/demo_morning_snapshot.json", paths)
            self.assertNotIn("scripts/build_historical_evidence_visualization.py", paths)
            self.assertNotIn("dashboard-app/data/latest.json", paths)
            self.assertFalse(any(path.startswith("outputs/") for path in paths))
            self.assertFalse(any(path.startswith("artifacts/") for path in paths))
            self.assertFalse(any(path.startswith("research/") for path in paths))
            self.assertFalse(any(path == ".env" for path in paths))
            self.assertFalse(result["private_runtime_paths_included"])
            self.assertTrue((output / "release-manifest.json").is_file())

    def test_release_refuses_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "public"
            output.mkdir()
            with self.assertRaises(FileExistsError):
                release.build_release(output)


if __name__ == "__main__":
    unittest.main()
