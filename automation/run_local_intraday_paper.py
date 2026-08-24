from __future__ import annotations

import argparse
import csv
import json
import os
import time
from dataclasses import asdict, replace
from datetime import date, datetime, time as day_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from aqt.broker import PaperBroker
from aqt.alerts import dispatch_status_alerts
from aqt.calendar import is_a_share_trading_day, parse_calendar_date
from aqt.config import AppConfig, load_config
from aqt.data import fetch_akshare_daily, load_market_data
from aqt.factors import load_external_factors
from aqt.flow import fetch_money_flow, load_money_flow
from aqt.health import fetch_stock_health, healthy_symbols, load_stock_health
from aqt.live_rules import screen_buy_orders, sell_point_orders
from aqt.live_dashboard import generate_live_dashboard
from aqt.models import Account, Bar, Fill, Order, Position, Side
from aqt.quotes import Quote, fetch_realtime_quotes
from aqt.run_status import write_run_status
from aqt.selection import build_selection_candidates


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch realtime quotes and execute local virtual paper trades")
    parser.add_argument("--config", default="configs/smallcap_live.yaml")
    parser.add_argument("--state-dir", default="local_runs/paper_live")
    parser.add_argument("--date", default=None)
    parser.add_argument("--market-end", default=None)
    parser.add_argument("--adjust", default="qfq")
    parser.add_argument("--interval-sec", type=int, default=300)
    parser.add_argument("--loop", action="store_true", help="keep running during A-share trading hours")
    parser.add_argument("--no-fetch", action="store_true", help="skip daily-bar refresh before quote watch")
    parser.add_argument("--no-refresh-factors", action="store_true", help="use cached health and money-flow factors")
    parser.add_argument("--force", action="store_true", help="run outside trading day/hour checks")
    parser.add_argument("--dashboard-output", default="reports/live_dashboard.html")
    parser.add_argument("--no-dashboard", action="store_true", help="skip refreshing the local live dashboard")
    args = parser.parse_args()

    while True:
        should_continue = run_once(args)
        if not args.loop:
            break
        if not should_continue:
            break
        time.sleep(max(args.interval_sec, 30))


def run_once(args: argparse.Namespace) -> bool:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    target_date = parse_calendar_date(args.date, now.date())
    if not args.force and not is_a_share_trading_day(target_date):
        print(f"skip: {target_date.isoformat()} is not an A-share trading day")
        write_run_status(
            Path(args.state_dir),
            "intraday_paper",
            "skipped_non_trading_day",
            target_date,
            "info",
            "target date is not an A-share trading day",
        )
        _refresh_dashboard(Path(args.state_dir), Path(args.dashboard_output), args.no_dashboard, Path(args.config))
        _dispatch_alerts(Path(args.state_dir))
        return False
    if not args.force and not _is_trading_time(now.time()):
        print(f"skip: {now.strftime('%H:%M:%S')} is outside A-share trading hours")
        write_run_status(
            Path(args.state_dir),
            "intraday_paper",
            "skipped_outside_trading_hours",
            target_date,
            "info",
            "current time is outside A-share trading hours",
            {"time": now.strftime("%H:%M:%S")},
        )
        _refresh_dashboard(Path(args.state_dir), Path(args.dashboard_output), args.no_dashboard, Path(args.config))
        _dispatch_alerts(Path(args.state_dir))
        return False

    config = _with_market_end(load_config(args.config), args.market_end or target_date.isoformat())
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    account = _load_account(state_dir / "account.json", config)
    config = _with_symbols(config, _effective_symbols(config, account))

    if not args.no_fetch:
        fetch_akshare_daily(config.data.path, config.data.symbols, config.data.start, config.data.end, args.adjust)

    health = load_stock_health(config.health.path) if args.no_refresh_factors else _refresh_health(config)
    flow = load_money_flow(config.flow.path) if args.no_refresh_factors else _refresh_money_flow(config)
    market_data = load_market_data(config.data.path, config.data.symbols, config.data.start, config.data.end, strict=False)
    factors = load_external_factors(config.factors, config.data.symbols, target_date)
    selection_candidates = build_selection_candidates(config, market_data, target_date, health, flow, factors)
    day_dir = state_dir / "decisions" / target_date.strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    if not selection_candidates.empty:
        selection_candidates.to_csv(day_dir / "intraday_selection_candidates.csv", index=False)
    if not factors.empty:
        factors.to_csv(day_dir / "intraday_external_factors.csv", index=False)
    target_symbols = _watch_symbols(config, market_data, target_date, account, health, selection_candidates)
    quotes = fetch_realtime_quotes(target_symbols, state_dir / "quotes.csv")
    if not quotes:
        print("skip: no realtime quotes loaded")
        write_run_status(
            state_dir,
            "intraday_paper",
            "quote_fetch_failed",
            target_date,
            "error",
            "no realtime quotes loaded for watch symbols",
            {"watch_symbols": target_symbols, "selection_rows": len(selection_candidates)},
        )
        _refresh_dashboard(state_dir, Path(args.dashboard_output), args.no_dashboard, Path(args.config))
        _dispatch_alerts(state_dir)
        return True

    broker = PaperBroker(config.account, config.risk)
    broker.account = account
    _mark_quotes(broker.account, quotes)
    allow_initial_entry = not _has_sell_fill_today(state_dir / "fills.csv", target_date)
    quote_bars = _quote_bars(quotes, target_date)
    orders = _intraday_orders(
        config,
        market_data,
        target_date,
        broker.account,
        quotes,
        health,
        flow,
        factors,
        selection_candidates,
        allow_initial_entry,
    )
    fills = broker.execute_orders(orders, quote_bars)
    _mark_quotes(broker.account, quotes)
    _save_account(state_dir / "account.json", broker.account)
    _append_rows(state_dir / "fills.csv", [_fill_row(fill) for fill in fills])
    _append_rows(state_dir / "rejections.csv", broker.rejected_orders)
    event = _event_row(now, broker.account, fills, broker.rejected_orders, quotes)
    changed = _append_event_if_material(state_dir / "intraday_events.csv", state_dir / "intraday_state.json", event)
    if changed:
        print(state_dir / "intraday_events.csv")
    else:
        print("unchanged")
    write_run_status(
        state_dir,
        "intraday_paper",
        "ok",
        target_date,
        "info",
        "intraday paper loop completed",
        {
            "quotes": len(quotes),
            "fills": len(fills),
            "rejections": len(broker.rejected_orders),
            "event_changed": changed,
            "events_path": state_dir / "intraday_events.csv",
        },
    )
    _refresh_dashboard(state_dir, Path(args.dashboard_output), args.no_dashboard, Path(args.config))
    _dispatch_alerts(state_dir)
    return True


