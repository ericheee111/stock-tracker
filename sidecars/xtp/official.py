# ruff: noqa: UP006, UP031, UP035, UP037, UP045
"""Fail-closed helpers for a future official XTP quote-library binding.

This module intentionally does not import or expose the trader/algo libraries.
It validates the CPython ABI and quote-only environment, and provides a callback
bridge that an operational adapter can attach to the official quote SPI.
"""

from __future__ import annotations

import importlib
import ipaddress
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import ModuleType
from typing import Any, Dict, Mapping, Optional

from .contracts import (
    ENV_CLIENT_ID,
    ENV_QUOTE_PASSWORD,
    ENV_QUOTE_PORT,
    ENV_QUOTE_PROTOCOL,
    ENV_QUOTE_SERVER,
    ENV_QUOTE_USER,
    EventEnvelope,
    XtpSidecarContractError,
    trading_day_for,
    validate_symbol,
)
from .runtime import SidecarRuntime

_OFFICIAL_API_VERSION = "2.2.50.8"
_ALLOWED_MODULE_NAMES = frozenset({"xtpquoteapi", "xtp_quote_api"})
_FORBIDDEN_MODULE_NAME_PARTS = ("trader", "order", "algo")
_FORBIDDEN_SURFACE_MARKERS = (
    "trader",
    "algo",
    "insertorder",
    "cancelorder",
    "placeorder",
    "submitorder",
    "createorder",
    "orderapi",
)


