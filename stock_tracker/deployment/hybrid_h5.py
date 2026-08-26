"""Fail-closed Hybrid H5 sharing/public-access eligibility gates.

This module is intentionally read-only. It never enables Funnel, Cloudflare
Tunnel, port forwarding, or any public listener. Trusted Tailnet sharing is the
only mode that can currently pass; public modes remain blocked until a separate
rate-limit/auth review slice is implemented.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from ..core.network import InvalidOriginError, normalize_http_origin
from ..core.security import PRIVATE_ACCESS_ENV, private_access_value_valid


class HybridH5Error(RuntimeError):
    """Raised when the H5 preflight input itself is invalid."""


class PublicAccessMode(StrEnum):
    TRUSTED_TAILNET = "TRUSTED_TAILNET"
    TAILSCALE_FUNNEL = "TAILSCALE_FUNNEL"
    CLOUDFLARE_TUNNEL = "CLOUDFLARE_TUNNEL"


def _runtime(bundle: Any) -> Any:
    runtime = getattr(getattr(bundle, "app", None), "runtime", None)
    if runtime is None:
        raise HybridH5Error("runtime configuration is unavailable")
    return runtime


def _exact_https_origins(values: object) -> bool:
    if type(values) is not list or not values:
        return False
    normalized: set[str] = set()
    for value in values:
        try:
            origin = normalize_http_origin(value)
        except InvalidOriginError:
            return False
        if not origin.startswith("https://"):
            return False
        normalized.add(origin)
    return len(normalized) == len(values)


def public_access_preflight(
    bundle: Any,
    *,
    mode: PublicAccessMode | str = PublicAccessMode.TRUSTED_TAILNET,
    environ: Mapping[str, str] | None = None,
    acknowledge_public_exposure: bool = False,
    independent_review_id: str | None = None,
) -> dict[str, object]:
    """Return H5 eligibility without mutating host/network state."""

    try:
        selected = mode if isinstance(mode, PublicAccessMode) else PublicAccessMode(mode)
    except (TypeError, ValueError) as exc:
        raise HybridH5Error("unknown H5 access mode") from exc
    if type(acknowledge_public_exposure) is not bool:
        raise HybridH5Error("acknowledge_public_exposure must be an actual boolean")
    if independent_review_id is not None and (
        type(independent_review_id) is not str
        or independent_review_id != independent_review_id.strip()
        or not independent_review_id
        or len(independent_review_id) > 128
        or any(ord(char) < 33 or ord(char) == 127 for char in independent_review_id)
    ):
        raise HybridH5Error("independent_review_id must be a visible stable identifier")

    env = os.environ if environ is None else environ
    runtime = _runtime(bundle)
    blockers: list[str] = []
    checks: dict[str, bool] = {
        "api_target_enabled": getattr(runtime, "api_target_enabled", False) is True,
        "remote_audit_enabled": getattr(runtime, "audit_enabled", False) is True,
        "private_access_strong": private_access_value_valid(env.get(PRIVATE_ACCESS_ENV, "")),
    }
    for name, passed in checks.items():
        if not passed:
            blockers.append(name.upper())

    if selected is PublicAccessMode.TRUSTED_TAILNET:
        if getattr(runtime, "deployment_mode", None) != "HYBRID_PRIVATE":
            blockers.append("TRUSTED_TAILNET_REQUIRES_HYBRID_PRIVATE")
    else:
        checks["public_mode_explicit"] = getattr(runtime, "deployment_mode", None) == "HYBRID_PUBLIC_AUTH"
        checks["public_cors_exact_https"] = _exact_https_origins(
            getattr(runtime, "cors_allowed_origins", None)
        )
        checks["public_exposure_acknowledged"] = acknowledge_public_exposure
        checks["independent_review_present"] = bool(independent_review_id)
        if not checks["public_mode_explicit"]:
            blockers.append("PUBLIC_MODE_NOT_EXPLICIT")
        if not checks["public_cors_exact_https"]:
            blockers.append("PUBLIC_CORS_NOT_EXACT_HTTPS")
        if not checks["public_exposure_acknowledged"]:
            blockers.append("PUBLIC_EXPOSURE_NOT_ACKNOWLEDGED")
        if not checks["independent_review_present"]:
            blockers.append("PUBLIC_INDEPENDENT_REVIEW_MISSING")
        # Deliberate merge-time blockers: no public enable action is shipped in H5.
        blockers.extend(
            [
                "PUBLIC_RATE_LIMIT_NOT_IMPLEMENTED",
                "PUBLIC_ENABLE_ACTION_NOT_IMPLEMENTED",
            ]
        )

    blockers = sorted(set(blockers))
    return {
        "schema": "stock-tracker-hybrid-h5-public-access-preflight-v1",
        "passed": not blockers,
        "mode": selected.value,
        "checks": checks,
        "blockers": blockers,
        "contains_private_access": False,
        "mutates_host_or_network": False,
        "recommended_next": (
            "TRUSTED_TAILNET"
            if selected is not PublicAccessMode.TRUSTED_TAILNET
            else "KEEP_TAILNET_PRIVATE"
        ),
    }


__all__ = [
    "HybridH5Error",
    "PublicAccessMode",
    "public_access_preflight",
]
