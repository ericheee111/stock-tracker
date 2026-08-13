from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from _helpers import utc_datetime

from stock_tracker.core.types import Market
from stock_tracker.quant.core.fingerprint import fingerprint
from stock_tracker.quant.data import (
    DataFormat,
    DataKind,
    DataSnapshotManifest,
    ManifestContractError,
    RawDataArtifact,
    safe_artifact_path,
    validate_storage_key,
)


def _recompute_artifact_id(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("artifact_id", None)
    payload["kind"] = DataKind(payload["kind"])
    payload["format"] = DataFormat(payload["format"])
    payload["market"] = Market(payload["market"]) if payload.get("market") else None
    for name in ("content_start", "content_end", "retrieved_at"):
        if payload.get(name) is not None:
            payload[name] = datetime.fromisoformat(payload[name])
    return fingerprint(payload)


def _recompute_snapshot_id(value: dict[str, Any]) -> str:
    return fingerprint(
        {
            "schema": "raw-data-snapshot-v1",
            "name": value["name"],
            "as_of": datetime.fromisoformat(value["as_of"]),
            "created_at": datetime.fromisoformat(value["created_at"]),
            "config_hash": value["config_hash"],
            "code_version": value["code_version"],
            "artifact_ids": sorted(
                artifact["artifact_id"] for artifact in value["artifacts"]
            ),
            "calendar_snapshot_ids": sorted(value["calendar_snapshot_ids"]),
            "universe_snapshot_id": value["universe_snapshot_id"],
            "require_verified": value["require_verified"],
            "require_calendar_for_market_data": value[
                "require_calendar_for_market_data"
            ],
            "require_universe_for_market_data": value[
                "require_universe_for_market_data"
            ],
            "notes": value["notes"],
        }
    )


class ManifestFixture(unittest.TestCase):
    def artifact(
        self,
        *,
        storage_key: str = "raw/a/bars.csv",
        sha256: str = "a" * 64,
        kind: DataKind = DataKind.MARKET_BARS,
        format: DataFormat = DataFormat.CSV,
        market: Market | None = Market.A,
        retrieved_day: int = 5,
        verified: bool = True,
        calendar_snapshot_id: str | None = None,
    ) -> RawDataArtifact:
        return RawDataArtifact(
            kind=kind,
            format=format,
            market=market,
            source="fixture-provider",
            source_dataset="fixture-dataset",
            storage_key=storage_key,
            sha256=sha256,
            byte_size=12,
            row_count=2,
            content_start=utc_datetime(2025, 1, 2),
            content_end=utc_datetime(2025, 1, 3),
            retrieved_at=utc_datetime(2025, 1, retrieved_day),
            provider_version="fixture-provider-v1",
            schema_version="fixture-schema-v1",
            adapter_version="fixture-adapter-v1",
            known_at_policy="provider-published-at",
            revision_policy="append-new-artifact",
            verified=verified,
            source_note="synthetic fixture only" if verified else "",
            calendar_snapshot_id=calendar_snapshot_id,
        )

    def snapshot(
        self,
        artifacts: tuple[RawDataArtifact, ...],
        **overrides: object,
    ) -> DataSnapshotManifest:
        values: dict[str, object] = {
            "name": "fixture-snapshot",
            "as_of": utc_datetime(2025, 1, 4),
            "created_at": utc_datetime(2025, 1, 6),
            "config_hash": fingerprint({"fixture": 1}),
            "code_version": "fixture-code-v1",
            "artifacts": artifacts,
            "calendar_snapshot_ids": ("c" * 64,),
            "universe_snapshot_id": "e" * 64,
            "require_verified": True,
            "require_calendar_for_market_data": True,
            "require_universe_for_market_data": True,
            "notes": {"fixture": True},
        }
        values.update(overrides)
        return DataSnapshotManifest(**values)


class TestStorageKeys(unittest.TestCase):
    def test_valid_key_is_unchanged(self) -> None:
        self.assertEqual(validate_storage_key("raw/a/bars.csv"), "raw/a/bars.csv")

    def test_unsafe_keys_are_rejected(self) -> None:
        unsafe = (
            "/absolute/file.csv",
            "C:/file.csv",
            "../file.csv",
            "raw/../file.csv",
            "raw\\file.csv",
            "https://example.test/file.csv",
            "raw/file.csv?token=x",
            "raw//file.csv",
            "raw/file.csv/",
        )
        for key in unsafe:
            with self.subTest(key=key), self.assertRaises(ManifestContractError):
                validate_storage_key(key)

    def test_safe_path_stays_under_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory).resolve() / "raw" / "a.csv"
            self.assertEqual(safe_artifact_path(directory, "raw/a.csv"), expected)

    def test_symlink_component_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with self.assertRaises(ManifestContractError):
                safe_artifact_path(root, "link/file.csv")


