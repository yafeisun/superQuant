from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List

from .config import AccountConfig, RiskConfig
from .models import Account, Bar, Fill, Order, Position, Side


class PaperBroker:
    def __init__(self, account_config: AccountConfig, risk_config: RiskConfig) -> None:
        self.account = Account(cash=account_config.initial_cash)
        self.account_config = account_config
        self.risk_config = risk_config
        self.fills: List[Fill] = []
        self.rejected_orders: List[dict] = []

    def mark_to_market(self, bars: Iterable[Bar]) -> None:
        for bar in bars:
            position = self.account.positions.get(bar.symbol)
            if position:
                position.last_price = bar.close
        self.account.updated_at = datetime.now()

    def settle_day(self) -> None:
        if not self.risk_config.enforce_t1:
            return
        for position in self.account.positions.values():
            position.available = position.quantity

    def execute_orders(self, orders: Iterable[Order], bars_by_symbol: Dict[str, Bar]) -> List[Fill]:
        fills: List[Fill] = []
        for order in orders:
            bar = bars_by_symbol.get(order.symbol)
            if not bar:
                self._reject(order, "no_bar")
                continue

            normalized = self._normalize_lot(order)
            if normalized.quantity <= 0:
                self._reject(order, "below_lot_size")
                continue

            fill = self._execute_order(normalized, bar)
            if fill:
                fills.append(fill)
                self.fills.append(fill)
        return fills

    def _normalize_lot(self, order: Order) -> Order:
        lot = self.risk_config.lot_size
        qty = (order.quantity // lot) * lot
        return Order(order.symbol, order.side, qty, order.created_at, order.reason, order.limit_price)

    def _execute_order(self, order: Order, bar: Bar) -> Fill | None:
        raw_price = order.limit_price if order.limit_price is not None else bar.close
        slip = self.account_config.slippage_bps / 10000.0
        price = raw_price * (1.0 + slip if order.side == Side.BUY else 1.0 - slip)
        gross_value = price * order.quantity
        commission = max(gross_value * self.account_config.commission_rate, self.account_config.min_commission)
        tax = gross_value * self.account_config.stamp_tax_rate if order.side == Side.SELL else 0.0

        position = self.account.positions.setdefault(order.symbol, Position(symbol=order.symbol))
        position.last_price = bar.close

        if order.side == Side.BUY:
            return self._buy(order, price, gross_value, commission, tax, position, bar)
        return self._sell(order, price, gross_value, commission, tax, position, bar)

    def _buy(
        self,
        order: Order,
        price: float,
        gross_value: float,
        commission: float,
        tax: float,
        position: Position,
        bar: Bar,
    ) -> Fill | None:
        equity = self.account.equity()
        max_order_value = equity * self.risk_config.max_order_value_pct
        max_position_value = equity * self.risk_config.max_position_pct
        if gross_value > max_order_value:
            self._reject(order, "order_value_limit")
            return None
        if position.market_value + gross_value > max_position_value:
            self._reject(order, "position_limit")
            return None
        if self.account.cash < gross_value + commission:
            self._reject(order, "cash_not_enough")
            return None

        new_qty = position.quantity + order.quantity
        position.avg_cost = (
            (position.avg_cost * position.quantity + gross_value) / new_qty if new_qty else 0.0
        )
        position.quantity = new_qty
        if not self.risk_config.enforce_t1:
            position.available += order.quantity
        self.account.cash -= gross_value + commission
        return Fill(order.symbol, order.side, order.quantity, price, commission, tax, bar.date, order.reason)

    def _sell(
        self,
        order: Order,
        price: float,
        gross_value: float,
        commission: float,
        tax: float,
        position: Position,
        bar: Bar,
    ) -> Fill | None:
        available = position.available if self.risk_config.enforce_t1 else position.quantity
        if order.quantity > available:
            self._reject(order, "shares_not_available")
            return None

        position.quantity -= order.quantity
        position.available = max(position.available - order.quantity, 0)
        self.account.cash += gross_value - commission - tax
        self.account.realized_pnl += (price - position.avg_cost) * order.quantity - commission - tax
        if position.quantity == 0:
            position.avg_cost = 0.0
        return Fill(order.symbol, order.side, order.quantity, price, commission, tax, bar.date, order.reason)

    def _reject(self, order: Order, reason: str) -> None:
        self.rejected_orders.append(
            {
                "date": order.created_at.isoformat(),
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": order.quantity,
                "reason": reason,
                "strategy_reason": order.reason,
            }
        )
