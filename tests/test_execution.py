from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date

from aqt.broker import PaperBroker
from aqt.config import load_config
from aqt.models import Account, Bar, Order, Position, Side


class ExecutionModelTests(unittest.TestCase):
    def test_volume_cap_partially_fills_and_rejects_residual(self) -> None:
        config = load_config("configs/demo.yaml")
        risk = replace(
            config.risk,
            allow_partial_fills=True,
            max_volume_participation_pct=0.10,
            volume_unit_multiplier=1.0,
        )
        broker = PaperBroker(config.account, risk)
        bar = Bar("000001.SZ", date(2026, 8, 24), 10.0, 10.5, 9.8, 10.0, 2500, 9.9)
        fills = broker.execute_orders([Order("000001.SZ", Side.BUY, 1000, bar.date, "test")], {"000001.SZ": bar})

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].quantity, 200)
        self.assertEqual(broker.rejected_orders[0]["quantity"], 800)
        self.assertEqual(broker.rejected_orders[0]["reason"], "partial_fill_volume_limit")

    def test_volume_cap_rejects_when_partial_disabled(self) -> None:
        config = load_config("configs/demo.yaml")
        risk = replace(
            config.risk,
            allow_partial_fills=False,
            max_volume_participation_pct=0.10,
            volume_unit_multiplier=1.0,
        )
        broker = PaperBroker(config.account, risk)
        bar = Bar("000001.SZ", date(2026, 8, 24), 10.0, 10.5, 9.8, 10.0, 2500, 9.9)
        fills = broker.execute_orders([Order("000001.SZ", Side.BUY, 1000, bar.date, "test")], {"000001.SZ": bar})

        self.assertEqual(fills, [])
        self.assertEqual(broker.rejected_orders[0]["reason"], "volume_limit")

    def test_limit_order_must_touch_bar_range(self) -> None:
        config = load_config("configs/demo.yaml")
        broker = PaperBroker(config.account, config.risk)
        bar = Bar("000001.SZ", date(2026, 8, 24), 10.0, 10.5, 9.8, 10.0, 10_000, 9.9)

        fills = broker.execute_orders([Order("000001.SZ", Side.BUY, 100, bar.date, "test", limit_price=9.7)], {"000001.SZ": bar})
        self.assertEqual(fills, [])
        self.assertEqual(broker.rejected_orders[0]["reason"], "buy_limit_not_reached")

    def test_limit_up_buy_and_limit_down_sell_are_blocked(self) -> None:
        config = load_config("configs/demo.yaml")
        broker = PaperBroker(config.account, config.risk)
        limit_up = Bar("000001.SZ", date(2026, 8, 24), 10.98, 10.98, 10.98, 10.98, 10_000, 10.0)

        fills = broker.execute_orders([Order("000001.SZ", Side.BUY, 100, limit_up.date, "test")], {"000001.SZ": limit_up})
        self.assertEqual(fills, [])
        self.assertEqual(broker.rejected_orders[0]["reason"], "limit_up_no_buy")

        broker = PaperBroker(config.account, config.risk)
        broker.account = Account(
            cash=0.0,
            positions={"000001.SZ": Position("000001.SZ", quantity=100, avg_cost=10.0, available=100, last_price=9.02)},
        )
        limit_down = Bar("000001.SZ", date(2026, 8, 25), 9.02, 9.02, 9.02, 9.02, 10_000, 10.0)
        fills = broker.execute_orders([Order("000001.SZ", Side.SELL, 100, limit_down.date, "test")], {"000001.SZ": limit_down})

        self.assertEqual(fills, [])
        self.assertEqual(broker.rejected_orders[0]["reason"], "limit_down_no_sell")


if __name__ == "__main__":
    unittest.main()
