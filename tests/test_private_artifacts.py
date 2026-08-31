from __future__ import annotations

import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "private_artifacts.py"
SPEC = importlib.util.spec_from_file_location("private_artifacts_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
private_artifacts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(private_artifacts)


class PrivateArtifactTests(unittest.TestCase):
    def test_shared_parent_is_unchanged_and_artifact_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            parent.chmod(0o755)
            destination = parent / "artifact.json"
            private_artifacts.write_private_text(destination, "{}\n")
            self.assertEqual(0o755, stat.S_IMODE(parent.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(destination.stat().st_mode))

    def test_new_leaf_directory_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "nested" / "artifact.json"
            private_artifacts.write_private_text(destination, "{}\n")
            self.assertEqual(0o700, stat.S_IMODE(destination.parent.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(destination.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
