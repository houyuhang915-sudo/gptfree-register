#!/usr/bin/env python3
"""scripts/run_email_proto_register.py - Free 注册批量入口。

支持两种注册方式：
  - protocol：Mail Auth 兼容协议（Outlook / iCloud）
  - browser ：工作台同款浏览器自动化（unified_pipeline._browser_register → GPTPipeline.signup）

可选：注册成功后接码绑号并获取 Codex RT。
  - agent identity：用 AT 注册 Ed25519 Agent Identity，跳过手机 add-phone/SMS
  - platform：接码平台取号，纯 HTTP 完成 Codex OAuth / add-phone / token exchange
  - manual/http：保留原 phone_binding 浏览器兼容流程

注册阶段的 protocol / browser 实现彼此独立；platform 只替换注册后的绑号与
Codex RT 阶段，不启动绑号浏览器。

输入：每行一条 outlook 凭据
    email----password----client_id----refresh_token

输出：JSONL。注册成功后固定带 registration_ok；启用绑号后另带
phone_bind_ok、status、bind_phone，成功时 codex_refresh_token 与 Outlook
refresh_token 分字段保存。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from queue import Queue
from typing import Any
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 浏览器 Free：与工作台 2FA / window_layout 一致，3×2 网格最多 6 窗
BROWSER_MAX_WORKERS = 6

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("free")


def _parse_lines(text: str) -> list[dict]:
    out = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.split("----")]
        if len(parts) >= 4:
            out.append({
                "email": parts[0],
                "password": parts[1],
                "client_id": parts[2],
                "refresh_token": parts[3],
                "mail_kind": "outlook",
            })
        elif len(parts) == 2 and parts[1].startswith(("http://", "https://")):
            # iCloud/relay 行：email----relay_url，由现有 email_provider 负责取码。
            out.append({
                "email": parts[0],
                "password": "",
                "client_id": "",
                "refresh_token": parts[1],
                "mail_kind": "relay",
            })
        else:
            log.warning("跳过不完整行: %s", s[:60])
    return out


def _load_proxy_pool(path_value: str) -> list[tuple[str, str]]:
    """Load ``REGION|proxy-url`` rows while keeping credentials out of logs."""
    value = (path_value or "").strip()
    if not value:
        return []
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"代理池文件不存在: {path}")
    rows: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            region, proxy = line.split("|", 1)
        else:
            region, proxy = "POOL", line
        region = region.strip().upper() or "POOL"
        proxy = proxy.strip()
        if proxy:
            rows.append((region, proxy))
    return rows


def _enrich_outlook_fields(email: str, account: dict) -> tuple[str, str, str]:
    outlook_pwd = (account.get("password") or "").strip()
    client_id = (account.get("client_id") or "").strip()
    refresh_token = (account.get("refresh_token") or "").strip()
    if not refresh_token or not client_id:
        try:
            import accounts_pool as _ap
            pool_hit = _ap.find_by_email(email)
            if pool_hit and pool_hit.kind == "outlook":
                client_id = client_id or (pool_hit.client_id or "")
                refresh_token = refresh_token or (pool_hit.refresh_token or "")
                outlook_pwd = outlook_pwd or (pool_hit.password or "")
            elif pool_hit and pool_hit.kind == "icloud":
                refresh_token = refresh_token or (pool_hit.relay_url or "")
        except Exception as exc:
            log.debug(f"补齐邮箱凭据失败: {exc}")
    return outlook_pwd, client_id, refresh_token


def _persist_success(out: dict, account: dict, *, note: str) -> dict:
    """写账号管理 + binding_store。"""
    email = out.get("email") or account.get("email") or ""
    outlook_pwd, client_id, refresh_token = _enrich_outlook_fields(email, account)
    out["client_id"] = client_id
    out["refresh_token"] = refresh_token
    out["outlook_password"] = outlook_pwd
    trial = out.get("free_trial") if isinstance(out.get("free_trial"), dict) else {}
    persisted_plan = str(out.get("plan_type") or "free").strip().lower()
    if persisted_plan not in {"free", "plus", "pro", "team", "enterprise", "edu", "k12"}:
        persisted_plan = "free"
    try:
        from output_writer import write_free_success
        path = write_free_success(
            email=email,
            password=out.get("password") or "",
            access_token=out.get("access_token") or "",
            session_token=out.get("session_token") or "",
            session_json=out.get("session_json") or {},
            device_id=out.get("device_id") or "",
            refresh_token=refresh_token,
            client_id=client_id,
            outlook_password=outlook_pwd,
            note=note,
            free_trial_status=str(trial.get("status") or "") if trial else "",
            free_trial_eligible=trial.get("eligible"),
            free_trial_campaign=str(trial.get("campaign_id") or ""),
            free_trial_checked_at=str(trial.get("checked_at") or ""),
            free_trial_error=str(trial.get("error") or ""),
            agent_runtime_id=str(out.get("agent_runtime_id") or ""),
            agent_private_key=str(out.get("agent_private_key") or ""),
            agent_account_id=str(out.get("agent_account_id") or ""),
            agent_user_id=str(out.get("agent_user_id") or ""),
        )
        out["account_file"] = str(path) if path else ""
        log.info(f"  ✓ 已写入账号管理 → {path.name if path else '?'}")
    except Exception as exc:
        log.warning(f"  写入账号管理失败（不致命）: {exc}")
        out["account_store_error"] = f"{type(exc).__name__}: {exc}"
    try:
        from phone_binding import binding_store
        binding_store.record_binding(
            email=email,
            access_token=out.get("access_token") or "",
            client_id=client_id or "",
            outlook_refresh_token=refresh_token or "",
            session_token=out.get("session_token") or "",
            plan_type=persisted_plan,
            free_trial_status=str(trial.get("status") or "") if trial else "",
            free_trial_eligible=trial.get("eligible"),
            free_trial_campaign=str(trial.get("campaign_id") or ""),
            free_trial_checked_at=str(trial.get("checked_at") or ""),
            free_trial_error=str(trial.get("error") or ""),
        )
    except Exception as exc:
        log.debug(f"  binding_store 写入跳过: {exc}")
    return out


def _register_protocol(
    account: dict,
    *,
    proxy: str,
    otp_timeout: int,
    with_password: bool,
    protocol_engine: str = "mail_auth",
    log_fn=None,
) -> dict:
    log_fn = log_fn or log.info
    from chatgpt_register import ChatGPTRegister
    bot = ChatGPTRegister(
        account,
        log_fn=log_fn,
        proxy=proxy or "",
        otp_timeout=otp_timeout,
        with_password=with_password,
    )
    return bot.register()


def _register_browser(
    account: dict,
    *,
    browser: str,
    log_fn,
    proxy: str = "",
    window_index: int | None = None,
) -> dict:
    from unified_pipeline import _browser_register
    at, st, raw, err = _browser_register(
        outlook_email=account.get("email") or "",
        outlook_password=account.get("password") or "",
        refresh_token=account.get("refresh_token") or "",
        client_id=account.get("client_id") or "",
        browser_choice=browser or "bitbrowser",
        proxy_url=proxy or "",
        log_fn=log_fn,
        window_index=window_index,
    )
    if err:
        return {
            "status": "failed",
            "email": account.get("email") or "",
            "error": err,
            "password": (raw or {}).get("password") or "",
            "access_token": "",
            "session_token": "",
        }
    return {
        "status": "success",
        "email": account.get("email") or "",
        "access_token": at or "",
        "session_token": st or "",
        "session_json": (raw or {}).get("session_json") or {},
        "password": (raw or {}).get("password") or account.get("password") or "",
        "device_id": (raw or {}).get("device_id") or "",
        "password_set": True,  # browser signup 走 UI 密码表单
        "raw": raw or {},
    }


def _register_agent_identity_after_register(
    access_token: str,
    *,
    proxy: str = "",
    log_fn=None,
) -> dict[str, Any]:
    from agent_identity import register_agent_identity

    return register_agent_identity(
        access_token,
        proxy=proxy or "",
        log_fn=log_fn,
    )


def _check_free_trial_after_register(out: dict, *, proxy: str = "") -> dict[str, Any]:
    from free_trial_check import check_free_trial

    return check_free_trial(
        out.get("access_token") or "",
        proxy=proxy,
        session_token=out.get("session_token") or "",
        device_id=out.get("device_id") or "",
    )


def _registered_plan_type(out: dict) -> str:
    from free_trial_check import token_plan_type

    return token_plan_type(out.get("access_token") or "")


def _bind_phone_after_register(
    account: dict,
    chatgpt_password: str,
    *,
    phone_lines: str = "",
    sms_source: str = "platform",
    use_bitbrowser: bool = True,
    proxy: str = "",
    log_fn=None,
    otp_timeout: int = 180,
    max_phone_attempts: int = 3,
    max_otp_retries: int = 2,
    window_index: int | None = None,
) -> dict[str, Any]:
    """注册成功后绑号。

    sms_source:
      - platform / legacy：号池「平台取号」config（smsbower 等 get_sms_provider）
      - manual：phone----sms_api 文本（与绑号 tab 相同）
      - http：static_phone+poll_url 合成的 phone_lines（调用前已合成）
    """
    from phone_binding import AccountEntry

    log_fn = log_fn or (lambda _m: None)
    email = account.get("email") or ""
    acc = AccountEntry(
        email=email,
        # Outlook mailbox passwords are not ChatGPT passwords. Keep this empty
        # for passwordless accounts so Codex OAuth takes the email-OTP branch.
        password=(chatgpt_password or "").strip(),
        client_id=(account.get("client_id") or "").strip(),
        refresh_token=(account.get("refresh_token") or "").strip(),
    )
    if not acc.refresh_token:
        return {"ok": False, "error": "bind_phone: missing outlook refresh_token（绑号需要邮箱 OTP）"}

    src = (sms_source or "platform").strip().lower()
    if src in ("legacy", "platform", "smsbower"):
        src = "platform"

    # ---- 平台取号 + 纯 HTTP Codex OAuth / add-phone / RT ----
    if src == "platform":
        try:
            from phone_binding import bind_account_with_protocol

            return bind_account_with_protocol(
                acc,
                proxy=proxy,
                email_otp_timeout=max(30, int(otp_timeout or 180)),
                sms_otp_timeout=max(30, int(otp_timeout or 30)),
                sms_max_attempts=max(1, int(max_phone_attempts or 1)),
                sms_max_otp_retries=max(0, int(max_otp_retries or 0)),
                persist=True,
                plan_type="free",
                log_fn=log_fn,
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": f"protocol binder load exception: {type(exc).__name__}: {exc}",
                "phone": "",
                "sms_source": "platform",
                "bind_mode": "protocol",
                "phone_bound": False,
            }

    # ---- 手动 phone----sms_api（绑号 tab 同款）----
    if not (phone_lines or "").strip():
        return {
            "ok": False,
            "error": "bind_phone: 未提供 phone_lines（格式：phone----sms_api_url）",
        }
    try:
        from phone_binding import PhoneBindingService
        svc = PhoneBindingService(persist=True)
        result = svc.bind(
            accounts=[acc],
            phone_lines=phone_lines,
            use_bitbrowser=use_bitbrowser,
            concurrency=1,
            log_fn=log_fn,
        )
    except Exception as exc:
        return {"ok": False, "error": f"bind_phone exception: {type(exc).__name__}: {exc}"}

    rows = (result or {}).get("results") or []
    row = rows[0] if rows else {}
    ok = bool(row.get("ok"))
    return {
        "ok": ok,
        "phone": row.get("phone") or "",
        "error": row.get("error") or ("" if ok else "bind_failed"),
        "access_token": row.get("access_token") or "",
        "refresh_token": row.get("refresh_token") or "",
        "id_token": row.get("id_token") or "",
        "sms_source": "manual",
    }

def _process_one(
    idx: int,
    total: int,
    account: dict,
    *,
    method: str = "protocol",
    browser: str = "bitbrowser",
    proxy: str = "",
    proxy_region: str = "",
    protocol_engine: str = "mail_auth",
    otp_timeout: int = 90,
    with_password: bool = True,
    bind_phone: bool = False,
    agent_identity: bool = False,
    phone_lines: str = "",
    sms_source: str = "platform",
    bind_use_bitbrowser: bool = True,
    sms_otp_timeout: int = 30,
    sms_max_attempts: int = 3,
    sms_max_otp_retries: int = 2,
    sms_semaphore: threading.Semaphore | None = None,
    window_index: int | None = None,
) -> dict:
    tag = f"#{idx + 1}/{total}"
    email = account.get("email", "")
    method = (method or "protocol").strip().lower()
    if method not in ("protocol", "browser"):
        method = "protocol"
    browser = (browser or "bitbrowser").strip().lower()
    slot_info = f" slot={window_index}" if window_index is not None else ""
    region_info = f" proxy_region={proxy_region}" if proxy_region else ""
    engine_info = f" engine={protocol_engine}" if method == "protocol" else ""
    log.info(f"=== {tag} {email} method={method}{engine_info}{slot_info}{region_info} ===")

    started = time.time()
    log_fn = lambda m: log.info(f"[{tag}] {m}")

    try:
        if method == "browser":
            result = _register_browser(
                account,
                browser=browser,
                proxy=proxy,
                log_fn=log_fn,
                window_index=window_index,
            )
        else:
            result = _register_protocol(
                account,
                proxy=proxy,
                otp_timeout=otp_timeout,
                with_password=with_password,
                protocol_engine=protocol_engine,
                log_fn=log_fn,
            )
    except Exception as exc:
        return {
            "email": email,
            "ok": False,
            "method": method,
            "protocol_engine": protocol_engine if method == "protocol" else "",
            "proxy_region": proxy_region,
            "duration_ms": int((time.time() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }

    elapsed_ms = int((time.time() - started) * 1000)
    if result.get("status") != "success":
        log.warning(f"[{tag}] ✗ 注册失败: {result.get('error')}")
        return {
            "email": email,
            "ok": False,
            "method": method,
            "protocol_engine": protocol_engine if method == "protocol" else "",
            "proxy_region": proxy_region,
            "password": result.get("password", ""),
            "duration_ms": elapsed_ms,
            "error": result.get("error") or "unknown",
        }

    log.info(
        f"[{tag}] ✓ 注册成功，耗时 {elapsed_ms} ms method={method}"
        + (f" engine={protocol_engine}" if method == "protocol" else "")
    )
    out = {
        "email": result.get("email") or email,
        "ok": True,
        "method": method,
        "protocol_engine": protocol_engine if method == "protocol" else "",
        "proxy_region": proxy_region,
        "access_token": result.get("access_token", ""),
        "session_token": result.get("session_token", ""),
        "session_json": result.get("session_json") or {},
        "password": result.get("password", ""),
        "device_id": result.get("device_id", ""),
        "password_set": bool(result.get("password_set")),
        "duration_ms": elapsed_ms,
        "registration_ok": True,
        "status": "registered",
        "registered_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    note = (
        f"free {method} register"
        + (" + browser" if method == "browser" else f" ({protocol_engine})")
    )
    registered_plan = _registered_plan_type(out)
    out["plan_type"] = registered_plan or "unknown"
    if registered_plan == "free":
        log.info(f"[{tag}] [trial-check] 检测 Plus 免费试用资格…")
        trial = _check_free_trial_after_register(out, proxy=proxy)
        out["free_trial"] = trial
        out["free_trial_status"] = trial.get("status") or "unknown"
        out["free_trial_eligible"] = trial.get("eligible")
        if trial.get("eligible") is True:
            log.info(f"[{tag}] [trial-check] ✓ 有免费试用 · {trial.get('campaign_id')}")
        elif trial.get("eligible") is False:
            log.info(f"[{tag}] [trial-check] 无免费试用 · state={trial.get('state') or 'not_eligible'}")
        else:
            log.warning(f"[{tag}] [trial-check] 检测状态未知: {trial.get('error') or trial.get('state') or 'unknown'}")
    else:
        log.info(
            f"[{tag}] [trial-check] 跳过：套餐={registered_plan or 'unknown'}，"
            "仅 Free 账号检测免费试用"
        )

    if agent_identity:
        log.info(f"[{tag}] [agent-identity] 创建免手机 SMS 的 Codex Agent 凭据…")
        agent_result = _register_agent_identity_after_register(
            out.get("access_token") or "",
            proxy=proxy,
            log_fn=log_fn,
        )
        out["agent_identity"] = agent_result
        out["agent_identity_ok"] = bool(agent_result.get("ok"))
        if agent_result.get("ok"):
            out["agent_runtime_id"] = agent_result.get("agent_runtime_id") or ""
            out["agent_private_key"] = agent_result.get("agent_private_key") or ""
            out["agent_account_id"] = agent_result.get("account_id") or ""
            out["agent_user_id"] = agent_result.get("user_id") or ""
            out["status"] = "agent_ready"
            log.info(f"[{tag}] ✓ Agent Identity 就绪，可直接导出 auth.json")
        else:
            out["ok"] = False
            out["status"] = "agent_failed"
            out["agent_identity_error"] = agent_result.get("error") or "agent_identity_failed"
            log.warning(f"[{tag}] ✗ Agent Identity 失败: {out['agent_identity_error']}")
    out = _persist_success(out, account, note=note)

    # 专用存储写入成功后只在该 0600 文件保留私钥；批任务 JSONL 始终移除私钥。
    if agent_identity and out.get("agent_identity_ok") and out.get("account_store_error"):
        out["ok"] = False
        out["agent_identity_ok"] = False
        out["status"] = "agent_failed"
        out["agent_identity_error"] = out["account_store_error"]
    if agent_identity:
        out.pop("agent_private_key", None)
        public_agent = dict(out.get("agent_identity") or {})
        public_agent.pop("agent_private_key", None)
        out["agent_identity"] = public_agent

    if bind_phone and not agent_identity:
        log.info(f"[{tag}] [..] 接码绑号 sms_source={sms_source}…")
        if sms_semaphore is not None:
            log.info(f"[{tag}] [..] 等待接码协议并发槽…")
            sms_semaphore.acquire()
        try:
            bind_res = _bind_phone_after_register(
                account,
                out.get("password") or "",
                phone_lines=phone_lines,
                sms_source=sms_source,
                use_bitbrowser=bind_use_bitbrowser,
                proxy=proxy,
                log_fn=log_fn,
                otp_timeout=sms_otp_timeout,
                max_phone_attempts=sms_max_attempts,
                max_otp_retries=sms_max_otp_retries,
                window_index=window_index,
            )
        finally:
            if sms_semaphore is not None:
                sms_semaphore.release()
        out["bind_phone"] = bind_res
        out["phone_bind_ok"] = bool(bind_res.get("ok"))
        if bind_res.get("ok"):
            out["status"] = "phone_bound"
            out["phone"] = bind_res.get("phone") or ""
            # 绑号成功可能带回新 AT / Codex RT
            if bind_res.get("access_token"):
                out["access_token"] = bind_res["access_token"]
            if bind_res.get("refresh_token"):
                out["codex_refresh_token"] = bind_res["refresh_token"]
            log.info(f"[{tag}] ✓ 绑号成功 phone={out.get('phone')}")
        else:
            out["ok"] = False
            out["status"] = "phone_failed"
            out["bind_error"] = bind_res.get("error") or "bind_failed"
            if bind_res.get("phone_bound") and bind_res.get("phone"):
                out["phone"] = bind_res["phone"]
            log.warning(f"[{tag}] ✗ 绑号失败: {out['bind_error']}")
    return out


def _append_jsonl(out_path: Path, item: dict) -> None:
    try:
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning(f"写 {out_path} 失败: {exc}")


def _export_sub2api_agent_results(emails: list[str]) -> dict[str, Any]:
    """把本批次成功生成 Agent Identity 的 Free 账号导出为
    Sub2API 管理后台“导入数据”可读取的 JSON。
    """
    import account_exports

    normalized = list(dict.fromkeys(
        str(email or "").strip().lower() for email in emails if str(email or "").strip()
    ))
    if not normalized:
        raise ValueError("本批次没有可导出的成功账号")
    selection = account_exports.ExportSelection(mode="emails", emails=normalized)
    records = account_exports.select_records(account_exports.load_records(), selection)
    records = [record for record in records if record.has_agent_identity]
    if not records:
        raise ValueError("本批次成功账号尚未持久化 Agent Identity")
    artifact = account_exports.export(
        "sub2api_agent",
        records=records,
        refresh_tokens=False,
    )
    saved_path = account_exports.write_artifact(artifact)
    return {
        "count": len(records),
        "filename": artifact.filename,
        "saved_path": str(saved_path),
    }


def _import_sub2api_agent_results(emails: list[str]) -> dict[str, Any]:
    """把本批成功账号的 Agent Identity 直接写入已配置的 Sub2API。"""
    import account_exports
    import gateway_push

    normalized = list(dict.fromkeys(
        str(email or "").strip().lower() for email in emails if str(email or "").strip()
    ))
    if not normalized:
        raise ValueError("本批次没有可导入的成功账号")
    selection = account_exports.ExportSelection(mode="emails", emails=normalized)
    records = account_exports.select_records(account_exports.load_records(), selection)
    records = [record for record in records if record.has_agent_identity]
    if not records:
        raise ValueError("本批次成功账号尚未持久化 Agent Identity")
    result = gateway_push.push_agent_identities(records=records)
    result["count"] = len(records)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Free 注册：protocol / browser + 可选接码绑号"
    )
    ap.add_argument("--emails-file", default="", help="账号文件路径，每行 email----pwd----cid----rt")
    ap.add_argument("--emails-lines", default="", help="多行字符串，跟 --emails-file 二选一")
    ap.add_argument("--out", default=str(ROOT / "output" / "free_accounts.jsonl"))
    ap.add_argument(
        "--workers", "-w", type=int, default=0,
        help="并发数：protocol 自定义（默认 3）；browser 固定最多 6（3×2 铺窗，完成自动补位）",
    )
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（调试用）")
    ap.add_argument("--proxy", default="", help="协议模式代理 URL")
    ap.add_argument(
        "--proxy-pool-file",
        default="",
        help="账号轮换代理池文件；每行 REGION|proxy-url",
    )
    ap.add_argument("--otp-timeout", type=int, default=90, help="协议模式 OTP 超时秒")
    ap.add_argument(
        "--method",
        default="protocol",
        choices=("protocol", "browser"),
        help="注册方式：protocol=HTTP 协议 / browser=浏览器自动化",
    )
    ap.add_argument(
        "--protocol-engine",
        default="mail_auth",
        choices=("mail_auth",),
        help="protocol 模式内核：Mail Auth（Outlook / iCloud）",
    )
    ap.add_argument(
        "--browser",
        default="bitbrowser",
        choices=("bitbrowser", "roxy", "chromium"),
        help="browser 模式下用哪个浏览器",
    )
    ap.add_argument("--with-password", action="store_true", default=True)
    ap.add_argument("--no-password", action="store_true", help="协议模式跳过设密码")
    ap.add_argument("--bind-phone", action="store_true", help="注册成功后接码绑号")
    ap.add_argument(
        "--agent-identity",
        action="store_true",
        help="注册后创建 Agent Identity；该模式跳过手机 add-phone/SMS",
    )
    ap.add_argument(
        "--sub2api-export",
        action="store_true",
        help="任务结束后把本批 Agent Identity 导出为 Sub2API 数据 JSON",
    )
    ap.add_argument(
        "--sub2api-import",
        action="store_true",
        help="任务结束后把本批 Agent Identity 自动导入已配置的 Sub2API",
    )
    ap.add_argument(
        "--phone-lines",
        default="",
        help="手动绑号：phone----sms_api_url（sms_source=manual 时）",
    )
    ap.add_argument(
        "--bind-use-bitbrowser",
        action="store_true",
        default=True,
        help="绑号时用 BitBrowser（默认开）",
    )
    ap.add_argument(
        "--bind-no-bitbrowser",
        action="store_true",
        help="绑号用本地 Chromium",
    )
    ap.add_argument(
        "--sms-source",
        default="platform",
        help="platform=号池 smsbower 平台取号；manual=phone_lines；http=static+poll；none=不绑",
    )
    ap.add_argument("--sms-max-attempts", type=int, default=3)
    ap.add_argument("--sms-workers", type=int, default=3,
                    help="接码/RT 阶段独立并发上限（避免一次租用过多号码）")
    ap.add_argument(
        "--sms-otp-timeout",
        type=int,
        default=30,
        help="单个号码的总等待上限（秒）；所有补发共享倒计时，到期取消并换号",
    )
    ap.add_argument("--sms-max-otp-retries", type=int, default=2)
    ap.add_argument("--sms-acquire-url", default="")
    ap.add_argument("--sms-static-phone", default="")
    ap.add_argument("--sms-poll-url", default="")
    ap.add_argument("--sms-release-complete-url", default="")
    ap.add_argument("--sms-release-cancel-url", default="")
    ap.add_argument("--sms-code-regex", default="")
    args = ap.parse_args()

    proxy = (args.proxy or "").strip()
    try:
        proxy_pool = _load_proxy_pool(args.proxy_pool_file)
    except (OSError, ValueError) as exc:
        log.error(f"读取代理池失败: {exc}")
        return 2
    otp_timeout = max(30, int(args.otp_timeout or 90))
    with_password = not bool(args.no_password)
    method = (args.method or "protocol").strip().lower()
    protocol_engine = "mail_auth"
    browser = (args.browser or "bitbrowser").strip().lower()
    bind_phone = bool(args.bind_phone)
    agent_identity = bool(args.agent_identity)
    sub2api_export = bool(args.sub2api_export)
    sub2api_import = bool(args.sub2api_import)
    phone_lines = (args.phone_lines or "").strip()
    bind_use_bb = not bool(args.bind_no_bitbrowser)
    sms_source = (args.sms_source or "platform").strip().lower()
    if sms_source in ("legacy", "smsbower"):
        sms_source = "platform"
    if sms_source == "none":
        bind_phone = False
    if agent_identity:
        bind_phone = False
    if (sub2api_export or sub2api_import) and not agent_identity:
        log.error("Sub2API 导出/导入需要同时启用 --agent-identity")
        return 2

    # http：static_phone+poll_url → phone_lines，sms_source 改 manual
    if bind_phone and sms_source == "http":
        static_phone = (args.sms_static_phone or "").strip()
        poll_url = (args.sms_poll_url or "").strip()
        if static_phone and poll_url and not phone_lines:
            poll = poll_url.replace("{phone}", static_phone)
            phone_lines = f"{static_phone}----{poll}"
            log.info(f"从 http static_phone+poll_url 合成 phone_lines: {static_phone}")
        sms_source = "manual"

    text = ""
    if args.emails_file:
        path = Path(args.emails_file)
        if not path.is_absolute():
            path = ROOT / path
        if path.exists():
            text = path.read_text(encoding="utf-8")
        else:
            log.error(f"账号文件不存在: {path}")
            return 2
    elif args.emails_lines:
        text = args.emails_lines

    accounts = _parse_lines(text)
    if not accounts:
        log.error("没有可处理的账号（每行需要 email----password----client_id----refresh_token）")
        return 2
    if args.limit > 0:
        accounts = accounts[: args.limit]

    if bind_phone and sms_source == "manual" and not phone_lines:
        log.error(
            "sms_source=manual 需要 phone_lines（phone----sms_api），"
            "或改用 --sms-source platform 走号池 smsbower。"
        )
        return 2
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 并发策略：
    #   protocol → 用户自定义 workers（默认 3，上限 32）
    #   browser  → 固定最多 6（与 2FA / window_layout 3×2 网格一致），完成自动补位
    if method == "browser":
        workers = int(args.workers or BROWSER_MAX_WORKERS)
        if workers > BROWSER_MAX_WORKERS:
            log.warning(
                f"browser 模式并发上限 {BROWSER_MAX_WORKERS}（3×2 铺窗），"
                f"已把 workers={workers} 压到 {BROWSER_MAX_WORKERS}"
            )
            workers = BROWSER_MAX_WORKERS
        workers = max(1, workers)
    else:
        workers = int(args.workers or 3)
        workers = max(1, min(workers, 32))

    sms_workers = max(1, min(int(args.sms_workers or 3), workers))
    sms_semaphore = threading.BoundedSemaphore(sms_workers) if bind_phone else None

    log.info(
        f"待处理账号: {len(accounts)} 个 | 并发: {workers} | method={method} | "
        f"browser={browser if method=='browser' else '-'} | "
        f"proxy={'pool:'+str(len(proxy_pool)) if proxy_pool else ('yes' if proxy else 'no')} | "
        f"with_password={with_password} | "
        f"protocol_engine={protocol_engine if method == 'protocol' else '-'} | "
        f"agent_identity={agent_identity} | "
        f"bind_phone={bind_phone} | sms_source={sms_source if bind_phone else '-'}"
        + (f" | sms_workers={sms_workers}" if bind_phone else "")
        + (f" | 铺窗=3×2 slot池" if method == "browser" and workers > 1 else "")
    )

    ok_count = 0
    fail_count = 0
    success_emails: list[str] = []
    total = len(accounts)
    base_kwargs = dict(
        method=method,
        protocol_engine=protocol_engine,
        browser=browser,
        otp_timeout=otp_timeout,
        with_password=with_password,
        bind_phone=bind_phone,
        agent_identity=agent_identity,
        phone_lines=phone_lines,
        sms_source=sms_source,
        bind_use_bitbrowser=bind_use_bb,
        sms_otp_timeout=max(30, int(args.sms_otp_timeout or 30)),
        sms_max_attempts=max(1, int(args.sms_max_attempts or 1)),
        sms_max_otp_retries=max(0, int(args.sms_max_otp_retries)),
        sms_semaphore=sms_semaphore,
    )

    def _proxy_for_account(index: int) -> tuple[str, str]:
        if proxy_pool:
            return proxy_pool[index % len(proxy_pool)]
        return ("SINGLE" if proxy else "", proxy)

    # browser 用 slot 池：占用 0..workers-1，完成后归还，下一个账号补位同槽
    slot_q: Queue | None = None
    if method == "browser" and workers > 1:
        slot_q = Queue()
        for s in range(workers):
            slot_q.put(s)

    def _run_one(i: int, acct: dict) -> dict:
        slot = None
        proxy_region, account_proxy = _proxy_for_account(i)
        if slot_q is not None:
            slot = slot_q.get()  # 阻塞等空闲槽位
        try:
            return _process_one(
                i,
                total,
                acct,
                proxy=account_proxy,
                proxy_region=proxy_region,
                window_index=slot,
                **base_kwargs,
            )
        except Exception as exc:
            return {
                "email": acct.get("email", ""),
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": 0,
            }
        finally:
            if slot_q is not None and slot is not None:
                slot_q.put(slot)

    if workers <= 1:
        for i, acct in enumerate(accounts):
            # 单并发也给 browser 一个明确槽位 0，保证窗口尺寸受限
            wi = 0 if method == "browser" else None
            proxy_region, account_proxy = _proxy_for_account(i)
            try:
                res = _process_one(
                    i,
                    total,
                    acct,
                    proxy=account_proxy,
                    proxy_region=proxy_region,
                    window_index=wi,
                    **base_kwargs,
                )
            except Exception as exc:
                res = {
                    "email": acct.get("email", ""),
                    "ok": False,
                    "error": str(exc),
                    "duration_ms": 0,
                }
            _append_jsonl(out_path, res)
            if res.get("ok"):
                ok_count += 1
                if res.get("email"):
                    success_emails.append(str(res["email"]))
            else:
                fail_count += 1
    else:
        # ThreadPoolExecutor：max_workers=N 同时最多 N 个在跑；
        # 提交全部任务后，完成一个立刻调度下一个（自动补位）
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(_run_one, i, acct): (i, acct)
                for i, acct in enumerate(accounts)
            }
            for f in as_completed(futs):
                try:
                    res = f.result()
                except Exception as exc:
                    i, acct = futs[f]
                    res = {
                        "email": acct.get("email", ""),
                        "ok": False,
                        "error": f"thread exception: {exc}",
                        "duration_ms": 0,
                    }
                _append_jsonl(out_path, res)
                if res.get("ok"):
                    ok_count += 1
                    if res.get("email"):
                        success_emails.append(str(res["email"]))
                else:
                    fail_count += 1
                done = ok_count + fail_count
                log.info(f"[progress] {done}/{total} ok={ok_count} fail={fail_count}")

    if sub2api_export and success_emails:
        try:
            export_result = _export_sub2api_agent_results(success_emails)
            log.info(
                "[sub2api-export] ✓ 已导出 %s 个 Agent Identity → %s",
                export_result["count"],
                export_result["saved_path"],
            )
        except Exception as exc:
            log.warning(
                "[sub2api-export] 导出失败: %s: %s",
                type(exc).__name__,
                exc,
            )

    if sub2api_import and success_emails:
        try:
            import_result = _import_sub2api_agent_results(success_emails)
            if import_result.get("ok"):
                log.info(
                    "[sub2api-import] ✓ 已导入 %s 个 Agent Identity → %s",
                    import_result.get("succeeded", import_result["count"]),
                    import_result.get("url", "Sub2API"),
                )
            else:
                log.warning(
                    "[sub2api-import] 导入未完成: 成功=%s 失败=%s HTTP=%s %s",
                    import_result.get("succeeded", 0),
                    import_result.get("failed", import_result["count"]),
                    import_result.get("status", 0),
                    import_result.get("body", ""),
                )
        except Exception as exc:
            log.warning(
                "[sub2api-import] 导入失败: %s: %s",
                type(exc).__name__,
                exc,
            )

    log.info("=" * 60)
    log.info(f"完成 | ok={ok_count} fail={fail_count} 总={total}")
    log.info(f"结果已追加到 {out_path}")
    return 0 if fail_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
