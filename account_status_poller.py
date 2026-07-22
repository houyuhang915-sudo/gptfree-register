"""Durable background scheduler for standalone account-status checks.

The poller deliberately does not know how an account is checked.  The console
injects a callback that performs one token-only check pass and returns its
summary.  Keeping the execution boundary here makes the scheduler reusable
without coupling it to the copied workbench modules or their mutable paths.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "data" / "status_poll.json"
DEFAULT_CONFIG: dict[str, object] = {
    "enabled": True,
    "interval_minutes": 60,
    "concurrency": 4,
    "refresh_codex_rt": True,
}
MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 24 * 60
MAX_CONCURRENCY = 8

log = logging.getLogger("account_status_poller")

PollCallback = Callable[[dict[str, object]], dict[str, object]]


def _iso(timestamp: float | None) -> str:
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class StatusPoller:
    """Schedule token-only account checks and expose their operational state.

    ``run_callback`` receives a copy of the active configuration and returns a
    summary dictionary.  It may block while a subprocess or worker completes;
    the poller never starts a second callback until the first one has returned.
    """

    def __init__(
        self,
        *,
        run_callback: PollCallback | None = None,
        config_path: Path = DEFAULT_CONFIG_PATH,
        clock: Callable[[], float] = time.time,
        start_delay_seconds: int = 60,
    ) -> None:
        if run_callback is None:
            raise ValueError("run_callback is required")
        if not callable(run_callback):
            raise TypeError("run_callback must be callable")
        self._run_callback = run_callback
        self.config_path = Path(config_path)
        self._clock = clock
        # Production starts sixty seconds after boot.  Accept zero in tests so
        # a scheduler run can be observed without sleeping.
        self._start_delay_seconds = max(0, int(start_delay_seconds))
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._run_requested = False
        self._last_started_at: float | None = None
        self._last_finished_at: float | None = None
        self._last_error = ""
        self._last_summary: dict[str, object] = {}
        self._config = self._load_config()
        self._next_run_at: float | None = (
            self._clock() + self._start_delay_seconds
            if self._config["enabled"]
            else None
        )

    def _load_config(self) -> dict[str, object]:
        if not self.config_path.exists():
            return dict(DEFAULT_CONFIG)
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return dict(DEFAULT_CONFIG)
        if not isinstance(payload, dict):
            return dict(DEFAULT_CONFIG)
        try:
            return self._validate_config({**DEFAULT_CONFIG, **payload})
        except ValueError:
            return dict(DEFAULT_CONFIG)

    @staticmethod
    def _validate_config(payload: Mapping[str, object]) -> dict[str, object]:
        enabled = payload.get("enabled")
        refresh_codex_rt = payload.get("refresh_codex_rt")
        if type(enabled) is not bool or type(refresh_codex_rt) is not bool:
            raise ValueError("enabled and refresh_codex_rt must be boolean")
        try:
            interval = int(payload.get("interval_minutes", 0))
            concurrency = int(payload.get("concurrency", 0))
        except (TypeError, ValueError):
            raise ValueError("interval and concurrency must be integers") from None
        if not MIN_INTERVAL_MINUTES <= interval <= MAX_INTERVAL_MINUTES:
            raise ValueError(
                f"interval_minutes must be {MIN_INTERVAL_MINUTES}-{MAX_INTERVAL_MINUTES}"
            )
        if not 1 <= concurrency <= MAX_CONCURRENCY:
            raise ValueError(f"concurrency must be 1-{MAX_CONCURRENCY}")
        return {
            "enabled": enabled,
            "interval_minutes": interval,
            "concurrency": concurrency,
            "refresh_codex_rt": refresh_codex_rt,
        }

    def _save_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(self._config, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.config_path.name}.",
            dir=str(self.config_path.parent),
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config_path)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def start(self) -> None:
        """Start the scheduler once; subsequent calls are no-ops."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="free-console-account-status-poller",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the scheduler to stop after any in-progress callback returns."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def update_config(self, payload: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(payload, Mapping):
            raise ValueError("config must be an object")
        with self._lock:
            candidate = self._validate_config({**self._config, **payload})
            self._config = candidate
            self._save_config()
            if candidate["enabled"]:
                self._next_run_at = self._clock() + int(candidate["interval_minutes"]) * 60
            else:
                self._run_requested = False
                self._next_run_at = None
        self._wake.set()
        return self.status()

    def run_now(self) -> bool:
        """Queue one immediate pass unless another pass is already running."""
        self.start()
        with self._lock:
            if self._running:
                return False
            self._run_requested = True
            self._next_run_at = self._clock()
        self._wake.set()
        return True

    def status(self) -> dict[str, object]:
        """Return configuration and in-memory scheduler state for the API/UI."""
        with self._lock:
            return {
                **self._config,
                "running": self._running,
                "last_started_at": _iso(self._last_started_at),
                "last_finished_at": _iso(self._last_finished_at),
                "next_run_at": _iso(self._next_run_at),
                "last_error": self._last_error,
                "last_summary": dict(self._last_summary),
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "mode": "codex_rt_then_at",
                "protocol_login": False,
            }

    def _loop(self) -> None:
        while not self._stop.is_set():
            should_run = False
            wait_seconds = 30.0
            with self._lock:
                now = self._clock()
                if (
                    (self._run_requested or self._config["enabled"])
                    and self._next_run_at is not None
                    and now >= self._next_run_at
                    and not self._running
                ):
                    self._running = True
                    self._run_requested = False
                    self._last_started_at = now
                    should_run = True
                elif self._config["enabled"] and self._next_run_at is not None:
                    wait_seconds = max(0.01, min(30.0, self._next_run_at - now))
            if should_run:
                self._execute()
                continue
            self._wake.wait(timeout=wait_seconds)
            self._wake.clear()

    def _execute(self) -> None:
        try:
            with self._lock:
                config = dict(self._config)
            summary = self._run_callback(config)
            if not isinstance(summary, dict):
                raise TypeError("run_callback must return a summary dictionary")
            with self._lock:
                self._last_summary = dict(summary)
                self._last_error = ""
        except Exception as exc:
            log.exception("standalone account status polling failed")
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                finished = self._clock()
                self._running = False
                self._last_finished_at = finished
                if self._config["enabled"]:
                    self._next_run_at = finished + int(self._config["interval_minutes"]) * 60
                else:
                    self._next_run_at = None


# Kept as a descriptive alias for callers that prefer the standalone-console
# name.  New integrations can use the workbench-aligned ``StatusPoller`` API.
AccountStatusPoller = StatusPoller