class TestRawDataArtifact(ManifestFixture):
    def test_structured_artifact_requires_row_count(self) -> None:
        with self.assertRaisesRegex(ManifestContractError, "row_count"):
            replace(self.artifact(), row_count=None)

    def test_market_artifact_requires_market(self) -> None:
        with self.assertRaisesRegex(ManifestContractError, "declare market"):
            replace(self.artifact(), market=None)

    def test_verified_artifact_requires_source_note(self) -> None:
        with self.assertRaisesRegex(ManifestContractError, "source note"):
            replace(self.artifact(), source_note="")

    def test_from_file_and_verify_detect_same_size_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "raw" / "a" / "bars.csv"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"a,b\n1,2\n")
            artifact = RawDataArtifact.from_file(
                root,
                storage_key="raw/a/bars.csv",
                kind=DataKind.MARKET_BARS,
                format=DataFormat.CSV,
                market=Market.A,
                source="fixture-provider",
                source_dataset="fixture-dataset",
                row_count=1,
                content_start=utc_datetime(2025, 1, 2),
                content_end=utc_datetime(2025, 1, 2),
                retrieved_at=utc_datetime(2025, 1, 5),
                provider_version="fixture-provider-v1",
                schema_version="fixture-schema-v1",
                adapter_version="fixture-adapter-v1",
                known_at_policy="provider-published-at",
                revision_policy="append-new-artifact",
                verified=True,
                source_note="synthetic fixture only",
            )
            artifact.verify_file(root)
            path.write_bytes(b"a,b\n9,9\n")
            self.assertEqual(path.stat().st_size, artifact.byte_size)
            with self.assertRaisesRegex(ManifestContractError, "hash changed"):
                artifact.verify_file(root)

    def test_artifact_round_trip_recomputes_identity(self) -> None:
        artifact = self.artifact()
        self.assertEqual(RawDataArtifact.from_dict(artifact.as_dict()), artifact)
        value = artifact.as_dict()
        value["source"] = "tampered"
        with self.assertRaisesRegex(ManifestContractError, "artifact_id"):
            RawDataArtifact.from_dict(value)

    def test_string_verified_rejected_even_with_recomputed_identity(self) -> None:
        value = self.artifact().as_dict()
        value["verified"] = "false"
        value["artifact_id"] = _recompute_artifact_id(value)
        with self.assertRaisesRegex(
            ManifestContractError,
            "verified must be a boolean",
        ):
            RawDataArtifact.from_dict(value)

    def test_numeric_fields_do_not_accept_json_strings_or_booleans(self) -> None:
        invalid = (
            ("byte_size", "12", "byte_size must be an integer"),
            ("row_count", "2", "row_count must be an integer"),
            ("row_count", True, "row_count must be an integer"),
        )
        for name, replacement, message in invalid:
            with self.subTest(name=name, replacement=replacement):
                value = self.artifact().as_dict()
                value[name] = replacement
                value["artifact_id"] = _recompute_artifact_id(value)
                with self.assertRaisesRegex(ManifestContractError, message):
                    RawDataArtifact.from_dict(value)

    def test_datetime_fields_require_iso_strings(self) -> None:
        value = self.artifact().as_dict()
        value["retrieved_at"] = 123
        with self.assertRaisesRegex(ManifestContractError, "retrieved_at must be"):
            RawDataArtifact.from_dict(value)

    def test_unknown_fields_rejected_even_with_recomputed_identity(self) -> None:
        value = self.artifact().as_dict()
        value["unexpected"] = "payload"
        value["artifact_id"] = _recompute_artifact_id(value)
        with self.assertRaisesRegex(ManifestContractError, "unknown fields"):
            RawDataArtifact.from_dict(value)

    def test_direct_constructor_rejects_wrong_runtime_types(self) -> None:
        with self.assertRaisesRegex(ManifestContractError, "byte_size must be an integer"):
            replace(self.artifact(), byte_size=cast(int, "12"))

    def test_unverified_artifact_with_empty_source_note_round_trips(self) -> None:
        artifact = self.artifact(verified=False)
        self.assertEqual(artifact.source_note, "")
        self.assertEqual(RawDataArtifact.from_dict(artifact.as_dict()), artifact)


