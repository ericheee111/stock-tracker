"""Canonical hashing for datasets, configs and governance evidence."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .time import to_utc


class FingerprintError(ValueError):
    """Raised when an object cannot be represented deterministically."""


def _canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FingerprintError("NaN and infinity are forbidden in fingerprints")
        return value
    if isinstance(value, Enum):
        return _canonicalize(value.value)
    if isinstance(value, datetime):
        return to_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonicalize(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise FingerprintError("mapping keys must be strings")
        pairs = sorted((key, _canonicalize(item)) for key, item in value.items())
        return {key: item for key, item in pairs}
    if isinstance(value, (set, frozenset)):
        items = [_canonicalize(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False),
        )
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if hasattr(value, "tolist"):
        return _canonicalize(value.tolist())
    raise FingerprintError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize *value* with stable ordering and temporal semantics."""

    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def fingerprint(value: Any) -> str:
    """Return SHA-256 over :func:`canonical_json`."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def hash_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading it fully into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dataset_fingerprint(*parts: Any) -> str:
    """Bind all named dataset partitions into one comparison identity."""

    return fingerprint({"schema": "dataset-fingerprint-v1", "parts": parts})
