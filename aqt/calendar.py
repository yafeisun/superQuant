from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd


DEFAULT_CALENDAR_PATH = Path("data/calendar/a_share_trade_dates.csv")


def parse_calendar_date(value: str | None, fallback: date) -> date:
    if not value:
        return fallback
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unsupported date format: {value}")


def is_a_share_trading_day(target: date, calendar_path: Path = DEFAULT_CALENDAR_PATH) -> bool:
    if target.weekday() >= 5:
        return False

    cached = _read_cached_calendar(calendar_path)
    if _calendar_covers_year(cached, target.year):
        return target in cached

    fetched = _fetch_trade_calendar()
    if fetched:
        _write_calendar(calendar_path, fetched)
        return target in fetched

    if cached:
        return target in cached
    return True


def _read_cached_calendar(path: Path) -> set[date]:
    if not path.exists():
        return set()
    try:
        frame = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return set()
    if "trade_date" not in frame.columns:
        return set()
    return set(pd.to_datetime(frame["trade_date"]).dt.date)


def _calendar_covers_year(days: set[date], year: int) -> bool:
    return any(day.year == year for day in days)


def _fetch_trade_calendar() -> set[date]:
    try:
        import akshare as ak

        frame = ak.tool_trade_date_hist_sina()
    except Exception:
        return set()
    if frame.empty or "trade_date" not in frame.columns:
        return set()
    return set(pd.to_datetime(frame["trade_date"]).dt.date)


def _write_calendar(path: Path, days: set[date]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"trade_date": day.isoformat()} for day in sorted(days)]
    pd.DataFrame(rows).to_csv(path, index=False)
