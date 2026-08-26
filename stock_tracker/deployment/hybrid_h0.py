"""Hybrid H0 bootstrap operations.

This module deliberately keeps Tailscale as a replaceable deployment adapter.
It never accepts a bearer token on the command line and only proxies the
loopback Local Engine target required by the H0 contract.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..core.security import PRIVATE_ACCESS_ENV, private_access_value_valid

DEFAULT_ENGINE_HOST = "127.0.0.1"
DEFAULT_ENGINE_PORT = 8080
TAILSCALE_BINARY_ENV = "TAILSCALE_BINARY"
_REMOTE_PROBE_HOST = "stock-tracker-h0.tailnet.invalid"
_REMOTE_PROBE_IP = "100.64.0.2"


class HybridH0Error(RuntimeError):
    """Raised when a Hybrid H0 safety or operational check fails."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass(frozen=True, slots=True)
class H0Check:
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class H0PreflightReport:
    engine_target: str
    tailscale_binary: str
    tailscale_version: str
    tailscale_dns_name: str | None
    tailscale_node_id: str | None
    checks: tuple[H0Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "stock-tracker-hybrid-h0-preflight-v1",
            "passed": self.passed,
            "engine_target": self.engine_target,
            "tailscale_binary": self.tailscale_binary,
            "tailscale_version": self.tailscale_version,
            "tailscale_dns_name": self.tailscale_dns_name,
            "tailscale_node_id": self.tailscale_node_id,
            "checks": [check.as_dict() for check in self.checks],
        }


CommandRunner = Callable[[tuple[str, ...], float], CommandResult]


def _validate_port(port: object) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise HybridH0Error("port must be an integer in the range 1..65535")
    return port


def _validate_timeout(timeout_sec: object) -> float:
    if (
        type(timeout_sec) not in (int, float)
        or not math.isfinite(float(timeout_sec))
        or float(timeout_sec) <= 0
    ):
        raise HybridH0Error("timeout_sec must be a positive finite number")
    return float(timeout_sec)


def serve_target(port: int) -> str:
    """Return the only target allowed by the H0 Serve contract."""

    return f"http://{DEFAULT_ENGINE_HOST}:{_validate_port(port)}"


def build_serve_enable_command(tailscale_binary: str, port: int) -> tuple[str, ...]:
    if type(tailscale_binary) is not str or not tailscale_binary.strip():
        raise HybridH0Error("tailscale binary path is required")
    return (tailscale_binary, "serve", "--bg", serve_target(port))


def build_serve_disable_command(tailscale_binary: str) -> tuple[str, ...]:
    if type(tailscale_binary) is not str or not tailscale_binary.strip():
        raise HybridH0Error("tailscale binary path is required")
    # Do not use `serve reset`: reset may remove unrelated Serve configuration.
    return (tailscale_binary, "serve", "off")


