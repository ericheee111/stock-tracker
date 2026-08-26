from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from stock_tracker import __main__ as entrypoint
from stock_tracker.api.audit import AuditWriteError, RemoteAuditLogger, route_template
from stock_tracker.api.handlers import AppContext
from stock_tracker.api.server import APIServer
from stock_tracker.api.sse import SSEHub
from stock_tracker.core import types as T
from stock_tracker.core.config import ConfigError, load_app, load_configs
from stock_tracker.core.security import PRIVATE_ACCESS_ENV
from stock_tracker.core.store import MarketStore
from stock_tracker.core.timezones import market_local_to_utc, utc_to_market_local
from stock_tracker.deployment.hybrid_h0 import CommandResult, serve_target
from stock_tracker.deployment.hybrid_h3 import (
    NEW_PRIVATE_ACCESS_ENV,
    HybridH3Error,
    apply_windows_task_plan,
    build_windows_task_plan,
    inspect_target_lane,
    migrate_to_api_target,
    rollback_to_whole_site,
    run_preflight,
    token_rotation_plan,
)
from stock_tracker.deployment.power_guard import (
    ES_CONTINUOUS,
    ES_SYSTEM_REQUIRED,
    TradingPowerGuard,
    active_trading_markets,
)
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository

ROOT = Path(__file__).resolve().parents[1]
_ALLOWED_ORIGIN = "https://app.example"
_STRONG_ACCESS = "hybrid-h3-test-only-" + ("x" * 32)
_NEW_ACCESS = "hybrid-h3-new-test-only-" + ("y" * 32)


class _Bus:
    def subscribe(self, callback) -> None:
        self.callback = callback


class _Router:
    def health_list(self) -> list[T.ProviderHealth]:
        return [
            T.ProviderHealth(
                provider="h3-fixture",
                circuit_state=T.CircuitState.CLOSED,
                last_success_at=datetime.now(timezone.utc),
            )
        ]


class _AliveThread:
    def is_alive(self) -> bool:
        return True


class _Preflight:
    def __init__(self, binary: str) -> None:
        self.tailscale_binary = binary

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "fixture-preflight",
            "passed": True,
            "contains_private_access": False,
        }


class _ServeRunner:
    def __init__(
        self,
        target: str | None,
        *,
        fail_api_enable: bool = False,
        fail_whole_site_enable: bool = False,
        conflict_on_api_failure: bool = False,
    ) -> None:
        self.target = target
        self.fail_api_enable = fail_api_enable
        self.fail_whole_site_enable = fail_whole_site_enable
        self.conflict_on_api_failure = conflict_on_api_failure
        self.calls: list[tuple[str, ...]] = []

    def _payload(self) -> str:
        if self.target is None:
            return "{}"
        return json.dumps(
            {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "fixture.tailnet.ts.net:443": {
                        "Handlers": {"/": {"Proxy": self.target}}
                    }
                },
                "AllowFunnel": {"fixture.tailnet.ts.net:443": False},
            }
        )

    def __call__(self, argv: tuple[str, ...], timeout_sec: float) -> CommandResult:
        del timeout_sec
        self.calls.append(argv)
        suffix = argv[1:]
        if suffix == ("version",):
            return CommandResult(argv, 0, "1.90.0", "")
        if suffix == ("status", "--json"):
            return CommandResult(
                argv,
                0,
                json.dumps(
                    {
                        "BackendState": "Running",
                        "Self": {
                            "DNSName": "fixture.tailnet.ts.net.",
                            "StableID": "node-h3-fixture",
                        },
                    }
                ),
                "",
            )
        if suffix == ("serve", "status", "--json"):
            return CommandResult(argv, 0, self._payload(), "")
        if suffix == ("serve", "off"):
            self.target = None
            return CommandResult(argv, 0, "off", "")
        if len(suffix) == 3 and suffix[:2] == ("serve", "--bg"):
            requested = suffix[2]
            if self.fail_api_enable and requested == serve_target(8081):
                self.fail_api_enable = False
                if self.conflict_on_api_failure:
                    self.target = "http://127.0.0.1:9999"
                return CommandResult(argv, 2, "", "injected H3 enable failure")
            if self.fail_whole_site_enable and requested == serve_target(8080):
                self.fail_whole_site_enable = False
                return CommandResult(argv, 2, "", "injected H0 restore failure")
            self.target = requested
            return CommandResult(argv, 0, "enabled", "")
        return CommandResult(argv, 2, "", f"unexpected command: {suffix}")


