from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd

from .broker import PaperBroker
from .config import AppConfig
from .data import iter_bars, load_market_data
from .models import Fill, Position
from .strategy import build_strategy


def run_backtest(config: AppConfig) -> Dict[str, Path]:
    return _run_event_loop(config, config.output.backtest_dir, max_cycles=None)


def run_paper(config: AppConfig, cycles: int | None) -> Dict[str, Path]:
    return _run_event_loop(config, config.output.paper_dir, max_cycles=cycles)


def _run_event_loop(config: AppConfig, output_dir: Path, max_cycles: int | None) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    market_data = load_market_data(config.data.path, config.data.symbols, config.data.start, config.data.end)
    strategy = build_strategy(config.strategy)
    broker = PaperBroker(config.account, config.risk)

    equity_rows: List[dict] = []
    fill_rows: List[dict] = []
    position_rows: List[dict] = []

    for index, bars in enumerate(iter_bars(market_data), start=1):
        if max_cycles is not None and index > max_cycles:
            break
        broker.mark_to_market(bars)
        orders = strategy.on_bars(bars, broker.account)
        fills = broker.execute_orders(orders, {bar.symbol: bar for bar in bars})
        broker.mark_to_market(bars)

        trading_day = bars[0].date
        equity_rows.append(
            {
                "date": trading_day.isoformat(),
                "cash": round(broker.account.cash, 4),
                "market_value": round(sum(p.market_value for p in broker.account.positions.values()), 4),
                "equity": round(broker.account.equity(), 4),
                "realized_pnl": round(broker.account.realized_pnl, 4),
                "fills": len(fills),
            }
        )
        fill_rows.extend(_fill_to_row(fill) for fill in fills)
        position_rows.extend(_positions_to_rows(trading_day.isoformat(), broker.account.positions))
        broker.settle_day()

    paths = {
        "equity": output_dir / "equity.csv",
        "fills": output_dir / "fills.csv",
        "trades": output_dir / "trades.csv",
        "positions": output_dir / "positions.csv",
        "rejections": output_dir / "rejections.csv",
        "metrics": output_dir / "metrics.csv",
        "summary": output_dir / "summary.txt",
    }
    trade_rows = _closed_trades(fill_rows)
    metrics = _metrics(equity_rows, fill_rows, trade_rows, broker.rejected_orders)
    pd.DataFrame(equity_rows).to_csv(paths["equity"], index=False)
    pd.DataFrame(fill_rows).to_csv(paths["fills"], index=False)
    pd.DataFrame(trade_rows).to_csv(paths["trades"], index=False)
    pd.DataFrame(position_rows).to_csv(paths["positions"], index=False)
    pd.DataFrame(broker.rejected_orders).to_csv(paths["rejections"], index=False)
    pd.DataFrame([metrics]).to_csv(paths["metrics"], index=False)
    _write_summary(paths["summary"], metrics)
    return paths


def _fill_to_row(fill: Fill) -> dict:
    row = asdict(fill)
    row["side"] = fill.side.value
    row["filled_at"] = fill.filled_at.isoformat()
    return row


def _positions_to_rows(trading_day: str, positions: Dict[str, Position]) -> Iterable[dict]:
    for symbol, position in positions.items():
        yield {
            "date": trading_day,
            "symbol": symbol,
            "quantity": position.quantity,
            "available": position.available,
            "avg_cost": round(position.avg_cost, 4),
            "last_price": round(position.last_price, 4),
            "market_value": round(position.market_value, 4),
        }


def _closed_trades(fill_rows: List[dict]) -> List[dict]:
    lots: Dict[str, List[dict]] = {}
    trades: List[dict] = []
    for fill in fill_rows:
        symbol = fill["symbol"]
        lots.setdefault(symbol, [])
        if fill["side"] == "BUY":
            lots[symbol].append(
                {
                    "date": fill["filled_at"],
                    "quantity": int(fill["quantity"]),
                    "price": float(fill["price"]),
                    "cost": float(fill["commission"]) + float(fill["tax"]),
                }
            )
            continue

        remaining = int(fill["quantity"])
        sell_value = float(fill["price"]) * int(fill["quantity"])
        sell_cost = float(fill["commission"]) + float(fill["tax"])
        while remaining > 0 and lots[symbol]:
            lot = lots[symbol][0]
            matched = min(remaining, lot["quantity"])
            buy_cost = lot["cost"] * matched / lot["quantity"] if lot["quantity"] else 0.0
            allocated_sell_cost = sell_cost * matched / int(fill["quantity"])
            pnl = (float(fill["price"]) - lot["price"]) * matched - buy_cost - allocated_sell_cost
            trades.append(
                {
                    "symbol": symbol,
                    "entry_date": lot["date"],
                    "exit_date": fill["filled_at"],
                    "quantity": matched,
                    "entry_price": round(lot["price"], 4),
                    "exit_price": round(float(fill["price"]), 4),
                    "pnl": round(pnl, 4),
                    "return_pct": round(pnl / (lot["price"] * matched), 6),
                    "cost": round(buy_cost + allocated_sell_cost, 4),
                    "win": pnl > 0,
                }
            )
            lot["quantity"] -= matched
            lot["cost"] -= buy_cost
            remaining -= matched
            if lot["quantity"] <= 0:
                lots[symbol].pop(0)
        if remaining > 0:
            trades.append(
                {
                    "symbol": symbol,
                    "entry_date": "unknown",
                    "exit_date": fill["filled_at"],
                    "quantity": remaining,
                    "entry_price": 0.0,
                    "exit_price": round(float(fill["price"]), 4),
                    "pnl": round(sell_value - sell_cost, 4),
                    "return_pct": 0.0,
                    "cost": round(sell_cost, 4),
                    "win": False,
                }
            )
    return trades


def _metrics(
    equity_rows: List[dict],
    fill_rows: List[dict],
    trade_rows: List[dict],
    rejections: List[dict],
) -> dict:
    if not equity_rows:
        return {"status": "no_data"}

    first = equity_rows[0]
    last = equity_rows[-1]
    total_return = last["equity"] / first["equity"] - 1.0 if first["equity"] else 0.0
    equity = pd.Series([row["equity"] for row in equity_rows], dtype="float64")
    drawdown = equity / equity.cummax() - 1.0
    total_cost = sum(float(row["commission"]) + float(row["tax"]) for row in fill_rows)
    closed_trades = len(trade_rows)
    winning_trades = sum(1 for row in trade_rows if row.get("win"))
    win_rate = winning_trades / closed_trades if closed_trades else 0.0
    return {
        "status": "ok",
        "start_date": first["date"],
        "end_date": last["date"],
        "start_equity": round(first["equity"], 2),
        "end_equity": round(last["equity"], 2),
        "total_return": round(total_return, 6),
        "max_drawdown": round(float(drawdown.min()), 6),
        "fills": len(fill_rows),
        "closed_trades": closed_trades,
        "winning_trades": winning_trades,
        "win_rate": round(win_rate, 6),
        "total_cost": round(total_cost, 4),
        "rejections": len(rejections),
    }


def _write_summary(path: Path, metrics: dict) -> None:
    if metrics.get("status") != "ok":
        path.write_text("no data\n", encoding="utf-8")
        return
    lines = [f"{key}: {value}" for key, value in metrics.items() if key != "status"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
