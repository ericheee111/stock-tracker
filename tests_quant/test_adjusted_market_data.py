from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from _helpers import make_bar, utc_datetime

from stock_tracker.core.types import Market
from stock_tracker.quant.core.corporate_actions import (
    AdjustmentBasis,
    AdjustmentConvention,
    CorporateActionBook,
    CorporateActionContractError,
    CorporateActionCoverage,
    CorporateActionFact,
    CorporateActionLifecycle,
    build_adjustment_series,
)
from stock_tracker.quant.core.universe import (
    InstrumentIdentityFact,
    SecurityType,
)
from stock_tracker.quant.data.adjusted_market_data import (
    AdjustedMarketDataError,
    AdjustedMarketDataPolicy,
    CalendarMaterializationSnapshot,
    RawBarSnapshot,
    SessionGapPolicy,
    load_adjusted_market_data_artifact,
    materialize_adjusted_market_data,
    write_adjusted_market_data_dataset,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "materialize_adjusted_market_data.py"
CLI_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "adjusted_market_data"
    / "valid_request.json"
)
INSTRUMENT_ID = "CN:SSE:stage2f-fixture-security"
SYMBOL = "600000.SH"
SOURCE = "stage2f-fixture-corporate-actions"
VERSION = "stage2f-fixture-v1"
START = date(2025, 1, 10)
END = date(2025, 1, 20)
AS_OF = utc_datetime(2025, 2, 1)
IDENTITY_KNOWN_AT = utc_datetime(2024, 12, 1)
OPEN_SESSIONS = (
    date(2025, 1, 10),
    date(2025, 1, 15),
    date(2025, 1, 16),
    date(2025, 1, 20),
)


