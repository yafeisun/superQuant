from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from aqt.broker import PaperBroker
from aqt.calendar import is_a_share_trading_day, parse_calendar_date
from aqt.config import AppConfig, load_config
from aqt.data import fetch_akshare_daily, iter_bars, load_market_data
from aqt.factors import evaluate_external_factors, load_external_factors
from aqt.flow import evaluate_money_flow, fetch_money_flow, load_money_flow
from aqt.health import evaluate_health, fetch_stock_health, load_stock_health, write_health_report
from aqt.live_rules import screen_buy_orders, sell_point_orders
from aqt.models import Account, Bar, Fill, Order, Position, Side
from aqt.selection import build_selection_candidates, evaluate_sell_point
from aqt.strategy import build_strategy


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local virtual paper trading with a persistent account")
    parser.add_argument("--config", default="configs/smallcap_live.yaml")
    parser.add_argument("--state-dir", default="local_runs/paper_live")
    parser.add_argument("--date", default=None, help="YYYYMMDD or YYYY-MM-DD, defaults to today in Asia/Shanghai")
    parser.add_argument("--market-end", default=None, help="YYYY-MM-DD data end override, defaults to target date")
    parser.add_argument("--adjust", default="qfq")
    parser.add_argument("--no-fetch", action="store_true", help="use existing local CSV data without downloading")
    parser.add_argument("--no-refresh-factors", action="store_true", help="use cached health and money-flow factors")
    parser.add_argument("--force", action="store_true", help="run even when the target date is not an A-share trading day")
    parser.add_argument("--rerun", action="store_true", help="allow executing orders again for an already processed date")
    args = parser.parse_args()

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    target_date = parse_calendar_date(args.date, now.date())
    if not args.force and not is_a_share_trading_day(target_date):
        print(f"skip: {target_date.isoformat()} is not an A-share trading day")
        return

    config = _with_market_end(load_config(args.config), args.market_end or target_date.isoformat())
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    account = _load_account(state_dir / "account.json", config)
    config = _with_symbols(config, _effective_symbols(config, account))

    health = load_stock_health(config.health.path) if args.no_refresh_factors else _refresh_health(config)

    if not args.no_fetch:
        fetch_akshare_daily(config.data.path, config.data.symbols, config.data.start, config.data.end, args.adjust)

    flow = load_money_flow(config.flow.path) if args.no_refresh_factors else _refresh_money_flow(config)
    factors = load_external_factors(config.factors, config.data.symbols, target_date)

    result = run_once(config, state_dir, target_date, allow_rerun=args.rerun, health=health, flow=flow, factors=factors)
    print(result["summary_path"])


