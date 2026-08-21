from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass(frozen=True)
class DataConfig:
    path: Path
    symbols: List[str]
    symbols_file: Optional[Path]
    start: str
    end: str


@dataclass(frozen=True)
class AccountConfig:
    initial_cash: float
    commission_rate: float
    min_commission: float
    stamp_tax_rate: float
    slippage_bps: float


@dataclass(frozen=True)
class RiskConfig:
    max_position_pct: float
    max_order_value_pct: float
    lot_size: int
    enforce_t1: bool


@dataclass(frozen=True)
class HealthConfig:
    enabled: bool
    path: Path
    min_turnover_rate: float
    min_amount: float
    min_pe: float
    max_pe: float
    min_float_market_cap: float
    max_float_market_cap: float
    min_health_score: float
    exclude_st: bool
    exclude_suspended: bool
    exclude_limit_up: bool
    exclude_limit_down: bool


@dataclass(frozen=True)
class FlowConfig:
    enabled: bool
    path: Path
    lookback_days: int
    min_positive_days: int
    min_main_net_inflow_ratio: float
    min_main_net_inflow_amount: float
    max_age_days: int


@dataclass(frozen=True)
class FactorConfig:
    enabled: bool
    event_path: Path
    macro_path: Path
    event_lookback_days: int
    min_event_confidence: float
    negative_event_score_block: float
    macro_risk_score_block: float
    max_score_adjustment: float


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    short_window: int
    long_window: int
    target_position_pct: float
    momentum_window: int
    rebalance_interval: int
    top_n: int
    target_gross_exposure: float
    min_momentum: float
    min_trade_value: float
    support_window: int
    target_window: int
    trend_window: int
    risk_reward_ratio: float
    entry_candidate_multiplier: int
    entry_max_above_support_pct: float
    entry_min_below_target_pct: float
    entry_max_recent_runup_pct: float
    entry_min_trend_slope: float


@dataclass(frozen=True)
class OutputConfig:
    backtest_dir: Path
    paper_dir: Path


@dataclass(frozen=True)
class AppConfig:
    data: DataConfig
    account: AccountConfig
    risk: RiskConfig
    health: HealthConfig
    flow: FlowConfig
    factors: FactorConfig
    strategy: StrategyConfig
    output: OutputConfig


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as file:
        raw: Dict[str, Any] = yaml.safe_load(file)

    data = raw["data"]
    account = raw["account"]
    risk = raw["risk"]
    health = raw.get("health", {})
    flow = raw.get("flow", {})
    factors = raw.get("factors", {})
    strategy = raw["strategy"]
    output = raw["output"]

    symbols = list(data.get("symbols", []))
    symbols_file = Path(data["symbols_file"]) if data.get("symbols_file") else None
    if symbols_file and symbols_file.exists():
        symbol_frame = _read_symbols_file(symbols_file)
        symbols = symbol_frame

    return AppConfig(
        data=DataConfig(
            path=Path(data["path"]),
            symbols=symbols,
            symbols_file=symbols_file,
            start=str(data["start"]),
            end=str(data["end"]),
        ),
        account=AccountConfig(
            initial_cash=float(account["initial_cash"]),
            commission_rate=float(account["commission_rate"]),
            min_commission=float(account["min_commission"]),
            stamp_tax_rate=float(account["stamp_tax_rate"]),
            slippage_bps=float(account["slippage_bps"]),
        ),
        risk=RiskConfig(
            max_position_pct=float(risk["max_position_pct"]),
            max_order_value_pct=float(risk["max_order_value_pct"]),
            lot_size=int(risk["lot_size"]),
            enforce_t1=bool(risk["enforce_t1"]),
        ),
        health=HealthConfig(
            enabled=bool(health.get("enabled", True)),
            path=Path(health.get("path", "data/health/latest.csv")),
            min_turnover_rate=float(health.get("min_turnover_rate", 0.5)),
            min_amount=float(health.get("min_amount", 30_000_000)),
            min_pe=float(health.get("min_pe", 0.0)),
            max_pe=float(health.get("max_pe", 120.0)),
            min_float_market_cap=float(health.get("min_float_market_cap", 1_000_000_000)),
            max_float_market_cap=float(health.get("max_float_market_cap", 20_000_000_000)),
            min_health_score=float(health.get("min_health_score", 60.0)),
            exclude_st=bool(health.get("exclude_st", True)),
            exclude_suspended=bool(health.get("exclude_suspended", True)),
            exclude_limit_up=bool(health.get("exclude_limit_up", True)),
            exclude_limit_down=bool(health.get("exclude_limit_down", False)),
        ),
        flow=FlowConfig(
            enabled=bool(flow.get("enabled", False)),
            path=Path(flow.get("path", "data/flow/latest.csv")),
            lookback_days=int(flow.get("lookback_days", 5)),
            min_positive_days=int(flow.get("min_positive_days", 3)),
            min_main_net_inflow_ratio=float(flow.get("min_main_net_inflow_ratio", 0.0)),
            min_main_net_inflow_amount=float(flow.get("min_main_net_inflow_amount", 0.0)),
            max_age_days=int(flow.get("max_age_days", 7)),
        ),
        factors=FactorConfig(
            enabled=bool(factors.get("enabled", False)),
            event_path=Path(factors.get("event_path", "data/factors/events.csv")),
            macro_path=Path(factors.get("macro_path", "data/factors/macro.csv")),
            event_lookback_days=int(factors.get("event_lookback_days", 10)),
            min_event_confidence=float(factors.get("min_event_confidence", 0.6)),
            negative_event_score_block=float(factors.get("negative_event_score_block", -35.0)),
            macro_risk_score_block=float(factors.get("macro_risk_score_block", -40.0)),
            max_score_adjustment=float(factors.get("max_score_adjustment", 12.0)),
        ),
        strategy=StrategyConfig(
            name=strategy["name"],
            short_window=int(strategy.get("short_window", 5)),
            long_window=int(strategy.get("long_window", 20)),
            target_position_pct=float(strategy.get("target_position_pct", 0.35)),
            momentum_window=int(strategy.get("momentum_window", 60)),
            rebalance_interval=int(strategy.get("rebalance_interval", 20)),
            top_n=int(strategy.get("top_n", 5)),
            target_gross_exposure=float(strategy.get("target_gross_exposure", 0.85)),
            min_momentum=float(strategy.get("min_momentum", 0.0)),
            min_trade_value=float(strategy.get("min_trade_value", 8000.0)),
            support_window=int(strategy.get("support_window", 20)),
            target_window=int(strategy.get("target_window", 60)),
            trend_window=int(strategy.get("trend_window", 5)),
            risk_reward_ratio=float(strategy.get("risk_reward_ratio", 2.0)),
            entry_candidate_multiplier=int(strategy.get("entry_candidate_multiplier", 4)),
            entry_max_above_support_pct=float(strategy.get("entry_max_above_support_pct", 0.10)),
            entry_min_below_target_pct=float(strategy.get("entry_min_below_target_pct", 0.03)),
            entry_max_recent_runup_pct=float(strategy.get("entry_max_recent_runup_pct", 0.12)),
            entry_min_trend_slope=float(strategy.get("entry_min_trend_slope", -0.02)),
        ),
        output=OutputConfig(
            backtest_dir=Path(output["backtest_dir"]),
            paper_dir=Path(output["paper_dir"]),
        ),
    )


def _read_symbols_file(path: Path) -> List[str]:
    symbols: List[str] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            symbol = stripped.split(",")[0]
            if symbol.lower() == "symbol":
                continue
            symbols.append(symbol)
    return symbols
