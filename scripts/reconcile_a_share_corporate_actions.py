from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from stock_tracker.core.types import Market
from stock_tracker.quant.data.corporate_action_adapter import (
    CandidateCorporateAction,
    CandidateCorporateActionLifecycle,
    CandidateCorporateActionType,
    CorporateActionSourceFamily,
    CorporateActionSourceOwner,
    SourcePublishedGranularity,
)
from stock_tracker.quant.data.corporate_action_extraction import (
    BoundCorporateActionCandidateBundle,
    IdentityBindingStatus,
    RowIdentityBinding,
)
from stock_tracker.quant.data.corporate_action_reconciliation import (
    CandidateActionMapping,
    CorporateActionReconciliationError,
    CoverageClaimCandidate,
    LicenseStatus,
    ReconciliationPolicy,
    reconcile_corporate_actions,
)

_SCHEMA = "stage2e-corporate-action-reconciliation-request-v1"
_TOP_FIELDS = frozenset(
    {
        "schema",
        "synthetic_fixture",
        "as_of",
        "policy",
        "bundles",
        "action_mappings",
        "coverage_claims",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "action_id",
        "instrument_id",
        "identity_fact_id",
        "symbol",
        "market",
        "exchange",
        "action_type",
        "lifecycle",
        "source_published_at",
        "source_published_granularity",
        "observed_at",
        "retrieved_at",
        "known_at",
        "usable_from",
        "ex_date",
        "record_date",
        "payment_date",
        "share_listing_date",
        "effective_date",
        "automatic_share_ratio",
        "cash_dividend_per_share",
        "rights_entitlement_ratio",
        "rights_subscription_price",
        "currency",
        "reference_price",
        "reference_price_snapshot_id",
        "revision_id",
        "supersedes_revision_id",
        "source_uri",
        "raw_artifact_id",
        "raw_descriptor_id",
        "parser_version",
        "source_owner",
        "source_family",
        "source_version",
        "synthetic_fixture",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "row_id",
        "status",
        "mapping_id",
        "identity_fact_id",
        "instrument_id",
        "reason",
    }
)
_BUNDLE_FIELDS = frozenset(
    {
        "document_id",
        "extraction_descriptor_id",
        "raw_artifact_id",
        "raw_descriptor_id",
        "mapping_policy_version",
        "as_of",
        "bindings",
        "candidates",
        "synthetic_fixture",
    }
)
_MAPPING_FIELDS = frozenset(
    {
        "candidate_id",
        "logical_action_id",
        "mapping_policy_version",
        "mapping_note",
    }
)
_CLAIM_FIELDS = frozenset(
    {
        "instrument_id",
        "source_owner",
        "source_version",
        "start_date",
        "end_date",
        "known_at",
        "usable_from",
        "surveyed_source_event_ids",
        "coverage_note",
        "license_status",
        "synthetic_fixture",
    }
)
_POLICY_FIELDS = frozenset(
    {
        "policy_version",
        "required_primary_owners",
        "minimum_independent_sources",
        "require_reference_price_evidence",
        "require_license_clearance",
        "require_attachment_evidence",
        "allow_synthetic_eligibility_test",
        "synthetic_fixture",
    }
)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CorporateActionReconciliationError(
            f"{name} must be a non-empty trimmed string"
        )
    return value


def _bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise CorporateActionReconciliationError(f"{name} must be a boolean")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise CorporateActionReconciliationError(
            f"{name} must be lowercase SHA-256"
        )
    return text