def run_once(
    config: AppConfig,
    state_dir: Path,
    target_date: date,
    allow_rerun: bool = False,
    health: pd.DataFrame | None = None,
    flow: pd.DataFrame | None = None,
    factors: pd.DataFrame | None = None,
) -> dict:
    account = _load_account(state_dir / "account.json", config)
    config = _with_symbols(config, _effective_symbols(config, account))
    market_data = load_market_data(config.data.path, config.data.symbols, config.data.start, config.data.end, strict=False)
    all_bars = list(iter_bars(market_data))
    if not all_bars:
        raise RuntimeError("no market data loaded")

    target_bars = _find_bars(all_bars, target_date)
    if not target_bars:
        summary_path = _write_no_data_summary(state_dir, target_date)
        return {"summary_path": summary_path, "status": "no_data"}

    processed_dates = _read_processed_dates(state_dir / "processed_dates.csv")
    already_processed = target_date.isoformat() in processed_dates

    broker = PaperBroker(config.account, config.risk)
    broker.account = account

    strategy = build_strategy(config.strategy)
    warmup_account = Account(cash=0.0)
    for bars in all_bars:
        if bars[0].date >= target_date:
            break
        strategy.on_bars(bars, warmup_account)

    broker.mark_to_market(target_bars)
    bars_by_symbol = {bar.symbol: bar for bar in target_bars}
    day_dir = state_dir / "decisions" / target_date.strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    selection_candidates = build_selection_candidates(config, market_data, target_date, health, flow, factors)
    if not selection_candidates.empty:
        selection_candidates.to_csv(day_dir / "selection_candidates.csv", index=False)

    strategy_orders = strategy.on_bars(target_bars, broker.account)
    sell_orders, sell_point_rows = sell_point_orders(
        broker.account, bars_by_symbol, market_data, target_date, health, flow, config, factors
    )
    sell_orders = _dedupe_orders(sell_orders + [order for order in strategy_orders if order.side == Side.SELL])
    buy_orders: list[Order] = []
    skipped_orders: list[dict] = []
    if _should_consider_buys(strategy_orders, broker.account, config):
        buy_orders, skipped_orders = _selection_entry_orders(
            config, selection_candidates, bars_by_symbol, broker.account, target_date
        )
        buy_orders, screened_skips = screen_buy_orders(
            buy_orders, health, flow, config, market_data, target_date, bars_by_symbol, factors
        )
        skipped_orders.extend(screened_skips)
    orders = sell_orders + buy_orders
    if already_processed and not allow_rerun:
        summary_path = day_dir / "summary.txt"
        if not summary_path.exists():
            _write_summary(summary_path, target_date, "already_processed", broker.account, [], [], [])
        if factors is not None and not factors.empty:
            factors.to_csv(day_dir / "external_factors.csv", index=False)
        _write_position_advice(day_dir, target_date, broker.account, market_data, health, flow, factors, config, [], sell_point_rows)
        return {"summary_path": summary_path, "status": "already_processed"}

    fills: list[Fill] = []
    rejections: list[dict] = []
    status = "executed"

    fills = broker.execute_orders(orders, bars_by_symbol)
    broker.mark_to_market(target_bars)
    broker.settle_day()
    rejections = list(broker.rejected_orders)
    _append_processed_date(state_dir / "processed_dates.csv", target_date)

    _save_account(state_dir / "account.json", broker.account)
    _append_rows(state_dir / "fills.csv", [_fill_row(fill) for fill in fills])
    _append_rows(state_dir / "rejections.csv", rejections)
    _upsert_equity(state_dir / "equity.csv", _equity_row(target_date, broker.account, fills, config.account.initial_cash))
    _write_positions(state_dir / "positions.csv", target_date, broker.account)

    order_rows = _order_rows(orders, fills, rejections, bars_by_symbol, skipped_orders)
    pd.DataFrame(order_rows).to_csv(day_dir / "orders.csv", index=False)
    if health is not None and not health.empty:
        write_health_report(health, config.health, day_dir / "health.csv")
    if flow is not None and not flow.empty:
        evaluate_money_flow(flow, config.flow, target_date).to_csv(day_dir / "money_flow.csv", index=False)
    if factors is not None and not factors.empty:
        factors.to_csv(day_dir / "external_factors.csv", index=False)
    _write_position_advice(day_dir, target_date, broker.account, market_data, health, flow, factors, config, fills, sell_point_rows)
    summary_path = _write_summary(day_dir / "summary.txt", target_date, status, broker.account, orders, fills, rejections)
    return {"summary_path": summary_path, "status": status}


def _with_market_end(config: AppConfig, market_end: str) -> AppConfig:
    return replace(config, data=replace(config.data, end=market_end))


def _with_symbols(config: AppConfig, symbols: list[str]) -> AppConfig:
    return replace(config, data=replace(config.data, symbols=symbols))


def _effective_symbols(config: AppConfig, account: Account) -> list[str]:
    symbols = set(config.data.symbols)
    symbols.update(symbol for symbol, position in account.positions.items() if position.quantity > 0)
    return sorted(symbols)


