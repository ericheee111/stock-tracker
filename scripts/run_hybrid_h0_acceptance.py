"""Run Hybrid H0 local, server, or second-device acceptance.

The server fixture always uses a temporary SQLite database and copied static
assets. Bearer access is read only from STOCK_TRACKER_PRIVATE_ACCESS; no command
line or evidence output contains the secret.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tracker.deployment.h0_acceptance import (
    HybridH0AcceptanceError,
    TemporaryH0Fixture,
    validate_tailnet_serve_origin,
    verify_h0_acceptance,
)
from stock_tracker.deployment.hybrid_h0 import (
    HybridH0Error,
    disable_serve,
    enable_serve,
    private_access_from_environment,
    tailscale_identity,
)

_REMOTE_STYLE_HOST = "stock-tracker-h0.tailnet.invalid"
_REMOTE_STYLE_IP = "100.64.0.9"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hybrid H0 acceptance runner")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    local = subparsers.add_parser(
        "local",
        help="Temporary-DB remote-style simulation; no Tailscale or production DB writes",
    )
    local.add_argument("--timeout-sec", type=float, default=8.0)
    local.add_argument("--json-output")
    local.add_argument(
        "--production-database",
        default=str(ROOT / "data" / "stock_tracker.db"),
        help="Optional production DB path hashed before/after; never opened as SQLite",
    )

    server = subparsers.add_parser(
        "server",
        help="Start a temporary acceptance Engine; optionally expose it via Tailscale Serve",
    )
    server.add_argument("--enable-serve", action="store_true")
    server.add_argument("--tailscale-binary")
    server.add_argument("--timeout-sec", type=float, default=15.0)
    server.add_argument("--duration-sec", type=float, default=0.0)
    server.add_argument("--json-output", help="Optional fixture descriptor path")
    server.add_argument(
        "--keep-serve-on-exit",
        action="store_true",
        help="Do not disable the exact H0 Serve target when the fixture exits",
    )

    client = subparsers.add_parser(
        "client",
        help="Run REST/SSE/Portfolio CRUD from a second Tailnet device",
    )
    client.add_argument("--base-url", required=True)
    client.add_argument("--fixture-id", required=True)
    client.add_argument("--timeout-sec", type=float, default=12.0)
    client.add_argument("--tailscale-binary")
    client.add_argument("--json-output")
    client.add_argument(
        "--allow-same-device",
        action="store_true",
        help="Diagnostic only; cannot satisfy physical two-device acceptance",
    )

    return parser


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render(result: dict[str, object], output_path: str | None) -> None:
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered, flush=True)
    if output_path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(rendered + "\n", encoding="utf-8")
        os.replace(temporary, output)


def _local(args: argparse.Namespace) -> int:
    database = Path(args.production_database)
    before = _sha256(database)
    with TemporaryH0Fixture(
        server_hostname="hybrid-h0-local-server-fixture",
    ) as fixture:
        assert fixture.base_url is not None
        report = verify_h0_acceptance(
            base_url=fixture.base_url,
            fixture_id=fixture.fixture_id,
            private_access=fixture.private_access,
            scope="LOCAL_REMOTE_STYLE_SIMULATION",
            require_distinct_devices=False,
            client_hostname="hybrid-h0-local-client-simulation",
            timeout_sec=args.timeout_sec,
            host_header=_REMOTE_STYLE_HOST,
            forwarded_for=_REMOTE_STYLE_IP,
        )
    after = _sha256(database)
    result = report.as_dict()
    result.update(
        {
            "operational_device_acceptance": "PENDING",
            "production_database_path": str(database),
            "production_database_sha256_before": before,
            "production_database_sha256_after": after,
            "production_database_hash_equal": before == after,
        }
    )
    _render(result, args.json_output)
    return 0 if report.passed and before == after else 1


def _server(args: argparse.Namespace) -> int:
    access = private_access_from_environment()
    serve_enabled = False
    serve_port: int | None = None
    effective_tailscale_binary = args.tailscale_binary
    dns_name: str | None = None
    server_node_id: str | None = None
    if args.enable_serve:
        identity = tailscale_identity(
            tailscale_binary=args.tailscale_binary,
            timeout_sec=args.timeout_sec,
        )
        effective_tailscale_binary = identity["tailscale_binary"]
        dns_name = identity["tailscale_dns_name"]
        server_node_id = identity["tailscale_node_id"]
        if not isinstance(dns_name, str) or not dns_name:
            raise HybridH0Error("Tailscale status did not provide a usable DNS name")
        if not isinstance(server_node_id, str) or not server_node_id:
            raise HybridH0Error("Tailscale status did not provide a stable node identity")

    with TemporaryH0Fixture(
        private_access=access,
        server_tailscale_node_id=server_node_id,
    ) as fixture:
        assert fixture.base_url is not None
        assert fixture.port is not None
        serve_port = fixture.port
        base_url = fixture.base_url
        enable_result: dict[str, object] | None = None
        try:
            if args.enable_serve:
                enable_result = enable_serve(
                    port=fixture.port,
                    private_access=access,
                    tailscale_binary=effective_tailscale_binary,
                    timeout_sec=args.timeout_sec,
                )
                serve_enabled = True
                assert dns_name is not None
                base_url = validate_tailnet_serve_origin(f"https://{dns_name}")

            descriptor: dict[str, object] = {
                "schema": "stock-tracker-hybrid-h0-server-fixture-v1",
                "fixture_id": fixture.fixture_id,
                "server_hostname": fixture.server_hostname,
                "server_tailscale_node_id": server_node_id,
                "base_url": base_url,
                "local_engine_target": fixture.base_url,
                "temporary_database": True,
                "production_database": False,
                "serve_enabled": serve_enabled,
                "contains_private_access": False,
                "required_client_command": (
                    "python scripts/run_hybrid_h0_acceptance.py client "
                    f"--base-url {base_url} --fixture-id {fixture.fixture_id}"
                ),
            }
            if enable_result is not None:
                descriptor["serve"] = {
                    "changed": enable_result.get("changed"),
                    "tailscale_dns_name": dns_name,
                    "server_tailscale_node_id": server_node_id,
                    "target": fixture.base_url,
                }
            _render(descriptor, args.json_output)

            if not math.isfinite(args.duration_sec) or args.duration_sec < 0:
                raise HybridH0AcceptanceError("duration-sec must be a finite number >= 0")
            if args.duration_sec > 0:
                threading.Event().wait(args.duration_sec)
            else:
                print("Hybrid H0 fixture is running; press Ctrl+C after the second-device client finishes.")
                try:
                    while True:
                        threading.Event().wait(3600)
                except KeyboardInterrupt:
                    pass
            return 0
        finally:
            if serve_enabled and not args.keep_serve_on_exit and serve_port is not None:
                disabled = disable_serve(
                    port=serve_port,
                    tailscale_binary=effective_tailscale_binary,
                    timeout_sec=args.timeout_sec,
                )
                print(
                    json.dumps(
                        {"serve_cleanup": disabled, "contains_private_access": False},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )


def _client(args: argparse.Namespace) -> int:
    base_url = args.base_url
    if not args.allow_same_device:
        base_url = validate_tailnet_serve_origin(base_url)
    access = private_access_from_environment()
    client_node_id: str | None = None
    if not args.allow_same_device:
        identity = tailscale_identity(
            tailscale_binary=args.tailscale_binary,
            timeout_sec=args.timeout_sec,
        )
        raw_node_id = identity["tailscale_node_id"]
        if not isinstance(raw_node_id, str) or not raw_node_id:
            raise HybridH0Error("Tailscale status did not provide a stable client node identity")
        client_node_id = raw_node_id
    report = verify_h0_acceptance(
        base_url=base_url,
        fixture_id=args.fixture_id,
        private_access=access,
        scope=(
            "SAME_DEVICE_DIAGNOSTIC"
            if args.allow_same_device
            else "TAILNET_TWO_DEVICE_ACCEPTANCE"
        ),
        require_distinct_devices=not args.allow_same_device,
        client_hostname=socket.gethostname(),
        client_tailscale_node_id=client_node_id,
        timeout_sec=args.timeout_sec,
    )
    result = report.as_dict()
    result["operational_device_acceptance"] = (
        "PASSED" if report.passed and not args.allow_same_device else "DIAGNOSTIC_ONLY"
    )
    _render(result, args.json_output)
    return 0 if report.passed else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.mode == "local":
            return _local(args)
        if args.mode == "server":
            return _server(args)
        return _client(args)
    except (HybridH0Error, HybridH0AcceptanceError, OSError, ValueError) as exc:
        error = {
            "schema": "stock-tracker-hybrid-h0-acceptance-error-v1",
            "passed": False,
            "error": {"code": "HYBRID_H0_ACCEPTANCE_FAILED", "message": str(exc)},
            "contains_private_access": False,
        }
        _render(error, getattr(args, "json_output", None))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
