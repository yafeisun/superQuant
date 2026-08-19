from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Dict, Iterable, List
from urllib.parse import urlencode

import numpy as np
import pandas as pd

from .models import Bar


REQUIRED_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def symbol_to_filename(symbol: str) -> str:
    return f"{symbol.replace('.', '_')}.csv"


def generate_sample_data(output_dir: Path, symbols: Iterable[str], start: str, end: str) -> List[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range(start=start, end=end)
    written: List[Path] = []

    for index, symbol in enumerate(symbols):
        seed = int(hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)
        base_price = 12.0 + index * 150.0
        daily_ret = rng.normal(loc=0.0006, scale=0.018 + index * 0.004, size=len(dates))
        close = base_price * np.cumprod(1.0 + daily_ret)
        open_price = close * (1.0 + rng.normal(0.0, 0.004, size=len(dates)))
        high = np.maximum(open_price, close) * (1.0 + rng.uniform(0.001, 0.018, size=len(dates)))
        low = np.minimum(open_price, close) * (1.0 - rng.uniform(0.001, 0.018, size=len(dates)))
        volume = rng.integers(2_000_000, 25_000_000, size=len(dates))

        frame = pd.DataFrame(
            {
                "date": dates.date,
                "open": np.round(open_price, 3),
                "high": np.round(high, 3),
                "low": np.round(low, 3),
                "close": np.round(close, 3),
                "volume": volume,
            }
        )
        file_path = output_dir / symbol_to_filename(symbol)
        frame.to_csv(file_path, index=False)
        written.append(file_path)

    return written


def load_symbol_frame(data_dir: Path, symbol: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    file_path = data_dir / symbol_to_filename(symbol)
    if not file_path.exists():
        raise FileNotFoundError(f"missing market data for {symbol}: {file_path}")

    frame = pd.read_csv(file_path, parse_dates=["date"])
    missing = sorted(set(REQUIRED_COLUMNS) - set(frame.columns))
    if missing:
        raise ValueError(f"{file_path} missing columns: {missing}")

    frame = frame[REQUIRED_COLUMNS].copy()
    frame = frame.drop_duplicates(subset=["date"]).sort_values("date")
    frame = frame.dropna(subset=REQUIRED_COLUMNS)
    if start:
        frame = frame[frame["date"] >= pd.Timestamp(start)]
    if end:
        frame = frame[frame["date"] <= pd.Timestamp(end)]
    return frame


def load_market_data(
    data_dir: Path,
    symbols: Iterable[str],
    start: str | None = None,
    end: str | None = None,
) -> Dict[str, pd.DataFrame]:
    return {symbol: load_symbol_frame(data_dir, symbol, start, end) for symbol in symbols}


def iter_bars(market_data: Dict[str, pd.DataFrame]) -> Iterable[List[Bar]]:
    calendar = sorted(
        set().union(*(set(frame["date"].dt.date) for frame in market_data.values()))
    )
    for trading_day in calendar:
        bars: List[Bar] = []
        for symbol, frame in market_data.items():
            rows = frame[frame["date"].dt.date == trading_day]
            if rows.empty:
                continue
            row = rows.iloc[0]
            bars.append(
                Bar(
                    symbol=symbol,
                    date=trading_day,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            )
        if bars:
            yield bars


def fetch_akshare_daily(
    output_dir: Path,
    symbols: Iterable[str],
    start: str,
    end: str,
    adjust: str = "qfq",
) -> List[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    start_date = start.replace("-", "")
    end_date = end.replace("-", "")

    for symbol in symbols:
        try:
            normalized = _fetch_eastmoney_daily(symbol, start_date, end_date, adjust)
        except Exception:
            try:
                normalized = _fetch_tencent_daily(symbol, start, end, adjust)
            except Exception:
                normalized = _fetch_with_akshare(symbol, start_date, end_date, adjust)

        file_path = output_dir / symbol_to_filename(symbol)
        normalized.to_csv(file_path, index=False)
        written.append(file_path)

    return written


def fetch_small_cap_universe(
    output_file: Path,
    limit: int,
    min_market_cap: float,
    max_market_cap: float,
) -> Path:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        rows = _fetch_eastmoney_stock_list(max(limit * 8, 100))
    except Exception:
        rows = _bootstrap_small_cap_rows()
    selected = []
    for row in rows:
        code = str(row.get("f12", ""))
        name = str(row.get("f14", ""))
        total_market_cap = _safe_float(row.get("f20"))
        turnover_rate = _safe_float(row.get("f8"))
        if not code or not name or total_market_cap <= 0:
            continue
        if name.startswith("ST") or name.startswith("*ST") or "退" in name:
            continue
        if code.startswith(("688", "689", "8", "4")):
            continue
        if not (min_market_cap <= total_market_cap <= max_market_cap):
            continue
        selected.append(
            {
                "symbol": _normalize_a_symbol(code),
                "name": name,
                "total_market_cap": total_market_cap,
                "turnover_rate": turnover_rate,
            }
        )
        if len(selected) >= limit:
            break

    if not selected:
        selected = _bootstrap_small_cap_rows()[:limit]
    pd.DataFrame(selected).to_csv(output_file, index=False)
    return output_file


def _fetch_eastmoney_stock_list(page_size: int) -> List[dict]:
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fid": "f20",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f14,f2,f3,f5,f6,f8,f9,f10,f20,f21",
    }
    payload = _curl_json(url, params)
    if payload.get("rc") != 0:
        raise RuntimeError(f"Eastmoney stock list failed: {payload.get('rc')}")
    return list(payload.get("data", {}).get("diff", []))


def _bootstrap_small_cap_rows() -> List[dict]:
    # Startup universe used only when public market-cap APIs are temporarily unavailable.
    candidates = [
        ("002871", "伟隆股份"),
        ("002875", "安奈儿"),
        ("002883", "中设股份"),
        ("002896", "中大力德"),
        ("002903", "宇环数控"),
        ("002909", "集泰股份"),
        ("002915", "中欣氟材"),
        ("002917", "金奥博"),
        ("002922", "伊戈尔"),
        ("002923", "润都股份"),
        ("003015", "日久光电"),
        ("003016", "欣贺股份"),
        ("003017", "大洋生物"),
        ("003018", "金富科技"),
        ("003020", "立方制药"),
        ("003023", "彩虹集团"),
        ("003025", "思进智能"),
        ("603655", "朗博科技"),
        ("603679", "华体科技"),
        ("603721", "中广天择"),
        ("603933", "睿能科技"),
        ("605033", "美邦股份"),
        ("605060", "联德股份"),
        ("605086", "龙高股份"),
        ("605118", "力鼎光电"),
        ("605151", "西上海"),
        ("605178", "时空科技"),
        ("605186", "健麾信息"),
        ("605188", "国光连锁"),
        ("605198", "德利股份"),
    ]
    return [
        {
            "symbol": _normalize_a_symbol(code),
            "name": name,
            "total_market_cap": 0.0,
            "turnover_rate": 0.0,
            "source": "bootstrap",
        }
        for code, name in candidates
    ]


def _curl_json(url: str, params: dict[str, str]) -> dict:
    result = subprocess.run(
        ["curl", "-fsSL", "-G", *sum((["--data-urlencode", f"{k}={v}"] for k, v in params.items()), []), url],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _normalize_a_symbol(code: str) -> str:
    if code.startswith("6"):
        return f"{code}.SH"
    return f"{code}.SZ"


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fetch_with_akshare(symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("direct Eastmoney fetch failed and AKShare is not installed") from exc

    try:
        frame = ak.stock_zh_a_hist(
            symbol=symbol.split(".")[0],
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
    except Exception as exc:
        raise RuntimeError(f"failed to fetch {symbol} from Eastmoney and AKShare") from exc
    if frame.empty:
        raise RuntimeError(f"AKShare returned no rows for {symbol}")
    return _normalize_akshare_frame(frame)


def _normalize_akshare_frame(frame: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(frame["日期"]).dt.date,
            "open": frame["开盘"],
            "high": frame["最高"],
            "low": frame["最低"],
            "close": frame["收盘"],
            "volume": frame["成交量"],
        }
    )


def _fetch_eastmoney_daily(symbol: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    import requests

    market = _eastmoney_market_id(symbol)
    fqt = {"": "0", "qfq": "1", "hfq": "2"}[adjust]
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": "101",
        "fqt": fqt,
        "secid": f"{market}.{symbol.split('.')[0]}",
        "beg": start_date,
        "end": end_date,
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        payload = _fetch_eastmoney_with_curl(url, params)
    return _eastmoney_payload_to_frame(symbol, payload)


def _fetch_eastmoney_with_curl(url: str, params: dict[str, str]) -> dict:
    full_url = f"{url}?{urlencode(params)}"
    result = subprocess.run(
        [
            "curl",
            "-fsSL",
            "-A",
            "Mozilla/5.0",
            "-e",
            "https://quote.eastmoney.com/",
            full_url,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _fetch_tencent_daily(symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    code = _tencent_symbol(symbol)
    adjust_arg = {"": "", "qfq": "qfq", "hfq": "hfq"}[adjust]
    if adjust_arg:
        param = f"{code},day,{start},{end},640,{adjust_arg}"
        data_key = f"{adjust_arg}day"
    else:
        param = f"{code},day,{start},{end},640"
        data_key = "day"

    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    result = subprocess.run(
        ["curl", "-fsSL", "-G", "--data-urlencode", f"param={param}", url],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    if payload.get("code") != 0:
        raise RuntimeError(f"Tencent returned invalid response for {symbol}: {payload.get('code')}")

    symbol_payload = payload.get("data", {}).get(code, {})
    raw_rows = symbol_payload.get(data_key) or symbol_payload.get("day")
    if not raw_rows:
        raise RuntimeError(f"Tencent returned no rows for {symbol}")

    rows = []
    for parts in raw_rows:
        rows.append(
            {
                "date": pd.to_datetime(parts[0]).date(),
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
            }
        )
    return pd.DataFrame(rows, columns=REQUIRED_COLUMNS)


def _eastmoney_payload_to_frame(symbol: str, payload: dict) -> pd.DataFrame:
    if payload.get("rc") != 0 or not payload.get("data"):
        raise RuntimeError(f"Eastmoney returned invalid response for {symbol}: {payload.get('rc')}")

    rows = []
    for line in payload["data"].get("klines", []):
        parts = line.split(",")
        rows.append(
            {
                "date": pd.to_datetime(parts[0]).date(),
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
            }
        )
    if not rows:
        raise RuntimeError(f"Eastmoney returned no rows for {symbol}")
    return pd.DataFrame(rows, columns=REQUIRED_COLUMNS)


def _eastmoney_market_id(symbol: str) -> str:
    if symbol.endswith(".SH"):
        return "1"
    if symbol.endswith(".SZ") or symbol.endswith(".BJ"):
        return "0"
    raise ValueError(f"unsupported A-share symbol suffix: {symbol}")


def _tencent_symbol(symbol: str) -> str:
    code, exchange = symbol.split(".")
    exchange = exchange.lower()
    if exchange not in {"sh", "sz", "bj"}:
        raise ValueError(f"unsupported A-share symbol suffix: {symbol}")
    return f"{exchange}{code}"
