"""Deployment adapters kept outside the financial decision core."""

from .h0_acceptance import (
    H0AcceptanceReport,
    HybridH0AcceptanceError,
    TemporaryH0Fixture,
    verify_h0_acceptance,
)
from .hybrid_h0 import (
    DEFAULT_ENGINE_HOST,
    DEFAULT_ENGINE_PORT,
    HybridH0Error,
    build_serve_disable_command,
    build_serve_enable_command,
    disable_serve,
    enable_serve,
    run_preflight,
    serve_status,
    tailscale_identity,
)

__all__ = [
    "DEFAULT_ENGINE_HOST",
    "DEFAULT_ENGINE_PORT",
    "H0AcceptanceReport",
    "HybridH0AcceptanceError",
    "HybridH0Error",
    "TemporaryH0Fixture",
    "build_serve_disable_command",
    "build_serve_enable_command",
    "disable_serve",
    "enable_serve",
    "run_preflight",
    "serve_status",
    "tailscale_identity",
    "verify_h0_acceptance",
]