class AdjustedFixtures(unittest.TestCase):
    def identity(
        self,
        *,
        instrument_id: str = INSTRUMENT_ID,
        symbol: str = SYMBOL,
        market: Market = Market.A,
        verified: bool = True,
        known_at=IDENTITY_KNOWN_AT,
        effective_from: date = date(2020, 1, 1),
        effective_to: date | None = None,
    ) -> InstrumentIdentityFact:
        return InstrumentIdentityFact(
            instrument_id=instrument_id,
            symbol=symbol,
            market=market,
            exchange="SSE",
            security_type=SecurityType.COMMON_EQUITY,
            effective_from=effective_from,
            effective_to=effective_to,
            known_at=known_at,
            usable_from=known_at,
            source="stage2f-fixture-identity",
            revision="identity-r1",
            verified=verified,
            source_note="synthetic Stage 2F identity" if verified else "",
        )

    def coverage(
        self,
        *,
        verified: bool = True,
        complete: bool = True,
    ) -> CorporateActionCoverage:
        return CorporateActionCoverage(
            instrument_id=INSTRUMENT_ID,
            market=Market.A,
            start_date=START,
            end_date=END,
            source=SOURCE,
            action_version=VERSION,
            known_at=utc_datetime(2025, 1, 31),
            usable_from=utc_datetime(2025, 1, 31),
            revision="coverage-r1",
            supersedes_revision=None,
            verified=verified,
            complete=complete,
            source_note=(
                "synthetic complete Stage 2F coverage"
                if verified or complete
                else ""
            ),
        )

    def action(
        self,
        identity: InstrumentIdentityFact,
        *,
        automatic_share_ratio: Decimal = Decimal(2),
        cash: Decimal = Decimal(0),
        rights: Decimal = Decimal(0),
        rights_price: Decimal | None = None,
        reference_price: Decimal | None = None,
        reference_snapshot_id: str | None = None,
        currency: str | None = None,
        share_listing_date: date = date(2025, 1, 20),
        lifecycle: CorporateActionLifecycle = CorporateActionLifecycle.EFFECTIVE,
        verified: bool = True,
    ) -> CorporateActionFact:
        return CorporateActionFact(
            action_id="stage2f-action-1",
            instrument_id=identity.instrument_id,
            identity_fact_id=identity.fact_id,
            symbol=identity.symbol,
            market=identity.market,
            ex_date=date(2025, 1, 15),
            record_date=date(2025, 1, 14),
            payment_date=date(2025, 1, 20),
            share_listing_date=share_listing_date,
            lifecycle=lifecycle,
            automatic_share_ratio=automatic_share_ratio,
            cash_dividend_per_share=cash,
            rights_entitlement_ratio=rights,
            rights_subscription_price=rights_price,
            currency=currency,
            reference_price=reference_price,
            reference_price_snapshot_id=reference_snapshot_id,
            known_at=utc_datetime(2025, 1, 14),
            usable_from=utc_datetime(2025, 1, 15),
            source=SOURCE,
            action_version=VERSION,
            revision="action-r1",
            supersedes_revision=None,
            verified=verified,
            source_note="synthetic Stage 2F action" if verified else "",
        )

    def series(
        self,
        *,
        identity: InstrumentIdentityFact | None = None,
        action: CorporateActionFact | None = None,
        basis: AdjustmentBasis = AdjustmentBasis.SHARE_CHANGE_ONLY,
        convention: AdjustmentConvention = AdjustmentConvention.BACKWARD,
    ):
        identity = identity or self.identity()
        action = action or self.action(identity)
        snapshot = CorporateActionBook(
            (self.coverage(),),
            (action,),
            (identity,),
        ).snapshot(
            identity.instrument_id,
            identity.market,
            START,
            END,
            AS_OF,
        )
        return build_adjustment_series(
            snapshot,
            basis=basis,
            convention=convention,
        )

    def bars(self, *, omit: date | None = None):
        return tuple(
            make_bar(
                session,
                symbol=SYMBOL,
                market=Market.A,
                open_price=10.0,
                high=11.0,
                low=9.0,
                close=10.0,
                volume=1000,
                source="stage2f-raw-fixture",
            )
            for session in OPEN_SESSIONS
            if session != omit
        )

    def raw_snapshot(
        self,
        identity: InstrumentIdentityFact,
        *,
        bars=None,
        as_of=AS_OF,
        raw_artifact_id: str = "a" * 64,
        instrument_id: str | None = None,
        identity_fact_id: str | None = None,
        symbol: str | None = None,
        market: Market | None = None,
    ) -> RawBarSnapshot:
        return RawBarSnapshot(
            raw_artifact_id=raw_artifact_id,
            instrument_id=instrument_id or identity.instrument_id,
            identity_fact_id=identity_fact_id or identity.fact_id,
            symbol=symbol or identity.symbol,
            market=market or identity.market,
            start_date=START,
            end_date=END,
            as_of=as_of,
            bars=bars if bars is not None else self.bars(),
            source_note="synthetic exact raw-bar snapshot",
        )

    def calendar(
        self,
        *,
        open_sessions=OPEN_SESSIONS,
        verified: bool = True,
        complete: bool = True,
        as_of=AS_OF,
    ) -> CalendarMaterializationSnapshot:
        return CalendarMaterializationSnapshot(
            market=Market.A,
            start_date=START,
            end_date=END,
            as_of=as_of,
            open_sessions=open_sessions,
            verified=verified,
            complete=complete,
            source_note=(
                "synthetic verified complete Calendar"
                if verified or complete
                else ""
            ),
        )

    @staticmethod
    def policy(
        gap_policy: SessionGapPolicy = (
            SessionGapPolicy.REQUIRE_ALL_OPEN_SESSIONS
        ),
        *,
        version: str = "stage2f-policy-v1",
    ) -> AdjustedMarketDataPolicy:
        return AdjustedMarketDataPolicy(
            policy_version=version,
            session_gap_policy=gap_policy,
        )

    def dataset(self):
        identity = self.identity()
        series = self.series(identity=identity)
        raw_snapshot = self.raw_snapshot(identity)
        calendar = self.calendar()
        dataset = materialize_adjusted_market_data(
            raw_snapshot=raw_snapshot,
            calendar_snapshot=calendar,
            identity=identity,
            series=series,
            policy=self.policy(),
        )
        return identity, series, raw_snapshot, calendar, dataset


