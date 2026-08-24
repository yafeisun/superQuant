from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request


SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}


def dispatch_status_alerts(
    state_dir: Path,
    min_severity: str = "warning",
    webhook_url: str | None = None,
    dry_run: bool = False,
) -> dict:
    status_dir = state_dir / "status"
    events = _read_events(status_dir / "events.jsonl")
    state = _read_json(status_dir / "alert_state.json")
    alerted = set(state.get("alerted_keys", []))
    threshold = SEVERITY_RANK.get(min_severity, SEVERITY_RANK["warning"])
    candidates = [
        event
        for event in events
        if SEVERITY_RANK.get(str(event.get("severity", "info")), 0) >= threshold and _alert_key(event) not in alerted
    ]
    results = []
    newly_alerted = []
    for event in candidates:
        result = _deliver_event(event, webhook_url, dry_run)
        results.append(result)
        if result["delivery_status"] in {"sent", "dry_run", "outbox"}:
            newly_alerted.append(_alert_key(event))
    if results:
        _append_jsonl(status_dir / "alerts.jsonl", results)
    if newly_alerted:
        state["alerted_keys"] = sorted(alerted.union(newly_alerted))[-1000:]
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        (status_dir / "alert_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "candidate_count": len(candidates),
        "delivered_count": sum(1 for row in results if row["delivery_status"] in {"sent", "dry_run", "outbox"}),
        "failed_count": sum(1 for row in results if row["delivery_status"] == "failed"),
        "alerts_path": status_dir / "alerts.jsonl",
        "state_path": status_dir / "alert_state.json",
    }


def _deliver_event(event: dict, webhook_url: str | None, dry_run: bool) -> dict:
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "delivery_status": "outbox",
        "event": event,
    }
    if dry_run:
        payload["delivery_status"] = "dry_run"
        return payload
    if not webhook_url:
        return payload
    try:
        request = urllib.request.Request(
            webhook_url,
            data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload["delivery_status"] = "sent"
            payload["http_status"] = response.status
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        payload["delivery_status"] = "failed"
        payload["error"] = str(exc)
    return payload


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _alert_key(event: dict) -> str:
    raw = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
