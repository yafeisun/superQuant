from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque, Dict, Iterable, List

from .config import StrategyConfig
from .models import Account, Bar, Order, Side


class Strategy:
    def on_bars(self, bars: Iterable[Bar], account: Account) -> List[Order]:
        raise NotImplementedError


class MovingAverageCrossStrategy(Strategy):
    def __init__(self, config: StrategyConfig) -> None:
        if config.short_window >= config.long_window:
            raise ValueError("short_window must be smaller than long_window")
        self.short_window = config.short_window
        self.long_window = config.long_window
        self.target_position_pct = config.target_position_pct
        self.history: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=self.long_window))
        self.in_market: Dict[str, bool] = defaultdict(bool)

    def on_bars(self, bars: Iterable[Bar], account: Account) -> List[Order]:
        orders: List[Order] = []
        equity = account.equity()

        for bar in bars:
            closes = self.history[bar.symbol]
            closes.append(bar.close)
            if len(closes) < self.long_window:
                continue

            values = list(closes)
            short_ma = sum(values[-self.short_window :]) / self.short_window
            long_ma = sum(values) / self.long_window
            position = account.positions.get(bar.symbol)
            current_qty = position.quantity if position else 0
            target_value = equity * self.target_position_pct
            target_qty = int(target_value / bar.close)

            if short_ma > long_ma and not self.in_market[bar.symbol]:
                qty = max(target_qty - current_qty, 0)
                if qty > 0:
                    orders.append(Order(bar.symbol, Side.BUY, qty, bar.date, "ma_cross_up"))
                self.in_market[bar.symbol] = True
            elif short_ma < long_ma and self.in_market[bar.symbol]:
                if current_qty > 0:
                    orders.append(Order(bar.symbol, Side.SELL, current_qty, bar.date, "ma_cross_down"))
                self.in_market[bar.symbol] = False

        return orders


class SmallCapMomentumStrategy(Strategy):
    def __init__(self, config: StrategyConfig) -> None:
        if config.top_n <= 0:
            raise ValueError("top_n must be positive")
        if config.momentum_window <= 1:
            raise ValueError("momentum_window must be greater than 1")
        if config.rebalance_interval <= 0:
            raise ValueError("rebalance_interval must be positive")
        self.momentum_window = config.momentum_window
        self.rebalance_interval = config.rebalance_interval
        self.top_n = config.top_n
        self.target_gross_exposure = config.target_gross_exposure
        self.min_momentum = config.min_momentum
        self.min_trade_value = config.min_trade_value
        self.support_window = config.support_window
        self.target_window = config.target_window
        self.trend_window = config.trend_window
        self.risk_reward_ratio = config.risk_reward_ratio
        history_window = max(self.momentum_window, self.target_window, self.support_window, self.trend_window) + 1
        self.history: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=history_window))
        self.day_index = 0

    def on_bars(self, bars: Iterable[Bar], account: Account) -> List[Order]:
        bar_list = list(bars)
        for bar in bar_list:
            self.history[bar.symbol].append(bar.close)

        self.day_index += 1
        bars_by_symbol = {bar.symbol: bar for bar in bar_list}
        orders = self._risk_exit_orders(account, bars_by_symbol)
        exited_symbols = {order.symbol for order in orders}
        if self.day_index < self.momentum_window or self.day_index % self.rebalance_interval != 0:
            return orders

        ranked = sorted(self._scores(bars_by_symbol), key=lambda item: item[1], reverse=True)
        selected = [symbol for symbol, score in ranked if score >= self.min_momentum][: self.top_n]
        if not selected:
            return orders + self._sell_all(account, bars_by_symbol, "risk_off", exited_symbols)

        equity = account.equity()
        target_value_per_name = equity * self.target_gross_exposure / len(selected)

        for symbol, position in account.positions.items():
            if position.quantity > 0 and symbol not in selected and symbol in bars_by_symbol and symbol not in exited_symbols:
                orders.append(Order(symbol, Side.SELL, position.quantity, bars_by_symbol[symbol].date, "rebalance_sell"))

        for symbol in selected:
            if symbol in exited_symbols:
                continue
            bar = bars_by_symbol[symbol]
            position = account.positions.get(symbol)
            current_qty = position.quantity if position else 0
            current_value = current_qty * bar.close
            diff_value = target_value_per_name - current_value
            if abs(diff_value) < self.min_trade_value:
                continue
            quantity = int(abs(diff_value) / bar.close)
            side = Side.BUY if diff_value > 0 else Side.SELL
            reason = "rebalance_buy" if side == Side.BUY else "rebalance_trim"
            orders.append(Order(symbol, side, quantity, bar.date, reason))

        return orders

    def _risk_exit_orders(self, account: Account, bars_by_symbol: Dict[str, Bar]) -> List[Order]:
        orders: List[Order] = []
        for symbol, position in account.positions.items():
            if position.quantity <= 0 or position.avg_cost <= 0 or symbol not in bars_by_symbol:
                continue
            bar = bars_by_symbol[symbol]
            levels = self._technical_levels(symbol)
            if not levels:
                continue
            support_level, resistance_level, trend_slope = levels
            upside_target_level = max(
                resistance_level,
                position.avg_cost + max(position.avg_cost - support_level, 0.0) * self.risk_reward_ratio,
            )
            if bar.close < support_level and trend_slope < 0:
                orders.append(Order(symbol, Side.SELL, position.quantity, bar.date, "support_break_trend_down"))
            elif bar.close >= upside_target_level and trend_slope <= 0:
                orders.append(Order(symbol, Side.SELL, position.quantity, bar.date, "target_reached_trend_fade"))
        return orders

    def _technical_levels(self, symbol: str) -> tuple[float, float, float] | None:
        closes = list(self.history[symbol])
        min_required = max(self.support_window, self.trend_window) + 1
        if len(closes) < min_required:
            return None
        support_values = closes[-self.support_window - 1 : -1]
        target_values = closes[-min(self.target_window, len(closes) - 1) - 1 : -1]
        if not support_values or not target_values:
            return None
        support_level = min(support_values)
        resistance_level = max(target_values)
        trend_values = closes[-self.trend_window - 1 :]
        trend_slope = trend_values[-1] / trend_values[0] - 1.0 if trend_values[0] > 0 else 0.0
        return support_level, resistance_level, trend_slope

    def _scores(self, bars_by_symbol: Dict[str, Bar]) -> List[tuple[str, float]]:
        scores: List[tuple[str, float]] = []
        for symbol in bars_by_symbol:
            closes = self.history[symbol]
            if len(closes) < self.momentum_window + 1:
                continue
            values = list(closes)
            if values[0] <= 0:
                continue
            momentum = values[-1] / values[0] - 1.0
            scores.append((symbol, momentum))
        return scores

    def _sell_all(
        self,
        account: Account,
        bars_by_symbol: Dict[str, Bar],
        reason: str,
        excluded_symbols: set[str] | None = None,
    ) -> List[Order]:
        excluded_symbols = excluded_symbols or set()
        orders: List[Order] = []
        for symbol, position in account.positions.items():
            if position.quantity > 0 and symbol in bars_by_symbol and symbol not in excluded_symbols:
                orders.append(Order(symbol, Side.SELL, position.quantity, bars_by_symbol[symbol].date, reason))
        return orders


def build_strategy(config: StrategyConfig) -> Strategy:
    if config.name == "moving_average_cross":
        return MovingAverageCrossStrategy(config)
    if config.name == "small_cap_momentum":
        return SmallCapMomentumStrategy(config)
    raise ValueError(f"unknown strategy: {config.name}")
