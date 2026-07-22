from __future__ import annotations

import json
import sqlite3
import sys
import time
import types

import pytest

import app as console
from account_registry import AccountRegistry


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    output = tmp_path / "output"
    monkeypatch.setattr(console, "DATA", data)
    monkeypatch.setattr(console, "JOB_DIR", data / "jobs")
    monkeypatch.setattr(console, "INPUT_DIR", data / "inputs")
    monkeypatch.setattr(console, "OUTPUT", output)
    monkeypatch.setattr(console, "RESULT_DIR", output / "results")
    monkeypatch.setattr(console, "SETTINGS_FILE", data / "settings.json")
    for directory in (console.JOB_DIR, console.INPUT_DIR, console.OUTPUT, console.RESULT_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    console.JOBS.clear()
    monkeypatch.setenv("FREE_CONSOLE_DRY_RUN", "1")
    monkeypatch.setenv("FREE_CONSOLE_DISABLE_STATUS_POLLER", "1")
    monkeypatch.delenv("FREE_CONSOLE_PASSWORD", raising=False)
    with console.app.test_client() as test_client:
        yield test_client
    console.JOBS.clear()
    if console.STATUS_POLLER is not None:
        console.STATUS_POLLER.stop(timeout=0.2)
    console.STATUS_POLLER = None


def wait_for_state(client, path, *, timeout: float = 5):
    deadline = time.time() + timeout
    payload = None
    while time.time() < deadline:
        payload = client.get(path).get_json()
        item = payload.get("job") or {}
        if item.get("state") not in {"queued", "running"}:
            return item
        time.sleep(0.05)
    return (payload or {}).get("job") or {}


def test_health_reports_standalone_runtime(client):
    response = client.get("/api/health")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["checks"]["runner"] is True
    assert payload["dry_run"] is True


def test_password_protects_console_but_not_healthcheck(client, monkeypatch):
    monkeypatch.setenv("FREE_CONSOLE_PASSWORD", "top-secret")
    assert client.get("/").status_code == 401
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/health").status_code == 200
    token = __import__("base64").b64encode(b"operator:top-secret").decode()
    assert client.get("/api/jobs", headers={"Authorization": f"Basic {token}"}).status_code == 200


def test_secret_settings_are_never_returned(client):
    response = client.patch("/api/settings", json={
        "free_sms_api_key": "secret-value",
        "free_sms_country": "145",
    })
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["settings"]["free_sms_api_key"] == ""
    assert payload["secret_configured"]["free_sms_api_key"] is True
    assert payload["settings"]["free_sms_country"] == "145"
    assert "secret-value" not in response.get_data(as_text=True)


def test_env_template_does_not_erase_saved_ui_setting(client, monkeypatch):
    client.patch("/api/settings", json={
        "proxy_host": "proxy.saved.example",
        "proxy_port": "10000",
        "proxy_user": "saved-user",
    })
    monkeypatch.setenv("PROXY_HOST", "")
    assert console.load_settings()["proxy_host"] == "proxy.saved.example"
    monkeypatch.setenv("PROXY_HOST", "proxy.env.example")
    assert console.load_settings()["proxy_host"] == "proxy.saved.example"


def test_health_proxy_is_derived_from_the_saved_proxy_settings() -> None:
    proxy = console.health_check_proxy({
        "proxy_host": "proxy.example.test",
        "proxy_port": "10000",
        "proxy_user": "user:name",
        "proxy_password": "pass@word",
    })

    assert proxy == "http://user%3Aname:pass%40word@proxy.example.test:10000"
    env = console.settings_env({
        "proxy_host": "proxy.example.test",
        "proxy_port": "10000",
        "proxy_user": "user:name",
        "proxy_password": "pass@word",
        "display_timezone": "Asia/Shanghai",
    })
    assert env["FREE_CONSOLE_HEALTH_PROXY"] == proxy


def test_dry_run_job_lifecycle(client):
    response = client.post("/api/runs", json={
        "name": "pytest smoke",
        "accounts": (
            "one@example.com----mail-pass----client----refresh\n"
            "two@example.com----mail-pass----client----refresh"
        ),
        "method": "protocol",
        "protocol_engine": "mail_auth",
        "workers": 2,
        "proxy_mode": "single",
        "proxy": "",
        "agent_identity": True,
        "sms_source": "none",
    })
    assert response.status_code == 200
    job_id = response.get_json()["job"]["id"]

    deadline = time.time() + 5
    job = None
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}?tail=50").get_json()["job"]
        if job["state"] != "running":
            break
        time.sleep(0.05)

    assert job is not None
    assert job["state"] == "completed"
    assert job["dry_run"] is True
    assert job["progress"] == 100
    assert job["success_count"] == 2
    assert job["failed_count"] == 0
    assert any("Agent Identity" in line for line in job["logs"])

    results = client.get("/api/results").get_json()["results"]
    assert {row["email"] for row in results} == {"one@example.com", "two@example.com"}
    assert all("access_token" not in row for row in results)
    assert all(row["dry_run"] is True for row in results)
    assert client.get("/api/accounts").get_json()["summary"]["registered"] == 0


