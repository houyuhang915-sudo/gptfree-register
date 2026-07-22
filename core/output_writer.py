"""统一输出。

成功账号 → output/success.txt（追加，每行一个完整三件套块）
失败账号 → output/failed.txt（追加，记录失败原因）
单账号详细 → output/account_<email>_<timestamp>.txt

调用方:
    from output_writer import write_success, write_failure
    write_success(email, password, access_token, session_id="...", ...)
    write_failure(email, reason, stage="...", url="...")
"""
from __future__ import annotations

import logging
import os
import json
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("output_writer")

OUTPUT_DIR = Path(__file__).parent / "output"
SUCCESS_FILE = OUTPUT_DIR / "success.txt"
FAILED_FILE = OUTPUT_DIR / "failed.txt"
# 5 件套批量导入格式: email|password|access_token|client_id|refresh_token
EXPORT_5PARTS_FILE = OUTPUT_DIR / "at_export_5parts.txt"
# ---- 分隔版本: email----password----client_id----refresh_token----access_token
EXPORT_5PARTS_DASHES_FILE = OUTPUT_DIR / "at_export_5parts_dashes.txt"
# 成功账号原始 4 段（outlook 真密码）: email----password----client_id----refresh_token
OUTLOOK_SUCCESS_FILE = OUTPUT_DIR / "outlook_accounts_success.txt"
AUTH_SESSION_DIR = OUTPUT_DIR / "auth_sessions"

_lock = threading.Lock()


def _ensure_dir():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_email_for_path(email: str) -> str:
    return email.replace("@", "_at_").replace(".", "_")


def auth_session_path(email: str) -> Path:
    return AUTH_SESSION_DIR / f"{_safe_email_for_path(email.lower())}.json"


