from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from typing import Any


def write_run_status(
    root: Path,
    job: str,
    status: str,
    trading_day: date | str | None = None,
    severity: str = "info",
    message: str = "",
    details: dict[str, Any] | None = None,
) -> Path:
    status_dir = root / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "job": job,
        "status": status,
        "severity": severity,
        "trading_day": _date_text(trading_day),
        "message": message,
        "details": _jsonable(details or {}),
    }
    latest_path = status_dir / f"latest_{job}.json"
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (status_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return latest_path


def _date_text(value: date | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value
