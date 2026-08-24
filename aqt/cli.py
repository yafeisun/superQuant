from __future__ import annotations

import argparse
import os
from pathlib import Path

import pandas as pd

from .alerts import dispatch_status_alerts
from .config import load_config
from .data import fetch_akshare_daily, fetch_small_cap_universe, generate_sample_data
from .factors import load_external_factors, normalize_event_factors
from .flow import evaluate_money_flow, fetch_money_flow, load_money_flow
from .health import fetch_stock_health, load_stock_health
from .history import generate_account_activity_markdown, generate_history_report, update_readme_account_activity
from .engine import run_backtest, run_paper
from .live_dashboard import generate_live_dashboard
from .optimize import optimize_small_cap_strategy
from .selection import build_selection_candidates
from .signals import generate_daily_signal
from .stress import run_stress_test


DEFAULT_CONFIG = "configs/demo.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description="A-share quant system CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_data = subparsers.add_parser("init-data", help="generate deterministic sample A-share CSV data")
    init_data.add_argument("--config", default=DEFAULT_CONFIG)

    fetch_data = subparsers.add_parser("fetch-akshare", help="download A-share daily bars with AKShare")
    fetch_data.add_argument("--config", default=DEFAULT_CONFIG)
    fetch_data.add_argument("--adjust", default="qfq", choices=["", "qfq", "hfq"], help="price adjustment")

    money_flow = subparsers.add_parser("fetch-money-flow", help="download recent individual-stock money-flow factors")
    money_flow.add_argument("--config", default="configs/smallcap_live.yaml")
    money_flow.add_argument("--output", default=None)

    event_factors = subparsers.add_parser("ingest-event-factors", help="validate LLM/NLP event factors and write canonical CSV")
    event_factors.add_argument("--input", required=True, help="JSONL or CSV with event factor fields")
    event_factors.add_argument("--output", default="data/factors/events.csv")

    selection = subparsers.add_parser("selection-candidates", help="score every configured symbol with buy/wait/block reasons")
    selection.add_argument("--config", default="configs/smallcap_live.yaml")
    selection.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to latest local bar date")
    selection.add_argument("--output", default="reports/selection_candidates.csv")
    selection.add_argument("--refresh-health", action="store_true")
    selection.add_argument("--refresh-flow", action="store_true")

    universe = subparsers.add_parser("fetch-smallcap-universe", help="build a low market-cap A-share universe")
    universe.add_argument("--output", default="data/universe/smallcap_symbols.csv")
    universe.add_argument("--limit", type=int, default=30)
    universe.add_argument("--min-market-cap", type=float, default=2_000_000_000)
    universe.add_argument("--max-market-cap", type=float, default=12_000_000_000)

    backtest = subparsers.add_parser("backtest", help="run event-driven backtest")
    backtest.add_argument("--config", default=DEFAULT_CONFIG)

    stress = subparsers.add_parser("stress-test", help="run backtest stress scenarios and quality checks")
    stress.add_argument("--config", default="configs/smallcap.yaml")
    stress.add_argument("--output", default="runs/stress_test")

    paper = subparsers.add_parser("paper-run", help="run virtual live trading over historical bars")
    paper.add_argument("--config", default=DEFAULT_CONFIG)
    paper.add_argument("--cycles", type=int, default=None, help="number of trading days to replay")

    optimize = subparsers.add_parser("optimize-smallcap", help="grid-search small-cap momentum parameters")
    optimize.add_argument("--config", default="configs/smallcap.yaml")
    optimize.add_argument("--output", default="runs/optimize_smallcap")
    optimize.add_argument("--momentum-windows", default="40,60,90")
    optimize.add_argument("--rebalance-intervals", default="10,20,30")
    optimize.add_argument("--top-ns", default="5,8,10")

    report = subparsers.add_parser("report", help="print summary metrics from a run directory")
    report.add_argument("--run-dir", default="runs/smallcap_backtest")

    history = subparsers.add_parser("history-report", help="generate an HTML history report from a run directory")
    history.add_argument("--run-dir", default="runs/smallcap_best_paper")
    history.add_argument("--output", default=None)

    live_dashboard = subparsers.add_parser("live-dashboard", help="generate a local live-trading dashboard HTML")
    live_dashboard.add_argument("--state-dir", default="local_runs/paper_live")
    live_dashboard.add_argument("--output", default="reports/live_dashboard.html")
    live_dashboard.add_argument("--config", default="configs/smallcap_live.yaml", help="optional strategy config for daily bars and money-flow drilldown")

    status_alerts = subparsers.add_parser("status-alerts", help="dispatch warning/error run-status alerts")
    status_alerts.add_argument("--state-dir", default="local_runs/paper_live")
    status_alerts.add_argument("--min-severity", default="warning", choices=["info", "warning", "error", "critical"])
    status_alerts.add_argument("--webhook-url", default=None)
    status_alerts.add_argument("--webhook-env", default="AQT_ALERT_WEBHOOK_URL")
    status_alerts.add_argument("--dry-run", action="store_true")

    activity = subparsers.add_parser("account-activity", help="generate a Markdown account activity snapshot")
    activity.add_argument("--state-dir", default="local_runs/paper_live")
    activity.add_argument("--output", default="reports/account_activity.md")
    activity.add_argument("--update-readme", nargs="?", const="README.md", default=None)
    activity.add_argument("--max-rows", type=int, default=120)

    signal = subparsers.add_parser("daily-signal", help="generate after-close signal and next-day order plan")
    signal.add_argument("--config", default="configs/smallcap_best.yaml")
    signal.add_argument("--output", default="runs/daily_signal")

    args = parser.parse_args()

    try:
        if args.command == "fetch-smallcap-universe":
            path = fetch_small_cap_universe(
                Path(args.output),
                args.limit,
                args.min_market_cap,
                args.max_market_cap,
            )
            print(path)
            return
        if args.command == "report":
            _print_report(Path(args.run_dir))
            return
        if args.command == "ingest-event-factors":
            path = normalize_event_factors(Path(args.input), Path(args.output))
            print(path)
            return
        if args.command == "history-report":
            run_dir = Path(args.run_dir)
            output = Path(args.output) if args.output else Path("reports/history") / f"{run_dir.name}.html"
            path = generate_history_report(run_dir, output)
            print(path)
            return
        if args.command == "live-dashboard":
            path = generate_live_dashboard(Path(args.state_dir), Path(args.output), Path(args.config) if args.config else None)
            print(path)
            return
        if args.command == "status-alerts":
            webhook_url = args.webhook_url or os.environ.get(args.webhook_env)
            result = dispatch_status_alerts(Path(args.state_dir), args.min_severity, webhook_url, args.dry_run)
            for key, value in result.items():
                print(f"{key}: {value}")
            return
        if args.command == "account-activity":
            path = generate_account_activity_markdown(Path(args.state_dir), Path(args.output), args.max_rows)
            print(path)
            if args.update_readme:
                readme_path = update_readme_account_activity(Path(args.update_readme), path.read_text(encoding="utf-8"))
                print(readme_path)
            return
        if args.command == "fetch-money-flow":
            config = load_config(args.config)
            output = Path(args.output) if args.output else config.flow.path
            frame = fetch_money_flow(config.data.symbols, output, config.flow.lookback_days)
            if frame.empty:
                raise RuntimeError("money-flow refresh returned no rows; existing cache was not overwritten")
            evaluate_money_flow(frame, config.flow).to_csv(output, index=False)
            print(output)
            return
        if args.command == "selection-candidates":
            config = load_config(args.config)
            path = _write_selection_candidates(config, args.date, Path(args.output), args.refresh_health, args.refresh_flow)
            print(path)
            return

        config = load_config(args.config)
        if args.command == "init-data":
            paths = generate_sample_data(config.data.path, config.data.symbols, config.data.start, config.data.end)
            for path in paths:
                print(path)
        elif args.command == "fetch-akshare":
            paths = fetch_akshare_daily(
                config.data.path,
                config.data.symbols,
                config.data.start,
                config.data.end,
                args.adjust,
            )
            for path in paths:
                print(path)
        elif args.command == "backtest":
            paths = run_backtest(config)
            _print_paths(paths)
        elif args.command == "stress-test":
            paths = run_stress_test(config, Path(args.output))
            _print_paths(paths)
        elif args.command == "paper-run":
            paths = run_paper(config, args.cycles)
            _print_paths(paths)
        elif args.command == "optimize-smallcap":
            path = optimize_small_cap_strategy(
                config,
                Path(args.output),
                _parse_ints(args.momentum_windows),
                _parse_ints(args.rebalance_intervals),
                _parse_ints(args.top_ns),
            )
            print(path)
        elif args.command == "daily-signal":
            paths = generate_daily_signal(config, Path(args.output))
            _print_paths(paths)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        parser.exit(status=1, message=f"error: {exc}\n")


