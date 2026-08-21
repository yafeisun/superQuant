from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import Iterable

import pandas as pd

from .config import FactorConfig


EVENT_COLUMNS = [
    "symbol",
    "event_date",
    "event_type",
    "sentiment",
    "impact_score",
    "confidence",
    "summary",
    "source_url",
    "model",
    "prompt_version",
]

MACRO_COLUMNS = [
    "date",
    "factor",
    "value",
    "regime_score",
    "risk_flag",
    "source",
    "description",
]

FACTOR_COLUMNS = [
    "symbol",
    "factor_score",
    "event_score",
    "macro_score",
    "event_risk_flag",
    "macro_risk_flag",
    "factor_reasons",
    "latest_event_date",
    "event_summary",
    "macro_date",
    "macro_factor",
    "macro_description",
]


def load_event_factors(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=EVENT_COLUMNS)
    try:
        if path.suffix.lower() == ".jsonl":
            return _read_event_jsonl(path)
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=EVENT_COLUMNS)


def load_macro_factors(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=MACRO_COLUMNS)
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=MACRO_COLUMNS)


def load_external_factors(config: FactorConfig, symbols: Iterable[str], as_of: date) -> pd.DataFrame:
    if not config.enabled:
        return pd.DataFrame(columns=FACTOR_COLUMNS)
    events = load_event_factors(config.event_path)
    macro = load_macro_factors(config.macro_path)
    return evaluate_external_factors(symbols, events, macro, config, as_of)


def normalize_event_factors(input_path: Path, output_path: Path) -> Path:
    if not input_path.exists():
        raise FileNotFoundError(f"missing event factor input: {input_path}")
    events = load_event_factors(input_path)
    normalized = _normalize_events(events)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized.to_csv(output_path, index=False)
    return output_path


def evaluate_external_factors(
    symbols: Iterable[str],
    events: pd.DataFrame | None,
    macro: pd.DataFrame | None,
    config: FactorConfig,
    as_of: date,
) -> pd.DataFrame:
    if not config.enabled:
        return pd.DataFrame(columns=FACTOR_COLUMNS)

    event_frame = _normalize_events(events) if events is not None and not events.empty else pd.DataFrame(columns=EVENT_COLUMNS)
    macro_row = _latest_macro_row(macro, as_of) if macro is not None and not macro.empty else {}
    rows = []
    for symbol in symbols:
        event_eval = _evaluate_symbol_events(str(symbol), event_frame, config, as_of)
        macro_eval = _evaluate_macro(macro_row, config)
        reasons = list(dict.fromkeys(event_eval["reasons"] + macro_eval["reasons"]))
        factor_score = _clamp(
            event_eval["event_score"] + macro_eval["macro_score"],
            -config.max_score_adjustment * 2.0,
            config.max_score_adjustment * 2.0,
        )
        rows.append(
            {
                "symbol": str(symbol),
                "factor_score": round(factor_score, 2),
                "event_score": round(event_eval["event_score"], 2),
                "macro_score": round(macro_eval["macro_score"], 2),
                "event_risk_flag": event_eval["event_risk_flag"],
                "macro_risk_flag": macro_eval["macro_risk_flag"],
                "factor_reasons": ";".join(reasons),
                "latest_event_date": event_eval["latest_event_date"],
                "event_summary": event_eval["event_summary"],
                "macro_date": macro_eval["macro_date"],
                "macro_factor": macro_eval["macro_factor"],
                "macro_description": macro_eval["macro_description"],
            }
        )
    return pd.DataFrame(rows, columns=FACTOR_COLUMNS)


def _read_event_jsonl(path: Path) -> pd.DataFrame:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} invalid JSONL event factor: {exc}") from exc
    return pd.DataFrame(rows)