def _is_trading_time(value: day_time) -> bool:
    return day_time(9, 30) <= value <= day_time(11, 30) or day_time(13, 0) <= value <= day_time(15, 0)


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


def _refresh_dashboard(state_dir: Path, output_path: Path, disabled: bool, config_path: Path | None = None) -> None:
    if disabled:
        return
    try:
        generate_live_dashboard(state_dir, output_path, config_path)
    except Exception as exc:
        write_run_status(
            state_dir,
            "dashboard",
            "refresh_failed",
            severity="warning",
            message="live dashboard refresh failed",
            details={"output_path": output_path, "error": str(exc)},
        )


def _dispatch_alerts(state_dir: Path) -> None:
    try:
        dispatch_status_alerts(state_dir, webhook_url=os.environ.get("AQT_ALERT_WEBHOOK_URL"))
    except Exception:
        return


def _watch_symbols(
    config: AppConfig,
    market_data: dict[str, pd.DataFrame],
    target_date: date,
    account: Account,
    health: pd.DataFrame,
    selection_candidates: pd.DataFrame,
) -> list[str]:
    position_symbols = [symbol for symbol, position in account.positions.items() if position.quantity > 0]
    if not selection_candidates.empty:
        ranked = selection_candidates.head(config.strategy.top_n * max(config.strategy.entry_candidate_multiplier, 1))[
            "symbol"
        ].astype(str).tolist()
    else:
        allowed = set(healthy_symbols(config.data.symbols, health, config.health))
        ranked = [symbol for symbol in _ranked_symbols(config, market_data, target_date) if symbol in allowed]
    limit = config.strategy.top_n * max(config.strategy.entry_candidate_multiplier, 1)
    return sorted(set(position_symbols + ranked[:limit]))


def _ranked_symbols(config: AppConfig, market_data: dict[str, pd.DataFrame], target_date: date) -> list[str]:
    rows = []
    for symbol, frame in market_data.items():
        history = frame[frame["date"].dt.date <= target_date]
        if len(history) < config.strategy.momentum_window + 1:
            continue
        current = float(history.iloc[-1]["close"])
        past = float(history.iloc[-config.strategy.momentum_window - 1]["close"])
        if past <= 0:
            continue
        score = current / past - 1.0
        if score >= config.strategy.min_momentum:
            rows.append((symbol, score))
    return [symbol for symbol, _ in sorted(rows, key=lambda item: item[1], reverse=True)]


