from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from aqt.config import load_config
from aqt.data import fetch_small_cap_universe, generate_sample_data
from aqt.models import Account, Bar, Position, Side
from aqt.strategy import SmallCapMomentumStrategy
from aqt.universe import read_symbols_file
from aqt.walk_forward import run_walk_forward


class ValidationTests(unittest.TestCase):
    def test_point_in_time_universe_filters_dates_and_st_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "universe.csv"
            pd.DataFrame(
                [
                    {
                        "symbol": "000001.SZ",
                        "universe_date": "2026-01-01",
                        "listed_date": "2020-01-01",
                        "delisted_date": "",
                        "valid_from": "2026-01-01",
                        "valid_to": "2026-01-03",
                        "st_start_date": "",
                        "st_end_date": "",
                    },
                    {
                        "symbol": "000002.SZ",
                        "universe_date": "2026-01-02",
                        "listed_date": "2020-01-01",
                        "delisted_date": "",
                        "valid_from": "2026-01-02",
                        "valid_to": "",
                        "st_start_date": "2026-01-03",
                        "st_end_date": "2026-01-04",
                    },
                    {
                        "symbol": "000003.SZ",
                        "universe_date": "2026-02-01",
                        "listed_date": "2020-01-01",
                        "delisted_date": "",
                        "valid_from": "2026-02-01",
                        "valid_to": "",
                        "st_start_date": "",
                        "st_end_date": "",
                    },
                ]
            ).to_csv(path, index=False)

            symbols, universe = read_symbols_file(path, "2026-01-01", "2026-01-05")

        self.assertEqual(symbols, ["000001.SZ", "000002.SZ"])
        self.assertIsNotNone(universe)
        assert universe is not None
        self.assertTrue(universe.is_symbol_eligible("000001.SZ", date(2026, 1, 3)))
        self.assertFalse(universe.is_symbol_eligible("000001.SZ", date(2026, 1, 4)))
        self.assertFalse(universe.is_symbol_eligible("000002.SZ", date(2026, 1, 3)))
        self.assertTrue(universe.is_symbol_eligible("000002.SZ", date(2026, 1, 5)))

    def test_fetch_smallcap_universe_can_write_point_in_time_snapshot(self) -> None:
        rows = [
            {"f12": "000001", "f14": "平安银行", "f20": 5_000_000_000, "f8": 2.0},
            {"f12": "688001", "f14": "科创样例", "f20": 5_000_000_000, "f8": 2.0},
        ]
        with tempfile.TemporaryDirectory() as tmp, patch("aqt.data._fetch_eastmoney_stock_list", return_value=rows):
            path = fetch_small_cap_universe(
                Path(tmp) / "universe.csv",
                limit=5,
                min_market_cap=1_000_000_000,
                max_market_cap=10_000_000_000,
                point_in_time=True,
                as_of="2026-08-24",
            )
            frame = pd.read_csv(path)

        self.assertEqual(frame["symbol"].tolist(), ["000001.SZ"])
        self.assertIn("universe_date", frame.columns)
        self.assertEqual(frame.iloc[0]["valid_from"], "2026-08-24")
        self.assertEqual(frame.iloc[0]["board"], "SZ_MAIN")

    def test_capital_recycle_exits_weak_recovered_position_and_rotates(self) -> None:
        base = load_config("configs/smallcap.yaml").strategy
        strategy_config = replace(
            base,
            momentum_window=3,
            rebalance_interval=100,
            top_n=1,
            target_gross_exposure=0.50,
            min_momentum=0.01,
            min_trade_value=100.0,
            support_window=2,
            target_window=3,
            trend_window=2,
            capital_recycle_enabled=True,
            capital_recycle_min_holding_days=2,
            capital_recycle_max_holding_days=5,
            capital_recycle_rank_multiplier=1,
            capital_recycle_min_momentum=0.01,
            capital_recycle_breakeven_buffer_pct=0.0,
        )
        strategy = SmallCapMomentumStrategy(strategy_config)
        account = Account(
            cash=90_000.0,
            positions={"BAD.SZ": Position("BAD.SZ", quantity=100, avg_cost=10.0, available=100, last_price=10.0)},
        )
        start = date(2026, 8, 20)
        bad_closes = [10.00, 10.01, 10.01, 10.02]
        good_closes = [10.00, 11.00, 12.00, 13.00]

        latest_orders = []
        for offset, (bad_close, good_close) in enumerate(zip(bad_closes, good_closes)):
            trading_day = start + timedelta(days=offset)
            account.positions["BAD.SZ"].last_price = bad_close
            latest_orders = strategy.on_bars(
                [
                    _bar("BAD.SZ", trading_day, bad_close, bad_closes[offset - 1] if offset else None),
                    _bar("GOOD.SZ", trading_day, good_close, good_closes[offset - 1] if offset else None),
                ],
                account,
            )

        self.assertEqual([(order.symbol, order.side, order.reason) for order in latest_orders[:2]], [
            ("BAD.SZ", Side.SELL, "capital_recycle_breakeven"),
            ("GOOD.SZ", Side.BUY, "rebalance_buy"),
        ])

    def test_walk_forward_writes_train_and_out_of_sample_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config("configs/demo.yaml")
            symbols = ["000001.SZ", "600519.SH"]
            raw_dir = root / "raw"
            generate_sample_data(raw_dir, symbols, "2021-01-01", "2021-04-30")
            config = replace(
                config,
                data=replace(config.data, path=raw_dir, symbols=symbols, start="2021-01-01", end="2021-04-30"),
                output=replace(config.output, backtest_dir=root / "backtest", paper_dir=root / "paper"),
            )

            paths = run_walk_forward(
                config,
                root / "walk_forward",
                "2021-01-01",
                "2021-03-15",
                "2021-03-16",
                "2021-04-30",
                [5],
                [5],
                [1],
            )
            summary = pd.read_csv(paths["summary"]).iloc[0]
            self.assertTrue(paths["leaderboard"].exists())
            self.assertTrue(paths["test_metrics"].exists())

        self.assertEqual(int(summary["momentum_window"]), 5)
        self.assertEqual(int(summary["top_n"]), 1)


def _bar(symbol: str, trading_day: date, close: float, previous_close: float | None) -> Bar:
    return Bar(symbol, trading_day, close, close * 1.01, close * 0.99, close, 1_000_000, previous_close)


if __name__ == "__main__":
    unittest.main()
