from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import subprocess
import time
from typing import Iterable

import pandas as pd

from .config import HealthConfig


STATIC_BLOCKLIST_PATH = Path("data/risk/blocked_symbols.csv")


def fetch_stock_health(symbols: Iterable[str], output_path: Path) -> pd.DataFrame:
    wanted = set(symbols)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        spot = _fetch_spot_em()
    except Exception:
        spot = pd.DataFrame()
    suspended = _fetch_suspended_symbols()
    st_symbols = _fetch_st_symbols()
    spot_rows = {}
    for _, row in spot.iterrows():
        symbol = _normalize_symbol(str(row.get("代码", "")))
        if symbol in wanted:
            spot_rows[symbol] = row
    rows = _static_blocked_rows(wanted)
    generated_at = datetime.now().isoformat(timespec="seconds")
    for symbol in sorted(wanted):
        row = spot_rows.get(symbol)
        if row is None:
            row = _fetch_quote_row(symbol)
        if row is None:
            continue
        name = str(row.get("名称", ""))
        latest = _safe_float(row.get("最新价"))
        pct_chg = _safe_float(row.get("涨跌幅"))
        turnover_rate = _safe_float(row.get("换手率"))
        pe = _safe_float(row.get("市盈率-动态"))
        amount = _safe_float(row.get("成交额"))
        total_market_cap = _safe_float(row.get("总市值"))
        float_market_cap = _safe_float(row.get("流通市值"))
        is_st = symbol in st_symbols or name.startswith(("ST", "*ST")) or "退" in name
        is_suspended = symbol in suspended or latest <= 0 or amount <= 0
        is_limit_up = pct_chg >= 9.8
        is_limit_down = pct_chg <= -9.8
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "generated_at": generated_at,
                "latest": latest,
                "pct_chg": pct_chg,
                "turnover_rate": turnover_rate,
                "pe": pe,
                "amount": amount,
                "total_market_cap": total_market_cap,
                "float_market_cap": float_market_cap,
                "is_st": is_st,
                "is_suspended": is_suspended,
                "is_limit_up": is_limit_up,
                "is_limit_down": is_limit_down,
            }
        )
    frame = _dedupe_health_rows(pd.DataFrame(rows))
    if not frame.empty:
        frame.to_csv(output_path, index=False)
    return frame


def load_stock_health(path: Path) -> pd.DataFrame:
    if not path.exists():
        return _dedupe_health_rows(pd.DataFrame(_static_blocked_rows()))
    try:
        frame = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        frame = pd.DataFrame()
    static = pd.DataFrame(_static_blocked_rows())
    if static.empty:
        return frame
    return _dedupe_health_rows(pd.concat([frame, static], ignore_index=True))


def healthy_symbols(symbols: Iterable[str], health: pd.DataFrame, config: HealthConfig) -> list[str]:
    if not config.enabled:
        return list(symbols)
    if health.empty:
        return []
    evaluated = evaluate_health(health, config)
    allowed = set(evaluated[evaluated["tradable"] & (evaluated["health_score"] >= config.min_health_score)]["symbol"])
    return [symbol for symbol in symbols if symbol in allowed]


def evaluate_health(health: pd.DataFrame, config: HealthConfig) -> pd.DataFrame:
    if health.empty:
        return health
    frame = health.copy()
    frame["block_reasons"] = frame.apply(lambda row: ";".join(_block_reasons(row, config)), axis=1)
    frame["tradable"] = frame["block_reasons"] == ""
    frame["health_score"] = frame.apply(lambda row: _health_score(row, config), axis=1)
    return frame.sort_values(["tradable", "health_score", "turnover_rate"], ascending=[False, False, False])


def write_health_report(health: pd.DataFrame, config: HealthConfig, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    evaluate_health(health, config).to_csv(output_path, index=False)
    return output_path


def _static_blocked_rows(wanted: set[str] | None = None) -> list[dict]:
    if not STATIC_BLOCKLIST_PATH.exists():
        return []
    try:
        frame = pd.read_csv(STATIC_BLOCKLIST_PATH)
    except pd.errors.EmptyDataError:
        return []
    if frame.empty or "symbol" not in frame.columns:
        return []
    rows = []
    generated_at = datetime.now().isoformat(timespec="seconds")
    for row in frame.to_dict(orient="records"):
        symbol = str(row.get("symbol", ""))
        if not symbol or wanted is not None and symbol not in wanted:
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": str(row.get("name", "")),
                "generated_at": generated_at,
                "latest": 0.0,
                "pct_chg": 0.0,
                "turnover_rate": 0.0,
                "pe": 0.0,
                "amount": 0.0,
                "total_market_cap": 0.0,
                "float_market_cap": 0.0,
                "is_st": True,
                "is_suspended": False,
                "is_limit_up": False,
                "is_limit_down": False,
            }
        )
    return rows


