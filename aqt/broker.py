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

            block_reason = self._trade_block_reason(normalized, bar)
            if block_reason:
                self._reject(normalized, block_reason)
                continue

            executable = self._volume_adjusted_order(normalized, bar)
            if executable is None:
                self._reject(normalized, "volume_limit")
                continue
            if executable.quantity < normalized.quantity:
                residual = Order(
                    normalized.symbol,
                    normalized.side,
                    normalized.quantity - executable.quantity,
                    normalized.created_at,
                    normalized.reason,
                    normalized.limit_price,
                )
                self._reject(residual, "partial_fill_volume_limit")

            fill = self._execute_order(executable, bar)
            if fill:
                fills.append(fill)
                self.fills.append(fill)
        return fills

    def _normalize_lot(self, order: Order) -> Order:
        lot = self.risk_config.lot_size
        qty = (order.quantity // lot) * lot
        return Order(order.symbol, order.side, qty, order.created_at, order.reason, order.limit_price)

    def _trade_block_reason(self, order: Order, bar: Bar) -> str | None:
        if order.limit_price is not None:
            if order.side == Side.BUY and order.limit_price < bar.low:
                return "buy_limit_not_reached"
            if order.side == Side.SELL and order.limit_price > bar.high:
                return "sell_limit_not_reached"

        change = self._bar_return(bar)
        if change is None:
            return None
        if (
            order.side == Side.BUY
            and self.risk_config.block_buy_limit_up
            and change >= self.risk_config.limit_move_pct
            and self._near(bar.close, bar.high)
        ):
            return "limit_up_no_buy"
        if (
            order.side == Side.SELL
            and self.risk_config.block_sell_limit_down
            and change <= -self.risk_config.limit_move_pct
            and self._near(bar.close, bar.low)
        ):
            return "limit_down_no_sell"
        return None

    def _volume_adjusted_order(self, order: Order, bar: Bar) -> Order | None:
        pct = self.risk_config.max_volume_participation_pct
        if pct <= 0:
            return order
        effective_volume = bar.volume * max(self.risk_config.volume_unit_multiplier, 0.0)
        if effective_volume <= 0:
            return None
        lot = self.risk_config.lot_size
        max_quantity = int(effective_volume * pct)
        max_quantity = (max_quantity // lot) * lot
        if max_quantity <= 0:
            return None
        if order.quantity <= max_quantity:
            return order
        if not self.risk_config.allow_partial_fills:
            return None
        return Order(order.symbol, order.side, max_quantity, order.created_at, order.reason, order.limit_price)

    def _execute_order(self, order: Order, bar: Bar) -> Fill | None:
        raw_price = order.limit_price if order.limit_price is not None else bar.close
        slip = self.account_config.slippage_bps / 10000.0
        price = self._fill_price(order, raw_price, slip, bar)
        gross_value = price * order.quantity
        commission = max(gross_value * self.account_config.commission_rate, self.account_config.min_commission)
        tax = gross_value * self.account_config.stamp_tax_rate if order.side == Side.SELL else 0.0

        position = self.account.positions.setdefault(order.symbol, Position(symbol=order.symbol))
        position.last_price = bar.close

        if order.side == Side.BUY:
            return self._buy(order, price, gross_value, commission, tax, position, bar)
        return self._sell(order, price, gross_value, commission, tax, position, bar)

    def _fill_price(self, order: Order, raw_price: float, slip: float, bar: Bar) -> float:
        if order.side == Side.BUY:
            price = raw_price * (1.0 + slip)
            price = min(price, bar.high) if bar.high > 0 else price
            if order.limit_price is not None:
                price = min(price, order.limit_price)
            return price
        price = raw_price * (1.0 - slip)
        price = max(price, bar.low) if bar.low > 0 else price
        if order.limit_price is not None:
            price = max(price, order.limit_price)
        return price

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

    @staticmethod
    def _bar_return(bar: Bar) -> float | None:
        if bar.previous_close is None or bar.previous_close <= 0:
            return None
        return bar.close / bar.previous_close - 1.0

    @staticmethod
    def _near(left: float, right: float) -> bool:
        scale = max(abs(left), abs(right), 1.0)
        return abs(left - right) <= scale * 1e-4
