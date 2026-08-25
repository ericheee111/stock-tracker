from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from stock_tracker import __main__ as entrypoint
from stock_tracker.api.server import APIServer
from stock_tracker.cli import parse_args
from stock_tracker.core.config import ConfigError, load_app, load_configs
from stock_tracker.core.network import (
    UnsafeBindError,
    is_loopback_host,
    require_safe_bind,
)
from stock_tracker.core.security import PRIVATE_ACCESS_ENV, private_access_value_valid
from stock_tracker.deployment.h0_acceptance import (
    HybridH0AcceptanceError,
    TemporaryH0Fixture,
    validate_tailnet_serve_origin,
    verify_h0_acceptance,
)
from stock_tracker.deployment.hybrid_h0 import (
    CommandResult,
    HybridH0Error,
    build_serve_disable_command,
    build_serve_enable_command,
    disable_serve,
    enable_serve,
    private_access_from_environment,
    serve_target,
)
from stock_tracker.storage.db import close_all
from stock_tracker.storage.repository import Repository

ROOT = Path(__file__).resolve().parents[1]
_STRONG_ACCESS = "hybrid-h0-private-access-0123456789abcdef"


def _load_start_script():
    spec = importlib.util.spec_from_file_location(
        "stock_tracker_h0_start_script",
        ROOT / "scripts" / "start.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeTailscaleRunner:
    def __init__(
        self,
        target: str,
        *,
        initial_target: str | None = None,
        funnel_enabled: object = False,
        extra_mount: bool = False,
    ) -> None:
        self.target = initial_target
        self.expected_target = target
        self.funnel_enabled = funnel_enabled
        self.extra_mount = extra_mount
        self.calls: list[tuple[str, ...]] = []

    def _serve_payload(self) -> str:
        if self.target is None:
            return "{}"
        handlers = {"/": {"Proxy": self.target}}
        if self.extra_mount:
            handlers["/extra"] = {"Proxy": self.target}
        return json.dumps(
            {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "fixture.tailnet.ts.net:443": {
                        "Handlers": handlers,
                    }
                },
                "AllowFunnel": {
                    "fixture.tailnet.ts.net:443": self.funnel_enabled,
                },
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
                            "StableID": "node-server-fixture",
                        },
                    }
                ),
                "",
            )
        if suffix == ("serve", "status", "--json"):
            return CommandResult(argv, 0, self._serve_payload(), "")
        if suffix == ("serve", "--bg", self.expected_target):
            self.target = self.expected_target
            return CommandResult(argv, 0, "Available within your tailnet", "")
        if suffix == ("serve", "off"):
            self.target = None
            return CommandResult(argv, 0, "Serve disabled", "")
        return CommandResult(argv, 2, "", f"unexpected command: {suffix}")