class _FailingAudit:
    def record(self, **kwargs) -> None:
        del kwargs
        raise AuditWriteError("injected audit failure")


class TestHybridH3Configuration(unittest.TestCase):
    def test_committed_h3_config_is_loopback_targeted_and_audited(self) -> None:
        runtime = load_configs(str(ROOT / "config")).app.runtime
        self.assertTrue(runtime.api_target_enabled)
        self.assertEqual(runtime.api_target_port, 8081)
        self.assertTrue(runtime.audit_enabled)
        self.assertTrue(runtime.audit_log_path.endswith(".jsonl"))
        self.assertFalse(runtime.prevent_sleep_during_trading)

    def test_effective_main_port_cannot_collide_with_api_target(self) -> None:
        bundle = load_configs(str(ROOT / "config"))
        args = SimpleNamespace(
            config_dir=str(ROOT / "config"),
            host=None,
            port=bundle.app.runtime.api_target_port,
            allow_non_loopback=False,
        )
        with (
            mock.patch.object(entrypoint, "load_configs", return_value=bundle),
            mock.patch.object(entrypoint, "setup_logging", return_value=mock.Mock()),
            self.assertRaisesRegex(RuntimeError, "conflicts"),
        ):
            entrypoint.build_context(args)

    def test_h3_config_rejects_unsafe_host_shapes(self) -> None:
        invalid = (
            "[server]\nport=8080\n[runtime]\napi_target_enabled=true\napi_target_port=8080\n",
            '[runtime]\naudit_log_path="../audit.jsonl"\n',
            '[runtime]\naudit_log_path="C:/audit.jsonl"\n',
            '[runtime]\naudit_log_path="data/audit.log"\n',
            '[runtime]\naudit_log_path="data/audit:stream.jsonl"\n',
            '[runtime]\napi_target_enabled=1\n',
            '[runtime]\ndeployment_mode="LOCAL_ONLY"\napi_target_enabled=true\n',
            '[runtime]\npower_guard_interval_sec=14\n',
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, document in enumerate(invalid):
                path = Path(directory) / f"app-{index}.toml"
                path.write_text(document, encoding="utf-8")
                with self.subTest(document=document), self.assertRaises(ConfigError):
                    load_app(str(path), directory)


class TestHybridH3AuditAndTarget(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="hybrid-h3-http-")
        self.bundle = load_configs(str(ROOT / "config"))
        self.bundle.app.runtime.cors_allowed_origins = [_ALLOWED_ORIGIN]
        self.store = MarketStore()
        self.repo = Repository(str(Path(self.temp.name) / "h3.db"))
        self.ctx = AppContext(
            bundle=self.bundle,
            store=self.store,
            repo=self.repo,
            router=_Router(),
            signal_manager=SimpleNamespace(_portfolio_heat=lambda: 0.0),
            sse_hub=SSEHub(_Bus()),
            scheduler=SimpleNamespace(
                _stop=threading.Event(),
                _threads=[_AliveThread()],
            ),
        )
        self.previous_access = os.environ.get(PRIVATE_ACCESS_ENV)
        os.environ[PRIVATE_ACCESS_ENV] = _STRONG_ACCESS

    def tearDown(self) -> None:
        close_all()
        self.temp.cleanup()
        if self.previous_access is None:
            os.environ.pop(PRIVATE_ACCESS_ENV, None)
        else:
            os.environ[PRIVATE_ACCESS_ENV] = self.previous_access

    def _request(
        self,
        server: APIServer,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        host: str | None = None,
        origin: str | None = _ALLOWED_ORIGIN,
    ) -> tuple[int, bytes, dict[str, str]]:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Host": host or f"127.0.0.1:{port}",
            "Sec-Fetch-Site": "cross-site" if origin else "none",
            "Authorization": f"Bearer {_STRONG_ACCESS}",
            "Accept": "application/json",
        }
        if origin:
            headers["Origin"] = origin
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            response_headers = {name.lower(): value for name, value in response.getheaders()}
            return response.status, raw, response_headers
        finally:
            connection.close()
            server.shutdown_wait()
            thread.join(timeout=5)

    def test_api_only_target_has_no_static_surface(self) -> None:
        audit = RemoteAuditLogger(Path(self.temp.name) / "audit.jsonl")
        server = APIServer("127.0.0.1", 0, self.ctx, None, api_only=True, audit_logger=audit)
        status, _, headers = self._request(server, "GET", "/")
        self.assertEqual(status, 404)
        self.assertIn("x-request-id", headers)

        server = APIServer("127.0.0.1", 0, self.ctx, None, api_only=True, audit_logger=audit)
        status, raw, headers = self._request(server, "GET", "/api/runtime/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["engine_id"], self.bundle.app.runtime.engine_id)
        self.assertEqual(headers["access-control-allow-origin"], _ALLOWED_ORIGIN)

    def test_h3_preflight_requires_api_only_listener(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = str(Path(directory) / "tailscale.exe")
            Path(binary).write_bytes(b"fixture")
            runner = _ServeRunner(None)
            audit = RemoteAuditLogger(Path(directory) / "preflight-audit.jsonl")

            api_only = APIServer(
                "127.0.0.1",
                0,
                self.ctx,
                None,
                api_only=True,
                audit_logger=audit,
            )
            api_thread = threading.Thread(target=api_only.serve_forever, daemon=True)
            api_thread.start()
            try:
                report = run_preflight(
                    port=api_only.server_address[1],
                    private_access=_STRONG_ACCESS,
                    tailscale_binary=binary,
                    runner=runner,
                )
                self.assertTrue(report.passed, report.as_dict())
                self.assertFalse(report.as_dict()["contains_private_access"])
            finally:
                api_only.shutdown_wait()
                api_thread.join(timeout=5)

            whole_site = APIServer(
                "127.0.0.1",
                0,
                self.ctx,
                None,
                api_only=False,
                audit_logger=audit,
            )
            whole_thread = threading.Thread(target=whole_site.serve_forever, daemon=True)
            whole_thread.start()
            try:
                with self.assertRaisesRegex(HybridH3Error, "api_target_static_root_is_absent"):
                    run_preflight(
                        port=whole_site.server_address[1],
                        private_access=_STRONG_ACCESS,
                        tailscale_binary=binary,
                        runner=runner,
                    )
            finally:
                whole_site.shutdown_wait()
                whole_thread.join(timeout=5)

    def test_remote_write_audit_is_metadata_only_and_redacts_identifiers(self) -> None:
        audit_path = Path(self.temp.name) / "remote.jsonl"
        audit = RemoteAuditLogger(audit_path, max_bytes=64 * 1024, backup_count=2)
        server = APIServer("127.0.0.1", 0, self.ctx, None, audit_logger=audit)
        status, raw, headers = self._request(
            server,
            "PUT",
            "/api/portfolio/profile",
            body={
                "account_equity": 120000,
                "available_cash": 60000,
                "risk_mode": "BALANCED",
                "per_trade_risk_pct": 0.007,
                "max_position_pct": 0.2,
                "max_portfolio_heat_pct": 0.08,
                "max_sector_pct": 0.35,
                "max_theme_pct": 0.35,
            },
        )
        self.assertEqual(status, 200, raw)
        self.assertIn("x-request-id", headers)
        records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["outcome"] for record in records], ["AUTHORIZED", "SUCCEEDED"])
        rendered = json.dumps(records, ensure_ascii=False)
        for forbidden in (_STRONG_ACCESS, "120000", "60000", self.repo.db_path):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(route_template("PATCH", "/api/portfolio/positions/private-id"), "/api/portfolio/positions/{position_id}")

    def test_audit_discards_unknown_fields_and_templates_unknown_writes(self) -> None:
        audit_path = Path(self.temp.name) / "bounded.jsonl"
        audit = RemoteAuditLogger(audit_path)
        record = audit.record(
            event="REMOTE_WRITE",
            outcome="AUTHORIZED",
            method="PATCH",
            path="/api/private-symbol/600000.SH",
            client_class="REMOTE_BROWSER",
            request_id="request-fixture",
            status_code=200,
            origin=_ALLOWED_ORIGIN,
            token=_STRONG_ACCESS,
            body={"account_equity": 120000},
            database_path=self.repo.db_path,
        )
        self.assertEqual(record["route"], "/api/{unclassified-write}")
        rendered = audit_path.read_text(encoding="utf-8")
        for forbidden in (_STRONG_ACCESS, "600000.SH", "120000", self.repo.db_path):
            self.assertNotIn(forbidden, rendered)

    def test_audit_rejects_symlink_destination(self) -> None:
        target = Path(self.temp.name) / "real-audit.jsonl"
        target.write_text("", encoding="utf-8")
        link = Path(self.temp.name) / "linked-audit.jsonl"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this host")
        with self.assertRaisesRegex(AuditWriteError, "symlink"):
            RemoteAuditLogger(link).ensure_ready()

    def test_remote_same_origin_proxy_is_not_misclassified_as_loopback(self) -> None:
        audit_path = Path(self.temp.name) / "same-origin-remote.jsonl"
        audit = RemoteAuditLogger(audit_path)
        server = APIServer("127.0.0.1", 0, self.ctx, None, audit_logger=audit)
        remote_origin = "https://fixture.tailnet.ts.net"
        status, raw, _ = self._request(
            server,
            "PUT",
            "/api/portfolio/profile",
            host="fixture.tailnet.ts.net",
            origin=remote_origin,
            body={
                "account_equity": 120000,
                "available_cash": 60000,
                "risk_mode": "BALANCED",
                "per_trade_risk_pct": 0.007,
                "max_position_pct": 0.2,
                "max_portfolio_heat_pct": 0.08,
                "max_sector_pct": 0.35,
                "max_theme_pct": 0.35,
            },
        )
        self.assertEqual(status, 200, raw)
        records = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(records), 2)
        self.assertTrue(
            all(record["client_boundary"] == "REMOTE_BROWSER" for record in records)
        )
        self.assertTrue(all(record["origin"] == remote_origin for record in records))

    def test_remote_write_fails_closed_when_audit_is_unavailable(self) -> None:
        server = APIServer(
            "127.0.0.1",
            0,
            self.ctx,
            None,
            audit_logger=_FailingAudit(),  # type: ignore[arg-type]
        )
        status, raw, _ = self._request(
            server,
            "PUT",
            "/api/portfolio/profile",
            body={
                "account_equity": 120000,
                "available_cash": 60000,
                "risk_mode": "BALANCED",
                "per_trade_risk_pct": 0.007,
                "max_position_pct": 0.2,
                "max_portfolio_heat_pct": 0.08,
                "max_sector_pct": 0.35,
                "max_theme_pct": 0.35,
            },
        )
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw)["error"]["code"], "REMOTE_AUDIT_UNAVAILABLE")
        self.assertIsNone(self.repo.load_portfolio_profile())


