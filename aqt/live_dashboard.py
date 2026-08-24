from __future__ import annotations

from datetime import datetime
from html import escape
import json
from pathlib import Path
from typing import Callable

import pandas as pd

from .config import load_config
from .data import load_market_data
from .flow import load_money_flow


def generate_live_dashboard(state_dir: Path, output_path: Path, config_path: Path | None = None) -> Path:
    snapshot = _load_snapshot(state_dir, config_path)
    html = _render_dashboard(snapshot)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _load_snapshot(state_dir: Path, config_path: Path | None = None) -> dict:
    account = _read_json(state_dir / "account.json")
    equity = _read_csv(state_dir / "equity.csv")
    positions = _read_csv(state_dir / "positions.csv")
    fills = _read_csv(state_dir / "fills.csv")
    rejections = _read_csv(state_dir / "rejections.csv")
    quotes = _read_csv(state_dir / "quotes.csv")
    intraday_events = _read_csv(state_dir / "intraday_events.csv")
    status_events = _read_status_events(state_dir / "status")
    latest_status = _latest_statuses(state_dir / "status")
    decision_dir = _latest_decision_dir(state_dir / "decisions")
    summary = _read_summary(decision_dir / "summary.txt") if decision_dir else {}
    advice = _read_csv(decision_dir / "position_advice.csv") if decision_dir else pd.DataFrame()
    orders = _read_csv(decision_dir / "orders.csv") if decision_dir else pd.DataFrame()
    candidates = _read_csv(decision_dir / "selection_candidates.csv") if decision_dir else pd.DataFrame()
    if candidates.empty and decision_dir:
        candidates = _read_csv(decision_dir / "intraday_selection_candidates.csv")
    decision_flow = _read_csv(decision_dir / "money_flow.csv") if decision_dir else pd.DataFrame()
    market_data, flow, dashboard_errors = _load_dashboard_market_context(
        config_path, account, positions, fills, quotes, advice, orders, candidates
    )
    return {
        "state_dir": state_dir,
        "config_path": config_path,
        "account": account,
        "equity": equity,
        "positions": positions,
        "fills": fills,
        "rejections": rejections,
        "quotes": quotes,
        "intraday_events": intraday_events,
        "status_events": status_events,
        "latest_status": latest_status,
        "decision_dir": decision_dir,
        "summary": summary,
        "advice": advice,
        "orders": orders,
        "candidates": candidates,
        "decision_flow": decision_flow,
        "market_data": market_data,
        "flow": flow,
        "dashboard_errors": dashboard_errors,
    }


