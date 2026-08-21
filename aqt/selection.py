from __future__ import annotations

from datetime import date

import pandas as pd

from .config import AppConfig
from .factors import evaluate_external_factors
from .flow import evaluate_money_flow
from .health import evaluate_health
from .models import Position


def evaluate_buy_entry(
    config: AppConfig,
    market_data: dict[str, pd.DataFrame],
    symbol: str,
    trading_day: date,
    price: float,
) -> dict:
    frame = market_data.get(symbol)
    if frame is None or frame.empty or price <= 0:
        return _decision(symbol, False, "entry_no_market_data")
    history = frame[frame["date"].dt.date <= trading_day].copy()
    min_required = max(config.strategy.support_window, config.strategy.target_window, config.strategy.trend_window) + 1
    if len(history) < min_required:
        return _decision(symbol, False, "entry_history_too_short")

    lows = history["low"].astype(float).tolist()
    highs = history["high"].astype(float).tolist()
    closes = history["close"].astype(float).tolist()
    support_values = lows[-config.strategy.support_window :]
    target_values = highs[-min(config.strategy.target_window, len(highs)) :]
    if not support_values or not target_values:
        return _decision(symbol, False, "entry_no_technical_levels")

    support_level = min(support_values)
    target_level = max(target_values)
    trend_values = closes[-config.strategy.trend_window - 1 :]
    trend_slope = trend_values[-1] / trend_values[0] - 1.0 if trend_values[0] > 0 else 0.0
    recent_base = closes[-6] if len(closes) >= 6 else closes[0]
    recent_runup_pct = price / recent_base - 1.0 if recent_base > 0 else 0.0
    above_support_pct = price / support_level - 1.0 if support_level > 0 else 0.0
    below_target_pct = target_level / price - 1.0 if price > 0 else 0.0

    reasons = []
    if above_support_pct > config.strategy.entry_max_above_support_pct:
        reasons.append("entry_too_far_from_support")
    if below_target_pct < config.strategy.entry_min_below_target_pct:
        reasons.append("entry_too_close_to_target")
    if recent_runup_pct > config.strategy.entry_max_recent_runup_pct:
        reasons.append("entry_recent_runup_too_hot")
    if trend_slope < config.strategy.entry_min_trend_slope:
        reasons.append("entry_trend_too_weak")

    return {
        "symbol": symbol,
        "entry_allowed": not reasons,
        "entry_reason": ";".join(reasons) if reasons else "entry_allowed",
        "entry_support_level": round(support_level, 4),
        "entry_target_level": round(target_level, 4),
        "entry_trend_slope": round(trend_slope, 6),
        "entry_above_support_pct": round(above_support_pct, 6),
        "entry_below_target_pct": round(below_target_pct, 6),
        "entry_recent_runup_pct": round(recent_runup_pct, 6),
    }


def build_selection_candidates(
    config: AppConfig,
    market_data: dict[str, pd.DataFrame],
    trading_day: date,
    health: pd.DataFrame | None = None,
    flow: pd.DataFrame | None = None,
    factors: pd.DataFrame | None = None,
) -> pd.DataFrame:
    health_map = _health_map(health, config)
    flow_map = _flow_map(flow, config, trading_day)
    factor_map = _factor_map(factors, config, trading_day)
    rows = [
        evaluate_selection_candidate(config, market_data, symbol, trading_day, health_map, flow_map, factor_map)
        for symbol in config.data.symbols
    ]
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame = frame.sort_values(["selection_score", "momentum_return", "flow_score"], ascending=[False, False, False])
    frame["selection_rank"] = range(1, len(frame) + 1)
    front = ["date", "selection_rank", "symbol", "name", "buy_decision", "selection_score", "buy_reason", "positive_reason"]
    return frame[[column for column in front if column in frame.columns] + [column for column in frame.columns if column not in front]]