class TestHybridH3ServeMigration(unittest.TestCase):
    def _patch_preflight(self, binary: str):
        return mock.patch(
            "stock_tracker.deployment.hybrid_h3.run_preflight",
            return_value=_Preflight(binary),
        )

    def test_inspection_accepts_only_exact_h0_h3_or_empty(self) -> None:
        empty = inspect_target_lane({})
        self.assertEqual(empty.state, "EMPTY")
        for port, expected in ((8080, "WHOLE_SITE"), (8081, "API_TARGET")):
            payload = {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {"fixture.ts.net:443": {"Handlers": {"/": {"Proxy": serve_target(port)}}}},
                "AllowFunnel": {"fixture.ts.net:443": False},
            }
            self.assertEqual(inspect_target_lane(payload).state, expected)
        conflict = {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {"fixture.ts.net:443": {"Handlers": {"/extra": {"Proxy": serve_target(8081)}}}},
            "AllowFunnel": {"fixture.ts.net:443": False},
        }
        self.assertEqual(inspect_target_lane(conflict).state, "CONFLICT")

    def test_migrate_and_rollback_are_exact_and_never_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = str(Path(directory) / "tailscale.exe")
            Path(binary).write_bytes(b"fixture")
            runner = _ServeRunner(serve_target(8080))
            with self._patch_preflight(binary):
                result = migrate_to_api_target(
                    private_access=_STRONG_ACCESS,
                    tailscale_binary=binary,
                    runner=runner,
                )
                self.assertTrue(result["passed"])
                self.assertEqual(runner.target, serve_target(8081))
                rolled = rollback_to_whole_site(
                    private_access=_STRONG_ACCESS,
                    tailscale_binary=binary,
                    runner=runner,
                )
                self.assertTrue(rolled["passed"])
                self.assertEqual(runner.target, serve_target(8080))
            flattened = [part for call in runner.calls for part in call]
            self.assertNotIn("reset", flattened)

    def test_failed_target_enable_restores_whole_site(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = str(Path(directory) / "tailscale.exe")
            Path(binary).write_bytes(b"fixture")
            runner = _ServeRunner(
                serve_target(8080),
                fail_api_enable=True,
                mutate_before_api_failure=True,
            )
            with self._patch_preflight(binary), self.assertRaisesRegex(
                HybridH3Error, "WHOLE_SITE recovery verified"
            ):
                migrate_to_api_target(
                    private_access=_STRONG_ACCESS,
                    tailscale_binary=binary,
                    runner=runner,
                )
            self.assertEqual(runner.target, serve_target(8080))

    def test_recovery_refuses_to_clear_concurrent_conflicting_serve_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = str(Path(directory) / "tailscale.exe")
            Path(binary).write_bytes(b"fixture")
            runner = _ServeRunner(
                serve_target(8080),
                fail_api_enable=True,
                conflict_on_api_failure=True,
            )
            with self._patch_preflight(binary), self.assertRaisesRegex(
                HybridH3Error, "recovery could not be verified"
            ):
                migrate_to_api_target(
                    private_access=_STRONG_ACCESS,
                    tailscale_binary=binary,
                    runner=runner,
                )
            self.assertEqual(runner.target, "http://127.0.0.1:9999")
            serve_off_calls = [call for call in runner.calls if call[1:] == ("serve", "off")]
            self.assertEqual(len(serve_off_calls), 1)

    def test_failed_whole_site_rollback_restores_api_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = str(Path(directory) / "tailscale.exe")
            Path(binary).write_bytes(b"fixture")
            runner = _ServeRunner(serve_target(8081), fail_whole_site_enable=True)
            with self._patch_preflight(binary), self.assertRaisesRegex(
                HybridH3Error, "API_TARGET recovery verified"
            ):
                rollback_to_whole_site(
                    private_access=_STRONG_ACCESS,
                    tailscale_binary=binary,
                    runner=runner,
                )
            self.assertEqual(runner.target, serve_target(8081))


class TestHybridH3OperationalTimezones(unittest.TestCase):
    def test_us_eastern_fallback_tracks_daylight_saving(self) -> None:
        summer_local = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).replace(tzinfo=None)
        winter_local = datetime(2026, 1, 2, 9, 30, tzinfo=timezone.utc).replace(tzinfo=None)
        with mock.patch("stock_tracker.core.timezones.resolve_zoneinfo", return_value=None):
            summer_utc = market_local_to_utc(
                summer_local,
                timezone_name="America/New_York",
                fallback_offset_hours=-5,
            )
            winter_utc = market_local_to_utc(
                winter_local,
                timezone_name="America/New_York",
                fallback_offset_hours=-5,
            )
            summer_round_trip = utc_to_market_local(
                summer_utc,
                timezone_name="America/New_York",
                fallback_offset_hours=-5,
            )
            winter_round_trip = utc_to_market_local(
                winter_utc,
                timezone_name="America/New_York",
                fallback_offset_hours=-5,
            )
        self.assertEqual(summer_utc.hour, 13)
        self.assertEqual(winter_utc.hour, 14)
        self.assertEqual((summer_round_trip.hour, summer_round_trip.minute), (9, 30))
        self.assertEqual((winter_round_trip.hour, winter_round_trip.minute), (9, 30))
        self.assertEqual(summer_round_trip.utcoffset().total_seconds(), -4 * 3600)
        self.assertEqual(winter_round_trip.utcoffset().total_seconds(), -5 * 3600)


