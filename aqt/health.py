from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import subprocess
from typing import Iterable

import pandas as pd

from .config import HealthConfig


def fetch_stock_health(symbols: Iterable[str], output_path: Path) -> pd.DataFrame:
    wanted = set(symbols)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    spot = _fetch_spot_em()
    suspended = _fetch_suspended_symbols()
    st_symbols = _fetch_st_symbols()
    rows = []
    generated_at = datetime.now().isoformat(timespec="seconds")
    for _, row in spot.iterrows():
        symbol = _normalize_symbol(str(row.get("代码", "")))
        if symbol not in wanted:
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
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame.to_csv(output_path, index=False)
    return frame


def load_stock_health(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


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
        import akshare as ak

        return ak.stock_zh_a_spot_em()
    except Exception:
        return _fetch_spot_em_direct()


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
        "-A",
        "Mozilla/5.0",
        "-e",
        "https://quote.eastmoney.com/",
        *sum((["--data-urlencode", f"{key}={value}"] for key, value in params.items()), []),
        url,
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
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


def _fetch_suspended_symbols() -> set[str]:
    try:
        import akshare as ak

        frame = ak.stock_zh_a_stop_em()
    except Exception:
        return set()
    if frame.empty or "代码" not in frame.columns:
        return set()
    return {_normalize_symbol(str(code)) for code in frame["代码"]}


def _fetch_st_symbols() -> set[str]:
    try:
        import akshare as ak

        frame = ak.stock_zh_a_st_em()
    except Exception:
        return set()
    if frame.empty or "代码" not in frame.columns:
        return set()
    return {_normalize_symbol(str(code)) for code in frame["代码"]}


def _normalize_symbol(code: str) -> str:
    code = code.strip().split(".")[0].zfill(6)
    if code.startswith("6"):
        return f"{code}.SH"
    return f"{code}.SZ"


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
