from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Callable

import pandas as pd


@dataclass
class RunFrames:
    run_dir: Path
    positions: pd.DataFrame
    equity: pd.DataFrame
    fills: pd.DataFrame
    trades: pd.DataFrame
    rejections: pd.DataFrame
    metrics: pd.DataFrame
    summary_text: str
    manifest_text: str


def generate_history_report(run_dir: Path, output_path: Path) -> Path:
    frames = _load_run_frames(run_dir)
    position_history = _build_position_history(frames.positions, frames.equity)
    fill_events = _normalize_fills(frames.fills)
    trade_events = _normalize_trades(frames.trades)
    rejection_events = _normalize_rejections(frames.rejections)
    summary = _build_summary(frames, position_history, fill_events, trade_events, rejection_events)
    html = _render_page(frames, summary, position_history, fill_events, trade_events, rejection_events)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _load_run_frames(run_dir: Path) -> RunFrames:
    return RunFrames(
        run_dir=run_dir,
        positions=_read_csv(run_dir / "positions.csv"),
        equity=_read_csv(run_dir / "equity.csv"),
        fills=_read_csv(run_dir / "fills.csv"),
        trades=_read_csv(run_dir / "trades.csv"),
        rejections=_read_csv(run_dir / "rejections.csv"),
        metrics=_read_csv(run_dir / "metrics.csv"),
        summary_text=_read_text(run_dir / "summary.txt"),
        manifest_text=_read_text(run_dir / "manifest.txt"),
    )


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _build_position_history(positions: pd.DataFrame, equity: pd.DataFrame) -> pd.DataFrame:
    if positions.empty:
        return pd.DataFrame()
    if "date" not in positions.columns or "symbol" not in positions.columns or "quantity" not in positions.columns:
        return pd.DataFrame()

    frame = positions.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["symbol"] = frame["symbol"].astype(str)
    has_history = frame["date"].nunique() > 1
    if has_history and not equity.empty and "date" in equity.columns:
        date_index = sorted(pd.to_datetime(equity["date"]).dt.normalize().unique())
    else:
        date_index = sorted(frame["date"].unique())

    pivot_fields = [
        field
        for field in [
            "quantity",
            "available_to_sell",
            "avg_cost",
            "last_price",
            "market_value",
            "unrealized_pnl",
            "return_pct",
        ]
        if field in frame.columns
    ]
    quantity_pivot = (
        frame.pivot_table(index="date", columns="symbol", values="quantity", aggfunc="last")
        .reindex(date_index)
        .fillna(0.0)
    )
    extra_pivots = {
        field: frame.pivot_table(index="date", columns="symbol", values=field, aggfunc="last").reindex(date_index)
        for field in pivot_fields
        if field != "quantity"
    }

    rows: list[dict] = []
    for symbol in quantity_pivot.columns:
        qty_series = quantity_pivot[symbol].fillna(0.0)
        prev_qty = 0.0
        for date_value, qty_value in qty_series.items():
            qty = float(qty_value or 0.0)
            delta = qty - prev_qty
            if qty <= 0 and prev_qty <= 0:
                prev_qty = qty
                continue

            row = {
                "date": date_value,
                "symbol": symbol,
                "action": _position_action(prev_qty, qty),
                "delta_qty": delta,
                "position_qty": qty,
            }
            for field, pivot in extra_pivots.items():
                if symbol in pivot.columns:
                    value = pivot.at[date_value, symbol]
                    row[field] = value if pd.notna(value) else ""
                else:
                    row[field] = ""
            rows.append(row)
            prev_qty = qty

    history = pd.DataFrame(rows)
    if history.empty:
        return history
    history = history.sort_values(["symbol", "date"]).reset_index(drop=True)
    return history


def _position_action(prev_qty: float, qty: float) -> str:
    if prev_qty <= 0 and qty > 0:
        return "OPEN"
    if prev_qty > 0 and qty > prev_qty:
        return "ADD"
    if prev_qty > 0 and qty < prev_qty and qty > 0:
        return "REDUCE"
    if prev_qty > 0 and qty <= 0:
        return "CLOSE"
    if qty > 0:
        return "HOLD"
    return "IDLE"