def _print_paths(paths: dict[str, Path]) -> None:
    for name, path in paths.items():
        print(f"{name}: {path}")


def _parse_ints(value: str) -> list[int]:
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def _write_selection_candidates(config, trading_date: str | None, output: Path, refresh_health: bool, refresh_flow: bool) -> Path:
    from .data import load_market_data

    market_data = load_market_data(config.data.path, config.data.symbols, config.data.start, config.data.end, strict=False)
    if trading_date:
        target_date = pd.to_datetime(trading_date).date()
    else:
        latest = max(frame["date"].max() for frame in market_data.values() if not frame.empty)
        target_date = pd.to_datetime(latest).date()
    health = fetch_stock_health(config.data.symbols, config.health.path) if refresh_health else load_stock_health(config.health.path)
    flow = (
        fetch_money_flow(config.data.symbols, config.flow.path, config.flow.lookback_days)
        if refresh_flow
        else load_money_flow(config.flow.path)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    if refresh_health and health.empty:
        health = load_stock_health(config.health.path)
    if refresh_flow and flow.empty:
        flow = load_money_flow(config.flow.path)
    factors = load_external_factors(config.factors, config.data.symbols, target_date)
    build_selection_candidates(config, market_data, target_date, health, flow, factors).to_csv(output, index=False)
    return output


def _print_report(run_dir: Path) -> None:
    metrics_path = run_dir / "metrics.csv"
    if not metrics_path.exists():
        raise FileNotFoundError(f"missing metrics file: {metrics_path}")

    import pandas as pd

    metrics = pd.read_csv(metrics_path).iloc[0].to_dict()
    for key in [
        "start_date",
        "end_date",
        "start_equity",
        "end_equity",
        "total_return",
        "max_drawdown",
        "fills",
        "closed_trades",
        "winning_trades",
        "win_rate",
        "total_cost",
        "rejections",
    ]:
        value = metrics.get(key)
        if key in {"total_return", "max_drawdown", "win_rate"}:
            print(f"{key}: {float(value):.2%}")
        else:
            print(f"{key}: {value}")

    positions_path = run_dir / "positions.csv"
    if positions_path.exists():
        positions = pd.read_csv(positions_path)
        if not positions.empty:
            last_date = positions["date"].max()
            latest = positions[(positions["date"] == last_date) & (positions["quantity"] > 0)]
            if not latest.empty:
                print("\ncurrent_positions:")
                print(latest[["date", "symbol", "quantity", "avg_cost", "last_price", "market_value"]].to_string(index=False))

    trades_path = run_dir / "trades.csv"
    if trades_path.exists():
        trades = pd.read_csv(trades_path)
        if not trades.empty:
            print("\nrecent_closed_trades:")
            print(trades.tail(8).to_string(index=False))


if __name__ == "__main__":
    main()