class TestAdjustedMaterialization(AdjustedFixtures):
    def test_raw_bars_are_unchanged_and_adjusted_rows_are_separate(self) -> None:
        identity = self.identity()
        bars = self.bars()
        before = tuple(
            (
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.amount,
                bar.turnover,
                bar.adjustment_factor,
            )
            for bar in bars
        )
        raw_snapshot = self.raw_snapshot(identity, bars=bars)
        dataset = materialize_adjusted_market_data(
            raw_snapshot=raw_snapshot,
            calendar_snapshot=self.calendar(),
            identity=identity,
            series=self.series(identity=identity),
            policy=self.policy(),
        )
        after = tuple(
            (
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.amount,
                bar.turnover,
                bar.adjustment_factor,
            )
            for bar in bars
        )
        self.assertEqual(before, after)
        self.assertEqual(dataset.rows[0].raw_close, Decimal(10))
        self.assertEqual(dataset.rows[0].adjusted_close, Decimal(5))
        self.assertEqual(dataset.rows[-1].adjusted_close, Decimal(10))
        self.assertIn("PRESERVED_RAW", dataset.rows[0].raw_fields_status)
        self.assertFalse(hasattr(dataset, "performance"))
        self.assertFalse(hasattr(dataset, "model_ready"))

    def test_price_ex_date_and_share_listing_date_remain_distinct(self) -> None:
        _, _, _, _, dataset = self.dataset()
        by_date = {row.session_date: row for row in dataset.rows}
        self.assertEqual(
            by_date[date(2025, 1, 10)].price_multiplier,
            Decimal("0.5"),
        )
        self.assertEqual(
            by_date[date(2025, 1, 15)].price_multiplier,
            Decimal(1),
        )
        self.assertEqual(
            by_date[date(2025, 1, 16)].automatic_share_multiplier,
            Decimal(2),
        )
        self.assertEqual(
            by_date[date(2025, 1, 20)].automatic_share_multiplier,
            Decimal(1),
        )

    def test_rights_entitlement_never_becomes_automatic_shares(self) -> None:
        identity = self.identity()
        action = self.action(
            identity,
            automatic_share_ratio=Decimal(1),
            rights=Decimal("0.2"),
            rights_price=Decimal(5),
            reference_price=Decimal(10),
            reference_snapshot_id="b" * 64,
            currency="CNY",
            share_listing_date=date(2025, 1, 15),
        )
        series = self.series(
            identity=identity,
            action=action,
            basis=AdjustmentBasis.TOTAL_RETURN,
        )
        dataset = materialize_adjusted_market_data(
            raw_snapshot=self.raw_snapshot(identity),
            calendar_snapshot=self.calendar(),
            identity=identity,
            series=series,
            policy=self.policy(),
        )
        self.assertTrue(
            all(
                row.automatic_share_multiplier == Decimal(1)
                for row in dataset.rows
            )
        )

    def test_dataset_identity_binds_every_input_and_derived_fields(self) -> None:
        identity, series, raw_snapshot, calendar, dataset = self.dataset()
        raw_changed = self.raw_snapshot(
            identity,
            raw_artifact_id="c" * 64,
        )
        changed_raw_dataset = materialize_adjusted_market_data(
            raw_snapshot=raw_changed,
            calendar_snapshot=calendar,
            identity=identity,
            series=series,
            policy=self.policy(),
        )
        changed_policy_dataset = materialize_adjusted_market_data(
            raw_snapshot=raw_snapshot,
            calendar_snapshot=calendar,
            identity=identity,
            series=series,
            policy=self.policy(version="stage2f-policy-v2"),
        )
        self.assertNotEqual(dataset.dataset_id, changed_raw_dataset.dataset_id)
        self.assertNotEqual(dataset.dataset_id, changed_policy_dataset.dataset_id)
        for field_name, value in (
            ("rows", ()),
            ("gaps", ()),
            ("dataset_id", "d" * 64),
            ("raw_bar_snapshot_id", "e" * 64),
            ("adjustment_series_id", "f" * 64),
        ):
            with self.subTest(field=field_name), self.assertRaisesRegex(
                TypeError,
                "init=False",
            ):
                replace(dataset, **{field_name: value})

    def test_identity_symbol_market_and_series_mismatch_fail_closed(self) -> None:
        identity = self.identity()
        series = self.series(identity=identity)
        cases = (
            {"instrument_id": "CN:SSE:wrong"},
            {"identity_fact_id": "f" * 64},
            {"symbol": "600001.SH"},
        )
        for changes in cases:
            with (
                self.subTest(changes=changes),
                self.assertRaises(AdjustedMarketDataError),
            ):
                raw_snapshot = self.raw_snapshot(identity, **changes)
                materialize_adjusted_market_data(
                    raw_snapshot=raw_snapshot,
                    calendar_snapshot=self.calendar(),
                    identity=identity,
                    series=series,
                    policy=self.policy(),
                )
        other_identity = self.identity(
            instrument_id="CN:SSE:other",
            symbol="600001.SH",
        )
        with self.assertRaises(AdjustedMarketDataError):
            materialize_adjusted_market_data(
                raw_snapshot=self.raw_snapshot(identity),
                calendar_snapshot=self.calendar(),
                identity=other_identity,
                series=series,
                policy=self.policy(),
            )

    def test_out_of_order_duplicate_closed_and_missing_sessions_fail_closed(self) -> None:
        identity = self.identity()
        bars = self.bars()
        for invalid in (
            tuple(reversed(bars)),
            bars + (bars[-1],),
        ):
            with self.assertRaises(AdjustedMarketDataError):
                self.raw_snapshot(identity, bars=invalid)

        closed_calendar = self.calendar(open_sessions=OPEN_SESSIONS[:-1])
        with self.assertRaisesRegex(AdjustedMarketDataError, "closed"):
            materialize_adjusted_market_data(
                raw_snapshot=self.raw_snapshot(identity),
                calendar_snapshot=closed_calendar,
                identity=identity,
                series=self.series(identity=identity),
                policy=self.policy(),
            )

        missing_date = date(2025, 1, 16)
        missing_raw = self.raw_snapshot(
            identity,
            bars=self.bars(omit=missing_date),
        )
        with self.assertRaisesRegex(AdjustedMarketDataError, "missing"):
            materialize_adjusted_market_data(
                raw_snapshot=missing_raw,
                calendar_snapshot=self.calendar(),
                identity=identity,
                series=self.series(identity=identity),
                policy=self.policy(),
            )
        allowed = materialize_adjusted_market_data(
            raw_snapshot=missing_raw,
            calendar_snapshot=self.calendar(),
            identity=identity,
            series=self.series(identity=identity),
            policy=self.policy(SessionGapPolicy.ALLOW_EXPLICIT_GAPS),
            explicit_gap_sessions=(missing_date,),
        )
        self.assertEqual(
            allowed.gaps,
            ("MISSING_OPEN_SESSION:2025-01-16",),
        )

    def test_unverified_incomplete_and_future_inputs_fail_closed(self) -> None:
        identity = self.identity()
        series = self.series(identity=identity)
        for calendar in (
            self.calendar(verified=False, complete=True),
            self.calendar(verified=True, complete=False),
        ):
            with self.assertRaisesRegex(AdjustedMarketDataError, "verified and complete"):
                materialize_adjusted_market_data(
                    raw_snapshot=self.raw_snapshot(identity),
                    calendar_snapshot=calendar,
                    identity=identity,
                    series=series,
                    policy=self.policy(),
                )
        unverified = self.identity(verified=False)
        with self.assertRaisesRegex(
            CorporateActionContractError,
            "unverified identity",
        ):
            self.series(identity=unverified)
        with self.assertRaisesRegex(AdjustedMarketDataError, "future"):
            materialize_adjusted_market_data(
                raw_snapshot=self.raw_snapshot(
                    identity,
                    as_of=utc_datetime(2025, 3, 1),
                ),
                calendar_snapshot=self.calendar(),
                identity=identity,
                series=series,
                policy=self.policy(),
            )
        with self.assertRaisesRegex(CorporateActionContractError, "verified and complete"):
            debug_snapshot = CorporateActionBook(
                (self.coverage(verified=False, complete=False),),
                (),
                (),
            ).snapshot(
                INSTRUMENT_ID,
                Market.A,
                START,
                END,
                AS_OF,
                require_verified=False,
                require_complete=False,
            )
            build_adjustment_series(
                debug_snapshot,
                basis=AdjustmentBasis.TOTAL_RETURN,
                convention=AdjustmentConvention.BACKWARD,
            )