def test_pool_import_batch_reservation_and_result_lifecycle(client):
    imported = client.post("/api/accounts/import", json={
        "lines": (
            "first@example.com----mail-pass----client----refresh-one\n"
            "second@example.com----mail-pass----client----refresh-two\n"
        ),
        "mode": "replace",
    })
    assert imported.status_code == 200
    assert imported.get_json()["imported"] == 2

    pool = client.get("/api/accounts/pool").get_json()
    assert pool["summary"]["spare"] == 2

    response = client.post("/api/runs", json={
        "name": "pool batch",
        "accounts": "",
        "use_pool": True,
        "pool_batch_size": 1,
        "method": "protocol",
        "protocol_engine": "mail_auth",
        "workers": 1,
        "proxy_mode": "single",
        "agent_identity": True,
        "sms_source": "none",
    })
    assert response.status_code == 200
    job_id = response.get_json()["job"]["id"]

    deadline = time.time() + 5
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()["job"]
        if job["state"] != "running":
            break
        time.sleep(0.05)
    assert job["state"] == "completed"

    accounts = client.get("/api/accounts").get_json()
    assert accounts["summary"]["spare"] == 2
    assert accounts["summary"]["registered"] == 0


def test_status_poll_configuration_and_empty_immediate_run(client):
    initial = client.get("/api/accounts/status-poll")
    assert initial.status_code == 200
    assert initial.get_json()["status_poll"]["protocol_login"] is False

    updated = client.patch("/api/accounts/status-poll", json={
        "enabled": False,
        "interval_minutes": 30,
        "concurrency": 2,
        "refresh_codex_rt": False,
    })
    assert updated.status_code == 200
    poll = updated.get_json()["status_poll"]
    assert poll["enabled"] is False
    assert poll["interval_minutes"] == 30
    assert poll["concurrency"] == 2
    assert poll["refresh_codex_rt"] is False
    assert json.loads((console.DATA / "status_poll.json").read_text(encoding="utf-8"))["concurrency"] == 2

    run = client.post("/api/accounts/status-poll/run")
    assert run.status_code == 200
    deadline = time.time() + 2
    while time.time() < deadline:
        poll = client.get("/api/accounts/status-poll").get_json()["status_poll"]
        if not poll["running"] and poll["last_finished_at"]:
            break
        time.sleep(0.02)
    assert poll["last_summary"]["registered_total"] == 0
    assert poll["last_summary"]["eligible"] == 0
    assert poll["last_summary"]["protocol_login"] is False


