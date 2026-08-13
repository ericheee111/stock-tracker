"""Strict loading and validation for the fail-closed quant configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

from .core.fingerprint import fingerprint


class QuantConfigError(ValueError):
    """Raised when quantitative safety controls are missing or relaxed."""


@dataclass(frozen=True, slots=True)
class QuantSafetyConfig:
    require_aware_timestamps: bool
    require_verified_data: bool
    require_complete_date_coverage: bool
    require_explicit_no_bar_status: bool
    require_calendar_snapshot_for_market_data: bool
    require_universe_snapshot_for_market_data: bool
    verify_sha256_before_use: bool
    reject_symlink_artifacts: bool
    enable_labeling_without_calendar_alignment: bool
    enable_training_without_verified_manifest: bool
    probability_advisory_only: bool
    auto_apply_sql: bool
    auto_promote_models: bool
    allow_best_case_same_bar: bool
    allow_random_kfold: bool
    allow_unverified_rules: bool
    allow_unverified_costs: bool

    def validate_fail_closed(self) -> None:
        required_true = {
            "require_aware_timestamps": self.require_aware_timestamps,
            "require_verified_data": self.require_verified_data,
            "require_complete_date_coverage": self.require_complete_date_coverage,
            "require_explicit_no_bar_status": self.require_explicit_no_bar_status,
            "require_calendar_snapshot_for_market_data": (
                self.require_calendar_snapshot_for_market_data
            ),
            "require_universe_snapshot_for_market_data": (
                self.require_universe_snapshot_for_market_data
            ),
            "verify_sha256_before_use": self.verify_sha256_before_use,
            "reject_symlink_artifacts": self.reject_symlink_artifacts,
            "probability_advisory_only": self.probability_advisory_only,
        }
        relaxed = [name for name, enabled in required_true.items() if not enabled]
        required_false = {
            "enable_labeling_without_calendar_alignment": (
                self.enable_labeling_without_calendar_alignment
            ),
            "enable_training_without_verified_manifest": (
                self.enable_training_without_verified_manifest
            ),
            "auto_apply_sql": self.auto_apply_sql,
            "auto_promote_models": self.auto_promote_models,
            "allow_best_case_same_bar": self.allow_best_case_same_bar,
            "allow_random_kfold": self.allow_random_kfold,
            "allow_unverified_rules": self.allow_unverified_rules,
            "allow_unverified_costs": self.allow_unverified_costs,
        }
        relaxed.extend(name for name, enabled in required_false.items() if enabled)
        if relaxed:
            raise QuantConfigError(
                "quant safety configuration is not fail-closed: "
                + ", ".join(sorted(relaxed))
            )


@dataclass(frozen=True, slots=True)
class QuantConfigBundle:
    source_paths: tuple[str, ...]
    raw: dict[str, Any]
    safety: QuantSafetyConfig

    @property
    def config_hash(self) -> str:
        return fingerprint(
            {
                "schema": "quant-config-bundle-v1",
                "source_files": [Path(path).name for path in self.source_paths],
                "raw": self.raw,
            }
        )


def default_config_paths(project_root: str | Path | None = None) -> tuple[Path, Path]:
    root = (
        Path(project_root)
        if project_root is not None
        else Path(__file__).resolve().parents[2]
    )
    return root / "config" / "quant_wave1.toml", root / "config" / "quant_wave2.toml"


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise QuantConfigError(f"quant config file is missing: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise QuantConfigError(f"quant config TOML is invalid: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise QuantConfigError(f"quant config root must be a table: {path}")
    return value


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    result = dict(target)
    for key, value in source.items():
        if key in {"schema_version", "foundation_version"}:
            versions = list(result.get(f"_{key}s", []))
            versions.append(value)
            result[f"_{key}s"] = versions
            continue
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _deep_merge(existing, value)
        elif key in result and existing != value:
            raise QuantConfigError(f"conflicting quant config value for {key}")
        else:
            result[key] = value
    return result


def _strict_bool(table: dict[str, Any], key: str) -> bool:
    if key not in table or type(table[key]) is not bool:
        raise QuantConfigError(f"safety.{key} must be an explicit boolean")
    return table[key]


def load_quant_config(
    paths: tuple[str | Path, ...] | None = None,
    *,
    project_root: str | Path | None = None,
) -> QuantConfigBundle:
    selected = (
        tuple(Path(path) for path in paths)
        if paths is not None
        else default_config_paths(project_root)
    )
    if not selected:
        raise QuantConfigError("at least one quant config file is required")
    merged: dict[str, Any] = {}
    for path in selected:
        merged = _deep_merge(merged, _read_toml(path))
    safety_table = merged.get("safety")
    if not isinstance(safety_table, dict):
        raise QuantConfigError("[safety] table is required")
    field_names = tuple(QuantSafetyConfig.__dataclass_fields__)
    unknown = sorted(set(safety_table) - set(field_names))
    if unknown:
        raise QuantConfigError(
            "unknown safety controls: " + ", ".join(unknown)
        )
    safety = QuantSafetyConfig(
        **{name: _strict_bool(safety_table, name) for name in field_names}
    )
    safety.validate_fail_closed()
    return QuantConfigBundle(
        source_paths=tuple(path.as_posix() for path in selected),
        raw=merged,
        safety=safety,
    )