def load_auth_session_json(email: str) -> dict:
    path = auth_session_path(email)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_success(email: str,
                  password: str,
                  access_token: str = "",
                  session_id: str = "",
                  setup_intent: str = "",
                  paypal_email: str = "",
                  paypal_password: str = "",
                  refresh_token: str = "",
                  client_id: str = "",
                  outlook_password: str = "",
                  *,
                  plan: str = "plus",
                  session_token: str = "",
                  session_json: dict | None = None,
                  device_id: str = "",
                  note: str = "",
                  free_trial_status: str = "",
                  free_trial_eligible: bool | None = None,
                  free_trial_campaign: str = "",
                  free_trial_checked_at: str = "",
                  free_trial_error: str = "",
                  agent_runtime_id: str = "",
                  agent_private_key: str = "",
                  agent_account_id: str = "",
                  agent_user_id: str = "") -> Path:
    """写一条成功账号记录。

    在 success.txt 追加 + 单文件 account_<email>_<ts>.txt
    + outlook_accounts_success.txt（原始 4 段，密码优先用 outlook 真密码）。
    返回单文件路径。

    plan:
      - "plus"（默认）：支付成功 Plus 文案
      - "free"：协议 Free 注册成功，纳入账号管理（plan_type 由 JWT 解析为 free）
    """
    _ensure_dir()
    plan_norm = (plan or "plus").strip().lower() or "plus"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    block = []
    block.append("=" * 64)
    if plan_norm == "free":
        block.append("  ChatGPT Free 账号 (协议注册成功)")
    else:
        block.append("  ChatGPT Plus 账号 (注册 + PayPal 支付成功)")
    block.append("=" * 64)
    block.append(f"成功时间:    {_now()}")
    block.append(f"邮箱:        {email}")
    block.append(f"密码:        {password}")
    if plan_norm == "free":
        block.append("套餐:        free")
        block.append("支付状态:    未支付 / Free")
    else:
        block.append("套餐:        plus")
        block.append("支付状态:    ✓ Plus 已开通")
    if note:
        block.append(f"备注:        {note}")
    if plan_norm == "free" and free_trial_status:
        trial_label = (
            "有"
            if free_trial_eligible is True
            else ("无" if free_trial_eligible is False else "未知")
        )
        block.append(f"免费试用:    {trial_label}")
        block.append(f"试用状态:    {free_trial_status}")
        if free_trial_campaign:
            block.append(f"试用活动:    {free_trial_campaign}")
        if free_trial_checked_at:
            block.append(f"试用检测时间: {free_trial_checked_at}")
        if free_trial_error:
            block.append(f"试用检测错误: {free_trial_error}")
    if session_id:
        block.append(f"session_id:  {session_id}")
    if session_token:
        block.append(f"session_token: {session_token}")
    if device_id:
        block.append(f"device_id:   {device_id}")
    if agent_runtime_id:
        block.append("Codex 凭据:   Agent Identity（免手机 SMS）")
        block.append(f"agent_runtime_id: {agent_runtime_id}")
    if setup_intent:
        block.append(f"setup_intent: {setup_intent}")
    if paypal_email:
        block.append("")
        block.append(f"PayPal guest 账号:")
        block.append(f"  email:    {paypal_email}")
        if paypal_password:
            block.append(f"  password: {paypal_password}")
    if access_token:
        block.append("")
        block.append("access_token (一次有效，过期请用账号密码重登):")
        block.append(access_token)
    if refresh_token:
        block.append("")
        block.append("refresh_token / client_id (Outlook 邮箱原始):")
        block.append(f"  client_id:     {client_id}")
        block.append(f"  refresh_token: {refresh_token}")
    block.append("=" * 64)
    block.append("")
    txt = "\n".join(block)

    with _lock:
        if isinstance(session_json, dict) and session_json:
            AUTH_SESSION_DIR.mkdir(parents=True, exist_ok=True)
            stored_session = dict(session_json)
            if access_token:
                stored_session["accessToken"] = access_token
            if session_token:
                stored_session["sessionToken"] = session_token
            session_path = auth_session_path(email)
            tmp_path = session_path.with_suffix(".json.tmp")
            tmp_path.write_text(
                json.dumps(stored_session, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, session_path)
        # 单文件
        single = OUTPUT_DIR / f"account_{_safe_email_for_path(email)}_{ts}.txt"
        single.write_text(txt, encoding="utf-8")
        # 集中追加
        with open(SUCCESS_FILE, "a", encoding="utf-8") as f:
            f.write(txt)
            f.write("\n")
        # 5 件套批量导入格式（追加）：
        #   email|password|access_token|client_id|refresh_token
        # 同时再写一份 ---- 分隔版（与 outlook_accounts.txt 同风格）
        if access_token:
            try:
                with open(EXPORT_5PARTS_FILE, "a", encoding="utf-8") as fp:
                    fp.write(f"{email}|{password}|{access_token}|{client_id}|{refresh_token}\n")
                with open(EXPORT_5PARTS_DASHES_FILE, "a", encoding="utf-8") as fp:
                    fp.write(f"{email}----{password}----{client_id}----{refresh_token}----{access_token}\n")
            except Exception as e:
                log.warning(f"  [output] 写 5 件套失败（不致命）: {e}")
        # 成功账号原始 4 段（email----password----client_id----refresh_token）。
        # 密码优先用 outlook 真密码（重登邮箱用），缺失时回退到 ChatGPT 注册密码。
        # 有 refresh_token 才写（没有也没法重登邮箱）。
        # 同 email 若已存在但缺 RT，用更完整的一行覆盖，确保账号管理「邮箱凭据」能亮。
        if refresh_token:
            try:
                pwd_for_outlook = outlook_password or password
                new_line = f"{email}----{pwd_for_outlook}----{client_id}----{refresh_token}"
                key = email.lower()
                lines: list[str] = []
                found = False
                if OUTLOOK_SUCCESS_FILE.exists():
                    for ln in OUTLOOK_SUCCESS_FILE.read_text(encoding="utf-8").splitlines():
                        s = ln.strip()
                        if not s or s.startswith("#") or "----" not in s:
                            lines.append(ln)
                            continue
                        em = s.split("----")[0].strip().lower()
                        if em != key:
                            lines.append(ln)
                            continue
                        found = True
                        parts = s.split("----")
                        old_rt = parts[3].strip() if len(parts) > 3 else ""
                        # 已有 RT 则保留原行；否则用新完整行替换
                        lines.append(ln if old_rt else new_line)
                if not found:
                    lines.append(new_line)
                text = "\n".join(lines)
                if text and not text.endswith("\n"):
                    text += "\n"
                OUTLOOK_SUCCESS_FILE.write_text(text, encoding="utf-8")
            except Exception as e:
                log.warning(f"  [output] 写 outlook_accounts_success.txt 失败（不致命）: {e}")
        log.info(
            f"  [output] 成功记录写入 {single.name} + success.txt + at_export_5parts.txt"
            f" + outlook_accounts_success.txt (plan={plan_norm})"
        )
        if agent_runtime_id and agent_private_key:
            try:
                import agent_identity_store

                agent_identity_store.save(
                    email=email,
                    agent_runtime_id=agent_runtime_id,
                    agent_private_key=agent_private_key,
                    account_id=agent_account_id,
                    user_id=agent_user_id,
                    plan_type=plan_norm,
                )
                log.info("  [output] Agent Identity 已写入 agent_identities.json")
            except Exception as e:
                log.error(f"  [output] Agent Identity 持久化失败: {e}")
                raise RuntimeError(f"Agent Identity 持久化失败: {e}") from e
    return single


def write_free_success(
    email: str,
    password: str = "",
    access_token: str = "",
    *,
    session_token: str = "",
    session_json: dict | None = None,
    device_id: str = "",
    refresh_token: str = "",
    client_id: str = "",
    outlook_password: str = "",
    note: str = "free protocol register",
    free_trial_status: str = "",
    free_trial_eligible: bool | None = None,
    free_trial_campaign: str = "",
    free_trial_checked_at: str = "",
    free_trial_error: str = "",
    agent_runtime_id: str = "",
    agent_private_key: str = "",
    agent_account_id: str = "",
    agent_user_id: str = "",
) -> Path:
    """Free 协议注册成功 → 写入账号管理数据源（success.txt 等）。"""
    return write_success(
        email=email,
        password=password or "",
        access_token=access_token or "",
        refresh_token=refresh_token or "",
        client_id=client_id or "",
        outlook_password=outlook_password or password or "",
        plan="free",
        session_token=session_token or "",
        session_json=session_json,
        device_id=device_id or "",
        note=note or "free protocol register",
        free_trial_status=free_trial_status or "",
        free_trial_eligible=free_trial_eligible,
        free_trial_campaign=free_trial_campaign or "",
        free_trial_checked_at=free_trial_checked_at or "",
        free_trial_error=free_trial_error or "",
        agent_runtime_id=agent_runtime_id or "",
        agent_private_key=agent_private_key or "",
        agent_account_id=agent_account_id or "",
        agent_user_id=agent_user_id or "",
    )


def write_failure(email: str,
                  reason: str,
                  stage: str = "",
                  url: str = "",
                  password: str = "",
                  refresh_token: str = "",
                  client_id: str = "") -> None:
    """写一条失败记录到 failed.txt"""
    _ensure_dir()
    block = []
    block.append("-" * 64)
    block.append(f"  失败 [{_now()}]")
    block.append(f"  邮箱:    {email}")
    if password:
        block.append(f"  密码:    {password}")
    if stage:
        block.append(f"  阶段:    {stage}")
    block.append(f"  原因:    {reason}")
    if url:
        block.append(f"  最终URL: {url[:200]}")
    if refresh_token:
        block.append(f"  refresh_token: {refresh_token[:60]}...")
    block.append("-" * 64)
    block.append("")
    with _lock:
        with open(FAILED_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(block))
            f.write("\n")
    log.info(f"  [output] 失败记录写入 failed.txt: {email} - {reason}")


def stats() -> dict:
    """读 success.txt / failed.txt 数行做汇总。"""
    _ensure_dir()
    succ = 0
    fail = 0
    if SUCCESS_FILE.exists():
        succ = sum(1 for line in SUCCESS_FILE.read_text(encoding="utf-8").splitlines()
                   if line.startswith("邮箱:"))
    if FAILED_FILE.exists():
        fail = sum(1 for line in FAILED_FILE.read_text(encoding="utf-8").splitlines()
                   if line.strip().startswith("邮箱:"))
    return {"success": succ, "failed": fail, "total": succ + fail}