class TestDataSnapshotManifest(ManifestFixture):
    def test_market_data_requires_calendar_binding(self) -> None:
        with self.assertRaisesRegex(ManifestContractError, "calendar binding"):
            self.snapshot((self.artifact(),), calendar_snapshot_ids=())

    def test_security_data_requires_universe_binding(self) -> None:
        with self.assertRaisesRegex(ManifestContractError, "universe binding"):
            self.snapshot((self.artifact(),), universe_snapshot_id=None)

    def test_unverified_artifact_rejected(self) -> None:
        with self.assertRaisesRegex(ManifestContractError, "unverified"):
            self.snapshot((self.artifact(verified=False),))

    def test_artifact_retrieved_after_creation_rejected(self) -> None:
        with self.assertRaisesRegex(ManifestContractError, "after snapshot"):
            self.snapshot((self.artifact(retrieved_day=7),))

    def test_same_storage_key_cannot_map_to_different_bytes(self) -> None:
        left = self.artifact(sha256="a" * 64)
        right = self.artifact(sha256="b" * 64)
        with self.assertRaisesRegex(ManifestContractError, "storage key"):
            self.snapshot((left, right))

    def test_calendar_artifact_must_bind_its_parsed_snapshot(self) -> None:
        calendar = self.artifact(
            storage_key="raw/calendar/a.json",
            sha256="d" * 64,
            kind=DataKind.EXCHANGE_CALENDAR,
            format=DataFormat.JSON,
            calendar_snapshot_id=None,
        )
        with self.assertRaisesRegex(ManifestContractError, "calendar artifact"):
            self.snapshot((calendar,))

    def test_snapshot_id_is_artifact_order_independent(self) -> None:
        left = self.artifact(storage_key="raw/a/one.csv", sha256="a" * 64)
        right = replace(
            self.artifact(storage_key="raw/a/two.csv", sha256="b" * 64),
            source_dataset="fixture-dataset-two",
        )
        self.assertEqual(
            self.snapshot((left, right)).snapshot_id,
            self.snapshot((right, left)).snapshot_id,
        )

    def test_json_round_trip_and_tamper_detection(self) -> None:
        manifest = self.snapshot((self.artifact(),))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            manifest.write_json(path)
            self.assertEqual(DataSnapshotManifest.read_json(path), manifest)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["name"] = "tampered"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ManifestContractError, "snapshot_id"):
                DataSnapshotManifest.read_json(path)

    def test_safety_gate_types_rejected_even_with_recomputed_identity(self) -> None:
        manifest = self.snapshot((self.artifact(),))
        invalid = (
            ("require_verified", 0),
            ("require_calendar_for_market_data", "true"),
            ("require_universe_for_market_data", "false"),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            for name, value_override in invalid:
                with self.subTest(name=name, value=value_override):
                    value = manifest.as_dict()
                    value[name] = value_override
                    value["snapshot_id"] = _recompute_snapshot_id(value)
                    path.write_text(json.dumps(value), encoding="utf-8")
                    with self.assertRaisesRegex(
                        ManifestContractError,
                        f"{name} must be a boolean",
                    ):
                        DataSnapshotManifest.read_json(path)

    def test_manifest_datetime_and_unknown_fields_fail_closed(self) -> None:
        manifest = self.snapshot((self.artifact(),))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"

            invalid_time = manifest.as_dict()
            invalid_time["as_of"] = 123
            path.write_text(json.dumps(invalid_time), encoding="utf-8")
            with self.assertRaisesRegex(ManifestContractError, "as_of must be"):
                DataSnapshotManifest.read_json(path)

            unknown = manifest.as_dict()
            unknown["unexpected"] = "payload"
            unknown["snapshot_id"] = _recompute_snapshot_id(unknown)
            path.write_text(json.dumps(unknown), encoding="utf-8")
            with self.assertRaisesRegex(ManifestContractError, "unknown fields"):
                DataSnapshotManifest.read_json(path)

    def test_atomic_write_leaves_no_temporary_file(self) -> None:
        manifest = self.snapshot((self.artifact(),))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            manifest.write_json(path)
            leftovers = [item for item in path.parent.iterdir() if item.name != path.name]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
