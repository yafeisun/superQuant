from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import subprocess
import time
from typing import Iterable
from urllib.parse import urlencode

import pandas as pd

from .config import FlowConfig


FLOW_COLUMNS = [
    "symbol",
    "generated_at",
    "latest_date",
    "lookback_days",
    "positive_main_flow_days",
    "main_net_inflow_sum",
    "main_net_inflow_ratio_avg",
    "latest_main_net_inflow",
    "latest_main_net_inflow_ratio",
]


def fetch_money_flow(symbols: Iterable[str], output_path: Path, lookback_days: int) -> pd.DataFrame:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    generated_at = datetime.now().isoformat(timespec="seconds")
    for symbol in symbols:
        try:
            frame = _fetch_individual_money_flow(symbol)
        except Exception:
            continue
        summary = _summarize_money_flow(symbol, frame, max(lookback_days, 1), generated_at)
        if summary:
            rows.append(summary)
    result = pd.DataFrame(rows, columns=FLOW_COLUMNS)
    if not result.empty:
        result.to_csv(output_path, index=False)
    return result


def load_money_flow(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=FLOW_COLUMNS)
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=FLOW_COLUMNS)


def evaluate_money_flow(flow: pd.DataFrame, config: FlowConfig, as_of: date | None = None) -> pd.DataFrame:
    if flow.empty:
        return flow
    frame = flow.copy()
    frame["block_reasons"] = frame.apply(lambda row: ";".join(_block_reasons(row, config, as_of)), axis=1)
    frame["flow_confirmed"] = frame["block_reasons"] == ""
    frame["flow_score"] = frame.apply(lambda row: _flow_score(row, config), axis=1)
    return frame.sort_values(["flow_confirmed", "flow_score", "positive_main_flow_days"], ascending=[False, False, False])


def confirmed_flow_symbols(symbols: Iterable[str], flow: pd.DataFrame, config: FlowConfig, as_of: date | None = None) -> list[str]:
    if not config.enabled:
        return list(symbols)
    if flow.empty:
        return []
    evaluated = evaluate_money_flow(flow, config, as_of)
    allowed = set(evaluated[evaluated["flow_confirmed"]]["symbol"])
    return [symbol for symbol in symbols if symbol in allowed]


def _block_reasons(row: pd.Series, config: FlowConfig, as_of: date | None) -> list[str]:
    reasons = []
    latest_date = _safe_date(row.get("latest_date"))
    if as_of and latest_date and (as_of - latest_date).days > config.max_age_days:
        reasons.append("money_flow_stale")
    if _safe_int(row.get("positive_main_flow_days")) < config.min_positive_days:
        reasons.append("main_flow_not_persistent")
    if _safe_float(row.get("main_net_inflow_sum")) < config.min_main_net_inflow_amount:
        reasons.append("main_flow_amount_too_low")
    if _safe_float(row.get("main_net_inflow_ratio_avg")) < config.min_main_net_inflow_ratio:
        reasons.append("main_flow_ratio_too_low")
    return reasons


def _flow_score(row: pd.Series, config: FlowConfig) -> float:
    positive_days = _safe_int(row.get("positive_main_flow_days"))
    ratio = _safe_float(row.get("main_net_inflow_ratio_avg"))
    amount = _safe_float(row.get("main_net_inflow_sum"))
    days_score = 50.0 * min(positive_days / max(config.lookback_days, 1), 1.0)
    ratio_score = max(min(ratio, 20.0), -20.0)
    amount_score = min(max(amount, 0.0) / 10_000_000.0, 30.0)
    return round(days_score + ratio_score + amount_score, 2)


def _fetch_individual_money_flow(symbol: str) -> pd.DataFrame:
    try:
        return _fetch_individual_money_flow_direct(symbol)
    except Exception:
        return _fetch_individual_money_flow_akshare(symbol)