def _refresh_health(config: AppConfig) -> pd.DataFrame:
    if not config.health.enabled:
        return load_stock_health(config.health.path)
    try:
        refreshed = fetch_stock_health(config.data.symbols, config.health.path)
        return refreshed if not refreshed.empty else load_stock_health(config.health.path)
    except Exception:
        return load_stock_health(config.health.path)


def _refresh_money_flow(config: AppConfig) -> pd.DataFrame:
    if not config.flow.enabled:
        return load_money_flow(config.flow.path)
    try:
        refreshed = fetch_money_flow(config.data.symbols, config.flow.path, config.flow.lookback_days)
        return refreshed if not refreshed.empty else load_money_flow(config.flow.path)
    except Exception:
        return load_money_flow(config.flow.path)


def _find_bars(all_bars: list[list[Bar]], target_date: date) -> list[Bar]:
    for bars in all_bars:
        if bars[0].date == target_date:
            return bars
    return []


def _is_empty_account(account: Account) -> bool:
    return not any(position.quantity > 0 for position in account.positions.values())


def _should_consider_buys(strategy_orders: list[Order], account: Account, config: AppConfig) -> bool:
    active_positions = sum(1 for position in account.positions.values() if position.quantity > 0)
    if active_positions >= config.strategy.top_n:
        return False
    return _is_empty_account(account) or any(order.side == Side.BUY for order in strategy_orders)


def _selection_entry_orders(
    config: AppConfig,
    selection_candidates: pd.DataFrame,
    bars_by_symbol: dict[str, Bar],
    account: Account,
    trading_day: date,
) -> tuple[list[Order], list[dict]]:
    if selection_candidates.empty:
        return [], []
    held_symbols = {symbol for symbol, position in account.positions.items() if position.quantity > 0}
    available_slots = max(config.strategy.top_n - len(held_symbols), 0)
    if available_slots <= 0:
        return [], []
    target_value_per_name = account.equity() * config.strategy.target_gross_exposure / config.strategy.top_n
    orders: list[Order] = []
    skipped: list[dict] = []
    audit_limit = config.strategy.top_n * max(config.strategy.entry_candidate_multiplier, 1)
    for row in selection_candidates.to_dict(orient="records"):
        symbol = str(row.get("symbol", ""))
        if symbol in held_symbols or symbol not in bars_by_symbol:
            continue
        bar = bars_by_symbol[symbol]
        if target_value_per_name < config.strategy.min_trade_value:
            continue
        quantity = int(target_value_per_name / bar.close)
        order = Order(symbol, Side.BUY, quantity, trading_day, "full_selection_buy")
        if row.get("buy_decision") == "BUY_READY":
            orders.append(order)
            if len(orders) >= available_slots:
                break
            continue
        if len(skipped) >= audit_limit:
            continue
        skipped.append(_candidate_wait_row(order, bar, row))
    return orders, skipped


def _candidate_wait_row(order: Order, bar: Bar, row: dict) -> dict:
    return {
        "date": order.created_at.isoformat(),
        "symbol": order.symbol,
        "side": order.side.value,
        "planned_quantity": order.quantity,
        "filled_quantity": 0,
        "reference_close": round(bar.close, 4),
        "fill_price": "",
        "status": row.get("buy_decision", "BUY_WAIT"),
        "reason": row.get("buy_reason", ""),
        "strategy_reason": order.reason,
        "selection_rank": row.get("selection_rank", ""),
        "selection_score": row.get("selection_score", ""),
        "positive_reason": row.get("positive_reason", ""),
        "health_score": row.get("health_score", ""),
        "health_reasons": row.get("health_reasons", ""),
        "flow_score": row.get("flow_score", ""),
        "flow_reasons": row.get("flow_reasons", ""),
        "flow_positive_days": row.get("flow_positive_days", ""),
        "flow_main_net_inflow_sum": row.get("flow_main_net_inflow_sum", ""),
        "flow_main_net_inflow_ratio_avg": row.get("flow_main_net_inflow_ratio_avg", ""),
        "entry_support_level": row.get("entry_support_level", ""),
        "entry_target_level": row.get("entry_target_level", ""),
        "entry_above_support_pct": row.get("entry_above_support_pct", ""),
        "entry_below_target_pct": row.get("entry_below_target_pct", ""),
        "entry_recent_runup_pct": row.get("entry_recent_runup_pct", ""),
        "entry_trend_slope": row.get("entry_trend_slope", ""),
        "factor_score": row.get("factor_score", ""),
        "factor_reasons": row.get("factor_reasons", ""),
        "event_summary": row.get("event_summary", ""),
    }


