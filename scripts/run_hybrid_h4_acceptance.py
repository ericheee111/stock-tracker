#!/usr/bin/env python3
"""Local real-browser acceptance for the Hybrid H4 static release boundary.

The harness uses two distinct loopback origins and a temporary SQLite database.
It proves the generated static artifact, response headers, Runtime handshake,
Bearer CRUD/SSE, metadata-only audit, and offline shell behavior without
claiming a real Pages or Tailscale deployment.
"""

from __future__ import annotations

import functools
import hashlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tracker.api.audit import RemoteAuditLogger
from stock_tracker.api.handlers import AppContext
from stock_tracker.api.server import APIServer
from stock_tracker.api.sse import SSEHub
from stock_tracker.core import types as T
from stock_tracker.core.config import load_configs
from stock_tracker.core.store import MarketStore
from stock_tracker.deployment.hybrid_h4 import StaticBuildConfig, build_static_site
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository

ENGINE_ID = "hybrid-h4-browser-fixture"
BUILD_ID = "hybrid-h4-browser-fixture-commit"
PRIVATE_ACCESS = secrets.token_urlsafe(32)


class _Bus:
    def subscribe(self, callback) -> None:
        self.callback = callback


class _Router:
    def health_list(self) -> list[T.ProviderHealth]:
        return [
            T.ProviderHealth(
                provider="h4-fixture",
                circuit_state=T.CircuitState.CLOSED,
                last_success_at=datetime.now(timezone.utc),
            )
        ]


class _AliveThread:
    def is_alive(self) -> bool:
        return True


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reserve_closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _parse_headers(directory: Path, request_path: str) -> dict[str, str]:
    path = directory / "_headers"
    if not path.is_file():
        return {}
    sections: list[tuple[str, dict[str, str]]] = []
    current_pattern: str | None = None
    current_headers: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith((" ", "\t")):
            if current_pattern is not None:
                sections.append((current_pattern, current_headers))
            current_pattern = line.strip()
            current_headers = {}
        elif current_pattern and line.startswith((" ", "\t")) and ":" in line:
            name, value = line.strip().split(":", 1)
            current_headers[name] = value.strip()
    if current_pattern is not None:
        sections.append((current_pattern, current_headers))

    normalized_path = "/index.html" if request_path == "/" else request_path.split("?", 1)[0]
    output: dict[str, str] = {}
    for pattern, values in sections:
        if pattern == "/*" or pattern == normalized_path:
            output.update(values)
    return output


class _StaticHandler(SimpleHTTPRequestHandler):
    server_version = "StockTrackerH4Fixture/1"

    def end_headers(self) -> None:
        directory = Path(self.directory)
        for name, value in _parse_headers(directory, self.path).items():
            self.send_header(name, value)
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        del format, args