def _fetch_individual_money_flow_akshare(symbol: str) -> pd.DataFrame:
    import akshare as ak

    frame = ak.stock_individual_fund_flow(stock=symbol.split(".")[0], market=symbol.split(".")[1].lower())
    if frame.empty:
        return frame
    return pd.DataFrame(
        {
            "date": pd.to_datetime(frame["日期"]),
            "close": pd.to_numeric(frame["收盘价"], errors="coerce"),
            "pct_chg": pd.to_numeric(frame["涨跌幅"], errors="coerce"),
            "main_net_inflow": pd.to_numeric(frame["主力净流入-净额"], errors="coerce"),
            "main_net_inflow_ratio": pd.to_numeric(frame["主力净流入-净占比"], errors="coerce"),
        }
    )


def _fetch_individual_money_flow_direct(symbol: str) -> pd.DataFrame:
    market = _eastmoney_market_id(symbol)
    code = symbol.split(".")[0]
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "lmt": "0",
        "klt": "101",
        "secid": f"{market}.{code}",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }
    payload = _curl_json(url, params)
    if payload.get("rc") != 0 or not payload.get("data"):
        raise RuntimeError(f"Eastmoney returned invalid money-flow data for {symbol}: {payload.get('rc')}")
    rows = []
    for line in payload["data"].get("klines", []) or []:
        parts = line.split(",")
        if len(parts) < 13:
            continue
        rows.append(
            {
                "date": pd.to_datetime(parts[0]),
                "close": _safe_float(parts[11]),
                "pct_chg": _safe_float(parts[12]),
                "main_net_inflow": _safe_float(parts[1]),
                "main_net_inflow_ratio": _safe_float(parts[6]),
            }
        )
    if not rows:
        raise RuntimeError(f"Eastmoney returned no money-flow rows for {symbol}")
    return pd.DataFrame(rows)


def _summarize_money_flow(symbol: str, frame: pd.DataFrame, lookback_days: int, generated_at: str) -> dict | None:
    if frame.empty:
        return None
    clean = frame.dropna(subset=["date", "main_net_inflow", "main_net_inflow_ratio"]).sort_values("date")
    if clean.empty:
        return None
    recent = clean.tail(lookback_days)
    main_amount = pd.to_numeric(recent["main_net_inflow"], errors="coerce").fillna(0.0)
    main_ratio = pd.to_numeric(recent["main_net_inflow_ratio"], errors="coerce").fillna(0.0)
    latest = recent.iloc[-1]
    return {
        "symbol": symbol,
        "generated_at": generated_at,
        "latest_date": pd.to_datetime(latest["date"]).date().isoformat(),
        "lookback_days": int(len(recent)),
        "positive_main_flow_days": int((main_amount > 0).sum()),
        "main_net_inflow_sum": round(float(main_amount.sum()), 2),
        "main_net_inflow_ratio_avg": round(float(main_ratio.mean()), 4),
        "latest_main_net_inflow": round(float(latest["main_net_inflow"]), 2),
        "latest_main_net_inflow_ratio": round(float(latest["main_net_inflow_ratio"]), 4),
    }


def _curl_json(url: str, params: dict[str, str]) -> dict:
    full_url = f"{url}?{urlencode(params)}"
    command = [
        "curl",
        "-fsSL",
        "--http1.1",
        "--connect-timeout",
        "5",
        "--max-time",
        "12",
        "--retry",
        "1",
        "--retry-delay",
        "1",
        "--retry-all-errors",
        "-A",
        "Mozilla/5.0",
        "-e",
        "https://quote.eastmoney.com/",
        full_url,
    ]
    result = None
    for attempt in range(2):
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt == 1:
                raise
            time.sleep(0.5)
    if result is None:
        raise RuntimeError("money-flow request returned no result")
    return json.loads(result.stdout)


def _eastmoney_market_id(symbol: str) -> str:
    if symbol.endswith(".SH"):
        return "1"
    if symbol.endswith(".SZ") or symbol.endswith(".BJ"):
        return "0"
    raise ValueError(f"unsupported A-share symbol suffix: {symbol}")


def _safe_date(value) -> date | None:
    try:
        return pd.to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def _safe_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value) -> float:
    if value is None or value == "-":
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if pd.isna(result):
        return 0.0
    return result