def _dedupe_orders(orders: list[Order]) -> list[Order]:
    deduped: dict[tuple[str, Side], Order] = {}
    for order in orders:
        key = (order.symbol, order.side)
        previous = deduped.get(key)
        if previous is None:
            deduped[key] = order
            continue
        quantity = max(previous.quantity, order.quantity)
        reasons = ";".join(dict.fromkeys([previous.reason, order.reason]))
        deduped[key] = Order(order.symbol, order.side, quantity, order.created_at, reasons, order.limit_price)
    return list(deduped.values())


def _load_account(path: Path, config: AppConfig) -> Account:
    if not path.exists():
        return Account(cash=config.account.initial_cash)
    raw = json.loads(path.read_text(encoding="utf-8"))
    positions = {
        symbol: Position(
            symbol=symbol,
            quantity=int(row.get("quantity", 0)),
            avg_cost=float(row.get("avg_cost", 0.0)),
            available=int(row.get("available", 0)),
            last_price=float(row.get("last_price", 0.0)),
        )
        for symbol, row in raw.get("positions", {}).items()
    }
    updated_at = raw.get("updated_at")
    return Account(
        cash=float(raw.get("cash", config.account.initial_cash)),
        positions=positions,
        realized_pnl=float(raw.get("realized_pnl", 0.0)),
        updated_at=datetime.fromisoformat(updated_at) if updated_at else None,
    )