class TestHybridH3HostPlans(unittest.TestCase):
    def test_rotation_plan_never_returns_secret_values(self) -> None:
        plan = token_rotation_plan(
            environ={
                PRIVATE_ACCESS_ENV: _STRONG_ACCESS,
                NEW_PRIVATE_ACCESS_ENV: _NEW_ACCESS,
            }
        )
        self.assertTrue(plan["passed"])
        rendered = json.dumps(plan)
        self.assertNotIn(_STRONG_ACCESS, rendered)
        self.assertNotIn(_NEW_ACCESS, rendered)

    def test_windows_task_is_no_secret_and_dry_run_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_windows_task_plan(
                project_root=ROOT,
                output_path=Path(directory) / "task.xml",
                python_executable=sys.executable,
                user_id="TEST\\user",
            )
            self.assertIn("RestartOnFailure", plan.xml)
            self.assertIn("127.0.0.1", plan.xml)
            self.assertNotIn(PRIVATE_ACCESS_ENV, plan.xml)
            calls: list[tuple[str, ...]] = []

            def runner(argv: tuple[str, ...], timeout_sec: float) -> CommandResult:
                del timeout_sec
                calls.append(argv)
                return CommandResult(argv, 0, "ok", "")

            dry = apply_windows_task_plan(plan, runner=runner)
            self.assertFalse(dry["applied"])
            self.assertEqual(calls, [])
            applied = apply_windows_task_plan(
                plan,
                apply=True,
                acknowledge_host_change=True,
                runner=runner,
            )
            self.assertTrue(applied["applied"])
            self.assertEqual(len(calls), 1)

    def test_power_guard_uses_market_timezone_and_releases(self) -> None:
        bundle = SimpleNamespace(
            app=SimpleNamespace(markets_enabled={"a": True, "hk": False, "us": False}),
            markets=SimpleNamespace(
                a=SimpleNamespace(timezone="UTC", utc_offset_hours=0, trading_hours=[[0, 0, 23, 59]]),
                hk=None,
                us=None,
            ),
        )
        monday = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
        sunday = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(active_trading_markets(bundle, now_utc=monday), ("A",))
        self.assertEqual(active_trading_markets(bundle, now_utc=sunday), ())
        calls: list[int] = []
        guard = TradingPowerGuard(
            bundle,
            None,
            enabled=True,
            interval_sec=60,
            platform_name="nt",
            set_state=lambda flags: calls.append(flags) or flags,
        )
        guard.tick(now_utc=monday)
        guard.tick(now_utc=sunday)
        self.assertEqual(calls, [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS])


if __name__ == "__main__":
    unittest.main()
