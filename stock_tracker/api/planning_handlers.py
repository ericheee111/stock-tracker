"""Private manual-plan API. Reads Portfolio; writes only an explicit planning DB."""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..portfolio_planning.domain import (
    PlanningError,
    attribution_scenario,
    digest,
    fields,
    public_book,
    reduce_command,
    require,
    validate_parent,
)
from ..portfolio_planning.resources import resource_summary
from ..portfolio_planning.store import PlanningStore
from .handlers import APIError, AppContext


def configure(ctx: AppContext, runtime_database: str, logger: Any) -> None:
    """Optional lane; invalid configuration cannot disable the normal dashboard."""
    path, store_id = os.environ.get("STOCK_TRACKER_PLANNING_DB", ""), os.environ.get("STOCK_TRACKER_PLANNING_STORE_ID", "")
    ctx.planning_store = None
    ctx.planning_status = "NOT_CONFIGURED"
    if not path and not store_id:
        return
    try:
        require(bool(path and store_id), "PLANNING_CONFIG_INCOMPLETE", "必须同时配置路径和Store ID")
        candidate = Path(path)
        primary = Path(runtime_database).absolute()
        require(candidate.absolute() != primary and not (
            candidate.exists() and primary.exists() and candidate.samefile(primary)),
            "PROTECTED_DATABASE", "不能复用Portfolio数据库")
        ctx.planning_store = PlanningStore(candidate, store_id)
        ctx.planning_status = "READY"
    except (PlanningError, OSError) as exc:
        ctx.planning_status = "UNAVAILABLE"
        logger.warning("Manual planning disabled: %s", getattr(exc, "code", "PLANNING_IO_ERROR"))


def inspect_parents(ctx: AppContext) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Isolate unsupported legacy rows; never hide them as zero or block closure."""
    output: dict[str, Any] = {}
    issues: list[dict[str, str]] = []
    symbols: dict[str, str] = {}
    duplicate_ids: set[str] = set()
    for position in ctx.repo.load_positions():
        if position.closed_at is not None:
            continue
        try:
            require(type(position.shares) in (int, float) and math.isfinite(position.shares)
                    and float(position.shares).is_integer(), "UNSUPPORTED_POSITION", "本版计划仅支持整股持仓")
            require(type(position.cost) in (int, float) and math.isfinite(position.cost),
                    "UNSUPPORTED_POSITION", "持仓成本无效")
            parent = validate_parent({"position_id": position.id, "symbol": position.symbol,
                                      "market": position.market.value, "shares": int(position.shares),
                                      "cost": format(Decimal(str(position.cost)), "f"),
                                      "added_at": position.added_at.isoformat()})
            if position.id in output or position.symbol in symbols:
                duplicate_ids.add(position.id)
                if position.symbol in symbols:
                    duplicate_ids.add(symbols[position.symbol])
                raise PlanningError("DUPLICATE_POSITION", "重复证券或持仓身份需人工核对", status=409)
            symbols[position.symbol] = position.id
            output[position.id] = parent
        except PlanningError as exc:
            issues.append({"position_id": str(position.id)[:120], "symbol": str(position.symbol)[:120], "code": exc.code})
    for pid in duplicate_ids:
        output.pop(pid, None)
    return output, issues


def parents(ctx: AppContext) -> dict[str, Any]:
    return inspect_parents(ctx)[0]


def _store(ctx: AppContext) -> PlanningStore:
    store = ctx.planning_store
    require(type(store) is PlanningStore, "PLANNER_NOT_INITIALIZED", "手工计划库未启用；请在本地显式初始化并配置", 503)
    return store


def get_book(ctx: AppContext) -> dict[str, Any]:
    try:
        current, issues = inspect_parents(ctx)
        store = ctx.planning_store
        raw_book = store.read() if store else None
        as_of = datetime.now(timezone.utc)
        return {"schema": "manual-planning-api-v1", "enabled": store is not None,
                "status": ctx.planning_status,
                "store_id": store.store_id if store else None,
                "positions": [{**p, "parent_hash": digest(p)} for p in current.values()],
                "position_issues": issues,
                "book": public_book(raw_book, current, as_of) if raw_book is not None else None,
                "resources": resource_summary(raw_book, current, as_of) if raw_book is not None else None,
                "auto_trade": False, "assurance": "MANUAL_UNVERIFIED"}
    except PlanningError as error:
        raise APIError(error.status, error.code, str(error)) from error


def checked_command(store: PlanningStore, payload: dict[str, Any]) -> dict[str, Any]:
    fields(payload, {"store_id", "command_id", "expected_revision", "kind", "data"})
    require(payload["store_id"] == store.store_id, "STORE_IDENTITY_MISMATCH", "页面对应另一计划库，请刷新核对", 409)
    return {key: value for key, value in payload.items() if key != "store_id"}


def post_command(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        store = _store(ctx)
        result = store.apply(checked_command(store, payload), lambda: parents(ctx), datetime.now(timezone.utc))
        # Latest independent Portfolio read flags a concurrent update. No cross-DB atomicity claim.
        result["book"] = public_book(result["book"], parents(ctx), datetime.now(timezone.utc))
        result.update(schema="manual-planning-command-response-v1", store_id=store.store_id, auto_trade=False)
        return result
    except PlanningError as error:
        raise APIError(error.status, error.code, str(error)) from error


def post_preview(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = checked_command(_store(ctx), payload)
        require(payload["kind"] == "RESERVE", "PREVIEW_KIND_INVALID", "预演只接受T计划")
        current = parents(ctx)
        book = _store(ctx).read()
        preview = reduce_command(book, payload, current, datetime.now(timezone.utc))
        return {"schema": "manual-t-preview-v1", "revision": book["revision"],
                "preview": preview["plans"][payload["data"]["plan_id"]],
                "reservation_created": False, "auto_trade": False,
                "message": "仅人工输入条件预演；尚未预留、不代表可以成交"}
    except PlanningError as error:
        raise APIError(error.status, error.code, str(error)) from error


def post_attribution(ctx: AppContext, payload: dict[str, Any]) -> dict[str, Any]:
    del ctx
    try:
        return attribution_scenario(payload)
    except PlanningError as error:
        raise APIError(error.status, error.code, str(error)) from error
