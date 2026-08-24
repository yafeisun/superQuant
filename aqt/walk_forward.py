from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable

import pandas as pd

from .config import AppConfig, DataConfig
from .engine import run_backtest
from .optimize import optimize_small_cap_strategy


def run_walk_forward(
    config: AppConfig,
    output_dir: Path,
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
    momentum_windows: Iterable[int],
    rebalance_intervals: Iterable[int],
    top_ns: Iterable[int],
) -> dict[str, Path]:
    if pd.Timestamp(train_start) > pd.Timestamp(train_end):
        raise ValueError("train_start must be on or before train_end")
    if pd.Timestamp(test_start) > pd.Timestamp(test_end):
        raise ValueError("test_start must be on or before test_end")
    if pd.Timestamp(train_end) >= pd.Timestamp(test_start):
        raise ValueError("train_end must be before test_start")

    output_dir.mkdir(parents=True, exist_ok=True)
    train_dir = output_dir / "train"
    test_dir = output_dir / "out_of_sample"

    train_config = _with_window(config, train_start, train_end, train_dir / "backtests")
    leaderboard_path = optimize_small_cap_strategy(
        train_config,
        train_dir / "optimize",
        momentum_windows,
        rebalance_intervals,
        top_ns,
    )
    leaderboard = pd.read_csv(leaderboard_path)
    if leaderboard.empty:
        raise RuntimeError("walk-forward train optimization produced no rows")
    best = leaderboard.iloc[0].to_dict()

    frozen_strategy = replace(
        config.strategy,
        name="small_cap_momentum",
        momentum_window=int(best["momentum_window"]),
        rebalance_interval=int(best["rebalance_interval"]),
        top_n=int(best["top_n"]),
    )
    test_config = replace(
        _with_window(config, test_start, test_end, test_dir),
        strategy=frozen_strategy,
    )
    test_paths = run_backtest(test_config)

    train_metrics = _prefixed_metrics(best, "train")
    test_metrics = _prefixed_metrics(pd.read_csv(test_paths["metrics"]).iloc[0].to_dict(), "test")
    summary = {
        "train_start": train_start,
        "train_end": train_end,
        "test_start": test_start,
        "test_end": test_end,
        "momentum_window": int(best["momentum_window"]),
        "rebalance_interval": int(best["rebalance_interval"]),
        "top_n": int(best["top_n"]),
        "train_run_dir": best.get("run_dir", ""),
        "test_run_dir": str(test_dir),
        **train_metrics,
        **test_metrics,
    }

    summary_path = output_dir / "summary.csv"
    summary_md_path = output_dir / "summary.md"
    pd.DataFrame([summary]).to_csv(summary_path, index=False)
    _write_summary(summary_md_path, summary)
    return {
        "leaderboard": leaderboard_path,
        "test_metrics": test_paths["metrics"],
        "summary": summary_path,
        "summary_md": summary_md_path,
    }


def _with_window(config: AppConfig, start: str, end: str, backtest_dir: Path) -> AppConfig:
    data = replace(config.data, start=start, end=end)
    data = _with_universe_symbols(data, start, end)
    return replace(config, data=data, output=replace(config.output, backtest_dir=backtest_dir))


def _with_universe_symbols(data: DataConfig, start: str, end: str) -> DataConfig:
    if data.universe is None:
        return data
    return replace(data, symbols=data.universe.symbols_between(start, end))


def _prefixed_metrics(metrics: dict, prefix: str) -> dict:
    keys = [
        "total_return",
        "max_drawdown",
        "fills",
        "closed_trades",
        "win_rate",
        "total_cost",
        "rejections",
        "robust_score",
        "return_drawdown_ratio",
    ]
    return {f"{prefix}_{key}": metrics.get(key, "") for key in keys}


def _write_summary(path: Path, summary: dict) -> None:
    lines = [
        "# Walk-Forward Summary",
        "",
        f"Train: {summary['train_start']} to {summary['train_end']}",
        f"Out-of-sample: {summary['test_start']} to {summary['test_end']}",
        "",
        "Frozen parameters:",
        "",
        f"- momentum_window: {summary['momentum_window']}",
        f"- rebalance_interval: {summary['rebalance_interval']}",
        f"- top_n: {summary['top_n']}",
        "",
        "| Split | Return | Max Drawdown | Win Rate | Fills | Cost | Rejections |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        "| Train | {train_total_return} | {train_max_drawdown} | {train_win_rate} | {train_fills} | {train_total_cost} | {train_rejections} |".format(
            **summary
        ),
        "| Out-of-sample | {test_total_return} | {test_max_drawdown} | {test_win_rate} | {test_fills} | {test_total_cost} | {test_rejections} |".format(
            **summary
        ),
        "",
        "Backtest output is a validation artifact, not proof of future profitability.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