def _dedupe_health_rows(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "symbol" not in frame.columns:
        return frame
    frame = frame.copy()
    frame["symbol"] = frame["symbol"].astype(str)
    frame["_static_rank"] = frame["is_st"].apply(lambda value: 1 if _safe_bool(value) else 0) if "is_st" in frame.columns else 0
    frame = frame.sort_values(["symbol", "_static_rank"]).drop_duplicates("symbol", keep="last")
    return frame.drop(columns=["_static_rank"], errors="ignore").reset_index(drop=True)


def _block_reasons(row: pd.Series, config: HealthConfig) -> list[str]:
    reasons = []
    if config.exclude_suspended and _safe_bool(row.get("is_suspended", False)):
        reasons.append("suspended")
    if config.exclude_st and _safe_bool(row.get("is_st", False)):
        reasons.append("st_or_delisting_risk")
    if config.exclude_limit_up and _safe_bool(row.get("is_limit_up", False)):
        reasons.append("limit_up_no_buy")
    if config.exclude_limit_down and _safe_bool(row.get("is_limit_down", False)):
        reasons.append("limit_down")
    if _safe_float(row.get("turnover_rate")) < config.min_turnover_rate:
        reasons.append("turnover_too_low")
    if _safe_float(row.get("amount")) < config.min_amount:
        reasons.append("amount_too_low")
    pe = _safe_float(row.get("pe"))
    if pe <= config.min_pe or pe > config.max_pe:
        reasons.append("pe_out_of_range")
    float_market_cap = _safe_float(row.get("float_market_cap"))
    if float_market_cap < config.min_float_market_cap or float_market_cap > config.max_float_market_cap:
        reasons.append("float_market_cap_out_of_range")
    return reasons


def _health_score(row: pd.Series, config: HealthConfig) -> float:
    score = 100.0
    score -= 40.0 if _safe_bool(row.get("is_suspended", False)) else 0.0
    score -= 35.0 if _safe_bool(row.get("is_st", False)) else 0.0
    score -= 20.0 if _safe_bool(row.get("is_limit_up", False)) else 0.0
    score -= 15.0 if _safe_bool(row.get("is_limit_down", False)) else 0.0
    turnover = _safe_float(row.get("turnover_rate"))
    if turnover < config.min_turnover_rate:
        score -= 20.0
    elif turnover < config.min_turnover_rate * 2:
        score -= 8.0
    amount = _safe_float(row.get("amount"))
    if amount < config.min_amount:
        score -= 20.0
    elif amount < config.min_amount * 2:
        score -= 8.0
    pe = _safe_float(row.get("pe"))
    if pe <= config.min_pe or pe > config.max_pe:
        score -= 15.0
    float_market_cap = _safe_float(row.get("float_market_cap"))
    if float_market_cap < config.min_float_market_cap or float_market_cap > config.max_float_market_cap:
        score -= 10.0
    return round(max(score, 0.0), 2)


def _fetch_spot_em() -> pd.DataFrame:
    try:
        return _fetch_spot_em_direct()
    except Exception:
        import akshare as ak

        return ak.stock_zh_a_spot_em()


def _fetch_spot_em_direct() -> pd.DataFrame:
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",
        "pz": "6000",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f2,f3,f6,f8,f9,f12,f14,f20,f21",
    }
    command = [
        "curl",
        "-fsSL",
        "-G",
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
        *sum((["--data-urlencode", f"{key}={value}"] for key, value in params.items()), []),
        url,
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("failed to fetch A-share spot health data") from exc
    payload = json.loads(result.stdout)
    rows = payload.get("data", {}).get("diff", []) or []
    normalized = [
        {
            "代码": row.get("f12"),
            "名称": row.get("f14"),
            "最新价": row.get("f2"),
            "涨跌幅": row.get("f3"),
            "换手率": row.get("f8"),
            "市盈率-动态": row.get("f9"),
            "成交额": row.get("f6"),
            "总市值": row.get("f20"),
            "流通市值": row.get("f21"),
        }
        for row in rows
    ]
    return pd.DataFrame(normalized)


def _fetch_quote_row(symbol: str) -> dict | None:
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": f"{_eastmoney_market_id(symbol)}.{symbol.split('.')[0]}",
        "fields": "f43,f57,f58,f170,f168,f162,f46,f44,f45,f47,f48,f116,f117",
    }
    command = [
        "curl",
        "-fsSL",
        "-G",
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
        *sum((["--data-urlencode", f"{key}={value}"] for key, value in params.items()), []),
        url,
    ]
    result = None
    for attempt in range(2):
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt == 1:
                return None
            time.sleep(0.5)
    if result is None:
        return None
    payload = json.loads(result.stdout)
    data = payload.get("data") or {}
    if payload.get("rc") != 0 or not data:
        return None
    return {
        "代码": data.get("f57"),
        "名称": data.get("f58"),
        "最新价": _scale_quote_value(data.get("f43")),
        "涨跌幅": _scale_quote_value(data.get("f170")),
        "换手率": _scale_quote_value(data.get("f168")),
        "市盈率-动态": _scale_quote_value(data.get("f162")),
        "成交额": data.get("f48"),
        "总市值": data.get("f116"),
        "流通市值": data.get("f117"),
    }


def _scale_quote_value(value) -> float:
    if value is None or value == "-":
        return 0.0
    return _safe_float(value) / 100.0


def _fetch_suspended_symbols() -> set[str]:
    return set()


def _fetch_st_symbols() -> set[str]:
    return set()


def _normalize_symbol(code: str) -> str:
    code = code.strip().split(".")[0].zfill(6)
    if code.startswith("6"):
        return f"{code}.SH"
    return f"{code}.SZ"


def _eastmoney_market_id(symbol: str) -> str:
    if symbol.endswith(".SH"):
        return "1"
    if symbol.endswith(".SZ") or symbol.endswith(".BJ"):
        return "0"
    raise ValueError(f"unsupported A-share symbol suffix: {symbol}")


def _safe_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or pd.isna(value):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


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
