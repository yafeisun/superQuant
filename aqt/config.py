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


@dataclass(frozen=True)
class OutputConfig:
    backtest_dir: Path
    paper_dir: Path


@dataclass(frozen=True)
class AppConfig:
    data: DataConfig
    account: AccountConfig
    risk: RiskConfig
    strategy: StrategyConfig
    output: OutputConfig


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as file:
        raw: Dict[str, Any] = yaml.safe_load(file)

    data = raw["data"]
    account = raw["account"]
    risk = raw["risk"]
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
