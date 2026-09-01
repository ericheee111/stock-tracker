from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from stock_tracker.storage.repository import (
    Repository,
    RuntimeDatabaseIntegrityError,
    RuntimeOutboxError,
    RuntimeOutboxLease,
    RuntimeWorkerIntegrityBlocked,
)
from stock_tracker.storage.runtime_migrations import RuntimeMigrationError

from .contracts import (
    RuntimeDecisionArtifact,
    RuntimeEvidenceContractError,
    SystemUtcClock,
    UtcClock,
    require_utc_clock_value,
)
from .store import (
    RuntimeArtifactFailureClass,
    RuntimeArtifactStore,
    RuntimeArtifactStoreError,
)


class RuntimeArtifactWorkerStatus(StrEnum):
    IDLE = "IDLE"
    DELIVERED = "DELIVERED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    QUARANTINED = "QUARANTINED"
    HARD_BLOCKED = "HARD_BLOCKED"


@dataclass(frozen=True, slots=True)
class RuntimeArtifactWorkerResult:
    status: RuntimeArtifactWorkerStatus
    artifact_id: str | None = None
    cursor: int | None = None
    retry_count: int | None = None
    quarantine_id: str | None = None
    failure_class: RuntimeArtifactFailureClass | None = None
    error_code: str | None = None
    retry_policy_id: str | None = None
    block_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeArtifactRetryPolicy:
    base_delay_seconds: int
    max_delay_seconds: int
    max_attempts: int
    version: str = "stage4g1-runtime-artifact-retry-v1"
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.base_delay_seconds) is not int or not 1 <= self.base_delay_seconds <= 86400:
            raise ValueError("base_delay_seconds must be in [1, 86400]")
        if type(self.max_delay_seconds) is not int or not self.base_delay_seconds <= self.max_delay_seconds <= 604800:
            raise ValueError("max_delay_seconds must be in [base_delay_seconds, 604800]")
        if type(self.max_attempts) is not int or not 0 <= self.max_attempts <= 100:
            raise ValueError("max_attempts must be in [0, 100]")
        document = {
            "schema": self.version,
            "base_delay_seconds": self.base_delay_seconds,
            "max_delay_seconds": self.max_delay_seconds,
            "max_attempts": self.max_attempts,
        }
        raw = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object.__setattr__(self, "policy_id", hashlib.sha256(raw).hexdigest())

    def delay_seconds(self, retry_count: int) -> int:
        if type(retry_count) is not int or retry_count < 1:
            raise ValueError("retry_count must be a positive integer")
        return min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** (retry_count - 1)),
        )


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
        max_retry_delay_seconds: int = 3600,
        max_store_retries: int = 3,
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
        self.retry_policy = RuntimeArtifactRetryPolicy(
            base_delay_seconds=retry_delay_seconds,
            max_delay_seconds=max_retry_delay_seconds,
            max_attempts=max_store_retries,
        )
        self._local_integrity_block_code: str | None = None

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
            or identity["decision_content_id"] != lease.decision_content_id
            or identity["occurrence_dedup_id"] != lease.occurrence_dedup_id
        ):
            raise RuntimeEvidenceContractError("outbox envelope identity mismatch")
        return artifact

    def _quarantine_row(
        self,
        lease: RuntimeOutboxLease,
        observed_at: datetime,
        *,
        reason_code: str,
        error_code: str,
        retry_count: int,
        failure_class: RuntimeArtifactFailureClass,
    ) -> RuntimeArtifactWorkerResult:
        quarantine_id, cursor = self.repository.mark_runtime_outbox_quarantined(
            lease,
            reason_code=reason_code,
            quarantined_at=observed_at,
            metadata={
                "failure_class": failure_class.value,
                "error_code": error_code,
                "retry_count": retry_count,
                "retry_policy_id": self.retry_policy.policy_id,
            },
        )
        return RuntimeArtifactWorkerResult(
            status=RuntimeArtifactWorkerStatus.QUARANTINED,
            artifact_id=lease.artifact_id,
            cursor=cursor,
            retry_count=retry_count,
            quarantine_id=quarantine_id,
            failure_class=failure_class,
            error_code=error_code,
            retry_policy_id=self.retry_policy.policy_id,
        )

    def _hard_block_result(
        self,
        *,
        artifact_id: str | None,
        error_code: str,
        retry_count: int | None = None,
        block_id: str | None = None,
    ) -> RuntimeArtifactWorkerResult:
        self._local_integrity_block_code = error_code
        return RuntimeArtifactWorkerResult(
            status=RuntimeArtifactWorkerStatus.HARD_BLOCKED,
            artifact_id=artifact_id,
            retry_count=retry_count,
            failure_class=RuntimeArtifactFailureClass.STORE_INTEGRITY_BLOCK,
            error_code=error_code,
            retry_policy_id=self.retry_policy.policy_id,
            block_id=block_id,
        )

    def _store_failure_result(
        self,
        lease: RuntimeOutboxLease,
        exc: RuntimeArtifactStoreError | OSError | sqlite3.Error,
    ) -> RuntimeArtifactWorkerResult:
        observed_at = require_utc_clock_value(
            self.clock.now(),
            "store_failure_observed_at",
        )
        if type(exc) is RuntimeArtifactStoreError:
            error_code = exc.code
            failure_class = exc.failure_class
        else:
            error_code = "ARTIFACT_STORE_IO_FAILURE"
            failure_class = RuntimeArtifactFailureClass.ROW_TRANSIENT
        next_retry_count = lease.retry_count + 1
        if failure_class is RuntimeArtifactFailureClass.ROW_PERMANENT:
            return self._quarantine_row(
                lease,
                observed_at,
                reason_code="ROW_PERMANENT",
                error_code=error_code,
                retry_count=next_retry_count,
                failure_class=failure_class,
            )
        if failure_class is RuntimeArtifactFailureClass.ROW_TRANSIENT and next_retry_count <= self.retry_policy.max_attempts:
            self.repository.mark_runtime_outbox_retry(
                lease,
                error_code=error_code,
                retry_at=observed_at
                + timedelta(seconds=self.retry_policy.delay_seconds(next_retry_count)),
                observed_at=observed_at,
            )
            return RuntimeArtifactWorkerResult(
                status=RuntimeArtifactWorkerStatus.RETRY_SCHEDULED,
                artifact_id=lease.artifact_id,
                retry_count=next_retry_count,
                failure_class=failure_class,
                error_code=error_code,
                retry_policy_id=self.retry_policy.policy_id,
            )
        if failure_class is RuntimeArtifactFailureClass.ROW_TRANSIENT:
            return self._quarantine_row(
                lease,
                observed_at,
                reason_code="TRANSIENT_RETRY_EXHAUSTED",
                error_code=error_code,
                retry_count=next_retry_count,
                failure_class=failure_class,
            )
        block_id = self.repository.mark_runtime_worker_integrity_blocked(
            lease,
            error_code=error_code,
            blocked_at=observed_at,
        )
        return self._hard_block_result(
            artifact_id=lease.artifact_id,
            error_code=error_code,
            retry_count=next_retry_count,
            block_id=block_id,
        )

    def run_once(self) -> RuntimeArtifactWorkerResult:
        if self._local_integrity_block_code is not None:
            return self._hard_block_result(
                artifact_id=None,
                error_code=self._local_integrity_block_code,
            )
        claimed_at = require_utc_clock_value(self.clock.now(), "claimed_at")
        try:
            lease = self.repository.claim_runtime_outbox(
                worker_id=self.worker_id,
                now=claimed_at,
                lease_seconds=self.lease_seconds,
            )
        except RuntimeWorkerIntegrityBlocked as exc:
            return self._hard_block_result(
                artifact_id=exc.artifact_id,
                error_code=exc.block_code,
            )
        except (RuntimeMigrationError, RuntimeDatabaseIntegrityError, sqlite3.Error) as exc:
            return self._hard_block_result(
                artifact_id=None,
                error_code=getattr(exc, "code", "RUNTIME_DATABASE_INTEGRITY"),
            )
        if lease is None:
            return RuntimeArtifactWorkerResult(status=RuntimeArtifactWorkerStatus.IDLE)
        try:
            artifact = self._artifact_from_lease(lease)
        except (RuntimeEvidenceContractError, UnicodeEncodeError):
            return self._quarantine_row(
                lease,
                require_utc_clock_value(self.clock.now(), "quarantined_at"),
                reason_code="ROW_PERMANENT",
                error_code="OUTBOX_ARTIFACT_CONTRACT_INVALID",
                retry_count=lease.retry_count,
                failure_class=RuntimeArtifactFailureClass.ROW_PERMANENT,
            )
        try:
            append_result = self.artifact_store.append(artifact)
            audit_report = self.artifact_store.audit()
        except (RuntimeArtifactStoreError, OSError, sqlite3.Error) as exc:
            return self._store_failure_result(lease, exc)
        delivered_at = require_utc_clock_value(self.clock.now(), "delivered_at")
        try:
            cursor = self.repository.mark_runtime_outbox_delivered(
                lease,
                append_result=append_result,
                audit_report=audit_report,
                delivered_at=delivered_at,
            )
        except (RuntimeMigrationError, RuntimeDatabaseIntegrityError, sqlite3.Error) as exc:
            return self._hard_block_result(
                artifact_id=lease.artifact_id,
                error_code=getattr(exc, "code", "RUNTIME_DATABASE_INTEGRITY"),
                retry_count=lease.retry_count,
            )
        return RuntimeArtifactWorkerResult(
            status=RuntimeArtifactWorkerStatus.DELIVERED,
            artifact_id=lease.artifact_id,
            cursor=cursor,
            retry_count=lease.retry_count,
            retry_policy_id=self.retry_policy.policy_id,
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
            elif result.status is RuntimeArtifactWorkerStatus.HARD_BLOCKED:
                return

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
    "RuntimeArtifactRetryPolicy",
    "RuntimeArtifactWorker",
    "RuntimeArtifactWorkerResult",
    "RuntimeArtifactWorkerService",
    "RuntimeArtifactWorkerStatus",
]
