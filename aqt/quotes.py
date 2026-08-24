from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import subprocess
from typing import Iterable

import pandas as pd
from urllib.parse import urlencode


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    open: float
    high: float
    low: float
    volume: float
    updated_at: datetime


def fetch_realtime_quotes(symbols: Iterable[str], cache_path: Path | None = None) -> dict[str, Quote]:
    wanted = set(symbols)
    if not wanted:
        return {}

    quotes: dict[str, Quote] = {}
    for fetcher in (_fetch_realtime_quotes_direct, _fetch_realtime_quotes_akshare):
        try:
            fetched = fetcher(wanted)
        except Exception:
            fetched = {}
        if fetched:
            quotes.update(fetched)
        if len(quotes) >= len(wanted):
            break

    if cache_path is not None:
        cached = _load_quote_cache(cache_path)
        for symbol in wanted:
            if symbol not in quotes and symbol in cached:
                quotes[symbol] = cached[symbol]
        if quotes:
            _save_quote_cache(cache_path, quotes)

    return {symbol: quotes[symbol] for symbol in wanted if symbol in quotes}


def _fetch_realtime_quotes_direct(symbols: Iterable[str]) -> dict[str, Quote]:
    wanted = set(symbols)
    if not wanted:
        return {}
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
        "fields": "f2,f5,f12,f14,f15,f16,f17",
    }
    payload = _curl_json(url, params)
    rows = payload.get("data", {}).get("diff", []) or []
    now = datetime.now()
    quotes: dict[str, Quote] = {}
    for row in rows:
        symbol = _normalize_symbol(str(row.get("f12", "")))
        if symbol not in wanted:
            continue
        price = _safe_float(row.get("f2"))
        if price <= 0:
            continue
        quotes[symbol] = Quote(
            symbol=symbol,
            price=price,
            open=_safe_float(row.get("f17")) or price,
            high=_safe_float(row.get("f15")) or price,
            low=_safe_float(row.get("f16")) or price,
            volume=_safe_float(row.get("f5")),
            updated_at=now,
        )
    return quotes


def _fetch_realtime_quotes_akshare(symbols: Iterable[str]) -> dict[str, Quote]:
    wanted = set(symbols)
    if not wanted:
        return {}
    import akshare as ak

    frame = ak.stock_zh_a_spot_em()
    if frame.empty:
        return {}

    now = datetime.now()
    quotes: dict[str, Quote] = {}
    for _, row in frame.iterrows():
        symbol = _normalize_symbol(str(row.get("代码", "")))
        if symbol not in wanted:
            continue
        price = _safe_float(row.get("最新价"))
        if price <= 0:
            continue
        quotes[symbol] = Quote(
            symbol=symbol,
            price=price,
            open=_safe_float(row.get("今开")) or price,
            high=_safe_float(row.get("最高")) or price,
            low=_safe_float(row.get("最低")) or price,
            volume=_safe_float(row.get("成交量")),
            updated_at=now,
        )
    return quotes


def _load_quote_cache(path: Path) -> dict[str, Quote]:
    if not path.exists():
        return {}
    try:
        frame = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return {}
    required = {"symbol", "price", "open", "high", "low", "volume", "updated_at"}
    if frame.empty or not required.issubset(frame.columns):
        return {}
    quotes: dict[str, Quote] = {}
    for row in frame.to_dict(orient="records"):
        symbol = _normalize_symbol(str(row.get("symbol", "")))
        if not symbol:
            continue
        updated_at = row.get("updated_at")
        try:
            parsed_updated_at = datetime.fromisoformat(str(updated_at))
        except (TypeError, ValueError):
            parsed_updated_at = datetime.now()
        quotes[symbol] = Quote(
            symbol=symbol,
            price=_safe_float(row.get("price")),
            open=_safe_float(row.get("open")),
            high=_safe_float(row.get("high")),
            low=_safe_float(row.get("low")),
            volume=_safe_float(row.get("volume")),
            updated_at=parsed_updated_at,
        )
    return quotes


def _save_quote_cache(path: Path, quotes: dict[str, Quote]) -> None:
    if not quotes:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for quote in quotes.values():
        row = asdict(quote)
        row["updated_at"] = quote.updated_at.isoformat(timespec="seconds")
        rows.append(row)
    pd.DataFrame(rows).to_csv(path, index=False)


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
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
    return json.loads(result.stdout)


def _normalize_symbol(code: str) -> str:
    code = code.strip().split(".")[0].zfill(6)
    if code.startswith("6"):
        return f"{code}.SH"
    return f"{code}.SZ"


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