def _render_dashboard(snapshot: dict) -> str:
    state_dir = snapshot["state_dir"]
    account = snapshot["account"]
    equity = snapshot["equity"]
    positions = _latest_positions(snapshot["positions"])
    fills_all = snapshot["fills"]
    rejections_all = snapshot["rejections"]
    fills = _tail(fills_all, 30)
    rejections = _tail(rejections_all, 30)
    quotes = _tail(snapshot["quotes"], 40)
    intraday_events = _tail(snapshot["intraday_events"], 40)
    status_events = _tail(snapshot["status_events"], 40)
    latest_status = snapshot["latest_status"]
    advice_full = snapshot["advice"]
    orders_full = snapshot["orders"]
    candidates_full = snapshot["candidates"]
    advice = _tail(advice_full, 40)
    orders = _tail(orders_full, 40)
    candidates = _head(candidates_full, 50)
    symbol_drilldowns = _render_symbol_drilldowns(snapshot, positions, fills_all, rejections_all, advice, orders, candidates, quotes)
    context_warnings = _render_context_warnings(snapshot["dashboard_errors"])
    summary = snapshot["summary"]
    decision_dir = snapshot["decision_dir"]
    market_value = _market_value(account, positions)
    equity_value = _safe_float(account.get("equity")) or _latest_numeric(equity, "equity") or _safe_float(summary.get("equity"))
    cash = _safe_float(account.get("cash")) or _latest_numeric(equity, "cash")
    return_pct = _latest_numeric(equity, "return_pct")
    active_positions = _active_position_count(account, positions)
    title = "Live Trading Dashboard"
    cards = _render_cards(
        [
            ("Equity", _fmt_money(equity_value)),
            ("Cash", _fmt_money(cash)),
            ("Market Value", _fmt_money(market_value)),
            ("Return", _fmt_pct(return_pct)),
            ("Positions", _fmt_int(active_positions)),
            ("Fills", _fmt_int(len(fills_all))),
            ("Rejections", _fmt_int(len(rejections_all))),
            ("Decision Date", escape(str(summary.get("date", summary.get("signal_date", ""))))),
        ]
    )
    status_panel = _render_status_panel(latest_status)
    equity_chart = _render_sparkline(
        equity["date"].tolist() if not equity.empty and "date" in equity.columns else [],
        equity["equity"].tolist() if not equity.empty and "equity" in equity.columns else [],
        "Equity Curve",
        "#0f766e",
    )
    holdings_table = _render_table(
        _records(positions),
        [
            ("symbol", "Symbol", str),
            ("quantity", "Qty", _fmt_int),
            ("available_to_sell", "Avail", _fmt_int),
            ("avg_cost", "Cost", _fmt_price),
            ("last_price", "Last", _fmt_price),
            ("market_value", "Mkt Value", _fmt_money),
            ("unrealized_pnl", "PnL", _fmt_money),
            ("return_pct", "Return", _fmt_pct),
        ],
        row_symbol_key="symbol",
    )
    advice_table = _render_table(
        _records(advice),
        [
            ("symbol", "Symbol", str),
            ("today_action", "Action", _fmt_badge),
            ("last_price", "Last", _fmt_price),
            ("unrealized_pnl", "PnL", _fmt_money),
            ("sell_stop_level", "Stop", _fmt_price),
            ("sell_take_profit_level", "Target", _fmt_price),
            ("sell_to_stop_pct", "To Stop", _fmt_pct),
            ("sell_to_target_pct", "To Target", _fmt_pct),
            ("next_advice", "Reason", str),
        ],
        row_symbol_key="symbol",
    )
    orders_table = _render_table(
        _records(orders),
        [
            ("symbol", "Symbol", str),
            ("side", "Side", _fmt_badge),
            ("planned_quantity", "Plan Qty", _fmt_int),
            ("filled_quantity", "Fill Qty", _fmt_int),
            ("reference_close", "Ref", _fmt_price),
            ("fill_price", "Fill", _fmt_price),
            ("status", "Status", _fmt_badge),
            ("reason", "Reason", str),
        ],
        row_symbol_key="symbol",
    )
    candidates_table = _render_table(
        _records(candidates),
        [
            ("selection_rank", "Rank", _fmt_int),
            ("symbol", "Symbol", str),
            ("name", "Name", str),
            ("buy_decision", "Decision", _fmt_badge),
            ("selection_score", "Score", _fmt_float),
            ("buy_reason", "Reason", str),
            ("positive_reason", "Positive", str),
        ],
        row_symbol_key="symbol",
    )
    fills_table = _render_table(
        _records(fills),
        [
            ("filled_at", "Date", str),
            ("symbol", "Symbol", str),
            ("side", "Side", _fmt_badge),
            ("quantity", "Qty", _fmt_int),
            ("price", "Price", _fmt_price),
            ("commission", "Fee", _fmt_money),
            ("tax", "Tax", _fmt_money),
            ("reason", "Reason", str),
        ],
        row_symbol_key="symbol",
    )
    rejections_table = _render_table(
        _records(rejections),
        [
            ("date", "Date", str),
            ("symbol", "Symbol", str),
            ("side", "Side", _fmt_badge),
            ("quantity", "Qty", _fmt_int),
            ("reason", "Reason", str),
        ],
        row_symbol_key="symbol",
    )
    quotes_table = _render_table(
        _records(quotes),
        [
            ("symbol", "Symbol", str),
            ("price", "Price", _fmt_price),
            ("open", "Open", _fmt_price),
            ("high", "High", _fmt_price),
            ("low", "Low", _fmt_price),
            ("volume", "Volume", _fmt_int),
            ("updated_at", "Updated", str),
        ],
        row_symbol_key="symbol",
    )
    intraday_table = _render_table(
        _records(intraday_events),
        [
            ("datetime", "Time", str),
            ("equity", "Equity", _fmt_money),
            ("cash", "Cash", _fmt_money),
            ("market_value", "Mkt Value", _fmt_money),
            ("positions", "Positions", _fmt_int),
            ("fills", "Fills", _fmt_int),
            ("rejections", "Rejects", _fmt_int),
            ("fill_symbols", "Activity", str),
        ],
    )
    status_table = _render_table(
        _records(status_events),
        [
            ("generated_at", "Time", str),
            ("job", "Job", str),
            ("status", "Status", _fmt_badge),
            ("severity", "Severity", _fmt_badge),
            ("trading_day", "Day", str),
            ("message", "Message", str),
        ],
    )
    generated_at = datetime.now().isoformat(timespec="seconds")
    decision_text = escape(str(decision_dir)) if decision_dir else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #f5f7f9;
      --panel: #ffffff;
      --text: #111827;
      --muted: #667085;
      --line: #d9e1e8;
      --soft: #eef2f6;
      --green: #0f766e;
      --green-bg: #dff3ef;
      --blue: #1d4ed8;
      --blue-bg: #e3edff;
      --amber: #92400e;
      --amber-bg: #fff4d6;
      --red: #991b1b;
      --red-bg: #fde8e8;
      --purple: #6d28d9;
      --purple-bg: #f1e8ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      font-size: 13px;
    }}
    main {{
      max-width: 1680px;
      margin: 0 auto;
      padding: 16px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      padding: 10px 0 16px;
    }}
    h1, h2 {{
      margin: 0;
      line-height: 1.2;
    }}
    h1 {{ font-size: 24px; }}
    h2 {{ font-size: 16px; margin-bottom: 10px; }}
    .muted {{ color: var(--muted); }}
    .path {{ color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-height: 74px;
    }}
    .card .label {{
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 8px;
    }}
    .card .value {{
      font-size: 20px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.6fr) minmax(340px, 0.8fr);
      gap: 12px;
      margin-top: 12px;
      align-items: start;
    }}
    .section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 12px;
      overflow: hidden;
    }}
    .status-strip {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 8px;
    }}
    .status-item {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fff;
    }}
    .status-item .top {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: center;
      margin-bottom: 6px;
    }}
    .chart {{
      width: 100%;
      min-height: 170px;
    }}
    .symbol-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      gap: 10px;
    }}
    .symbol-panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      min-width: 0;
    }}
    .symbol-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }}
    .symbol-title {{
      font-size: 15px;
      font-weight: 800;
    }}
    .symbol-subtitle {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px;
      margin: 8px 0;
    }}
    .metric {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px;
      min-width: 0;
    }}
    .metric .label {{
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 4px;
    }}
    .metric .value {{
      font-weight: 700;
      overflow-wrap: anywhere;
    }}
    .mini-chart {{
      width: 100%;
      height: 170px;
      display: block;
    }}
    .timeline {{
      border-top: 1px solid var(--line);
      margin-top: 8px;
      padding-top: 8px;
    }}
    .timeline-row {{
      display: grid;
      grid-template-columns: 82px 82px minmax(0, 1fr);
      gap: 6px;
      padding: 4px 0;
      border-bottom: 1px solid var(--soft);
    }}
    .timeline-row:last-child {{ border-bottom: 0; }}
    .warning-box {{
      border: 1px solid var(--amber-bg);
      background: var(--amber-bg);
      color: var(--amber);
      border-radius: 6px;
      padding: 8px;
      margin-bottom: 8px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      table-layout: auto;
    }}
    thead th {{
      position: sticky;
      top: 0;
      z-index: 1;
      background: #f8fafc;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 6px 8px;
      text-align: left;
      vertical-align: top;
    }}
    tr:hover td {{ background: #f8fafc; }}
    .table-wrap {{
      overflow: auto;
      max-height: 430px;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .toolbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
    }}
    input {{
      width: 260px;
      max-width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 9px;
      font: inherit;
      background: #fff;
    }}
    .badge {{
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .badge-buy-ready, .badge-buy, .badge-ok, .badge-executed, .badge-hold {{ background: var(--green-bg); color: var(--green); }}
    .badge-buy-wait, .badge-warning, .badge-stale-data-fallback, .badge-already-processed {{ background: var(--amber-bg); color: var(--amber); }}
    .badge-block, .badge-error, .badge-sell, .badge-sell-now, .badge-no-data, .badge-quote-fetch-failed {{ background: var(--red-bg); color: var(--red); }}
    .badge-info, .badge-skipped-non-trading-day, .badge-skipped-outside-trading-hours {{ background: var(--blue-bg); color: var(--blue); }}
    .badge-empty {{ background: var(--soft); color: var(--muted); }}
    .positive {{ color: var(--green); font-weight: 700; }}
    .negative {{ color: var(--red); font-weight: 700; }}
    @media (max-width: 980px) {{
      main {{ padding: 10px; }}
      header {{ display: block; }}
      .layout {{ grid-template-columns: 1fr; }}
      .symbol-grid {{ grid-template-columns: 1fr; }}
      .metric-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .timeline-row {{ grid-template-columns: 1fr; }}
      .card .value {{ font-size: 18px; }}
      input {{ width: 100%; }}
    }}
  </style>
  <script>
    function filterTables() {{
      const query = document.getElementById('symbolFilter').value.trim().toUpperCase();
      document.querySelectorAll('[data-symbol]').forEach(function(row) {{
        const symbol = (row.getAttribute('data-symbol') || '').toUpperCase();
        row.style.display = !query || symbol.indexOf(query) >= 0 ? '' : 'none';
      }});
    }}
  </script>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>{escape(title)}</h1>
        <div class="path">{escape(str(state_dir))}</div>
        <div class="muted">updated {escape(generated_at)}{f' · decision {decision_text}' if decision_text else ''}</div>
      </div>
      <input id="symbolFilter" type="text" placeholder="Filter symbol" oninput="filterTables()">
    </header>
    <section class="grid">{cards}</section>
    <section class="section">{status_panel}</section>
    <div class="layout">
      <div>
        <section class="section"><h2>Equity</h2>{equity_chart}</section>
        <section class="section"><h2>Holdings</h2>{holdings_table}</section>
        <section class="section"><h2>Symbol Drilldown</h2>{context_warnings}{symbol_drilldowns}</section>
        <section class="section"><h2>Position Actions</h2>{advice_table}</section>
        <section class="section"><h2>Orders</h2>{orders_table}</section>
        <section class="section"><h2>Candidates</h2>{candidates_table}</section>
      </div>
      <aside>
        <section class="section"><h2>Realtime Quotes</h2>{quotes_table}</section>
        <section class="section"><h2>Intraday Events</h2>{intraday_table}</section>
        <section class="section"><h2>Fills</h2>{fills_table}</section>
        <section class="section"><h2>Rejections</h2>{rejections_table}</section>
        <section class="section"><h2>Run Status Events</h2>{status_table}</section>
      </aside>
    </div>
  </main>
</body>
</html>
"""


def _render_cards(items: list[tuple[str, str]]) -> str:
    return "".join(
        f'<div class="card"><div class="label">{escape(label)}</div><div class="value">{value}</div></div>'
        for label, value in items
    )


def _render_status_panel(statuses: list[dict]) -> str:
    if not statuses:
        return '<h2>Run Status</h2><div class="muted">No status files.</div>'
    rows = []
    for item in statuses:
        rows.append(
            '<div class="status-item">'
            '<div class="top">'
            f'<strong>{escape(str(item.get("job", "")))}</strong>'
            f'{_fmt_badge(item.get("status", ""))}'
            '</div>'
            f'<div class="muted">{escape(str(item.get("generated_at", "")))}</div>'
            f'<div>{escape(str(item.get("message", "")))}</div>'
            '</div>'
        )
    return f'<h2>Run Status</h2><div class="status-strip">{"".join(rows)}</div>'


def _render_context_warnings(messages: list[str]) -> str:
    if not messages:
        return ""
    items = "<br>".join(escape(message) for message in messages[:4])
    return f'<div class="warning-box">{items}</div>'


def _render_symbol_drilldowns(
    snapshot: dict,
    positions: pd.DataFrame,
    fills: pd.DataFrame,
    rejections: pd.DataFrame,
    advice: pd.DataFrame,
    orders: pd.DataFrame,
    candidates: pd.DataFrame,
    quotes: pd.DataFrame,
) -> str:
    symbols = _dashboard_symbols(positions, advice, orders, candidates, quotes, fills, rejections)
    if not symbols:
        return '<div class="muted">No symbols to drill down.</div>'

    market_data: dict[str, pd.DataFrame] = snapshot.get("market_data", {})
    flow = snapshot.get("flow", pd.DataFrame())
    decision_flow = snapshot.get("decision_flow", pd.DataFrame())
    position_map = _record_map(positions, "symbol")
    advice_map = _record_map(advice, "symbol")
    candidate_map = _record_map(candidates, "symbol")
    quote_map = _record_map(quotes, "symbol")
    flow_map = _record_map(flow, "symbol")
    decision_flow_map = _record_map(decision_flow, "symbol")
    panels = []
    for symbol in symbols[:18]:
        position = position_map.get(symbol, {})
        candidate = candidate_map.get(symbol, {})
        quote = quote_map.get(symbol, {})
        action = advice_map.get(symbol, {}).get("today_action") or candidate.get("buy_decision") or ""
        name = candidate.get("name") or ""
        flow_row = decision_flow_map.get(symbol) or flow_map.get(symbol, {})
        panel = (
            f'<article class="symbol-panel" data-symbol="{escape(symbol)}">'
            '<div class="symbol-head">'
            '<div>'
            f'<div class="symbol-title">{escape(symbol)}{f" · {escape(str(name))}" if name else ""}</div>'
            f'<div class="symbol-subtitle">{escape(_symbol_subtitle(position, candidate, quote))}</div>'
            '</div>'
            f'{_fmt_badge(action)}'
            '</div>'
            f'{_render_symbol_metrics(position, candidate, quote)}'
            f'{_render_candles(market_data.get(symbol, pd.DataFrame()), symbol)}'
            f'{_render_flow_snapshot(flow_row)}'
            f'{_render_symbol_timeline(symbol, fills, orders, rejections)}'
            '</article>'
        )
        panels.append(panel)
    return f'<div class="symbol-grid">{"".join(panels)}</div>'


def _render_symbol_metrics(position: dict, candidate: dict, quote: dict) -> str:
    metrics = [
        ("Qty", _fmt_int(position.get("quantity", ""))),
        ("Last", _fmt_price(quote.get("price") or position.get("last_price", ""))),
        ("PnL", _fmt_money(position.get("unrealized_pnl", ""))),
        ("Return", _fmt_pct(position.get("return_pct", ""))),
        ("Score", _fmt_float(candidate.get("selection_score", ""))),
        ("Rank", _fmt_int(candidate.get("selection_rank", ""))),
    ]
    return _render_metric_grid(metrics)


def _render_flow_snapshot(row: dict) -> str:
    if not row:
        return '<div class="metric-grid"><div class="metric"><div class="label">Flow</div><div class="value muted">No money-flow snapshot.</div></div></div>'
    days = _fmt_int(row.get("positive_main_flow_days", ""))
    lookback = _fmt_int(row.get("lookback_days", ""))
    positive_days = f"{days}/{lookback}" if days or lookback else ""
    return _render_metric_grid(
        [
            ("Flow Date", escape(str(row.get("latest_date", "")))),
            ("Positive Days", positive_days),
            ("Main Inflow", _fmt_money(row.get("main_net_inflow_sum", ""))),
            ("Avg Ratio", _fmt_float(row.get("main_net_inflow_ratio_avg", ""))),
            ("Latest Inflow", _fmt_money(row.get("latest_main_net_inflow", ""))),
            ("Latest Ratio", _fmt_float(row.get("latest_main_net_inflow_ratio", ""))),
        ]
    )


def _render_metric_grid(items: list[tuple[str, str]]) -> str:
    cells = []
    for label, value in items:
        cells.append(
            '<div class="metric">'
            f'<div class="label">{escape(label)}</div>'
            f'<div class="value">{value}</div>'
            '</div>'
        )
    return f'<div class="metric-grid">{"".join(cells)}</div>'


def _render_candles(frame: pd.DataFrame, symbol: str, days: int = 45) -> str:
    required = {"date", "open", "high", "low", "close"}
    if frame.empty or not required.issubset(frame.columns):
        return '<div class="muted">No local daily bars.</div>'
    clean = frame.copy()
    for column in ["open", "high", "low", "close"]:
        clean[column] = pd.to_numeric(clean[column], errors="coerce")
    clean = clean.dropna(subset=["date", "open", "high", "low", "close"]).tail(days)
    if len(clean) < 2:
        return '<div class="muted">No local daily bars.</div>'

    width = 520
    height = 170
    pad_x = 24
    pad_y = 18
    min_v = float(clean["low"].min())
    max_v = float(clean["high"].max())
    span = max(max_v - min_v, 1.0)
    step = (width - pad_x * 2) / max(len(clean), 1)
    candle_width = max(min(step * 0.58, 8.0), 2.5)
    bars = []

    def y_price(value: float) -> float:
        return height - pad_y - (value - min_v) / span * (height - pad_y * 2)

    for idx, row in enumerate(clean.to_dict(orient="records")):
        open_price = float(row["open"])
        high_price = float(row["high"])
        low_price = float(row["low"])
        close_price = float(row["close"])
        x = pad_x + idx * step + step / 2
        y_high = y_price(high_price)
        y_low = y_price(low_price)
        y_open = y_price(open_price)
        y_close = y_price(close_price)
        color = "#b42318" if close_price >= open_price else "#0f766e"
        body_y = min(y_open, y_close)
        body_h = max(abs(y_open - y_close), 1.2)
        bars.append(
            f'<line x1="{x:.2f}" y1="{y_high:.2f}" x2="{x:.2f}" y2="{y_low:.2f}" stroke="{color}" stroke-width="1.3"/>'
            f'<rect x="{x - candle_width / 2:.2f}" y="{body_y:.2f}" width="{candle_width:.2f}" height="{body_h:.2f}" fill="{color}"/>'
        )
    first_close = float(clean.iloc[0]["close"])
    last_close = float(clean.iloc[-1]["close"])
    pct = last_close / first_close - 1.0 if first_close else 0.0
    first_date = str(pd.to_datetime(clean.iloc[0]["date"]).date())
    last_date = str(pd.to_datetime(clean.iloc[-1]["date"]).date())
    return (
        f'<svg class="mini-chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(symbol)} daily candlesticks">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>'
        f'<line x1="{pad_x}" y1="{height - pad_y}" x2="{width - pad_x}" y2="{height - pad_y}" stroke="#d9e1e8"/>'
        f'{"".join(bars)}'
        f'<text x="{pad_x}" y="14" fill="#667085" font-size="11">{escape(first_date)}</text>'
        f'<text x="{width - pad_x}" y="14" text-anchor="end" fill="#111827" font-size="11" font-weight="700">{escape(last_date)} {_fmt_plain_price(last_close)} ({_fmt_plain_pct(pct)})</text>'
        "</svg>"
    )


def _render_symbol_timeline(symbol: str, fills: pd.DataFrame, orders: pd.DataFrame, rejections: pd.DataFrame) -> str:
    rows: list[dict[str, str]] = []
    for row in _records(fills):
        if str(row.get("symbol", "")) != symbol:
            continue
        rows.append(
            {
                "time": str(row.get("filled_at", "")),
                "type": str(row.get("side", "FILL")),
                "text": f'{_fmt_int(row.get("quantity", ""))} @ {_fmt_price(row.get("price", ""))} {row.get("reason", "")}',
            }
        )
    for row in _records(orders):
        if str(row.get("symbol", "")) != symbol:
            continue
        rows.append(
            {
                "time": str(row.get("date", "")),
                "type": str(row.get("status", row.get("side", "ORDER"))),
                "text": f'{row.get("side", "")} plan {_fmt_int(row.get("planned_quantity", ""))} fill {_fmt_int(row.get("filled_quantity", ""))} {row.get("reason", "")}',
            }
        )
    for row in _records(rejections):
        if str(row.get("symbol", "")) != symbol:
            continue
        rows.append(
            {
                "time": str(row.get("date", "")),
                "type": "REJECTED",
                "text": f'{row.get("side", "")} {_fmt_int(row.get("quantity", ""))} {row.get("reason", "")}',
            }
        )
    rows = sorted(rows, key=lambda row: row["time"], reverse=True)[:6]
    if not rows:
        return '<div class="timeline"><div class="muted">No recent order timeline.</div></div>'
    rendered = []
    for row in rows:
        rendered.append(
            '<div class="timeline-row">'
            f'<div class="muted">{escape(row["time"])}</div>'
            f'<div>{_fmt_badge(row["type"])}</div>'
            f'<div>{escape(row["text"])}</div>'
            '</div>'
        )
    return f'<div class="timeline">{"".join(rendered)}</div>'


def _render_table(
    rows: list[dict],
    columns: list[tuple[str, str, Callable[[object], str]]],
    row_symbol_key: str | None = None,
) -> str:
    if not rows:
        return '<div class="muted">No rows.</div>'
    header = "".join(f"<th>{escape(label)}</th>" for _, label, _ in columns)
    body = []
    for row in rows:
        symbol_attr = ""
        if row_symbol_key:
            symbol_attr = f' data-symbol="{escape(str(row.get(row_symbol_key, "")))}"'
        cells = []
        for key, _, formatter in columns:
            formatted = formatter(row.get(key, ""))
            if formatter is str:
                formatted = escape(formatted)
            cells.append(f"<td>{formatted}</td>")
        body.append(f"<tr{symbol_attr}>{''.join(cells)}</tr>")
    return f'<div class="table-wrap"><table><thead><tr>{header}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _render_sparkline(dates: list, values: list, label: str, color: str) -> str:
    points = [float(value) for value in values if _safe_float(value) is not None]
    if len(points) < 2:
        return '<div class="muted">No equity curve.</div>'
    width = 680
    height = 160
    pad = 16
    min_v = min(points)
    max_v = max(points)
    span = max(max_v - min_v, 1.0)
    step = (width - pad * 2) / max(len(points) - 1, 1)
    coords = []
    for idx, value in enumerate(points):
        x = pad + idx * step
        y = height - pad - (value - min_v) / span * (height - pad * 2)
        coords.append(f"{x:.2f},{y:.2f}")
    first = f"{points[0]:,.2f}"
    last = f"{points[-1]:,.2f}"
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>'
        f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" stroke="#d9e1e8"/>'
        f'<polyline points="{" ".join(coords)}" fill="none" stroke="{escape(color)}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
        f'<text x="{pad}" y="20" fill="#667085" font-size="12">{escape(first)}</text>'
        f'<text x="{width - pad}" y="20" text-anchor="end" fill="#111827" font-size="12" font-weight="700">{escape(last)}</text>'
        "</svg>"
    )


def _latest_positions(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "date" not in frame.columns:
        return frame
    latest_date = frame["date"].astype(str).max()
    latest = frame[frame["date"].astype(str) == latest_date].copy()
    if "quantity" in latest.columns:
        latest = latest[pd.to_numeric(latest["quantity"], errors="coerce").fillna(0) > 0]
    return latest


def _load_dashboard_market_context(
    config_path: Path | None,
    account: dict,
    positions: pd.DataFrame,
    fills: pd.DataFrame,
    quotes: pd.DataFrame,
    advice: pd.DataFrame,
    orders: pd.DataFrame,
    candidates: pd.DataFrame,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, list[str]]:
    if config_path is None:
        return {}, pd.DataFrame(), []
    path = Path(config_path)
    if not path.exists():
        return {}, pd.DataFrame(), [f"dashboard config not found: {path}"]
    try:
        config = load_config(path)
    except Exception as exc:
        return {}, pd.DataFrame(), [f"dashboard config load failed: {exc}"]

    errors: list[str] = []
    market_data: dict[str, pd.DataFrame] = {}
    flow = pd.DataFrame()
    symbols = _symbols_for_market_load(config.data.symbols, account, positions, fills, quotes, advice, orders, candidates)
    try:
        market_data = load_market_data(config.data.path, symbols, config.data.start, config.data.end, strict=False)
    except Exception as exc:
        errors.append(f"daily bars load failed: {exc}")
    try:
        flow = load_money_flow(config.flow.path)
    except Exception as exc:
        errors.append(f"money-flow load failed: {exc}")
    return market_data, flow, errors


def _symbols_for_market_load(
    configured_symbols: list[str],
    account: dict,
    positions: pd.DataFrame,
    fills: pd.DataFrame,
    quotes: pd.DataFrame,
    advice: pd.DataFrame,
    orders: pd.DataFrame,
    candidates: pd.DataFrame,
) -> list[str]:
    ordered: list[str] = []

    def add(symbol: object) -> None:
        text = str(symbol or "").strip()
        if not text or text.lower() == "nan" or text in ordered:
            return
        ordered.append(text)

    for symbol, row in (account.get("positions", {}) or {}).items():
        if isinstance(row, dict) and _safe_float(row.get("quantity")) and _safe_float(row.get("quantity")) > 0:
            add(symbol)
    for frame in [positions, advice, orders, candidates, quotes, fills]:
        for symbol in _symbols_from_frame(frame):
            add(symbol)
    for symbol in configured_symbols:
        add(symbol)
    return ordered[:160]


def _dashboard_symbols(
    positions: pd.DataFrame,
    advice: pd.DataFrame,
    orders: pd.DataFrame,
    candidates: pd.DataFrame,
    quotes: pd.DataFrame,
    fills: pd.DataFrame,
    rejections: pd.DataFrame,
) -> list[str]:
    ordered: list[str] = []

    def add(symbol: object) -> None:
        text = str(symbol or "").strip()
        if not text or text.lower() == "nan" or text in ordered:
            return
        ordered.append(text)

    for frame in [positions, advice, orders, quotes, fills, rejections]:
        for symbol in _symbols_from_frame(frame):
            add(symbol)
    if not candidates.empty:
        for row in candidates.head(12).to_dict(orient="records"):
            add(row.get("symbol", ""))
    return ordered


def _symbols_from_frame(frame: pd.DataFrame) -> list[str]:
    if frame.empty or "symbol" not in frame.columns:
        return []
    return [str(symbol) for symbol in frame["symbol"].dropna().astype(str).tolist() if symbol]


def _record_map(frame: pd.DataFrame, key: str) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    if frame.empty or key not in frame.columns:
        return mapping
    for row in _records(frame):
        value = str(row.get(key, "")).strip()
        if value:
            mapping[value] = row
    return mapping


def _symbol_subtitle(position: dict, candidate: dict, quote: dict) -> str:
    parts = []
    if position:
        parts.append(f"holding {_fmt_plain_int(position.get('quantity', ''))}")
    if candidate.get("buy_reason"):
        parts.append(str(candidate.get("buy_reason", ""))[:80])
    elif candidate.get("positive_reason"):
        parts.append(str(candidate.get("positive_reason", ""))[:80])
    if quote.get("updated_at"):
        parts.append(f"quote {quote.get('updated_at')}")
    return " · ".join(parts)


def _latest_statuses(status_dir: Path) -> list[dict]:
    if not status_dir.exists():
        return []
    statuses = []
    for path in sorted(status_dir.glob("latest_*.json")):
        data = _read_json(path)
        if data:
            statuses.append(data)
    return sorted(statuses, key=lambda row: str(row.get("generated_at", "")), reverse=True)


def _read_status_events(status_dir: Path) -> pd.DataFrame:
    path = status_dir / "events.jsonl"
    if not path.exists():
        return pd.DataFrame()
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        row["details"] = json.dumps(row.get("details", {}), ensure_ascii=False, sort_keys=True)
        rows.append(row)
    return pd.DataFrame(rows)


def _latest_decision_dir(root: Path) -> Path | None:
    if not root.exists():
        return None
    dirs = [path for path in root.iterdir() if path.is_dir()]
    return sorted(dirs)[-1] if dirs else None


def _read_summary(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _records(frame: pd.DataFrame) -> list[dict]:
    if frame.empty:
        return []
    clean = frame.where(pd.notnull(frame), "")
    return clean.to_dict(orient="records")


def _tail(frame: pd.DataFrame, count: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame.tail(count)


def _head(frame: pd.DataFrame, count: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame.head(count)


def _market_value(account: dict, positions: pd.DataFrame) -> float:
    value = _safe_float(account.get("equity"))
    cash = _safe_float(account.get("cash"))
    if value is not None and cash is not None and value >= cash:
        return value - cash
    if positions.empty or "market_value" not in positions.columns:
        return 0.0
    return float(pd.to_numeric(positions["market_value"], errors="coerce").fillna(0.0).sum())


def _active_position_count(account: dict, positions: pd.DataFrame) -> int:
    account_positions = account.get("positions", {})
    if isinstance(account_positions, dict):
        return sum(1 for row in account_positions.values() if _safe_float(row.get("quantity")) and _safe_float(row.get("quantity")) > 0)
    if positions.empty or "quantity" not in positions.columns:
        return 0
    return int((pd.to_numeric(positions["quantity"], errors="coerce").fillna(0) > 0).sum())


def _latest_numeric(frame: pd.DataFrame, column: str) -> float | None:
    if frame.empty or column not in frame.columns:
        return None
    return _safe_float(frame.iloc[-1].get(column))


def _fmt_badge(value) -> str:
    text = str(value or "")
    if not text:
        text = "EMPTY"
    slug = text.lower().replace("_", "-").replace(" ", "-")
    return f'<span class="badge badge-{escape(slug)}">{escape(text)}</span>'


def _fmt_money(value) -> str:
    number = _safe_float(value)
    if number is None:
        return ""
    cls = "negative" if number < 0 else "positive" if number > 0 else ""
    text = f"{number:,.2f}"
    return f'<span class="{cls}">{text}</span>' if cls else text


def _fmt_pct(value) -> str:
    number = _safe_float(value)
    if number is None:
        return ""
    cls = "negative" if number < 0 else "positive" if number > 0 else ""
    text = f"{number:.2%}"
    return f'<span class="{cls}">{text}</span>' if cls else text


def _fmt_price(value) -> str:
    number = _safe_float(value)
    return "" if number is None else f"{number:.4f}"


def _fmt_float(value) -> str:
    number = _safe_float(value)
    return "" if number is None else f"{number:.2f}"


def _fmt_int(value) -> str:
    number = _safe_float(value)
    return "" if number is None else f"{int(number):,}"


def _fmt_plain_price(value) -> str:
    number = _safe_float(value)
    return "" if number is None else f"{number:.2f}"


def _fmt_plain_pct(value) -> str:
    number = _safe_float(value)
    return "" if number is None else f"{number:.2%}"


def _fmt_plain_int(value) -> str:
    number = _safe_float(value)
    return "" if number is None else f"{int(number):,}"


def _safe_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return number
