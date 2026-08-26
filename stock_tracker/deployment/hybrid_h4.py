"""Deterministic, no-secret static build contract for Hybrid H4."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.network import InvalidOriginError, normalize_http_origin

BUILD_SCHEMA = "stock-tracker-hybrid-h4-static-build-v1"
MANIFEST_NAME = "deployment-manifest.json"
_RUNTIME_CONFIG_NAME = "runtime-config.js"
_ALLOWED_HOSTS = frozenset({"cloudflare", "github"})
_TEXT_SUFFIXES = frozenset({"", ".css", ".html", ".js", ".json", ".md", ".svg", ".txt", ".webmanifest", ".xml"})
_ALLOWED_SOURCE_SUFFIXES = frozenset(
    {".css", ".html", ".ico", ".jpeg", ".jpg", ".js", ".json", ".md", ".png", ".svg", ".txt", ".webmanifest", ".webp", ".xml"}
)
_ALLOWED_GENERATED_NAMES = frozenset({".nojekyll", "_headers", MANIFEST_NAME})
_FORBIDDEN_FILE_SUFFIXES = frozenset(
    {
        ".bat",
        ".cmd",
        ".db",
        ".db-shm",
        ".db-wal",
        ".log",
        ".pid",
        ".ps1",
        ".py",
        ".pyc",
        ".pyo",
        ".sh",
        ".sql",
        ".tar",
        ".toml",
        ".zip",
    }
)
_FORBIDDEN_FILE_NAMES = frozenset({"runtime-config.example.js"})
_VISIBLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUNTIME_CONFIG_RE = re.compile(
    r"^/\* Public deployment metadata only\.[^\n]*\*/\n"
    r"window\.STOCK_TRACKER_RUNTIME = Object\.freeze\((\{.*\})\);\n$",
    re.DOTALL,
)
_MANIFEST_FIELDS = frozenset(
    {
        "api_origin",
        "build_id",
        "contains_private_access",
        "deployment_mode",
        "engine_id",
        "expected_api_major",
        "files",
        "host",
        "response_headers_supported",
        "schema",
        "web_origin",
    }
)
_RUNTIME_FIELDS = frozenset(
    {
        "allowApiOriginOverride",
        "allowPrivateBrowserCache",
        "allowedApiOrigins",
        "apiBaseUrl",
        "deploymentMode",
        "expectedApiMajor",
        "expectedEngineId",
        "frontendBuild",
        "healthPollMs",
        "ssePath",
    }
)


class HybridH4Error(RuntimeError):
    """Raised when an H4 build cannot prove its public/static boundary."""


@dataclass(frozen=True, slots=True)
class StaticBuildConfig:
    web_origin: str
    api_origin: str
    engine_id: str
    build_id: str
    expected_api_major: int = 1
    host: str = "cloudflare"
    health_poll_ms: int = 15000
    allow_loopback_http: bool = False

    def normalized(self) -> StaticBuildConfig:
        try:
            web_origin = normalize_http_origin(self.web_origin)
            api_origin = normalize_http_origin(self.api_origin)
        except InvalidOriginError as exc:
            raise HybridH4Error(str(exc)) from exc
        if type(self.allow_loopback_http) is not bool:
            raise HybridH4Error("allow_loopback_http must be an actual boolean")
        if not self.allow_loopback_http and (
            not web_origin.startswith("https://") or not api_origin.startswith("https://")
        ):
            raise HybridH4Error("production H4 web/API origins must both use HTTPS")
        if type(self.host) is not str or self.host not in _ALLOWED_HOSTS:
            raise HybridH4Error("host must be cloudflare or github")
        for name, value in (("engine_id", self.engine_id), ("build_id", self.build_id)):
            if type(value) is not str or _VISIBLE_ID_RE.fullmatch(value) is None:
                raise HybridH4Error(f"{name} must be a visible stable identifier")
        if type(self.expected_api_major) is not int or not 1 <= self.expected_api_major <= 999:
            raise HybridH4Error("expected_api_major must be in 1..999")
        if type(self.health_poll_ms) is not int or not 5000 <= self.health_poll_ms <= 300000:
            raise HybridH4Error("health_poll_ms must be in 5000..300000")
        return StaticBuildConfig(
            web_origin=web_origin,
            api_origin=api_origin,
            engine_id=self.engine_id,
            build_id=self.build_id,
            expected_api_major=self.expected_api_major,
            host=self.host,
            health_poll_ms=self.health_poll_ms,
            allow_loopback_http=self.allow_loopback_http,
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _forbidden_name(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in _FORBIDDEN_FILE_NAMES
        or name == ".env"
        or name.startswith(".env.")
        or path.suffix.lower() in _FORBIDDEN_FILE_SUFFIXES
    )


def _normalize_text_bytes(raw: bytes, relative: Path) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HybridH4Error(f"public text file is not UTF-8: {relative.as_posix()}") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _write_text_lf(path: Path, text: str) -> None:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    path.write_bytes(normalized.encode("utf-8"))


def _runtime_config(config: StaticBuildConfig) -> str:
    payload = {
        "deploymentMode": "HYBRID_PRIVATE",
        "apiBaseUrl": config.api_origin,
        "allowedApiOrigins": [config.api_origin],
        "ssePath": "/api/stream",
        "frontendBuild": config.build_id,
        "expectedApiMajor": config.expected_api_major,
        "expectedEngineId": config.engine_id,
        "allowApiOriginOverride": False,
        "allowPrivateBrowserCache": False,
        "healthPollMs": config.health_poll_ms,
    }
    return (
        "/* Public deployment metadata only. Never add bearer values or private facts. */\n"
        "window.STOCK_TRACKER_RUNTIME = Object.freeze("
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + ");\n"
    )


def _csp(config: StaticBuildConfig, *, meta: bool) -> str:
    directives = [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        f"connect-src 'self' {config.api_origin}",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "manifest-src 'self'",
        "worker-src 'none'",
    ]
    if not meta:
        directives.append("frame-ancestors 'none'")
    if not config.allow_loopback_http:
        directives.append("upgrade-insecure-requests")
    return "; ".join(directives)


def _inject_security_meta(index_path: Path, config: StaticBuildConfig) -> None:
    text = index_path.read_bytes().decode("utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(
        r"\s*<!-- HYBRID_H4_SECURITY_BEGIN -->.*?<!-- HYBRID_H4_SECURITY_END -->\s*",
        "\n",
        text,
        flags=re.DOTALL,
    )
    marker = (
        "\n  <!-- HYBRID_H4_SECURITY_BEGIN -->\n"
        f'  <meta http-equiv="Content-Security-Policy" content="{_csp(config, meta=True)}" />\n'
        '  <meta name="referrer" content="no-referrer" />\n'
        '  <meta http-equiv="X-Content-Type-Options" content="nosniff" />\n'
        "  <!-- HYBRID_H4_SECURITY_END -->\n"
    )
    if "</head>" not in text:
        raise HybridH4Error("web/index.html does not contain </head>")
    _write_text_lf(index_path, text.replace("</head>", marker + "</head>", 1))


def _cloudflare_headers(config: StaticBuildConfig) -> str:
    return "\n".join(
        [
            "/*",
            f"  Content-Security-Policy: {_csp(config, meta=False)}",
            "  Referrer-Policy: no-referrer",
            "  X-Content-Type-Options: nosniff",
            "  X-Frame-Options: DENY",
            "  Permissions-Policy: accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=(), payment=(), usb=()",
            "  Cross-Origin-Opener-Policy: same-origin",
            "  Cache-Control: public, max-age=300, must-revalidate",
            "",
            "/index.html",
            "  Cache-Control: no-store",
            "",
            "/runtime-config.js",
            "  Cache-Control: no-store",
            "",
        ]
    )


def _copy_web(source: Path, target: Path) -> None:
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        relative = path.relative_to(source)
        if path.is_symlink():
            raise HybridH4Error(f"symlink is forbidden in static source: {relative.as_posix()}")
        if any(part == "__pycache__" for part in relative.parts) or _forbidden_name(path):
            continue
        if path.is_file() and path.suffix.lower() not in _ALLOWED_SOURCE_SUFFIXES:
            raise HybridH4Error(f"unapproved static source type: {relative.as_posix()}")
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            raise HybridH4Error(f"unsupported static source entry: {relative.as_posix()}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw = path.read_bytes()
        if path.suffix.lower() in _TEXT_SUFFIXES:
            destination.write_bytes(_normalize_text_bytes(raw, relative))
        else:
            destination.write_bytes(raw)


def _text_files(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in _TEXT_SUFFIXES
    ]


def _scan_public_boundary(root: Path, forbidden_values: tuple[str, ...]) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise HybridH4Error(f"symlink is forbidden in static build: {path.relative_to(root)}")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part == "__pycache__" for part in relative.parts):
            raise HybridH4Error(f"Python cache in static build: {relative}")
        if _forbidden_name(path):
            raise HybridH4Error(f"forbidden file in static build: {relative}")
        if path.name not in _ALLOWED_GENERATED_NAMES and path.suffix.lower() not in _ALLOWED_SOURCE_SUFFIXES:
            raise HybridH4Error(f"unapproved static file type in build: {relative}")
    for path in _text_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise HybridH4Error(f"public text file is not UTF-8: {path.relative_to(root)}") from exc
        if "\r" in text:
            raise HybridH4Error(f"public text file is not LF-normalized: {path.relative_to(root)}")
        for value in forbidden_values:
            if type(value) is not str:
                raise HybridH4Error("forbidden_values must contain strings")
            if value and value in text:
                raise HybridH4Error(f"forbidden secret value found in {path.relative_to(root)}")
        if re.search(r"STOCK_TRACKER_(?:NEW_)?PRIVATE_ACCESS\s*=", text):
            raise HybridH4Error(f"private environment assignment found in {path.relative_to(root)}")
        if "-----BEGIN PRIVATE KEY-----" in text or "-----BEGIN OPENSSH PRIVATE KEY-----" in text:
            raise HybridH4Error(f"private key marker found in {path.relative_to(root)}")


def _manifest(root: Path, config: StaticBuildConfig) -> dict[str, Any]:
    files = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
        )
    return {
        "schema": BUILD_SCHEMA,
        "build_id": config.build_id,
        "engine_id": config.engine_id,
        "expected_api_major": config.expected_api_major,
        "deployment_mode": "HYBRID_PRIVATE",
        "host": config.host,
        "web_origin": config.web_origin,
        "api_origin": config.api_origin,
        "contains_private_access": False,
        "response_headers_supported": config.host == "cloudflare",
        "files": files,
    }


def _parse_runtime_config(text: str) -> dict[str, Any]:
    match = _RUNTIME_CONFIG_RE.fullmatch(text)
    if match is None:
        raise HybridH4Error("runtime-config.js shape is invalid")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise HybridH4Error("runtime-config.js payload is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _RUNTIME_FIELDS:
        raise HybridH4Error("runtime-config.js fields are not the frozen public contract")
    return payload


def _validate_manifest(manifest: object) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise HybridH4Error("static build manifest fields are invalid")
    if manifest.get("schema") != BUILD_SCHEMA:
        raise HybridH4Error("static build manifest schema is invalid")
    if manifest.get("deployment_mode") != "HYBRID_PRIVATE":
        raise HybridH4Error("static build deployment mode is invalid")
    if manifest.get("host") not in _ALLOWED_HOSTS:
        raise HybridH4Error("static build host is invalid")
    if manifest.get("contains_private_access") is not False:
        raise HybridH4Error("static build manifest does not prove a no-secret artifact")
    expected_headers = manifest.get("host") == "cloudflare"
    if manifest.get("response_headers_supported") is not expected_headers:
        raise HybridH4Error("static build response-header capability is inconsistent")
    for field in ("engine_id", "build_id"):
        if type(manifest.get(field)) is not str or _VISIBLE_ID_RE.fullmatch(manifest[field]) is None:
            raise HybridH4Error(f"static build {field} is invalid")
    if type(manifest.get("expected_api_major")) is not int or not 1 <= manifest["expected_api_major"] <= 999:
        raise HybridH4Error("static build API major is invalid")
    for field in ("web_origin", "api_origin"):
        try:
            if normalize_http_origin(manifest.get(field)) != manifest.get(field):
                raise HybridH4Error(f"static build {field} is not canonical")
        except InvalidOriginError as exc:
            raise HybridH4Error(f"static build {field} is invalid") from exc
    if type(manifest.get("files")) is not list:
        raise HybridH4Error("manifest files must be a list")
    return manifest


def verify_static_build(
    output_dir: str | Path,
    *,
    forbidden_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    root_raw = Path(output_dir)
    if root_raw.is_symlink():
        raise HybridH4Error("static build root must not be a symlink")
    root = root_raw.resolve()
    manifest_path = root / MANIFEST_NAME
    if not root.is_dir() or not manifest_path.is_file() or manifest_path.is_symlink():
        raise HybridH4Error("static build or deployment manifest is missing")
    try:
        manifest = _validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise HybridH4Error("static build manifest is invalid JSON") from exc

    _scan_public_boundary(root, forbidden_values)
    expected_paths: set[str] = set()
    manifest_paths: list[str] = []
    for record in manifest["files"]:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}:
            raise HybridH4Error("manifest file record is invalid")
        relative = record.get("path")
        if (
            type(relative) is not str
            or not relative
            or relative.startswith(("/", "\\"))
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in Path(relative).parts)
        ):
            raise HybridH4Error("manifest contains unsafe file path")
        if relative in expected_paths:
            raise HybridH4Error("manifest contains duplicate file paths")
        if type(record.get("sha256")) is not str or _SHA256_RE.fullmatch(record["sha256"]) is None:
            raise HybridH4Error("manifest contains an invalid SHA-256")
        if type(record.get("size")) is not int or record["size"] < 0:
            raise HybridH4Error("manifest contains an invalid file size")
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise HybridH4Error(f"manifest file is missing or unsafe: {relative}")
        if _sha256(path) != record["sha256"] or path.stat().st_size != record["size"]:
            raise HybridH4Error(f"manifest hash/size mismatch: {relative}")
        expected_paths.add(relative)
        manifest_paths.append(relative)
    if manifest_paths != sorted(manifest_paths):
        raise HybridH4Error("manifest file records are not deterministically sorted")

    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }
    if expected_paths != actual_paths:
        raise HybridH4Error("manifest file set does not match the build directory")

    runtime_path = root / _RUNTIME_CONFIG_NAME
    index_path = root / "index.html"
    fallback_path = root / "404.html"
    if not runtime_path.is_file() or not index_path.is_file() or not fallback_path.is_file():
        raise HybridH4Error("required H4 static files are missing")
    runtime_payload = _parse_runtime_config(runtime_path.read_text(encoding="utf-8"))
    expected_runtime = {
        "deploymentMode": "HYBRID_PRIVATE",
        "apiBaseUrl": manifest["api_origin"],
        "allowedApiOrigins": [manifest["api_origin"]],
        "ssePath": "/api/stream",
        "frontendBuild": manifest["build_id"],
        "expectedApiMajor": manifest["expected_api_major"],
        "expectedEngineId": manifest["engine_id"],
        "allowApiOriginOverride": False,
        "allowPrivateBrowserCache": False,
        "healthPollMs": runtime_payload.get("healthPollMs"),
    }
    if runtime_payload != expected_runtime:
        raise HybridH4Error("runtime-config.js does not match the deployment manifest")
    if type(runtime_payload["healthPollMs"]) is not int or not 5000 <= runtime_payload["healthPollMs"] <= 300000:
        raise HybridH4Error("runtime-config.js healthPollMs is invalid")

    index_text = index_path.read_text(encoding="utf-8")
    fallback_text = fallback_path.read_text(encoding="utf-8")
    for name, text in (("index.html", index_text), ("404.html", fallback_text)):
        if "HYBRID_H4_SECURITY_BEGIN" not in text or "connect-src" not in text:
            raise HybridH4Error(f"{name} is missing H4 security metadata")
        if "connect-src *" in text or manifest["api_origin"] not in text:
            raise HybridH4Error(f"{name} does not pin the exact API origin")
    if index_text != fallback_text:
        raise HybridH4Error("404.html must match the verified application shell")

    headers_path = root / "_headers"
    if manifest["host"] == "cloudflare":
        if not headers_path.is_file():
            raise HybridH4Error("Cloudflare build is missing _headers")
        headers = headers_path.read_text(encoding="utf-8")
        if "connect-src *" in headers or manifest["api_origin"] not in headers:
            raise HybridH4Error("Cloudflare headers do not pin the exact API origin")
        for required in (
            "Content-Security-Policy:",
            "Referrer-Policy: no-referrer",
            "X-Content-Type-Options: nosniff",
            "X-Frame-Options: DENY",
        ):
            if required not in headers:
                raise HybridH4Error(f"Cloudflare headers are missing {required}")
    elif headers_path.exists():
        raise HybridH4Error("GitHub fallback must not claim Cloudflare response headers")

    return {
        "schema": "stock-tracker-hybrid-h4-static-verify-v1",
        "passed": True,
        "build_id": manifest["build_id"],
        "file_count": len(actual_paths),
        "contains_private_access": False,
        "manifest_sha256": _sha256(manifest_path),
        "response_headers_supported": manifest["response_headers_supported"],
    }


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def build_static_site(
    source_web: str | Path,
    output_dir: str | Path,
    config: StaticBuildConfig,
    *,
    forbidden_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Create, verify, and activate a deterministic public static build."""

    normalized = config.normalized()
    source_raw = Path(source_web)
    output_raw = Path(output_dir)
    if source_raw.is_symlink() or output_raw.is_symlink():
        raise HybridH4Error("source_web/output_dir must not be symlinks")
    source = source_raw.resolve()
    output = output_raw.resolve()
    if not source.is_dir() or not (source / "index.html").is_file():
        raise HybridH4Error("source_web must contain index.html")
    if source == output or source in output.parents or output in source.parents:
        raise HybridH4Error("output_dir must be a sibling/outside path, never inside or above source_web")

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".hybrid-h4-build-", dir=output.parent))
    backup: Path | None = None
    activated = False
    try:
        _copy_web(source, temp)
        _write_text_lf(temp / _RUNTIME_CONFIG_NAME, _runtime_config(normalized))
        _inject_security_meta(temp / "index.html", normalized)
        (temp / "404.html").write_bytes((temp / "index.html").read_bytes())
        (temp / ".nojekyll").write_bytes(b"")
        if normalized.host == "cloudflare":
            _write_text_lf(temp / "_headers", _cloudflare_headers(normalized))
        _scan_public_boundary(temp, forbidden_values)
        manifest = _manifest(temp, normalized)
        _write_text_lf(
            temp / MANIFEST_NAME,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        verify_static_build(temp, forbidden_values=forbidden_values)

        if output.exists():
            backup = output.parent / f".{output.name}.backup-{uuid.uuid4().hex}"
            os.replace(output, backup)
        try:
            os.replace(temp, output)
            activated = True
            verification = verify_static_build(output, forbidden_values=forbidden_values)
        except Exception:
            _remove_tree(output)
            if backup is not None and backup.exists():
                os.replace(backup, output)
            raise
        if backup is not None:
            _remove_tree(backup)
            backup = None
        return {
            "schema": "stock-tracker-hybrid-h4-static-build-result-v1",
            "passed": True,
            "output_dir": str(output),
            "build_id": normalized.build_id,
            "web_origin": normalized.web_origin,
            "api_origin": normalized.api_origin,
            "host": normalized.host,
            "contains_private_access": False,
            "backend_cors_allowed_origins": [normalized.web_origin],
            "verification": verification,
        }
    finally:
        if not activated:
            _remove_tree(temp)
        if backup is not None and backup.exists() and not output.exists():
            os.replace(backup, output)


__all__ = [
    "BUILD_SCHEMA",
    "HybridH4Error",
    "StaticBuildConfig",
    "build_static_site",
    "verify_static_build",
]