def evaluate_selection_candidate(
    config: AppConfig,
    market_data: dict[str, pd.DataFrame],
    symbol: str,
    trading_day: date,
    health_map: dict[str, dict] | None = None,
    flow_map: dict[str, dict] | None = None,
    factor_map: dict[str, dict] | None = None,
) -> dict:
    health_map = health_map or {}
    flow_map = flow_map or {}
    factor_map = factor_map or {}
    metrics = _market_metrics(config, market_data, symbol, trading_day)
    health_row = health_map.get(symbol, {})
    flow_row = flow_map.get(symbol, {})
    factor_row = factor_map.get(symbol, {})
    price = _safe_float(metrics.get("close"))
    entry = evaluate_buy_entry(config, market_data, symbol, trading_day, price)

    blockers = []
    waits = []
    positives = []

    if not metrics.get("has_market_data"):
        blockers.append("market_data_missing")
    if config.health.enabled:
        if not health_row:
            blockers.append("health_data_missing")
        elif not _truthy(health_row.get("tradable")) or _safe_float(health_row.get("health_score")) < config.health.min_health_score:
            blockers.extend(_split_reasons(health_row.get("block_reasons")) or ["health_score_too_low"])
        else:
            positives.append("health_passed")
    if config.flow.enabled:
        if not flow_row:
            waits.append("money_flow_missing")
        elif not _truthy(flow_row.get("flow_confirmed")):
            waits.extend(_split_reasons(flow_row.get("block_reasons")) or ["money_flow_not_confirmed"])
        else:
            positives.append("main_money_flow_confirmed")
    if config.factors.enabled:
        if not factor_row:
            positives.append("external_factors_neutral")
        elif _truthy(factor_row.get("event_risk_flag")) or _truthy(factor_row.get("macro_risk_flag")):
            blockers.extend(_split_reasons(factor_row.get("factor_reasons")) or ["external_factor_risk"])
        elif _safe_float(factor_row.get("factor_score")) < -3.0:
            waits.extend(_split_reasons(factor_row.get("factor_reasons")) or ["external_factor_negative"])
        else:
            positives.append("external_factors_ok")
    if metrics.get("has_market_data"):
        if _safe_float(metrics.get("momentum_return")) < config.strategy.min_momentum:
            waits.append("momentum_below_threshold")
        else:
            positives.append("momentum_positive")
        if not _truthy(entry.get("entry_allowed")):
            waits.extend(_split_reasons(entry.get("entry_reason")) or ["entry_not_ready"])
        else:
            positives.append("buy_point_ready")
        if _safe_float(metrics.get("volatility_20d")) > 0.055:
            waits.append("volatility_too_high")
        else:
            positives.append("volatility_acceptable")

    decision = "BUY_READY"
    if blockers:
        decision = "BLOCK"
    elif waits:
        decision = "BUY_WAIT"

    row = {
        "date": trading_day.isoformat(),
        "symbol": symbol,
        "name": health_row.get("name", ""),
        "buy_decision": decision,
        "selection_score": _selection_score(config, metrics, health_row, flow_row, factor_row, entry, decision),
        "buy_reason": ";".join(dict.fromkeys(blockers + waits)) if blockers or waits else "all_checks_passed",
        "positive_reason": ";".join(dict.fromkeys(positives)),
        **metrics,
        "health_score": health_row.get("health_score", ""),
        "health_tradable": health_row.get("tradable", ""),
        "health_reasons": health_row.get("block_reasons", ""),
        "is_st": health_row.get("is_st", ""),
        "is_suspended": health_row.get("is_suspended", ""),
        "turnover_rate": health_row.get("turnover_rate", ""),
        "amount": health_row.get("amount", ""),
        "pe": health_row.get("pe", ""),
        "float_market_cap": health_row.get("float_market_cap", ""),
        "flow_confirmed": flow_row.get("flow_confirmed", ""),
        "flow_score": flow_row.get("flow_score", ""),
        "flow_reasons": flow_row.get("block_reasons", ""),
        "flow_positive_days": flow_row.get("positive_main_flow_days", ""),
        "flow_main_net_inflow_sum": flow_row.get("main_net_inflow_sum", ""),
        "flow_main_net_inflow_ratio_avg": flow_row.get("main_net_inflow_ratio_avg", ""),
        "flow_latest_main_net_inflow": flow_row.get("latest_main_net_inflow", ""),
        "flow_latest_main_net_inflow_ratio": flow_row.get("latest_main_net_inflow_ratio", ""),
        "factor_score": factor_row.get("factor_score", ""),
        "event_score": factor_row.get("event_score", ""),
        "macro_score": factor_row.get("macro_score", ""),
        "event_risk_flag": factor_row.get("event_risk_flag", ""),
        "macro_risk_flag": factor_row.get("macro_risk_flag", ""),
        "factor_reasons": factor_row.get("factor_reasons", ""),
        "latest_event_date": factor_row.get("latest_event_date", ""),
        "event_summary": factor_row.get("event_summary", ""),
        "macro_date": factor_row.get("macro_date", ""),
        "macro_factor": factor_row.get("macro_factor", ""),
        "macro_description": factor_row.get("macro_description", ""),
        **entry,
    }
    return row