def test_token_only_status_poll_filters_registered_accounts_and_never_uses_protocol(client, tmp_path, monkeypatch):
    registry = console.pool_registry()
    registry.sync_pool({
        "token@example.com": "token@example.com----mail-pass----client----refresh",
        "skip@example.com": "skip@example.com----mail-pass----client----refresh",
        "spare@example.com": "spare@example.com----mail-pass----client----refresh",
    })
    registry.record_registered_results("job_tokens", [
        {"email": "token@example.com", "ok": True, "registered_at": "2026-07-22T00:00:00Z"},
        {"email": "skip@example.com", "ok": True, "registered_at": "2026-07-22T00:00:00Z"},
    ])
    plan_path = tmp_path / "plan_check_results.json"
    calls: dict[str, object] = {}

    def gather(*, emails):
        calls["gather_emails"] = list(emails)
        return [
            {"email": "token@example.com", "refresh_token": "codex-rt", "access_token": ""},
            {"email": "skip@example.com", "refresh_token": "", "access_token": ""},
        ]

    def bulk_check(**kwargs):
        calls["bulk"] = kwargs
        return {
            "total": 1,
            "plus": 0,
            "k12": 0,
            "free": 1,
            "plus_expired": 0,
            "errors": 0,
            "tier1": 1,
            "tier2": 0,
            "tier3": 0,
            "results": [{
                "email": "token@example.com",
                "status": "free",
                "plan_type": "free",
                "tier": 1,
                "last_checked": "2026-07-22T01:00:00Z",
            }],
        }

    def write_results(result):
        plan_path.write_text(json.dumps({"results": result["results"]}), encoding="utf-8")
        return plan_path

    fake_plan_check = types.SimpleNamespace(
        _gather_candidates=gather,
        bulk_check=bulk_check,
        write_results=write_results,
    )
    monkeypatch.setitem(sys.modules, "plan_check", fake_plan_check)

    summary = console.run_token_only_status_poll({"refresh_codex_rt": True, "concurrency": 4})

    assert calls["gather_emails"] == ["skip@example.com", "token@example.com"]
    bulk = calls["bulk"]
    assert isinstance(bulk, dict)
    assert bulk["emails"] == ["token@example.com"]
    assert bulk["only_with_token"] is False
    assert bulk["refresh_first"] is True
    assert bulk["use_browser_fallback"] is False
    assert bulk["concurrency"] == 4
    assert callable(bulk["log_fn"])
    assert summary["registered_total"] == 2
    assert summary["eligible"] == 1
    assert summary["skipped_without_token"] == 1
    assert summary["protocol_login"] is False
    row = registry.lookup(["token@example.com"])["token@example.com"]
    assert row["health_status"] == "free"
    assert row["last_probe_status"] == "free"


def test_token_only_status_poll_skips_bulk_check_when_no_registered_token_exists(client, monkeypatch):
    email = "no-token@example.com"
    registry = console.pool_registry()
    registry.sync_pool({email: f"{email}----mail-pass----client----refresh"})
    registry.record_registered_results("job_no_token", [{"email": email, "ok": True}])

    def gather(*, emails):
        assert emails == [email]
        return [{"email": email, "refresh_token": "", "access_token": ""}]

    def bulk_check(**_kwargs):
        raise AssertionError("bulk_check must not run for an empty eligible token list")

    fake_plan_check = types.SimpleNamespace(
        _gather_candidates=gather,
        bulk_check=bulk_check,
    )
    monkeypatch.setitem(sys.modules, "plan_check", fake_plan_check)

    summary = console.run_token_only_status_poll({"refresh_codex_rt": True, "concurrency": 4})

    assert summary["registered_total"] == 1
    assert summary["eligible"] == 0
    assert summary["skipped_without_token"] == 1
    assert summary["total"] == 0
    assert summary["errors"] == 0