class TestAdjustedArtifact(AdjustedFixtures):
    def test_write_load_round_trip_and_tamper_detection(self) -> None:
        _, _, _, _, dataset = self.dataset()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = write_adjusted_market_data_dataset(root, dataset=dataset)
            loaded, rows = load_adjusted_market_data_artifact(
                root,
                descriptor_key=artifact.descriptor_key,
            )
            self.assertEqual(artifact, loaded)
            self.assertEqual(rows, dataset.rows)

            data_path = root / artifact.data_key
            tampered = bytearray(data_path.read_bytes())
            tampered[0] ^= 1
            data_path.write_bytes(bytes(tampered))
            with self.assertRaisesRegex(AdjustedMarketDataError, "hash changed"):
                load_adjusted_market_data_artifact(
                    root,
                    descriptor_key=artifact.descriptor_key,
                )

    def test_descriptor_row_and_immutable_overwrite_tamper_fail(self) -> None:
        _, _, _, _, dataset = self.dataset()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = write_adjusted_market_data_dataset(root, dataset=dataset)
            descriptor_path = root / artifact.descriptor_key
            original_descriptor = descriptor_path.read_bytes()
            value = json.loads(original_descriptor.decode("utf-8"))
            value["policy_id"] = "f" * 64
            descriptor_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                AdjustedMarketDataError,
                "dataset_id",
            ):
                load_adjusted_market_data_artifact(
                    root,
                    descriptor_key=artifact.descriptor_key,
                )
            descriptor_path.write_bytes(original_descriptor)
            data_path = root / artifact.data_key
            original = data_path.read_bytes()
            data_path.write_bytes(original + b"x")
            with self.assertRaises(AdjustedMarketDataError):
                write_adjusted_market_data_dataset(root, dataset=dataset)

    def test_loader_rejects_row_id_tamper_even_with_recomputed_data_hash(self) -> None:
        _, _, _, _, dataset = self.dataset()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = write_adjusted_market_data_dataset(root, dataset=dataset)
            data_path = root / artifact.data_key
            lines = data_path.read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0])
            first["row_id"] = "f" * 64
            lines[0] = json.dumps(first, sort_keys=True)
            changed = ("\n".join(lines) + "\n").encode("utf-8")
            import hashlib

            new_hash = hashlib.sha256(changed).hexdigest()
            new_key = f"adjusted-market-data/{new_hash}.jsonl"
            (root / new_key).parent.mkdir(parents=True, exist_ok=True)
            (root / new_key).write_bytes(changed)
            descriptor_path = root / artifact.descriptor_key
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
            descriptor["data_sha256"] = new_hash
            descriptor["data_key"] = new_key
            descriptor["byte_length"] = len(changed)
            descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
            with self.assertRaisesRegex(AdjustedMarketDataError, "row_id"):
                load_adjusted_market_data_artifact(
                    root,
                    descriptor_key=artifact.descriptor_key,
                )


class TestAdjustedCli(AdjustedFixtures):
    def test_cli_has_no_database_model_or_trust_switches(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lowered = result.stdout.lower()
        for forbidden in (
            "--database",
            "--apply",
            "--train",
            "--backtest",
            "--verified",
            "--trust",
            "--promote",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_cli_materializes_synthetic_fixture_and_rejects_unknown_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--input",
                    str(CLI_FIXTURE),
                    "--output-root",
                    str(root),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["row_count"], 4)
            self.assertIn("T3_NOT_REACHED", payload["evidence_boundary"])
            self.assertTrue((root / payload["descriptor_key"]).is_file())
            self.assertTrue((root / payload["data_key"]).is_file())

        request = json.loads(CLI_FIXTURE.read_text(encoding="utf-8"))
        request["identity"]["trust_tier"] = "T3"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "request.json"
            output_root = root / "out"
            input_path.write_text(json.dumps(request), encoding="utf-8")
            blocked = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--input",
                    str(input_path),
                    "--output-root",
                    str(output_root),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("unknown", blocked.stderr)


if __name__ == "__main__":
    unittest.main()
