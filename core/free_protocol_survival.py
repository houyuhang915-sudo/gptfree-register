"""Build a non-secret survival snapshot for Free protocol registrations."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parent
FREE_RESULTS = ROOT / "output" / "free_accounts.jsonl"
SUCCESS_FILE = ROOT / "output" / "success.txt"
PLAN_RESULTS = ROOT / "output" / "plan_check_results.json"
REPORT_FILE = ROOT / "output" / "free_protocol_survival.json"


def _parse_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        from zoneinfo import ZoneInfo

        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(timezone.utc)


def _legacy_success_times(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for block in re.split(r"={20,}", text or ""):
        email_match = re.search(r"邮箱[:：]\s*(\S+@\S+)", block)
        time_match = re.search(r"成功时间[:：]\s*([^\n]+)", block)
        note_match = re.search(r"备注[:：]\s*([^\n]+)", block)
        if not email_match or not time_match:
            continue
        if note_match and "protocol" not in note_match.group(1).lower():
            continue
        result[email_match.group(1).strip().lower()] = time_match.group(1).strip()
    return result


def build_survival_report() -> dict[str, Any]:
    legacy_times = _legacy_success_times(
        SUCCESS_FILE.read_text(encoding="utf-8", errors="ignore")
        if SUCCESS_FILE.exists() else ""
    )
    registrations: dict[str, dict[str, Any]] = {}
    if FREE_RESULTS.exists():
        for line in FREE_RESULTS.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict) or row.get("method") != "protocol" or not row.get("ok"):
                continue
            email = str(row.get("email") or "").strip()
            if not email:
                continue
            registrations[email.lower()] = {
                "email": email,
                "registered_at": row.get("registered_at") or legacy_times.get(email.lower(), ""),
            }

    snapshot: dict[str, Any] = {}
    if PLAN_RESULTS.exists():
        try:
            snapshot = json.loads(PLAN_RESULTS.read_text(encoding="utf-8"))
        except Exception:
            snapshot = {}
    checks = {
        str(row.get("email") or "").lower(): row
        for row in snapshot.get("results") or []
        if isinstance(row, dict) and row.get("email")
    }

    rows: list[dict[str, Any]] = []
    for key, registration in registrations.items():
        check = checks.get(key) or {}
        registered = _parse_time(str(registration.get("registered_at") or ""))
        checked = _parse_time(str(check.get("last_checked") or ""))
        seconds = max(0, int((checked - registered).total_seconds())) if registered and checked else None
        status = str(check.get("status") or "unchecked")
        rows.append({
            **registration,
            "last_checked": check.get("last_checked") or "",
            "status": status,
            "plan_type": check.get("plan_type") or "",
            "alive": status in {"free", "plus", "k12"},
            "observed_seconds": seconds,
            "observed_hours": round(seconds / 3600, 2) if seconds is not None else None,
            "error": check.get("error") or "",
        })
    rows.sort(key=lambda row: str(row.get("registered_at") or ""))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "protocol_successes": len(rows),
        "alive": sum(bool(row["alive"]) for row in rows),
        "not_alive": sum(not bool(row["alive"]) for row in rows),
        "rows": rows,
    }


def write_survival_report() -> Path:
    report = build_survival_report()
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return REPORT_FILE


__all__ = ["REPORT_FILE", "build_survival_report", "write_survival_report"]
