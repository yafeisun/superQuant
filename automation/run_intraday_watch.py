from __future__ import annotations

import argparse
import csv
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from aqt.calendar import is_a_share_trading_day, parse_calendar_date
from aqt.config import load_config
from aqt.data import fetch_akshare_daily
from aqt.engine import run_paper
from aqt.signals import generate_daily_signal


def main() -> None:
    parser = argparse.ArgumentParser(description="Run intraday watch automation and append only material state changes")
    parser.add_argument("--config", default="configs/smallcap_best.yaml")
    parser.add_argument("--output-root", default="reports/intraday")
    parser.add_argument("--adjust", default="qfq")
    parser.add_argument("--date", default=None)
    parser.add_argument("--time", default=None)
    parser.add_argument("--market-end", default=None, help="YYYY-MM-DD data end override, defaults to today in Asia/Shanghai")
    parser.add_argument("--force", action="store_true", help="run even when the target date is not an A-share trading day")
    args = parser.parse_args()

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    output_date = args.date or now.strftime("%Y%m%d")
    output_time = args.time or now.strftime("%H%M")
    target_date = parse_calendar_date(args.market_end or output_date, now.date())
    if not args.force and not is_a_share_trading_day(target_date):
        print(f"skip: {target_date.isoformat()} is not an A-share trading day")
        return

    output_dir = Path(args.output_root) / output_date
    output_dir.mkdir(parents=True, exist_ok=True)

    config = _with_market_end(load_config(args.config), args.market_end)
    fetch_akshare_daily(config.data.path, config.data.symbols, config.data.start, config.data.end, args.adjust)
    paper_paths = run_paper(config, cycles=None)
    signal_paths = generate_daily_signal(config, Path("runs/daily_signal"))

    state = _build_state(signal_paths, paper_paths)
    changed = _append_event_if_changed(output_dir, output_time, state)
    if changed:
        _write_watchlist(output_dir, state)
        _write_manifest(output_dir, args.config, output_date, output_time, state)
    else:
        print(f"{output_dir} unchanged")
        return
    print(output_dir)


def _with_market_end(config, market_end: str | None):
    end = market_end or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    return replace(config, data=replace(config.data, end=end))


def _build_state(signal_paths: dict[str, Path], paper_paths: dict[str, Path]) -> dict:
    summary = _read_summary(signal_paths["summary"])
    orders = _read_csv(signal_paths["orders"])
    positions = _read_csv(signal_paths["positions"])
    rankings = _read_csv(signal_paths["rankings"])
    metrics = _read_csv(paper_paths["metrics"])

    allowed_orders = _records(orders[orders["decision"] == "ALLOW"]) if "decision" in orders else []
    blocked_orders = _records(orders[orders["decision"] == "BLOCK"]) if "decision" in orders else []
    position_records = _records(positions)
    selected = _records(rankings.head(10))
    metric = _records(metrics)[0] if not metrics.empty else {}

    state = {
        "signal_date": summary.get("signal_date", ""),
        "rebalance_day": summary.get("rebalance_day", ""),
        "next_rebalance_in_trading_days": summary.get("next_rebalance_in_trading_days", ""),
        "equity": summary.get("equity", metric.get("end_equity", "")),
        "cash": summary.get("cash", ""),
        "planned_orders": summary.get("planned_orders", str(len(allowed_orders) + len(blocked_orders))),
        "allowed_orders": allowed_orders,
        "blocked_orders": blocked_orders,
        "positions": position_records,
        "selected_symbols": [row.get("symbol", "") for row in selected],
        "watch_symbols": sorted(
            set([row.get("symbol", "") for row in selected] + [row.get("symbol", "") for row in position_records])
        ),
        "metrics": {
            "total_return": metric.get("total_return", ""),
            "max_drawdown": metric.get("max_drawdown", ""),
            "win_rate": metric.get("win_rate", ""),
            "total_cost": metric.get("total_cost", ""),
        },
    }
    state["fingerprint"] = _fingerprint(state)
    return state


def _append_event_if_changed(output_dir: Path, output_time: str, state: dict) -> bool:
    latest_path = output_dir / "latest_state.json"
    previous = _read_json(latest_path)
    if previous.get("fingerprint") == state["fingerprint"]:
        return False

    event = _event_row(output_time, state)
    events_path = output_dir / "events.csv"
    with events_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(event.keys()))
        if handle.tell() == 0:
            writer.writeheader()
        writer.writerow(event)

    state_with_time = dict(state)
    state_with_time["updated_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    latest_path.write_text(json.dumps(state_with_time, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True


def _event_row(output_time: str, state: dict) -> dict:
    return {
        "time": output_time,
        "signal_date": state["signal_date"],
        "rebalance_day": state["rebalance_day"],
        "next_rebalance_in_trading_days": state["next_rebalance_in_trading_days"],
        "equity": state["equity"],
        "cash": state["cash"],
        "positions": len(state["positions"]),
        "planned_orders": state["planned_orders"],
        "allow_orders": len(state["allowed_orders"]),
        "block_orders": len(state["blocked_orders"]),
        "order_symbols": ";".join(_format_order(row) for row in state["allowed_orders"] + state["blocked_orders"]),
        "watch_symbols": ";".join(state["watch_symbols"]),
        "total_return": state["metrics"].get("total_return", ""),
        "max_drawdown": state["metrics"].get("max_drawdown", ""),
        "win_rate": state["metrics"].get("win_rate", ""),
        "total_cost": state["metrics"].get("total_cost", ""),
    }


def _format_order(row: dict) -> str:
    fields = [row.get("side", ""), row.get("symbol", ""), str(row.get("quantity", "")), row.get("decision", "")]
    return ":".join(fields)


def _write_watchlist(output_dir: Path, state: dict) -> None:
    result = pd.DataFrame({"symbol": state["watch_symbols"]})
    result.to_csv(output_dir / "watchlist.csv", index=False)


def _write_manifest(output_dir: Path, config_path: str, output_date: str, output_time: str, state: dict) -> None:
    lines = [
        "job_type: intraday_watch",
        f"config: {config_path}",
        f"watch_date: {output_date}",
        f"last_changed_time: {output_time}",
        f"signal_date: {state['signal_date']}",
        f"generated_at: {datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')}",
    ]
    (output_dir / "manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_summary(path: Path) -> dict[str, str]:
    result = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _records(frame: pd.DataFrame) -> list[dict]:
    if frame.empty:
        return []
    clean = frame.copy()
    clean = clean.where(pd.notnull(clean), "")
    return clean.to_dict(orient="records")


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _fingerprint(state: dict) -> str:
    payload = {key: value for key, value in state.items() if key != "fingerprint"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    main()
