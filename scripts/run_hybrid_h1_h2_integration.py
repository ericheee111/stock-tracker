#!/usr/bin/env python3
"""Run real-browser Hybrid H1/H2 cross-origin acceptance on temporary state only."""

from __future__ import annotations

import functools
import json
import os
import secrets
import shutil
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

from stock_tracker.api.handlers import AppContext
from stock_tracker.api.server import APIServer
from stock_tracker.api.sse import SSEHub
from stock_tracker.core import types as T
from stock_tracker.core.config import load_configs
from stock_tracker.core.security import PRIVATE_ACCESS_ENV
from stock_tracker.core.store import MarketStore
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository

PRIVATE_ACCESS = secrets.token_urlsafe(32)
ENGINE_ID = "hybrid-h1-h2-browser-fixture"
COMMIT_ID = "hybrid-h1-h2-fixture-commit"


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class _Bus:
    def subscribe(self, callback: object) -> None:
        self.callback = callback


class _AliveThread:
    def is_alive(self) -> bool:
        return True


class _Router:
    def health_list(self) -> list[T.ProviderHealth]:
        return [
            T.ProviderHealth(
                provider="hybrid-browser-fixture",
                circuit_state=T.CircuitState.CLOSED,
                last_success_at=datetime.now(timezone.utc),
            )
        ]


def _runtime_config(api_origin: str) -> str:
    payload = {
        "deploymentMode": "HYBRID_PRIVATE",
        "apiBaseUrl": api_origin,
        "allowedApiOrigins": [api_origin],
        "ssePath": "/api/stream",
        "frontendBuild": COMMIT_ID,
        "expectedApiMajor": 1,
        "expectedEngineId": ENGINE_ID,
        "allowApiOriginOverride": False,
        "allowPrivateBrowserCache": False,
        "healthPollMs": 15000,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        "/* generated temporary Hybrid H1/H2 browser fixture; contains no secret */\n"
        "window.STOCK_TRACKER_RUNTIME=Object.freeze(" + encoded + ");\n"
    )


def _start_static(directory: Path) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    handler = functools.partial(_QuietStaticHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread, origin


def _context(directory: Path, web_origin: str) -> AppContext:
    bundle = load_configs(str(ROOT / "config"))
    bundle.app.runtime.engine_id = ENGINE_ID
    bundle.app.runtime.commit_id = COMMIT_ID
    bundle.app.runtime.cors_allowed_origins = [web_origin]
    bundle.app.runtime.cors_max_age_sec = 300

    # Stage 1 source timestamps are market-local naive values; collection times are
    # process UTC metadata. Keep both contracts explicit in the browser fixture.
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
            source="hybrid-browser-fixture",
            received_at=now_utc,
            computed_at=now_utc,
            displayed_at=now_utc,
            observed_age_ms=25,
            data_status=T.DataStatus.LIVE,
        )
    )
    repo = Repository(str(directory / "hybrid-browser.db"))
    return AppContext(
        bundle=bundle,
        store=store,
        repo=repo,
        router=_Router(),
        signal_manager=SimpleNamespace(_portfolio_heat=lambda: 0.0),
        sse_hub=SSEHub(_Bus()),
        web_root=str(directory / "web"),
        scheduler=SimpleNamespace(
            _stop=threading.Event(),
            _threads=[_AliveThread()],
        ),
    )


def main() -> int:
    previous_access = os.environ.get(PRIVATE_ACCESS_ENV)
    os.environ[PRIVATE_ACCESS_ENV] = PRIVATE_ACCESS
    temp = tempfile.TemporaryDirectory(prefix="stock-tracker-hybrid-h1-h2-")
    directory = Path(temp.name)
    static_server: ThreadingHTTPServer | None = None
    static_thread: threading.Thread | None = None
    api_server: APIServer | None = None
    api_thread: threading.Thread | None = None
    return_code = 1
    try:
        web_directory = directory / "web"
        shutil.copytree(ROOT / "web", web_directory)

        static_server, static_thread, web_origin = _start_static(web_directory)
        ctx = _context(directory, web_origin)
        api_server = APIServer("127.0.0.1", 0, ctx, None)
        api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
        api_thread.start()
        api_origin = f"http://localhost:{api_server.server_address[1]}"
        (web_directory / "runtime-config.js").write_text(
            _runtime_config(api_origin),
            encoding="utf-8",
        )

        environment = os.environ.copy()
        environment.update(
            {
                "HYBRID_WEB_BASE_URL": web_origin,
                "HYBRID_API_ORIGIN": api_origin,
                "HYBRID_PRIVATE_ACCESS": PRIVATE_ACCESS,
                "HYBRID_EXPECTED_ENGINE": ENGINE_ID,
                "HYBRID_EXPECTED_COMMIT": COMMIT_ID,
            }
        )
        return_code = 0
        for script in (
            "ui/hybrid_runtime_qa.cjs",
            "ui/hybrid_runtime_config_error_qa.cjs",
            "ui/hybrid_runtime_invalid_health_qa.cjs",
            "ui/hybrid_runtime_build_mismatch_qa.cjs",
            "ui/hybrid_runtime_stale_qa.cjs",
        ):
            result = subprocess.run(
                ["node", script],
                cwd=ROOT / "qa",
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=90,
            )
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            if result.returncode:
                return_code = result.returncode
                break
    finally:
        if api_server is not None:
            api_server.shutdown_wait()
        if api_thread is not None:
            api_thread.join(timeout=5)
        if static_server is not None:
            static_server.shutdown()
            static_server.server_close()
        if static_thread is not None:
            static_thread.join(timeout=5)
        close_all()
        temp.cleanup()
        if previous_access is None:
            os.environ.pop(PRIVATE_ACCESS_ENV, None)
        else:
            os.environ[PRIVATE_ACCESS_ENV] = previous_access
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
