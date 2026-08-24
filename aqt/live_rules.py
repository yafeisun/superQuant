from __future__ import annotations

from datetime import date

import pandas as pd

from .config import AppConfig
from .factors import evaluate_external_factors
from .flow import evaluate_money_flow
from .health import evaluate_health
from .models import Account, Bar, Order, Side
from .selection import evaluate_buy_entry, evaluate_sell_point


def health_exit_orders(
    account: Account,
    bars_by_symbol: dict[str, Bar],
    trading_day: date,
    health: pd.DataFrame | None,
    config: AppConfig,
) -> list[Order]:
    health_map = _health_map(health, config)
    orders: list[Order] = []
    for symbol, position in account.positions.items():
        if position.quantity <= 0 or symbol not in bars_by_symbol:
            continue
        reasons = str(health_map.get(symbol, {}).get("block_reasons", ""))
        if "st_or_delisting_risk" not in reasons:
            continue
        quantity = position.available if config.risk.enforce_t1 else position.quantity
        if quantity > 0:
            orders.append(Order(symbol, Side.SELL, quantity, trading_day, "health_exit_st_or_delisting_risk"))
    return orders


def sell_point_orders(
    account: Account,
    bars_by_symbol: dict[str, Bar],
    market_data: dict[str, pd.DataFrame],
    trading_day: date,
    health: pd.DataFrame | None,
    flow: pd.DataFrame | None,
    config: AppConfig,
    factors: pd.DataFrame | None = None,
) -> tuple[list[Order], list[dict]]:
    health_map = _health_map(health, config)
    flow_map = _flow_map(flow, config, trading_day)
    factor_map = _factor_map(factors, config, trading_day)
    orders: list[Order] = []
    rows: list[dict] = []
    for symbol, position in account.positions.items():
        if position.quantity <= 0 or symbol not in bars_by_symbol:
            continue
        position.last_price = bars_by_symbol[symbol].close
        evaluation = evaluate_sell_point(
            config,
            market_data,
            symbol,
            trading_day,
            position,
            health_map.get(symbol, {}),
            flow_map.get(symbol, {}),
            factor_map.get(symbol, {}),
        )
        rows.append(
            {
                "date": trading_day.isoformat(),
                "symbol": symbol,
                "quantity": position.quantity,
                "available_to_sell": position.available,
                "avg_cost": round(position.avg_cost, 4),
                "last_price": round(position.last_price, 4),
                "market_value": round(position.market_value, 2),
                "unrealized_pnl": round((position.last_price - position.avg_cost) * position.quantity, 2),
                **evaluation,
            }
        )
        if evaluation["sell_decision"] != "SELL_NOW":
            continue
        quantity = position.available if config.risk.enforce_t1 else position.quantity
        if quantity <= 0:
            continue
        orders.append(Order(symbol, Side.SELL, quantity, trading_day, evaluation["sell_reason"]))
    return orders, rows


def screen_buy_orders(
    orders: list[Order],
    health: pd.DataFrame | None,
    flow: pd.DataFrame | None,
    config: AppConfig,
    market_data: dict[str, pd.DataFrame],
    trading_day: date,
    bars_by_symbol: dict[str, Bar],
    factors: pd.DataFrame | None = None,
) -> tuple[list[Order], list[dict]]:
    health_map = _health_map(health, config)
    flow_map = _flow_map(flow, config, trading_day)
    factor_map = _factor_map(factors, config, trading_day)
    accepted: list[Order] = []
    skipped: list[dict] = []
    for order in orders:
        if order.side == Side.SELL:
            accepted.append(order)
            continue
        bar = bars_by_symbol.get(order.symbol)
        price = order.limit_price if order.limit_price is not None else bar.close if bar else 0.0
        reasons = []
        health_row = health_map.get(order.symbol, {})
        if config.health.enabled:
            if not health_row:
                reasons.append("health_data_missing")
            elif not _truthy(health_row.get("tradable")) or _safe_float(health_row.get("health_score")) < config.health.min_health_score:
                reasons.extend(_split_reasons(health_row.get("block_reasons")) or ["health_score_too_low"])
        flow_row = flow_map.get(order.symbol, {})
        if config.flow.enabled:
            if flow_row and not _truthy(flow_row.get("flow_confirmed")):
                reasons.extend(_split_reasons(flow_row.get("block_reasons")) or ["money_flow_not_confirmed"])
        factor_row = factor_map.get(order.symbol, {})
        if config.factors.enabled:
            if _truthy(factor_row.get("event_risk_flag")) or _truthy(factor_row.get("macro_risk_flag")):
                reasons.extend(_split_reasons(factor_row.get("factor_reasons")) or ["external_factor_risk"])
            elif _safe_float(factor_row.get("factor_score")) < -3.0:
                reasons.extend(_split_reasons(factor_row.get("factor_reasons")) or ["external_factor_negative"])
        entry = evaluate_buy_entry(config, market_data, order.symbol, trading_day, price)
        if not _truthy(entry.get("entry_allowed")):
            reasons.extend(_split_reasons(entry.get("entry_reason")) or ["entry_not_ready"])
        if reasons:
            skipped.append(skipped_order_row(order, bar, reasons, health_row, flow_row, entry, factor_row))
        else:
            accepted.append(order)
    return accepted, skipped


