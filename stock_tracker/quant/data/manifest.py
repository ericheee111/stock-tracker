"""Tamper-evident raw-data artifacts and immutable dataset snapshots."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, cast

from stock_tracker.core.types import Market

from ..core.fingerprint import fingerprint, hash_file
from ..core.time import ensure_aware, to_utc


class ManifestContractError(ValueError):
    """Raised when raw-data identity or provenance is incomplete."""


class DataKind(StrEnum):
    MARKET_BARS = "MARKET_BARS"
    EXCHANGE_CALENDAR = "EXCHANGE_CALENDAR"
    INSTRUMENT_STATUS = "INSTRUMENT_STATUS"
    UNIVERSE_MEMBERSHIP = "UNIVERSE_MEMBERSHIP"
    SECTOR_MEMBERSHIP = "SECTOR_MEMBERSHIP"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    FUNDAMENTAL = "FUNDAMENTAL"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    MARKET_RULE = "MARKET_RULE"
    COST_SCHEDULE = "COST_SCHEDULE"
    FX_RATE = "FX_RATE"


class DataFormat(StrEnum):
    CSV = "CSV"
    JSON = "JSON"
    JSONL = "JSONL"
    PARQUET = "PARQUET"
    SQLITE = "SQLITE"
    TOML = "TOML"
    BINARY = "BINARY"


_STRUCTURED_FORMATS = {
    DataFormat.CSV,
    DataFormat.JSON,
    DataFormat.JSONL,
    DataFormat.PARQUET,
    DataFormat.SQLITE,
    DataFormat.TOML,
}
_MARKET_KINDS = set(DataKind)
_SECURITY_SET_KINDS = {
    DataKind.MARKET_BARS,
    DataKind.INSTRUMENT_STATUS,
    DataKind.UNIVERSE_MEMBERSHIP,
    DataKind.SECTOR_MEMBERSHIP,
    DataKind.CORPORATE_ACTION,
    DataKind.FUNDAMENTAL,
    DataKind.ANNOUNCEMENT,
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _expect_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ManifestContractError(f"{name} must be a string")
    return value


def _expect_str(value: object, name: str) -> str:
    result = _expect_text(value, name)
    if not result:
        raise ManifestContractError(f"{name} must be a non-empty string")
    return result


def _expect_optional_str(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _expect_str(value, name)


def _expect_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestContractError(f"{name} must be an integer")
    return value


def _expect_optional_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _expect_int(value, name)


def _expect_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ManifestContractError(f"{name} must be a boolean")
    return value


def _expect_dict(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ManifestContractError(f"{name} must be a JSON object with string keys")
    return cast(dict[str, Any], value)


def _expect_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ManifestContractError(f"{name} must be a JSON array")
    return cast(list[object], value)


def _expect_str_sequence(value: object, name: str) -> tuple[str, ...]:
    items = _expect_list(value, name)
    return tuple(_expect_str(item, f"{name} item") for item in items)


def _parse_datetime(value: object, name: str) -> datetime:
    text = _expect_str(value, name)
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ManifestContractError(f"{name} must be an ISO-8601 datetime") from exc


def _parse_optional_datetime(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value, name)


def validate_storage_key(value: str) -> str:
    """Validate a portable logical key rather than accepting host paths/URLs."""

    if not isinstance(value, str) or not value:
        raise ManifestContractError("storage_key must be a non-empty string")
    if "\\" in value:
        raise ManifestContractError("storage_key must use POSIX separators")
    if any(character in value for character in ("?", "#", "@", "=")):
        raise ManifestContractError("storage_key cannot contain URL/credential syntax")
    if ":" in value:
        raise ManifestContractError("storage_key cannot contain a scheme or drive")
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise ManifestContractError("storage_key must be a canonical relative key")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ManifestContractError("storage_key cannot be absolute or traverse parents")
    if pure.as_posix() != value:
        raise ManifestContractError("storage_key is not canonical POSIX form")
    return value


def _is_link(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", lambda _: False)
    return path.is_symlink() or bool(isjunction(path))


def safe_artifact_path(root: str | Path, storage_key: str) -> Path:
    """Resolve one key under *root* without traversing links or junctions."""

    key = validate_storage_key(storage_key)
    root_input = Path(root).expanduser()
    if _is_link(root_input):
        raise ManifestContractError("artifact root cannot be a symlink or junction")
    root_path = root_input.resolve()
    if not root_path.is_dir():
        raise FileNotFoundError(f"artifact root does not exist: {root_path}")
    current = root_path
    for part in PurePosixPath(key).parts:
        current = current / part
        if current.exists() and _is_link(current):
            raise ManifestContractError(f"artifact path traverses a link: {key}")
    try:
        current.resolve(strict=False).relative_to(root_path)
    except ValueError as exc:
        raise ManifestContractError(f"artifact path escapes root: {key}") from exc
    return current


@dataclass(frozen=True, slots=True)
class RawDataArtifact:
    kind: DataKind
    format: DataFormat
    market: Market | None
    source: str
    source_dataset: str
    storage_key: str
    sha256: str
    byte_size: int
    row_count: int | None
    content_start: datetime | None
    content_end: datetime | None
    retrieved_at: datetime
    provider_version: str
    schema_version: str
    adapter_version: str
    known_at_policy: str
    revision_policy: str
    verified: bool
    source_note: str
    calendar_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DataKind):
            raise ManifestContractError("kind must be DataKind")
        if not isinstance(self.format, DataFormat):
            raise ManifestContractError("format must be DataFormat")
        if self.market is not None and not isinstance(self.market, Market):
            raise ManifestContractError("market must be Market or None")
        for name in (
            "source",
            "source_dataset",
            "provider_version",
            "schema_version",
            "adapter_version",
            "known_at_policy",
            "revision_policy",
        ):
            _expect_str(getattr(self, name), name)
        _expect_text(self.source_note, "source_note")
        _expect_bool(self.verified, "verified")
        _expect_int(self.byte_size, "byte_size")
        _expect_optional_int(self.row_count, "row_count")
        _expect_optional_str(self.calendar_snapshot_id, "calendar_snapshot_id")
        validate_storage_key(self.storage_key)
        sha256 = _expect_str(self.sha256, "sha256")
        if not _SHA256.fullmatch(sha256):
            raise ManifestContractError("sha256 must be 64 lowercase hexadecimal characters")
        if self.byte_size < 0:
            raise ManifestContractError("byte_size cannot be negative")
        if self.format in _STRUCTURED_FORMATS and self.row_count is None:
            raise ManifestContractError("structured artifacts require explicit row_count")
        if self.row_count is not None and self.row_count < 0:
            raise ManifestContractError("row_count cannot be negative")
        if self.kind in _MARKET_KINDS and self.market is None:
            raise ManifestContractError("market-related artifacts must declare market")
        required = (
            self.source,
            self.source_dataset,
            self.provider_version,
            self.schema_version,
            self.adapter_version,
            self.known_at_policy,
            self.revision_policy,
        )
        if any(not value for value in required):
            raise ManifestContractError("artifact provenance/version fields must be non-empty")
        ensure_aware(self.retrieved_at, "retrieved_at")
        if self.content_start is not None:
            ensure_aware(self.content_start, "content_start")
        if self.content_end is not None:
            ensure_aware(self.content_end, "content_end")
        if (self.content_start is None) != (self.content_end is None):
            raise ManifestContractError("content_start and content_end must be paired")
        if (
            self.content_start is not None
            and self.content_end is not None
            and to_utc(self.content_end) < to_utc(self.content_start)
        ):
            raise ManifestContractError("content_end cannot precede content_start")
        if self.verified and not self.source_note:
            raise ManifestContractError("verified artifacts require a source note")
        if self.calendar_snapshot_id is not None and not _SHA256.fullmatch(
            self.calendar_snapshot_id
        ):
            raise ManifestContractError("calendar_snapshot_id must be SHA-256")

    @property
    def artifact_id(self) -> str:
        return fingerprint(self)

    @classmethod
    def from_file(
        cls,
        root: str | Path,
        *,
        storage_key: str,
        kind: DataKind,
        format: DataFormat,
        market: Market | None,
        source: str,
        source_dataset: str,
        row_count: int | None,
        content_start: datetime | None,
        content_end: datetime | None,
        retrieved_at: datetime,
        provider_version: str,
        schema_version: str,
        adapter_version: str,
        known_at_policy: str,
        revision_policy: str,
        verified: bool,
        source_note: str,
        calendar_snapshot_id: str | None = None,
    ) -> "RawDataArtifact":
        path = safe_artifact_path(root, storage_key)
        if not path.is_file() or _is_link(path):
            raise FileNotFoundError(f"artifact is not a regular file: {storage_key}")
        return cls(
            kind=kind,
            format=format,
            market=market,
            source=source,
            source_dataset=source_dataset,
            storage_key=storage_key,
            sha256=hash_file(path),
            byte_size=path.stat().st_size,
            row_count=row_count,
            content_start=content_start,
            content_end=content_end,
            retrieved_at=retrieved_at,
            provider_version=provider_version,
            schema_version=schema_version,
            adapter_version=adapter_version,
            known_at_policy=known_at_policy,
            revision_policy=revision_policy,
            verified=verified,
            source_note=source_note,
            calendar_snapshot_id=calendar_snapshot_id,
        )

    def verify_file(self, root: str | Path) -> None:
        path = safe_artifact_path(root, self.storage_key)
        if not path.is_file() or _is_link(path):
            raise ManifestContractError(f"artifact disappeared: {self.storage_key}")
        if path.stat().st_size != self.byte_size:
            raise ManifestContractError(f"artifact size changed: {self.storage_key}")
        if hash_file(path) != self.sha256:
            raise ManifestContractError(f"artifact hash changed: {self.storage_key}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "format": self.format.value,
            "market": self.market.value if self.market is not None else None,
            "source": self.source,
            "source_dataset": self.source_dataset,
            "storage_key": self.storage_key,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "row_count": self.row_count,
            "content_start": self.content_start.isoformat() if self.content_start else None,
            "content_end": self.content_end.isoformat() if self.content_end else None,
            "retrieved_at": self.retrieved_at.isoformat(),
            "provider_version": self.provider_version,
            "schema_version": self.schema_version,
            "adapter_version": self.adapter_version,
            "known_at_policy": self.known_at_policy,
            "revision_policy": self.revision_policy,
            "verified": self.verified,
            "source_note": self.source_note,
            "calendar_snapshot_id": self.calendar_snapshot_id,
            "artifact_id": self.artifact_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RawDataArtifact":
        descriptor = _expect_dict(value, "artifact")
        expected_id_value = descriptor.get("artifact_id")
        expected_id = (
            _expect_str(expected_id_value, "artifact_id")
            if expected_id_value is not None
            else None
        )
        payload = dict(descriptor)
        payload.pop("artifact_id", None)
        allowed = {
            "kind",
            "format",
            "market",
            "source",
            "source_dataset",
            "storage_key",
            "sha256",
            "byte_size",
            "row_count",
            "content_start",
            "content_end",
            "retrieved_at",
            "provider_version",
            "schema_version",
            "adapter_version",
            "known_at_policy",
            "revision_policy",
            "verified",
            "source_note",
            "calendar_snapshot_id",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ManifestContractError(
                "artifact contains unknown fields: " + ", ".join(unknown)
            )
        try:
            kind = DataKind(_expect_str(payload.get("kind"), "kind"))
            data_format = DataFormat(_expect_str(payload.get("format"), "format"))
            market_value = payload.get("market")
            market = (
                Market(_expect_str(market_value, "market"))
                if market_value is not None
                else None
            )
        except ValueError as exc:
            raise ManifestContractError("artifact enum value is invalid") from exc
        artifact = cls(
            kind=kind,
            format=data_format,
            market=market,
            source=_expect_str(payload.get("source"), "source"),
            source_dataset=_expect_str(
                payload.get("source_dataset"),
                "source_dataset",
            ),
            storage_key=_expect_str(payload.get("storage_key"), "storage_key"),
            sha256=_expect_str(payload.get("sha256"), "sha256"),
            byte_size=_expect_int(payload.get("byte_size"), "byte_size"),
            row_count=_expect_optional_int(payload.get("row_count"), "row_count"),
            content_start=_parse_optional_datetime(
                payload.get("content_start"),
                "content_start",
            ),
            content_end=_parse_optional_datetime(
                payload.get("content_end"),
                "content_end",
            ),
            retrieved_at=_parse_datetime(payload.get("retrieved_at"), "retrieved_at"),
            provider_version=_expect_str(
                payload.get("provider_version"),
                "provider_version",
            ),
            schema_version=_expect_str(
                payload.get("schema_version"),
                "schema_version",
            ),
            adapter_version=_expect_str(
                payload.get("adapter_version"),
                "adapter_version",
            ),
            known_at_policy=_expect_str(
                payload.get("known_at_policy"),
                "known_at_policy",
            ),
            revision_policy=_expect_str(
                payload.get("revision_policy"),
                "revision_policy",
            ),
            verified=_expect_bool(payload.get("verified"), "verified"),
            source_note=_expect_text(payload.get("source_note"), "source_note"),
            calendar_snapshot_id=_expect_optional_str(
                payload.get("calendar_snapshot_id"),
                "calendar_snapshot_id",
            ),
        )
        if expected_id is not None:
            if not _SHA256.fullmatch(expected_id):
                raise ManifestContractError("artifact_id must be SHA-256")
            if artifact.artifact_id != expected_id:
                raise ManifestContractError("artifact_id does not match artifact content")
        return artifact


@dataclass(frozen=True, slots=True)
class DataSnapshotManifest:
    name: str
    as_of: datetime
    created_at: datetime
    config_hash: str
    code_version: str
    artifacts: tuple[RawDataArtifact, ...]
    calendar_snapshot_ids: tuple[str, ...]
    universe_snapshot_id: str | None
    require_verified: bool = True
    require_calendar_for_market_data: bool = True
    require_universe_for_market_data: bool = True
    notes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "require_verified",
            "require_calendar_for_market_data",
            "require_universe_for_market_data",
        ):
            _expect_bool(getattr(self, name), name)
        _expect_str(self.name, "name")
        _expect_str(self.code_version, "code_version")
        _expect_str(self.config_hash, "config_hash")
        _expect_optional_str(self.universe_snapshot_id, "universe_snapshot_id")
        _expect_dict(self.notes, "notes")
        if not isinstance(self.artifacts, tuple) or any(
            not isinstance(artifact, RawDataArtifact) for artifact in self.artifacts
        ):
            raise ManifestContractError("artifacts must be a tuple of RawDataArtifact")
        if not isinstance(self.calendar_snapshot_ids, tuple):
            raise ManifestContractError("calendar_snapshot_ids must be a tuple")
        for value in self.calendar_snapshot_ids:
            _expect_str(value, "calendar_snapshot_id")
        ensure_aware(self.as_of, "as_of")
        ensure_aware(self.created_at, "created_at")
        if to_utc(self.created_at) < to_utc(self.as_of):
            raise ManifestContractError("created_at cannot precede as_of")
        if not _SHA256.fullmatch(self.config_hash):
            raise ManifestContractError("config_hash must be SHA-256")
        if not self.artifacts:
            raise ManifestContractError("snapshot must contain at least one artifact")
        if any(not _SHA256.fullmatch(value) for value in self.calendar_snapshot_ids):
            raise ManifestContractError("calendar snapshot IDs must be SHA-256")
        if self.universe_snapshot_id is not None and not _SHA256.fullmatch(
            self.universe_snapshot_id
        ):
            raise ManifestContractError("universe_snapshot_id must be SHA-256")
        storage: dict[str, str] = {}
        logical: dict[tuple[Any, ...], str] = {}
        for artifact in self.artifacts:
            if to_utc(artifact.retrieved_at) > to_utc(self.created_at):
                raise ManifestContractError("artifact was retrieved after snapshot creation")
            if self.require_verified and not artifact.verified:
                raise ManifestContractError("unverified artifact entered verified snapshot")
            previous = storage.setdefault(artifact.storage_key, artifact.sha256)
            if previous != artifact.sha256:
                raise ManifestContractError("one storage key maps to multiple payloads")
            identity = (
                artifact.kind,
                artifact.market,
                artifact.source,
                artifact.source_dataset,
                artifact.content_start,
                artifact.content_end,
                artifact.retrieved_at,
                artifact.provider_version,
                artifact.schema_version,
                artifact.adapter_version,
                artifact.known_at_policy,
                artifact.revision_policy,
            )
            prior_payload = logical.setdefault(identity, artifact.sha256)
            if prior_payload != artifact.sha256:
                raise ManifestContractError(
                    "one provenance/revision identity maps to different bytes"
                )
        has_market = any(artifact.kind in _MARKET_KINDS for artifact in self.artifacts)
        has_security_set = any(
            artifact.kind in _SECURITY_SET_KINDS for artifact in self.artifacts
        )
        if (
            self.require_calendar_for_market_data
            and has_market
            and not self.calendar_snapshot_ids
        ):
            raise ManifestContractError("market data snapshot requires calendar binding")
        if (
            self.require_universe_for_market_data
            and has_security_set
            and self.universe_snapshot_id is None
        ):
            raise ManifestContractError("security-set data requires PIT universe binding")
        for artifact in self.artifacts:
            if artifact.kind is DataKind.EXCHANGE_CALENDAR:
                if artifact.calendar_snapshot_id is None:
                    raise ManifestContractError(
                        "calendar artifact must declare parsed calendar snapshot ID"
                    )
                if artifact.calendar_snapshot_id not in self.calendar_snapshot_ids:
                    raise ManifestContractError(
                        "calendar artifact is not bound by the snapshot"
                    )

    @property
    def snapshot_id(self) -> str:
        return fingerprint(
            {
                "schema": "raw-data-snapshot-v1",
                "name": self.name,
                "as_of": self.as_of,
                "created_at": self.created_at,
                "config_hash": self.config_hash,
                "code_version": self.code_version,
                "artifact_ids": sorted(
                    artifact.artifact_id for artifact in self.artifacts
                ),
                "calendar_snapshot_ids": sorted(self.calendar_snapshot_ids),
                "universe_snapshot_id": self.universe_snapshot_id,
                "require_verified": self.require_verified,
                "require_calendar_for_market_data": self.require_calendar_for_market_data,
                "require_universe_for_market_data": self.require_universe_for_market_data,
                "notes": self.notes,
            }
        )

    def verify_files(self, root: str | Path) -> None:
        for artifact in self.artifacts:
            artifact.verify_file(root)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "raw-data-snapshot-v1",
            "name": self.name,
            "as_of": self.as_of.isoformat(),
            "created_at": self.created_at.isoformat(),
            "config_hash": self.config_hash,
            "code_version": self.code_version,
            "artifacts": [
                artifact.as_dict()
                for artifact in sorted(self.artifacts, key=lambda item: item.artifact_id)
            ],
            "calendar_snapshot_ids": sorted(self.calendar_snapshot_ids),
            "universe_snapshot_id": self.universe_snapshot_id,
            "require_verified": self.require_verified,
            "require_calendar_for_market_data": self.require_calendar_for_market_data,
            "require_universe_for_market_data": self.require_universe_for_market_data,
            "notes": self.notes,
            "snapshot_id": self.snapshot_id,
        }

    def write_json(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.tmp-",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(self.as_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    @classmethod
    def read_json(cls, path: str | Path) -> "DataSnapshotManifest":
        with Path(path).open("r", encoding="utf-8") as handle:
            value = _expect_dict(json.load(handle), "manifest")
        allowed = {
            "schema",
            "name",
            "as_of",
            "created_at",
            "config_hash",
            "code_version",
            "artifacts",
            "calendar_snapshot_ids",
            "universe_snapshot_id",
            "require_verified",
            "require_calendar_for_market_data",
            "require_universe_for_market_data",
            "notes",
            "snapshot_id",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ManifestContractError(
                "manifest contains unknown fields: " + ", ".join(unknown)
            )
        if value.get("schema") != "raw-data-snapshot-v1":
            raise ManifestContractError("unsupported manifest schema")
        expected_id = _expect_str(value.get("snapshot_id"), "snapshot_id")
        if not _SHA256.fullmatch(expected_id):
            raise ManifestContractError("snapshot_id must be SHA-256")
        artifact_values = _expect_list(value.get("artifacts"), "artifacts")
        manifest = cls(
            name=_expect_str(value.get("name"), "name"),
            as_of=_parse_datetime(value.get("as_of"), "as_of"),
            created_at=_parse_datetime(value.get("created_at"), "created_at"),
            config_hash=_expect_str(value.get("config_hash"), "config_hash"),
            code_version=_expect_str(value.get("code_version"), "code_version"),
            artifacts=tuple(
                RawDataArtifact.from_dict(_expect_dict(artifact, "artifact"))
                for artifact in artifact_values
            ),
            calendar_snapshot_ids=_expect_str_sequence(
                value.get("calendar_snapshot_ids"),
                "calendar_snapshot_ids",
            ),
            universe_snapshot_id=_expect_optional_str(
                value.get("universe_snapshot_id"),
                "universe_snapshot_id",
            ),
            require_verified=_expect_bool(
                value.get("require_verified"),
                "require_verified",
            ),
            require_calendar_for_market_data=_expect_bool(
                value.get("require_calendar_for_market_data"),
                "require_calendar_for_market_data",
            ),
            require_universe_for_market_data=_expect_bool(
                value.get("require_universe_for_market_data"),
                "require_universe_for_market_data",
            ),
            notes=_expect_dict(value.get("notes", {}), "notes"),
        )
        if manifest.snapshot_id != expected_id:
            raise ManifestContractError("snapshot_id does not match manifest content")
        return manifest