def _visible_environment(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value or value != value.strip():
        raise XtpSidecarContractError("required XTP quote environment value is missing: %s" % name)
    if len(value) > 1024 or any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise XtpSidecarContractError("XTP quote environment value is invalid: %s" % name)
    return value


@dataclass(frozen=True)
class OfficialQuoteEnvironment:
    user_present: bool
    credential_present: bool
    server: str = field(repr=False)
    port: int = field(repr=False)
    client_id: int = field(repr=False)
    protocol: str = "TCP"
    api_version: str = _OFFICIAL_API_VERSION

    @classmethod
    def from_environ(cls) -> "OfficialQuoteEnvironment":
        user = _visible_environment(ENV_QUOTE_USER)
        credential = _visible_environment(ENV_QUOTE_PASSWORD)
        server = _visible_environment(ENV_QUOTE_SERVER)
        port_text = _visible_environment(ENV_QUOTE_PORT)
        protocol = _visible_environment(ENV_QUOTE_PROTOCOL).upper()
        client_text = _visible_environment(ENV_CLIENT_ID)
        try:
            ipaddress.ip_address(server)
        except ValueError as exc:
            raise XtpSidecarContractError(
                "XTP quote server must be the literal IP supplied by the test-account portal"
            ) from exc
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise XtpSidecarContractError("XTP quote port must be between 1 and 65535")
        if protocol != "TCP":
            raise XtpSidecarContractError(
                "current read-only XTP test sidecar requires TCP protocol"
            )
        if not client_text.isdigit() or not 1 <= int(client_text) <= 255:
            raise XtpSidecarContractError("XTP client id must be between 1 and 255")
        return cls(
            user_present=bool(user),
            credential_present=bool(credential),
            server=server,
            port=int(port_text),
            client_id=int(client_text),
            protocol=protocol,
        )

    def as_safe_dict(self) -> Dict[str, Any]:
        return {
            "schema": "stock-tracker-xtp-official-environment-v1",
            "user_present": self.user_present,
            "credential_present": self.credential_present,
            "server_present": bool(self.server),
            "port_present": self.port > 0,
            "client_id_present": self.client_id > 0,
            "protocol": self.protocol,
            "api_version": self.api_version,
            "contains_account_value": False,
            "contains_server_value": False,
        }


def _runtime_python_series() -> tuple[int, int]:
    return sys.version_info[:2]


def require_python39() -> None:
    if _runtime_python_series() != (3, 9):
        raise XtpSidecarContractError(
            "official XTP Python binary requires an isolated CPython 3.9 runtime"
        )


def load_quote_module(name: str = "xtpquoteapi") -> ModuleType:
    require_python39()
    if name not in _ALLOWED_MODULE_NAMES or any(
        part in name.lower() for part in _FORBIDDEN_MODULE_NAME_PARTS
    ):
        raise XtpSidecarContractError("only the official quote module may be loaded")
    try:
        module = importlib.import_module(name)
    except ImportError as exc:
        raise XtpSidecarContractError(
            "official XTP quote module is not installed in the CPython 3.9 sidecar"
        ) from exc
    module_file = str(getattr(module, "__file__", ""))
    if not module_file:
        raise XtpSidecarContractError("official XTP quote module has no file identity")
    return module


def quote_module_capabilities(module: ModuleType) -> Dict[str, Any]:
    names = set(dir(module))
    forbidden = sorted(
        name
        for name in names
        if any(
            marker in re.sub(r"[^a-z0-9]", "", name.lower())
            for marker in _FORBIDDEN_SURFACE_MARKERS
        )
    )
    quote_factory_present = any(
        name in names
        for name in ("QuoteApi", "QuoteAPI", "createQuoteApi", "CreateQuoteApi")
    )
    if forbidden:
        raise XtpSidecarContractError(
            "official quote module exposes a forbidden trading/algo surface"
        )
    if not quote_factory_present:
        raise XtpSidecarContractError(
            "official quote module does not expose a recognized quote factory"
        )
    return {
        "schema": "stock-tracker-xtp-quote-module-probe-v1",
        "module_file_present": bool(getattr(module, "__file__", None)),
        "quote_factory_present": True,
        "forbidden_surface_detected": False,
        "forbidden_surface_names": [],
        "operational_binding": "PENDING_ACCOUNT_AND_SDK_ACCEPTANCE",
    }


class QuoteCallbackBridge:
    """Normalize quote callbacks without depending on the SDK's callback class."""

    def __init__(self, runtime: SidecarRuntime, *, feed_mode: str = "LEVEL1") -> None:
        if feed_mode not in {"LEVEL1", "LEVEL2"}:
            raise XtpSidecarContractError("official feed_mode must be LEVEL1 or LEVEL2")
        self.runtime = runtime
        self.feed_mode = feed_mode

    @staticmethod
    def _number(
        raw: Mapping[str, Any],
        name: str,
        *,
        positive: bool = False,
        nonnegative: bool = False,
    ) -> float:
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise XtpSidecarContractError("callback %s must be numeric" % name)
        number = float(value)
        if not math.isfinite(number):
            raise XtpSidecarContractError("callback %s must be finite" % name)
        if positive and number <= 0:
            raise XtpSidecarContractError("callback %s must be positive" % name)
        if nonnegative and number < 0:
            raise XtpSidecarContractError("callback %s must be non-negative" % name)
        return number

    def on_market_data(
        self,
        raw: Mapping[str, Any],
        *,
        symbol: str,
        provider_timestamp: datetime,
        provider_seq: Optional[int] = None,
        received_at: Optional[datetime] = None,
    ) -> EventEnvelope:
        symbol = validate_symbol(symbol)
        if symbol not in self.runtime.symbols:
            raise XtpSidecarContractError("callback symbol is not subscribed")
        if provider_timestamp.tzinfo is None or provider_timestamp.utcoffset() is None:
            raise XtpSidecarContractError("provider timestamp must be timezone-aware")
        current = received_at or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise XtpSidecarContractError("received_at must be timezone-aware")
        volume = self._number(raw, "volume", nonnegative=True)
        if not volume.is_integer():
            raise XtpSidecarContractError("callback volume must be an integer")
        payload = {
            "last": self._number(raw, "last", positive=True),
            "open": self._number(raw, "open", positive=True),
            "high": self._number(raw, "high", positive=True),
            "low": self._number(raw, "low", positive=True),
            "prev_close": self._number(raw, "prev_close", positive=True),
            "volume": int(volume),
            "amount": self._number(raw, "amount", nonnegative=True),
            "simulator": False,
        }
        if payload["low"] > min(payload["open"], payload["last"], payload["high"]):
            raise XtpSidecarContractError("callback OHLC values are inconsistent")
        if payload["high"] < max(payload["open"], payload["last"], payload["low"]):
            raise XtpSidecarContractError("callback OHLC values are inconsistent")
        event = EventEnvelope.create(
            feed_mode=self.feed_mode,
            symbol=symbol,
            event_type="MARKET_DATA",
            trading_day=trading_day_for(provider_timestamp),
            exchange_timestamp=provider_timestamp,
            provider_timestamp=provider_timestamp,
            received_at=current,
            session_id=self.runtime.session_id,
            callback_seq=self.runtime.next_callback_seq(),
            provider_seq=provider_seq,
            payload=payload,
        )
        self.runtime.append(event)
        return event

    def on_disconnected(self, reason: Any) -> None:
        code = "XTP_DISCONNECTED"
        if type(reason) is int and -(1 << 31) <= reason <= (1 << 31) - 1:
            code = "XTP_DISCONNECTED_%d" % reason
        self.runtime.mark_disconnected(code)

    def on_reconnecting(self) -> None:
        self.runtime.mark_reconnecting()

    def on_connected(self) -> None:
        self.runtime.mark_connected(feed_mode=self.feed_mode)