@pytest.mark.parametrize("http_status", [401, 403])
def test_token_only_at_rejection_keeps_existing_confirmed_account_alive(
    client, tmp_path, monkeypatch, http_status,
):
    """An AT rejection is not sufficient evidence to retire an account.

    The automatic poller intentionally has no mailbox-login fallback.  Its
    ``/backend-api/me`` probe can therefore only establish that the saved AT
    is unusable, not that the account was explicitly deactivated.  A prior
    confirmed plan must remain the account state and the UI must get the
    token-specific latest-probe marker instead of a permanent red status.
    """
    email = "at-only@example.com"
    registry = console.pool_registry()
    registry.sync_pool({email: f"{email}----mail-pass----client----refresh"})
    registry.record_registered_results("job_at_only", [{
        "email": email,
        "ok": True,
        "registered_at": "2026-07-22T00:00:00Z",
    }])
    registry.apply_health_results([{
        "email": email,
        "status": "free",
        "plan_type": "free",
        "last_checked": "2026-07-22T01:00:00Z",
    }])
    plan_path = tmp_path / "plan_check_results.json"
    calls: dict[str, object] = {}

    def gather(*, emails):
        assert emails == [email]
        return [{"email": email, "access_token": "stale-at", "refresh_token": ""}]

    def bulk_check(**kwargs):
        calls["bulk"] = kwargs
        return {
            "total": 1,
            "plus": 0,
            "k12": 0,
            "free": 0,
            "plus_expired": 0,
            "errors": 1,
            "tier1": 0,
            "tier2": 0,
            "tier3": 1,
            "results": [{
                "email": email,
                "status": "account_deactivated",
                "tier": 3,
                "at_probe_used": True,
                "ban_reason": f"http_{http_status}",
                "error": f"account_deactivated: at_probe_failed: http_{http_status}",
                "last_checked": "2026-07-22T02:00:00Z",
            }],
        }

    def write_results(result):
        calls["written_result"] = result
        plan_path.write_text(json.dumps({"results": result["results"]}), encoding="utf-8")
        return plan_path

    monkeypatch.setitem(sys.modules, "plan_check", types.SimpleNamespace(
        _gather_candidates=gather,
        bulk_check=bulk_check,
        write_results=write_results,
    ))

    console.run_token_only_status_poll({"refresh_codex_rt": False, "concurrency": 1})

    bulk = calls["bulk"]
    assert isinstance(bulk, dict)
    assert bulk["emails"] == [email]
    assert bulk["use_browser_fallback"] is False
    written = calls["written_result"]
    assert isinstance(written, dict)
    assert written["results"][0]["status"] == "token_dead"
    assert written["results"][0]["probe_status"] == "token_dead"
    row = registry.lookup([email])[email]
    assert row["health_status"] == "free"
    assert row["health_alive"] is True
    assert row["last_confirmed_at"] == "2026-07-22T01:00:00Z"
    assert row["last_probe_status"] == "token_dead"
    assert "access_token_rejected" in row["check_error"]
    assert str(http_status) in row["check_error"]
    assert row["last_banned_at"] == ""


def test_token_only_normalizer_preserves_protocol_deactivation() -> None:
    """A later AT failure must not erase an explicit protocol verdict."""
    rows = console.normalize_token_only_probe_rows([{
        "email": "protocol-verdict@example.com",
        "status": "account_deactivated",
        "deactivation_source": "protocol_login",
        "at_probe_used": True,
        "error": "account_deactivated: protocol_login: deleted or deactivated",
        "probe_error": "token_dead: at_probe_failed: http_403",
    }])

    assert rows[0]["status"] == "account_deactivated"
    assert rows[0]["deactivation_source"] == "protocol_login"


def test_protocol_deep_explicit_deactivation_marks_account_red(client):
    """A direct protocol-login deactivation remains an authoritative result."""
    email = "protocol-deactivated@example.com"
    registry = console.pool_registry()
    registry.sync_pool({email: f"{email}----mail-pass----client----refresh"})
    registry.record_registered_results("job_protocol", [{
        "email": email,
        "ok": True,
        "registered_at": "2026-07-22T00:00:00Z",
    }])
    registry.apply_health_results([{
        "email": email,
        "status": "free",
        "plan_type": "free",
        "last_checked": "2026-07-22T01:00:00Z",
    }])

    # Preserve an explicit deactivation verdict supplied by a prior probe.
    registry.apply_health_results([{
        "email": email,
        "status": "account_deactivated",
        "probe_status": "account_deactivated",
        "tier": 2,
        "error": "account_deactivated: deleted or deactivated",
        "banned_at": "2026-07-22T02:00:00Z",
        "probe_checked_at": "2026-07-22T02:00:00Z",
    }])

    row = registry.lookup([email])[email]
    assert row["health_status"] == "account_deactivated"
    assert row["health_alive"] is False
    assert row["last_probe_status"] == "account_deactivated"
    assert row["last_banned_at"] == "2026-07-22T02:00:00Z"


