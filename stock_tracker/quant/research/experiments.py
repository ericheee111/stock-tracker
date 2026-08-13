"""Append-only experiment ledger with reproducibility evidence."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..core.fingerprint import fingerprint
from ..core.reproducibility import ReproducibilityRecord
from ..core.time import to_utc, utc_now

_LOCK = threading.Lock()


class ExperimentContractError(RuntimeError):
    """Raised when an experiment event is tampered or invalid."""


class ExperimentEventType(StrEnum):
    CREATED = "CREATED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class ExperimentEvent:
    experiment_id: str
    event_type: ExperimentEventType
    occurred_at: datetime
    reproducibility_id: str
    comparison_id: str | None
    metrics: dict[str, float] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.experiment_id:
            raise ExperimentContractError("experiment_id must be non-empty")
        to_utc(self.occurred_at, "occurred_at")
        if len(self.reproducibility_id) != 64:
            raise ExperimentContractError("reproducibility_id must be SHA-256")
        if self.comparison_id is not None and len(self.comparison_id) != 64:
            raise ExperimentContractError("comparison_id must be SHA-256")

    @property
    def event_id(self) -> str:
        return fingerprint({"schema": "experiment-event-v1", "event": self})

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "experiment-event-v1",
            "experiment_id": self.experiment_id,
            "event_type": self.event_type.value,
            "occurred_at": self.occurred_at.isoformat(),
            "reproducibility_id": self.reproducibility_id,
            "comparison_id": self.comparison_id,
            "metrics": self.metrics,
            "artifacts": self.artifacts,
            "notes": self.notes,
            "event_id": self.event_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExperimentEvent:
        if value.get("schema") != "experiment-event-v1":
            raise ExperimentContractError("unsupported experiment event schema")
        expected_id = value.get("event_id")
        event = cls(
            experiment_id=value["experiment_id"],
            event_type=ExperimentEventType(value["event_type"]),
            occurred_at=datetime.fromisoformat(value["occurred_at"]),
            reproducibility_id=value["reproducibility_id"],
            comparison_id=value.get("comparison_id"),
            metrics={key: float(item) for key, item in value.get("metrics", {}).items()},
            artifacts=dict(value.get("artifacts", {})),
            notes=dict(value.get("notes", {})),
        )
        if expected_id != event.event_id:
            raise ExperimentContractError("experiment event hash mismatch")
        return event


class ExperimentLedger:
    """Append-only JSONL evidence; past experiments are never rewritten."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def events(self) -> tuple[ExperimentEvent, ...]:
        if not self.path.exists():
            return ()
        result: list[ExperimentEvent] = []
        seen: set[str] = set()
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    event = ExperimentEvent.from_dict(json.loads(line))
                except Exception as exc:
                    raise ExperimentContractError(
                        f"invalid experiment ledger line {line_number}"
                    ) from exc
                if event.event_id in seen:
                    raise ExperimentContractError("duplicate experiment event")
                seen.add(event.event_id)
                result.append(event)
        return tuple(result)

    def append(self, event: ExperimentEvent) -> None:
        with _LOCK:
            existing = self.events()
            if event.event_id in {item.event_id for item in existing}:
                raise ExperimentContractError("experiment event already exists")
            history = [
                item for item in existing if item.experiment_id == event.experiment_id
            ]
            if history and history[-1].event_type in {
                ExperimentEventType.COMPLETED,
                ExperimentEventType.FAILED,
                ExperimentEventType.REJECTED,
            }:
                raise ExperimentContractError("terminal experiment cannot receive events")
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


def experiment_event(
    *,
    experiment_id: str,
    event_type: ExperimentEventType,
    reproducibility: ReproducibilityRecord,
    comparison_id: str | None = None,
    metrics: dict[str, float] | None = None,
    artifacts: dict[str, str] | None = None,
    notes: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> ExperimentEvent:
    return ExperimentEvent(
        experiment_id=experiment_id,
        event_type=event_type,
        occurred_at=to_utc(occurred_at or utc_now()),
        reproducibility_id=reproducibility.record_id,
        comparison_id=comparison_id,
        metrics=dict(metrics or {}),
        artifacts=dict(artifacts or {}),
        notes=dict(notes or {}),
    )