def _intraday_orders(
    config: AppConfig,
    market_data: dict[str, pd.DataFrame],
    target_date: date,
    account: Account,
    quotes: dict[str, Quote],
    health: pd.DataFrame,
    flow: pd.DataFrame,
    factors: pd.DataFrame,
    selection_candidates: pd.DataFrame,
    allow_initial_entry: bool,
) -> list[Order]:
    quote_bars = _quote_bars(quotes, target_date)
    orders, _ = sell_point_orders(account, quote_bars, market_data, target_date, health, flow, config, factors)

    if any(position.quantity > 0 for position in account.positions.values()) or not allow_initial_entry:
        return orders

    buy_orders = _intraday_selection_orders(config, selection_candidates, quotes, account, target_date)
    buy_orders, _ = screen_buy_orders(buy_orders, health, flow, config, market_data, target_date, quote_bars, factors)
    return orders + buy_orders[: config.strategy.top_n]


def _intraday_selection_orders(
    config: AppConfig,
    selection_candidates: pd.DataFrame,
    quotes: dict[str, Quote],
    account: Account,
    target_date: date,
) -> list[Order]:
    if selection_candidates.empty:
        return []
    selected = selection_candidates[selection_candidates["buy_decision"].astype(str) == "BUY_READY"]
    if selected.empty:
        return []
    target_value_per_name = account.equity() * config.strategy.target_gross_exposure / config.strategy.top_n
    orders: list[Order] = []
    for row in selected.to_dict(orient="records"):
        symbol = str(row.get("symbol", ""))
        if symbol not in quotes:
            continue
        quote = quotes[symbol]
        if target_value_per_name < config.strategy.min_trade_value:
            continue
        orders.append(
            Order(symbol, Side.BUY, int(target_value_per_name / quote.price), target_date, "intraday_full_selection_buy", quote.price)
        )
        if len(orders) >= config.strategy.top_n:
            break
    return orders


def _mark_quotes(account: Account, quotes: dict[str, Quote]) -> None:
    for symbol, quote in quotes.items():
        position = account.positions.get(symbol)
        if position:
            position.last_price = quote.price
    account.updated_at = datetime.now(ZoneInfo("Asia/Shanghai"))


def _technical_levels(
    config: AppConfig,
    market_data: dict[str, pd.DataFrame],
    symbol: str,
    target_date: date,
    current_price: float,
    avg_cost: float,
) -> tuple[float, float, float] | None:
    frame = market_data.get(symbol)
    if frame is None or frame.empty:
        return None
    history = frame[frame["date"].dt.date <= target_date]
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
    trend_values = (closes + [current_price])[-config.strategy.trend_window - 1 :]
    trend_slope = trend_values[-1] / trend_values[0] - 1.0 if trend_values[0] > 0 else 0.0
    return support_level, upside_target_level, trend_slope


def _quote_bars(quotes: dict[str, Quote], target_date: date) -> dict[str, Bar]:
    return {
        symbol: Bar(symbol, target_date, quote.open, quote.high, quote.low, quote.price, quote.volume)
        for symbol, quote in quotes.items()
    }


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


def _fill_row(fill: Fill) -> dict:
    row = asdict(fill)
    row["side"] = fill.side.value
    row["filled_at"] = fill.filled_at.isoformat()
    return row


def _has_sell_fill_today(path: Path, target_date: date) -> bool:
    if not path.exists():
        return False
    try:
        frame = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return False
    if frame.empty or "filled_at" not in frame.columns or "side" not in frame.columns:
        return False
    rows = frame[(frame["filled_at"].astype(str) == target_date.isoformat()) & (frame["side"].astype(str) == "SELL")]
    return not rows.empty


def _event_row(now: datetime, account: Account, fills: list[Fill], rejections: list[dict], quotes: dict[str, Quote]) -> dict:
    positions = [position for position in account.positions.values() if position.quantity > 0]
    return {
        "datetime": now.isoformat(timespec="seconds"),
        "cash": round(account.cash, 2),
        "market_value": round(sum(position.market_value for position in positions), 2),
        "equity": round(account.equity(), 2),
        "positions": len(positions),
        "fills": len(fills),
        "rejections": len(rejections),
        "fill_symbols": ";".join(f"{fill.side.value}:{fill.symbol}:{fill.quantity}:{fill.reason}" for fill in fills),
        "quote_symbols": ";".join(sorted(quotes)),
    }


def _append_event_if_material(events_path: Path, state_path: Path, event: dict) -> bool:
    previous = _read_json(state_path)
    previous_equity = float(previous.get("equity", 0.0) or 0.0)
    equity = float(event["equity"])
    equity_changed = abs(equity - previous_equity) >= 50.0
    activity = event["fills"] > 0 or event["rejections"] > 0
    if previous and not equity_changed and not activity:
        return False
    _append_rows(events_path, [event])
    state_path.write_text(json.dumps(event, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _append_rows(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        if handle.tell() == 0:
            writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
