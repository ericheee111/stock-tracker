"""Append-only model registry and promotion event stream."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from stock_tracker.core.types import Market

from ..core.fingerprint import fingerprint
from ..core.time import to_utc, utc_now

_LOCK = threading.Lock()


class RegistryContractError(RuntimeError):
    """Raised when a registry event is duplicated, tampered or invalid."""


class RegistryEventType(StrEnum):
    REGISTER = "REGISTER"
    PROMOTE = "PROMOTE"
    REJECT = "REJECT"
    RETIRE = "RETIRE"


@dataclass(frozen=True, slots=True)
class RegistryEvent:
    event_type: RegistryEventType
    model_id: str
    strategy_id: str
    market: Market
    horizon_sessions: int
    occurred_at: datetime
    data_snapshot_ids: tuple[str, ...]
    feature_set_id: str
    label_version: str
    comparison_id: str | None
    evidence_id: str
    predecessor_model_id: str | None = None
    artifact_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.model_id or not self.strategy_id or not self.label_version:
            raise RegistryContractError("model/strategy/label identities are required")
        if self.horizon_sessions <= 0:
            raise RegistryContractError("horizon_sessions must be positive")
        to_utc(self.occurred_at, "occurred_at")
        hashes = (*self.data_snapshot_ids, self.feature_set_id, self.evidence_id)
        if any(len(value) != 64 for value in hashes):
            raise RegistryContractError("snapshot/feature/evidence IDs must be SHA-256")
        if self.comparison_id is not None and len(self.comparison_id) != 64:
            raise RegistryContractError("comparison_id must be SHA-256")

    @property
    def event_id(self) -> str:
        return fingerprint({"schema": "model-registry-event-v1", "event": self})

    @property
    def stream_key(self) -> tuple[str, Market, int]:
        return (self.strategy_id, self.market, self.horizon_sessions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "model-registry-event-v1",
            "event_type": self.event_type.value,
            "model_id": self.model_id,
            "strategy_id": self.strategy_id,
            "market": self.market.value,
            "horizon_sessions": self.horizon_sessions,
            "occurred_at": self.occurred_at.isoformat(),
            "data_snapshot_ids": list(self.data_snapshot_ids),
            "feature_set_id": self.feature_set_id,
            "label_version": self.label_version,
            "comparison_id": self.comparison_id,
            "evidence_id": self.evidence_id,
            "predecessor_model_id": self.predecessor_model_id,
            "artifact_key": self.artifact_key,
            "metadata": self.metadata,
            "event_id": self.event_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RegistryEvent:
        if value.get("schema") != "model-registry-event-v1":
            raise RegistryContractError("unsupported registry event schema")
        expected_id = value.get("event_id")
        horizon_sessions = value.get("horizon_sessions")
        if isinstance(horizon_sessions, bool) or not isinstance(
            horizon_sessions,
            int,
        ):
            raise RegistryContractError("horizon_sessions must be an integer")
        event = cls(
            event_type=RegistryEventType(value["event_type"]),
            model_id=value["model_id"],
            strategy_id=value["strategy_id"],
            market=Market(value["market"]),
            horizon_sessions=horizon_sessions,
            occurred_at=datetime.fromisoformat(value["occurred_at"]),
            data_snapshot_ids=tuple(value["data_snapshot_ids"]),
            feature_set_id=value["feature_set_id"],
            label_version=value["label_version"],
            comparison_id=value.get("comparison_id"),
            evidence_id=value["evidence_id"],
            predecessor_model_id=value.get("predecessor_model_id"),
            artifact_key=value.get("artifact_key"),
            metadata=dict(value.get("metadata", {})),
        )
        if expected_id != event.event_id:
            raise RegistryContractError("registry event hash mismatch")
        return event


class ModelRegistry:
    """JSONL registry whose existing lines are never updated or deleted."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def events(self) -> tuple[RegistryEvent, ...]:
        if not self.path.exists():
            return ()
        events: list[RegistryEvent] = []
        seen: set[str] = set()
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    event = RegistryEvent.from_dict(json.loads(line))
                except Exception as exc:
                    raise RegistryContractError(
                        f"invalid registry line {line_number}"
                    ) from exc
                if event.event_id in seen:
                    raise RegistryContractError("duplicate registry event")
                seen.add(event.event_id)
                events.append(event)
        return tuple(events)

    def append(self, event: RegistryEvent) -> None:
        with _LOCK:
            existing = self.events()
            if event.event_id in {item.event_id for item in existing}:
                raise RegistryContractError("event already exists")
            if event.event_type is RegistryEventType.PROMOTE:
                current = self.current_champion(event.stream_key, existing=existing)
                if current is not None and event.predecessor_model_id != current.model_id:
                    raise RegistryContractError(
                        "promotion predecessor differs from current champion"
                    )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                event.as_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            with self.path.open("ab") as handle:
                handle.write(payload.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())

    def current_champion(
        self,
        stream_key: tuple[str, Market, int],
        *,
        existing: tuple[RegistryEvent, ...] | None = None,
    ) -> RegistryEvent | None:
        champion: RegistryEvent | None = None
        for event in existing if existing is not None else self.events():
            if event.stream_key != stream_key:
                continue
            if event.event_type is RegistryEventType.PROMOTE:
                champion = event
            elif (
                event.event_type is RegistryEventType.RETIRE
                and champion is not None
                and event.model_id == champion.model_id
            ):
                champion = None
        return champion


def registry_event(
    *,
    event_type: RegistryEventType,
    model_id: str,
    strategy_id: str,
    market: Market,
    horizon_sessions: int,
    data_snapshot_ids: tuple[str, ...],
    feature_set_id: str,
    label_version: str,
    evidence_id: str,
    comparison_id: str | None = None,
    predecessor_model_id: str | None = None,
    artifact_key: str | None = None,
    metadata: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> RegistryEvent:
    return RegistryEvent(
        event_type=event_type,
        model_id=model_id,
        strategy_id=strategy_id,
        market=market,
        horizon_sessions=horizon_sessions,
        occurred_at=to_utc(occurred_at or utc_now()),
        data_snapshot_ids=tuple(sorted(data_snapshot_ids)),
        feature_set_id=feature_set_id,
        label_version=label_version,
        comparison_id=comparison_id,
        evidence_id=evidence_id,
        predecessor_model_id=predecessor_model_id,
        artifact_key=artifact_key,
        metadata=dict(metadata or {}),
    )
