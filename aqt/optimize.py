from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Dict, Iterable, List

import pandas as pd

from .config import AppConfig
from .engine import run_backtest


def optimize_small_cap_strategy(
    config: AppConfig,
    output_dir: Path,
    momentum_windows: Iterable[int],
    rebalance_intervals: Iterable[int],
    top_ns: Iterable[int],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, object]] = []

    for momentum_window in momentum_windows:
        for rebalance_interval in rebalance_intervals:
            for top_n in top_ns:
                strategy = replace(
                    config.strategy,
                    name="small_cap_momentum",
                    momentum_window=momentum_window,
                    rebalance_interval=rebalance_interval,
                    top_n=top_n,
                )
                run_dir = output_dir / f"mw{momentum_window}_rb{rebalance_interval}_top{top_n}"
                trial_config = replace(config, strategy=strategy, output=replace(config.output, backtest_dir=run_dir))
                paths = run_backtest(trial_config)
                metrics = pd.read_csv(paths["metrics"]).iloc[0].to_dict()
                metrics.update(
                    {
                        "momentum_window": momentum_window,
                        "rebalance_interval": rebalance_interval,
                        "top_n": top_n,
                        "run_dir": str(run_dir),
                    }
                )
                start_equity = float(metrics.get("start_equity", 0.0)) or 1.0
                total_return = float(metrics.get("total_return", 0.0))
                max_drawdown = abs(float(metrics.get("max_drawdown", 0.0)))
                cost_pct = float(metrics.get("total_cost", 0.0)) / start_equity
                metrics["cost_pct"] = round(cost_pct, 6)
                metrics["return_drawdown_ratio"] = round(total_return / max_drawdown, 6) if max_drawdown else 0.0
                metrics["robust_score"] = round(total_return - 0.35 * max_drawdown - 1.5 * cost_pct, 6)
                rows.append(metrics)

    result = pd.DataFrame(rows)
    result = result.sort_values(
        by=["robust_score", "total_return", "max_drawdown", "total_cost"],
        ascending=[False, False, False, True],
    )
    output_path = output_dir / "leaderboard.csv"
    result.to_csv(output_path, index=False)
    return output_path