def _save_account(path: Path, account: Account) -> None:
    payload = {
        "cash": round(account.cash, 4),
        "realized_pnl": round(account.realized_pnl, 4),
        "equity": round(account.equity(), 4),
        "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "positions": {symbol: asdict(position) for symbol, position in account.positions.items() if position.quantity > 0},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_processed_dates(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        frame = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return set()
    if "date" not in frame.columns:
        return set()
    return set(frame["date"].astype(str))


def _append_processed_date(path: Path, trading_day: date) -> None:
    if trading_day.isoformat() in _read_processed_dates(path):
        return
    _append_rows(path, [{"date": trading_day.isoformat()}])


def _fill_row(fill: Fill) -> dict:
    row = asdict(fill)
    row["side"] = fill.side.value
    row["filled_at"] = fill.filled_at.isoformat()
    return row


def _equity_row(trading_day: date, account: Account, fills: list[Fill], initial_cash: float) -> dict:
    market_value = sum(position.market_value for position in account.positions.values())
    return {
        "date": trading_day.isoformat(),
        "cash": round(account.cash, 4),
        "market_value": round(market_value, 4),
        "equity": round(account.equity(), 4),
        "realized_pnl": round(account.realized_pnl, 4),
        "return_pct": round(account.equity() / initial_cash - 1.0, 6) if initial_cash else 0.0,
        "fills": len(fills),
    }


def _write_positions(path: Path, trading_day: date, account: Account) -> None:
    rows = []
    for symbol, position in account.positions.items():
        if position.quantity <= 0:
            continue
        unrealized_pnl = (position.last_price - position.avg_cost) * position.quantity
        rows.append(
            {
                "date": trading_day.isoformat(),
                "symbol": symbol,
                "quantity": position.quantity,
                "available_to_sell": position.available,
                "avg_cost": round(position.avg_cost, 4),
                "last_price": round(position.last_price, 4),
                "market_value": round(position.market_value, 4),
                "unrealized_pnl": round(unrealized_pnl, 4),
                "return_pct": round(position.last_price / position.avg_cost - 1.0, 6) if position.avg_cost else 0.0,
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False)


def _order_rows(
    orders: list[Order],
    fills: list[Fill],
    rejections: list[dict],
    bars_by_symbol: dict[str, Bar],
    skipped_orders: list[dict] | None = None,
) -> list[dict]:
    fills_by_key = {(fill.symbol, fill.side.value, fill.reason): fill for fill in fills}
    rejections_by_key = {(row["symbol"], row["side"], row.get("strategy_reason", "")): row for row in rejections}
    rows = list(skipped_orders or [])
    for order in orders:
        key = (order.symbol, order.side.value, order.reason)
        fill = fills_by_key.get(key)
        rejection = rejections_by_key.get(key)
        status = "FILLED" if fill else "REJECTED" if rejection else "SKIPPED"
        bar = bars_by_symbol.get(order.symbol)
        rows.append(
            {
                "date": order.created_at.isoformat(),
                "symbol": order.symbol,
                "side": order.side.value,
                "planned_quantity": order.quantity,
                "filled_quantity": fill.quantity if fill else 0,
                "reference_close": round(bar.close, 4) if bar else "",
                "fill_price": round(fill.price, 4) if fill else "",
                "status": status,
                "reason": rejection.get("reason", order.reason) if rejection else order.reason,
                "strategy_reason": order.reason,
            }
        )
    return rows


def _write_position_advice(
    day_dir: Path,
    trading_day: date,
    account: Account,
    market_data: dict[str, pd.DataFrame],
    health: pd.DataFrame | None,
    flow: pd.DataFrame | None,
    factors: pd.DataFrame | None,
    config: AppConfig,
    fills: list[Fill],
    sell_point_rows: list[dict] | None = None,
) -> None:
    fill_map = {fill.symbol: fill for fill in fills}
    health_map = _health_map(health, config)
    flow_map = _flow_map(flow, config, trading_day)
    factor_map = _factor_map(factors, config, trading_day)
    rows = []
    for symbol, position in sorted(account.positions.items()):
        if position.quantity <= 0:
            continue
        return_pct = position.last_price / position.avg_cost - 1.0 if position.avg_cost else 0.0
        health_row = health_map.get(symbol, {})
        flow_row = flow_map.get(symbol, {})
        factor_row = factor_map.get(symbol, {})
        today_fill = fill_map.get(symbol)
        sell_eval = evaluate_sell_point(config, market_data, symbol, trading_day, position, health_row, flow_row, factor_row)
        action, advice = _position_action(sell_eval, today_fill)
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
                "return_pct": round(return_pct, 6),
                "support_level": sell_eval.get("sell_stop_level", ""),
                "upside_target_level": sell_eval.get("sell_take_profit_level", ""),
                "trend_slope": sell_eval.get("sell_trend_slope", ""),
                "sell_decision": sell_eval.get("sell_decision", ""),
                "sell_reason": sell_eval.get("sell_reason", ""),
                "sell_stop_level": sell_eval.get("sell_stop_level", ""),
                "sell_take_profit_level": sell_eval.get("sell_take_profit_level", ""),
                "sell_to_stop_pct": sell_eval.get("sell_to_stop_pct", ""),
                "sell_to_target_pct": sell_eval.get("sell_to_target_pct", ""),
                "sell_drawdown_from_high": sell_eval.get("sell_drawdown_from_high", ""),
                "health_score": health_row.get("health_score", ""),
                "tradable": health_row.get("tradable", ""),
                "health_reasons": health_row.get("block_reasons", ""),
                "flow_score": flow_row.get("flow_score", ""),
                "flow_reasons": flow_row.get("block_reasons", ""),
                "flow_positive_days": flow_row.get("positive_main_flow_days", ""),
                "flow_main_net_inflow_sum": flow_row.get("main_net_inflow_sum", ""),
                "flow_main_net_inflow_ratio_avg": flow_row.get("main_net_inflow_ratio_avg", ""),
                "factor_score": factor_row.get("factor_score", ""),
                "factor_reasons": factor_row.get("factor_reasons", ""),
                "event_summary": factor_row.get("event_summary", ""),
                "today_action": action,
                "next_advice": advice,
            }
        )

    pd.DataFrame(rows).to_csv(day_dir / "position_advice.csv", index=False)
    if sell_point_rows:
        pd.DataFrame(sell_point_rows).to_csv(day_dir / "sell_points.csv", index=False)
    _write_position_advice_markdown(day_dir / "position_advice.md", trading_day, rows)


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


