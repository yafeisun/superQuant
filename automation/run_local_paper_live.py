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
from aqt.health import evaluate_health, fetch_stock_health, healthy_symbols, load_stock_health, write_health_report
from aqt.models import Account, Bar, Fill, Order, Position, Side
from aqt.strategy import build_strategy


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local virtual paper trading with a persistent account")
    parser.add_argument("--config", default="configs/smallcap_live.yaml")
    parser.add_argument("--state-dir", default="local_runs/paper_live")
    parser.add_argument("--date", default=None, help="YYYYMMDD or YYYY-MM-DD, defaults to today in Asia/Shanghai")
    parser.add_argument("--market-end", default=None, help="YYYY-MM-DD data end override, defaults to target date")
    parser.add_argument("--adjust", default="qfq")
    parser.add_argument("--no-fetch", action="store_true", help="use existing local CSV data without downloading")
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

    health = _refresh_health(config)

    if not args.no_fetch:
        fetch_akshare_daily(config.data.path, config.data.symbols, config.data.start, config.data.end, args.adjust)

    result = run_once(config, state_dir, target_date, allow_rerun=args.rerun, health=health)
    print(result["summary_path"])


def run_once(
    config: AppConfig,
    state_dir: Path,
    target_date: date,
    allow_rerun: bool = False,
    health: pd.DataFrame | None = None,
) -> dict:
    market_data = load_market_data(config.data.path, config.data.symbols, config.data.start, config.data.end)
    all_bars = list(iter_bars(market_data))
    if not all_bars:
        raise RuntimeError("no market data loaded")

    target_bars = _find_bars(all_bars, target_date)
    if not target_bars:
        summary_path = _write_no_data_summary(state_dir, target_date)
        return {"summary_path": summary_path, "status": "no_data"}

    processed_dates = _read_processed_dates(state_dir / "processed_dates.csv")
    already_processed = target_date.isoformat() in processed_dates

    account = _load_account(state_dir / "account.json", config)
    broker = PaperBroker(config.account, config.risk)
    broker.account = account

    strategy = build_strategy(config.strategy)
    warmup_account = Account(cash=0.0)
    for bars in all_bars:
        if bars[0].date >= target_date:
            break
        strategy.on_bars(bars, warmup_account)

    broker.mark_to_market(target_bars)
    orders = strategy.on_bars(target_bars, broker.account)
    orders = _filter_buy_orders(orders, health, config)
    if not orders and _is_empty_account(broker.account):
        orders = _initial_entry_orders(config, market_data, target_bars, broker.account, health)

    day_dir = state_dir / "decisions" / target_date.strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    if already_processed and not allow_rerun:
        summary_path = day_dir / "summary.txt"
        if not summary_path.exists():
            _write_summary(summary_path, target_date, "already_processed", broker.account, [], [], [])
        _write_position_advice(day_dir, target_date, broker.account, market_data, health, config, [])
        return {"summary_path": summary_path, "status": "already_processed"}

    fills: list[Fill] = []
    rejections: list[dict] = []
    status = "executed"

    fills = broker.execute_orders(orders, {bar.symbol: bar for bar in target_bars})
    broker.mark_to_market(target_bars)
    broker.settle_day()
    rejections = list(broker.rejected_orders)
    _append_processed_date(state_dir / "processed_dates.csv", target_date)

    _save_account(state_dir / "account.json", broker.account)
    _append_rows(state_dir / "fills.csv", [_fill_row(fill) for fill in fills])
    _append_rows(state_dir / "rejections.csv", rejections)
    _upsert_equity(state_dir / "equity.csv", _equity_row(target_date, broker.account, fills, config.account.initial_cash))
    _write_positions(state_dir / "positions.csv", target_date, broker.account)

    order_rows = _order_rows(orders, fills, rejections, {bar.symbol: bar for bar in target_bars})
    pd.DataFrame(order_rows).to_csv(day_dir / "orders.csv", index=False)
    if health is not None and not health.empty:
        write_health_report(health, config.health, day_dir / "health.csv")
    _write_position_advice(day_dir, target_date, broker.account, market_data, health, config, fills)
    summary_path = _write_summary(day_dir / "summary.txt", target_date, status, broker.account, orders, fills, rejections)
    return {"summary_path": summary_path, "status": status}


