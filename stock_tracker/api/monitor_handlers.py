"""Private monitor-workspace REST handlers; never call upstream providers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..monitor.repository import MonitorRepositoryError
from ..monitor.service import MonitorService, MonitorServiceError
from .handlers import APIError, AppContext


def _service(ctx: AppContext) -> MonitorService:
    service = ctx.monitor_service
    if not isinstance(service, MonitorService):
        raise APIError(503, "MONITOR_UNAVAILABLE", "monitor service is unavailable")
    return service


def _time(value: str, field: str) -> datetime:
    if type(value) is not str or not value or len(value) > 64:
        raise APIError(400, "INVALID_TIME", f"{field} must be ISO 8601", field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise APIError(400, "INVALID_TIME", f"{field} must be ISO 8601", field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise APIError(400, "INVALID_TIME", f"{field} must include a timezone", field)
    return parsed


def get_summary(ctx: AppContext) -> dict[str, Any]:
    return _service(ctx).summary()


def get_data_link(ctx: AppContext) -> dict[str, Any]:
    return _service(ctx).data_link()


def get_rules(ctx: AppContext) -> dict[str, Any]:
    return {"schema": "stock-tracker-monitor-rules-v1", "rules": _service(ctx).rules()}


def get_inbox(
    ctx: AppContext,
    *,
    states: tuple[str, ...] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    try:
        rows = _service(ctx).inbox(states=states, limit=limit)
    except MonitorServiceError as exc:
        raise APIError(400, "INVALID_MONITOR_FILTER", str(exc)) from exc
    return {"schema": "stock-tracker-monitor-inbox-v1", "inbox": rows}


def get_outbox(ctx: AppContext, *, limit: int = 200) -> dict[str, Any]:
    try:
        rows = _service(ctx).repository.outbox(limit=limit)
    except MonitorRepositoryError as exc:
        raise APIError(400, "INVALID_OUTBOX_FILTER", "invalid outbox filter") from exc
    return {"schema": "stock-tracker-monitor-outbox-v1", "outbox": rows}


def get_replay(
    ctx: AppContext,
    *,
    symbol: str,
    start: str,
    end: str,
    backend: str = "auto",
    limit: int = 5000,
) -> dict[str, Any]:
    try:
        return _service(ctx).replay_data(
            symbol=symbol,
            start_at=_time(start, "start"),
            end_at=_time(end, "end"),
            backend=backend,
            limit=limit,
        )
    except MonitorServiceError as exc:
        raise APIError(400, "INVALID_REPLAY_REQUEST", str(exc)) from exc


def put_rule(ctx: AppContext, payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        rule = _service(ctx).create_or_update_rule(payload)
    except MonitorServiceError as exc:
        raise APIError(400, "INVALID_MONITOR_RULE", str(exc)) from exc
    return {"schema": "stock-tracker-monitor-rule-v1", "rule": rule}


def delete_rule(ctx: AppContext, rule_id: str) -> dict[str, Any]:
    try:
        removed = _service(ctx).delete_rule(rule_id)
    except MonitorServiceError as exc:
        raise APIError(409, "MONITOR_RULE_DELETE_BLOCKED", str(exc)) from exc
    if not removed:
        raise APIError(404, "MONITOR_RULE_NOT_FOUND", "monitor rule not found")
    return {"schema": "stock-tracker-monitor-rule-delete-v1", "removed": True}


def transition_inbox(
    ctx: AppContext,
    inbox_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    allowed = {"state", "reason", "snooze_sec"}
    if not isinstance(payload, dict) or set(payload) - allowed or not {"state", "reason"}.issubset(payload):
        raise APIError(400, "INVALID_MONITOR_TRANSITION", "invalid transition field set")
    state = payload["state"]
    reason = payload["reason"]
    snooze = payload.get("snooze_sec")
    if type(state) is not str or type(reason) is not str:
        raise APIError(400, "INVALID_MONITOR_TRANSITION", "state and reason must be strings")
    try:
        inbox = _service(ctx).transition(
            inbox_id,
            state,
            reason=reason,
            snooze_sec=snooze,
        )
    except MonitorServiceError as exc:
        raise APIError(409, "MONITOR_TRANSITION_REJECTED", str(exc)) from exc
    return {"schema": "stock-tracker-monitor-transition-v1", "inbox": inbox}
