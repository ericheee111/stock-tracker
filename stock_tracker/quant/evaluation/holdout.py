"""Irreversible frozen-holdout state management."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..core.fingerprint import fingerprint
from ..core.time import ensure_aware, to_utc, utc_now

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HoldoutContractError(RuntimeError):
    """Raised when a sealed holdout is viewed under a different identity."""


class HoldoutState(StrEnum):
    SEALED = "SEALED"
    EXPOSED = "EXPOSED"
    COMPROMISED = "COMPROMISED"


@dataclass(frozen=True, slots=True)
class FrozenHoldoutRecord:
    holdout_id: str
    config_hash: str
    data_snapshot_id: str
    sealed_at: datetime
    state: HoldoutState = HoldoutState.SEALED
    first_exposed_at: datetime | None = None
    compromised_at: datetime | None = None
    compromise_reason: str | None = None
    exposure_count: int = 0

    def __post_init__(self) -> None:
        if not self.holdout_id:
            raise HoldoutContractError("holdout_id must be non-empty")
        if not _SHA256.fullmatch(self.config_hash):
            raise HoldoutContractError("config_hash must be SHA-256")
        if not _SHA256.fullmatch(self.data_snapshot_id):
            raise HoldoutContractError("data_snapshot_id must be SHA-256")
        ensure_aware(self.sealed_at, "sealed_at")
        if self.first_exposed_at is not None:
            ensure_aware(self.first_exposed_at, "first_exposed_at")
        if self.compromised_at is not None:
            ensure_aware(self.compromised_at, "compromised_at")
        if self.exposure_count < 0:
            raise HoldoutContractError("exposure_count cannot be negative")
        if self.state is HoldoutState.SEALED:
            if self.first_exposed_at is not None or self.exposure_count != 0:
                raise HoldoutContractError("sealed holdout cannot have exposure history")
        elif self.state is HoldoutState.EXPOSED:
            if self.first_exposed_at is None or self.exposure_count <= 0:
                raise HoldoutContractError("exposed holdout requires exposure history")
        elif self.state is HoldoutState.COMPROMISED and (
            self.compromised_at is None or not self.compromise_reason
        ):
            raise HoldoutContractError("compromised holdout requires reason and time")

    @property
    def record_hash(self) -> str:
        return fingerprint(self)

    def expose(
        self,
        *,
        config_hash: str,
        data_snapshot_id: str,
        exposed_at: datetime | None = None,
    ) -> FrozenHoldoutRecord:
        timestamp = to_utc(exposed_at or utc_now(), "exposed_at")
        if self.state is HoldoutState.COMPROMISED:
            raise HoldoutContractError(
                f"holdout is permanently compromised: {self.compromise_reason}"
            )
        mismatches: list[str] = []
        if config_hash != self.config_hash:
            mismatches.append("CONFIG_HASH_MISMATCH")
        if data_snapshot_id != self.data_snapshot_id:
            mismatches.append("DATA_SNAPSHOT_MISMATCH")
        if mismatches:
            return replace(
                self,
                state=HoldoutState.COMPROMISED,
                compromised_at=timestamp,
                compromise_reason=";".join(mismatches),
            )
        return replace(
            self,
            state=HoldoutState.EXPOSED,
            first_exposed_at=self.first_exposed_at or timestamp,
            exposure_count=self.exposure_count + 1,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "frozen-holdout-v1",
            "holdout_id": self.holdout_id,
            "config_hash": self.config_hash,
            "data_snapshot_id": self.data_snapshot_id,
            "sealed_at": self.sealed_at.isoformat(),
            "state": self.state.value,
            "first_exposed_at": (
                self.first_exposed_at.isoformat() if self.first_exposed_at else None
            ),
            "compromised_at": (
                self.compromised_at.isoformat() if self.compromised_at else None
            ),
            "compromise_reason": self.compromise_reason,
            "exposure_count": self.exposure_count,
            "record_hash": self.record_hash,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FrozenHoldoutRecord:
        if value.get("schema") != "frozen-holdout-v1":
            raise HoldoutContractError("unsupported holdout schema")
        expected_hash = value.get("record_hash")
        record = cls(
            holdout_id=value["holdout_id"],
            config_hash=value["config_hash"],
            data_snapshot_id=value["data_snapshot_id"],
            sealed_at=datetime.fromisoformat(value["sealed_at"]),
            state=HoldoutState(value["state"]),
            first_exposed_at=(
                datetime.fromisoformat(value["first_exposed_at"])
                if value.get("first_exposed_at")
                else None
            ),
            compromised_at=(
                datetime.fromisoformat(value["compromised_at"])
                if value.get("compromised_at")
                else None
            ),
            compromise_reason=value.get("compromise_reason"),
            exposure_count=int(value["exposure_count"]),
        )
        if expected_hash != record.record_hash:
            raise HoldoutContractError("record_hash does not match holdout content")
        return record


class FrozenHoldout:
    """Persist state before returning exposure success or mismatch failure."""

    def __init__(self, path: str | Path, record: FrozenHoldoutRecord) -> None:
        self.path = Path(path)
        self.record = record

    @classmethod
    def seal(
        cls,
        path: str | Path,
        *,
        holdout_id: str,
        config_hash: str,
        data_snapshot_id: str,
        sealed_at: datetime | None = None,
    ) -> FrozenHoldout:
        destination = Path(path)
        if destination.exists():
            raise HoldoutContractError("holdout file already exists and cannot be resealed")
        instance = cls(
            destination,
            FrozenHoldoutRecord(
                holdout_id=holdout_id,
                config_hash=config_hash,
                data_snapshot_id=data_snapshot_id,
                sealed_at=to_utc(sealed_at or utc_now()),
            ),
        )
        instance._write()
        return instance

    @classmethod
    def load(cls, path: str | Path) -> FrozenHoldout:
        source = Path(path)
        with source.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return cls(source, FrozenHoldoutRecord.from_dict(value))

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.tmp-",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(self.record.as_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def expose(
        self,
        *,
        config_hash: str,
        data_snapshot_id: str,
        exposed_at: datetime | None = None,
    ) -> FrozenHoldoutRecord:
        updated = self.record.expose(
            config_hash=config_hash,
            data_snapshot_id=data_snapshot_id,
            exposed_at=exposed_at,
        )
        self.record = updated
        self._write()
        if updated.state is HoldoutState.COMPROMISED:
            raise HoldoutContractError(
                f"holdout permanently compromised: {updated.compromise_reason}"
            )
        return updated
