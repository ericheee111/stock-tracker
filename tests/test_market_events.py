from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sidecars.xtp.contracts import EventEnvelope
from stock_tracker.market_events import (
    EventDisposition,
    GapKind,
    MarketEventStore,
    MarketEventStoreError,
    MinuteCompleteness,
)
from stock_tracker.market_events.replay import MarketEventReplay

ROOT = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


class TestMarketEventStore(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = MarketEventStore(
            root / "events",
            root / "catalog.sqlite3",
            quarantine_root=root / "quarantine",
        )
        self.base_time = datetime(2026, 8, 27, 1, 30, 5, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _event(
        self,
        *,
        seq: int,
        symbol: str = "600519.SH",
        provider_seq: int | None = None,
        seconds: int = 0,
        session_id: str = "session-a",
        feed_mode: str = "SIMULATOR",
        last: float = 10.0,
        volume: int = 1000,
        amount: float = 10000.0,
    ) -> EventEnvelope:
        current = self.base_time + timedelta(seconds=seconds)
        return EventEnvelope.create(
            feed_mode=feed_mode,
            symbol=symbol,
            event_type="MARKET_DATA",
            trading_day="2026-08-27",
            exchange_timestamp=current - timedelta(milliseconds=5),
            provider_timestamp=current - timedelta(milliseconds=4),
            received_at=current,
            session_id=session_id,
            callback_seq=seq,
            provider_seq=provider_seq,
            payload={
                "last": last,
                "open": last,
                "high": last,
                "low": last,
                "prev_close": 9.9,
                "volume": volume,
                "amount": amount,
                "simulator": True,
            },
        )

    def test_global_callback_sequence_does_not_create_cross_symbol_false_gap(self) -> None:
        first = self.store.append(
            self._event(seq=1, symbol="600519.SH", provider_seq=1)
        )
        second = self.store.append(
            self._event(seq=2, symbol="000001.SZ", provider_seq=1, seconds=1)
        )
        third = self.store.append(
            self._event(seq=3, symbol="600519.SH", provider_seq=2, seconds=2)
        )
        self.assertEqual(first.disposition, EventDisposition.ACCEPTED)
        self.assertEqual(second.findings, ())
        self.assertEqual(third.findings, ())
        self.assertEqual(self.store.status()["finding_count"], 0)

    def test_session_feed_mode_identity_cannot_change_mid_session(self) -> None:
        self.store.append(self._event(seq=1, feed_mode="SIMULATOR"))
        with self.assertRaisesRegex(MarketEventStoreError, "session identity"):
            self.store.append(
                self._event(seq=2, seconds=1, feed_mode="LEVEL1")
            )
        status = self.store.status()
        self.assertEqual(status["event_count"], 1)
        self.assertTrue(self.store.verify_integrity()["passed"])

    def test_callback_provider_and_source_time_findings_are_explicit(self) -> None:
        self.store.append(self._event(seq=1, provider_seq=10))
        gap = self.store.append(
            self._event(seq=3, provider_seq=12, seconds=2)
        )
        kinds = {finding.kind for finding in gap.findings}
        self.assertIn(GapKind.CALLBACK_SEQUENCE, kinds)
        self.assertIn(GapKind.PROVIDER_SEQUENCE, kinds)

        regression = self.store.append(
            self._event(seq=4, provider_seq=13, seconds=-1)
        )
        regression_kinds = {finding.kind for finding in regression.findings}
        self.assertIn(GapKind.SOURCE_TIME_REGRESSION, regression_kinds)

    def test_immutable_files_manifest_and_hash_chain_are_tamper_evident(self) -> None:
        first = self.store.append(self._event(seq=1, provider_seq=1))
        second = self.store.append(
            self._event(
                seq=2,
                provider_seq=2,
                seconds=10,
                last=10.2,
                volume=1100,
                amount=11200.0,
            )
        )
        self.assertEqual(first.disposition, EventDisposition.ACCEPTED)
        self.assertEqual(second.disposition, EventDisposition.ACCEPTED)
        integrity = self.store.verify_integrity()
        self.assertTrue(integrity["passed"])
        self.assertEqual(integrity["checked_event_count"], 2)
        self.assertEqual(integrity["checked_partition_count"], 1)

        events = self.store.list_events(
            "600519.SH",
            start_at=self.base_time - timedelta(minutes=1),
            end_at=self.base_time + timedelta(minutes=1),
        )
        self.assertEqual(events[0]["previous_record_hash"], "0" * 64)
        self.assertEqual(events[1]["previous_record_hash"], events[0]["record_hash"])
        self.assertNotIn("payload_json", events[0])

        event_files = sorted((Path(self.temp.name) / "events").rglob("event-*.json"))
        self.assertEqual(len(event_files), 2)
        event_files[0].write_text("{}\n", encoding="utf-8")
        failed = self.store.verify_integrity()
        self.assertFalse(failed["passed"])
        with self.assertRaises(MarketEventStoreError):
            self.store.append(self._event(seq=1, provider_seq=1))

    def test_out_of_order_callback_preserves_append_order_hash_chain(self) -> None:
        self.store.append(self._event(seq=1, provider_seq=1, seconds=1))
        self.store.append(self._event(seq=3, provider_seq=3, seconds=3))
        out_of_order = self.store.append(
            self._event(seq=2, provider_seq=2, seconds=2)
        )
        self.assertIn(
            GapKind.OUT_OF_ORDER,
            {finding.kind for finding in out_of_order.findings},
        )
        integrity = self.store.verify_integrity()
        self.assertTrue(integrity["passed"], integrity["errors"])
        manifests = list((Path(self.temp.name) / "events").rglob("manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(
            [item["callback_seq"] for item in manifest["events"]],
            [1, 3, 2],
        )
        self.assertEqual(
            [item["append_order"] for item in manifest["events"]],
            [1, 2, 3],
        )
        for index in range(1, len(manifest["events"])):
            previous = manifest["events"][index - 1]
            current = manifest["events"][index]
            self.assertEqual(
                current["previous_record_hash"], previous["record_hash"]
            )

    def test_v2_catalog_migrates_append_order_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "legacy.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    CREATE TABLE event_store_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT INTO event_store_meta(key,value)
                    VALUES('schema','stock-tracker-market-event-store-v2');
                    CREATE TABLE sessions (
                        session_id TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        feed_mode TEXT NOT NULL,
                        first_received_at TEXT NOT NULL,
                        last_received_at TEXT NOT NULL,
                        event_count INTEGER NOT NULL DEFAULT 0,
                        last_callback_seq INTEGER NOT NULL DEFAULT 0
                    );
                    INSERT INTO sessions VALUES(
                        'legacy-session','xtp','SIMULATOR',
                        '2026-08-27T00:00:00+00:00','2026-08-27T00:00:00+00:00',1,1
                    );
                    CREATE TABLE events (
                        event_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(session_id),
                        symbol TEXT NOT NULL,
                        market TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        trading_day TEXT NOT NULL,
                        source_time TEXT NOT NULL,
                        received_at TEXT NOT NULL,
                        callback_seq INTEGER NOT NULL,
                        provider_seq INTEGER,
                        partition_key TEXT NOT NULL,
                        event_file TEXT NOT NULL UNIQUE,
                        event_file_sha256 TEXT NOT NULL,
                        previous_record_hash TEXT NOT NULL,
                        record_hash TEXT NOT NULL UNIQUE,
                        payload_json TEXT NOT NULL,
                        raw_payload_sha256 TEXT NOT NULL,
                        ingested_at TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "legacy-event",
                        "legacy-session",
                        "600519.SH",
                        "A",
                        "MARKET_DATA",
                        "2026-08-27",
                        "2026-08-27T00:00:00+00:00",
                        "2026-08-27T00:00:00+00:00",
                        1,
                        1,
                        "market=A/trading_day=2026-08-27/symbol=600519.SH",
                        "legacy.json",
                        "a" * 64,
                        "0" * 64,
                        "b" * 64,
                        "{}",
                        "c" * 64,
                        "2026-08-27T00:00:00+00:00",
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            MarketEventStore(root / "events", database)
            connection = sqlite3.connect(database)
            try:
                schema = connection.execute(
                    "SELECT value FROM event_store_meta WHERE key='schema'"
                ).fetchone()[0]
                append_order = connection.execute(
                    "SELECT append_order FROM events WHERE event_id='legacy-event'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(schema, "stock-tracker-market-event-store-v3")
            self.assertEqual(append_order, 1)

    def test_integrity_can_verify_only_affected_partitions(self) -> None:
        first = self.store.append(
            self._event(seq=1, symbol="600519.SH", provider_seq=1)
        )
        self.store.append(
            self._event(
                seq=2,
                symbol="000001.SZ",
                provider_seq=1,
                seconds=1,
            )
        )
        full = self.store.verify_integrity()
        targeted = self.store.verify_integrity(
            partition_keys=(first.partition_key,)
        )
        self.assertEqual(full["scope"], "FULL")
        self.assertEqual(full["checked_partition_count"], 2)
        self.assertEqual(targeted["scope"], "PARTITIONS")
        self.assertEqual(targeted["requested_partition_count"], 1)
        self.assertEqual(targeted["checked_partition_count"], 1)
        self.assertEqual(targeted["checked_event_count"], 1)
        self.assertTrue(targeted["passed"])

    def test_full_integrity_detects_missing_partition_metadata(self) -> None:
        accepted = self.store.append(self._event(seq=1, provider_seq=1))
        connection = sqlite3.connect(self.store.metadata_db)
        try:
            connection.execute(
                "DELETE FROM partitions WHERE partition_key=?",
                (accepted.partition_key,),
            )
            connection.commit()
        finally:
            connection.close()
        integrity = self.store.verify_integrity()
        self.assertFalse(integrity["passed"])
        self.assertTrue(
            any("MISSING_METADATA" in error for error in integrity["errors"]),
            integrity["errors"],
        )

    def test_duplicate_is_idempotent_only_while_immutable_file_is_intact(self) -> None:
        event = self._event(seq=1, provider_seq=1)
        accepted = self.store.append(event)
        duplicate = self.store.append(event)
        self.assertEqual(accepted.disposition, EventDisposition.ACCEPTED)
        self.assertEqual(duplicate.disposition, EventDisposition.DUPLICATE)
        self.assertEqual(self.store.status()["event_count"], 1)

    def test_minute_rebuild_failure_rolls_back_raw_event_and_manifest(self) -> None:
        event = self._event(seq=1, provider_seq=1)
        with (
            mock.patch.object(
                self.store,
                "_refresh_minute_with_connection",
                side_effect=MarketEventStoreError("fixture minute rebuild failure"),
            ),
            self.assertRaisesRegex(MarketEventStoreError, "fixture minute rebuild failure"),
        ):
            self.store.append(event)
        status = self.store.status()
        self.assertEqual(status["event_count"], 0)
        self.assertEqual(status["partition_count"], 0)
        self.assertEqual(status["minute_bar_count"], 0)
        self.assertEqual(list((Path(self.temp.name) / "events").rglob("event-*.json")), [])
        self.assertEqual(list((Path(self.temp.name) / "events").rglob("manifest.json")), [])

    def test_minute_bar_uses_same_session_baseline_for_cumulative_deltas(self) -> None:
        self.store.append(
            self._event(
                seq=1,
                provider_seq=1,
                seconds=-10,
                last=9.9,
                volume=1000,
                amount=10000.0,
            )
        )
        self.store.append(
            self._event(
                seq=2,
                provider_seq=2,
                last=10.0,
                volume=1000,
                amount=10000.0,
            )
        )
        self.store.append(
            self._event(
                seq=3,
                provider_seq=3,
                seconds=10,
                last=10.2,
                volume=1100,
                amount=11020.0,
            )
        )
        self.store.append(
            self._event(
                seq=4,
                provider_seq=4,
                seconds=20,
                last=10.1,
                volume=1300,
                amount=13040.0,
            )
        )
        bars = self.store.minute_bars(
            "600519.SH",
            start_at=self.base_time,
            end_at=self.base_time + timedelta(seconds=50),
        )
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertEqual(bar["open"], 10.0)
        self.assertEqual(bar["high"], 10.2)
        self.assertEqual(bar["low"], 10.0)
        self.assertEqual(bar["close"], 10.1)
        self.assertEqual(bar["volume"], 300)
        self.assertEqual(bar["amount"], 3040.0)
        self.assertEqual(bar["completeness"], MinuteCompleteness.COMPLETE.value)
        self.assertEqual(bar["data_status"], "DELAYED")

    def test_cumulative_regression_marks_minute_incomplete(self) -> None:
        self.store.append(
            self._event(
                seq=1,
                provider_seq=1,
                seconds=-10,
                volume=1000,
                amount=10000.0,
            )
        )
        self.store.append(
            self._event(
                seq=2,
                provider_seq=2,
                volume=1000,
                amount=10000.0,
            )
        )
        self.store.append(
            self._event(
                seq=3,
                provider_seq=3,
                seconds=10,
                volume=900,
                amount=9000.0,
            )
        )
        bar = self.store.minute_bars(
            "600519.SH",
            start_at=self.base_time,
            end_at=self.base_time + timedelta(seconds=50),
        )[0]
        self.assertEqual(
            bar["completeness"],
            MinuteCompleteness.INCOMPLETE_OUT_OF_ORDER.value,
        )
        self.assertEqual(bar["volume"], 0)
        self.assertEqual(bar["amount"], 0.0)

    def test_minute_without_same_session_baseline_is_explicitly_incomplete(self) -> None:
        self.store.append(
            self._event(seq=1, provider_seq=1, volume=1000, amount=10000.0)
        )
        self.store.append(
            self._event(
                seq=2,
                provider_seq=2,
                seconds=10,
                volume=1100,
                amount=11020.0,
            )
        )
        bar = self.store.minute_bars("600519.SH")[0]
        self.assertEqual(
            bar["completeness"],
            MinuteCompleteness.INCOMPLETE_BASELINE.value,
        )
        self.assertEqual(bar["volume"], 100)
        self.assertEqual(bar["amount"], 1020.0)

    def test_minute_bar_replay_is_bounded_by_requested_time_window(self) -> None:
        self.store.append(self._event(seq=1, provider_seq=1))
        self.store.append(
            self._event(
                seq=2,
                provider_seq=2,
                seconds=70,
                last=10.2,
                volume=1100,
                amount=11200.0,
            )
        )
        first_window = self.store.minute_bars(
            "600519.SH",
            start_at=self.base_time - timedelta(seconds=10),
            end_at=self.base_time + timedelta(seconds=50),
        )
        second_window = self.store.minute_bars(
            "600519.SH",
            start_at=self.base_time + timedelta(seconds=60),
            end_at=self.base_time + timedelta(seconds=120),
        )
        empty_window = self.store.minute_bars(
            "600519.SH",
            start_at=self.base_time + timedelta(hours=1),
            end_at=self.base_time + timedelta(hours=2),
        )
        self.assertEqual(len(first_window), 1)
        self.assertEqual(len(second_window), 1)
        self.assertEqual(empty_window, [])
        self.assertNotEqual(
            first_window[0]["minute_start"],
            second_window[0]["minute_start"],
        )
        with self.assertRaisesRegex(MarketEventStoreError, "provided together"):
            self.store.minute_bars(
                "600519.SH",
                start_at=self.base_time,
            )

    def test_quarantine_persists_metadata_not_malformed_raw_payload(self) -> None:
        marker = "SENSITIVE-MARKER-DO-NOT-PERSIST"
        result = self.store.ingest_dict(
            ("{\"schema\":\"bad\",\"marker\":\"" + marker + "\"}").encode()
        )
        self.assertEqual(result.disposition, EventDisposition.QUARANTINED)
        files = list((Path(self.temp.name) / "quarantine").glob("*.json"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertNotIn(marker, text)
        descriptor = json.loads(text)
        self.assertFalse(descriptor["raw_payload_persisted"])
        self.assertFalse(descriptor["contains_account_value"])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_symlink_root_is_rejected_when_platform_allows_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real = base / "real"
            real.mkdir()
            link = base / "link"
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation is not permitted")
            with self.assertRaises(MarketEventStoreError):
                MarketEventStore(link, base / "catalog.sqlite3")

    def test_replay_is_deterministic_and_records_backend_honestly(self) -> None:
        for index in range(3):
            self.store.append(
                self._event(
                    seq=index + 1,
                    provider_seq=index + 1,
                    seconds=index * 5,
                    last=10 + index * 0.1,
                    volume=1000 + index * 100,
                    amount=10000 + index * 1000,
                )
            )
        replay = MarketEventReplay(self.store)
        first = replay.run(
            "600519.SH",
            start_at=self.base_time - timedelta(seconds=1),
            end_at=self.base_time + timedelta(minutes=1),
            backend="python",
            synthetic_fixture_only=True,
        )
        second = replay.run(
            "600519.SH",
            start_at=self.base_time - timedelta(seconds=1),
            end_at=self.base_time + timedelta(minutes=1),
            backend="python",
            synthetic_fixture_only=True,
        )
        self.assertEqual(first.replay_id, second.replay_id)
        self.assertEqual(first.rows, second.rows)
        self.assertEqual(first.backend_used, "python")
        self.assertTrue(first.integrity_verified)
        self.assertEqual(first.integrity_partition_count, 1)
        self.assertTrue(first.synthetic_fixture_only)
        self.assertFalse(first.production_database_modified)

    def test_replay_fails_closed_when_an_event_partition_is_tampered(self) -> None:
        self.store.append(self._event(seq=1, provider_seq=1))
        connection = sqlite3.connect(self.store.metadata_db)
        try:
            event_file = str(
                connection.execute(
                    "SELECT event_file FROM events LIMIT 1"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        event_path = self.store.root / event_file
        event_path.write_bytes(event_path.read_bytes() + b" ")

        with self.assertRaisesRegex(MarketEventStoreError, "integrity verification failed"):
            MarketEventReplay(self.store).run(
                "600519.SH",
                start_at=self.base_time - timedelta(seconds=1),
                end_at=self.base_time + timedelta(seconds=1),
                backend="python",
                synthetic_fixture_only=True,
            )

    def test_replay_fails_closed_when_catalog_payload_disagrees_with_file(self) -> None:
        event = self._event(seq=1, provider_seq=1)
        self.store.append(event)
        connection = sqlite3.connect(self.store.metadata_db)
        try:
            connection.execute(
                "UPDATE events SET payload_json=? WHERE event_id=?",
                ('{"last":999.0}', event.event_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(MarketEventStoreError, "integrity verification failed"):
            MarketEventReplay(self.store).run(
                "600519.SH",
                start_at=self.base_time - timedelta(seconds=1),
                end_at=self.base_time + timedelta(seconds=1),
                backend="python",
                synthetic_fixture_only=True,
            )

    def test_replay_fails_closed_when_partition_metadata_is_tampered(self) -> None:
        self.store.append(self._event(seq=1, provider_seq=1))
        connection = sqlite3.connect(self.store.metadata_db)
        try:
            connection.execute("UPDATE partitions SET event_count=999")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(MarketEventStoreError, "integrity verification failed"):
            MarketEventReplay(self.store).run(
                "600519.SH",
                start_at=self.base_time - timedelta(seconds=1),
                end_at=self.base_time + timedelta(seconds=1),
                backend="python",
                synthetic_fixture_only=True,
            )

    @unittest.skipUnless(
        importlib.util.find_spec("duckdb") is not None,
        "DuckDB is not installed",
    )
    def test_duckdb_replay_handles_an_empty_window(self) -> None:
        replay = MarketEventReplay(self.store).run(
            "600519.SH",
            start_at=self.base_time,
            end_at=self.base_time + timedelta(minutes=1),
            backend="duckdb",
            synthetic_fixture_only=True,
        )
        self.assertEqual(replay.rows, ())
        self.assertEqual(replay.backend_used, "duckdb")
        self.assertTrue(replay.duckdb_available)

    def test_production_database_is_never_opened_or_modified(self) -> None:
        production = ROOT / "data" / "stock_tracker.db"
        before = _sha(production)
        self.store.append(self._event(seq=1, provider_seq=1))
        self.store.verify_integrity()
        after = _sha(production)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
