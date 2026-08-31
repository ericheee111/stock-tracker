from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from stock_tracker.storage.repository import (
    Repository,
    RuntimeOutboxError,
    RuntimeOutboxLease,
)
from stock_tracker.storage.runtime_migrations import RuntimeMigrationError

from .contracts import (
    RuntimeDecisionArtifact,
    RuntimeEvidenceContractError,
    SystemUtcClock,
    UtcClock,
    require_utc_clock_value,
)
from .store import RuntimeArtifactStore, RuntimeArtifactStoreError


class RuntimeArtifactWorkerStatus(StrEnum):
    IDLE = "IDLE"
    DELIVERED = "DELIVERED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class RuntimeArtifactWorkerResult:
    status: RuntimeArtifactWorkerStatus
    artifact_id: str | None
    cursor: int | None
    retry_count: int | None
    quarantine_id: str | None


class RuntimeArtifactWorker:
    def __init__(
        self,
        repository: Repository,
        artifact_store: RuntimeArtifactStore,
        *,
        worker_id: str = "stage4g1-runtime-artifact",
        clock: UtcClock | None = None,
        lease_seconds: int = 60,
        retry_delay_seconds: int = 30,
    ) -> None:
        if type(repository) is not Repository:
            raise TypeError("repository must be Repository")
        if type(artifact_store) is not RuntimeArtifactStore:
            raise TypeError("artifact_store must be RuntimeArtifactStore")
        if type(worker_id) is not str or not worker_id.strip() or worker_id != worker_id.strip():
            raise ValueError("worker_id must be a trimmed non-empty string")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be in [1, 3600]")
        if (
            type(retry_delay_seconds) is not int
            or not 1 <= retry_delay_seconds <= 86400
        ):
            raise ValueError("retry_delay_seconds must be in [1, 86400]")
        self.repository = repository
        self.artifact_store = artifact_store
        self.worker_id = worker_id
        self.clock = clock or SystemUtcClock()
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds

    @staticmethod
    def _artifact_from_lease(lease: RuntimeOutboxLease) -> RuntimeDecisionArtifact:
        raw = lease.payload_json.encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != lease.payload_sha256:
            raise RuntimeEvidenceContractError("outbox payload SHA mismatch")
        artifact = RuntimeDecisionArtifact.from_json_bytes(raw)
        identity = artifact.identity_dict()
        if (
            artifact.artifact_id != lease.artifact_id
            or identity["runtime_signal_id"] != lease.runtime_signal_id
            or identity["transition_event_id"] != lease.transition_event_id
        ):
            raise RuntimeEvidenceContractError("outbox envelope identity mismatch")
        return artifact

    def _quarantine_poison(
        self, lease: RuntimeOutboxLease, observed_at: datetime
    ) -> RuntimeArtifactWorkerResult:
        quarantine_id, cursor = self.repository.mark_runtime_outbox_quarantined(
            lease,
            reason_code="POISON_RUNTIME_ARTIFACT",
            quarantined_at=observed_at,
            metadata={"stage": "OUTBOX_DECODE"},
        )
        return RuntimeArtifactWorkerResult(
            RuntimeArtifactWorkerStatus.QUARANTINED,
            lease.artifact_id,
            cursor,
            lease.retry_count,
            quarantine_id,
        )

    def run_once(self) -> RuntimeArtifactWorkerResult:
        claimed_at = require_utc_clock_value(self.clock.now(), "claimed_at")
        lease = self.repository.claim_runtime_outbox(
            worker_id=self.worker_id,
            now=claimed_at,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            return RuntimeArtifactWorkerResult(
                RuntimeArtifactWorkerStatus.IDLE, None, None, None, None
            )
        try:
            artifact = self._artifact_from_lease(lease)
        except (RuntimeEvidenceContractError, UnicodeEncodeError):
            return self._quarantine_poison(
                lease, require_utc_clock_value(self.clock.now(), "quarantined_at")
            )
        try:
            append_result = self.artifact_store.append(artifact)
            audit_report = self.artifact_store.audit()
            delivered_at = require_utc_clock_value(self.clock.now(), "delivered_at")
            cursor = self.repository.mark_runtime_outbox_delivered(
                lease,
                append_result=append_result,
                audit_report=audit_report,
                delivered_at=delivered_at,
            )
            return RuntimeArtifactWorkerResult(
                RuntimeArtifactWorkerStatus.DELIVERED,
                lease.artifact_id,
                cursor,
                lease.retry_count,
                None,
            )
        except RuntimeArtifactStoreError:
            observed_at = require_utc_clock_value(self.clock.now(), "retry_observed_at")
            self.repository.mark_runtime_outbox_retry(
                lease,
                error_code="ARTIFACT_STORE_UNAVAILABLE",
                retry_at=observed_at + timedelta(seconds=self.retry_delay_seconds),
                observed_at=observed_at,
            )
            return RuntimeArtifactWorkerResult(
                RuntimeArtifactWorkerStatus.RETRY_SCHEDULED,
                lease.artifact_id,
                None,
                lease.retry_count + 1,
                None,
            )


class RuntimeArtifactWorkerService:
    def __init__(
        self,
        worker: RuntimeArtifactWorker,
        logger: logging.Logger,
        *,
        idle_wait_seconds: float = 1.0,
    ) -> None:
        if type(worker) is not RuntimeArtifactWorker:
            raise TypeError("worker must be RuntimeArtifactWorker")
        if type(idle_wait_seconds) not in (int, float) or not 0.05 <= idle_wait_seconds <= 60:
            raise ValueError("idle_wait_seconds must be in [0.05, 60]")
        self.worker = worker
        self.logger = logger
        self.idle_wait_seconds = float(idle_wait_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.worker.run_once()
            except (
                OSError,
                RuntimeArtifactStoreError,
                RuntimeEvidenceContractError,
                RuntimeMigrationError,
                RuntimeOutboxError,
                sqlite3.Error,
            ):
                self.logger.exception("Runtime Artifact Worker 失败，Outcome lane 保持失败关闭")
                self._stop.wait(self.idle_wait_seconds)
                continue
            if result.status in {
                RuntimeArtifactWorkerStatus.IDLE,
                RuntimeArtifactWorkerStatus.RETRY_SCHEDULED,
            }:
                self._stop.wait(self.idle_wait_seconds)

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="stage4g1-runtime-artifact",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self.idle_wait_seconds * 2))
        self._thread = None


__all__ = [
    "RuntimeArtifactWorker",
    "RuntimeArtifactWorkerResult",
    "RuntimeArtifactWorkerService",
    "RuntimeArtifactWorkerStatus",
]
