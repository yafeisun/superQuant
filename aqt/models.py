from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Dict, Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Bar:
    symbol: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Order:
    symbol: str
    side: Side
    quantity: int
    created_at: date
    reason: str = ""
    limit_price: Optional[float] = None


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: Side
    quantity: int
    price: float
    commission: float
    tax: float
    filled_at: date
    reason: str = ""


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    avg_cost: float = 0.0
    available: int = 0
    last_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price


@dataclass
class Account:
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    updated_at: Optional[datetime] = None

    def equity(self) -> float:
        return self.cash + sum(position.market_value for position in self.positions.values())