def _normalize_events(events: pd.DataFrame | None) -> pd.DataFrame:
    if events is None or events.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    frame = events.copy()
    for column in EVENT_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    frame["symbol"] = frame["symbol"].astype(str).str.strip()
    frame["event_date"] = pd.to_datetime(frame["event_date"], errors="coerce")
    frame["event_type"] = frame["event_type"].astype(str).str.strip().str.lower().replace({"": "other"})
    frame["sentiment"] = pd.to_numeric(frame["sentiment"], errors="coerce").fillna(0.0).clip(-1.0, 1.0)
    frame["impact_score"] = pd.to_numeric(frame["impact_score"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    frame["confidence"] = pd.to_numeric(frame["confidence"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    frame["summary"] = frame["summary"].astype(str).str.strip()
    frame["source_url"] = frame["source_url"].astype(str).str.strip()
    frame["model"] = frame["model"].astype(str).str.strip()
    frame["prompt_version"] = frame["prompt_version"].astype(str).str.strip()
    frame = frame.dropna(subset=["event_date"])
    frame = frame[(frame["symbol"] != "") & (frame["source_url"] != "")]
    return frame[EVENT_COLUMNS].sort_values(["event_date", "symbol"])


def _latest_macro_row(macro: pd.DataFrame | None, as_of: date) -> dict:
    if macro is None or macro.empty:
        return {}
    frame = macro.copy()
    for column in MACRO_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"])
    frame = frame[frame["date"].dt.date <= as_of]
    if frame.empty:
        return {}
    frame["regime_score"] = pd.to_numeric(frame["regime_score"], errors="coerce").fillna(0.0)
    frame = frame.sort_values("date")
    row = frame.iloc[-1].to_dict()
    row["date"] = pd.to_datetime(row["date"]).date().isoformat()
    return row


def _evaluate_symbol_events(symbol: str, events: pd.DataFrame, config: FactorConfig, as_of: date) -> dict:
    if events.empty:
        return {
            "event_score": 0.0,
            "event_risk_flag": False,
            "latest_event_date": "",
            "event_summary": "",
            "reasons": ["event_factor_missing_neutral"],
        }

    start_date = as_of - timedelta(days=max(config.event_lookback_days, 1))
    rows = events[
        (events["symbol"].astype(str) == symbol)
        & (events["event_date"].dt.date <= as_of)
        & (events["event_date"].dt.date >= start_date)
        & (events["confidence"] >= config.min_event_confidence)
    ].copy()
    if rows.empty:
        return {
            "event_score": 0.0,
            "event_risk_flag": False,
            "latest_event_date": "",
            "event_summary": "",
            "reasons": ["no_recent_event_factor"],
        }

    rows["age_days"] = rows["event_date"].dt.date.apply(lambda item: max((as_of - item).days, 0))
    rows["recency_weight"] = 1.0 - rows["age_days"] / max(config.event_lookback_days * 2.0, 1.0)
    rows["recency_weight"] = rows["recency_weight"].clip(0.5, 1.0)
    rows["raw_score"] = rows["sentiment"] * rows["impact_score"] * rows["confidence"] * rows["recency_weight"] * 100.0
    raw_score = float(rows["raw_score"].sum())
    event_score = _clamp(raw_score, -config.max_score_adjustment, config.max_score_adjustment)
    event_risk = bool((rows["raw_score"] <= config.negative_event_score_block).any())

    reasons = []
    if event_risk:
        reasons.append("negative_event_risk")
    elif event_score > 3:
        reasons.append("positive_event_support")
    elif event_score < -3:
        reasons.append("negative_event_drag")
    else:
        reasons.append("event_factor_neutral")

    top_events = rows.assign(abs_score=rows["raw_score"].abs()).sort_values("abs_score", ascending=False).head(3)
    summary_parts = []
    for _, row in top_events.iterrows():
        event_date = pd.to_datetime(row["event_date"]).date().isoformat()
        summary = str(row["summary"])[:80]
        summary_parts.append(f"{event_date}:{row['event_type']}:{summary}")

    return {
        "event_score": event_score,
        "event_risk_flag": event_risk,
        "latest_event_date": pd.to_datetime(rows["event_date"].max()).date().isoformat(),
        "event_summary": " | ".join(summary_parts),
        "reasons": reasons,
    }


def _evaluate_macro(row: dict, config: FactorConfig) -> dict:
    if not row:
        return {
            "macro_score": 0.0,
            "macro_risk_flag": False,
            "macro_date": "",
            "macro_factor": "",
            "macro_description": "",
            "reasons": ["macro_factor_missing_neutral"],
        }
    regime_score = _safe_float(row.get("regime_score"))
    macro_score = _clamp(regime_score / 100.0 * config.max_score_adjustment, -config.max_score_adjustment, config.max_score_adjustment)
    risk_flag = _truthy(row.get("risk_flag")) or regime_score <= config.macro_risk_score_block
    reasons = []
    if risk_flag:
        reasons.append("macro_risk_off")
    elif macro_score > 2:
        reasons.append("macro_supportive")
    elif macro_score < -2:
        reasons.append("macro_drag")
    else:
        reasons.append("macro_neutral")
    return {
        "macro_score": macro_score,
        "macro_risk_flag": risk_flag,
        "macro_date": str(row.get("date", "")),
        "macro_factor": str(row.get("factor", "")),
        "macro_description": str(row.get("description", ""))[:120],
        "reasons": reasons,
    }


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or pd.isna(value):
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _safe_float(value) -> float:
    if value is None or value == "" or pd.isna(value):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))