def _with_market_end(config: AppConfig, market_end: str) -> AppConfig:
    return replace(config, data=replace(config.data, end=market_end))


def _refresh_health(config: AppConfig) -> pd.DataFrame:
    if not config.health.enabled:
        return load_stock_health(config.health.path)
    try:
        return fetch_stock_health(config.data.symbols, config.health.path)
    except Exception:
        return load_stock_health(config.health.path)


def _find_bars(all_bars: list[list[Bar]], target_date: date) -> list[Bar]:
    for bars in all_bars:
        if bars[0].date == target_date:
            return bars
    return []


def _is_empty_account(account: Account) -> bool:
    return not any(position.quantity > 0 for position in account.positions.values())


def _initial_entry_orders(
    config: AppConfig,
    market_data: dict[str, pd.DataFrame],
    target_bars: list[Bar],
    account: Account,
    health: pd.DataFrame | None,
) -> list[Order]:
    bars_by_symbol = {bar.symbol: bar for bar in target_bars}
    ranked = sorted(
        _momentum_scores(market_data, target_bars[0].date, config.strategy.momentum_window),
        key=lambda item: item[1],
        reverse=True,
    )
    allowed = set(healthy_symbols(config.data.symbols, health if health is not None else pd.DataFrame(), config.health))
    selected = [
        symbol
        for symbol, score in ranked
        if score >= config.strategy.min_momentum and symbol in bars_by_symbol and symbol in allowed
    ][: config.strategy.top_n]
    if not selected:
        return []

    target_value_per_name = account.equity() * config.strategy.target_gross_exposure / len(selected)
    orders: list[Order] = []
    for symbol in selected:
        bar = bars_by_symbol[symbol]
        if target_value_per_name < config.strategy.min_trade_value:
            continue
        quantity = int(target_value_per_name / bar.close)
        orders.append(Order(symbol, Side.BUY, quantity, bar.date, "initial_entry"))
    return orders


def _momentum_scores(market_data: dict[str, pd.DataFrame], target_date: date, window: int) -> list[tuple[str, float]]:
    scores: list[tuple[str, float]] = []
    for symbol, frame in market_data.items():
        history = frame[frame["date"].dt.date <= target_date]
        if len(history) < window + 1:
            continue
        current = float(history.iloc[-1]["close"])
        past = float(history.iloc[-window - 1]["close"])
        if past <= 0:
            continue
        scores.append((symbol, current / past - 1.0))
    return scores


def _filter_buy_orders(orders: list[Order], health: pd.DataFrame | None, config: AppConfig) -> list[Order]:
    allowed = set(healthy_symbols(config.data.symbols, health if health is not None else pd.DataFrame(), config.health))
    return [order for order in orders if order.side == Side.SELL or order.symbol in allowed]


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


def _order_rows(orders: list[Order], fills: list[Fill], rejections: list[dict], bars_by_symbol: dict[str, Bar]) -> list[dict]:
    fills_by_key = {(fill.symbol, fill.side.value, fill.reason): fill for fill in fills}
    rejections_by_key = {(row["symbol"], row["side"], row.get("strategy_reason", "")): row for row in rejections}
    rows = []
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
    config: AppConfig,
    fills: list[Fill],
) -> None:
    fill_map = {fill.symbol: fill for fill in fills}
    health_map = _health_map(health, config)
    rows = []
    for symbol, position in sorted(account.positions.items()):
        if position.quantity <= 0:
            continue
        levels = _position_levels(config, market_data, symbol, trading_day, position.avg_cost)
        support_level, upside_target_level, trend_slope = levels if levels else (0.0, 0.0, 0.0)
        return_pct = position.last_price / position.avg_cost - 1.0 if position.avg_cost else 0.0
        health_row = health_map.get(symbol, {})
        today_fill = fill_map.get(symbol)
        action, advice = _position_action(position, support_level, upside_target_level, trend_slope, health_row, today_fill)
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
                "support_level": round(support_level, 4) if support_level else "",
                "upside_target_level": round(upside_target_level, 4) if upside_target_level else "",
                "trend_slope": round(trend_slope, 6),
                "health_score": health_row.get("health_score", ""),
                "tradable": health_row.get("tradable", ""),
                "health_reasons": health_row.get("block_reasons", ""),
                "today_action": action,
                "next_advice": advice,
            }
        )

    pd.DataFrame(rows).to_csv(day_dir / "position_advice.csv", index=False)
    _write_position_advice_markdown(day_dir / "position_advice.md", trading_day, rows)


