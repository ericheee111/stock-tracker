"""Governance contracts for the optional free-stockdb local sidecar.

The runtime adapter only proves that a pinned localhost process returned bytes.
This module governs release sandbox evidence, multi-symbol reconciliation, and
an explicitly non-production shadow lane.  It never promotes data to T3,
enables model training, or changes live trading decisions.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath

from stock_tracker.core.types import Market

from ..core.fingerprint import fingerprint
from ..core.time import ensure_aware, to_utc
from .bar_artifact import DataTrustTier

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ZERO = Decimal(0)
_ONE = Decimal(1)
_VENUE_CATEGORIES = frozenset(
    {
        "SH_MAIN",
        "SZ_MAIN",
        "CHINEXT",
        "STAR",
        "BSE",
    }
)


class FreeStockDbGovernanceError(ValueError):
    """Raised when sidecar audit or comparison evidence is unsafe."""


class SidecarAuditFileKind(StrEnum):
    EXECUTABLE = "EXECUTABLE"
    LIBRARY = "LIBRARY"
    MANIFEST = "MANIFEST"
    CONFIG = "CONFIG"
    OTHER = "OTHER"


class SidecarNetworkProtocol(StrEnum):
    DNS = "DNS"
    HTTP = "HTTP"
    HTTPS = "HTTPS"
    TCP = "TCP"


class SidecarLicenseStatus(StrEnum):
    PENDING = "LICENSE_PENDING"
    CLEARED = "LICENSE_CLEARED"
    REJECTED = "LICENSE_REJECTED"


class SidecarReleaseAuditState(StrEnum):
    CONTRACT_ONLY = "CONTRACT_ONLY"
    BLOCKED = "BLOCKED"
    SANDBOX_AUDITED = "SANDBOX_AUDITED"


class SidecarSampleCategory(StrEnum):
    SH_MAIN = "SH_MAIN"
    SZ_MAIN = "SZ_MAIN"
    CHINEXT = "CHINEXT"
    STAR = "STAR"
    BSE = "BSE"
    ETF = "ETF"
    ST = "ST"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"
    RENAMED = "RENAMED"
    CASH_DIVIDEND = "CASH_DIVIDEND"
    SHARE_DISTRIBUTION = "SHARE_DISTRIBUTION"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    GAP = "GAP"


class SidecarFindingSeverity(StrEnum):
    HARD_BLOCK = "HARD_BLOCK"
    TRUST_BLOCK = "TRUST_BLOCK"
    WARNING = "WARNING"
    INFO = "INFO"


class SidecarComparisonState(StrEnum):
    CONTRACT_ONLY = "CONTRACT_ONLY"
    BLOCKED = "BLOCKED"
    SHADOW_ELIGIBLE = "SHADOW_ELIGIBLE"


class SidecarShadowState(StrEnum):
    DISABLED = "DISABLED"
    BLOCKED = "BLOCKED"
    SHADOW_ONLY = "SHADOW_ONLY"


def _require_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise FreeStockDbGovernanceError(
            f"{name} must be a non-empty trimmed string"
        )
    return value


def _require_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise FreeStockDbGovernanceError(f"{name} must be a boolean")
    return value


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise FreeStockDbGovernanceError(f"{name} must be lowercase SHA-256")
    return text


def _require_nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FreeStockDbGovernanceError(
            f"{name} must be a non-negative integer"
        )
    return value


def _require_positive_int(value: object, name: str) -> int:
    result = _require_nonnegative_int(value, name)
    if result == 0:
        raise FreeStockDbGovernanceError(f"{name} must be positive")
    return result


def _require_decimal(
    value: object,
    name: str,
    *,
    lower: Decimal | None = None,
    upper: Decimal | None = None,
) -> Decimal:
    if type(value) is not Decimal:
        raise FreeStockDbGovernanceError(
            f"{name} must be Decimal; float, integer and boolean are forbidden"
        )
    if not value.is_finite():
        raise FreeStockDbGovernanceError(f"{name} must be finite")
    if lower is not None and value < lower:
        raise FreeStockDbGovernanceError(f"{name} is below its lower bound")
    if upper is not None and value > upper:
        raise FreeStockDbGovernanceError(f"{name} is above its upper bound")
    return value


def _relative_delta(reference: Decimal, actual: Decimal) -> Decimal:
    denominator = max(abs(reference), Decimal("0.000000000001"))
    return abs(actual - reference) / denominator


@dataclass(frozen=True, slots=True)
class SidecarAuditFile:
    relative_path: str
    kind: SidecarAuditFileKind
    byte_size: int
    sha256: str
    file_id: str = field(init=False)

    def __post_init__(self) -> None:
        path = _require_text(self.relative_path, "relative_path")
        if "\\" in path:
            raise FreeStockDbGovernanceError(
                "relative_path must use POSIX separators"
            )
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or ".." in parsed.parts or parsed.as_posix() != path:
            raise FreeStockDbGovernanceError(
                "relative_path must be canonical and relative"
            )
        if not isinstance(self.kind, SidecarAuditFileKind):
            raise FreeStockDbGovernanceError(
                "kind must be SidecarAuditFileKind"
            )
        _require_positive_int(self.byte_size, "byte_size")
        _require_sha256(self.sha256, "sha256")
        object.__setattr__(
            self,
            "file_id",
            fingerprint(
                {
                    "schema": "free-stockdb-audit-file-v1",
                    "relative_path": path,
                    "kind": self.kind,
                    "byte_size": self.byte_size,
                    "sha256": self.sha256,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class SidecarNetworkObservation:
    process_sha256: str
    observed_at: datetime
    destination_host: str
    destination_port: int
    protocol: SidecarNetworkProtocol
    purpose: str
    approved: bool
    approval_evidence_id: str | None
    observation_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha256(self.process_sha256, "process_sha256")
        ensure_aware(self.observed_at, "observed_at")
        _require_text(self.destination_host, "destination_host")
        port = _require_positive_int(self.destination_port, "destination_port")
        if port > 65535:
            raise FreeStockDbGovernanceError(
                "destination_port must be no greater than 65535"
            )
        if not isinstance(self.protocol, SidecarNetworkProtocol):
            raise FreeStockDbGovernanceError(
                "protocol must be SidecarNetworkProtocol"
            )
        _require_text(self.purpose, "purpose")
        _require_bool(self.approved, "approved")
        if self.approved:
            _require_sha256(
                self.approval_evidence_id,
                "approval_evidence_id",
            )
        elif self.approval_evidence_id is not None:
            raise FreeStockDbGovernanceError(
                "unapproved network observation cannot carry approval evidence"
            )
        object.__setattr__(
            self,
            "observation_id",
            fingerprint(
                {
                    "schema": "free-stockdb-network-observation-v1",
                    "process_sha256": self.process_sha256,
                    "observed_at": to_utc(self.observed_at),
                    "destination_host": self.destination_host,
                    "destination_port": self.destination_port,
                    "protocol": self.protocol,
                    "purpose": self.purpose,
                    "approved": self.approved,
                    "approval_evidence_id": self.approval_evidence_id,
                }
            ),
        )

    @property
    def destination_is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.destination_host).is_loopback
        except ValueError:
            return False


@dataclass(frozen=True, slots=True)
class FreeStockDbReleaseAudit:
    release_version: str
    source_locator: str
    asset_sha256: str
    data_snapshot_manifest_sha256: str
    sync_manifest_sha256: str
    captured_at: datetime
    files: tuple[SidecarAuditFile, ...]
    network_observations: tuple[SidecarNetworkObservation, ...]
    low_privilege_process: bool
    project_data_isolated: bool
    production_store_isolated: bool
    loopback_listener_only: bool
    updater_behavior_observed: bool
    license_status: SidecarLicenseStatus
    license_evidence_ids: tuple[str, ...]
    synthetic_fixture_only: bool
    source_note: str
    binary_inventory_sha256: str = field(init=False)
    engineering_blockers: tuple[str, ...] = field(init=False)
    trust_blockers: tuple[str, ...] = field(init=False)
    state: SidecarReleaseAuditState = field(init=False)
    audit_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.release_version, "release_version")
        _require_text(self.source_locator, "source_locator")
        for name in (
            "asset_sha256",
            "data_snapshot_manifest_sha256",
            "sync_manifest_sha256",
        ):
            _require_sha256(getattr(self, name), name)
        ensure_aware(self.captured_at, "captured_at")
        if any(not isinstance(item, SidecarAuditFile) for item in self.files):
            raise FreeStockDbGovernanceError(
                "files must contain SidecarAuditFile values"
            )
        file_order = tuple((item.relative_path, item.file_id) for item in self.files)
        if not self.files or file_order != tuple(sorted(file_order)):
            raise FreeStockDbGovernanceError(
                "files must be non-empty and sorted by relative_path"
            )
        if len({item.relative_path for item in self.files}) != len(self.files):
            raise FreeStockDbGovernanceError("file paths must be unique")
        binary_files = tuple(
            item
            for item in self.files
            if item.kind
            in {
                SidecarAuditFileKind.EXECUTABLE,
                SidecarAuditFileKind.LIBRARY,
            }
        )
        binary_inventory_sha256 = fingerprint(
            {
                "schema": "free-stockdb-binary-inventory-v1",
                "file_ids": [item.file_id for item in binary_files],
            }
        )
        object.__setattr__(
            self,
            "binary_inventory_sha256",
            binary_inventory_sha256,
        )
        if any(
            not isinstance(item, SidecarNetworkObservation)
            for item in self.network_observations
        ):
            raise FreeStockDbGovernanceError(
                "network_observations must contain SidecarNetworkObservation values"
            )
        observation_ids = tuple(
            item.observation_id for item in self.network_observations
        )
        if any(
            to_utc(item.observed_at) > to_utc(self.captured_at)
            for item in self.network_observations
        ):
            raise FreeStockDbGovernanceError(
                "audit capture cannot precede an observation"
            )
        if observation_ids != tuple(sorted(set(observation_ids))):
            raise FreeStockDbGovernanceError(
                "network observations must be sorted and unique"
            )
        audited_process_hashes = {item.sha256 for item in binary_files}
        if any(
            observation.process_sha256 not in audited_process_hashes
            for observation in self.network_observations
        ):
            raise FreeStockDbGovernanceError(
                "network observation process is not in the audited binary inventory"
            )
        if any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in self.license_evidence_ids
        ):
            raise FreeStockDbGovernanceError(
                "license_evidence_ids must contain lowercase SHA-256"
            )
        if self.license_evidence_ids != tuple(
            sorted(set(self.license_evidence_ids))
        ):
            raise FreeStockDbGovernanceError(
                "license_evidence_ids must be sorted and unique"
            )
        for name in (
            "low_privilege_process",
            "project_data_isolated",
            "production_store_isolated",
            "loopback_listener_only",
            "updater_behavior_observed",
            "synthetic_fixture_only",
        ):
            _require_bool(getattr(self, name), name)
        if not isinstance(self.license_status, SidecarLicenseStatus):
            raise FreeStockDbGovernanceError(
                "license_status must be SidecarLicenseStatus"
            )
        if self.license_status is SidecarLicenseStatus.PENDING:
            if self.license_evidence_ids:
                raise FreeStockDbGovernanceError(
                    "pending license status cannot carry clearance evidence"
                )
        elif not self.license_evidence_ids:
            raise FreeStockDbGovernanceError(
                "cleared or rejected license status requires evidence IDs"
            )
        _require_text(self.source_note, "source_note")

        engineering: set[str] = set()
        kinds = {item.kind for item in self.files}
        if not kinds & {
            SidecarAuditFileKind.EXECUTABLE,
            SidecarAuditFileKind.LIBRARY,
        }:
            engineering.add("MISSING_EXECUTABLE_OR_LIBRARY")
        if SidecarAuditFileKind.MANIFEST not in kinds:
            engineering.add("MISSING_MANIFEST")
        if not self.low_privilege_process:
            engineering.add("PROCESS_NOT_LOW_PRIVILEGE")
        if not self.project_data_isolated:
            engineering.add("PROJECT_DATA_NOT_ISOLATED")
        if not self.production_store_isolated:
            engineering.add("PRODUCTION_STORE_NOT_ISOLATED")
        if not self.loopback_listener_only:
            engineering.add("LISTENER_NOT_LOOPBACK_ONLY")
        if not self.updater_behavior_observed:
            engineering.add("UPDATER_BEHAVIOR_NOT_OBSERVED")
        elif not self.network_observations:
            engineering.add("NETWORK_OBSERVATIONS_MISSING")
        for observation in self.network_observations:
            if not observation.destination_is_loopback and not observation.approved:
                engineering.add(
                    f"UNAPPROVED_NETWORK:{observation.observation_id}"
                )

        trust: set[str] = set()
        if self.license_status is SidecarLicenseStatus.PENDING:
            trust.add("LICENSE_PENDING")
        elif self.license_status is SidecarLicenseStatus.REJECTED:
            trust.add("LICENSE_REJECTED")

        if self.synthetic_fixture_only:
            state = SidecarReleaseAuditState.CONTRACT_ONLY
        elif engineering:
            state = SidecarReleaseAuditState.BLOCKED
        else:
            state = SidecarReleaseAuditState.SANDBOX_AUDITED
        engineering_tuple = tuple(sorted(engineering))
        trust_tuple = tuple(sorted(trust))
        object.__setattr__(self, "engineering_blockers", engineering_tuple)
        object.__setattr__(self, "trust_blockers", trust_tuple)
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "audit_id",
            fingerprint(
                {
                    "schema": "free-stockdb-release-audit-v1",
                    "release_version": self.release_version,
                    "source_locator": self.source_locator,
                    "asset_sha256": self.asset_sha256,
                    "binary_inventory_sha256": self.binary_inventory_sha256,
                    "data_snapshot_manifest_sha256": self.data_snapshot_manifest_sha256,
                    "sync_manifest_sha256": self.sync_manifest_sha256,
                    "captured_at": to_utc(self.captured_at),
                    "file_ids": [item.file_id for item in self.files],
                    "network_observation_ids": list(observation_ids),
                    "low_privilege_process": self.low_privilege_process,
                    "project_data_isolated": self.project_data_isolated,
                    "production_store_isolated": self.production_store_isolated,
                    "loopback_listener_only": self.loopback_listener_only,
                    "updater_behavior_observed": self.updater_behavior_observed,
                    "license_status": self.license_status,
                    "license_evidence_ids": list(self.license_evidence_ids),
                    "synthetic_fixture_only": self.synthetic_fixture_only,
                    "source_note": self.source_note,
                    "engineering_blockers": engineering_tuple,
                    "trust_blockers": trust_tuple,
                    "state": state,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class SidecarNormalizedBarPoint:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: Decimal
    turnover: Decimal | None
    point_id: str = field(init=False)

    def __post_init__(self) -> None:
        ensure_aware(self.timestamp, "timestamp")
        for name in ("open", "high", "low", "close"):
            _require_decimal(
                getattr(self, name),
                name,
                lower=Decimal("0.000000000001"),
            )
        if self.low > min(self.open, self.close, self.high):
            raise FreeStockDbGovernanceError("low is inconsistent with OHLC")
        if self.high < max(self.open, self.close, self.low):
            raise FreeStockDbGovernanceError("high is inconsistent with OHLC")
        _require_nonnegative_int(self.volume, "volume")
        _require_decimal(self.amount, "amount", lower=_ZERO)
        if self.turnover is not None:
            _require_decimal(self.turnover, "turnover", lower=_ZERO)
        object.__setattr__(
            self,
            "point_id",
            fingerprint(
                {
                    "schema": "free-stockdb-normalized-bar-point-v1",
                    "timestamp": to_utc(self.timestamp),
                    "open": self.open,
                    "high": self.high,
                    "low": self.low,
                    "close": self.close,
                    "volume": self.volume,
                    "amount": self.amount,
                    "turnover": self.turnover,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class SidecarBarSeriesEvidence:
    source_name: str
    symbol: str
    market: Market
    interval: str
    start_at: datetime
    end_at: datetime
    snapshot_id: str
    sidecar_release_audit_id: str | None
    trust_tier: DataTrustTier
    verified: bool
    complete: bool
    synthetic_fixture_only: bool
    provenance_ids: tuple[str, ...]
    points: tuple[SidecarNormalizedBarPoint, ...]
    series_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.source_name, "source_name")
        symbol = _require_text(self.symbol, "symbol")
        if symbol != symbol.upper() or not symbol.endswith((".SH", ".SZ")):
            raise FreeStockDbGovernanceError(
                "symbol must use uppercase A-share canonical form"
            )
        if not isinstance(self.market, Market) or self.market is not Market.A:
            raise FreeStockDbGovernanceError(
                "sidecar comparison currently supports A-share only"
            )
        if self.interval not in {"1d", "1m"}:
            raise FreeStockDbGovernanceError("interval must be 1d or 1m")
        ensure_aware(self.start_at, "start_at")
        ensure_aware(self.end_at, "end_at")
        if to_utc(self.end_at) < to_utc(self.start_at):
            raise FreeStockDbGovernanceError("end_at cannot precede start_at")
        _require_sha256(self.snapshot_id, "snapshot_id")
        if self.source_name == "free_stockdb":
            _require_sha256(
                self.sidecar_release_audit_id,
                "sidecar_release_audit_id",
            )
        elif self.sidecar_release_audit_id is not None:
            raise FreeStockDbGovernanceError(
                "non-sidecar reference cannot carry sidecar_release_audit_id"
            )
        if not isinstance(self.trust_tier, DataTrustTier):
            raise FreeStockDbGovernanceError("trust_tier must be DataTrustTier")
        for name in ("verified", "complete", "synthetic_fixture_only"):
            _require_bool(getattr(self, name), name)
        if any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in self.provenance_ids
        ):
            raise FreeStockDbGovernanceError(
                "provenance_ids must contain lowercase SHA-256"
            )
        if self.provenance_ids != tuple(sorted(set(self.provenance_ids))):
            raise FreeStockDbGovernanceError(
                "provenance_ids must be sorted and unique"
            )
        if self.verified and not self.provenance_ids:
            raise FreeStockDbGovernanceError(
                "verified series requires provenance evidence"
            )
        if self.source_name == "free_stockdb":
            if self.verified or self.trust_tier is not DataTrustTier.BEST_EFFORT:
                raise FreeStockDbGovernanceError(
                    "free_stockdb series must remain unverified BEST_EFFORT"
                )
        elif self.trust_tier in {
            DataTrustTier.OPERATIONAL_VERIFIED,
            DataTrustTier.RESEARCH_GRADE,
            DataTrustTier.FROZEN_HOLDOUT,
        } and not self.verified:
            raise FreeStockDbGovernanceError(
                "high-trust reference series must be verified"
            )
        if self.synthetic_fixture_only and (
            self.verified or self.trust_tier is not DataTrustTier.BEST_EFFORT
        ):
            raise FreeStockDbGovernanceError(
                "synthetic series must remain unverified BEST_EFFORT"
            )
        if any(
            not isinstance(item, SidecarNormalizedBarPoint) for item in self.points
        ):
            raise FreeStockDbGovernanceError(
                "points must contain SidecarNormalizedBarPoint values"
            )
        point_order = tuple(
            (to_utc(item.timestamp), item.point_id) for item in self.points
        )
        if point_order != tuple(sorted(point_order)):
            raise FreeStockDbGovernanceError("points must be sorted by timestamp")
        if len({to_utc(item.timestamp) for item in self.points}) != len(self.points):
            raise FreeStockDbGovernanceError("bar timestamps must be unique")
        if any(
            to_utc(item.timestamp) < to_utc(self.start_at)
            or to_utc(item.timestamp) > to_utc(self.end_at)
            for item in self.points
        ):
            raise FreeStockDbGovernanceError(
                "bar point falls outside series range"
            )
        object.__setattr__(
            self,
            "series_id",
            fingerprint(
                {
                    "schema": "free-stockdb-bar-series-evidence-v1",
                    "source_name": self.source_name,
                    "symbol": self.symbol,
                    "market": self.market,
                    "interval": self.interval,
                    "start_at": to_utc(self.start_at),
                    "end_at": to_utc(self.end_at),
                    "snapshot_id": self.snapshot_id,
                    "sidecar_release_audit_id": self.sidecar_release_audit_id,
                    "trust_tier": self.trust_tier,
                    "verified": self.verified,
                    "complete": self.complete,
                    "synthetic_fixture_only": self.synthetic_fixture_only,
                    "provenance_ids": list(self.provenance_ids),
                    "point_ids": [item.point_id for item in self.points],
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class SidecarComparisonSample:
    instrument_id: str
    identity_fact_id: str
    symbol: str
    categories: tuple[SidecarSampleCategory, ...]
    category_evidence_ids: tuple[str, ...]
    reference: SidecarBarSeriesEvidence
    sidecar: SidecarBarSeriesEvidence
    sample_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        _require_sha256(self.identity_fact_id, "identity_fact_id")
        _require_text(self.symbol, "symbol")
        if any(
            not isinstance(item, SidecarSampleCategory)
            for item in self.categories
        ):
            raise FreeStockDbGovernanceError(
                "categories must contain SidecarSampleCategory values"
            )
        expected_categories = tuple(
            sorted(set(self.categories), key=lambda item: item.value)
        )
        if not self.categories or self.categories != expected_categories:
            raise FreeStockDbGovernanceError(
                "categories must be non-empty, sorted and unique"
            )
        venue_categories = {
            item.value for item in self.categories if item.value in _VENUE_CATEGORIES
        }
        if len(venue_categories) != 1:
            raise FreeStockDbGovernanceError(
                "sample must carry exactly one primary venue category"
            )
        if any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in self.category_evidence_ids
        ):
            raise FreeStockDbGovernanceError(
                "category_evidence_ids must contain lowercase SHA-256"
            )
        if self.category_evidence_ids != tuple(
            sorted(set(self.category_evidence_ids))
        ) or len(self.category_evidence_ids) != len(self.categories):
            raise FreeStockDbGovernanceError(
                "each category requires one sorted unique evidence ID"
            )
        if not isinstance(self.reference, SidecarBarSeriesEvidence) or not isinstance(
            self.sidecar,
            SidecarBarSeriesEvidence,
        ):
            raise FreeStockDbGovernanceError(
                "reference and sidecar must be SidecarBarSeriesEvidence"
            )
        if self.reference.source_name == "free_stockdb":
            raise FreeStockDbGovernanceError(
                "reference source cannot be free_stockdb"
            )
        if self.sidecar.source_name != "free_stockdb":
            raise FreeStockDbGovernanceError(
                "sidecar source_name must be free_stockdb"
            )
        if self.sidecar.trust_tier is not DataTrustTier.BEST_EFFORT:
            raise FreeStockDbGovernanceError(
                "sidecar evidence must remain BEST_EFFORT"
            )
        for name in ("symbol", "market", "interval", "start_at", "end_at"):
            if getattr(self.reference, name) != getattr(self.sidecar, name):
                raise FreeStockDbGovernanceError(
                    f"reference and sidecar {name} must match"
                )
        if self.symbol != self.reference.symbol:
            raise FreeStockDbGovernanceError(
                "sample symbol must match series symbol"
            )
        object.__setattr__(
            self,
            "sample_id",
            fingerprint(
                {
                    "schema": "free-stockdb-comparison-sample-v1",
                    "instrument_id": self.instrument_id,
                    "identity_fact_id": self.identity_fact_id,
                    "symbol": self.symbol,
                    "categories": [item.value for item in self.categories],
                    "category_evidence_ids": list(self.category_evidence_ids),
                    "reference_series_id": self.reference.series_id,
                    "sidecar_series_id": self.sidecar.series_id,
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class SidecarComparisonPolicy:
    policy_version: str
    minimum_samples: int
    maximum_samples: int
    required_categories: tuple[SidecarSampleCategory, ...]
    price_relative_tolerance: Decimal
    volume_relative_tolerance: Decimal
    amount_relative_tolerance: Decimal
    turnover_relative_tolerance: Decimal
    policy_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.policy_version, "policy_version")
        minimum = _require_positive_int(self.minimum_samples, "minimum_samples")
        maximum = _require_positive_int(self.maximum_samples, "maximum_samples")
        if minimum > maximum or maximum > 100:
            raise FreeStockDbGovernanceError(
                "sample bounds must satisfy 1 <= minimum <= maximum <= 100"
            )
        if any(
            not isinstance(item, SidecarSampleCategory)
            for item in self.required_categories
        ):
            raise FreeStockDbGovernanceError(
                "required_categories must contain SidecarSampleCategory values"
            )
        expected_categories = tuple(
            sorted(set(self.required_categories), key=lambda item: item.value)
        )
        if self.required_categories != expected_categories:
            raise FreeStockDbGovernanceError(
                "required_categories must be sorted and unique"
            )
        for name in (
            "price_relative_tolerance",
            "volume_relative_tolerance",
            "amount_relative_tolerance",
            "turnover_relative_tolerance",
        ):
            _require_decimal(getattr(self, name), name, lower=_ZERO, upper=_ONE)
        object.__setattr__(
            self,
            "policy_id",
            fingerprint(
                {
                    "schema": "free-stockdb-comparison-policy-v1",
                    "policy_version": self.policy_version,
                    "minimum_samples": self.minimum_samples,
                    "maximum_samples": self.maximum_samples,
                    "required_categories": [
                        item.value for item in self.required_categories
                    ],
                    "price_relative_tolerance": self.price_relative_tolerance,
                    "volume_relative_tolerance": self.volume_relative_tolerance,
                    "amount_relative_tolerance": self.amount_relative_tolerance,
                    "turnover_relative_tolerance": self.turnover_relative_tolerance,
                }
            ),
        )


DEFAULT_SIDECAR_COMPARISON_POLICY = SidecarComparisonPolicy(
    policy_version="free-stockdb-comparison-v1",
    minimum_samples=50,
    maximum_samples=100,
    required_categories=tuple(
        sorted(SidecarSampleCategory, key=lambda item: item.value)
    ),
    price_relative_tolerance=Decimal("0.0005"),
    volume_relative_tolerance=Decimal("0.001"),
    amount_relative_tolerance=Decimal("0.001"),
    turnover_relative_tolerance=Decimal("0.002"),
)


@dataclass(frozen=True, slots=True)
class SidecarComparisonFinding:
    severity: SidecarFindingSeverity
    code: str
    sample_id: str
    timestamp: datetime | None
    metric: str | None
    detail: str
    finding_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.severity, SidecarFindingSeverity):
            raise FreeStockDbGovernanceError(
                "severity must be SidecarFindingSeverity"
            )
        _require_text(self.code, "code")
        _require_sha256(self.sample_id, "sample_id")
        if self.timestamp is not None:
            ensure_aware(self.timestamp, "timestamp")
        if self.metric is not None:
            _require_text(self.metric, "metric")
        _require_text(self.detail, "detail")
        object.__setattr__(
            self,
            "finding_id",
            fingerprint(
                {
                    "schema": "free-stockdb-comparison-finding-v1",
                    "severity": self.severity,
                    "code": self.code,
                    "sample_id": self.sample_id,
                    "timestamp": (
                        None if self.timestamp is None else to_utc(self.timestamp)
                    ),
                    "metric": self.metric,
                    "detail": self.detail,
                }
            ),
        )


def _finding(
    severity: SidecarFindingSeverity,
    code: str,
    sample: SidecarComparisonSample,
    *,
    timestamp: datetime | None = None,
    metric: str | None = None,
    detail: str,
) -> SidecarComparisonFinding:
    return SidecarComparisonFinding(
        severity=severity,
        code=code,
        sample_id=sample.sample_id,
        timestamp=timestamp,
        metric=metric,
        detail=detail,
    )


def compare_sidecar_sample(
    sample: SidecarComparisonSample,
    policy: SidecarComparisonPolicy = DEFAULT_SIDECAR_COMPARISON_POLICY,
) -> tuple[SidecarComparisonFinding, ...]:
    """Compare normalized bars without making a trust-promotion claim."""

    if not isinstance(sample, SidecarComparisonSample):
        raise FreeStockDbGovernanceError(
            "sample must be SidecarComparisonSample"
        )
    if not isinstance(policy, SidecarComparisonPolicy):
        raise FreeStockDbGovernanceError(
            "policy must be SidecarComparisonPolicy"
        )
    findings: list[SidecarComparisonFinding] = []
    reference = {to_utc(item.timestamp): item for item in sample.reference.points}
    sidecar = {to_utc(item.timestamp): item for item in sample.sidecar.points}
    if not reference:
        findings.append(
            _finding(
                SidecarFindingSeverity.HARD_BLOCK,
                "REFERENCE_SERIES_EMPTY",
                sample,
                detail="reference series contains no bars",
            )
        )
    if not sidecar:
        findings.append(
            _finding(
                SidecarFindingSeverity.HARD_BLOCK,
                "SIDECAR_SERIES_EMPTY",
                sample,
                detail="sidecar series contains no bars",
            )
        )
    for timestamp in sorted(set(reference) - set(sidecar)):
        findings.append(
            _finding(
                SidecarFindingSeverity.HARD_BLOCK,
                "SIDECAR_BAR_MISSING",
                sample,
                timestamp=timestamp,
                detail="reference timestamp is absent from sidecar series",
            )
        )
    for timestamp in sorted(set(sidecar) - set(reference)):
        findings.append(
            _finding(
                SidecarFindingSeverity.HARD_BLOCK,
                "SIDECAR_BAR_EXTRA",
                sample,
                timestamp=timestamp,
                detail="sidecar timestamp is absent from reference series",
            )
        )
    for timestamp in sorted(set(reference) & set(sidecar)):
        expected = reference[timestamp]
        actual = sidecar[timestamp]
        for metric in ("open", "high", "low", "close"):
            delta = _relative_delta(getattr(expected, metric), getattr(actual, metric))
            if delta > policy.price_relative_tolerance:
                findings.append(
                    _finding(
                        SidecarFindingSeverity.TRUST_BLOCK,
                        "PRICE_MISMATCH",
                        sample,
                        timestamp=timestamp,
                        metric=metric,
                        detail=f"relative delta {delta} exceeds tolerance",
                    )
                )
        volume_delta = _relative_delta(
            Decimal(expected.volume),
            Decimal(actual.volume),
        )
        if volume_delta > policy.volume_relative_tolerance:
            findings.append(
                _finding(
                    SidecarFindingSeverity.TRUST_BLOCK,
                    "VOLUME_MISMATCH",
                    sample,
                    timestamp=timestamp,
                    metric="volume",
                    detail=f"relative delta {volume_delta} exceeds tolerance",
                )
            )
        amount_delta = _relative_delta(expected.amount, actual.amount)
        if amount_delta > policy.amount_relative_tolerance:
            findings.append(
                _finding(
                    SidecarFindingSeverity.TRUST_BLOCK,
                    "AMOUNT_MISMATCH",
                    sample,
                    timestamp=timestamp,
                    metric="amount",
                    detail=f"relative delta {amount_delta} exceeds tolerance",
                )
            )
        if expected.turnover is None or actual.turnover is None:
            if expected.turnover != actual.turnover:
                findings.append(
                    _finding(
                        SidecarFindingSeverity.WARNING,
                        "TURNOVER_MISSING_ON_ONE_SOURCE",
                        sample,
                        timestamp=timestamp,
                        metric="turnover",
                        detail="turnover is unavailable on exactly one source",
                    )
                )
        else:
            turnover_delta = _relative_delta(expected.turnover, actual.turnover)
            if turnover_delta > policy.turnover_relative_tolerance:
                findings.append(
                    _finding(
                        SidecarFindingSeverity.TRUST_BLOCK,
                        "TURNOVER_MISMATCH",
                        sample,
                        timestamp=timestamp,
                        metric="turnover",
                        detail=f"relative delta {turnover_delta} exceeds tolerance",
                    )
                )
    return tuple(sorted(findings, key=lambda item: item.finding_id))


@dataclass(frozen=True, slots=True)
class FreeStockDbComparisonReport:
    release_audit: FreeStockDbReleaseAudit
    policy: SidecarComparisonPolicy
    generated_at: datetime
    samples: tuple[SidecarComparisonSample, ...]
    findings: tuple[SidecarComparisonFinding, ...] = field(init=False)
    engineering_blockers: tuple[str, ...] = field(init=False)
    trust_blockers: tuple[str, ...] = field(init=False)
    state: SidecarComparisonState = field(init=False)
    report_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.release_audit, FreeStockDbReleaseAudit):
            raise FreeStockDbGovernanceError(
                "release_audit must be FreeStockDbReleaseAudit"
            )
        if not isinstance(self.policy, SidecarComparisonPolicy):
            raise FreeStockDbGovernanceError(
                "policy must be SidecarComparisonPolicy"
            )
        ensure_aware(self.generated_at, "generated_at")
        if to_utc(self.generated_at) < to_utc(self.release_audit.captured_at):
            raise FreeStockDbGovernanceError(
                "generated_at cannot precede release audit capture"
            )
        if any(
            not isinstance(item, SidecarComparisonSample) for item in self.samples
        ):
            raise FreeStockDbGovernanceError(
                "samples must contain SidecarComparisonSample values"
            )
        sample_ids = tuple(item.sample_id for item in self.samples)
        if sample_ids != tuple(sorted(set(sample_ids))):
            raise FreeStockDbGovernanceError(
                "samples must be sorted and unique by sample_id"
            )
        if len({sample.instrument_id for sample in self.samples}) != len(
            self.samples
        ):
            raise FreeStockDbGovernanceError(
                "comparison report must contain at most one sample per instrument"
            )
        if self.samples and to_utc(self.generated_at) < max(
            to_utc(series.end_at)
            for sample in self.samples
            for series in (sample.reference, sample.sidecar)
        ):
            raise FreeStockDbGovernanceError(
                "generated_at cannot precede comparison series end"
            )
        for sample in self.samples:
            if sample.sidecar.sidecar_release_audit_id != self.release_audit.audit_id:
                raise FreeStockDbGovernanceError(
                    "sidecar series is not bound to this release audit"
                )

        findings = tuple(
            sorted(
                (
                    finding
                    for sample in self.samples
                    for finding in compare_sidecar_sample(sample, self.policy)
                ),
                key=lambda item: item.finding_id,
            )
        )
        engineering: set[str] = set(self.release_audit.engineering_blockers)
        trust: set[str] = set(self.release_audit.trust_blockers)
        if self.release_audit.state is not SidecarReleaseAuditState.SANDBOX_AUDITED:
            engineering.add(
                f"RELEASE_AUDIT_STATE:{self.release_audit.state.value}"
            )
        count = len(self.samples)
        if count < self.policy.minimum_samples:
            engineering.add("SAMPLE_COUNT_BELOW_MINIMUM")
        if count > self.policy.maximum_samples:
            engineering.add("SAMPLE_COUNT_ABOVE_MAXIMUM")
        covered_categories = {
            category for sample in self.samples for category in sample.categories
        }
        for category in self.policy.required_categories:
            if category not in covered_categories:
                engineering.add(f"MISSING_CATEGORY:{category.value}")
        if any(
            finding.severity is SidecarFindingSeverity.HARD_BLOCK
            for finding in findings
        ):
            engineering.add("HARD_COMPARISON_FINDINGS")
        if any(
            finding.severity is SidecarFindingSeverity.TRUST_BLOCK
            for finding in findings
        ):
            trust.add("TRUST_COMPARISON_FINDINGS")
        for sample in self.samples:
            if sample.reference.trust_tier not in {
                DataTrustTier.OPERATIONAL_VERIFIED,
                DataTrustTier.RESEARCH_GRADE,
                DataTrustTier.FROZEN_HOLDOUT,
            }:
                trust.add(f"REFERENCE_TRUST_INSUFFICIENT:{sample.sample_id}")
            if not sample.reference.verified:
                trust.add(f"REFERENCE_UNVERIFIED:{sample.sample_id}")
            if not sample.reference.complete:
                trust.add(f"REFERENCE_INCOMPLETE:{sample.sample_id}")
            if not sample.sidecar.complete:
                engineering.add(f"SIDECAR_SERIES_INCOMPLETE:{sample.sample_id}")

        synthetic = self.release_audit.synthetic_fixture_only or any(
            sample.reference.synthetic_fixture_only
            or sample.sidecar.synthetic_fixture_only
            for sample in self.samples
        )
        if synthetic:
            state = SidecarComparisonState.CONTRACT_ONLY
        elif engineering or trust:
            state = SidecarComparisonState.BLOCKED
        else:
            state = SidecarComparisonState.SHADOW_ELIGIBLE
        engineering_tuple = tuple(sorted(engineering))
        trust_tuple = tuple(sorted(trust))
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "engineering_blockers", engineering_tuple)
        object.__setattr__(self, "trust_blockers", trust_tuple)
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "report_id",
            fingerprint(
                {
                    "schema": "free-stockdb-comparison-report-v1",
                    "release_audit_id": self.release_audit.audit_id,
                    "policy_id": self.policy.policy_id,
                    "generated_at": to_utc(self.generated_at),
                    "sample_ids": list(sample_ids),
                    "finding_ids": [item.finding_id for item in findings],
                    "engineering_blockers": engineering_tuple,
                    "trust_blockers": trust_tuple,
                    "state": state,
                    "evidence_tier_status": "T3_NOT_REACHED",
                }
            ),
        )

    @property
    def evidence_tier_status(self) -> str:
        return "T3_NOT_REACHED"


@dataclass(frozen=True, slots=True)
class FreeStockDbShadowAssessment:
    comparison_report: FreeStockDbComparisonReport
    requested_enabled: bool
    evaluated_at: datetime
    state: SidecarShadowState = field(init=False)
    blockers: tuple[str, ...] = field(init=False)
    trust_tier: DataTrustTier = field(
        init=False,
        default=DataTrustTier.BEST_EFFORT,
    )
    affects_live_decision: bool = field(init=False, default=False)
    affects_model_training: bool = field(init=False, default=False)
    allows_public_redistribution: bool = field(init=False, default=False)
    formal_research_eligible: bool = field(init=False, default=False)
    assessment_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.comparison_report, FreeStockDbComparisonReport):
            raise FreeStockDbGovernanceError(
                "comparison_report must be FreeStockDbComparisonReport"
            )
        _require_bool(self.requested_enabled, "requested_enabled")
        ensure_aware(self.evaluated_at, "evaluated_at")
        if to_utc(self.evaluated_at) < to_utc(self.comparison_report.generated_at):
            raise FreeStockDbGovernanceError(
                "evaluated_at cannot precede comparison report"
            )
        if not self.requested_enabled:
            state = SidecarShadowState.DISABLED
            blockers = ("CONFIG_DISABLED",)
        elif self.comparison_report.state is SidecarComparisonState.SHADOW_ELIGIBLE:
            state = SidecarShadowState.SHADOW_ONLY
            blockers = ()
        else:
            state = SidecarShadowState.BLOCKED
            blockers = tuple(
                sorted(
                    set(
                        self.comparison_report.engineering_blockers
                        + self.comparison_report.trust_blockers
                        + (
                            f"REPORT_STATE:{self.comparison_report.state.value}",
                        )
                    )
                )
            )
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "blockers", blockers)
        object.__setattr__(
            self,
            "assessment_id",
            fingerprint(
                {
                    "schema": "free-stockdb-shadow-assessment-v1",
                    "comparison_report_id": self.comparison_report.report_id,
                    "requested_enabled": self.requested_enabled,
                    "evaluated_at": to_utc(self.evaluated_at),
                    "state": state,
                    "blockers": blockers,
                    "trust_tier": DataTrustTier.BEST_EFFORT,
                    "affects_live_decision": False,
                    "affects_model_training": False,
                    "allows_public_redistribution": False,
                    "formal_research_eligible": False,
                }
            ),
        )


__all__ = [
    "DEFAULT_SIDECAR_COMPARISON_POLICY",
    "FreeStockDbComparisonReport",
    "FreeStockDbGovernanceError",
    "FreeStockDbReleaseAudit",
    "FreeStockDbShadowAssessment",
    "SidecarAuditFile",
    "SidecarAuditFileKind",
    "SidecarBarSeriesEvidence",
    "SidecarComparisonFinding",
    "SidecarComparisonPolicy",
    "SidecarComparisonSample",
    "SidecarComparisonState",
    "SidecarFindingSeverity",
    "SidecarLicenseStatus",
    "SidecarNetworkObservation",
    "SidecarNetworkProtocol",
    "SidecarNormalizedBarPoint",
    "SidecarReleaseAuditState",
    "SidecarSampleCategory",
    "SidecarShadowState",
    "compare_sidecar_sample",
]
