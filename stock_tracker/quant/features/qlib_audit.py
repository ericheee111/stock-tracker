"""Explicit Qlib adaptation audit; blockers prevent equivalence claims."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..core.fingerprint import fingerprint


class AuditStatus(StrEnum):
    PASS = "PASS"
    BLOCKER = "BLOCKER"


@dataclass(frozen=True, slots=True)
class AuditItem:
    name: str
    status: AuditStatus
    evidence: str

    def __post_init__(self) -> None:
        if not self.name or not self.evidence:
            raise ValueError("audit item requires name and evidence")


@dataclass(frozen=True, slots=True)
class QlibAdaptationAudit:
    items: tuple[AuditItem, ...]

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.items if item.status is AuditStatus.BLOCKER)

    @property
    def may_claim_numerical_equivalence(self) -> bool:
        return not self.blockers

    @property
    def audit_id(self) -> str:
        return fingerprint({"schema": "qlib-adaptation-audit-v1", "items": self.items})


def default_qlib_audit() -> QlibAdaptationAudit:
    return QlibAdaptationAudit(
        items=(
            AuditItem(
                "Feature causality",
                AuditStatus.PASS,
                "FeatureContext rejects bars after as_of.",
            ),
            AuditItem(
                "Train-only normalization",
                AuditStatus.PASS,
                "Transform identity binds the training dataset only.",
            ),
            AuditItem(
                "Point-in-Time universe",
                AuditStatus.PASS,
                "FeatureContext requires a PIT universe snapshot ID.",
            ),
            AuditItem(
                "Default Qlib label replaced",
                AuditStatus.PASS,
                "Execution-aware Target-Before-Stop is used instead.",
            ),
            AuditItem(
                "PIT cross-sectional transform",
                AuditStatus.PASS,
                "Ranks consume one same-timestamp mapping only.",
            ),
            AuditItem(
                "Corporate-action golden mapping",
                AuditStatus.BLOCKER,
                "No authoritative split/dividend golden corpus is bundled.",
            ),
            AuditItem(
                "Exact Qlib revision pinned",
                AuditStatus.BLOCKER,
                "No reviewed upstream Qlib commit is pinned in production config.",
            ),
            AuditItem(
                "Golden-data numerical equivalence",
                AuditStatus.BLOCKER,
                "No official Qlib fixture has been compared feature-by-feature.",
            ),
        )
    )