class TestHybridH0Binding(unittest.TestCase):
    def test_repository_default_and_committed_config_are_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = load_app(str(Path(directory) / "missing.toml"), directory)
        committed = load_configs(str(ROOT / "config"))
        self.assertEqual(missing.server.host, "127.0.0.1")
        self.assertEqual(committed.app.server.host, "127.0.0.1")

    def test_server_host_and_port_use_strict_types_and_bounds(self) -> None:
        documents = (
            ("[server]\nhost = 1\n", "server.host"),
            ('[server]\nhost = ""\n', "server.host"),
            ('[server]\nhost = " 127.0.0.1"\n', "server.host"),
            ("[server]\nport = true\n", "server.port"),
            ("[server]\nport = 0\n", "server.port"),
            ("[server]\nport = 65536\n", "server.port"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, (document, field) in enumerate(documents):
                with self.subTest(document=document):
                    path = Path(directory) / f"app-{index}.toml"
                    path.write_text(document, encoding="utf-8")
                    with self.assertRaisesRegex(ConfigError, field):
                        load_app(str(path), directory)

    def test_non_loopback_bind_requires_explicit_actual_boolean(self) -> None:
        for host in ("127.0.0.1", "127.0.0.2", "::1", "localhost"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))
                self.assertEqual(require_safe_bind(host, allow_non_loopback=False), host)
        for host in ("0.0.0.0", "192.168.1.10", "stock.example"):
            with self.subTest(host=host):
                with self.assertRaises(UnsafeBindError):
                    require_safe_bind(host, allow_non_loopback=False)
                self.assertEqual(require_safe_bind(host, allow_non_loopback=True), host)
        with self.assertRaisesRegex(UnsafeBindError, "actual boolean"):
            require_safe_bind("0.0.0.0", allow_non_loopback=1)  # type: ignore[arg-type]

    def test_cli_and_local_start_make_the_risk_acknowledgement_explicit(self) -> None:
        self.assertFalse(parse_args([]).allow_non_loopback)
        args = parse_args(["--host", "0.0.0.0", "--allow-non-loopback"])
        self.assertTrue(args.allow_non_loopback)
        start_script = _load_start_script()
        command = start_script.build_start_command(9090)
        self.assertIn("--host", command)
        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
        self.assertNotIn("--allow-non-loopback", command)

    def test_build_context_refuses_public_bind_before_creating_runtime_state(self) -> None:
        bundle = SimpleNamespace(
            app=SimpleNamespace(
                server=SimpleNamespace(host="0.0.0.0", port=8080),
                logging=SimpleNamespace(),
            )
        )
        args = SimpleNamespace(
            config_dir=str(ROOT / "config"),
            host=None,
            port=None,
            allow_non_loopback=False,
        )
        with (
            mock.patch.object(entrypoint, "load_configs", return_value=bundle),
            mock.patch.object(entrypoint, "setup_logging", return_value=mock.Mock()),
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaises(UnsafeBindError),
        ):
            entrypoint.build_context(args)

    def test_http_server_boundary_also_rejects_public_bind(self) -> None:
        with self.assertRaises(UnsafeBindError):
            APIServer("0.0.0.0", 0, None, None)  # type: ignore[arg-type]

    def test_cloud_experiment_and_docker_context_are_explicit_and_private(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        procfile = (ROOT / "Procfile").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for content in (dockerfile, procfile):
            self.assertIn("0.0.0.0", content)
            self.assertIn("--allow-non-loopback", content)
        for required in ("/data/", "/build/", "*.zip", "**/__pycache__/", ".ai-bridge/"):
            self.assertIn(required, dockerignore)


class TestHybridH0ServeOperations(unittest.TestCase):
    def test_commands_are_locked_to_loopback_and_never_use_reset(self) -> None:
        enable = build_serve_enable_command("tailscale", 8080)
        disable = build_serve_disable_command("tailscale")
        self.assertEqual(
            enable,
            ("tailscale", "serve", "--bg", "http://127.0.0.1:8080"),
        )
        self.assertEqual(disable, ("tailscale", "serve", "off"))
        self.assertNotIn("reset", enable + disable)
        self.assertEqual(serve_target(443), "http://127.0.0.1:443")
        for invalid in (True, 0, 65536, "8080"):
            with self.subTest(invalid=invalid), self.assertRaises(HybridH0Error):
                serve_target(invalid)  # type: ignore[arg-type]

    def test_private_access_is_environment_only_and_strict(self) -> None:
        self.assertTrue(private_access_value_valid(_STRONG_ACCESS))
        self.assertFalse(private_access_value_valid("short"))
        self.assertFalse(private_access_value_valid(_STRONG_ACCESS + " "))
        with self.assertRaises(HybridH0Error):
            private_access_from_environment(environ={})
        self.assertEqual(
            private_access_from_environment(environ={PRIVATE_ACCESS_ENV: _STRONG_ACCESS}),
            _STRONG_ACCESS,
        )

    def test_enable_and_disable_are_idempotent_and_owned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "tailscale.exe"
            binary.write_bytes(b"fixture")
            target = serve_target(8080)
            runner = _FakeTailscaleRunner(target)
            with TemporaryH0Fixture(
                private_access=_STRONG_ACCESS,
                server_hostname="fixture-server",
            ) as fixture:
                assert fixture.port is not None
                target = serve_target(fixture.port)
                runner.expected_target = target
                enabled = enable_serve(
                    port=fixture.port,
                    private_access=_STRONG_ACCESS,
                    tailscale_binary=str(binary),
                    runner=runner,
                )
                self.assertTrue(enabled["passed"])
                self.assertTrue(enabled["changed"])
                self.assertEqual(runner.target, target)

                second = enable_serve(
                    port=fixture.port,
                    private_access=_STRONG_ACCESS,
                    tailscale_binary=str(binary),
                    runner=runner,
                )
                self.assertFalse(second["changed"])

                disabled = disable_serve(
                    port=fixture.port,
                    tailscale_binary=str(binary),
                    runner=runner,
                )
                self.assertTrue(disabled["changed"])
                self.assertIsNone(runner.target)
                no_op = disable_serve(
                    port=fixture.port,
                    tailscale_binary=str(binary),
                    runner=runner,
                )
                self.assertFalse(no_op["changed"])

    def test_existing_different_serve_backend_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "tailscale.exe"
            binary.write_bytes(b"fixture")
            with TemporaryH0Fixture(
                private_access=_STRONG_ACCESS,
                server_hostname="fixture-server",
            ) as fixture:
                assert fixture.port is not None
                runner = _FakeTailscaleRunner(
                    serve_target(fixture.port),
                    initial_target="http://127.0.0.1:9999",
                )
                with self.assertRaisesRegex(HybridH0Error, "conflict|not owned"):
                    enable_serve(
                        port=fixture.port,
                        private_access=_STRONG_ACCESS,
                        tailscale_binary=str(binary),
                        runner=runner,
                    )
                self.assertNotIn(
                    (str(binary), "serve", "--bg", serve_target(fixture.port)),
                    runner.calls,
                )

    def test_funnel_or_extra_mount_never_counts_as_owned_h0_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "tailscale.exe"
            binary.write_bytes(b"fixture")
            with TemporaryH0Fixture(
                private_access=_STRONG_ACCESS,
                server_hostname="fixture-server",
            ) as fixture:
                assert fixture.port is not None
                target = serve_target(fixture.port)
                for runner in (
                    _FakeTailscaleRunner(
                        target,
                        initial_target=target,
                        funnel_enabled=True,
                    ),
                    _FakeTailscaleRunner(
                        target,
                        initial_target=target,
                        extra_mount=True,
                    ),
                    _FakeTailscaleRunner(
                        target,
                        initial_target=target,
                        funnel_enabled="false",
                    ),
                ):
                    with self.subTest(runner=runner), self.assertRaises(HybridH0Error):
                        disable_serve(
                            port=fixture.port,
                            tailscale_binary=str(binary),
                            runner=runner,
                        )
                    self.assertNotIn((str(binary), "serve", "off"), runner.calls)


class TestHybridH0Acceptance(unittest.TestCase):
    def tearDown(self) -> None:
        close_all()

    def test_real_client_origin_is_locked_to_https_ts_net(self) -> None:
        self.assertEqual(
            validate_tailnet_serve_origin("https://server.tailnet.ts.net/"),
            "https://server.tailnet.ts.net",
        )
        self.assertEqual(
            validate_tailnet_serve_origin("https://SERVER.TAILNET.TS.NET:443"),
            "https://server.tailnet.ts.net",
        )
        for invalid in (
            "http://server.tailnet.ts.net",
            "https://example.com",
            "https://server.tailnet.ts.net:8443",
            "https://server.tailnet.ts.net/api",
            "https://user@server.tailnet.ts.net",
            " https://server.tailnet.ts.net",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(HybridH0AcceptanceError):
                validate_tailnet_serve_origin(invalid)

    def test_remote_style_rest_sse_and_portfolio_crud_use_only_temp_database(self) -> None:
        with TemporaryH0Fixture(
            private_access=_STRONG_ACCESS,
            server_hostname="acceptance-server",
        ) as fixture:
            assert fixture.base_url is not None
            assert fixture.database_path is not None
            report = verify_h0_acceptance(
                base_url=fixture.base_url,
                fixture_id=fixture.fixture_id,
                private_access=_STRONG_ACCESS,
                scope="TEST_REMOTE_STYLE_SIMULATION",
                require_distinct_devices=False,
                client_hostname="acceptance-client",
                host_header="fixture.tailnet.invalid",
                forwarded_for="100.64.0.50",
            )
            self.assertTrue(report.passed, report.as_dict())
            rendered = json.dumps(report.as_dict(), sort_keys=True)
            self.assertNotIn(_STRONG_ACCESS, rendered)
            self.assertFalse(report.as_dict()["production_database_modified"])
            self.assertEqual(Repository(fixture.database_path).load_positions(), [])

    def test_marker_mismatch_or_same_device_blocks_all_writes(self) -> None:
        with TemporaryH0Fixture(
            private_access=_STRONG_ACCESS,
            server_hostname="same-device",
            server_tailscale_node_id="node-same-device",
        ) as fixture:
            assert fixture.base_url is not None
            assert fixture.database_path is not None
            wrong = verify_h0_acceptance(
                base_url=fixture.base_url,
                fixture_id="0" * 32,
                private_access=_STRONG_ACCESS,
                scope="NEGATIVE",
                require_distinct_devices=False,
                client_hostname="client",
                host_header="fixture.tailnet.invalid",
                forwarded_for="100.64.0.50",
            )
            self.assertFalse(wrong.passed)
            same = verify_h0_acceptance(
                base_url=fixture.base_url,
                fixture_id=fixture.fixture_id,
                private_access=_STRONG_ACCESS,
                scope="NEGATIVE",
                require_distinct_devices=True,
                client_hostname="same-device",
                client_tailscale_node_id="node-same-device",
                host_header="fixture.tailnet.invalid",
                forwarded_for="100.64.0.50",
            )
            self.assertFalse(same.passed)
            repository = Repository(fixture.database_path)
            self.assertIsNone(repository.load_portfolio_profile())
            self.assertEqual(repository.load_positions(), [])

    def test_distinct_tailscale_node_ids_are_required_for_device_acceptance(self) -> None:
        with TemporaryH0Fixture(
            private_access=_STRONG_ACCESS,
            server_hostname="server-host",
            server_tailscale_node_id="node-server",
        ) as fixture:
            assert fixture.base_url is not None
            report = verify_h0_acceptance(
                base_url=fixture.base_url,
                fixture_id=fixture.fixture_id,
                private_access=_STRONG_ACCESS,
                scope="TAILNET_TWO_DEVICE_ACCEPTANCE_TEST",
                require_distinct_devices=True,
                client_hostname="client-host",
                client_tailscale_node_id="node-client",
                host_header="fixture.tailnet.invalid",
                forwarded_for="100.64.0.50",
            )
            self.assertTrue(report.passed, report.as_dict())
            self.assertTrue(report.device_distinct)
            self.assertEqual(report.server_tailscale_node_id, "node-server")
            self.assertEqual(report.client_tailscale_node_id, "node-client")


if __name__ == "__main__":
    unittest.main()