def skipped_order_row(
    order: Order,
    bar: Bar | None,
    reasons: list[str],
    health_row: dict | None = None,
    flow_row: dict | None = None,
    entry: dict | None = None,
    factor_row: dict | None = None,
) -> dict:
    health_row = health_row or {}
    flow_row = flow_row or {}
    entry = entry or {}
    factor_row = factor_row or {}
    return {
        "date": order.created_at.isoformat(),
        "symbol": order.symbol,
        "side": order.side.value,
        "quantity": order.quantity,
        "planned_quantity": order.quantity,
        "filled_quantity": 0,
        "reference_close": round(bar.close, 4) if bar else "",
        "fill_price": "",
        "status": "BUY_WAIT" if order.side == Side.BUY else "SKIPPED",
        "reason": ";".join(dict.fromkeys(reason for reason in reasons if reason)),
        "strategy_reason": order.reason,
        "health_score": health_row.get("health_score", ""),
        "health_reasons": health_row.get("block_reasons", ""),
        "flow_score": flow_row.get("flow_score", ""),
        "flow_reasons": flow_row.get("block_reasons", ""),
        "flow_positive_days": flow_row.get("positive_main_flow_days", ""),
        "flow_main_net_inflow_sum": flow_row.get("main_net_inflow_sum", ""),
        "flow_main_net_inflow_ratio_avg": flow_row.get("main_net_inflow_ratio_avg", ""),
        "entry_support_level": entry.get("entry_support_level", ""),
        "entry_target_level": entry.get("entry_target_level", ""),
        "entry_above_support_pct": entry.get("entry_above_support_pct", ""),
        "entry_below_target_pct": entry.get("entry_below_target_pct", ""),
        "entry_recent_runup_pct": entry.get("entry_recent_runup_pct", ""),
        "entry_trend_slope": entry.get("entry_trend_slope", ""),
        "factor_score": factor_row.get("factor_score", ""),
        "factor_reasons": factor_row.get("factor_reasons", ""),
        "event_summary": factor_row.get("event_summary", ""),
    }


def _health_map(health: pd.DataFrame | None, config: AppConfig) -> dict[str, dict]:
    if health is None or health.empty:
        return {}
    evaluated = evaluate_health(health, config.health)
    clean = evaluated.where(pd.notnull(evaluated), "")
    return {str(row["symbol"]): row for row in clean.to_dict(orient="records")}


def _flow_map(flow: pd.DataFrame | None, config: AppConfig, trading_day: date) -> dict[str, dict]:
    if flow is None or flow.empty:
        return {}
    evaluated = evaluate_money_flow(flow, config.flow, trading_day)
    clean = evaluated.where(pd.notnull(evaluated), "")
    return {str(row["symbol"]): row for row in clean.to_dict(orient="records")}


def _factor_map(factors: pd.DataFrame | None, config: AppConfig, trading_day: date) -> dict[str, dict]:
    if not config.factors.enabled:
        return {}
    if factors is None or factors.empty:
        evaluated = evaluate_external_factors(config.data.symbols, pd.DataFrame(), pd.DataFrame(), config.factors, trading_day)
    else:
        evaluated = factors
    clean = evaluated.where(pd.notnull(evaluated), "")
    return {str(row["symbol"]): row for row in clean.to_dict(orient="records")}


def _split_reasons(value) -> list[str]:
    if value is None or pd.isna(value):
        return []
    return [part for part in str(value).split(";") if part]


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or pd.isna(value):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _safe_float(value) -> float:
    if value is None or value == "" or pd.isna(value):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