@pytest.mark.parametrize("token_status", ["token_dead", "rt_revoked", "no_token"])
def test_token_issue_keeps_previous_confirmation_instead_of_downgrading_it(client, token_status):
    """A token problem updates the probe record, not a known live account."""
    email = f"{token_status}@example.com"
    registry = console.pool_registry()
    registry.sync_pool({email: f"{email}----mail-pass----client----refresh"})
    registry.record_registered_results("job_token_history", [{
        "email": email,
        "ok": True,
        "registered_at": "2026-07-22T00:00:00Z",
    }])
    registry.apply_health_results([{
        "email": email,
        "status": "plus",
        "plan_type": "plus",
        "last_checked": "2026-07-22T01:00:00Z",
    }])
    registry.apply_health_results([{
        "email": email,
        "status": token_status,
        "probe_status": token_status,
        "probe_error": f"{token_status}: credential no longer accepted",
        "probe_checked_at": "2026-07-22T02:00:00Z",
    }])

    row = registry.lookup([email])[email]
    assert row["health_status"] == "plus"
    assert row["health_alive"] is True
    assert row["last_confirmed_at"] == "2026-07-22T01:00:00Z"
    assert row["last_checked_at"] == "2026-07-22T02:00:00Z"
    assert row["last_probe_status"] == token_status
    assert row["check_error"] == f"{token_status}: credential no longer accepted"


def test_health_registry_preserves_confirmed_status_for_inconclusive_probe(client):
    registry = console.pool_registry()
    registry.sync_pool({"alive@example.com": "alive@example.com----mail-pass----client----refresh"})
    registry.record_registered_results("job_1", [{
        "email": "alive@example.com",
        "ok": True,
        "registered_at": "2026-07-22T00:00:00Z",
    }])
    registry.apply_health_results([{
        "email": "alive@example.com",
        "status": "plus_expired",
        "plan_type": "plus",
        "last_checked": "2026-07-22T01:00:00Z",
    }])
    registry.apply_health_results([{
        "email": "alive@example.com",
        "status": "error",
        "error": "network: timeout",
        "probe_checked_at": "2026-07-22T02:00:00Z",
    }])

    row = registry.lookup(["alive@example.com"])["alive@example.com"]
    assert row["health_status"] == "plus_expired"
    assert row["health_alive"] is True
    assert row["last_checked_at"] == "2026-07-22T02:00:00Z"
    assert row["last_confirmed_at"] == "2026-07-22T01:00:00Z"
    assert row["last_probe_status"] == "error"
    assert row["check_error"] == "network: timeout"
    assert row["observed_seconds"] == 3600


def test_registered_account_waits_for_confirmation_before_showing_survival_duration(client):
    registry = console.pool_registry()
    registry.sync_pool({"unchecked@example.com": "unchecked@example.com----mail-pass----client----refresh"})
    registry.record_registered_results("job_1", [{
        "email": "unchecked@example.com",
        "ok": True,
        "registered_at": "2026-07-22T00:00:00Z",
    }])

    before = registry.lookup(["unchecked@example.com"])["unchecked@example.com"]
    assert before["observed_seconds"] is None
    assert before["last_confirmed_at"] == ""

    registry.apply_health_results([{
        "email": "unchecked@example.com",
        "status": "error",
        "probe_status": "error",
        "probe_error": "network: timeout",
        "probe_checked_at": "2026-07-22T02:00:00Z",
    }])
    after = registry.lookup(["unchecked@example.com"])["unchecked@example.com"]
    assert after["health_alive"] is None
    assert after["last_probe_status"] == "error"
    assert after["observed_seconds"] is None


