"""Hybrid H3 target-lane, recovery, and host-operation contracts.

This module keeps all host mutations explicit. Read-only status and plan
operations are the default; Tailscale Serve or Windows Task Scheduler changes
require a caller-provided acknowledgement and an ``apply`` flag.
"""

from __future__ import annotations

import getpass
import http.client
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from ..core.security import PRIVATE_ACCESS_ENV, private_access_value_valid
from .hybrid_h0 import (
    CommandResult,
    CommandRunner,
    H0PreflightReport,
    HybridH0Error,
    build_serve_disable_command,
    build_serve_enable_command,
    default_command_runner,
    inspect_serve_config,
    read_serve_status,
    resolve_tailscale_binary,
    run_command_checked,
    serve_target,
)
from .hybrid_h0 import run_preflight as run_h0_preflight

DEFAULT_WHOLE_SITE_PORT = 8080
DEFAULT_API_TARGET_PORT = 8081
NEW_PRIVATE_ACCESS_ENV = "STOCK_TRACKER_NEW_PRIVATE_ACCESS"
DEFAULT_TASK_NAME = "Stock Tracker Hybrid Private"
_WINDOWS_TASK_SCHEMA = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_REMOTE_PROBE_HOST = "stock-tracker-h3.tailnet.invalid"
_REMOTE_PROBE_IP = "100.64.0.3"


class HybridH3Error(RuntimeError):
    """Raised when H3 cannot prove a safe target or host operation."""


@dataclass(frozen=True, slots=True)
class H3Check:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class H3PreflightReport:
    h0: H0PreflightReport
    api_target: str
    checks: tuple[H3Check, ...]

    @property
    def tailscale_binary(self) -> str:
        return self.h0.tailscale_binary

    @property
    def passed(self) -> bool:
        return self.h0.passed and all(check.passed for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "stock-tracker-hybrid-h3-preflight-v1",
            "passed": self.passed,
            "api_target": self.api_target,
            "tailscale_binary": self.h0.tailscale_binary,
            "tailscale_version": self.h0.tailscale_version,
            "tailscale_dns_name": self.h0.tailscale_dns_name,
            "tailscale_node_id": self.h0.tailscale_node_id,
            "h0_checks": [check.as_dict() for check in self.h0.checks],
            "h3_checks": [check.as_dict() for check in self.checks],
            "contains_private_access": False,
        }


@dataclass(frozen=True, slots=True)
class ServeTargetInspection:
    state: str
    expected_target: str
    whole_site_target: str
    conflicts: tuple[str, ...]

    @property
    def safe(self) -> bool:
        return self.state in {"EMPTY", "WHOLE_SITE", "API_TARGET"}

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "expected_target": self.expected_target,
            "whole_site_target": self.whole_site_target,
            "conflicts": list(self.conflicts),
            "safe": self.safe,
        }


@dataclass(frozen=True, slots=True)
class WindowsTaskPlan:
    task_name: str
    xml_path: str
    xml: str
    install_command: tuple[str, ...]
    remove_command: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "stock-tracker-hybrid-h3-windows-task-plan-v1",
            "task_name": self.task_name,
            "xml_path": self.xml_path,
            "install_command": list(self.install_command),
            "remove_command": list(self.remove_command),
            "contains_private_access": False,
        }


HostRunner = Callable[[tuple[str, ...], float], CommandResult]


def _validate_port(port: object, name: str) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise HybridH3Error(f"{name} must be an integer in 1..65535")
    return port


def _validate_timeout(timeout_sec: object) -> float:
    if (
        type(timeout_sec) not in (int, float)
        or not math.isfinite(float(timeout_sec))
        or float(timeout_sec) <= 0
    ):
        raise HybridH3Error("timeout_sec must be a positive finite number")
    return float(timeout_sec)


