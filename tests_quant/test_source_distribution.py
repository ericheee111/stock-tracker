from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from stock_tracker.quant.data import DataSnapshotManifest, RawDataArtifact


class TestSourceDistribution(unittest.TestCase):
    """Guard against source packages being present locally but omitted by Git."""

    def test_quant_data_contract_imports(self) -> None:
        self.assertTrue(callable(RawDataArtifact))
        self.assertTrue(callable(DataSnapshotManifest))

    def test_critical_quant_data_files_are_tracked(self) -> None:
        root = Path(__file__).resolve().parents[1]
        if not (root / ".git").exists():
            self.skipTest("source-distribution check requires a Git checkout")

        critical = (
            "scripts/capture_quant_bars.py",
            "stock_tracker/quant/data/__init__.py",
            "stock_tracker/quant/data/bar_artifact.py",
            "stock_tracker/quant/data/manifest.py",
        )
        for relative_path in critical:
            with self.subTest(path=relative_path):
                result = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", relative_path],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    msg=(
                        f"critical source file is not tracked: {relative_path}\n"
                        f"stdout={result.stdout}\nstderr={result.stderr}"
                    ),
                )


if __name__ == "__main__":
    unittest.main()