def evaluate_sell_point(
    config: AppConfig,
    market_data: dict[str, pd.DataFrame],
    symbol: str,
    trading_day: date,
    position: Position,
    health_row: dict | None = None,
    flow_row: dict | None = None,
    factor_row: dict | None = None,
) -> dict:
    health_row = health_row or {}
    flow_row = flow_row or {}
    factor_row = factor_row or {}
    frame = market_data.get(symbol)
    price = position.last_price
    if frame is None or frame.empty or price <= 0:
        return _sell_decision(symbol, "HOLD_WATCH", "sell_no_market_data")
    history = frame[frame["date"].dt.date <= trading_day].copy()
    min_required = max(config.strategy.support_window, config.strategy.target_window, config.strategy.trend_window) + 1
    if len(history) < min_required:
        return _sell_decision(symbol, "HOLD_WATCH", "sell_history_too_short")

    lows = history["low"].astype(float).tolist()
    highs = history["high"].astype(float).tolist()
    closes = history["close"].astype(float).tolist()
    support_level = min(lows[-config.strategy.support_window :])
    resistance_level = max(highs[-min(config.strategy.target_window, len(highs)) :])
    trend_values = closes[-config.strategy.trend_window - 1 :]
    trend_slope = trend_values[-1] / trend_values[0] - 1.0 if trend_values[0] > 0 else 0.0
    take_profit_level = max(
        resistance_level,
        position.avg_cost + max(position.avg_cost - support_level, 0.0) * config.strategy.risk_reward_ratio,
    )
    recent_high = max(highs[-min(config.strategy.target_window, len(highs)) :])
    drawdown_from_high = price / recent_high - 1.0 if recent_high > 0 else 0.0
    return_pct = price / position.avg_cost - 1.0 if position.avg_cost > 0 else 0.0
    to_stop_pct = price / support_level - 1.0 if support_level > 0 else 0.0
    to_target_pct = take_profit_level / price - 1.0 if price > 0 else 0.0

    reasons = []
    watch_reasons = []
    if "st_or_delisting_risk" in str(health_row.get("block_reasons", "")):
        reasons.append("sell_st_or_delisting_risk")
    if _truthy(factor_row.get("event_risk_flag")):
        reasons.append("sell_external_event_risk")
    if _truthy(factor_row.get("macro_risk_flag")):
        reasons.append("sell_macro_risk_off")
    if price < support_level and trend_slope < 0:
        reasons.append("sell_support_break_trend_down")
    if price >= take_profit_level and trend_slope <= 0:
        reasons.append("sell_target_reached_trend_fade")
    if not _truthy(flow_row.get("flow_confirmed")) and _safe_float(flow_row.get("latest_main_net_inflow")) < 0 and trend_slope < 0:
        watch_reasons.append("sell_watch_main_flow_out_trend_down")
    if drawdown_from_high <= -0.12 and trend_slope < 0:
        watch_reasons.append("sell_watch_drawdown_from_high")
    if return_pct < -0.08 and trend_slope < 0:
        watch_reasons.append("sell_watch_loss_trend_down")

    decision = "HOLD"
    reason = "hold_price_above_support_target_not_faded"
    if reasons:
        decision = "SELL_NOW" if position.available > 0 or not config.risk.enforce_t1 else "SELL_WAIT_T1"
        reason = ";".join(dict.fromkeys(reasons))
    elif watch_reasons:
        decision = "SELL_WATCH"
        reason = ";".join(dict.fromkeys(watch_reasons))

    return {
        "symbol": symbol,
        "sell_decision": decision,
        "sell_reason": reason,
        "sell_stop_level": round(support_level, 4),
        "sell_take_profit_level": round(take_profit_level, 4),
        "sell_resistance_level": round(resistance_level, 4),
        "sell_trend_slope": round(trend_slope, 6),
        "sell_to_stop_pct": round(to_stop_pct, 6),
        "sell_to_target_pct": round(to_target_pct, 6),
        "sell_drawdown_from_high": round(drawdown_from_high, 6),
        "position_return_pct": round(return_pct, 6),
        "factor_score": factor_row.get("factor_score", ""),
        "factor_reasons": factor_row.get("factor_reasons", ""),
        "event_summary": factor_row.get("event_summary", ""),
    }


def _decision(symbol: str, allowed: bool, reason: str) -> dict:
    return {
        "symbol": symbol,
        "entry_allowed": allowed,
        "entry_reason": reason,
        "entry_support_level": "",
        "entry_target_level": "",
        "entry_trend_slope": "",
        "entry_above_support_pct": "",
        "entry_below_target_pct": "",
        "entry_recent_runup_pct": "",
    }


def _sell_decision(symbol: str, decision: str, reason: str) -> dict:
    return {
        "symbol": symbol,
        "sell_decision": decision,
        "sell_reason": reason,
        "sell_stop_level": "",
        "sell_take_profit_level": "",
        "sell_resistance_level": "",
        "sell_trend_slope": "",
        "sell_to_stop_pct": "",
        "sell_to_target_pct": "",
        "sell_drawdown_from_high": "",
        "position_return_pct": "",
    }


