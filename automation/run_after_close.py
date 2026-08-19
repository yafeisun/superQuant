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
    parser = argparse.ArgumentParser(description="Run after-close stock selection automation")
    parser.add_argument("--config", default="configs/smallcap_best.yaml")
    parser.add_argument("--date", default=None, help="YYYYMMDD override for output folder")
    parser.add_argument("--market-end", default=None, help="YYYY-MM-DD data end override, defaults to today in Asia/Shanghai")
    parser.add_argument("--output-root", default="reports/daily")
    parser.add_argument("--adjust", default="qfq")
    args = parser.parse_args()

    config = _with_market_end(load_config(args.config), args.market_end)
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


def _write_manifest(output_dir: Path, config_path: str, signal_date: str, job_type: str) -> None:
    lines = [
        f"job_type: {job_type}",
        f"config: {config_path}",
        f"signal_date: {signal_date}",
        f"generated_at: {datetime.now().isoformat(timespec='seconds')}",
    ]
    (output_dir / "manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