def _health_map(health: pd.DataFrame | None, config: AppConfig) -> dict[str, dict]:
    if health is None or health.empty:
        return {}
    evaluated = evaluate_health(health, config.health)
    clean = evaluated.where(pd.notnull(evaluated), "")
    return {str(row["symbol"]): row for row in clean.to_dict(orient="records")}


def _position_levels(
    config: AppConfig,
    market_data: dict[str, pd.DataFrame],
    symbol: str,
    trading_day: date,
    avg_cost: float,
) -> tuple[float, float, float] | None:
    frame = market_data.get(symbol)
    if frame is None or frame.empty:
        return None
    history = frame[frame["date"].dt.date <= trading_day]
    if len(history) < max(config.strategy.support_window, config.strategy.trend_window) + 1:
        return None
    closes = history["close"].astype(float).tolist()
    support_values = closes[-config.strategy.support_window :]
    target_values = closes[-min(config.strategy.target_window, len(closes)) :]
    if not support_values or not target_values:
        return None
    support_level = min(support_values)
    resistance_level = max(target_values)
    upside_target_level = max(
        resistance_level,
        avg_cost + max(avg_cost - support_level, 0.0) * config.strategy.risk_reward_ratio,
    )
    trend_values = closes[-config.strategy.trend_window - 1 :]
    trend_slope = trend_values[-1] / trend_values[0] - 1.0 if trend_values[0] > 0 else 0.0
    return support_level, upside_target_level, trend_slope


def _position_action(
    position: Position,
    support_level: float,
    upside_target_level: float,
    trend_slope: float,
    health_row: dict,
    today_fill: Fill | None,
) -> tuple[str, str]:
    if today_fill and today_fill.side == Side.BUY:
        return "BOUGHT", "new_position_follow_support_and_target"
    if today_fill and today_fill.side == Side.SELL:
        return "SOLD", f"exit_executed_by_{today_fill.reason}"
    if support_level and position.last_price < support_level and trend_slope < 0:
        return "SELL_WATCH", "price_below_support_and_trend_down_prepare_exit"
    if upside_target_level and position.last_price >= upside_target_level and trend_slope <= 0:
        return "TAKE_PROFIT_WATCH", "target_reached_and_trend_fading_prepare_profit_lock"
    if health_row.get("tradable") is False or str(health_row.get("tradable", "")).lower() == "false":
        return "HEALTH_WATCH", "do_not_add_position_monitor_exit_only"
    if trend_slope > 0:
        return "HOLD", "trend_intact_hold_and_raise_attention_near_target"
    return "HOLD_WATCH", "trend_flat_or_weak_watch_support_level"


def _write_position_advice_markdown(path: Path, trading_day: date, rows: list[dict]) -> None:
    lines = [f"# Position Advice {trading_day.isoformat()}", ""]
    if not rows:
        lines.extend(["No open positions.", ""])
        path.write_text("\n".join(lines), encoding="utf-8")
        return
    lines.append("| Symbol | Qty | Last | PnL | Support | Target | Trend | Action | Advice |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |")
    for row in rows:
        lines.append(
            "| {symbol} | {quantity} | {last_price} | {unrealized_pnl} | {support_level} | {upside_target_level} | {trend_slope} | {today_action} | {next_advice} |".format(
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
