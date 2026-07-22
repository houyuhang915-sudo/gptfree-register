#!/usr/bin/env python3
"""Standalone web service for the isolated Free registration workflow."""
from __future__ import annotations

import base64
import hmac
import importlib.util
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from flask import Flask, Response, jsonify, render_template, request, send_file

from account_registry import AccountRegistry, HEALTHY_STATUSES
from account_status_poller import StatusPoller


ROOT = Path(__file__).resolve().parent
CORE = ROOT / "core"
DATA = ROOT / "data"
JOB_DIR = DATA / "jobs"
INPUT_DIR = DATA / "inputs"
OUTPUT = ROOT / "output"
RESULT_DIR = OUTPUT / "results"
SETTINGS_FILE = DATA / "settings.json"
PYTHON = os.environ.get("FREE_CONSOLE_PYTHON", sys.executable)

if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

for directory in (DATA, JOB_DIR, INPUT_DIR, OUTPUT, RESULT_DIR):
    directory.mkdir(parents=True, exist_ok=True)


def bootstrap_persistent_paths() -> None:
    """Keep mutable core data in the mounted data/output directories."""
    account_dir = DATA / "accounts"
    account_dir.mkdir(parents=True, exist_ok=True)
    for name, header in (
        ("outlook_accounts.txt", "# email----password----client_id----refresh_token\n"),
        ("icloud_accounts.txt", "# email----relay_url\n"),
    ):
        core_path = CORE / name
        durable_path = account_dir / name
        if not durable_path.exists():
            if core_path.exists() and not core_path.is_symlink():
                durable_path.write_text(core_path.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
            else:
                durable_path.write_text(header, encoding="utf-8")
            os.chmod(durable_path, 0o600)
        if core_path.is_symlink() and core_path.resolve() == durable_path.resolve():
            continue
        if core_path.exists() or core_path.is_symlink():
            core_path.unlink()
        core_path.symlink_to(durable_path)

    durable_output = OUTPUT / "core"
    durable_output.mkdir(parents=True, exist_ok=True)
    core_output = CORE / "output"
    if core_output.is_symlink() and core_output.resolve() == durable_output.resolve():
        return
    if core_output.exists() and not core_output.is_symlink():
        for child in core_output.iterdir():
            if child.name == ".gitkeep":
                child.unlink()
                continue
            target = durable_output / child.name
            if not target.exists():
                child.replace(target)
        core_output.rmdir()
    elif core_output.is_symlink():
        core_output.unlink()
    core_output.symlink_to(durable_output, target_is_directory=True)


bootstrap_persistent_paths()

app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
app.config.update(JSON_SORT_KEYS=False, MAX_CONTENT_LENGTH=2 * 1024 * 1024)


DEFAULT_SETTINGS: dict[str, Any] = {
    "free_sms_provider": "smsbower",
    "free_sms_api_key": "",
    "free_sms_base_url": "",
    "free_sms_service": "dr",
    "free_sms_country": "12",
    "free_sms_max_price": "",
    "free_sms_operator": "",
    "free_sms_use_v2": "1",
    "proxy_host": "",
    "proxy_port": "10000",
    "proxy_user": "",
    "proxy_password": "",
    "gateway_sub2api_url": "",
    "gateway_sub2api_token": "",
    "gateway_sub2api_agent_path": "/api/v1/admin/accounts/import/codex-session",
    "gateway_sub2api_group_ids": "2",
    "bitbrowser_api": "http://127.0.0.1:54345",
    "roxy_api_port": "50000",
    "roxy_api_token": "",
    "chrome_path": "",
    "display_timezone": "Asia/Shanghai",
}

SECRET_KEYS = {
    "free_sms_api_key",
    "proxy_password",
    "gateway_sub2api_token",
    "roxy_api_token",
}

SETTING_ENV = {
    "free_sms_provider": "FREE_SMS_PROVIDER",
    "free_sms_api_key": "FREE_SMSBOWER_API_KEY",
    "free_sms_base_url": "FREE_SMSBOWER_BASE_URL",
    "free_sms_service": "FREE_SMSBOWER_SERVICE",
    "free_sms_country": "FREE_SMSBOWER_COUNTRY",
    "free_sms_max_price": "FREE_SMSBOWER_MAX_PRICE",
    "free_sms_operator": "FREE_SMSBOWER_OPERATOR",
    "free_sms_use_v2": "FREE_SMSBOWER_USE_V2",
    "proxy_host": "PROXY_HOST",
    "proxy_port": "PROXY_PORT",
    "proxy_user": "PROXY_USER",
    "proxy_password": "PROXY_PASS",
    "gateway_sub2api_url": "GATEWAY_SUB2API_URL",
    "gateway_sub2api_token": "GATEWAY_SUB2API_TOKEN",
    "gateway_sub2api_agent_path": "GATEWAY_SUB2API_AGENT_PATH",
    "gateway_sub2api_group_ids": "GATEWAY_SUB2API_GROUP_IDS",
    "bitbrowser_api": "BITBROWSER_API",
    "roxy_api_port": "ROXY_API_PORT",
    "roxy_api_token": "ROXY_API_TOKEN",
    "chrome_path": "CHROME_PATH",
    "display_timezone": "FREE_CONSOLE_TIMEZONE",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def display_timezone_name(settings: dict[str, Any] | None = None) -> str:
    value = str((settings or load_settings()).get("display_timezone") or "Asia/Shanghai").strip()
    try:
        ZoneInfo(value)
        return value
    except ZoneInfoNotFoundError:
        return "Asia/Shanghai"


def apply_process_timezone(settings: dict[str, Any] | None = None) -> str:
    """Keep legacy runtime timestamps aligned with the console display zone."""
    name = display_timezone_name(settings)
    os.environ["TZ"] = name
    if hasattr(time, "tzset"):
        time.tzset()
    return name


def local_now(settings: dict[str, Any] | None = None) -> str:
    zone = ZoneInfo(display_timezone_name(settings))
    return datetime.now(zone).isoformat(timespec="seconds")


def atomic_json(path: Path, payload: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, mode)
    temp.replace(path)


def load_settings() -> dict[str, Any]:
    data = dict(DEFAULT_SETTINGS)
    stored_keys: set[str] = set()
    if SETTINGS_FILE.exists():
        try:
            stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                saved = {key: stored[key] for key in DEFAULT_SETTINGS if key in stored}
                data.update(saved)
                stored_keys.update(saved)
        except (OSError, ValueError):
            pass
    for key, env_name in SETTING_ENV.items():
        # .env seeds a fresh install. Once the UI has saved a key, its value is
        # authoritative so template defaults (for example country=12) cannot
        # silently replace the operator's choice (for example country=145).
        env_value = os.environ.get(env_name)
        if key not in stored_keys and env_value is not None and str(env_value).strip() != "":
            data[key] = env_value
    return data


def settings_env(settings: dict[str, Any] | None = None) -> dict[str, str]:
    current = settings or load_settings()
    env = {env_name: str(current.get(key, "")) for key, env_name in SETTING_ENV.items()}
    env["ACCOUNT_VAULT_DIR"] = str(DATA / "account_vault")
    env["FREE_CONSOLE_HEALTH_PROXY"] = health_check_proxy(current)
    env["TZ"] = display_timezone_name(current)
    return env


def health_check_proxy(settings: dict[str, Any] | None = None) -> str:
    """Build the configured proxy URL for non-browser health probes.

    The standalone health worker previously ignored the proxy configured in
    the console and connected from the local host IP directly. Keep the secret
    in the child environment rather than exposing it in a command or job log.
    """
    current = settings or load_settings()
    host = str(current.get("proxy_host") or "").strip()
    port = str(current.get("proxy_port") or "").strip()
    user = str(current.get("proxy_user") or "").strip()
    password = str(current.get("proxy_password") or "")
    if not (host and port and user):
        return ""
    try:
        parsed_port = int(port)
    except (TypeError, ValueError):
        return ""
    if not 1 <= parsed_port <= 65535:
        return ""
    credentials = urllib.parse.quote(user, safe="")
    if password:
        credentials += ":" + urllib.parse.quote(password, safe="")
    return f"http://{credentials}@{host}:{parsed_port}"


apply_process_timezone()


@app.before_request
def _auth_guard() -> Response | None:
    if request.path == "/api/health":
        return None
    expected = os.environ.get("FREE_CONSOLE_PASSWORD", "")
    if not expected:
        return None
    auth = request.authorization
    if auth and hmac.compare_digest(auth.password or "", expected):
        return None
    return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Free Console"'})


def clean_account_lines(text: str) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        email = line.split("----", 1)[0].strip().lower()
        if "@" not in email or email in seen:
            continue
        seen.add(email)
        rows.append(line)
    return rows


def account_pool_path(name: str) -> Path:
    return DATA / "accounts" / name


def console_vault():
    """Build the standalone credential vault against durable, non-symlink paths."""
    from account_vault import AccountVault

    return AccountVault(
        data_dir=DATA / "account_vault",
        outlook_file=account_pool_path("outlook_accounts.txt"),
        icloud_file=account_pool_path("icloud_accounts.txt"),
    )


def replace_console_vault(vault, text: str) -> dict[str, Any]:
    """Keep replacement imports available after a future core sync."""
    if hasattr(vault, "replace_text"):
        return vault.replace_text(text)
    from account_vault import parse_import_text

    # Validate before touching existing credentials. The fallback supports an
    # older workbench Vault implementation that predates replace_text().
    parse_import_text(text)
    for account in vault.list_accounts():
        vault.delete(account.id)
    return vault.import_text(text, update_existing=True)


def load_pool_records(*, include_success: bool = False) -> dict[str, str]:
    try:
        records = {
            account.email.strip().casefold(): account.raw_line
            for account in console_vault().list_accounts()
            if account.email.strip()
        }
        if records:
            return records
    except Exception:
        # Keep legacy files readable during an interrupted first-time migration.
        pass
    records: dict[str, str] = {}
    for name in ("outlook_accounts.txt", "icloud_accounts.txt"):
        path = account_pool_path(name)
        if not path.exists():
            continue
        for line in clean_account_lines(path.read_text(encoding="utf-8", errors="ignore")):
            records.setdefault(line.split("----", 1)[0].strip().lower(), line)
    if include_success:
        success = CORE / "output" / "outlook_accounts_success.txt"
        if success.exists():
            for line in clean_account_lines(success.read_text(encoding="utf-8", errors="ignore")):
                records.setdefault(line.split("----", 1)[0].strip().lower(), line)
    return records


def pool_registry() -> AccountRegistry:
    return AccountRegistry(DATA / "pool_state.db")


def sync_pool_registry() -> AccountRegistry:
    registry = pool_registry()
    registry.sync_pool(load_pool_records())
    return registry


def enrich_accounts(rows: list[str]) -> tuple[list[str], list[str]]:
    pool = load_pool_records(include_success=True)
    enriched: list[str] = []
    missing: list[str] = []
    for row in rows:
        parts = row.split("----")
        if len(parts) >= 4 and parts[3].strip():
            enriched.append(row)
            continue
        if len(parts) == 2 and parts[1].strip().startswith(("http://", "https://")):
            enriched.append(row)
            continue
        email = parts[0].strip().lower()
        match = pool.get(email)
        if match:
            enriched.append(match)
        else:
            enriched.append(row)
            missing.append(email)
    return enriched, missing


def normalize_proxy(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text and text.count(":") == 3:
        host, port, user, password = text.split(":", 3)
        text = f"http://{urllib.parse.quote(user)}:{urllib.parse.quote(password)}@{host}:{port}"
    elif "://" not in text:
        text = "http://" + text
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https", "socks4", "socks5", "socks5h"}:
        raise ValueError("代理协议仅支持 http/https/socks4/socks5/socks5h")
    if not parsed.hostname or not parsed.port:
        raise ValueError("代理地址缺少 host 或 port")
    return text


def managed_proxy(region: str, session_id: str, settings: dict[str, Any]) -> str:
    host = str(settings.get("proxy_host") or "").strip()
    port = int(settings.get("proxy_port") or 0)
    user = str(settings.get("proxy_user") or "").strip()
    password = str(settings.get("proxy_password") or "")
    if not (host and port and user):
        raise ValueError("托管代理尚未配置 host / port / user")
    region = (region or "JP").upper()
    if "-region-" in user:
        user = re.sub(r"-region-[A-Za-z]{2}", f"-region-{region}", user)
    else:
        user = f"{user}-region-{region}"
    user = re.sub(r"-session-[A-Za-z0-9]+(?:-sessTime-\d+)?(?:-sessAuto-\d+)?", "", user)
    user = f"{user}-session-{session_id}-sessTime-10-sessAuto-1"
    return f"http://{urllib.parse.quote(user)}:{urllib.parse.quote(password)}@{host}:{port}"


def safe_tail(path: Path, limit: int = 400) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return list(deque((line.rstrip("\n") for line in handle), maxlen=max(1, min(limit, 4000))))


def read_results(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for raw in text.splitlines():
        try:
            item = json.loads(raw)
            if isinstance(item, dict):
                rows.append(item)
        except ValueError:
            continue
    return rows


class Job:
    def __init__(self, meta: dict[str, Any]):
        self.meta = meta
        self.id = str(meta["id"])
        self.proc: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()

    @property
    def meta_path(self) -> Path:
        return JOB_DIR / f"{self.id}.json"

    @property
    def log_path(self) -> Path:
        return Path(self.meta["log_path"])

    @property
    def result_path(self) -> Path:
        return Path(self.meta["result_path"])

    def save(self) -> None:
        atomic_json(self.meta_path, self.meta)

    def start(self, command: list[str], env: dict[str, str]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(
            f"=== Free Console task {self.id} · {self.meta['created_at']} ===\n",
            encoding="utf-8",
        )
        log_handle = self.log_path.open("a", encoding="utf-8", buffering=1)
        child_env = os.environ.copy()
        child_env.update(env)
        child_env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            command,
            cwd=str(CORE),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.meta.update({"pid": self.proc.pid, "state": "running", "started_at": utc_now()})
        self.save()
        threading.Thread(target=self._wait, args=(log_handle,), daemon=True).start()

    def _wait(self, log_handle) -> None:
        assert self.proc is not None
        rc = self.proc.wait()
        log_handle.write(f"=== process finished rc={rc} ===\n")
        log_handle.close()
        final_state = "completed" if rc == 0 else ("stopped" if self.meta.get("stop_requested") else "failed")
        with self.lock:
            self.meta.update({
                "rc": rc,
                "state": final_state,
                "finished_at": utc_now(),
            })
            self.save()
        try:
            registry = pool_registry()
            task_results = read_results(self.result_path)
            registry.record_registered_results(self.id, task_results)
            if self.meta.get("pool_batch"):
                registry.record_job_results(self.id, task_results, final_state=final_state)
        except Exception as exc:
            with self.lock:
                self.meta["pool_sync_error"] = f"{type(exc).__name__}: {exc}"
                self.save()
        for path in self.meta.get("cleanup_paths", []):
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.meta["stop_requested"] = True
            self.save()
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def snapshot(self, tail: int = 0) -> dict[str, Any]:
        results = read_results(self.result_path)
        total = int(self.meta.get("account_count") or 0)
        success = sum(1 for row in results if row.get("ok"))
        failed = sum(1 for row in results if not row.get("ok"))
        state = self.meta.get("state", "queued")
        if self.proc is not None and self.proc.poll() is None:
            state = "running"
        progress = round(min(100, len(results) * 100 / total)) if total else 0
        if state == "completed":
            progress = 100
        payload = {
            **{key: value for key, value in self.meta.items() if key not in {"command"}},
            "state": state,
            "progress": progress,
            "completed_count": len(results),
            "success_count": success,
            "failed_count": failed,
            "duration_seconds": self._duration(),
        }
        if tail:
            payload["logs"] = safe_tail(self.log_path, tail)
            payload["results"] = [public_result(row) for row in results]
        return payload

    def _duration(self) -> int:
        try:
            started = datetime.fromisoformat(str(self.meta.get("started_at", "")).replace("Z", "+00:00"))
        except ValueError:
            return 0
        end = datetime.now(timezone.utc)
        if self.meta.get("finished_at"):
            try:
                end = datetime.fromisoformat(str(self.meta["finished_at"]).replace("Z", "+00:00"))
            except ValueError:
                pass
        return max(0, int((end - started).total_seconds()))


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
HEALTH_PIPELINE_LOCK = threading.Lock()
STATUS_POLLER: StatusPoller | None = None
STATUS_POLLER_LOCK = threading.Lock()


def status_poll_config_path() -> Path:
    return DATA / "status_poll.json"


def registered_pool_emails(registry: AccountRegistry) -> list[str]:
    """Return every registered pool address without imposing a UI page limit."""
    emails: list[str] = []
    offset = 0
    while True:
        rows, total = registry.list_accounts(state="registered", limit=1000, offset=offset)
        emails.extend(str(row.get("email") or "").casefold() for row in rows if row.get("email"))
        offset += len(rows)
        if not rows or offset >= total:
            break
    return emails


def normalize_token_only_probe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep an AT rejection from being mistaken for account deactivation.

    The automatic pass deliberately has no mailbox-login path. A 401/403 from
    an old access token proves only that this credential no longer works; it
    cannot establish that the underlying account has been deleted.
    """
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        saved = dict(row)
        status = str(saved.get("status") or "").strip().lower()
        evidence = " ".join(str(saved.get(field) or "").lower() for field in (
            "error", "probe_error", "ban_reason",
        ))
        source = str(saved.get("deactivation_source") or "").strip().lower()
        protocol_deactivation = (
            source in {"protocol", "protocol_login", "protocol-login"}
            or "protocol_login" in evidence
            or "deleted or deactivated" in evidence
        )
        is_at_rejection = (
            (bool(saved.get("at_probe_used")) or "at_probe_failed" in evidence)
            and ("http_401" in evidence or "http_403" in evidence)
        )
        if status == "account_deactivated" and is_at_rejection and not protocol_deactivation:
            code = "403" if "403" in evidence else ("401" if "401" in evidence else "")
            saved["status"] = "token_dead"
            saved["probe_status"] = "token_dead"
            saved["probe_error"] = f"access_token_rejected: HTTP {code}".rstrip()
            saved["error"] = saved["probe_error"]
            for field in ("ban_reason", "banned_at", "last_banned_at", "deactivation_source"):
                saved.pop(field, None)
        normalized.append(saved)
    return normalized


def merged_poll_rows(plan_path: Path, emails: list[str], fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read the durable poll snapshot without letting stale rows replace this run."""
    try:
        snapshot = json.loads(plan_path.read_text(encoding="utf-8"))
        persisted = snapshot.get("results") if isinstance(snapshot, dict) else []
    except (OSError, ValueError):
        persisted = []
    persisted_by_email = {
        str(row.get("email") or "").strip().casefold(): row
        for row in persisted
        if isinstance(row, dict) and str(row.get("email") or "").strip()
    }
    fallback_by_email = {
        str(row.get("email") or "").strip().casefold(): row
        for row in fallback
        if isinstance(row, dict) and str(row.get("email") or "").strip()
    }
    return [
        dict(persisted_by_email.get(email.casefold()) or fallback_by_email.get(email.casefold()) or {"email": email})
        for email in emails
    ]


def run_token_only_status_poll(config: dict[str, object]) -> dict[str, object]:
    """Check registered accounts by Codex RT or an existing AT only.

    This deliberately uses no Outlook/Relay protocol fallback.  The explicit
    candidate pre-filter matters because ``bulk_check(emails=...)`` otherwise
    keeps placeholder rows without a token in its selected slice.
    """
    with HEALTH_PIPELINE_LOCK:
        registry = sync_pool_registry()
        registered = registered_pool_emails(registry)
        summary: dict[str, object] = {
            "registered_total": len(registered),
            "eligible": 0,
            "skipped_without_token": len(registered),
            "updated": 0,
            "mode": "codex_rt_then_at",
            "protocol_login": False,
            "network_route": "managed_proxy" if health_check_proxy() else "direct",
        }
        if not registered:
            summary.update({
                "total": 0,
                "plus": 0,
                "k12": 0,
                "free": 0,
                "plus_expired": 0,
                "errors": 0,
                "tier1": 0,
                "tier2": 0,
                "tier3": 0,
            })
            return summary

        import plan_check

        candidates = plan_check._gather_candidates(emails=registered)
        eligible = [
            str(candidate.get("email") or "").casefold()
            for candidate in candidates
            if candidate.get("access_token") or candidate.get("refresh_token")
        ]
        eligible = list(dict.fromkeys(email for email in eligible if email))
        summary["eligible"] = len(eligible)
        summary["skipped_without_token"] = max(0, len(registered) - len(eligible))

        result = plan_check.bulk_check(
            emails=eligible,
            only_with_token=False,
            refresh_first=bool(config.get("refresh_codex_rt", True)),
            use_browser_fallback=False,
            concurrency=max(1, min(int(config.get("concurrency") or 4), 8)),
            proxy=health_check_proxy() or None,
            log_fn=lambda message: app.logger.info("[status-poll] %s", message),
        )
        result = {
            **result,
            "results": normalize_token_only_probe_rows([
                row for row in result.get("results") or [] if isinstance(row, dict)
            ]),
        }
        result_path = plan_check.write_results(result)
        merged_rows = normalize_token_only_probe_rows(merged_poll_rows(
            result_path,
            eligible,
            [row for row in result.get("results") or [] if isinstance(row, dict)],
        ))
        summary["updated"] = registry.apply_health_results(merged_rows)
        summary.update({
            key: int(result.get(key, 0) or 0)
            for key in (
                "total", "plus", "k12", "free", "plus_expired",
                "errors", "tier1", "tier2", "tier3",
            )
        })
        return summary


def get_status_poller() -> StatusPoller:
    """Build one durable scheduler for the active standalone data directory."""
    global STATUS_POLLER
    config_path = status_poll_config_path()
    with STATUS_POLLER_LOCK:
        if STATUS_POLLER is not None and STATUS_POLLER.config_path != config_path:
            STATUS_POLLER.stop(timeout=0.2)
            STATUS_POLLER = None
        if STATUS_POLLER is None:
            STATUS_POLLER = StatusPoller(
                run_callback=run_token_only_status_poll,
                config_path=config_path,
            )
        return STATUS_POLLER


def start_status_poller() -> None:
    if os.environ.get("FREE_CONSOLE_DISABLE_STATUS_POLLER", "0") != "1":
        get_status_poller().start()


def load_jobs() -> None:
    for path in sorted(JOB_DIR.glob("*.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            if meta.get("state") == "running":
                meta["state"] = "interrupted"
                meta["finished_at"] = utc_now()
                result_value = str(meta.get("result_path") or "")
                task_results = read_results(Path(result_value)) if result_value else []
                registry = pool_registry()
                registry.record_registered_results(str(meta.get("id") or ""), task_results)
                if meta.get("pool_batch"):
                    # Reconcile completed rows before releasing only the rows
                    # that never produced a result during the interrupted run.
                    registry.record_job_results(
                        str(meta.get("id") or ""),
                        task_results,
                        final_state="interrupted",
                    )
                atomic_json(path, meta)
            JOBS[str(meta["id"])] = Job(meta)
        except (OSError, ValueError, KeyError):
            continue


def public_result(row: dict[str, Any]) -> dict[str, Any]:
    trial = row.get("free_trial") if isinstance(row.get("free_trial"), dict) else {}
    return {
        "email": row.get("email", ""),
        "ok": bool(row.get("ok")),
        "status": row.get("status") or ("failed" if not row.get("ok") else "registered"),
        "method": row.get("method", ""),
        "protocol_engine": row.get("protocol_engine", ""),
        "proxy_region": row.get("proxy_region", ""),
        "duration_ms": int(row.get("duration_ms") or 0),
        "registered_at": row.get("registered_at", ""),
        "phone": row.get("phone", ""),
        "plan_type": row.get("plan_type", ""),
        "trial_eligible": trial.get("eligible", row.get("free_trial_eligible")),
        "trial_status": trial.get("status", row.get("free_trial_status", "")),
        "agent_identity_ok": bool(row.get("agent_identity_ok")),
        "registration_ok": bool(row.get("registration_ok")),
        "phone_bind_ok": row.get("phone_bind_ok"),
        "error": row.get("error") or row.get("bind_error") or row.get("agent_identity_error") or "",
    }


load_jobs()


@app.before_request
def _ensure_status_poller() -> None:
    # Also cover WSGI hosts that defer application initialization until their
    # first request.
    start_status_poller()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def api_health():
    checks = {
        "runner": (CORE / "scripts" / "run_email_proto_register.py").exists(),
        "runtime": (CORE / "chatgpt_register.py").exists(),
        "writable": os.access(OUTPUT, os.W_OK),
        "curl_cffi": importlib.util.find_spec("curl_cffi") is not None,
        "httpx": importlib.util.find_spec("httpx") is not None,
        "cryptography": importlib.util.find_spec("cryptography") is not None,
    }
    return jsonify({
        "ok": all(checks.values()),
        "service": "gptfree-register",
        "version": "0.1.0",
        "time": utc_now(),
        "local_time": local_now(),
        "timezone": display_timezone_name(),
        "checks": checks,
        "dry_run": os.environ.get("FREE_CONSOLE_DRY_RUN", "0") == "1",
    })


@app.route("/api/settings", methods=["GET", "PATCH"])
def api_settings():
    current = load_settings()
    if request.method == "PATCH":
        body = request.get_json(silent=True) or {}
        for key in DEFAULT_SETTINGS:
            if key not in body:
                continue
            value = body[key]
            if key in SECRET_KEYS and value in (None, "", "••••••••") and current.get(key):
                continue
            current[key] = str(value or "").strip()
        atomic_json(SETTINGS_FILE, current)
        apply_process_timezone(current)
    public = dict(current)
    configured = {key: bool(public.get(key)) for key in SECRET_KEYS}
    for key in SECRET_KEYS:
        public[key] = ""
    return jsonify({"ok": True, "settings": public, "secret_configured": configured})


@app.get("/api/accounts/pool")
def api_accounts_pool():
    registry = sync_pool_registry()
    records = load_pool_records()
    emails = [line.split("----", 1)[0] for line in records.values()]
    return jsonify({
        "ok": True,
        "count": len(emails),
        "emails": emails,
        "summary": registry.summary(),
    })


@app.get("/api/accounts")
def api_accounts_list():
    registry = sync_pool_registry()
    rows, total = registry.list_accounts(
        state=str(request.args.get("state") or ""),
        query=str(request.args.get("q") or ""),
        limit=int(request.args.get("limit") or 100),
        offset=int(request.args.get("offset") or 0),
    )
    return jsonify({"ok": True, "accounts": rows, "count": total, "summary": registry.summary()})


@app.get("/api/accounts/survival")
def api_accounts_survival():
    registry = sync_pool_registry()
    registry.refresh_observed_seconds()
    rows, total = registry.list_accounts(
        state="registered",
        limit=max(1, min(int(request.args.get("limit") or 500), 1000)),
    )
    return jsonify({"ok": True, "accounts": rows, "count": total, "summary": registry.summary()})


@app.route("/api/accounts/status-poll", methods=["GET", "PATCH"])
def api_account_status_poll():
    poller = get_status_poller()
    poller.start()
    if request.method == "PATCH":
        body = request.get_json(silent=True) or {}
        try:
            status = poller.update_config(body)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "status_poll": status})
    return jsonify({"ok": True, "status_poll": poller.status()})


@app.post("/api/accounts/status-poll/run")
def api_account_status_poll_run():
    poller = get_status_poller()
    queued = poller.run_now()
    status = poller.status()
    if not queued:
        return jsonify({
            "ok": False,
            "error": "自动轮询正在运行",
            "status_poll": status,
            "queued": False,
        }), 409
    return jsonify({"ok": True, "queued": True, "status_poll": status})


@app.post("/api/accounts/import")
def api_accounts_import():
    body = request.get_json(silent=True) or {}
    text = str(body.get("lines") or "")
    if not text.strip():
        return jsonify({"ok": False, "error": "没有识别到有效账号行"}), 400
    replace = str(body.get("mode") or "append") == "replace"
    try:
        from account_vault import ImportValidationError

        if replace and sync_pool_registry().summary()["reserved"]:
            return jsonify({
                "ok": False,
                "error": "有账号池任务正在运行，不能替换导入；请等待任务结束后再操作",
            }), 409
        vault = console_vault()
        result = replace_console_vault(vault, text) if replace else vault.import_text(text, update_existing=True)
        registry = sync_pool_registry()
        if replace:
            registry.prune_unregistered_missing_credentials(load_pool_records().keys())
    except ImportValidationError as exc:
        return jsonify({"ok": False, "error": "账号格式校验失败", "details": exc.errors[:20]}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
    return jsonify({
        "ok": True,
        "imported": int(result.get("total") or 0),
        "outlook": int((result.get("added_by_kind") or {}).get("outlook", 0)) + int((result.get("updated_by_kind") or {}).get("outlook", 0)),
        "relay": int((result.get("added_by_kind") or {}).get("icloud", 0)) + int((result.get("updated_by_kind") or {}).get("icloud", 0)),
        "added": int(result.get("added") or 0),
        "updated": int(result.get("updated") or 0),
        "duplicates": int(result.get("dup") or 0),
        "summary": registry.summary(),
    })


@app.post("/api/proxy/test")
def api_proxy_test():
    body = request.get_json(silent=True) or {}
    try:
        proxy = normalize_proxy(body.get("proxy", ""))
        if not proxy and body.get("managed_region"):
            proxy = managed_proxy(str(body["managed_region"]), f"{secrets.randbelow(100_000_000):08d}", load_settings())
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not proxy:
        return jsonify({"ok": False, "error": "请填写代理地址"}), 400
    transport = proxy.replace("socks5://", "socks5h://", 1)
    try:
        with httpx.Client(proxy=transport, timeout=15, trust_env=False) as client:
            ip = client.get("https://api.ipify.org").text.strip()
            geo = client.get(f"http://ip-api.com/json/{ip}", timeout=10).json()
        return jsonify({
            "ok": True,
            "ip": ip,
            "country": geo.get("country", ""),
            "country_code": geo.get("countryCode", ""),
            "region": geo.get("regionName", ""),
            "city": geo.get("city", ""),
            "isp": geo.get("isp", ""),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502


@app.post("/api/sms/test")
def api_sms_test():
    code = (
        "import json; from sms_provider import get_sms_provider; "
        "p=get_sms_provider(purpose='openai'); "
        "print(json.dumps({'ok':bool(p),'balance':p.get_balance() if p else None}))"
    )
    try:
        result = subprocess.run(
            [PYTHON, "-c", code], cwd=CORE, env={**os.environ, **settings_env()},
            capture_output=True, text=True, timeout=25,
        )
        last = (result.stdout.strip().splitlines() or [""])[-1]
        payload = json.loads(last)
        payload["provider"] = load_settings().get("free_sms_provider", "")
        return jsonify(payload), (200 if payload.get("ok") else 400)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502


@app.post("/api/runs")
def api_create_run():
    body = request.get_json(silent=True) or {}
    rows = clean_account_lines(body.get("accounts", ""))
    pool_batch = False
    pool_selected: list[str] = []
    pool_retry_failed = bool(body.get("pool_retry_failed"))
    if body.get("use_pool"):
        registry = sync_pool_registry()
        requested = [row.split("----", 1)[0].strip().casefold() for row in rows]
        requested = [email for email in requested if email]
        default_size = len(requested) if requested else 20
        try:
            batch_size = max(1, min(int(body.get("pool_batch_size") or default_size), 500))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "账号池批次大小必须是 1 到 500"}), 400
        pool_selected = registry.choose_batch(
            count=batch_size,
            requested=requested,
            include_failed=pool_retry_failed,
        )
        if requested and len(pool_selected) != min(batch_size, len(requested)):
            available = registry.lookup(requested)
            unavailable = [email for email in requested if email not in pool_selected]
            detail = [f"{email} ({available.get(email, {}).get('state', 'not_found')})" for email in unavailable[:10]]
            return jsonify({"ok": False, "error": "选中的账号不在可注册备用池", "unavailable": detail}), 400
        if not pool_selected:
            return jsonify({"ok": False, "error": "备用池中没有可领取的账号"}), 400
        pool = load_pool_records()
        missing_pool = [email for email in pool_selected if email not in pool]
        if missing_pool:
            return jsonify({"ok": False, "error": "账号池凭据与状态库不同步", "missing": missing_pool[:20]}), 400
        rows = [pool[email] for email in pool_selected]
        pool_batch = True
    rows, missing = enrich_accounts(rows)
    if not rows:
        return jsonify({"ok": False, "error": "请添加至少一个账号"}), 400
    if missing:
        return jsonify({
            "ok": False,
            "error": "这些邮箱缺少 Outlook refresh token 或 relay URL",
            "missing": missing[:20],
        }), 400
    if len(rows) > 500:
        return jsonify({"ok": False, "error": "单个任务最多 500 个账号"}), 400

    method = str(body.get("method") or "protocol").lower()
    if method not in {"protocol", "browser"}:
        return jsonify({"ok": False, "error": "method 参数错误"}), 400
    engine = "mail_auth"
    browser = str(body.get("browser") or "bitbrowser").lower()
    if browser not in {"bitbrowser", "roxy", "chromium"}:
        return jsonify({"ok": False, "error": "browser 参数错误"}), 400
    workers = int(body.get("workers") or (3 if method == "protocol" else 6))
    workers = max(1, min(workers, 32 if method == "protocol" else 6))
    agent_identity = bool(body.get("agent_identity")) and method == "protocol"
    bind_phone = bool(body.get("bind_phone")) and not agent_identity
    sms_source = str(body.get("sms_source") or "platform").lower()
    if sms_source not in {"platform", "manual", "none"}:
        return jsonify({"ok": False, "error": "sms_source 参数错误"}), 400
    if sms_source == "none":
        bind_phone = False
    phone_lines = str(body.get("phone_lines") or "").strip()
    if bind_phone and sms_source == "manual" and not phone_lines:
        return jsonify({"ok": False, "error": "手动接码需要 phone----sms_api_url"}), 400

    job_id = f"free_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    account_file = INPUT_DIR / f"{job_id}_accounts.txt"
    account_file.write_text("\n".join(rows) + "\n", encoding="utf-8")
    os.chmod(account_file, 0o600)
    cleanup = [str(account_file)]
    result_path = RESULT_DIR / f"{job_id}.jsonl"
    log_path = OUTPUT / "jobs" / f"{job_id}.log"

    proxy_mode = str(body.get("proxy_mode") or "single")
    proxy_arg: list[str] = []
    proxy_label = "direct"
    if proxy_mode == "single":
        try:
            proxy = normalize_proxy(body.get("proxy", ""))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if proxy:
            proxy_arg = ["--proxy", proxy]
            proxy_label = "single"
    elif proxy_mode in {"managed_jp", "managed_us", "pool"}:
        pool_rows: list[str] = []
        if proxy_mode == "pool":
            for raw in str(body.get("proxy_pool") or "").splitlines():
                value = raw.strip()
                if not value or value.startswith("#"):
                    continue
                region, proxy_value = (value.split("|", 1) if "|" in value else ("POOL", value))
                pool_rows.append(f"{region.upper()}|{normalize_proxy(proxy_value)}")
        else:
            region = "JP" if proxy_mode == "managed_jp" else "US"
            settings = load_settings()
            try:
                for _ in range(100):
                    sid = f"{secrets.randbelow(100_000_000):08d}"
                    pool_rows.append(f"{region}|{managed_proxy(region, sid, settings)}")
            except (ValueError, TypeError) as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
        if not pool_rows:
            return jsonify({"ok": False, "error": "代理池为空"}), 400
        pool_file = INPUT_DIR / f"{job_id}_proxies.txt"
        pool_file.write_text("\n".join(pool_rows) + "\n", encoding="utf-8")
        os.chmod(pool_file, 0o600)
        cleanup.append(str(pool_file))
        proxy_arg = ["--proxy-pool-file", str(pool_file)]
        proxy_label = f"{proxy_mode}:{len(pool_rows)}"
    else:
        return jsonify({"ok": False, "error": "proxy_mode 参数错误"}), 400

    runner = CORE / "scripts" / "run_email_proto_register.py"
    command = [
        PYTHON, "-u", str(runner),
        "--emails-file", str(account_file),
        "--out", str(result_path),
        "--method", method,
        "--protocol-engine", engine,
        "--browser", browser,
        "--workers", str(workers),
        "--otp-timeout", str(max(30, min(int(body.get("otp_timeout") or 90), 900))),
        "--sms-source", sms_source,
        *proxy_arg,
    ]
    if body.get("no_password"):
        command.append("--no-password")
    if agent_identity:
        command.append("--agent-identity")
        if body.get("sub2api_export"):
            command.append("--sub2api-export")
        if body.get("sub2api_import"):
            command.append("--sub2api-import")
    if bind_phone:
        command.append("--bind-phone")
        command += [
            "--sms-workers", str(max(1, min(int(body.get("sms_workers") or 3), workers))),
            "--sms-otp-timeout", str(max(30, min(int(body.get("sms_otp_timeout") or 30), 600))),
            "--sms-max-attempts", str(max(1, min(int(body.get("sms_max_attempts") or 3), 10))),
            "--sms-max-otp-retries", str(max(0, min(int(body.get("sms_max_otp_retries") or 2), 5))),
        ]
        if phone_lines:
            command += ["--phone-lines", phone_lines]
        if sms_source == "platform" or not body.get("bind_use_bitbrowser"):
            command.append("--bind-no-bitbrowser")

    if os.environ.get("FREE_CONSOLE_DRY_RUN", "0") == "1":
        command = [
            PYTHON, str(ROOT / "scripts" / "fake_runner.py"),
            "--accounts-file", str(account_file), "--out", str(result_path),
            "--method", method, "--workers", str(workers),
        ]

    meta = {
        "id": job_id,
        "label": str(body.get("name") or f"Free · {method} · {len(rows)} accounts")[:80],
        "created_at": utc_now(),
        "started_at": "",
        "finished_at": "",
        "state": "queued",
        "rc": None,
        "pid": None,
        "method": method,
        "protocol_engine": engine if method == "protocol" else "",
        "browser": browser if method == "browser" else "",
        "workers": workers,
        "account_count": len(rows),
        "agent_identity": agent_identity,
        "bind_phone": bind_phone,
        "sms_source": sms_source if bind_phone else "none",
        "proxy_label": proxy_label,
        "log_path": str(log_path),
        "result_path": str(result_path),
        "cleanup_paths": cleanup,
        "pool_batch": pool_batch,
        "pool_selected": pool_selected if pool_batch else [],
    }
    if pool_batch:
        reserved = pool_registry().reserve(
            job_id,
            pool_selected,
            include_failed=pool_retry_failed,
        )
        if len(reserved) != len(pool_selected):
            pool_registry().release(job_id)
            for path in cleanup:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
            return jsonify({"ok": False, "error": "账号池已被其他任务占用，请刷新后重试"}), 409
    job = Job(meta)
    with JOBS_LOCK:
        JOBS[job_id] = job
    job.save()
    try:
        job.start(command, settings_env())
    except Exception as exc:
        meta.update({"state": "failed", "finished_at": utc_now(), "error": f"{type(exc).__name__}: {exc}"})
        job.save()
        if pool_batch:
            pool_registry().release(job_id)
        return jsonify({"ok": False, "error": meta["error"]}), 500
    return jsonify({"ok": True, "job": job.snapshot()})


@app.get("/api/jobs")
def api_jobs():
    with JOBS_LOCK:
        rows = [job.snapshot() for job in JOBS.values()]
    rows.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return jsonify({"ok": True, "jobs": rows})


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    return jsonify({"ok": True, "job": job.snapshot(tail=int(request.args.get("tail", 500)))})


@app.post("/api/jobs/<job_id>/stop")
def api_stop_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    job.stop()
    return jsonify({"ok": True})


@app.get("/api/results")
def api_results():
    rows: list[dict[str, Any]] = []
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    registry = sync_pool_registry()
    for job in jobs:
        job_rows = read_results(job.result_path)
        registry.record_registered_results(job.id, job_rows)
        if job.meta.get("pool_batch") and job.meta.get("state") in {"completed", "failed", "stopped", "interrupted"}:
            registry.record_job_results(job.id, job_rows, final_state=str(job.meta.get("state")))
        states = registry.lookup(item.get("email", "") for item in job_rows)
        for item in job_rows:
            public = public_result(item)
            public["job_id"] = job.id
            state = states.get(str(public.get("email") or "").casefold(), {})
            public.update({
                "pool_state": state.get("state", ""),
                "last_checked_at": state.get("last_checked_at", ""),
                "last_confirmed_at": state.get("last_confirmed_at", ""),
                "last_probe_status": state.get("last_probe_status", ""),
                "health_status": state.get("health_status", ""),
                "health_alive": state.get("health_alive"),
                "observed_seconds": state.get("observed_seconds"),
                "health_error": state.get("check_error", ""),
            })
            rows.append(public)
    rows.sort(key=lambda item: item.get("registered_at", ""), reverse=True)
    return jsonify({"ok": True, "count": len(rows), "results": rows})


@app.get("/api/jobs/<job_id>/download/<kind>")
def api_download(job_id: str, kind: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    path = job.result_path if kind == "results" else job.log_path if kind == "log" else None
    if not path or not path.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


if "pytest" not in sys.modules:
    # The documented local Gunicorn runtime uses one worker, so this process is
    # the sole owner of scheduler state and the merged plan-check result file.
    start_status_poller()


if __name__ == "__main__":
    host = "127.0.0.1"
    port = int(os.environ.get("FREE_CONSOLE_PORT", "8866"))
    app.run(host=host, port=port, debug=False, threaded=True)