def _start_static(directory: Path) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    handler = functools.partial(_StaticHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _context(directory: Path, web_origin: str) -> AppContext:
    bundle = load_configs(str(ROOT / "config"))
    bundle.app.runtime.engine_id = ENGINE_ID
    bundle.app.runtime.commit_id = BUILD_ID
    bundle.app.runtime.cors_allowed_origins = [web_origin]
    bundle.app.runtime.audit_enabled = True

    now_utc = datetime.now(timezone.utc)
    market_now = (
        now_utc + timedelta(hours=bundle.markets.a.utc_offset_hours)
    ).replace(tzinfo=None)
    store = MarketStore()
    store.update_quote(
        T.Quote(
            symbol="600000.SH",
            market=T.Market.A,
            timestamp=market_now,
            name="浦发银行",
            open=10.0,
            high=10.2,
            low=9.9,
            close=10.1,
            last=10.1,
            prev_close=10.0,
            source="hybrid-h4-browser-fixture",
            received_at=now_utc,
            computed_at=now_utc,
            displayed_at=now_utc,
            observed_age_ms=25,
            data_status=T.DataStatus.LIVE,
        )
    )
    context = AppContext(
        bundle=bundle,
        store=store,
        repo=Repository(str(directory / "hybrid-h4.db")),
        router=_Router(),
        signal_manager=SimpleNamespace(_portfolio_heat=lambda: 0.0),
        sse_hub=SSEHub(_Bus()),
        scheduler=SimpleNamespace(_stop=threading.Event(), _threads=[_AliveThread()]),
        web_root=str(ROOT / "web"),
    )
    context.audit_logger = RemoteAuditLogger(directory / "remote-audit.jsonl")
    return context


def _shutdown(
    api_server: APIServer | None,
    api_thread: threading.Thread | None,
    static_servers: list[tuple[ThreadingHTTPServer, threading.Thread]],
) -> None:
    if api_server is not None:
        api_server.shutdown_wait()
    if api_thread is not None:
        api_thread.join(timeout=5)
    for server, thread in static_servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    close_all()


def main() -> int:
    production_db = ROOT / "data" / "stock_tracker.db"
    production_before = _sha256(production_db)
    previous_access = os.environ.get("STOCK_TRACKER_PRIVATE_ACCESS")
    os.environ["STOCK_TRACKER_PRIVATE_ACCESS"] = PRIVATE_ACCESS

    static_servers: list[tuple[ThreadingHTTPServer, threading.Thread]] = []
    api_server: APIServer | None = None
    api_thread: threading.Thread | None = None
    directory = Path(tempfile.mkdtemp(prefix="hybrid-h4-acceptance-"))
    try:
        online_output = directory / "online"
        offline_output = directory / "offline"

        # Reserve the online static Origin before configuring exact backend CORS.
        online_server, online_thread, web_origin = _start_static(online_output)
        static_servers.append((online_server, online_thread))
        ctx = _context(directory, web_origin)
        api_server = APIServer(
            "127.0.0.1",
            0,
            ctx,
            None,
            api_only=True,
            audit_logger=ctx.audit_logger,
        )
        api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
        api_thread.start()
        api_origin = f"http://localhost:{api_server.server_address[1]}"

        build_static_site(
            ROOT / "web",
            online_output,
            StaticBuildConfig(
                web_origin=web_origin,
                api_origin=api_origin,
                engine_id=ENGINE_ID,
                build_id=BUILD_ID,
                host="cloudflare",
                allow_loopback_http=True,
            ),
            forbidden_values=(PRIVATE_ACCESS,),
        )

        offline_server, offline_thread, offline_web_origin = _start_static(offline_output)
        static_servers.append((offline_server, offline_thread))
        offline_api_origin = f"http://localhost:{_reserve_closed_port()}"
        build_static_site(
            ROOT / "web",
            offline_output,
            StaticBuildConfig(
                web_origin=offline_web_origin,
                api_origin=offline_api_origin,
                engine_id=ENGINE_ID,
                build_id=BUILD_ID,
                host="cloudflare",
                allow_loopback_http=True,
            ),
            forbidden_values=(PRIVATE_ACCESS,),
        )

        environment = os.environ.copy()
        environment.update(
            {
                "H4_WEB_BASE_URL": web_origin,
                "H4_API_ORIGIN": api_origin,
                "H4_OFFLINE_WEB_BASE_URL": offline_web_origin,
                "H4_OFFLINE_API_ORIGIN": offline_api_origin,
                "H4_PRIVATE_ACCESS": PRIVATE_ACCESS,
                "H4_EXPECTED_ENGINE": ENGINE_ID,
                "H4_EXPECTED_BUILD": BUILD_ID,
            }
        )
        result = subprocess.run(
            ["node", "ui/hybrid_h4_qa.cjs"],
            cwd=ROOT / "qa",
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=120,
        )
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.returncode != 0:
            return result.returncode

        audit_path = directory / "remote-audit.jsonl"
        if not audit_path.is_file():
            print("FAIL H4 remote write audit was not created", file=sys.stderr)
            return 1
        audit_text = audit_path.read_text(encoding="utf-8")
        if PRIVATE_ACCESS in audit_text:
            print("FAIL H4 audit contains private access", file=sys.stderr)
            return 1

        production_after = _sha256(production_db)
        if production_before != production_after:
            print("FAIL production database hash changed", file=sys.stderr)
            return 1
        print("PASS production database hash unchanged | " + str(production_after))
        print("H4_LOCAL_STATIC_ACCEPTANCE=PASSED")
        print("H4_REAL_PAGES_DEPLOYMENT=PENDING")
        return 0
    finally:
        _shutdown(api_server, api_thread, static_servers)
        shutil.rmtree(directory, ignore_errors=False)
        if previous_access is None:
            os.environ.pop("STOCK_TRACKER_PRIVATE_ACCESS", None)
        else:
            os.environ["STOCK_TRACKER_PRIVATE_ACCESS"] = previous_access


if __name__ == "__main__":
    raise SystemExit(main())