def _probe_api_target(
    port: int,
    *,
    timeout_sec: float,
) -> tuple[H3Check, ...]:
    """Prove that the H3 loopback listener is API-only before Serve mutation."""

    validated_port = _validate_port(port, "api_target_port")
    timeout = _validate_timeout(timeout_sec)

    def request(path: str) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            validated_port,
            timeout=timeout,
        )
        try:
            connection.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", _REMOTE_PROBE_HOST)
            connection.putheader("X-Forwarded-For", _REMOTE_PROBE_IP)
            connection.putheader("Accept", "application/json")
            connection.putheader("Connection", "close")
            connection.endheaders()
            response = connection.getresponse()
            body = response.read(1024 * 1024)
            headers = {name.lower(): value for name, value in response.getheaders()}
            return response.status, headers, body
        except OSError as exc:
            raise HybridH3Error(
                f"H3 API target is not reachable at {serve_target(validated_port)}"
            ) from exc
        finally:
            connection.close()

    root_status, root_headers, root_body = request("/")
    health_status, health_headers, health_body = request("/api/runtime/health")
    try:
        health_payload = json.loads(health_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        health_payload = None
    health_valid = (
        isinstance(health_payload, dict)
        and health_payload.get("schema_version") == "hybrid-runtime-v1"
        and health_payload.get("deployment_mode") == "HYBRID_PRIVATE"
    )
    return (
        H3Check(
            "api_target_static_root_is_absent",
            root_status == 404 and root_body == b"Not Found",
            f"HTTP {root_status}; bytes={len(root_body)}",
        ),
        H3Check(
            "api_target_runtime_health_is_public_metadata",
            health_status == 200
            and health_valid
            and health_headers.get("cache-control") == "no-store",
            (
                f"HTTP {health_status}; "
                f"schema={health_payload.get('schema_version') if isinstance(health_payload, dict) else None}"
            ),
        ),
        H3Check(
            "api_target_request_ids_present",
            bool(root_headers.get("x-request-id"))
            and bool(health_headers.get("x-request-id")),
            "root and health response IDs checked",
        ),
    )


def run_preflight(
    *,
    port: int = DEFAULT_API_TARGET_PORT,
    private_access: str | None = None,
    tailscale_binary: str | None = None,
    runner: CommandRunner = default_command_runner,
    timeout_sec: float = 10.0,
    environ: Mapping[str, str] | None = None,
) -> H3PreflightReport:
    """Verify H0 prerequisites plus the API-only H3 listener contract."""

    try:
        h0_report = run_h0_preflight(
            port=port,
            private_access=private_access,
            tailscale_binary=tailscale_binary,
            runner=runner,
            timeout_sec=timeout_sec,
            environ=environ,
        )
    except HybridH0Error as exc:
        raise HybridH3Error(str(exc)) from exc
    checks = _probe_api_target(port, timeout_sec=timeout_sec)
    report = H3PreflightReport(
        h0=h0_report,
        api_target=serve_target(port),
        checks=checks,
    )
    if not report.passed:
        failed = ", ".join(check.name for check in checks if not check.passed)
        raise HybridH3Error(f"Hybrid H3 preflight failed: {failed}")
    return report


def inspect_target_lane(
    payload: object,
    *,
    whole_site_port: int = DEFAULT_WHOLE_SITE_PORT,
    api_target_port: int = DEFAULT_API_TARGET_PORT,
) -> ServeTargetInspection:
    """Classify exact H0/H3 ownership without accepting partial matches."""

    whole_target = serve_target(_validate_port(whole_site_port, "whole_site_port"))
    api_target = serve_target(_validate_port(api_target_port, "api_target_port"))
    if whole_target == api_target:
        raise HybridH3Error("whole-site and API target ports must differ")
    if payload in ({}, [], None):
        return ServeTargetInspection("EMPTY", api_target, whole_target, ())

    api_present, api_conflicts = inspect_serve_config(payload, api_target)
    if api_present and not api_conflicts:
        return ServeTargetInspection("API_TARGET", api_target, whole_target, ())

    whole_present, whole_conflicts = inspect_serve_config(payload, whole_target)
    if whole_present and not whole_conflicts:
        return ServeTargetInspection("WHOLE_SITE", api_target, whole_target, ())

    conflicts = sorted(
        {f"API:{item}" for item in api_conflicts}
        | {f"WHOLE:{item}" for item in whole_conflicts}
    )
    if not conflicts:
        conflicts = ["UNOWNED_SERVE_CONFIGURATION"]
    return ServeTargetInspection("CONFLICT", api_target, whole_target, tuple(conflicts))


def _read_inspection(
    binary: str,
    *,
    runner: CommandRunner,
    timeout_sec: float,
    whole_site_port: int,
    api_target_port: int,
) -> tuple[CommandResult, object, ServeTargetInspection]:
    result, payload = read_serve_status(
        binary,
        runner=runner,
        timeout_sec=timeout_sec,
    )
    return (
        result,
        payload,
        inspect_target_lane(
            payload,
            whole_site_port=whole_site_port,
            api_target_port=api_target_port,
        ),
    )


def target_lane_status(
    *,
    whole_site_port: int = DEFAULT_WHOLE_SITE_PORT,
    api_target_port: int = DEFAULT_API_TARGET_PORT,
    tailscale_binary: str | None = None,
    runner: CommandRunner = default_command_runner,
    timeout_sec: float = 10.0,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return a read-only H3 ownership status."""

    binary = resolve_tailscale_binary(tailscale_binary, environ=environ)
    result, _, inspection = _read_inspection(
        binary,
        runner=runner,
        timeout_sec=_validate_timeout(timeout_sec),
        whole_site_port=whole_site_port,
        api_target_port=api_target_port,
    )
    return {
        "schema": "stock-tracker-hybrid-h3-target-status-v1",
        "passed": inspection.safe,
        "tailscale_binary": binary,
        "inspection": inspection.as_dict(),
        "serve_status_raw": result.stdout,
    }


def _enable_exact_target(
    binary: str,
    port: int,
    *,
    runner: CommandRunner,
    timeout_sec: float,
    label: str,
) -> CommandResult:
    return run_command_checked(
        runner,
        build_serve_enable_command(binary, port),
        timeout_sec=timeout_sec,
        label=label,
    )


def _disable_exact_target(
    binary: str,
    *,
    runner: CommandRunner,
    timeout_sec: float,
    label: str,
) -> CommandResult:
    return run_command_checked(
        runner,
        build_serve_disable_command(binary),
        timeout_sec=timeout_sec,
        label=label,
    )


def _recover_original_target(
    *,
    binary: str,
    original_state: str,
    whole_site_port: int,
    api_target_port: int,
    runner: CommandRunner,
    timeout_sec: float,
    commands: list[list[str]],
) -> bool:
    """Restore an exact pre-mutation state without clearing unowned config."""

    _, _, current = _read_inspection(
        binary,
        runner=runner,
        timeout_sec=timeout_sec,
        whole_site_port=whole_site_port,
        api_target_port=api_target_port,
    )
    if current.state == original_state:
        return True
    if current.state == "CONFLICT":
        raise HybridH3Error(
            "recovery refused because Serve ownership changed to a conflicting configuration"
        )
    if current.state in {"WHOLE_SITE", "API_TARGET"}:
        disabled = _disable_exact_target(
            binary,
            runner=runner,
            timeout_sec=timeout_sec,
            label=f"clear exact {current.state} target before recovery",
        )
        commands.append(list(disabled.argv))
        _, _, current = _read_inspection(
            binary,
            runner=runner,
            timeout_sec=timeout_sec,
            whole_site_port=whole_site_port,
            api_target_port=api_target_port,
        )
        if current.state != "EMPTY":
            raise HybridH3Error("Serve was not empty after clearing an exact owned target")
    if original_state == "EMPTY":
        return current.state == "EMPTY"
    if original_state not in {"WHOLE_SITE", "API_TARGET"}:
        raise HybridH3Error(f"unsupported recovery state: {original_state}")
    target_port = whole_site_port if original_state == "WHOLE_SITE" else api_target_port
    enabled = _enable_exact_target(
        binary,
        target_port,
        runner=runner,
        timeout_sec=timeout_sec,
        label=f"restore exact {original_state} target",
    )
    commands.append(list(enabled.argv))
    _, _, restored = _read_inspection(
        binary,
        runner=runner,
        timeout_sec=timeout_sec,
        whole_site_port=whole_site_port,
        api_target_port=api_target_port,
    )
    return restored.state == original_state


def migrate_to_api_target(
    *,
    whole_site_port: int = DEFAULT_WHOLE_SITE_PORT,
    api_target_port: int = DEFAULT_API_TARGET_PORT,
    private_access: str | None = None,
    tailscale_binary: str | None = None,
    runner: CommandRunner = default_command_runner,
    timeout_sec: float = 15.0,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Safely move exact H0 Serve ownership to the API-only H3 target.

    If enabling the API target fails after an exact H0 target was disabled, the
    function attempts a verified rollback to the original whole-site target.
    It never uses ``tailscale serve reset``.
    """

    whole_port = _validate_port(whole_site_port, "whole_site_port")
    api_port = _validate_port(api_target_port, "api_target_port")
    if whole_port == api_port:
        raise HybridH3Error("whole-site and API target ports must differ")
    timeout = _validate_timeout(timeout_sec)

    try:
        preflight = run_preflight(
            port=api_port,
            private_access=private_access,
            tailscale_binary=tailscale_binary,
            runner=runner,
            timeout_sec=timeout,
            environ=environ,
        )
    except HybridH0Error as exc:
        raise HybridH3Error(str(exc)) from exc

    binary = preflight.tailscale_binary
    before_result, _, before = _read_inspection(
        binary,
        runner=runner,
        timeout_sec=timeout,
        whole_site_port=whole_port,
        api_target_port=api_port,
    )
    if before.state == "CONFLICT":
        raise HybridH3Error(
            "existing Serve configuration is not exclusively owned by H0/H3: "
            + ", ".join(before.conflicts)
        )
    if before.state == "API_TARGET":
        return {
            "schema": "stock-tracker-hybrid-h3-migrate-v1",
            "passed": True,
            "changed": False,
            "rolled_back": False,
            "before": before.as_dict(),
            "after": before.as_dict(),
            "preflight": preflight.as_dict(),
            "commands": [],
            "serve_status_before": before_result.stdout,
        }

    commands: list[list[str]] = []
    disabled_h0 = before.state == "WHOLE_SITE"
    if disabled_h0:
        disabled = _disable_exact_target(
            binary,
            runner=runner,
            timeout_sec=timeout,
            label="disable exact H0 whole-site target",
        )
        commands.append(list(disabled.argv))
        _, _, empty_check = _read_inspection(
            binary,
            runner=runner,
            timeout_sec=timeout,
            whole_site_port=whole_port,
            api_target_port=api_port,
        )
        if empty_check.state != "EMPTY":
            raise HybridH3Error("Serve was not empty after disabling the exact H0 target")

    rolled_back = False
    try:
        enabled = _enable_exact_target(
            binary,
            api_port,
            runner=runner,
            timeout_sec=timeout,
            label="enable H3 API target",
        )
        commands.append(list(enabled.argv))
        after_result, _, after = _read_inspection(
            binary,
            runner=runner,
            timeout_sec=timeout,
            whole_site_port=whole_port,
            api_target_port=api_port,
        )
        if after.state != "API_TARGET":
            raise HybridH3Error("Serve status does not match the exact H3 API target")
    except (HybridH0Error, HybridH3Error, OSError, TypeError, ValueError) as exc:
        recovery_error: str | None = None
        try:
            rolled_back = _recover_original_target(
                binary=binary,
                original_state=before.state,
                whole_site_port=whole_port,
                api_target_port=api_port,
                runner=runner,
                timeout_sec=timeout,
                commands=commands,
            )
        except (HybridH0Error, HybridH3Error, OSError, TypeError, ValueError) as recovery_exc:
            recovery_error = type(recovery_exc).__name__
            rolled_back = False
        detail = (
            f"{before.state} recovery verified"
            if rolled_back
            else f"{before.state} recovery could not be verified"
        )
        if recovery_error:
            detail += f" ({recovery_error})"
        raise HybridH3Error(f"H3 target migration failed; {detail}") from exc

    return {
        "schema": "stock-tracker-hybrid-h3-migrate-v1",
        "passed": True,
        "changed": True,
        "rolled_back": rolled_back,
        "before": before.as_dict(),
        "after": after.as_dict(),
        "preflight": preflight.as_dict(),
        "commands": commands,
        "serve_status_before": before_result.stdout,
        "serve_status": after_result.stdout,
    }


def rollback_to_whole_site(
    *,
    whole_site_port: int = DEFAULT_WHOLE_SITE_PORT,
    api_target_port: int = DEFAULT_API_TARGET_PORT,
    private_access: str | None = None,
    tailscale_binary: str | None = None,
    runner: CommandRunner = default_command_runner,
    timeout_sec: float = 15.0,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Safely restore exact H0 whole-site Serve ownership."""

    whole_port = _validate_port(whole_site_port, "whole_site_port")
    api_port = _validate_port(api_target_port, "api_target_port")
    if whole_port == api_port:
        raise HybridH3Error("whole-site and API target ports must differ")
    timeout = _validate_timeout(timeout_sec)
    try:
        preflight = run_h0_preflight(
            port=whole_port,
            private_access=private_access,
            tailscale_binary=tailscale_binary,
            runner=runner,
            timeout_sec=timeout,
            environ=environ,
        )
    except HybridH0Error as exc:
        raise HybridH3Error(str(exc)) from exc
    binary = preflight.tailscale_binary
    _, _, before = _read_inspection(
        binary,
        runner=runner,
        timeout_sec=timeout,
        whole_site_port=whole_port,
        api_target_port=api_port,
    )
    if before.state == "CONFLICT":
        raise HybridH3Error(
            "existing Serve configuration is not exclusively owned by H0/H3: "
            + ", ".join(before.conflicts)
        )
    if before.state == "WHOLE_SITE":
        return {
            "schema": "stock-tracker-hybrid-h3-rollback-v1",
            "passed": True,
            "changed": False,
            "before": before.as_dict(),
            "after": before.as_dict(),
            "commands": [],
        }

    commands: list[list[str]] = []
    disabled_h3 = before.state == "API_TARGET"
    if disabled_h3:
        disabled = _disable_exact_target(
            binary,
            runner=runner,
            timeout_sec=timeout,
            label="disable exact H3 API target",
        )
        commands.append(list(disabled.argv))
        _, _, empty_check = _read_inspection(
            binary,
            runner=runner,
            timeout_sec=timeout,
            whole_site_port=whole_port,
            api_target_port=api_port,
        )
        if empty_check.state != "EMPTY":
            raise HybridH3Error("Serve was not empty after disabling the exact H3 target")

    restored_h3 = False
    try:
        enabled = _enable_exact_target(
            binary,
            whole_port,
            runner=runner,
            timeout_sec=timeout,
            label="enable H0 whole-site target",
        )
        commands.append(list(enabled.argv))
        result, _, after = _read_inspection(
            binary,
            runner=runner,
            timeout_sec=timeout,
            whole_site_port=whole_port,
            api_target_port=api_port,
        )
        if after.state != "WHOLE_SITE":
            raise HybridH3Error("Serve status does not match the restored H0 whole-site target")
    except (HybridH0Error, HybridH3Error, OSError, TypeError, ValueError) as exc:
        recovery_error: str | None = None
        try:
            restored_h3 = _recover_original_target(
                binary=binary,
                original_state=before.state,
                whole_site_port=whole_port,
                api_target_port=api_port,
                runner=runner,
                timeout_sec=timeout,
                commands=commands,
            )
        except (HybridH0Error, HybridH3Error, OSError, TypeError, ValueError) as recovery_exc:
            recovery_error = type(recovery_exc).__name__
            restored_h3 = False
        detail = (
            f"{before.state} recovery verified"
            if restored_h3
            else f"{before.state} recovery could not be verified"
        )
        if recovery_error:
            detail += f" ({recovery_error})"
        raise HybridH3Error(f"H0 rollback failed; {detail}") from exc

    return {
        "schema": "stock-tracker-hybrid-h3-rollback-v1",
        "passed": True,
        "changed": True,
        "before": before.as_dict(),
        "after": after.as_dict(),
        "commands": commands,
        "serve_status": result.stdout,
    }


def token_rotation_plan(
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Validate a two-value restart rotation without returning either secret."""

    env = os.environ if environ is None else environ
    current = env.get(PRIVATE_ACCESS_ENV, "")
    replacement = env.get(NEW_PRIVATE_ACCESS_ENV, "")
    checks = (
        H3Check("current_access_valid", private_access_value_valid(current), "environment only"),
        H3Check("new_access_valid", private_access_value_valid(replacement), "environment only"),
        H3Check(
            "values_are_distinct",
            bool(current and replacement and current != replacement),
            "old and new values must differ",
        ),
    )
    passed = all(check.passed for check in checks)
    return {
        "schema": "stock-tracker-hybrid-h3-token-rotation-plan-v1",
        "passed": passed,
        "contains_private_access": False,
        "checks": [check.as_dict() for check in checks],
        "steps": [
            "set the replacement value in the supervised Engine environment",
            "restart the Engine and verify Runtime Health/Build identity",
            "re-authenticate each approved browser session for the same API Origin",
            "verify the previous value is rejected",
            "remove the previous value from the host environment and password manager history",
        ],
    }


def _windows_identity(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    domain = env.get("USERDOMAIN", "").strip()
    username = env.get("USERNAME", "").strip() or getpass.getuser()
    if not username:
        raise HybridH3Error("Windows task user identity is unavailable")
    return f"{domain}\\{username}" if domain else username


def _task_xml(
    *,
    python_executable: str,
    project_root: str,
    user_id: str,
) -> str:
    python_path = str(Path(python_executable).resolve())
    root = str(Path(project_root).resolve())
    arguments = "-m stock_tracker --host 127.0.0.1"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="{_WINDOWS_TASK_SCHEMA}">
  <RegistrationInfo><Description>Stock Tracker HYBRID_PRIVATE Local Engine</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user_id)}</UserId></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{escape(user_id)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>10</Count></RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(python_path)}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(root)}</WorkingDirectory></Exec>
  </Actions>
</Task>
"""


def build_windows_task_plan(
    *,
    project_root: str | Path,
    output_path: str | Path,
    python_executable: str | None = None,
    task_name: str = DEFAULT_TASK_NAME,
    user_id: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> WindowsTaskPlan:
    """Generate a no-secret Task Scheduler XML plan without host mutation."""

    if os.name != "nt" and user_id is None:
        raise HybridH3Error("automatic Windows identity discovery requires Windows")
    if type(task_name) is not str or not task_name.strip() or task_name != task_name.strip():
        raise HybridH3Error("task_name must be a non-empty trimmed string")
    if any(char in task_name for char in "\r\n\x00"):
        raise HybridH3Error("task_name contains invalid characters")
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise HybridH3Error("project_root must exist")
    python_path = Path(python_executable or sys.executable).resolve()
    if not python_path.is_file():
        raise HybridH3Error("python executable must exist")
    identity = user_id or _windows_identity(environ)
    xml_path = Path(output_path).resolve()
    xml = _task_xml(
        python_executable=str(python_path),
        project_root=str(root),
        user_id=identity,
    )
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(xml, encoding="utf-16")
    return WindowsTaskPlan(
        task_name=task_name,
        xml_path=str(xml_path),
        xml=xml,
        install_command=(
            "schtasks.exe",
            "/Create",
            "/TN",
            task_name,
            "/XML",
            str(xml_path),
            "/F",
        ),
        remove_command=("schtasks.exe", "/Delete", "/TN", task_name, "/F"),
    )


def default_host_runner(argv: tuple[str, ...], timeout_sec: float) -> CommandResult:
    timeout = _validate_timeout(timeout_sec)
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            shell=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HybridH3Error(f"host command failed or timed out: {argv[0]}") from exc
    return CommandResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def apply_windows_task_plan(
    plan: WindowsTaskPlan,
    *,
    apply: bool = False,
    acknowledge_host_change: bool = False,
    runner: HostRunner = default_host_runner,
    timeout_sec: float = 30.0,
) -> dict[str, object]:
    """Install the generated task only after two explicit mutation gates."""

    if not apply or not acknowledge_host_change:
        return {
            **plan.as_dict(),
            "passed": True,
            "applied": False,
            "reason": "dry-run; --apply and explicit host-change acknowledgement are required",
        }
    result = runner(plan.install_command, _validate_timeout(timeout_sec))
    if result.returncode != 0:
        raise HybridH3Error(
            "Windows task installation failed: "
            + (result.stderr or result.stdout or f"exit {result.returncode}")
        )
    return {
        **plan.as_dict(),
        "passed": True,
        "applied": True,
        "stdout": result.stdout,
    }


def remove_windows_task(
    *,
    task_name: str = DEFAULT_TASK_NAME,
    apply: bool = False,
    acknowledge_host_change: bool = False,
    runner: HostRunner = default_host_runner,
    timeout_sec: float = 30.0,
) -> dict[str, object]:
    command = ("schtasks.exe", "/Delete", "/TN", task_name, "/F")
    if not apply or not acknowledge_host_change:
        return {
            "schema": "stock-tracker-hybrid-h3-windows-task-remove-v1",
            "passed": True,
            "applied": False,
            "task_name": task_name,
            "remove_command": list(command),
        }
    result = runner(command, _validate_timeout(timeout_sec))
    if result.returncode != 0:
        raise HybridH3Error(
            "Windows task removal failed: "
            + (result.stderr or result.stdout or f"exit {result.returncode}")
        )
    return {
        "schema": "stock-tracker-hybrid-h3-windows-task-remove-v1",
        "passed": True,
        "applied": True,
        "task_name": task_name,
        "stdout": result.stdout,
    }


def render_result(result: Mapping[str, object]) -> str:
    return json.dumps(dict(result), ensure_ascii=False, indent=2, sort_keys=True)


__all__ = [
    "DEFAULT_API_TARGET_PORT",
    "DEFAULT_TASK_NAME",
    "DEFAULT_WHOLE_SITE_PORT",
    "NEW_PRIVATE_ACCESS_ENV",
    "H3PreflightReport",
    "HybridH3Error",
    "ServeTargetInspection",
    "WindowsTaskPlan",
    "apply_windows_task_plan",
    "build_windows_task_plan",
    "inspect_target_lane",
    "migrate_to_api_target",
    "remove_windows_task",
    "rollback_to_whole_site",
    "run_preflight",
    "target_lane_status",
    "token_rotation_plan",
]