def default_command_runner(argv: tuple[str, ...], timeout_sec: float) -> CommandResult:
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
        raise HybridH0Error(f"command failed to start or timed out: {argv[0]}") from exc
    return CommandResult(
        argv=tuple(argv),
        returncode=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def resolve_tailscale_binary(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve Tailscale without installing or mutating the host system."""

    env = os.environ if environ is None else environ
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    configured = env.get(TAILSCALE_BINARY_ENV, "").strip()
    if configured:
        candidates.append(configured)
    discovered = shutil.which("tailscale") or shutil.which("tailscale.exe")
    if discovered:
        candidates.append(discovered)
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        root = env.get(variable, "").strip()
        if root:
            candidates.append(str(Path(root) / "Tailscale" / "tailscale.exe"))

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
        path = Path(candidate)
        if path.is_file():
            return str(path)
    raise HybridH0Error(
        "Tailscale CLI was not found; install and sign in to Tailscale before enabling Hybrid H0"
    )


def private_access_from_environment(
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    env = os.environ if environ is None else environ
    value = env.get(PRIVATE_ACCESS_ENV, "")
    if not private_access_value_valid(value):
        raise HybridH0Error(
            f"{PRIVATE_ACCESS_ENV} must contain at least 32 visible characters; "
            "the value is read from the process environment only"
        )
    return value


def _run_checked(
    runner: CommandRunner,
    argv: tuple[str, ...],
    *,
    timeout_sec: float,
    label: str,
) -> CommandResult:
    result = runner(argv, _validate_timeout(timeout_sec))
    if result.returncode != 0:
        detail = result.stderr or result.stdout or f"exit {result.returncode}"
        raise HybridH0Error(f"{label} failed: {detail}")
    return result


def _json_payload(result: CommandResult, *, label: str) -> object:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HybridH0Error(f"{label} returned invalid JSON") from exc


def _funnel_config_disabled(value: object) -> bool:
    """Accept only absent/empty Funnel config or exact boolean false leaves."""

    if value in (None, {}, []):
        return True
    if not isinstance(value, dict):
        return False
    return all(type(child) is bool and child is False for child in value.values())


def _serve_config_summary(payload: object, target: str) -> tuple[bool, set[str]]:
    """Verify exact ownership of the local H0 Serve configuration.

    A matching backend string alone is insufficient: the same node may also
    have Funnel, extra mounts, foreground configs, or Tailscale Services. H0
    refuses to mutate any such configuration.
    """

    if payload in ({}, [], None):
        return False, set()
    if not isinstance(payload, dict):
        return False, {"MALFORMED_SERVE_STATUS"}

    conflicts: set[str] = set()
    if not _funnel_config_disabled(payload.get("AllowFunnel")):
        conflicts.add("FUNNEL_NOT_STRICTLY_DISABLED")
    for section in ("Services", "Foreground"):
        if payload.get(section) not in (None, {}, []):
            conflicts.add(f"{section.upper()}_PRESENT")

    handlers: list[tuple[str, object]] = []
    web = payload.get("Web")
    if isinstance(web, dict):
        for web_server in web.values():
            if not isinstance(web_server, dict):
                conflicts.add("MALFORMED_WEB_SERVER")
                continue
            mounts = web_server.get("Handlers")
            if not isinstance(mounts, dict):
                conflicts.add("MALFORMED_HANDLERS")
                continue
            handlers.extend((str(mount), handler) for mount, handler in mounts.items())
    else:
        conflicts.add("WEB_CONFIG_MISSING")

    target_present = False
    if len(handlers) != 1:
        conflicts.add(f"HANDLER_COUNT_{len(handlers)}")
    else:
        mount, handler = handlers[0]
        if mount != "/":
            conflicts.add(f"UNEXPECTED_MOUNT_{mount}")
        if not isinstance(handler, dict):
            conflicts.add("MALFORMED_HANDLER")
        else:
            proxy = handler.get("Proxy")
            target_present = proxy == target
            if proxy != target:
                conflicts.add(f"UNEXPECTED_PROXY_{proxy!r}")
            extra_handler_fields = set(handler) - {"Proxy"}
            if extra_handler_fields:
                conflicts.add(
                    "EXTRA_HANDLER_FIELDS_" + ",".join(sorted(str(key) for key in extra_handler_fields))
                )

    tcp = payload.get("TCP")
    if not isinstance(tcp, dict) or len(tcp) != 1:
        conflicts.add("TCP_LISTENER_NOT_EXACTLY_ONE")
    else:
        listener = next(iter(tcp.values()))
        if not isinstance(listener, dict) or listener.get("HTTPS") is not True:
            conflicts.add("HTTPS_LISTENER_MISSING")
        elif any(key != "HTTPS" and value for key, value in listener.items()):
            conflicts.add("EXTRA_TCP_LISTENER_MODE")

    allowed_top_level = {"TCP", "Web", "AllowFunnel", "Foreground", "Services"}
    for key, value in payload.items():
        if key not in allowed_top_level and value not in (None, {}, [], False, ""):
            conflicts.add(f"UNKNOWN_SECTION_{key}")

    return target_present, conflicts


def _read_serve_status(
    tailscale_binary: str,
    *,
    runner: CommandRunner,
    timeout_sec: float,
) -> tuple[CommandResult, object]:
    result = _run_checked(
        runner,
        (tailscale_binary, "serve", "status", "--json"),
        timeout_sec=timeout_sec,
        label="tailscale serve status",
    )
    return result, _json_payload(result, label="tailscale serve status")


def inspect_serve_config(payload: object, target: str) -> tuple[bool, set[str]]:
    """Public read-only ownership inspection shared by later Hybrid lanes."""

    if type(target) is not str or not target.startswith("http://127.0.0.1:"):
        raise HybridH0Error("Serve ownership target must be an explicit loopback HTTP URL")
    return _serve_config_summary(payload, target)


def read_serve_status(
    tailscale_binary: str,
    *,
    runner: CommandRunner = default_command_runner,
    timeout_sec: float = 10.0,
) -> tuple[CommandResult, object]:
    """Read and parse Tailscale Serve status without mutating configuration."""

    return _read_serve_status(
        tailscale_binary,
        runner=runner,
        timeout_sec=timeout_sec,
    )


def run_command_checked(
    runner: CommandRunner,
    argv: tuple[str, ...],
    *,
    timeout_sec: float,
    label: str,
) -> CommandResult:
    """Execute one bounded Tailscale command with the shared error contract."""

    return _run_checked(runner, argv, timeout_sec=timeout_sec, label=label)


def _tailscale_identity(
    tailscale_binary: str,
    *,
    runner: CommandRunner,
    timeout_sec: float,
) -> tuple[str, str | None, str | None]:
    version_result = _run_checked(
        runner,
        (tailscale_binary, "version"),
        timeout_sec=timeout_sec,
        label="tailscale version",
    )
    version = version_result.stdout.splitlines()[0] if version_result.stdout else "unknown"
    status_result = _run_checked(
        runner,
        (tailscale_binary, "status", "--json"),
        timeout_sec=timeout_sec,
        label="tailscale status",
    )
    try:
        status = json.loads(status_result.stdout)
    except json.JSONDecodeError as exc:
        raise HybridH0Error("tailscale status --json returned invalid JSON") from exc
    if not isinstance(status, dict) or status.get("BackendState") != "Running":
        state = status.get("BackendState") if isinstance(status, dict) else None
        raise HybridH0Error(f"Tailscale is not running and authenticated (BackendState={state!r})")
    self_info = status.get("Self", {})
    dns_name: str | None = None
    node_id: str | None = None
    if isinstance(self_info, dict):
        raw_dns = self_info.get("DNSName")
        if isinstance(raw_dns, str) and raw_dns.strip():
            dns_name = raw_dns.strip().rstrip(".")
        raw_node_id = self_info.get("StableID") or self_info.get("ID")
        if isinstance(raw_node_id, str) and raw_node_id.strip():
            node_id = raw_node_id.strip()
    return version, dns_name, node_id


def _http_request(
    *,
    port: int,
    method: str,
    path: str,
    host_header: str,
    authorization: str | None = None,
    forwarded_for: str | None = None,
    timeout_sec: float = 5.0,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(DEFAULT_ENGINE_HOST, port, timeout=timeout_sec)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", host_header)
        connection.putheader("Accept", "application/json")
        connection.putheader("Connection", "close")
        if authorization:
            connection.putheader("Authorization", authorization)
        if forwarded_for:
            connection.putheader("X-Forwarded-For", forwarded_for)
        connection.endheaders()
        response = connection.getresponse()
        body = response.read(1024 * 1024)
        headers = {name.lower(): value for name, value in response.getheaders()}
        return response.status, headers, body
    except OSError as exc:
        raise HybridH0Error(
            f"Local Engine is not reachable at {serve_target(port)}"
        ) from exc
    finally:
        connection.close()


def _error_code(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    return error.get("code") if isinstance(error, dict) else None


def _probe_engine(port: int, private_access: str) -> tuple[H0Check, ...]:
    checks: list[H0Check] = []
    status, _, body = _http_request(
        port=port,
        method="GET",
        path="/api/provider_health",
        host_header=f"{DEFAULT_ENGINE_HOST}:{port}",
    )
    checks.append(
        H0Check(
            "local_engine_public_health",
            status == 200,
            f"HTTP {status}; bytes={len(body)}",
        )
    )

    status, _, body = _http_request(
        port=port,
        method="GET",
        path="/api/portfolio",
        host_header=_REMOTE_PROBE_HOST,
        forwarded_for=_REMOTE_PROBE_IP,
    )
    unauth_code = _error_code(body)
    checks.append(
        H0Check(
            "remote_style_private_api_requires_bearer",
            status == 401 and unauth_code == "PRIVATE_API_AUTH_REQUIRED",
            f"HTTP {status}; code={unauth_code}",
        )
    )

    status, _, body = _http_request(
        port=port,
        method="GET",
        path="/api/portfolio",
        host_header=_REMOTE_PROBE_HOST,
        authorization=f"Bearer {private_access}",
        forwarded_for=_REMOTE_PROBE_IP,
    )
    schema: str | None = None
    try:
        payload = json.loads(body.decode("utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("schema_version"), str):
            schema = payload["schema_version"]
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    checks.append(
        H0Check(
            "remote_style_private_api_accepts_exact_bearer",
            status == 200 and schema == "stage1-v1",
            f"HTTP {status}; schema={schema}",
        )
    )
    return tuple(checks)


def tailscale_identity(
    *,
    tailscale_binary: str | None = None,
    runner: CommandRunner = default_command_runner,
    timeout_sec: float = 10.0,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    """Return the authenticated local Tailscale node identity without mutation."""

    binary = resolve_tailscale_binary(tailscale_binary, environ=environ)
    version, dns_name, node_id = _tailscale_identity(
        binary,
        runner=runner,
        timeout_sec=timeout_sec,
    )
    return {
        "tailscale_binary": binary,
        "tailscale_version": version,
        "tailscale_dns_name": dns_name,
        "tailscale_node_id": node_id,
    }


def run_preflight(
    *,
    port: int = DEFAULT_ENGINE_PORT,
    private_access: str | None = None,
    tailscale_binary: str | None = None,
    runner: CommandRunner = default_command_runner,
    timeout_sec: float = 10.0,
    environ: Mapping[str, str] | None = None,
) -> H0PreflightReport:
    """Verify H0 prerequisites without changing Tailscale configuration."""

    validated_port = _validate_port(port)
    access = private_access if private_access is not None else private_access_from_environment(environ=environ)
    if not private_access_value_valid(access):
        raise HybridH0Error("private access value does not meet the server contract")
    identity = tailscale_identity(
        tailscale_binary=tailscale_binary,
        runner=runner,
        timeout_sec=timeout_sec,
        environ=environ,
    )
    binary = identity["tailscale_binary"]
    version = identity["tailscale_version"]
    dns_name = identity["tailscale_dns_name"]
    node_id = identity["tailscale_node_id"]
    assert binary is not None
    assert version is not None
    checks = (
        H0Check(
            "tailscale_dns_name_available",
            dns_name is not None,
            f"dns_name={dns_name}",
        ),
        H0Check(
            "tailscale_node_id_available",
            node_id is not None,
            f"node_id_available={node_id is not None}",
        ),
        *_probe_engine(validated_port, access),
    )
    report = H0PreflightReport(
        engine_target=serve_target(validated_port),
        tailscale_binary=binary,
        tailscale_version=version,
        tailscale_dns_name=dns_name,
        tailscale_node_id=node_id,
        checks=checks,
    )
    if not report.passed:
        failed = ", ".join(check.name for check in checks if not check.passed)
        raise HybridH0Error(f"Hybrid H0 preflight failed: {failed}")
    return report


def enable_serve(
    *,
    port: int = DEFAULT_ENGINE_PORT,
    private_access: str | None = None,
    tailscale_binary: str | None = None,
    runner: CommandRunner = default_command_runner,
    timeout_sec: float = 15.0,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Enable tailnet-only Serve after all H0 preflight checks pass."""

    preflight = run_preflight(
        port=port,
        private_access=private_access,
        tailscale_binary=tailscale_binary,
        runner=runner,
        timeout_sec=timeout_sec,
        environ=environ,
    )
    target = serve_target(port)
    before_result, before_payload = _read_serve_status(
        preflight.tailscale_binary,
        runner=runner,
        timeout_sec=timeout_sec,
    )
    target_present, conflicts = _serve_config_summary(before_payload, target)
    if conflicts:
        raise HybridH0Error(
            "existing Tailscale Serve backends conflict with Hybrid H0: "
            + ", ".join(sorted(conflicts))
        )
    if before_payload not in ({}, [], None) and not target_present:
        raise HybridH0Error(
            "an existing Tailscale Serve configuration is present but is not owned by Hybrid H0; "
            "review it manually instead of overwriting it"
        )

    command = build_serve_enable_command(preflight.tailscale_binary, port)
    if target_present:
        enabled = CommandResult(command, 0, "already configured", "")
    else:
        enabled = _run_checked(
            runner,
            command,
            timeout_sec=timeout_sec,
            label="tailscale serve enable",
        )
    status_result, status_payload = _read_serve_status(
        preflight.tailscale_binary,
        runner=runner,
        timeout_sec=timeout_sec,
    )
    after_present, after_conflicts = _serve_config_summary(status_payload, target)
    if not after_present or after_conflicts:
        raise HybridH0Error("Tailscale Serve status does not match the locked Hybrid H0 target")
    return {
        "schema": "stock-tracker-hybrid-h0-enable-v1",
        "passed": True,
        "changed": not target_present,
        "preflight": preflight.as_dict(),
        "serve_command": list(command),
        "serve_stdout": enabled.stdout,
        "serve_status_before": before_result.stdout,
        "serve_status": status_result.stdout,
    }


def serve_status(
    *,
    tailscale_binary: str | None = None,
    runner: CommandRunner = default_command_runner,
    timeout_sec: float = 10.0,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    binary = resolve_tailscale_binary(tailscale_binary, environ=environ)
    result, payload = _read_serve_status(
        binary,
        runner=runner,
        timeout_sec=timeout_sec,
    )
    return {
        "schema": "stock-tracker-hybrid-h0-status-v1",
        "passed": True,
        "tailscale_binary": binary,
        "serve_status": payload,
        "serve_status_raw": result.stdout,
    }


def disable_serve(
    *,
    port: int = DEFAULT_ENGINE_PORT,
    tailscale_binary: str | None = None,
    runner: CommandRunner = default_command_runner,
    timeout_sec: float = 10.0,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Disable only a root Serve that matches the locked H0 loopback target."""

    binary = resolve_tailscale_binary(tailscale_binary, environ=environ)
    target = serve_target(port)
    before_result, before_payload = _read_serve_status(
        binary,
        runner=runner,
        timeout_sec=timeout_sec,
    )
    target_present, conflicts = _serve_config_summary(before_payload, target)
    if conflicts:
        raise HybridH0Error(
            "refusing to disable Serve because other backend targets are configured: "
            + ", ".join(sorted(conflicts))
        )
    if before_payload in ({}, [], None):
        return {
            "schema": "stock-tracker-hybrid-h0-disable-v1",
            "passed": True,
            "changed": False,
            "serve_command": [],
            "serve_stdout": "no Serve configuration",
            "serve_status_before": before_result.stdout,
        }
    if not target_present:
        raise HybridH0Error(
            "refusing to disable an existing Serve configuration not owned by Hybrid H0"
        )

    command = build_serve_disable_command(binary)
    result = _run_checked(
        runner,
        command,
        timeout_sec=timeout_sec,
        label="tailscale serve disable",
    )
    _, after_payload = _read_serve_status(
        binary,
        runner=runner,
        timeout_sec=timeout_sec,
    )
    after_present, after_conflicts = _serve_config_summary(after_payload, target)
    if after_present or after_conflicts or after_payload not in ({}, [], None):
        raise HybridH0Error(
            "Serve configuration is not empty after disabling the exact Hybrid H0 target"
        )
    return {
        "schema": "stock-tracker-hybrid-h0-disable-v1",
        "passed": True,
        "changed": True,
        "serve_command": list(command),
        "serve_stdout": result.stdout,
        "serve_status_before": before_result.stdout,
    }
