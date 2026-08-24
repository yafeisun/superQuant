from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from aqt.config import load_config
from aqt.alerts import dispatch_status_alerts
from aqt.health import _fetch_st_symbols, _fetch_suspended_symbols, evaluate_health
from aqt.flow import fetch_money_flow
from aqt.live_rules import screen_buy_orders
from aqt.live_dashboard import generate_live_dashboard
from aqt.models import Bar, Order, Side
from aqt.quotes import fetch_realtime_quotes
from aqt.run_status import write_run_status
from aqt.selection import evaluate_selection_candidate
from automation.run_local_paper_live import _find_latest_bars


class ResilienceTests(unittest.TestCase):
    def test_selection_and_screening_allow_missing_flow(self) -> None:
        config = load_config("configs/smallcap_live.yaml")
        symbol = "000001.SZ"
        config = replace(
            config,
            data=replace(config.data, symbols=[symbol]),
        )
        market_data = {symbol: _make_market_frame()}
        health = pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "name": "Test",
                    "generated_at": "2026-08-24T09:30:00",
                    "latest": 10.6,
                    "pct_chg": 0.5,
                    "turnover_rate": 3.0,
                    "pe": 20.0,
                    "amount": 50_000_000.0,
                    "total_market_cap": 4_000_000_000.0,
                    "float_market_cap": 3_000_000_000.0,
                    "is_st": False,
                    "is_suspended": False,
                    "is_limit_up": False,
                    "is_limit_down": False,
                }
            ]
        )
        trading_day = market_data[symbol]["date"].dt.date.iloc[-1]

        evaluated_health = evaluate_health(health, config.health)
        candidate = evaluate_selection_candidate(
            config, market_data, symbol, trading_day, evaluated_health, pd.DataFrame(), pd.DataFrame()
        )
        self.assertEqual(candidate["buy_decision"], "BUY_READY")
        self.assertNotIn("money_flow_missing", str(candidate["buy_reason"]))

        order = Order(symbol, Side.BUY, 100, trading_day, "test_buy")
        bars_by_symbol = {symbol: _make_bar(symbol, trading_day)}
        accepted, skipped = screen_buy_orders(
            [order],
            health,
            pd.DataFrame(),
            config,
            market_data,
            trading_day,
            bars_by_symbol,
            pd.DataFrame(),
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(skipped), 0)

    def test_quote_cache_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "quotes.csv"
            pd.DataFrame(
                [
                    {
                        "symbol": "000001.SZ",
                        "price": 10.5,
                        "open": 10.2,
                        "high": 10.8,
                        "low": 10.0,
                        "volume": 123456.0,
                        "updated_at": "2026-08-24T09:30:00",
                    }
                ]
            ).to_csv(cache_path, index=False)
            with patch("aqt.quotes._fetch_realtime_quotes_direct", side_effect=RuntimeError("boom")), patch(
                "aqt.quotes._fetch_realtime_quotes_akshare", side_effect=RuntimeError("boom")
            ):
                quotes = fetch_realtime_quotes(["000001.SZ"], cache_path)
            self.assertIn("000001.SZ", quotes)
            self.assertAlmostEqual(quotes["000001.SZ"].price, 10.5)
            self.assertTrue(cache_path.exists())

    def test_money_flow_cache_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "latest.csv"
            pd.DataFrame(
                [
                    {
                        "symbol": "000001.SZ",
                        "generated_at": "2026-08-24T09:30:00",
                        "latest_date": "2026-08-23",
                        "lookback_days": 5,
                        "positive_main_flow_days": 3,
                        "main_net_inflow_sum": 123456.0,
                        "main_net_inflow_ratio_avg": 1.23,
                        "latest_main_net_inflow": 4567.0,
                        "latest_main_net_inflow_ratio": 0.12,
                    }
                ]
            ).to_csv(cache_path, index=False)
            with patch("aqt.flow._fetch_individual_money_flow", side_effect=RuntimeError("boom")):
                flow = fetch_money_flow(["000001.SZ"], cache_path, 5)
            self.assertFalse(flow.empty)
            self.assertEqual(flow.iloc[0]["symbol"], "000001.SZ")
            self.assertTrue((Path(tmp) / "latest.status.json").exists())

    def test_run_status_and_live_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "account.json").write_text(
                '{"cash": 90000, "equity": 101000, "positions": {"000001.SZ": {"quantity": 100, "last_price": 110}}}',
                encoding="utf-8",
            )
            pd.DataFrame(
                [{"date": "2026-08-24", "cash": 90000, "market_value": 11000, "equity": 101000, "return_pct": 0.01}]
            ).to_csv(state_dir / "equity.csv", index=False)
            pd.DataFrame(
                [
                    {
                        "date": "2026-08-24",
                        "symbol": "000001.SZ",
                        "quantity": 100,
                        "available_to_sell": 100,
                        "avg_cost": 100.0,
                        "last_price": 110.0,
                        "market_value": 11000.0,
                        "unrealized_pnl": 1000.0,
                        "return_pct": 0.1,
                    }
                ]
            ).to_csv(state_dir / "positions.csv", index=False)
            decision_dir = state_dir / "decisions" / "20260824"
            decision_dir.mkdir(parents=True)
            (decision_dir / "summary.txt").write_text("date: 2026-08-24\nstatus: executed\n", encoding="utf-8")
            pd.DataFrame([{"symbol": "000001.SZ", "today_action": "HOLD", "next_advice": "hold"}]).to_csv(
                decision_dir / "position_advice.csv", index=False
            )
            data_dir = state_dir / "data" / "raw"
            data_dir.mkdir(parents=True)
            _make_market_frame().to_csv(data_dir / "000001_SZ.csv", index=False)
            flow_dir = state_dir / "data" / "flow"
            flow_dir.mkdir(parents=True)
            pd.DataFrame(
                [
                    {
                        "symbol": "000001.SZ",
                        "generated_at": "2026-08-24T09:30:00",
                        "latest_date": "2026-08-24",
                        "lookback_days": 5,
                        "positive_main_flow_days": 3,
                        "main_net_inflow_sum": 123456.0,
                        "main_net_inflow_ratio_avg": 1.23,
                        "latest_main_net_inflow": 4567.0,
                        "latest_main_net_inflow_ratio": 0.12,
                    }
                ]
            ).to_csv(flow_dir / "latest.csv", index=False)
            config_path = state_dir / "config.yaml"
            config_path.write_text(
                f"""
data:
  path: {data_dir}
  symbols: ["000001.SZ"]
  start: "2026-04-01"
  end: "2026-08-24"
account:
  initial_cash: 100000
  commission_rate: 0.0003
  min_commission: 5
  stamp_tax_rate: 0.001
  slippage_bps: 3
risk:
  max_position_pct: 0.22
  max_order_value_pct: 0.20
  lot_size: 100
  enforce_t1: true
health:
  enabled: false
flow:
  enabled: true
  path: {flow_dir / "latest.csv"}
  lookback_days: 5
factors:
  enabled: false
strategy:
  name: small_cap_momentum
  momentum_window: 40
  rebalance_interval: 20
  top_n: 5
  target_gross_exposure: 0.85
  min_momentum: 0
  min_trade_value: 3000
output:
  backtest_dir: {state_dir / "runs" / "backtest"}
  paper_dir: {state_dir / "runs" / "paper"}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            write_run_status(state_dir, "paper_live", "executed", date(2026, 8, 24), "info", "done")

            dashboard_path = generate_live_dashboard(state_dir, state_dir / "dashboard.html", config_path)
            html = dashboard_path.read_text(encoding="utf-8")
            self.assertIn("Live Trading Dashboard", html)
            self.assertIn("Symbol Drilldown", html)
            self.assertIn("daily candlesticks", html)
            self.assertIn("Positive Days", html)
            self.assertIn("000001.SZ", html)
            self.assertTrue((state_dir / "status" / "latest_paper_live.json").exists())
            self.assertTrue((state_dir / "status" / "events.jsonl").exists())

    def test_status_alert_dispatch_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            write_run_status(state_dir, "paper_live", "ok", date(2026, 8, 24), "info", "normal")
            write_run_status(state_dir, "intraday_paper", "quote_fetch_failed", date(2026, 8, 24), "error", "quotes failed")

            first = dispatch_status_alerts(state_dir, dry_run=True)
            self.assertEqual(first["candidate_count"], 1)
            self.assertEqual(first["delivered_count"], 1)
            second = dispatch_status_alerts(state_dir, dry_run=True)
            self.assertEqual(second["candidate_count"], 0)
            alerts = (state_dir / "status" / "alerts.jsonl").read_text(encoding="utf-8")
            self.assertIn("quote_fetch_failed", alerts)
            self.assertNotIn("normal", alerts)

    def test_health_sources_extract_symbols(self) -> None:
        with patch(
            "akshare.stock_feature.stock_tfp_em.stock_tfp_em",
            return_value=pd.DataFrame({"证券代码": ["000001", "600000"]}),
        ), patch(
            "akshare.stock.stock_stop.stock_staq_net_stop",
            return_value=pd.DataFrame({"股票代码": ["000002"]}),
        ), patch(
            "akshare.stock.stock_zh_a_special.stock_zh_a_stop_em",
            return_value=pd.DataFrame({"代码": ["000003"]}),
        ):
            suspended = _fetch_suspended_symbols()
        self.assertTrue({"000001.SZ", "000002.SZ", "000003.SZ", "600000.SH"}.issubset(suspended))

        with patch(
            "akshare.stock.stock_zh_a_special.stock_zh_a_st_em",
            return_value=pd.DataFrame({"证券代码": ["000004", "600001"]}),
        ):
            st_symbols = _fetch_st_symbols()
        self.assertTrue({"000004.SZ", "600001.SH"}.issubset(st_symbols))

    def test_latest_bars_fallback_uses_previous_available_day(self) -> None:
        symbol = "000001.SZ"
        bars_1 = [_make_bar(symbol, date(2026, 8, 18))]
        bars_2 = [_make_bar(symbol, date(2026, 8, 20))]
        all_bars = [bars_1, bars_2]

        latest = _find_latest_bars(all_bars, date(2026, 8, 19))
        self.assertEqual(latest[0].date, date(2026, 8, 18))
        exact = _find_latest_bars(all_bars, date(2026, 8, 20))
        self.assertEqual(exact[0].date, date(2026, 8, 20))


def _make_market_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2026-04-01", periods=70)
    closes: list[float] = []
    for idx in range(len(dates)):
        if idx < 30:
            closes.append(10.0)
        elif idx < 60:
            closes.append(10.0 + (idx - 29) * 0.02)
        else:
            closes.append(10.6 + (idx - 60) * 0.004)
    highs = []
    lows = []
    for idx, close in enumerate(closes):
        highs.append(15.0 if idx == 12 else close + 0.2)
        lows.append(close - 0.15)
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [2_000_000] * len(dates),
        }
    )


def _make_bar(symbol: str, trading_day: date) -> Bar:
    return Bar(
        symbol=symbol,
        date=trading_day,
        open=10.0,
        high=10.2,
        low=9.9,
        close=10.1,
        volume=2_000_000,
    )


if __name__ == "__main__":
    unittest.main()
