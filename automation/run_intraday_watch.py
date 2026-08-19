from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import shutil
from zoneinfo import ZoneInfo

import pandas as pd

from aqt.config import load_config
from aqt.data import fetch_akshare_daily
from aqt.engine import run_paper
from aqt.signals import generate_daily_signal


def main() -> None:
    parser = argparse.ArgumentParser(description="Run intraday watch automation snapshot")
    parser.add_argument("--config", default="configs/smallcap_best.yaml")
    parser.add_argument("--output-root", default="reports/intraday")
    parser.add_argument("--adjust", default="qfq")
    parser.add_argument("--date", default=None)
    parser.add_argument("--time", default=None)
    parser.add_argument("--market-end", default=None, help="YYYY-MM-DD data end override, defaults to today in Asia/Shanghai")
    args = parser.parse_args()

    now = datetime.now()
    output_date = args.date or now.strftime("%Y%m%d")
    output_time = args.time or now.strftime("%H%M")
    output_dir = Path(args.output_root) / output_date / output_time
    output_dir.mkdir(parents=True, exist_ok=True)

    config = _with_market_end(load_config(args.config), args.market_end)
    fetch_akshare_daily(config.data.path, config.data.symbols, config.data.start, config.data.end, args.adjust)
    paper_paths = run_paper(config, cycles=None)
    signal_paths = generate_daily_signal(config, Path("runs/daily_signal"))

    _copy(signal_paths["summary"], output_dir / "summary.txt")
    _copy(signal_paths["orders"], output_dir / "orders.csv")
    _copy(signal_paths["positions"], output_dir / "positions.csv")
    _copy(signal_paths["rankings"], output_dir / "rankings.csv")
    _copy(paper_paths["metrics"], output_dir / "metrics.csv")
    _write_watchlist(output_dir)
    _write_manifest(output_dir, args.config, output_date, output_time)
    print(output_dir)


def _copy(src: Path, dst: Path) -> None:
    if src.exists():
        shutil.copy2(src, dst)


def _with_market_end(config, market_end: str | None):
    end = market_end or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    return replace(config, data=replace(config.data, end=end))


def _write_watchlist(output_dir: Path) -> None:
    rankings = pd.read_csv(output_dir / "rankings.csv") if (output_dir / "rankings.csv").exists() else pd.DataFrame()
    positions = pd.read_csv(output_dir / "positions.csv") if (output_dir / "positions.csv").exists() else pd.DataFrame()
    watch_symbols = []
    if not rankings.empty:
        watch_symbols.extend(rankings.head(10)["symbol"].tolist())
    if not positions.empty:
        watch_symbols.extend(positions["symbol"].tolist())
    result = pd.DataFrame({"symbol": sorted(set(watch_symbols))})
    result.to_csv(output_dir / "watchlist.csv", index=False)


def _write_manifest(output_dir: Path, config_path: str, output_date: str, output_time: str) -> None:
    lines = [
        "job_type: intraday_watch",
        f"config: {config_path}",
        f"snapshot_date: {output_date}",
        f"snapshot_time: {output_time}",
        f"generated_at: {datetime.now().isoformat(timespec='seconds')}",
    ]
    (output_dir / "manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