def _datetime(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CorporateActionReconciliationError(
            f"{name} must be ISO-8601 datetime"
        ) from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise CorporateActionReconciliationError(f"{name} must include timezone")
    return result


def _date(value: object, name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(_text(value, name))
    except ValueError as exc:
        raise CorporateActionReconciliationError(
            f"{name} must be YYYY-MM-DD or null"
        ) from exc


def _decimal(value: object, name: str) -> Decimal | None:
    if value is None:
        return None
    text = _text(value, name)
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise CorporateActionReconciliationError(
            f"{name} is not decimal text"
        ) from exc
    if not result.is_finite():
        raise CorporateActionReconciliationError(f"{name} must be finite")
    canonical = format(result, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if result == 0:
        canonical = "0"
    if canonical != text:
        raise CorporateActionReconciliationError(
            f"{name} must be canonical decimal text"
        )
    return result


def _dict(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise CorporateActionReconciliationError(f"{name} must be a JSON object")
    return value


def _strict_dict(
    value: object,
    name: str,
    fields: frozenset[str],
) -> dict[str, object]:
    result = _dict(value, name)
    unknown = sorted(set(result) - fields)
    missing = sorted(fields - set(result))
    if unknown or missing:
        raise CorporateActionReconciliationError(
            f"{name} field mismatch; unknown={unknown}, missing={missing}"
        )
    return result


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise CorporateActionReconciliationError(f"{name} must be a JSON array")
    return value


def _strict_json(text: str) -> object:
    def reject_constant(value: str) -> object:
        raise CorporateActionReconciliationError(
            f"non-finite JSON constant {value!r} is forbidden"
        )

    def reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise CorporateActionReconciliationError(
                    f"duplicate JSON field is forbidden: {key}"
                )
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except json.JSONDecodeError as exc:
        raise CorporateActionReconciliationError(
            "input request is unreadable JSON"
        ) from exc


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.is_symlink():
            raise CorporateActionReconciliationError(
                "output path must be a regular non-symlink file"
            )
        if path.read_bytes() != payload:
            raise CorporateActionReconciliationError(
                "immutable output path contains different content"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if any(parent.is_symlink() for parent in (path.parent, *path.parents)):
        raise CorporateActionReconciliationError(
            "output path cannot traverse a symlink"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _published(value: object, granularity: SourcePublishedGranularity):
    if granularity is SourcePublishedGranularity.UNKNOWN:
        if value is not None:
            raise CorporateActionReconciliationError(
                "UNKNOWN publication granularity requires null value"
            )
        return None
    if granularity is SourcePublishedGranularity.DATE:
        return _date(value, "source_published_at")
    return _datetime(value, "source_published_at")


def _candidate(value: object) -> CandidateCorporateAction:
    item = _strict_dict(value, "candidate", _CANDIDATE_FIELDS)
    granularity = SourcePublishedGranularity(
        _text(
            item["source_published_granularity"],
            "source_published_granularity",
        )
    )
    return CandidateCorporateAction(
        action_id=_text(item["action_id"], "action_id"),
        instrument_id=_text(item["instrument_id"], "instrument_id"),
        identity_fact_id=_sha(item["identity_fact_id"], "identity_fact_id"),
        symbol=_text(item["symbol"], "symbol"),
        market=Market(_text(item["market"], "market")),
        exchange=_text(item["exchange"], "exchange"),
        action_type=CandidateCorporateActionType(
            _text(item["action_type"], "action_type")
        ),
        lifecycle=CandidateCorporateActionLifecycle(
            _text(item["lifecycle"], "lifecycle")
        ),
        source_published_at=_published(
            item["source_published_at"],
            granularity,
        ),
        source_published_granularity=granularity,
        observed_at=_datetime(item["observed_at"], "observed_at"),
        retrieved_at=_datetime(item["retrieved_at"], "retrieved_at"),
        known_at=_datetime(item["known_at"], "known_at"),
        usable_from=_datetime(item["usable_from"], "usable_from"),
        ex_date=_date(item["ex_date"], "ex_date"),
        record_date=_date(item["record_date"], "record_date"),
        payment_date=_date(item["payment_date"], "payment_date"),
        share_listing_date=_date(
            item["share_listing_date"],
            "share_listing_date",
        ),
        effective_date=_date(item["effective_date"], "effective_date"),
        automatic_share_ratio=_decimal(
            item["automatic_share_ratio"],
            "automatic_share_ratio",
        ),
        cash_dividend_per_share=_decimal(
            item["cash_dividend_per_share"],
            "cash_dividend_per_share",
        ),
        rights_entitlement_ratio=_decimal(
            item["rights_entitlement_ratio"],
            "rights_entitlement_ratio",
        ),
        rights_subscription_price=_decimal(
            item["rights_subscription_price"],
            "rights_subscription_price",
        ),
        currency=(
            None
            if item["currency"] is None
            else _text(item["currency"], "currency")
        ),
        reference_price=_decimal(item["reference_price"], "reference_price"),
        reference_price_snapshot_id=(
            None
            if item["reference_price_snapshot_id"] is None
            else _sha(
                item["reference_price_snapshot_id"],
                "reference_price_snapshot_id",
            )
        ),
        revision_id=_text(item["revision_id"], "revision_id"),
        supersedes_revision_id=(
            None
            if item["supersedes_revision_id"] is None
            else _text(
                item["supersedes_revision_id"],
                "supersedes_revision_id",
            )
        ),
        source_uri=_text(item["source_uri"], "source_uri"),
        raw_artifact_id=_sha(item["raw_artifact_id"], "raw_artifact_id"),
        raw_descriptor_id=_sha(item["raw_descriptor_id"], "raw_descriptor_id"),
        parser_version=_text(item["parser_version"], "parser_version"),
        source_owner=CorporateActionSourceOwner(
            _text(item["source_owner"], "source_owner")
        ),
        source_family=CorporateActionSourceFamily(
            _text(item["source_family"], "source_family")
        ),
        source_version=_text(item["source_version"], "source_version"),
        synthetic_fixture=_bool(item["synthetic_fixture"], "synthetic_fixture"),
    )


def _binding(value: object) -> RowIdentityBinding:
    item = _strict_dict(value, "binding", _BINDING_FIELDS)
    return RowIdentityBinding(
        row_id=_sha(item["row_id"], "row_id"),
        status=IdentityBindingStatus(_text(item["status"], "status")),
        mapping_id=(
            None
            if item["mapping_id"] is None
            else _sha(item["mapping_id"], "mapping_id")
        ),
        identity_fact_id=(
            None
            if item["identity_fact_id"] is None
            else _sha(item["identity_fact_id"], "identity_fact_id")
        ),
        instrument_id=(
            None
            if item["instrument_id"] is None
            else _text(item["instrument_id"], "instrument_id")
        ),
        reason=_text(item["reason"], "reason"),
    )


def _bundle(value: object) -> BoundCorporateActionCandidateBundle:
    item = _strict_dict(value, "bundle", _BUNDLE_FIELDS)
    return BoundCorporateActionCandidateBundle(
        document_id=_sha(item["document_id"], "document_id"),
        extraction_descriptor_id=_sha(
            item["extraction_descriptor_id"],
            "extraction_descriptor_id",
        ),
        raw_artifact_id=_sha(item["raw_artifact_id"], "raw_artifact_id"),
        raw_descriptor_id=_sha(item["raw_descriptor_id"], "raw_descriptor_id"),
        mapping_policy_version=_text(
            item["mapping_policy_version"],
            "mapping_policy_version",
        ),
        as_of=_datetime(item["as_of"], "bundle.as_of"),
        bindings=tuple(
            _binding(entry) for entry in _list(item["bindings"], "bindings")
        ),
        candidates=tuple(
            _candidate(entry)
            for entry in _list(item["candidates"], "candidates")
        ),
        synthetic_fixture=_bool(
            item["synthetic_fixture"],
            "bundle.synthetic_fixture",
        ),
    )


def _mapping(value: object) -> CandidateActionMapping:
    item = _strict_dict(value, "action mapping", _MAPPING_FIELDS)
    return CandidateActionMapping(
        candidate_id=_sha(item["candidate_id"], "candidate_id"),
        logical_action_id=_text(item["logical_action_id"], "logical_action_id"),
        mapping_policy_version=_text(
            item["mapping_policy_version"],
            "mapping_policy_version",
        ),
        mapping_note=_text(item["mapping_note"], "mapping_note"),
    )


def _claim(value: object) -> CoverageClaimCandidate:
    item = _strict_dict(value, "coverage claim", _CLAIM_FIELDS)
    surveyed = tuple(
        _text(entry, "surveyed_source_event_id")
        for entry in _list(
            item["surveyed_source_event_ids"],
            "surveyed_source_event_ids",
        )
    )
    start = _date(item["start_date"], "start_date")
    end = _date(item["end_date"], "end_date")
    if start is None or end is None:
        raise CorporateActionReconciliationError(
            "coverage dates cannot be null"
        )
    return CoverageClaimCandidate(
        instrument_id=_text(item["instrument_id"], "instrument_id"),
        source_owner=CorporateActionSourceOwner(
            _text(item["source_owner"], "source_owner")
        ),
        source_version=_text(item["source_version"], "source_version"),
        start_date=start,
        end_date=end,
        known_at=_datetime(item["known_at"], "known_at"),
        usable_from=_datetime(item["usable_from"], "usable_from"),
        surveyed_source_event_ids=surveyed,
        coverage_note=_text(item["coverage_note"], "coverage_note"),
        license_status=LicenseStatus(
            _text(item["license_status"], "license_status")
        ),
        synthetic_fixture=_bool(
            item["synthetic_fixture"],
            "coverage.synthetic_fixture",
        ),
    )


def _policy(value: object) -> ReconciliationPolicy:
    item = _strict_dict(value, "policy", _POLICY_FIELDS)
    minimum = item["minimum_independent_sources"]
    if isinstance(minimum, bool) or not isinstance(minimum, int):
        raise CorporateActionReconciliationError(
            "minimum_independent_sources must be integer"
        )
    owners = tuple(
        CorporateActionSourceOwner(_text(entry, "required_primary_owner"))
        for entry in _list(
            item["required_primary_owners"],
            "required_primary_owners",
        )
    )
    return ReconciliationPolicy(
        policy_version=_text(item["policy_version"], "policy_version"),
        required_primary_owners=owners,
        minimum_independent_sources=minimum,
        require_reference_price_evidence=_bool(
            item["require_reference_price_evidence"],
            "require_reference_price_evidence",
        ),
        require_license_clearance=_bool(
            item["require_license_clearance"],
            "require_license_clearance",
        ),
        require_attachment_evidence=_bool(
            item["require_attachment_evidence"],
            "require_attachment_evidence",
        ),
        allow_synthetic_eligibility_test=_bool(
            item["allow_synthetic_eligibility_test"],
            "allow_synthetic_eligibility_test",
        ),
        synthetic_fixture=_bool(
            item["synthetic_fixture"],
            "policy.synthetic_fixture",
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline Stage 2E reconciliation of synthetic bound-candidate "
            "bundles. Writes a report only; no network, database, trust, or "
            "promotion mutation is available."
        )
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        input_path = Path(args.input).expanduser().resolve(strict=True)
        output_path = Path(args.output).expanduser().resolve(strict=False)
        if not input_path.is_file() or input_path.is_symlink():
            raise CorporateActionReconciliationError(
                "--input must be a regular non-symlink file"
            )
        if input_path == output_path:
            raise CorporateActionReconciliationError(
                "--output cannot overwrite the input"
            )
        try:
            request = _strict_dict(
                _strict_json(input_path.read_text(encoding="utf-8")),
                "request",
                _TOP_FIELDS,
            )
        except (OSError, UnicodeError) as exc:
            raise CorporateActionReconciliationError(
                "input request is unreadable JSON"
            ) from exc
        if request["schema"] != _SCHEMA:
            raise CorporateActionReconciliationError(
                "unsupported reconciliation request schema"
            )
        if _bool(request["synthetic_fixture"], "synthetic_fixture") is not True:
            raise CorporateActionReconciliationError(
                "Stage 2E CLI accepts synthetic fixtures only"
            )
        report = reconcile_corporate_actions(
            bundles=tuple(
                _bundle(entry) for entry in _list(request["bundles"], "bundles")
            ),
            action_mappings=tuple(
                _mapping(entry)
                for entry in _list(
                    request["action_mappings"],
                    "action_mappings",
                )
            ),
            coverage_claims=tuple(
                _claim(entry)
                for entry in _list(
                    request["coverage_claims"],
                    "coverage_claims",
                )
            ),
            policy=_policy(request["policy"]),
            as_of=_datetime(request["as_of"], "as_of"),
        )
        payload = {
            "schema": "stage2e-corporate-action-reconciliation-result-v1",
            "report_id": report.report_id,
            "eligibility_status": report.eligibility.status.value,
            "eligibility_reasons": list(report.eligibility.reasons),
            "conflict_ids": [item.conflict_id for item in report.conflicts],
            "global_gaps": list(report.global_gaps),
            "logical_action_ids": [
                item.action_id for item in report.logical_actions
            ],
            "evidence_boundary": "LICENSE_PENDING / T3_NOT_REACHED",
        }
        encoded = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        ).encode("utf-8")
        _write_immutable(output_path, encoded)
        print(encoded.decode("utf-8"), end="")
        return 0
    except (CorporateActionReconciliationError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
