from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from account_status_poller import StatusPoller


def wait_until_not_running(poller: StatusPoller, *, timeout: float = 1) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    status = poller.status()
    while status["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
        status = poller.status()
    return status


def test_poller_persists_validated_config_in_console_data_path(tmp_path: Path) -> None:
    path = tmp_path / "data" / "status_poll.json"
    poller = StatusPoller(run_callback=lambda _config: {}, config_path=path)

    status = poller.update_config({
        "enabled": False,
        "interval_minutes": 30,
        "concurrency": 2,
        "refresh_codex_rt": False,
    })

    assert status["enabled"] is False
    assert status["protocol_login"] is False
    assert status["mode"] == "codex_rt_then_at"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "enabled": False,
        "interval_minutes": 30,
        "concurrency": 2,
        "refresh_codex_rt": False,
    }
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="interval_minutes"):
        poller.update_config({"interval_minutes": 14})
    with pytest.raises(ValueError, match="concurrency"):
        poller.update_config({"concurrency": 9})


def test_poller_runs_callback_after_initial_delay_and_records_summary(tmp_path: Path) -> None:
    called = threading.Event()
    received: list[dict[str, object]] = []

    def callback(config: dict[str, object]) -> dict[str, object]:
        received.append(config)
        called.set()
        return {"total": 3, "free": 2, "errors": 1}

    poller = StatusPoller(
        run_callback=callback,
        config_path=tmp_path / "data" / "status_poll.json",
        start_delay_seconds=0,
    )
    try:
        poller.start()
        assert called.wait(timeout=1)
        status = wait_until_not_running(poller)
        assert received == [{
            "enabled": True,
            "interval_minutes": 60,
            "concurrency": 4,
            "refresh_codex_rt": True,
        }]
        assert status["last_summary"] == {"total": 3, "free": 2, "errors": 1}
        assert status["last_error"] == ""
        assert status["last_started_at"]
        assert status["last_finished_at"]
        assert status["next_run_at"]
    finally:
        poller.stop()


def test_poller_run_now_works_while_disabled_and_records_callback_error(tmp_path: Path) -> None:
    called = threading.Event()

    def callback(_config: dict[str, object]) -> dict[str, object]:
        called.set()
        raise RuntimeError("worker failed")

    poller = StatusPoller(
        run_callback=callback,
        config_path=tmp_path / "data" / "status_poll.json",
        start_delay_seconds=3600,
    )
    try:
        poller.update_config({"enabled": False})
        assert poller.run_now() is True
        assert called.wait(timeout=1)
        status = wait_until_not_running(poller)
        assert status["running"] is False
        assert status["last_error"] == "RuntimeError: worker failed"
        assert status["next_run_at"] == ""
    finally:
        poller.stop()