def _normalize_fills(fills: pd.DataFrame) -> pd.DataFrame:
    if fills.empty:
        return pd.DataFrame()
    frame = fills.copy()
    if "filled_at" in frame.columns:
        frame["date"] = pd.to_datetime(frame["filled_at"]).dt.normalize()
    elif "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    else:
        return pd.DataFrame()
    frame["symbol"] = frame["symbol"].astype(str)
    if "side" in frame.columns:
        frame["side"] = frame["side"].astype(str).str.upper()
    if "quantity" in frame.columns:
        frame["quantity"] = pd.to_numeric(frame["quantity"], errors="coerce").fillna(0.0)
    if "price" in frame.columns:
        frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    if "commission" in frame.columns:
        frame["commission"] = pd.to_numeric(frame["commission"], errors="coerce").fillna(0.0)
    if "tax" in frame.columns:
        frame["tax"] = pd.to_numeric(frame["tax"], errors="coerce").fillna(0.0)
    if "price" in frame.columns and "quantity" in frame.columns and "side" in frame.columns:
        signed = frame["quantity"].where(frame["side"] == "BUY", -frame["quantity"])
        commission = frame["commission"] if "commission" in frame.columns else 0.0
        tax = frame["tax"] if "tax" in frame.columns else 0.0
        frame["cash_impact"] = -frame["price"] * signed - commission - tax
        frame["delta_qty"] = signed
    frame = frame.sort_values(["date", "symbol", "side"]).reset_index(drop=True)
    return frame


