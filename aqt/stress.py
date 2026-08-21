from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Dict

import pandas as pd

from .config import AppConfig
from .data import load_market_data
from .engine import run_backtest_on_market_data


def run_stress_test(config: AppConfig, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    market_data = load_market_data(config.data.path, config.data.symbols, config.data.start, config.data.end, strict=False)
    if not market_data:
        raise RuntimeError("stress test has no market data to run")

    missing_symbols = sorted(set(config.data.symbols) - set(market_data))
    shock_date = _shock_date(market_data)
    scenarios = [
        ("baseline", config, market_data, "configured costs and prices"),
        (
            "slippage_15bps",
            replace(config, account=replace(config.account, slippage_bps=max(config.account.slippage_bps, 15.0))),
            market_data,
            "raise slippage to at least 15 bps",
        ),
        (
            "costs_2x",
            replace(
                config,
                account=replace(
                    config.account,
                    commission_rate=config.account.commission_rate * 2.0,
                    stamp_tax_rate=config.account.stamp_tax_rate * 2.0,
                    slippage_bps=max(config.account.slippage_bps, 8.0),
                ),
            ),
            market_data,
            "double commission and stamp tax, raise slippage floor to 8 bps",
        ),
        (
            "market_shock_5pct",
            config,
            _shock_market_data(market_data, shock_date, -0.05),
            f"apply -5% market-wide price shock from {shock_date}",
        ),
        (
            "market_shock_10pct",
            config,
            _shock_market_data(market_data, shock_date, -0.10),
            f"apply -10% market-wide price shock from {shock_date}",
        ),
    ]

    scenario_rows = []
    for name, scenario_config, scenario_market_data, description in scenarios:
        scenario_dir = output_dir / name
        paths = run_backtest_on_market_data(scenario_config, scenario_dir, scenario_market_data)
        metrics = _read_metrics(paths["metrics"])
        scenario_rows.append(
            {
                "scenario": name,
                "description": description,
                "symbols_used": len(scenario_market_data),
                "missing_symbols": ";".join(missing_symbols),
                **metrics,
            }
        )

    quality_rows = _quality_checks(config, market_data, missing_symbols, len(scenarios))
    scenario_path = output_dir / "scenario_metrics.csv"
    quality_path = output_dir / "quality_checks.csv"
    summary_path = output_dir / "summary.md"
    pd.DataFrame(scenario_rows).to_csv(scenario_path, index=False)
    pd.DataFrame(quality_rows).to_csv(quality_path, index=False)
    _write_summary(summary_path, scenario_rows, quality_rows)
    return {"scenario_metrics": scenario_path, "quality_checks": quality_path, "summary": summary_path}


def _read_metrics(path: Path) -> dict:
    if not path.exists():
        return {"status": "missing_metrics"}
    frame = pd.read_csv(path)
    if frame.empty:
        return {"status": "empty_metrics"}
    return frame.iloc[0].to_dict()


def _quality_checks(
    config: AppConfig,
    market_data: Dict[str, pd.DataFrame],
    missing_symbols: list[str],
    scenario_count: int,
) -> list[dict]:
    rows = []
    rows.append(
        _check(
            "transaction_costs",
            "PASS" if config.account.commission_rate > 0 and config.account.stamp_tax_rate > 0 else "FAIL",
            f"commission_rate={config.account.commission_rate}, stamp_tax_rate={config.account.stamp_tax_rate}, min_commission={config.account.min_commission}",
        )
    )
    rows.append(
        _check(
            "slippage_model",
            "PASS" if config.account.slippage_bps > 0 else "FAIL",
            f"slippage_bps={config.account.slippage_bps}",
        )
    )
    rows.append(
        _check(
            "trading_rules",
            "PASS" if config.risk.lot_size > 0 and config.risk.enforce_t1 else "WARN",
            f"lot_size={config.risk.lot_size}, enforce_t1={config.risk.enforce_t1}",
        )
    )
    rows.append(
        _check(
            "data_coverage",
            "PASS" if not missing_symbols else "WARN",
            f"symbols_used={len(market_data)}, missing={';'.join(missing_symbols)}",
        )
    )
    rows.append(_survivorship_check(config))
    rows.append(
        _check(
            "stress_scenarios",
            "PASS" if scenario_count >= 4 else "FAIL",
            f"scenarios={scenario_count}",
        )
    )
    rows.append(
        _check(
            "interpretation",
            "WARN",
            "backtest filters invalid strategies; it does not prove live profitability",
        )
    )
    return rows


def _survivorship_check(config: AppConfig) -> dict:
    path = config.data.symbols_file
    if path is None or not path.exists():
        return _check("survivorship_bias_control", "WARN", "no symbols_file with point-in-time universe metadata")
    try:
        frame = pd.read_csv(path, nrows=5)
    except pd.errors.EmptyDataError:
        return _check("survivorship_bias_control", "WARN", f"{path} is empty")
    required = {"listed_date", "delisted_date", "universe_date"}
    missing = sorted(required - set(frame.columns))
    if missing:
        return _check(
            "survivorship_bias_control",
            "WARN",
            f"{path} lacks point-in-time columns: {','.join(missing)}",
        )
    return _check("survivorship_bias_control", "PASS", f"{path} has point-in-time universe columns")


def _check(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def _shock_date(market_data: Dict[str, pd.DataFrame]) -> str:
    dates = sorted(set().union(*(set(frame["date"].dt.date) for frame in market_data.values())))
    if not dates:
        raise RuntimeError("cannot compute shock date without market data")
    return dates[len(dates) // 2].isoformat()


def _shock_market_data(market_data: Dict[str, pd.DataFrame], shock_date: str, shock_pct: float) -> Dict[str, pd.DataFrame]:
    shocked: Dict[str, pd.DataFrame] = {}
    threshold = pd.Timestamp(shock_date)
    multiplier = 1.0 + shock_pct
    for symbol, frame in market_data.items():
        changed = frame.copy()
        mask = changed["date"] >= threshold
        for column in ["open", "high", "low", "close"]:
            changed.loc[mask, column] = changed.loc[mask, column].astype(float) * multiplier
        shocked[symbol] = changed
    return shocked


def _write_summary(path: Path, scenarios: list[dict], checks: list[dict]) -> None:
    lines = ["# Stress Test Summary", ""]
    lines.append("| Scenario | Return | Max Drawdown | Fills | Cost | Rejections |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for row in scenarios:
        lines.append(
            "| {scenario} | {total_return} | {max_drawdown} | {fills} | {total_cost} | {rejections} |".format(
                scenario=row.get("scenario", ""),
                total_return=row.get("total_return", ""),
                max_drawdown=row.get("max_drawdown", ""),
                fills=row.get("fills", ""),
                total_cost=row.get("total_cost", ""),
                rejections=row.get("rejections", ""),
            )
        )
    lines.extend(["", "## Quality Checks", ""])
    lines.append("| Check | Status | Detail |")
    lines.append("| --- | --- | --- |")
    for row in checks:
        lines.append(f"| {row['check']} | {row['status']} | {row['detail']} |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
