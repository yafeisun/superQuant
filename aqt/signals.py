from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import pandas as pd

from .broker import PaperBroker
from .config import AppConfig
from .data import iter_bars, load_market_data
from .models import Account, Bar, Order, Position, Side
from .strategy import build_strategy


def generate_daily_signal(config: AppConfig, output_dir: Path) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    market_data = load_market_data(config.data.path, config.data.symbols, config.data.start, config.data.end, strict=False)
    all_bars = list(iter_bars(market_data))
    if not all_bars:
        raise RuntimeError("no market data loaded")

    strategy = build_strategy(config.strategy)
    broker = PaperBroker(config.account, config.risk)

    for bars in all_bars[:-1]:
        broker.mark_to_market(bars)
        orders = strategy.on_bars(bars, broker.account)
        broker.execute_orders(orders, {bar.symbol: bar for bar in bars})
        broker.mark_to_market(bars)
        broker.settle_day()

    latest_bars = all_bars[-1]
    latest_date = latest_bars[0].date.isoformat()
    bars_by_symbol = {bar.symbol: bar for bar in latest_bars}
    broker.mark_to_market(latest_bars)
    candidate_orders = strategy.on_bars(latest_bars, broker.account)
    rankings = _momentum_rankings(market_data, latest_date, config.strategy.momentum_window)
    order_rows = _order_plan_rows(candidate_orders, bars_by_symbol, broker.account, config)
    position_rows = _position_rows(latest_date, broker.account.positions)

    day_index = len(all_bars)
    is_rebalance_day = (
        config.strategy.name == "small_cap_momentum"
        and day_index >= config.strategy.momentum_window
        and day_index % config.strategy.rebalance_interval == 0
    )
    next_rebalance_in = 0 if is_rebalance_day else config.strategy.rebalance_interval - (day_index % config.strategy.rebalance_interval)

    paths = {
        "orders": output_dir / "orders.csv",
        "positions": output_dir / "positions.csv",
        "rankings": output_dir / "rankings.csv",
        "summary": output_dir / "summary.txt",
    }
    pd.DataFrame(order_rows).to_csv(paths["orders"], index=False)
    pd.DataFrame(position_rows).to_csv(paths["positions"], index=False)
    pd.DataFrame(rankings).to_csv(paths["rankings"], index=False)
    _write_signal_summary(
        paths["summary"],
        latest_date,
        day_index,
        is_rebalance_day,
        next_rebalance_in,
        broker.account,
        order_rows,
        rankings[: config.strategy.top_n],
    )
    return paths


def _momentum_rankings(market_data: Dict[str, pd.DataFrame], latest_date: str, window: int) -> List[dict]:
    rows: List[dict] = []
    for symbol, frame in market_data.items():
        frame = frame[frame["date"] <= pd.Timestamp(latest_date)]
        if len(frame) < window + 1:
            continue
        current = float(frame.iloc[-1]["close"])
        past = float(frame.iloc[-window - 1]["close"])
        if past <= 0:
            continue
        rows.append(
            {
                "symbol": symbol,
                "date": latest_date,
                "close": current,
                "momentum": round(current / past - 1.0, 6),
                "rank": 0,
            }
        )
    rows.sort(key=lambda row: row["momentum"], reverse=True)
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows


def _order_plan_rows(
    orders: List[Order],
    bars_by_symbol: Dict[str, Bar],
    account: Account,
    config: AppConfig,
) -> List[dict]:
    rows: List[dict] = []
    cash_left = account.cash
    for order in orders:
        bar = bars_by_symbol[order.symbol]
        lot = config.risk.lot_size
        quantity = (order.quantity // lot) * lot
        side = order.side
        slip = config.account.slippage_bps / 10000.0
        estimate_price = bar.close * (1.0 + slip if side == Side.BUY else 1.0 - slip)
        gross_value = estimate_price * quantity
        commission = max(gross_value * config.account.commission_rate, config.account.min_commission) if quantity else 0.0
        tax = gross_value * config.account.stamp_tax_rate if side == Side.SELL else 0.0
        ok, reason = _check_order(order, quantity, gross_value, commission, account, cash_left, config)
        if ok and side == Side.BUY:
            cash_left -= gross_value + commission
        rows.append(
            {
                "symbol": order.symbol,
                "side": side.value,
                "quantity": quantity,
                "reference_close": round(bar.close, 4),
                "estimated_price": round(estimate_price, 4),
                "estimated_value": round(gross_value, 2),
                "estimated_cost": round(commission + tax, 2),
                "decision": "ALLOW" if ok else "BLOCK",
                "reason": reason,
                "strategy_reason": order.reason,
            }
        )
    return rows


def _check_order(
    order: Order,
    quantity: int,
    gross_value: float,
    commission: float,
    account: Account,
    cash_left: float,
    config: AppConfig,
) -> tuple[bool, str]:
    if quantity <= 0:
        return False, "below_lot_size"
    position = account.positions.get(order.symbol, Position(symbol=order.symbol))
    if order.side == Side.SELL:
        available = position.available if config.risk.enforce_t1 else position.quantity
        if quantity > available:
            return False, "shares_not_available_t1"
        return True, "sell_allowed"

    equity = account.equity()
    if gross_value > equity * config.risk.max_order_value_pct:
        return False, "order_value_limit"
    if position.market_value + gross_value > equity * config.risk.max_position_pct:
        return False, "position_limit"
    if cash_left < gross_value + commission:
        return False, "cash_not_enough"
    return True, "buy_allowed"


def _position_rows(latest_date: str, positions: Dict[str, Position]) -> List[dict]:
    rows: List[dict] = []
    for symbol, position in positions.items():
        if position.quantity <= 0:
            continue
        rows.append(
            {
                "date": latest_date,
                "symbol": symbol,
                "quantity": position.quantity,
                "available_to_sell": position.available,
                "avg_cost": round(position.avg_cost, 4),
                "last_price": round(position.last_price, 4),
                "market_value": round(position.market_value, 2),
                "unrealized_pnl": round((position.last_price - position.avg_cost) * position.quantity, 2),
            }
        )
    return rows


def _write_signal_summary(
    path: Path,
    latest_date: str,
    day_index: int,
    is_rebalance_day: bool,
    next_rebalance_in: int,
    account: Account,
    order_rows: List[dict],
    selected_rows: List[dict],
) -> None:
    lines = [
        f"signal_date: {latest_date}",
        f"trading_day_index: {day_index}",
        f"equity: {account.equity():.2f}",
        f"cash: {account.cash:.2f}",
        f"rebalance_day: {is_rebalance_day}",
        f"next_rebalance_in_trading_days: {next_rebalance_in}",
        f"planned_orders: {len(order_rows)}",
        "",
        "selected_by_momentum:",
    ]
    for row in selected_rows:
        lines.append(f"  {row['rank']}. {row['symbol']} momentum={row['momentum']:.2%} close={row['close']}")
    lines.append("")
    lines.append("order_plan:")
    if order_rows:
        for row in order_rows:
            lines.append(
                "  {side} {symbol} qty={quantity} value={value:.2f} decision={decision} reason={reason}".format(
                    side=row["side"],
                    symbol=row["symbol"],
                    quantity=row["quantity"],
                    value=row["estimated_value"],
                    decision=row["decision"],
                    reason=row["reason"],
                )
            )
    else:
        lines.append("  HOLD")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