def _market_metrics(config: AppConfig, market_data: dict[str, pd.DataFrame], symbol: str, trading_day: date) -> dict:
    frame = market_data.get(symbol)
    if frame is None or frame.empty:
        return {
            "has_market_data": False,
            "close": "",
            "momentum_return": "",
            "short_momentum_return": "",
            "volatility_20d": "",
            "drawdown_from_60d_high": "",
            "ma20_distance_pct": "",
        }
    history = frame[frame["date"].dt.date <= trading_day].copy()
    if history.empty:
        return {
            "has_market_data": False,
            "close": "",
            "momentum_return": "",
            "short_momentum_return": "",
            "volatility_20d": "",
            "drawdown_from_60d_high": "",
            "ma20_distance_pct": "",
        }

    closes = history["close"].astype(float)
    highs = history["high"].astype(float)
    close = float(closes.iloc[-1])
    momentum = _window_return(closes, config.strategy.momentum_window)
    short_momentum = _window_return(closes, min(20, max(len(closes) - 1, 1)))
    returns = closes.pct_change().dropna()
    volatility = float(returns.tail(20).std()) if len(returns) >= 2 else 0.0
    high_60 = float(highs.tail(min(60, len(highs))).max())
    drawdown = close / high_60 - 1.0 if high_60 > 0 else 0.0
    ma20 = float(closes.tail(min(20, len(closes))).mean())
    ma20_distance = close / ma20 - 1.0 if ma20 > 0 else 0.0
    return {
        "has_market_data": True,
        "close": round(close, 4),
        "momentum_return": round(momentum, 6),
        "short_momentum_return": round(short_momentum, 6),
        "volatility_20d": round(volatility, 6),
        "drawdown_from_60d_high": round(drawdown, 6),
        "ma20_distance_pct": round(ma20_distance, 6),
    }


def _selection_score(
    config: AppConfig,
    metrics: dict,
    health_row: dict,
    flow_row: dict,
    factor_row: dict,
    entry: dict,
    decision: str,
) -> float:
    health_score = _safe_float(health_row.get("health_score"))
    flow_score = _safe_float(flow_row.get("flow_score"))
    momentum = _safe_float(metrics.get("momentum_return"))
    volatility = _safe_float(metrics.get("volatility_20d"))
    drawdown = abs(_safe_float(metrics.get("drawdown_from_60d_high")))
    amount = _safe_float(health_row.get("amount"))
    turnover = _safe_float(health_row.get("turnover_rate"))
    pe = _safe_float(health_row.get("pe"))
    above_support = _safe_float(entry.get("entry_above_support_pct"))
    below_target = _safe_float(entry.get("entry_below_target_pct"))
    factor_score = _safe_float(factor_row.get("factor_score"))

    score = 0.0
    score += 20.0 * _clamp(health_score / 100.0, 0.0, 1.0)
    score += 25.0 * _clamp(flow_score / 100.0, 0.0, 1.0)
    score += 20.0 * _clamp((momentum - config.strategy.min_momentum) / 0.25, 0.0, 1.0)
    score += 8.0 * _clamp(amount / max(config.health.min_amount * 3.0, 1.0), 0.0, 1.0)
    score += 5.0 * _clamp(turnover / max(config.health.min_turnover_rate * 4.0, 1.0), 0.0, 1.0)
    if config.health.min_pe < pe <= min(config.health.max_pe, 80.0):
        score += 5.0
    score += 7.0 * (1.0 - _clamp(volatility / 0.06, 0.0, 1.0))
    score += 4.0 * (1.0 - _clamp(drawdown / 0.25, 0.0, 1.0))
    if _truthy(entry.get("entry_allowed")):
        score += 6.0
    score += 3.0 * (1.0 - _clamp(above_support / max(config.strategy.entry_max_above_support_pct, 0.01), 0.0, 1.0))
    score += 2.0 * _clamp(below_target / 0.15, 0.0, 1.0)
    if config.factors.enabled:
        score += _clamp(factor_score, -config.factors.max_score_adjustment, config.factors.max_score_adjustment)
    if decision == "BLOCK":
        score = min(score, 20.0)
    elif decision == "BUY_WAIT":
        score = min(score, 65.0)
    return round(score, 2)


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


def _window_return(values: pd.Series, window: int) -> float:
    if len(values) < window + 1:
        return 0.0
    base = float(values.iloc[-window - 1])
    current = float(values.iloc[-1])
    return current / base - 1.0 if base > 0 else 0.0


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


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))
