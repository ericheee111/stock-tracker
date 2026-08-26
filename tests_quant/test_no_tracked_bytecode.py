from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def test_python_bytecode_is_not_tracked() -> None:
    """Interpreter caches are reproducible local artifacts, never source evidence."""

    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        pytest.skip("tracked-file check requires a Git checkout")
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    tracked = [
        item.decode("utf-8", errors="strict")
        for item in result.stdout.split(b"\0")
        if item
    ]
    bytecode = [
        path
        for path in tracked
        if path.endswith((".pyc", ".pyo")) or "__pycache__/" in path
    ]
    assert not bytecode, f"Python bytecode must not be tracked: {bytecode}"