def test_registry_migrates_legacy_health_timestamp_as_best_available_confirmation(tmp_path):
    path = tmp_path / "legacy_pool.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE pool_accounts (
                email TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL DEFAULT '',
                imported_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'spare',
                current_job_id TEXT NOT NULL DEFAULT '',
                last_job_id TEXT NOT NULL DEFAULT '',
                registered_at TEXT NOT NULL DEFAULT '',
                registration_status TEXT NOT NULL DEFAULT '',
                registration_error TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT NOT NULL DEFAULT '',
                health_status TEXT NOT NULL DEFAULT '',
                health_alive INTEGER,
                plan_type TEXT NOT NULL DEFAULT '',
                active_until TEXT NOT NULL DEFAULT '',
                check_tier INTEGER NOT NULL DEFAULT 0,
                check_error TEXT NOT NULL DEFAULT '',
                observed_seconds INTEGER,
                last_banned_at TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO pool_accounts(
                email, state, registered_at, last_checked_at, health_status, health_alive
            ) VALUES (
                'legacy@example.com', 'registered',
                '2026-07-22T00:00:00Z', '2026-07-22T01:00:00Z', 'free', 1
            );
        """)

    registry = AccountRegistry(path)
    registry.refresh_observed_seconds()
    row = registry.lookup(["legacy@example.com"])["legacy@example.com"]
    assert row["last_confirmed_at"] == "2026-07-22T01:00:00Z"
    assert row["last_probe_status"] == "free"
    assert row["observed_seconds"] == 3600


def test_legacy_shanghai_timestamp_is_normalized_before_duration_calculation(client):
    registry = console.pool_registry()
    registry.sync_pool({"time@example.com": "time@example.com----mail-pass----client----refresh"})
    registry.record_registered_results("job_time", [{
        "email": "time@example.com",
        "ok": True,
        "registered_at": "2026-07-22T00:00:00Z",
    }])
    registry.apply_health_results([{
        "email": "time@example.com",
        "status": "free",
        "last_checked": "2026-07-22 09:00:00",
    }])
    row = registry.lookup(["time@example.com"])["time@example.com"]
    assert row["observed_seconds"] == 3600


def test_status_poller_uses_persisted_rows_for_the_current_account_slice(tmp_path):
    plan_path = tmp_path / "plan_check_results.json"
    plan_path.write_text(json.dumps({
        "results": [{
            "email": "alive@example.com",
            "status": "free",
            "last_checked": "2026-07-22T00:00:00Z",
            "probe_status": "error",
            "probe_error": "network: timeout",
            "probe_checked_at": "2026-07-22T01:00:00Z",
        }],
    }), encoding="utf-8")

    rows = console.merged_poll_rows(
        plan_path,
        ["alive@example.com"],
        [{"email": "alive@example.com", "status": "error"}],
    )
    assert rows[0]["status"] == "free"
    assert rows[0]["probe_error"] == "network: timeout"


def test_interrupted_pool_job_reconciles_completed_results_before_release(client):
    imported = client.post("/api/accounts/import", json={
        "lines": "recovered@example.com----mail-pass----client----refresh-token",
        "mode": "replace",
    })
    assert imported.status_code == 200
    registry = console.pool_registry()
    assert registry.reserve("free_recovered", ["recovered@example.com"]) == ["recovered@example.com"]

    result_path = console.RESULT_DIR / "free_recovered.jsonl"
    result_path.write_text(json.dumps({
        "email": "recovered@example.com",
        "ok": True,
        "status": "registered",
        "registered_at": "2026-07-22T01:00:00Z",
    }) + "\n", encoding="utf-8")
    console.atomic_json(console.JOB_DIR / "free_recovered.json", {
        "id": "free_recovered",
        "state": "running",
        "pool_batch": True,
        "result_path": str(result_path),
    })

    console.load_jobs()
    row = registry.lookup(["recovered@example.com"])["recovered@example.com"]
    assert console.JOBS["free_recovered"].meta["state"] == "interrupted"
    assert row["state"] == "registered"
    assert row["registered_at"] == "2026-07-22T01:00:00Z"


def test_replace_import_is_blocked_while_pool_accounts_are_reserved(client):
    first = client.post("/api/accounts/import", json={
        "lines": "reserved@example.com----mail-pass----client----refresh-token",
        "mode": "replace",
    })
    assert first.status_code == 200
    registry = console.pool_registry()
    assert registry.reserve("free_active", ["reserved@example.com"]) == ["reserved@example.com"]

    replacement = client.post("/api/accounts/import", json={
        "lines": "new@example.com----mail-pass----client----refresh-token",
        "mode": "replace",
    })
    assert replacement.status_code == 409
    assert "正在运行" in replacement.get_json()["error"]


def test_timezone_setting_is_exposed_as_local_server_time(client):
    response = client.patch("/api/settings", json={"display_timezone": "Asia/Shanghai"})
    assert response.status_code == 200
    health = client.get("/api/health").get_json()
    assert health["timezone"] == "Asia/Shanghai"
    assert health["local_time"].endswith("+08:00")
