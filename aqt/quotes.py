from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import pandas as pd


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    open: float
    high: float
    low: float
    volume: float
    updated_at: datetime


def fetch_realtime_quotes(symbols: Iterable[str]) -> dict[str, Quote]:
    wanted = set(symbols)
    if not wanted:
        return {}

    try:
        import akshare as ak

        frame = ak.stock_zh_a_spot_em()
    except Exception as exc:
        raise RuntimeError("failed to fetch realtime A-share quotes") from exc

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
