from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence


SYMBOL_COLUMNS = ("symbol", "code", "证券代码", "股票代码")
METADATA_COLUMNS = {
    "universe_date",
    "listed_date",
    "delisted_date",
    "valid_from",
    "valid_to",
    "universe_end_date",
    "removed_date",
    "board",
    "st_start_date",
    "st_end_date",
    "st_intervals",
}


@dataclass(frozen=True)
class UniverseMember:
    symbol: str
    name: str | None
    universe_date: date | None
    listed_date: date | None
    delisted_date: date | None
    valid_from: date | None
    valid_to: date | None
    board: str | None
    st_intervals: tuple[tuple[date | None, date | None], ...]

    def is_eligible(self, trading_day: date) -> bool:
        effective_start = self.valid_from or self.universe_date
        if effective_start and trading_day < effective_start:
            return False
        if self.valid_to and trading_day > self.valid_to:
            return False
        if self.listed_date and trading_day < self.listed_date:
            return False
        if self.delisted_date and trading_day > self.delisted_date:
            return False
        return not self._is_st_on(trading_day)

    def overlaps(self, start: date | None, end: date | None) -> bool:
        effective_start = _latest_date(self.valid_from or self.universe_date, self.listed_date)
        effective_end = _earliest_date(self.valid_to, self.delisted_date)
        if end and effective_start and effective_start > end:
            return False
        if start and effective_end and effective_end < start:
            return False
        return True

    def _is_st_on(self, trading_day: date) -> bool:
        for start, end in self.st_intervals:
            if start and trading_day < start:
                continue
            if end and trading_day > end:
                continue
            return True
        return False


@dataclass(frozen=True)
class PointInTimeUniverse:
    members: tuple[UniverseMember, ...]

    @property
    def symbols(self) -> list[str]:
        return _unique_preserve_order(member.symbol for member in self.members)

    def symbols_between(self, start: str | date | None, end: str | date | None) -> list[str]:
        start_date = parse_date(start)
        end_date = parse_date(end)
        return _unique_preserve_order(
            member.symbol for member in self.members if member.overlaps(start_date, end_date)
        )

    def is_symbol_eligible(self, symbol: str, trading_day: date) -> bool:
        return any(member.symbol == symbol and member.is_eligible(trading_day) for member in self.members)

    def metadata_warnings(self) -> list[str]:
        warnings: list[str] = []
        if not self.members:
            return ["universe metadata has no members"]
        if not any(member.universe_date for member in self.members):
            warnings.append("universe_date is empty for every member")
        if not any(member.listed_date for member in self.members):
            warnings.append("listed_date is empty for every member")
        return warnings


def read_symbols_file(path: Path, start: str | None = None, end: str | None = None) -> tuple[list[str], PointInTimeUniverse | None]:
    lines = _non_comment_lines(path)
    if not lines:
        return [], None

    first_cell = lines[0].split(",", 1)[0].strip().lower()
    if first_cell not in {column.lower() for column in SYMBOL_COLUMNS}:
        return _plain_symbols(lines), None

    reader = csv.DictReader(lines)
    fieldnames = set(reader.fieldnames or [])
    symbol_column = _symbol_column(fieldnames)
    if symbol_column is None:
        raise ValueError(f"{path} must contain a symbol/code column")

    rows = list(reader)
    has_metadata = bool({name.lower() for name in fieldnames} & METADATA_COLUMNS)
    if not has_metadata:
        return _unique_preserve_order(_normalize_symbol(row.get(symbol_column, "")) for row in rows), None

    members = tuple(_member_from_row(row, symbol_column) for row in rows if _normalize_symbol(row.get(symbol_column, "")))
    universe = PointInTimeUniverse(members)
    return universe.symbols_between(start, end), universe


def parse_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "nat"}:
        return None
    text = text.replace("/", "-")
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text[:10])


def infer_board(symbol: str) -> str:
    code = symbol.split(".", 1)[0]
    if code.startswith(("688", "689")):
        return "STAR"
    if code.startswith(("8", "4")):
        return "BSE"
    if code.startswith("3"):
        return "CHINEXT"
    if code.startswith("6"):
        return "SH_MAIN"
    return "SZ_MAIN"


def _member_from_row(row: dict[str, str], symbol_column: str) -> UniverseMember:
    symbol = _normalize_symbol(row.get(symbol_column, ""))
    valid_to = parse_date(_first_value(row, "valid_to", "universe_end_date", "removed_date"))
    st_intervals = _parse_st_intervals(row)
    return UniverseMember(
        symbol=symbol,
        name=_first_value(row, "name", "证券简称", "股票简称") or None,
        universe_date=parse_date(_first_value(row, "universe_date")),
        listed_date=parse_date(_first_value(row, "listed_date")),
        delisted_date=parse_date(_first_value(row, "delisted_date")),
        valid_from=parse_date(_first_value(row, "valid_from")),
        valid_to=valid_to,
        board=(_first_value(row, "board") or infer_board(symbol)),
        st_intervals=st_intervals,
    )


def _parse_st_intervals(row: dict[str, str]) -> tuple[tuple[date | None, date | None], ...]:
    intervals: list[tuple[date | None, date | None]] = []
    start = parse_date(_first_value(row, "st_start_date", "st_start"))
    end = parse_date(_first_value(row, "st_end_date", "st_end"))
    if start or end:
        intervals.append((start, end))

    raw_intervals = _first_value(row, "st_intervals")
    if raw_intervals:
        for part in raw_intervals.split(";"):
            text = part.strip()
            if not text:
                continue
            if ":" in text:
                raw_start, raw_end = text.split(":", 1)
            else:
                raw_start, raw_end = text, ""
            intervals.append((parse_date(raw_start), parse_date(raw_end)))
    return tuple(intervals)


def _plain_symbols(lines: Sequence[str]) -> list[str]:
    return _unique_preserve_order(_normalize_symbol(line.split(",", 1)[0]) for line in lines)


def _non_comment_lines(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig") as file:
        return [line for line in file if line.strip() and not line.lstrip().startswith("#")]


def _symbol_column(fieldnames: Iterable[str]) -> str | None:
    lowered = {name.lower(): name for name in fieldnames}
    for column in SYMBOL_COLUMNS:
        match = lowered.get(column.lower())
        if match:
            return match
    return None


def _first_value(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is None:
            lowered = {str(name).lower(): name for name in row}
            match = lowered.get(key.lower())
            value = row.get(match) if match else None
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalize_symbol(value: object) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        return ""
    if "." in symbol:
        return symbol
    code = symbol.zfill(6)
    suffix = "SH" if code.startswith("6") else "SZ"
    return f"{code}.{suffix}"


def _unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _latest_date(*values: date | None) -> date | None:
    dates = [value for value in values if value]
    return max(dates) if dates else None


def _earliest_date(*values: date | None) -> date | None:
    dates = [value for value in values if value]
    return min(dates) if dates else None
