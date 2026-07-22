"""Persistent Agent Identity credential store for Free registrations."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
STORE_FILE = OUTPUT_DIR / "agent_identities.json"
LOCK_FILE = OUTPUT_DIR / ".agent_identities.lock"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextmanager
def _locked() -> Iterator[None]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_unlocked() -> dict[str, dict[str, Any]]:
    if not STORE_FILE.exists():
        return {}
    try:
        raw = json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key).strip().lower(): value
        for key, value in raw.items()
        if str(key).strip() and isinstance(value, dict)
    }


def _write_unlocked(data: dict[str, dict[str, Any]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".agent_identities.", suffix=".tmp", dir=OUTPUT_DIR)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, STORE_FILE)
        os.chmod(STORE_FILE, 0o600)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def load_all() -> dict[str, dict[str, Any]]:
    with _locked():
        return _load_unlocked()


def get(email: str) -> dict[str, Any] | None:
    key = str(email or "").strip().lower()
    if not key:
        return None
    return load_all().get(key)


def save(
    *,
    email: str,
    agent_runtime_id: str,
    agent_private_key: str,
    account_id: str = "",
    user_id: str = "",
    plan_type: str = "free",
) -> dict[str, Any]:
    key = str(email or "").strip().lower()
    runtime_id = str(agent_runtime_id or "").strip()
    private_key = str(agent_private_key or "").strip()
    if not key or not runtime_id or not private_key:
        raise ValueError("email, agent_runtime_id and agent_private_key are required")
    with _locked():
        data = _load_unlocked()
        existing = data.get(key) or {}
        record = {
            **existing,
            "email": str(email).strip(),
            "agent_runtime_id": runtime_id,
            "agent_private_key": private_key,
            "account_id": str(account_id or existing.get("account_id") or ""),
            "user_id": str(user_id or existing.get("user_id") or ""),
            "plan_type": str(plan_type or existing.get("plan_type") or "free"),
            "created_at": str(existing.get("created_at") or _now_iso()),
            "updated_at": _now_iso(),
        }
        data[key] = record
        _write_unlocked(data)
        return dict(record)


__all__ = ["STORE_FILE", "get", "load_all", "save"]