def _normalize_trades(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    frame = trades.copy()
    if "exit_date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["exit_date"]).dt.normalize()
    elif "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    else:
        return pd.DataFrame()
    if "symbol" in frame.columns:
        frame["symbol"] = frame["symbol"].astype(str)
    frame = frame.sort_values(["date", "symbol"] if "symbol" in frame.columns else ["date"]).reset_index(drop=True)
    return frame


def _normalize_rejections(rejections: pd.DataFrame) -> pd.DataFrame:
    if rejections.empty:
        return pd.DataFrame()
    frame = rejections.copy()
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    elif "filled_at" in frame.columns:
        frame["date"] = pd.to_datetime(frame["filled_at"]).dt.normalize()
    if "symbol" in frame.columns:
        frame["symbol"] = frame["symbol"].astype(str)
    return frame


def _build_summary(
    frames: RunFrames,
    position_history: pd.DataFrame,
    fill_events: pd.DataFrame,
    trade_events: pd.DataFrame,
    rejection_events: pd.DataFrame,
) -> dict:
    summary = _parse_key_value_text(frames.manifest_text)
    summary.update(_parse_key_value_text(frames.summary_text))

    if not frames.equity.empty and "date" in frames.equity.columns:
        equity = frames.equity.copy()
        equity["date"] = pd.to_datetime(equity["date"]).dt.normalize()
        equity = equity.sort_values("date")
        latest_equity_row = equity.iloc[-1].to_dict()
        first_equity_row = equity.iloc[0].to_dict()
        start_equity = float(first_equity_row.get("equity", first_equity_row.get("market_value", 0.0)))
        end_equity = float(latest_equity_row.get("equity", latest_equity_row.get("market_value", 0.0)))
        peak = equity["equity"].astype(float).cummax() if "equity" in equity.columns else None
        max_drawdown = 0.0
        if peak is not None and not peak.empty:
            drawdown = equity["equity"].astype(float) / peak - 1.0
            max_drawdown = float(drawdown.min())
        summary["start_date"] = str(first_equity_row.get("date", ""))
        summary["end_date"] = str(latest_equity_row.get("date", ""))
        summary["start_equity"] = start_equity
        summary["end_equity"] = end_equity
        summary["total_return"] = end_equity / start_equity - 1.0 if start_equity else 0.0
        summary["max_drawdown"] = max_drawdown
        if "cash" in latest_equity_row:
            summary["latest_cash"] = float(latest_equity_row["cash"])
        if "market_value" in latest_equity_row:
            summary["latest_market_value"] = float(latest_equity_row["market_value"])
        if "equity" in latest_equity_row:
            summary["latest_equity"] = float(latest_equity_row["equity"])
        summary["equity_points"] = len(equity)
    else:
        summary.setdefault("start_equity", 0.0)
        summary.setdefault("end_equity", 0.0)
        summary.setdefault("total_return", 0.0)
        summary.setdefault("max_drawdown", 0.0)
        summary["equity_points"] = 0

    summary["position_points"] = len(position_history)
    summary["symbols"] = int(position_history["symbol"].nunique()) if not position_history.empty else 0
    if not position_history.empty:
        latest_date = pd.to_datetime(position_history["date"]).dt.normalize().max()
        active = position_history[
            (pd.to_datetime(position_history["date"]).dt.normalize() == latest_date)
            & (position_history["position_qty"].astype(float) > 0)
        ]
        summary["active_symbols"] = int(active["symbol"].nunique())
    else:
        summary["active_symbols"] = 0
    summary["fills_count"] = len(fill_events)
    summary["trades_count"] = len(trade_events)
    summary["rejections_count"] = len(rejection_events)
    if not trade_events.empty and "win" in trade_events.columns:
        summary["winning_trades"] = int(trade_events["win"].astype(str).str.lower().isin(["true", "1"]).sum())
    elif "winning_trades" not in summary:
        summary["winning_trades"] = 0
    if not trade_events.empty and "win" in trade_events.columns:
        summary["win_rate"] = (
            summary["winning_trades"] / len(trade_events) if len(trade_events) else 0.0
        )
    elif "win_rate" not in summary:
        summary["win_rate"] = 0.0
    if not fill_events.empty:
        if "commission" in fill_events.columns or "tax" in fill_events.columns:
            commission_source = fill_events["commission"] if "commission" in fill_events.columns else pd.Series(0.0, index=fill_events.index)
            tax_source = fill_events["tax"] if "tax" in fill_events.columns else pd.Series(0.0, index=fill_events.index)
            commission = pd.to_numeric(commission_source, errors="coerce").fillna(0.0)
            tax = pd.to_numeric(tax_source, errors="coerce").fillna(0.0)
            summary["total_cost"] = float((commission + tax).sum())
        elif "cash_impact" in fill_events.columns:
            summary["total_cost"] = float(fill_events["cash_impact"].abs().sum())
    elif "total_cost" not in summary:
        summary["total_cost"] = 0.0
    return summary


def _parse_key_value_text(text: str) -> dict:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def _render_page(
    frames: RunFrames,
    summary: dict,
    position_history: pd.DataFrame,
    fill_events: pd.DataFrame,
    trade_events: pd.DataFrame,
    rejection_events: pd.DataFrame,
) -> str:
    title = f"History Report - {frames.run_dir.name}"
    equities = frames.equity.copy()
    if not equities.empty and "date" in equities.columns and "equity" in equities.columns:
        equities["date"] = pd.to_datetime(equities["date"]).dt.normalize()
        equities = equities.sort_values("date")
    latest_positions = _latest_positions(position_history)
    symbol_stats = _symbol_stats(position_history)

    summary_cards = _render_summary_cards(frames, summary)
    equity_chart = _render_sparkline_block(
        equities["date"].tolist() if not equities.empty else [],
        equities["equity"].astype(float).tolist() if not equities.empty and "equity" in equities.columns else [],
        label="Equity",
        color="#0f766e",
        uid="equity",
    )
    active_chart = _render_sparkline_block(
        latest_positions["date"].tolist() if not latest_positions.empty else [],
        latest_positions["position_qty"].astype(float).tolist() if not latest_positions.empty else [],
        label="Latest Holdings",
        color="#2563eb",
        uid="latest-holdings",
    )
    position_table = _render_latest_holdings_table(latest_positions)
    position_sections = _render_symbol_sections(symbol_stats, position_history)
    fill_table = _render_fill_table(fill_events)
    trade_table = _render_trade_table(trade_events)
    rejection_table = _render_rejection_table(rejection_events)

    raw_summary = escape(frames.summary_text.strip()) if frames.summary_text.strip() else ""
    raw_manifest = escape(frames.manifest_text.strip()) if frames.manifest_text.strip() else ""
    note = ""
    if position_history.empty:
        note = "No position history found in this run directory."
    elif position_history["date"].nunique() <= 1:
        note = "This run only has a single position snapshot. The table below is a snapshot plus any fills."

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #f6f7fb;
      --panel: #ffffff;
      --line: #e5e7eb;
      --text: #111827;
      --muted: #6b7280;
      --buy: #e8f5e9;
      --buy-text: #1b5e20;
      --sell: #fde8e8;
      --sell-text: #991b1b;
      --hold: #e0f2fe;
      --hold-text: #075985;
      --open: #dcfce7;
      --open-text: #166534;
      --close: #fee2e2;
      --close-text: #991b1b;
      --reduce: #fef3c7;
      --reduce-text: #92400e;
      --add: #dbeafe;
      --add-text: #1d4ed8;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }}
    main {{
      max-width: 1500px;
      margin: 0 auto;
      padding: 20px;
    }}
    h1, h2, h3 {{
      margin: 0 0 12px 0;
      line-height: 1.2;
    }}
    h1 {{ font-size: 24px; }}
    h2 {{ font-size: 18px; }}
    h3 {{ font-size: 15px; }}
    .muted {{ color: var(--muted); }}
    .section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 16px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
    }}
    .card {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 12px;
      background: #fff;
    }}
    .card .label {{
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    .card .value {{
      font-size: 20px;
      font-weight: 700;
    }}
    .toolbar {{
      display: flex;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }}
    .toolbar input {{
      min-width: 260px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      background: #fff;
    }}
    .sparkline {{
      display: inline-block;
      vertical-align: middle;
    }}
    .sparkline svg {{
      display: block;
    }}
    .summary-row {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      align-items: flex-start;
    }}
    .summary-block {{
      flex: 1 1 320px;
      min-width: 320px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      background: #fff;
    }}
    thead th {{
      position: sticky;
      top: 0;
      background: #f9fafb;
      z-index: 1;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      text-align: left;
      padding: 6px 8px;
      vertical-align: top;
    }}
    tr:hover td {{
      background: #f9fafb;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0;
      white-space: nowrap;
    }}
    .action-open {{ background: var(--open); color: var(--open-text); }}
    .action-add {{ background: var(--add); color: var(--add-text); }}
    .action-reduce {{ background: var(--reduce); color: var(--reduce-text); }}
    .action-close {{ background: var(--close); color: var(--close-text); }}
    .action-hold {{ background: var(--hold); color: var(--hold-text); }}
    .action-buy {{ background: var(--buy); color: var(--buy-text); }}
    .action-sell {{ background: var(--sell); color: var(--sell-text); }}
    details.symbol {{
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      margin-bottom: 10px;
      overflow: clip;
    }}
    details.symbol summary {{
      cursor: pointer;
      padding: 10px 12px;
      list-style: none;
      display: flex;
      align-items: center;
      gap: 12px;
      justify-content: space-between;
    }}
    details.symbol summary::-webkit-details-marker {{
      display: none;
    }}
    .symbol-name {{
      font-weight: 700;
      min-width: 110px;
    }}
    .symbol-meta {{
      color: var(--muted);
      font-size: 12px;
      flex: 1 1 auto;
    }}
    .symbol-body {{
      border-top: 1px solid var(--line);
      padding: 12px;
    }}
    .section-note {{
      margin-bottom: 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #0b1220;
      color: #e5e7eb;
      padding: 12px;
      border-radius: 6px;
      overflow-x: auto;
      font-size: 12px;
      line-height: 1.45;
    }}
    .stack {{
      display: grid;
      gap: 12px;
    }}
  </style>
  <script>
    function filterHistory() {{
      const query = document.getElementById('symbolFilter').value.trim().toUpperCase();
      document.querySelectorAll('[data-symbol]').forEach(function(el) {{
        const symbol = (el.getAttribute('data-symbol') || '').toUpperCase();
        el.style.display = !query || symbol.indexOf(query) >= 0 ? '' : 'none';
      }});
    }}
  </script>
</head>
<body>
  <main>
    <div class="section">
      <h1>{escape(title)}</h1>
      <div class="muted">{escape(str(frames.run_dir))}</div>
      {f'<div class="section-note">{escape(note)}</div>' if note else ''}
    </div>

    <div class="section">
      <h2>Summary</h2>
      <div class="grid">
        {summary_cards}
      </div>
    </div>

    <div class="section">
      <h2>Overview</h2>
      <div class="summary-row">
        <div class="summary-block">{equity_chart}</div>
        <div class="summary-block">{active_chart}</div>
      </div>
    </div>

    <div class="section">
      <h2>Current Holdings</h2>
      <div class="toolbar">
        <input id="symbolFilter" type="text" placeholder="Filter by symbol" oninput="filterHistory()">
      </div>
      {position_table}
    </div>

    <div class="section">
      <h2>Per-Symbol Timeline</h2>
      <div class="stack">
        {position_sections}
      </div>
    </div>

    <div class="section">
      <h2>Fills</h2>
      {fill_table}
    </div>

    <div class="section">
      <h2>Closed Trades</h2>
      {trade_table}
    </div>

    <div class="section">
      <h2>Rejections</h2>
      {rejection_table}
    </div>

    {f'<div class="section"><h2>Raw Summary</h2><details><summary>summary.txt</summary><pre>{raw_summary}</pre></details><details><summary>manifest.txt</summary><pre>{raw_manifest}</pre></details></div>' if raw_summary or raw_manifest else ''}
  </main>
</body>
</html>
"""
    return html


def _render_summary_cards(frames: RunFrames, summary: dict) -> str:
    items = [
        ("Period", f"{_fmt_date(summary.get('start_date'))} to {_fmt_date(summary.get('end_date'))}"),
        ("Equity", _fmt_money(summary.get("latest_equity", summary.get("end_equity")))),
        ("Return", _fmt_pct(summary.get("total_return"))),
        ("Max DD", _fmt_pct(summary.get("max_drawdown"))),
        ("Symbols", _fmt_int(summary.get("symbols"))),
        ("Active", _fmt_int(summary.get("active_symbols"))),
        ("Fills", _fmt_int(summary.get("fills_count"))),
        ("Trades", _fmt_int(summary.get("trades_count"))),
        ("Rejections", _fmt_int(summary.get("rejections_count"))),
    ]
    if summary.get("latest_cash") is not None:
        items.append(("Cash", _fmt_money(summary.get("latest_cash"))))
    if summary.get("latest_market_value") is not None:
        items.append(("Market Value", _fmt_money(summary.get("latest_market_value"))))

    cards = []
    for label, value in items:
        cards.append(
            f'<div class="card"><div class="label">{escape(label)}</div><div class="value">{escape(value)}</div></div>'
        )
    return "".join(cards)


def _latest_positions(position_history: pd.DataFrame) -> pd.DataFrame:
    if position_history.empty:
        return pd.DataFrame()
    position_history = position_history.copy()
    position_history["date"] = pd.to_datetime(position_history["date"]).dt.normalize()
    latest_date = position_history["date"].max()
    latest = position_history[position_history["date"] == latest_date].copy()
    if latest.empty:
        return latest
    latest = latest[latest["position_qty"].astype(float) > 0]
    return latest.sort_values(["position_qty", "symbol"], ascending=[False, True]).reset_index(drop=True)


def _symbol_stats(position_history: pd.DataFrame) -> pd.DataFrame:
    if position_history.empty:
        return pd.DataFrame()
    frame = position_history.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["position_qty"] = pd.to_numeric(frame["position_qty"], errors="coerce").fillna(0.0)
    stats = frame.groupby("symbol").agg(
        first_date=("date", "min"),
        last_date=("date", "max"),
        rows=("date", "count"),
        held_days=("position_qty", lambda s: int((s > 0).sum())),
        latest_qty=("position_qty", "last"),
        latest_action=("action", "last"),
    )
    if "market_value" in frame.columns:
        frame["market_value"] = pd.to_numeric(frame["market_value"], errors="coerce")
        stats["latest_market_value"] = frame.groupby("symbol")["market_value"].last()
    else:
        stats["latest_market_value"] = 0.0
    if "unrealized_pnl" in frame.columns:
        frame["unrealized_pnl"] = pd.to_numeric(frame["unrealized_pnl"], errors="coerce")
        stats["latest_unrealized_pnl"] = frame.groupby("symbol")["unrealized_pnl"].last()
    else:
        stats["latest_unrealized_pnl"] = 0.0
    stats["net_delta"] = frame.groupby("symbol")["delta_qty"].sum()
    stats["buy_days"] = frame.groupby("symbol")["action"].apply(lambda s: int(s.isin(["OPEN", "ADD"]).sum()))
    stats["sell_days"] = frame.groupby("symbol")["action"].apply(lambda s: int(s.isin(["REDUCE", "CLOSE"]).sum()))
    stats = stats.reset_index()
    stats = stats.sort_values(["latest_market_value", "latest_qty", "symbol"], ascending=[False, False, True]).reset_index(drop=True)
    return stats


def _render_latest_holdings_table(latest_positions: pd.DataFrame) -> str:
    if latest_positions.empty:
        return '<p class="muted">No active holdings.</p>'
    columns = [
        ("symbol", "Symbol", _fmt_symbol_link),
        ("date", "Date", _fmt_date),
        ("position_qty", "Qty", _fmt_int),
        ("available_to_sell", "Avail", _fmt_int),
        ("avg_cost", "Avg Cost", _fmt_money_4),
        ("last_price", "Last", _fmt_money_4),
        ("market_value", "Market Value", _fmt_money),
        ("unrealized_pnl", "PnL", _fmt_money),
        ("return_pct", "Return", _fmt_pct),
        ("action", "Action", _fmt_action),
    ]
    rows = latest_positions.to_dict(orient="records")
    return _render_table(rows, columns, row_symbol_key="symbol")


def _render_symbol_sections(stats: pd.DataFrame, position_history: pd.DataFrame) -> str:
    if stats.empty:
        return '<p class="muted">No position history available.</p>'
    sections = []
    for row in stats.to_dict(orient="records"):
        symbol = str(row["symbol"])
        symbol_rows = position_history[position_history["symbol"] == symbol].copy()
        symbol_rows = symbol_rows.sort_values("date")
        spark = _render_sparkline_block(
            symbol_rows["date"].tolist(),
            pd.to_numeric(symbol_rows["position_qty"], errors="coerce").fillna(0.0).tolist(),
            label="Quantity",
            color="#2563eb",
            uid=f"{symbol}-quantity",
        )
        mv_values = pd.to_numeric(symbol_rows["market_value"], errors="coerce").fillna(0.0).tolist() if "market_value" in symbol_rows.columns else []
        mv_spark = _render_sparkline_block(
            symbol_rows["date"].tolist() if mv_values else [],
            mv_values,
            label="Market Value",
            color="#0f766e",
            uid=f"{symbol}-market-value",
        ) if mv_values else ""
        columns = [
            ("date", "Date", _fmt_date),
            ("action", "Action", _fmt_action),
            ("delta_qty", "Delta", _fmt_signed_int),
            ("position_qty", "Qty", _fmt_int),
        ]
        if "available_to_sell" in symbol_rows.columns:
            columns.append(("available_to_sell", "Avail", _fmt_int))
        if "avg_cost" in symbol_rows.columns:
            columns.append(("avg_cost", "Avg Cost", _fmt_money_4))
        if "last_price" in symbol_rows.columns:
            columns.append(("last_price", "Last", _fmt_money_4))
        if "market_value" in symbol_rows.columns:
            columns.append(("market_value", "Market Value", _fmt_money))
        if "unrealized_pnl" in symbol_rows.columns:
            columns.append(("unrealized_pnl", "PnL", _fmt_money))
        if "return_pct" in symbol_rows.columns:
            columns.append(("return_pct", "Return", _fmt_pct))
        table = _render_table(symbol_rows.to_dict(orient="records"), columns, row_symbol_key="symbol")
        summary = (
            f"{escape(symbol)}  "
            f"first={_fmt_date(row['first_date'])}  "
            f"last={_fmt_date(row['last_date'])}  "
            f"held_days={_fmt_int(row['held_days'])}  "
            f"latest_qty={_fmt_int(row['latest_qty'])}  "
            f"latest_mv={_fmt_money(row.get('latest_market_value'))}  "
            f"latest_pnl={_fmt_money(row.get('latest_unrealized_pnl'))}"
        )
        sections.append(
            f'<details class="symbol" id="symbol-{_slug(symbol)}" data-symbol="{escape(symbol)}"><summary>'
            f'<span class="symbol-name">{escape(symbol)}</span>'
            f'<span class="symbol-meta">{summary}</span>'
            f'<span>{spark}</span>'
            f'</summary><div class="symbol-body">{mv_spark}{table}</div></details>'
        )
    return "".join(sections)


def _render_fill_table(fills: pd.DataFrame) -> str:
    if fills.empty:
        return '<p class="muted">No fills file found.</p>'
    rows = fills.to_dict(orient="records")
    columns = [
        ("date", "Date", _fmt_date),
        ("symbol", "Symbol", _fmt_symbol_link),
        ("side", "Side", _fmt_side),
        ("quantity", "Qty", _fmt_int),
        ("price", "Price", _fmt_money_4),
    ]
    if "cash_impact" in fills.columns:
        columns.append(("cash_impact", "Cash Impact", _fmt_money))
    if "commission" in fills.columns:
        columns.append(("commission", "Commission", _fmt_money_4))
    if "tax" in fills.columns:
        columns.append(("tax", "Tax", _fmt_money_4))
    if "reason" in fills.columns:
        columns.append(("reason", "Reason", _fmt_text))
    return _render_table(rows, columns, row_symbol_key="symbol")


def _render_trade_table(trades: pd.DataFrame) -> str:
    if trades.empty:
        return '<p class="muted">No trades file found.</p>'
    rows = trades.to_dict(orient="records")
    columns = [
        ("date", "Exit Date", _fmt_date),
        ("symbol", "Symbol", _fmt_symbol_link),
        ("entry_date", "Entry Date", _fmt_date),
        ("quantity", "Qty", _fmt_int),
        ("entry_price", "Entry Price", _fmt_money_4),
        ("exit_price", "Exit Price", _fmt_money_4),
        ("pnl", "PnL", _fmt_money),
        ("return_pct", "Return", _fmt_pct),
        ("win", "Win", _fmt_bool),
    ]
    return _render_table(rows, columns, row_symbol_key="symbol")


def _render_rejection_table(rejections: pd.DataFrame) -> str:
    if rejections.empty:
        return '<p class="muted">No rejections file found.</p>'
    rows = rejections.to_dict(orient="records")
    columns = [
        ("date", "Date", _fmt_date),
        ("symbol", "Symbol", _fmt_symbol_link),
        ("side", "Side", _fmt_side),
        ("reason", "Reason", _fmt_text),
    ]
    if "strategy_reason" in rejections.columns:
        columns.append(("strategy_reason", "Strategy Reason", _fmt_text))
    if "quantity" in rejections.columns:
        columns.append(("quantity", "Qty", _fmt_int))
    return _render_table(rows, columns, row_symbol_key="symbol")


def _render_table(rows: list[dict], columns: list[tuple[str, str, Callable[[object], str]]], row_symbol_key: str | None = None) -> str:
    if not rows:
        return '<p class="muted">No data.</p>'
    parts = ["<table>", "<thead><tr>"]
    for _, label, _ in columns:
        parts.append(f"<th>{escape(label)}</th>")
    parts.append("</tr></thead><tbody>")
    for row in rows:
        symbol = str(row.get(row_symbol_key, "")) if row_symbol_key else ""
        cls = _row_class(row)
        data_attr = f' data-symbol="{escape(symbol)}"' if symbol else ""
        parts.append(f'<tr class="{escape(cls)}"{data_attr}>')
        for key, _, formatter in columns:
            value = row.get(key, "")
            parts.append(f"<td>{formatter(value)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _row_class(row: dict) -> str:
    action = str(row.get("action", row.get("side", ""))).upper()
    if action in {"OPEN", "BUY"}:
        return "action-open"
    if action in {"ADD"}:
        return "action-add"
    if action in {"REDUCE"}:
        return "action-reduce"
    if action in {"CLOSE", "SELL"}:
        return "action-close"
    if action in {"HOLD", "HOLD_WATCH"}:
        return "action-hold"
    return ""


def _render_sparkline_block(dates: list, values: list[float], label: str, color: str, uid: str | None = None) -> str:
    if not values:
        return ""
    points = []
    cleaned = [float(v) for v in values]
    width = 180
    height = 42
    if len(cleaned) == 1:
        cleaned = [cleaned[0], cleaned[0]]
    min_v = min(cleaned)
    max_v = max(cleaned)
    if max_v == min_v:
        max_v = min_v + 1.0
    for index, value in enumerate(cleaned):
        x = 4 + (width - 8) * index / (len(cleaned) - 1)
        y = height - 4 - (value - min_v) / (max_v - min_v) * (height - 8)
        points.append(f"{x:.1f},{y:.1f}")
    spark_id = _slug(uid or label)
    return (
        f'<div class="sparkline" title="{escape(label)}">'
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" aria-label="{escape(label)}">'
        f'<defs><linearGradient id="fill-{spark_id}" x1="0" x2="0" y1="0" y2="1">'
        f'<stop offset="0%" stop-color="{escape(color)}" stop-opacity="0.24"/>'
        f'<stop offset="100%" stop-color="{escape(color)}" stop-opacity="0.02"/>'
        f'</linearGradient></defs>'
        f'<polyline fill="url(#fill-{spark_id})" stroke="{escape(color)}" stroke-width="2" '
        f'points="4,{height-4} {" ".join(points)} {width-4},{height-4}" />'
        f'</svg></div>'
    )


def _fmt_text(value) -> str:
    if value is None or value == "" or pd.isna(value):
        return ""
    return escape(str(value))


def _fmt_date(value) -> str:
    if value is None or value == "" or pd.isna(value):
        return ""
    return escape(pd.to_datetime(value).strftime("%Y-%m-%d"))


def _fmt_int(value) -> str:
    if value is None or value == "" or pd.isna(value):
        return ""
    return escape(f"{int(round(float(value))):,}")


def _fmt_signed_int(value) -> str:
    if value is None or value == "" or pd.isna(value):
        return ""
    number = int(round(float(value)))
    return escape(f"{number:+,}")


def _fmt_money(value) -> str:
    if value is None or value == "" or pd.isna(value):
        return ""
    return escape(f"{float(value):,.2f}")


def _fmt_money_4(value) -> str:
    if value is None or value == "" or pd.isna(value):
        return ""
    return escape(f"{float(value):,.4f}")


def _fmt_pct(value) -> str:
    if value is None or value == "" or pd.isna(value):
        return ""
    return escape(f"{float(value):.2%}")


def _fmt_bool(value) -> str:
    if value is None or value == "" or pd.isna(value):
        return ""
    if isinstance(value, str):
        return "Yes" if value.strip().lower() in {"true", "1", "yes", "y"} else "No"
    return "Yes" if bool(value) else "No"


def _fmt_side(value) -> str:
    if value is None or value == "" or pd.isna(value):
        return ""
    side = str(value).upper()
    cls = "action-buy" if side == "BUY" else "action-sell" if side == "SELL" else "action-hold"
    return f'<span class="badge {cls}">{escape(side)}</span>'


def _fmt_action(value) -> str:
    if value is None or value == "" or pd.isna(value):
        return ""
    action = str(value).upper()
    cls_map = {
        "OPEN": "action-open",
        "ADD": "action-add",
        "REDUCE": "action-reduce",
        "CLOSE": "action-close",
        "HOLD": "action-hold",
        "BUY": "action-buy",
        "SELL": "action-sell",
    }
    cls = cls_map.get(action, "action-hold")
    return f'<span class="badge {cls}">{escape(action)}</span>'


def _fmt_symbol_link(value) -> str:
    if value is None or value == "" or pd.isna(value):
        return ""
    symbol = str(value)
    return f'<a href="#symbol-{_slug(symbol)}">{escape(symbol)}</a>'


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in str(value))
