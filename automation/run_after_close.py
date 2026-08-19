from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import shutil
from zoneinfo import ZoneInfo

import pandas as pd

from aqt.calendar import is_a_share_trading_day, parse_calendar_date
from aqt.config import load_config
from aqt.data import fetch_akshare_daily
from aqt.engine import run_paper
from aqt.health import fetch_stock_health, healthy_symbols, load_stock_health, write_health_report
from aqt.signals import generate_daily_signal


def main() -> None:
    parser = argparse.ArgumentParser(description="Run after-close stock selection automation")
    parser.add_argument("--config", default="configs/smallcap_best.yaml")
    parser.add_argument("--date", default=None, help="YYYYMMDD override for output folder")
    parser.add_argument("--market-end", default=None, help="YYYY-MM-DD data end override, defaults to today in Asia/Shanghai")
    parser.add_argument("--output-root", default="reports/daily")
    parser.add_argument("--adjust", default="qfq")
    parser.add_argument("--force", action="store_true", help="run even when the target date is not an A-share trading day")
    args = parser.parse_args()

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    target_date = parse_calendar_date(args.market_end or args.date, today)
    if not args.force and not is_a_share_trading_day(target_date):
        print(f"skip: {target_date.isoformat()} is not an A-share trading day")
        return

    config = _with_market_end(load_config(args.config), args.market_end)
    health = _refresh_health(config)
    healthy = healthy_symbols(config.data.symbols, health, config.health)
    if not healthy:
        output_date = args.date or target_date.strftime("%Y%m%d")
        output_dir = Path(args.output_root) / output_date
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_no_eligible_report(output_dir, args.config, target_date, health, config.health)
        print(output_dir)
        return

    config = replace(config, data=replace(config.data, symbols=healthy))
    fetch_akshare_daily(config.data.path, config.data.symbols, config.data.start, config.data.end, args.adjust)
    paper_paths = run_paper(config, cycles=None)

    signal_tmp = Path("runs/daily_signal")
    signal_paths = generate_daily_signal(config, signal_tmp)
    signal_date = _signal_date(signal_paths["summary"])
    output_date = args.date or signal_date.replace("-", "")
    output_dir = Path(args.output_root) / output_date
    output_dir.mkdir(parents=True, exist_ok=True)

    _copy(signal_paths["summary"], output_dir / "summary.txt")
    _copy(signal_paths["orders"], output_dir / "orders.csv")
    _copy(signal_paths["positions"], output_dir / "positions.csv")
    _copy(signal_paths["rankings"], output_dir / "rankings.csv")
    _copy(paper_paths["metrics"], output_dir / "metrics.csv")
    _copy(paper_paths["trades"], output_dir / "trades.csv")
    _copy(paper_paths["equity"], output_dir / "equity.csv")
    if not health.empty:
        write_health_report(health, config.health, output_dir / "health.csv")
    _write_manifest(output_dir, args.config, signal_date, "after_close")
    print(output_dir)


def _signal_date(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("signal_date:"):
            return line.split(":", 1)[1].strip()
    return datetime.now().strftime("%Y-%m-%d")


def _with_market_end(config, market_end: str | None):
    end = market_end or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    return replace(config, data=replace(config.data, end=end))


def _copy(src: Path, dst: Path) -> None:
    if src.exists():
        shutil.copy2(src, dst)


def _refresh_health(config):
    if not config.health.enabled:
        return load_stock_health(config.health.path)
    try:
        return fetch_stock_health(config.data.symbols, config.health.path)
    except Exception:
        return load_stock_health(config.health.path)


def _write_manifest(output_dir: Path, config_path: str, signal_date: str, job_type: str) -> None:
    lines = [
        f"job_type: {job_type}",
        f"config: {config_path}",
        f"signal_date: {signal_date}",
        f"generated_at: {datetime.now().isoformat(timespec='seconds')}",
    ]
    (output_dir / "manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_no_eligible_report(output_dir: Path, config_path: str, target_date, health: pd.DataFrame, health_config) -> None:
    lines = [
        f"signal_date: {target_date.isoformat()}",
        "status: no_eligible_symbols",
        "planned_orders: 0",
        "reason: health_data_missing_or_all_symbols_filtered",
        "",
        "order_plan:",
        "  HOLD",
    ]
    (output_dir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    pd.DataFrame().to_csv(output_dir / "orders.csv", index=False)
    pd.DataFrame().to_csv(output_dir / "positions.csv", index=False)
    pd.DataFrame().to_csv(output_dir / "rankings.csv", index=False)
    pd.DataFrame([{"status": "no_eligible_symbols"}]).to_csv(output_dir / "metrics.csv", index=False)
    if not health.empty:
        write_health_report(health, health_config, output_dir / "health.csv")
    _write_manifest(output_dir, config_path, target_date.isoformat(), "after_close_no_eligible_symbols")


if __name__ == "__main__":
    main()
