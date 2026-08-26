#!/usr/bin/env python3
"""Operate Hybrid H3 target-lane and host-recovery contracts.

Bearer values are accepted only through process environment variables. Host
mutations are dry-run by default and require explicit acknowledgement flags.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_tracker.deployment.hybrid_h0 import HybridH0Error
from stock_tracker.deployment.hybrid_h3 import (
    DEFAULT_API_TARGET_PORT,
    DEFAULT_TASK_NAME,
    DEFAULT_WHOLE_SITE_PORT,
    HybridH3Error,
    apply_windows_task_plan,
    build_windows_task_plan,
    migrate_to_api_target,
    remove_windows_task,
    rollback_to_whole_site,
    run_preflight,
    target_lane_status,
    token_rotation_plan,
)


def _add_tailscale_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tailscale-binary")
    parser.add_argument("--timeout-sec", type=float, default=15.0)
    parser.add_argument("--whole-site-port", type=int, default=DEFAULT_WHOLE_SITE_PORT)
    parser.add_argument("--api-target-port", type=int, default=DEFAULT_API_TARGET_PORT)
    parser.add_argument("--json-output")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Hybrid H3 API Target, rotation, and Windows recovery tooling. "
            "Set STOCK_TRACKER_PRIVATE_ACCESS in the process environment."
        )
    )
    sub = parser.add_subparsers(dest="action", required=True)

    preflight = sub.add_parser("preflight", help="Read-only API-target and Tailscale checks")
    _add_tailscale_common(preflight)

    status = sub.add_parser("status", help="Read exact H0/H3 Serve ownership")
    _add_tailscale_common(status)

    migrate = sub.add_parser("migrate-target", help="Move exact H0 Serve to API-only H3 target")
    _add_tailscale_common(migrate)

    rollback = sub.add_parser("rollback-target", help="Restore exact H0 whole-site Serve target")
    _add_tailscale_common(rollback)

    rotation = sub.add_parser(
        "token-rotation-plan",
        help="Validate current/new environment values without printing either secret",
    )
    rotation.add_argument("--json-output")

    task = sub.add_parser("task-plan", help="Generate a no-secret Windows Task Scheduler XML")
    task.add_argument(
        "--output",
        default=str(ROOT / "build" / "hybrid-h3" / "stock-tracker-task.xml"),
    )
    task.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    task.add_argument("--python-executable", default=sys.executable)
    task.add_argument("--user-id")
    task.add_argument("--apply", action="store_true")
    task.add_argument("--acknowledge-host-change", action="store_true")
    task.add_argument("--timeout-sec", type=float, default=30.0)
    task.add_argument("--json-output")

    remove = sub.add_parser("task-remove", help="Remove the generated Windows task")
    remove.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    remove.add_argument("--apply", action="store_true")
    remove.add_argument("--acknowledge-host-change", action="store_true")
    remove.add_argument("--timeout-sec", type=float, default=30.0)
    remove.add_argument("--json-output")
    return parser


def _write(result: dict[str, object], output_path: str | None) -> None:
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if output_path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "preflight":
            result = run_preflight(
                port=args.api_target_port,
                tailscale_binary=args.tailscale_binary,
                timeout_sec=args.timeout_sec,
            ).as_dict()
            result["schema"] = "stock-tracker-hybrid-h3-preflight-v1"
            result["whole_site_target"] = f"http://127.0.0.1:{args.whole_site_port}"
            result["api_target"] = f"http://127.0.0.1:{args.api_target_port}"
        elif args.action == "status":
            result = target_lane_status(
                whole_site_port=args.whole_site_port,
                api_target_port=args.api_target_port,
                tailscale_binary=args.tailscale_binary,
                timeout_sec=args.timeout_sec,
            )
        elif args.action == "migrate-target":
            result = migrate_to_api_target(
                whole_site_port=args.whole_site_port,
                api_target_port=args.api_target_port,
                tailscale_binary=args.tailscale_binary,
                timeout_sec=args.timeout_sec,
            )
        elif args.action == "rollback-target":
            result = rollback_to_whole_site(
                whole_site_port=args.whole_site_port,
                api_target_port=args.api_target_port,
                tailscale_binary=args.tailscale_binary,
                timeout_sec=args.timeout_sec,
            )
        elif args.action == "token-rotation-plan":
            result = token_rotation_plan()
        elif args.action == "task-plan":
            plan = build_windows_task_plan(
                project_root=ROOT,
                output_path=args.output,
                python_executable=args.python_executable,
                task_name=args.task_name,
                user_id=args.user_id,
            )
            result = apply_windows_task_plan(
                plan,
                apply=args.apply,
                acknowledge_host_change=args.acknowledge_host_change,
                timeout_sec=args.timeout_sec,
            )
        else:
            result = remove_windows_task(
                task_name=args.task_name,
                apply=args.apply,
                acknowledge_host_change=args.acknowledge_host_change,
                timeout_sec=args.timeout_sec,
            )
    except (HybridH0Error, HybridH3Error, OSError, ValueError, TypeError) as exc:
        result = {
            "schema": "stock-tracker-hybrid-h3-cli-error-v1",
            "passed": False,
            "contains_private_access": False,
            "error": {"code": "HYBRID_H3_FAILED", "message": str(exc)},
        }
        _write(result, getattr(args, "json_output", None))
        return 2

    _write(result, getattr(args, "json_output", None))
    return 0 if bool(result.get("passed", True)) else 2


if __name__ == "__main__":
    raise SystemExit(main())