def _position_action(sell_eval: dict, today_fill: Fill | None) -> tuple[str, str]:
    if today_fill and today_fill.side == Side.BUY:
        return "BOUGHT", sell_eval.get("sell_reason", "new_position_follow_sell_point")
    if today_fill and today_fill.side == Side.SELL:
        return "SOLD", f"exit_executed_by_{today_fill.reason}"
    return str(sell_eval.get("sell_decision", "HOLD_WATCH")), str(sell_eval.get("sell_reason", "hold_watch"))


def _write_position_advice_markdown(path: Path, trading_day: date, rows: list[dict]) -> None:
    lines = [f"# Position Advice {trading_day.isoformat()}", ""]
    if not rows:
        lines.extend(["No open positions.", ""])
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    lines.append("| Symbol | Qty | Last | PnL | Stop | Target | To Stop | To Target | Action | Reason |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |")
    for row in rows:
        lines.append(
            "| {symbol} | {quantity} | {last_price} | {unrealized_pnl} | {sell_stop_level} | {sell_take_profit_level} | {sell_to_stop_pct} | {sell_to_target_pct} | {today_action} | {next_advice} |".format(
                **row
            )
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _append_rows(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        if handle.tell() == 0:
            writer.writeheader()
        writer.writerows(rows)


def _upsert_equity(path: Path, row: dict) -> None:
    if path.exists():
        frame = pd.read_csv(path)
        frame = frame[frame["date"].astype(str) != row["date"]]
        frame = pd.concat([frame, pd.DataFrame([row])], ignore_index=True)
    else:
        frame = pd.DataFrame([row])
    frame = frame.sort_values("date")
    frame.to_csv(path, index=False)


def _write_summary(
    path: Path,
    trading_day: date,
    status: str,
    account: Account,
    orders: list[Order],
    fills: list[Fill],
    rejections: list[dict],
) -> Path:
    market_value = sum(position.market_value for position in account.positions.values())
    lines = [
        f"date: {trading_day.isoformat()}",
        f"status: {status}",
        f"cash: {account.cash:.2f}",
        f"market_value: {market_value:.2f}",
        f"equity: {account.equity():.2f}",
        f"realized_pnl: {account.realized_pnl:.2f}",
        f"positions: {sum(1 for position in account.positions.values() if position.quantity > 0)}",
        f"orders: {len(orders)}",
        f"fills: {len(fills)}",
        f"rejections: {len(rejections)}",
        "",
        "fills_detail:",
    ]
    if fills:
        for fill in fills:
            lines.append(
                f"  {fill.side.value} {fill.symbol} qty={fill.quantity} price={fill.price:.4f} reason={fill.reason}"
            )
    else:
        lines.append("  NONE")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_no_data_summary(state_dir: Path, target_date: date) -> Path:
    day_dir = state_dir / "decisions" / target_date.strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "summary.txt"
    path.write_text(
        f"date: {target_date.isoformat()}\nstatus: no_data\nmessage: no local bar data for target date\n",
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    main()
