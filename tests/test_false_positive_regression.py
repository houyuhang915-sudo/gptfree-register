from __future__ import annotations

import json
import sys
from pathlib import Path

from account_registry import AccountRegistry


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import plan_check


def _configure_output(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(plan_check, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(plan_check, "PLAN_CHECK_FILE", tmp_path / "plan_check_results.json")
    monkeypatch.setattr(plan_check, "BANNED_REPORT_FILE", tmp_path / "banned_accounts_sorted.tsv")


def _write_result(monkeypatch, tmp_path: Path, row: dict) -> dict:
    _configure_output(monkeypatch, tmp_path)
    plan_check.write_results({
        "total": 1,
        "plus": 0,
        "k12": 0,
        "free": 0,
        "plus_expired": 0,
        "errors": 1,
        "tier1": 0,
        "tier2": 0,
        "tier3": 0,
        "results": [row],
    })
    return json.loads(plan_check.PLAN_CHECK_FILE.read_text(encoding="utf-8"))


def test_at_probe_401_is_token_dead_not_account_deactivated(monkeypatch) -> None:
    monkeypatch.setattr(
        plan_check,
        "_probe_access_token_alive",
        lambda *_args, **_kwargs: (False, "http_401", {}),
    )

    row = plan_check._check_one(
        email="expired-at@example.test",
        access_token="old-access-token",
        refresh_token="",
        outlook_creds=None,
        use_browser_fallback=False,
    )

    assert row["status"] == "token_dead"
    assert row["at_probe_used"] is True
    assert row["error"] == "token_dead: at_probe_failed: http_401"
    assert "banned_at" not in row


def test_protocol_and_at_probes_receive_the_selected_proxy(monkeypatch) -> None:
    calls: dict[str, str | None] = {}

    def protocol_login(**kwargs):
        calls["protocol"] = kwargs.get("proxy")
        return "session-token", ""

    monkeypatch.setattr(plan_check, "_check_via_protocol_login", protocol_login)
    monkeypatch.setattr(
        plan_check,
        "_jwt_auth_claims",
        lambda _token: {"chatgpt_plan_type": "free"},
    )
    proxy = "http://user:pass@proxy.example.test:10000"
    protocol_row = plan_check._check_one(
        email="protocol@example.test",
        access_token="",
        outlook_creds={"refresh_token": "outlook-rt", "client_id": "client"},
        refresh_first=False,
        proxy=proxy,
    )

    assert protocol_row["status"] == "free"
    assert protocol_row["network_route"] == "managed_proxy"
    assert calls["protocol"] == proxy

    def at_probe(_token, **kwargs):
        calls["at"] = kwargs.get("proxy")
        return True, "", {}

    monkeypatch.setattr(plan_check, "_probe_access_token_alive", at_probe)
    at_row = plan_check._check_one(
        email="at@example.test",
        access_token="existing-at",
        use_browser_fallback=False,
        proxy=proxy,
    )

    assert at_row["status"] == "free"
    assert calls["at"] == proxy


def test_legacy_at_probe_deactivation_snapshot_is_migrated(monkeypatch, tmp_path: Path) -> None:
    saved = _write_result(monkeypatch, tmp_path, {
        "email": "legacy@example.test",
        "status": "account_deactivated",
        "plan_type": "",
        "tier": 3,
        "at_probe_used": True,
        "error": "account_deactivated: at_probe_failed: http_403",
        "last_checked": "2026-07-22T12:00:00.000Z",
    })

    row = saved["results"][0]
    assert row["status"] == "token_dead"
    assert row["probe_status"] == "token_dead"
    assert row["error"] == "token_dead: at_probe_failed: http_403"
    assert "banned_at" not in row
    assert saved["errors"] == 1
    assert plan_check.BANNED_REPORT_FILE.read_text(encoding="utf-8").splitlines() == [
        "封号时间(北京时间)\t账号\t历史套餐\t封号原因"
    ]


def test_explicit_protocol_deactivation_stays_deactivated(monkeypatch, tmp_path: Path) -> None:
    saved = _write_result(monkeypatch, tmp_path, {
        "email": "protocol@example.test",
        "status": "account_deactivated",
        "deactivation_source": "protocol_login",
        "plan_type": "",
        "tier": 2,
        "error": "account_deactivated: protocol_login: deleted or deactivated",
        "last_checked": "2026-07-22T12:00:00.000Z",
    })

    row = saved["results"][0]
    assert row["status"] == "account_deactivated"
    assert row["probe_status"] == "account_deactivated"
    assert row["ban_reason"] == "protocol_login"
    assert "protocol@example.test" in plan_check.BANNED_REPORT_FILE.read_text(encoding="utf-8")


def test_registry_normalizes_legacy_at_probe_deactivation_at_boundary(tmp_path: Path) -> None:
    registry = AccountRegistry(tmp_path / "pool_state.db")
    registry.sync_pool({"legacy@example.test": "legacy@example.test----pass----client----refresh"})
    registry.record_registered_results("job_legacy", [{
        "email": "legacy@example.test",
        "ok": True,
        "registered_at": "2026-07-22T00:00:00Z",
    }])

    registry.apply_health_results([{
        "email": "legacy@example.test",
        "status": "account_deactivated",
        "probe_status": "account_deactivated",
        "at_probe_used": True,
        "error": "account_deactivated: at_probe_failed: http_401",
        "last_checked": "2026-07-22T01:00:00Z",
    }])

    row = registry.lookup(["legacy@example.test"])["legacy@example.test"]
    assert row["health_status"] == "token_dead"
    assert row["last_probe_status"] == "token_dead"
    assert row["health_alive"] is None
    assert row["last_banned_at"] == ""


def test_registry_startup_migrates_persisted_at_probe_false_positive(tmp_path: Path) -> None:
    path = tmp_path / "pool_state.db"
    registry = AccountRegistry(path)
    registry.sync_pool({"stored@example.test": "stored@example.test----pass----client----refresh"})
    registry.record_registered_results("job_stored", [{
        "email": "stored@example.test",
        "ok": True,
        "registered_at": "2026-07-22T00:00:00Z",
    }])
    with registry._connect() as connection:
        connection.execute(
            """
            UPDATE pool_accounts
            SET health_status = 'account_deactivated', health_alive = 0,
                last_probe_status = 'account_deactivated',
                check_error = 'account_deactivated: at_probe_failed: http_403',
                last_banned_at = '2026-07-22T01:00:00Z'
            WHERE email = ?
            """,
            ("stored@example.test",),
        )

    migrated = AccountRegistry(path)
    row = migrated.lookup(["stored@example.test"])["stored@example.test"]
    assert row["health_status"] == "token_dead"
    assert row["last_probe_status"] == "token_dead"
    assert row["health_alive"] is None
    assert row["check_error"] == "token_dead: at_probe_failed: http_403"
    assert row["last_banned_at"] == ""
