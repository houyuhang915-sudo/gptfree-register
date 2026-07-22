"""
绑定手机号 — 给已有 ChatGPT 账号绑定手机号并获取 Codex RT。

参考 _aBaiAutoplus-ref/application/phone_binding.py 的服务层设计，搬到本项目里：
  - platform：SMS 平台动态取号 + 纯 HTTP Codex OAuth/add-phone/RT
  - manual：phone_lines 多行 `phone----sms_api` + 原浏览器流程
  - 一号最多绑 3 个账号（MAX_ACCOUNTS_PER_PHONE = 3）
  - 同号串行：进程内 threading.Lock + 跨进程 fcntl.flock，保证多 worker subprocess 不会
    在同一个号上撞 OTP
  - 不同号可并发
  - manual 浏览器层走 pipeline.GPTPipeline.bind_phone_to_account（DrissionPage）
  - 拉 OTP 走 pipeline.fetch_sms_otp（兼容 62-us / headone / 任意 GET URL 协议）
  - 号被占用（phone_in_use）时自动从池子里换一个号继续，不浪费已勾选的账号
  - 绑定成功后自动落库：output/phone_bindings.json + 更新 outlook_accounts_success.txt 第 5 列

公开 API:
    parse_phone_lines(raw)            → list[PhoneEntry]
    bind_accounts_with_protocol(...)  → platform 全协议批量绑定
    PhoneBindingService().bind(...)   → manual 浏览器批量绑定
    binding_store.* (load/save/list)  → 持久化的绑定记录

CLI 入口在 bind_phone.py。WebUI 路由在 webui.py 里 /api/phone-bind/*。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger("phone_binding")

# 一个手机号最多用来给多少个账号绑（参考 ref 项目同款常量）
MAX_ACCOUNTS_PER_PHONE = 3


@dataclass(frozen=True)
class PhoneEntry:
    phone: str          # E.164 格式，'+1...' / '+81...'
    sms_api: str        # GET 一次返回 SMS 文本的 URL


@dataclass
class AccountEntry:
    """一个待绑定的 ChatGPT 账号。

    至少要有 email + password；refresh_token / client_id 可选（拿来登录后
    刷邮箱 OTP，邮箱重登验证用）。
    """
    email: str
    password: str
    refresh_token: str = ""
    client_id: str = ""


def _normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return ""
    # 10 位号默认补 US 国码 +1（跟 ref 行为对齐）
    if len(digits) == 10:
        return f"+1{digits}"
    return f"+{digits}"


def parse_phone_lines(raw: str) -> list[PhoneEntry]:
    """解析多行 `phone----sms_api`。

    跟 _aBaiAutoplus-ref parse_phone_bind_lines 同语义：
      - 空行 / 以 # 开头跳过
      - 必须包含 ---- 且 sms_api 是 http(s)://
      - 10 位号补成 +1...
    """
    entries: list[PhoneEntry] = []
    for line in str(raw or "").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if "----" not in text:
            raise ValueError(f"invalid phone line (missing '----'): {text}")
        phone_raw, sms_api = text.split("----", 1)
        phone = _normalize_phone(phone_raw.strip())
        sms_api = sms_api.strip()
        if not phone or not sms_api.startswith(("http://", "https://")):
            raise ValueError(f"invalid phone line: {text}")
        entries.append(PhoneEntry(phone=phone, sms_api=sms_api))
    if not entries:
        raise ValueError("phone_lines is empty")
    return entries


# ============================================================
#  账号源：账号管理统一记录 + 历史账号文件兼容
# ============================================================

_DEFAULT_ACCOUNTS_FILE = Path(__file__).parent / "outlook_accounts.txt"
_DEFAULT_ICLOUD_FILE = Path(__file__).parent / "icloud_accounts.txt"
_DEFAULT_SUCCESS_FILE = Path(__file__).parent / "output" / "outlook_accounts_success.txt"


def parse_account_lines(raw: str) -> list[AccountEntry]:
    """解析 outlook_accounts 风格 4 段：

        email----password----client_id----refresh_token

    也兼容 icloud 风格 2 段（email----relay_url）：refresh_token 字段填 relay_url，
    email_provider.fetch_otp 看到 URL 形态自动走 fetch_otp_relay。
    """
    out: list[AccountEntry] = []
    for line in str(raw or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split("----")
        if len(parts) < 2:
            continue
        email = parts[0].strip()
        if not email:
            continue
        # icloud 2 段：email----relay_url
        if len(parts) == 2:
            relay = parts[1].strip()
            if relay.startswith(("http://", "https://")):
                out.append(AccountEntry(
                    email=email,
                    password="",
                    client_id="",
                    refresh_token=relay,   # URL；fetch_otp 自动走 relay
                ))
            continue
        # outlook 4+ 段
        password = parts[1].strip()
        client_id = parts[2].strip() if len(parts) > 2 else ""
        refresh_token = parts[3].strip() if len(parts) > 3 else ""
        if not password and not refresh_token:
            continue
        out.append(AccountEntry(
            email=email,
            password=password,
            client_id=client_id,
            refresh_token=refresh_token,
        ))
    return out


def load_accounts_from_file(path: str | Path) -> list[AccountEntry]:
    p = Path(path)
    if not p.exists():
        return []
    return parse_account_lines(p.read_text(encoding="utf-8"))


def load_bind_candidates() -> list[AccountEntry]:
    """Load bindable accounts from the same sources as the account manager.

    The active Outlook/iCloud pool supplies mailbox credentials, while unified
    export records supply the ChatGPT password when available. Historical
    ``outlook_accounts_success.txt`` rows remain a final compatibility source.
    """

    legacy = load_accounts_from_file(_DEFAULT_SUCCESS_FILE)
    legacy_by_email = {account.email.lower(): account for account in legacy}
    try:
        import accounts_pool

        pool_accounts = accounts_pool.load_pool()
    except Exception:
        pool_accounts = []
    try:
        import account_exports

        records = account_exports.load_records()
    except Exception:
        records = []

    pool_by_email = {
        str(getattr(account, "email", "") or "").strip().lower(): account
        for account in pool_accounts
        if str(getattr(account, "email", "") or "").strip()
    }
    records_by_email = {
        str(getattr(record, "email", "") or "").strip().lower(): record
        for record in records
        if str(getattr(record, "email", "") or "").strip()
    }

    ordered_keys: list[str] = []
    ordered_emails: dict[str, str] = {}
    for source in (pool_accounts, records, legacy):
        for item in source:
            email = str(getattr(item, "email", "") or "").strip()
            key = email.lower()
            if not key or key in ordered_emails:
                continue
            ordered_keys.append(key)
            ordered_emails[key] = email

    out: list[AccountEntry] = []
    for key in ordered_keys:
        pool_account = pool_by_email.get(key)
        record = records_by_email.get(key)
        fallback = legacy_by_email.get(key)
        password = str(
            (getattr(record, "password", "") if record is not None else "")
            or (getattr(pool_account, "password", "") if pool_account is not None else "")
            or (fallback.password if fallback else "")
        ).strip()
        client_id = str(
            (getattr(record, "outlook_client_id", "") if record is not None else "")
            or (getattr(pool_account, "client_id", "") if pool_account is not None else "")
            or (fallback.client_id if fallback else "")
        ).strip()
        refresh_token = str(
            (getattr(record, "outlook_refresh_token", "") if record is not None else "")
            or (getattr(pool_account, "refresh_token", "") if pool_account is not None else "")
            or (getattr(pool_account, "relay_url", "") if pool_account is not None else "")
            or (fallback.refresh_token if fallback else "")
        ).strip()
        # A bind flow needs a ChatGPT password or an email OTP credential.
        if not password and not refresh_token:
            continue
        out.append(AccountEntry(
            email=(
                str(getattr(record, "email", "") or "").strip()
                if record is not None else ordered_emails[key]
            ),
            password=password,
            client_id=client_id,
            refresh_token=refresh_token,
        ))
    return out


def list_bind_candidate_emails() -> set[str]:
    """Return lowercase emails currently selectable in the bind UI."""

    return {account.email.lower() for account in load_bind_candidates()}


def load_plus_success_accounts() -> list[AccountEntry]:
    """Compatibility alias for the historical bind-candidate loader."""

    return load_bind_candidates()


def list_plus_success_emails() -> set[str]:
    """Compatibility alias for the historical candidate-list function name."""

    return list_bind_candidate_emails()


def resolve_accounts(
    *,
    emails: list[str] | None = None,
    accounts_text: str = "",
    accounts_file: str | Path | None = None,
    plus_only: bool = False,
) -> list[AccountEntry]:
    """三种模式合并解析：

      1. emails 列表 + accounts_file：从 file 里挑指定邮箱
      2. accounts_text 里带完整 4 段：直接用
      3. 都没传：从默认 outlook_accounts.txt 拿全部

    plus_only=True 时只保留账号管理候选列表里出现过的 email。
    参数名为历史兼容名，不表示账号计划必须是 Plus。

    返回去重后的 AccountEntry 列表（按 email 去重，保留第一次出现）。
    """
    seen_email = set()
    out: list[AccountEntry] = []
    # The historical ``plus_only`` name is retained for callers.
    enriched_candidates = {
        account.email.lower(): account
        for account in load_bind_candidates()
        if account.email
    }
    plus_emails = set(enriched_candidates) if plus_only else None

    def _accept(entry: AccountEntry) -> bool:
        if plus_emails is not None and entry.email.lower() not in plus_emails:
            return False
        return True

    if accounts_text:
        for entry in parse_account_lines(accounts_text):
            key = entry.email.lower()
            if key in seen_email:
                continue
            if not _accept(entry):
                log.warning(f"  [phone_binding] {entry.email} 不在账号管理候选列表里，已跳过")
                continue
            seen_email.add(key)
            out.append(entry)

    if emails:
        # 显式文件保持兼容；默认从加密账号仓储读取。
        files = []
        if accounts_file:
            files.append(Path(accounts_file))
        pool: dict[str, AccountEntry] = {}
        if not accounts_file:
            try:
                import accounts_pool

                for account in accounts_pool.load_pool():
                    pool[account.email.lower()] = AccountEntry(
                        email=account.email,
                        password=account.password,
                        client_id=account.client_id,
                        refresh_token=account.refresh_token or account.relay_url,
                    )
            except Exception as exc:
                log.warning(f"  [phone_binding] 加密账号仓储读取失败: {exc}")
            files.append(_DEFAULT_SUCCESS_FILE)
        for f in files:
            for entry in load_accounts_from_file(f):
                pool.setdefault(entry.email.lower(), entry)
        # success.txt + binding store provide the ChatGPT password and the
        # matching Outlook credentials. Prefer that unified view over the raw
        # Outlook source, whose second column is the mailbox password.
        if accounts_file:
            for key in list(pool):
                if key in enriched_candidates:
                    pool[key] = enriched_candidates[key]
        else:
            pool.update(enriched_candidates)
        for email in emails:
            key = (email or "").strip().lower()
            if not key or key in seen_email:
                continue
            entry = pool.get(key)
            if not entry:
                log.warning(f"  [phone_binding] 账号 {email} 在账号文件里找不到")
                continue
            if not _accept(entry):
                log.warning(f"  [phone_binding] 账号 {email} 不在账号管理候选列表里，已跳过")
                continue
            seen_email.add(key)
            out.append(entry)
        return out

    if not out and not emails:
        # 兜底：默认账号文件全部（plus_only 时只取 success 文件）
        if accounts_file:
            sources = [Path(accounts_file)]
        elif plus_only:
            sources = []
        else:
            sources = []
        if plus_only and not accounts_file:
            out.extend(enriched_candidates.values())
            seen_email.update(enriched_candidates)
        elif not accounts_file:
            try:
                import accounts_pool

                for account in accounts_pool.load_pool():
                    key = account.email.lower()
                    if key in seen_email:
                        continue
                    seen_email.add(key)
                    out.append(AccountEntry(
                        email=account.email,
                        password=account.password,
                        client_id=account.client_id,
                        refresh_token=account.refresh_token or account.relay_url,
                    ))
            except Exception as exc:
                log.warning(f"  [phone_binding] 加密账号仓储读取失败: {exc}")
        for f in sources:
            for entry in load_accounts_from_file(f):
                key = entry.email.lower()
                if key in seen_email:
                    continue
                if not _accept(entry):
                    continue
                entry = enriched_candidates.get(key, entry)
                seen_email.add(key)
                out.append(entry)

    return out


# ============================================================
#  默认 binder：DrissionPage + GPTPipeline.bind_phone_to_account
# ============================================================

Binder = Callable[..., dict[str, Any]]


def default_phone_binder(
    account: AccountEntry,
    phone_entry: PhoneEntry,
    *,
    use_bitbrowser: bool = False,
    bb_proxy: dict | None = None,
    log_fn: Callable[[str], Any] | None = None,
    keep_browser_seconds: int = 5,
    rt_only: bool = False,
    sms_provider=None,
    sms_activation: dict | None = None,
    window_index: int | None = None,
) -> dict[str, Any]:
    """单账号绑定：起浏览器 → login → add-phone → 拉 OTP → 提交。

    rt_only=True 时跳过 add-phone 流程（账号已经绑过号），只走完 OAuth 链路
    把 refresh_token 拿出来。

    返回:
        {
          "ok": True/False,
          "phone": "+1xxx" 或 ""（rt_only 时空）,
          "error": "...",            # 失败时
          "access_token": "...",     # 绑定成功后
          "refresh_token": "...",    # OAuth 成功后
          "id_token": "...",
        }
    """
    log = log_fn or (lambda _msg: None)
    page = None
    bb_id = ""
    try:
        # 启动浏览器
        if use_bitbrowser:
            from browser_mgr import (
                bb_health, open_bitbrowser_with_url, bb_close_window,
                bb_delete_window,
            )
            if not bb_health():
                return {"ok": False, "phone": phone_entry.phone,
                        "error": "BitBrowser 未运行，无法走指纹模式"}
            mode_label = "rt-only" if rt_only else "phone-bind"
            log(f"[{mode_label}] 启动 BitBrowser ephemeral profile (account={account.email}) slot={window_index}")
            page, bb_id = open_bitbrowser_with_url(
                "https://chatgpt.com/auth/login",
                name=f"{mode_label}_{account.email.split('@')[0][:16]}_{int(time.time())}",
                proxy=bb_proxy,
                goto=True,
                window_index=window_index,
            )
        else:
            from browser_mgr import open_local_chromium
            page = open_local_chromium(window_index=window_index)

        from pipeline import GPTPipeline
        pipeline = GPTPipeline(page)
        outlook_creds = {
            "email": account.email,
            "refresh_token": account.refresh_token,
            "client_id": account.client_id,
            "password": account.password,
        }

        if rt_only:
            # 直接跑 OAuth — 完全不走 chatgpt.com 登录。
            # auth.openai.com 跟 chatgpt.com 不共享 session，先在 chatgpt.com 登录
            # 反而会让 OAuth 链路再要一次 OTP。直接 navigate 到 auth_url，链路内部
            # 处理 email_entry → 邮箱 OTP → consent → callback。
            from codex_oauth import run_codex_oauth_on_page
            oauth = run_codex_oauth_on_page(
                pipeline,
                email=account.email,
                outlook_creds=outlook_creds,
                log_fn=log,
            )
            if not oauth or not oauth.get("access_token"):
                err = (oauth or {}).get("error") or "oauth_failed_no_token"
                return {"ok": False, "phone": "", "error": err,
                        "stage": (oauth or {}).get("stage") or "oauth"}
            return {
                "ok": True,
                "phone": "",
                "access_token": oauth.get("access_token") or "",
                "refresh_token": oauth.get("refresh_token") or "",
                "id_token": oauth.get("id_token") or "",
                "chatgpt_account_id": oauth.get("account_id") or "",
            }

        # 否则走原绑号流程（sms_api URL 或平台 provider+activation）
        result = pipeline.bind_phone_to_account(
            email=account.email,
            password=account.password,
            phone=phone_entry.phone if phone_entry else "",
            sms_api=(phone_entry.sms_api if phone_entry else "") or "",
            outlook_creds=outlook_creds,
            log_fn=log,
            sms_provider=sms_provider,
            sms_activation=sms_activation,
        )
        return result
    except Exception as exc:
        log(f"[phone-bind] 异常: {exc}")
        return {
            "ok": False,
            "phone": (phone_entry.phone if phone_entry else "") or "",
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if keep_browser_seconds > 0:
            try:
                time.sleep(keep_browser_seconds)
            except KeyboardInterrupt:
                pass
        try:
            if page is not None:
                page.quit()
        except Exception:
            pass
        if bb_id:
            try:
                from browser_mgr import bb_close_window, bb_delete_window
                bb_close_window(bb_id)
                bb_delete_window(bb_id)
            except Exception:
                pass


# ============================================================
#  Phone binding service
# ============================================================

# ============================================================
#  跨进程 per-phone 锁（基于 phone 哈希的 fcntl.flock）
# ============================================================
#
# 进程内已经有 threading.Lock 串行同号；跨 subprocess（webui 一键绑号 +
# CLI 同时跑、或 run_parallel 多个 worker 一起绑）需要文件锁兜底，
# 否则两个 subprocess 拿到同一个号会同时去 add-phone，OpenAI 会以
# `phone_number_in_use` 拒绝其中一个。

_LOCK_DIR = Path(tempfile.gettempdir())
_LOCK_PREFIX = "gpt_phone_bind_"


def _phone_lock_path(phone: str) -> Path:
    key = hashlib.md5(phone.encode("utf-8")).hexdigest()[:16]
    return _LOCK_DIR / f"{_LOCK_PREFIX}{key}.lock"


@contextmanager
def _phone_file_lock(phone: str):
    """阻塞文件锁；同号跨进程串行。"""
    path = _phone_lock_path(phone)
    fp = open(path, "w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fp, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fp.close()
        except Exception:
            pass


# ============================================================
#  绑定记录持久化（output/phone_bindings.json + outlook_accounts_success.txt 第 5 列）
# ============================================================

OUTPUT_DIR = Path(__file__).parent / "output"
PHONE_BIND_RESULT_FILE = OUTPUT_DIR / "phone_bind_results.txt"
PHONE_BIND_SUCCESS_FILE = OUTPUT_DIR / "phone_bind_success.txt"
PHONE_BIND_STORE_FILE = OUTPUT_DIR / "phone_bindings.json"
OUTLOOK_SUCCESS_FILE = OUTPUT_DIR / "outlook_accounts_success.txt"


class _BindingStore:
    """`output/phone_bindings.json` 的薄读写层。

    结构（JSON object，key 是 email 小写）::

        {
          "alice@outlook.com": {
            "email": "alice@outlook.com",
            "phone": "+17857019646",
            "sms_api": "https://...",
            "access_token": "...",        # 绑定后 fetch /api/auth/session 拿到的
            "refresh_token": "",          # 绑定流程不主动跑 Codex OAuth；空字符串
            "id_token": "",
            "client_id": "outlook 注册时的 client_id",
            "outlook_refresh_token": "...",
            "bound_at": "2026-06-02 12:34:56"
          },
          ...
        }

    并发安全用文件锁；多 worker subprocess 同时写也不会丢。
    """

    LOCK = _LOCK_DIR / f"{_LOCK_PREFIX}_store.lock"

    def _load_locked(self) -> dict[str, Any]:
        if not PHONE_BIND_STORE_FILE.exists():
            return {}
        try:
            return json.loads(PHONE_BIND_STORE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @contextmanager
    def _write_lock(self):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        fp = open(self.LOCK, "w")
        try:
            fcntl.flock(fp, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fp, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fp.close()
            except Exception:
                pass

    def load_all(self) -> dict[str, Any]:
        with self._write_lock():
            return self._load_locked()

    def get(self, email: str) -> dict[str, Any] | None:
        if not email:
            return None
        return self.load_all().get(email.strip().lower())

    def list_bound_emails(self) -> set[str]:
        return {
            email
            for email, record in self.load_all().items()
            if str((record or {}).get("phone") or "").strip()
        }

    def record_binding(self, *, email: str, phone: str = "", sms_api: str = "",
                       access_token: str = "", refresh_token: str = "",
                       id_token: str = "",
                       chatgpt_account_id: str = "",
                       activation_id: str = "",
                       sms_provider: str = "",
                       phone_attempts: int = 0,
                       client_id: str = "",
                       outlook_refresh_token: str = "",
                       session_token: str = "",
                       plan_type: str = "",
                       free_trial_status: str = "",
                       free_trial_eligible: bool | None = None,
                       free_trial_campaign: str = "",
                       free_trial_checked_at: str = "",
                       free_trial_error: str = "") -> None:
        with self._write_lock():
            data = self._load_locked()
            key = email.strip().lower()
            existing = data.get(key) or {}
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            stored_phone = str(existing.get("phone") or "")
            bound_at = str(existing.get("bound_at") or "")
            if phone and (not stored_phone or phone != stored_phone or not bound_at):
                bound_at = now
            trial_status = str(free_trial_status or "").strip().lower()
            if trial_status:
                trial_eligible = (
                    None if free_trial_eligible is None else bool(free_trial_eligible)
                )
            else:
                trial_eligible = existing.get("free_trial_eligible")
            entry = {
                **existing,
                "email": email,
                # phone/sms_api 也走 fallback：RT-only 模式不传 phone，但已绑过的
                # 记录里有 phone，不应该被覆盖成空字符串。
                "phone": phone or existing.get("phone", ""),
                "sms_api": sms_api or existing.get("sms_api", ""),
                # 不覆盖之前可能更全的字段（如果新调用没传 token 就保留原值）
                "access_token": access_token or existing.get("access_token", ""),
                "refresh_token": refresh_token or existing.get("refresh_token", ""),
                "id_token": id_token or existing.get("id_token", ""),
                "chatgpt_account_id": (
                    chatgpt_account_id or existing.get("chatgpt_account_id", "")
                ),
                "activation_id": activation_id or existing.get("activation_id", ""),
                "sms_provider": sms_provider or existing.get("sms_provider", ""),
                "phone_attempts": (
                    int(phone_attempts)
                    if int(phone_attempts or 0) > 0
                    else int(existing.get("phone_attempts") or 0)
                ),
                "client_id": client_id or existing.get("client_id", ""),
                "outlook_refresh_token": outlook_refresh_token
                    or existing.get("outlook_refresh_token", ""),
                "session_token": session_token or existing.get("session_token", ""),
                "plan_type": plan_type or existing.get("plan_type", ""),
                "free_trial_status": trial_status
                    or existing.get("free_trial_status", ""),
                "free_trial_eligible": trial_eligible,
                "free_trial_campaign": (
                    free_trial_campaign
                    if trial_status else existing.get("free_trial_campaign", "")
                ),
                "free_trial_checked_at": (
                    free_trial_checked_at
                    if trial_status else existing.get("free_trial_checked_at", "")
                ),
                "free_trial_error": (
                    free_trial_error
                    if trial_status else existing.get("free_trial_error", "")
                ),
                "bound_at": bound_at,
                "updated_at": now,
            }
            data[key] = entry
            PHONE_BIND_STORE_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # Keep both stores in the same critical section so concurrent
            # rebinds of one account cannot leave them pointing at two numbers.
            if phone:
                try:
                    self._update_outlook_success_file(email=email, phone=phone)
                except Exception as exc:
                    log.debug(
                        "  [binding_store] 更新 outlook_accounts_success.txt "
                        f"失败（不致命）: {exc}"
                    )

    def _update_outlook_success_file(self, *, email: str, phone: str) -> None:
        """把 outlook_accounts_success.txt 中匹配 email 的行升到 5 段：
        email----password----client_id----refresh_token----phone

        如果该 email 不在文件里，不主动新增（成功记录由 output_writer.write_success 写入）。
        """
        if not OUTLOOK_SUCCESS_FILE.exists():
            return
        lines = OUTLOOK_SUCCESS_FILE.read_text(encoding="utf-8").splitlines()
        changed = False
        new_lines = []
        target = email.strip().lower()
        for ln in lines:
            s = ln.rstrip()
            if not s.strip() or s.startswith("#"):
                new_lines.append(s)
                continue
            parts = s.split("----")
            if not parts or parts[0].strip().lower() != target:
                new_lines.append(s)
                continue
            # 升级到 5 段
            while len(parts) < 4:
                parts.append("")
            if len(parts) >= 5:
                parts[4] = phone
            else:
                parts.append(phone)
            new_lines.append("----".join(parts))
            changed = True
        if changed:
            OUTLOOK_SUCCESS_FILE.write_text("\n".join(new_lines) + "\n",
                                            encoding="utf-8")


binding_store = _BindingStore()


# ============================================================
#  Platform 全协议绑定：SMS 动态取号 + Codex OAuth/add-phone/RT
# ============================================================


def bind_account_with_protocol(
    account: AccountEntry,
    *,
    proxy: str = "",
    sms_provider: Any | None = None,
    require_phone: bool = True,
    email_otp_timeout: int = 180,
    sms_otp_timeout: int = 30,
    sms_max_attempts: int = 3,
    sms_max_otp_retries: int = 2,
    persist: bool = True,
    plan_type: str = "",
    log_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Run one account through the shared pure-HTTP Codex OAuth phone flow."""

    say = log_fn or (lambda _m: None)
    provider = sms_provider
    if require_phone and provider is None:
        try:
            from gpt_trial_protocol.sms import from_legacy_provider
            from sms_provider import get_sms_provider

            legacy = get_sms_provider(purpose="openai")
            if legacy is None:
                return {
                    "ok": False,
                    "phone": "",
                    "phone_bound": False,
                    "phone_bound_in_flow": False,
                    "stage": "sms_provider",
                    "error": "Free/OpenAI SMS provider is not configured",
                    "activation_id": "",
                    "sms_provider": "",
                    "phone_attempts": 0,
                    "sms_source": "platform",
                    "bind_mode": "protocol",
                }
            provider = from_legacy_provider(legacy)
        except Exception as exc:
            return {
                "ok": False,
                "phone": "",
                "phone_bound": False,
                "phone_bound_in_flow": False,
                "stage": "sms_provider",
                "error": f"sms_provider load failed: {type(exc).__name__}: {exc}",
                "activation_id": "",
                "sms_provider": "",
                "phone_attempts": 0,
                "sms_source": "platform",
                "bind_mode": "protocol",
            }

    existing = binding_store.get(account.email) or {}
    existing_phone = str(existing.get("phone") or "").strip()

    def emit(name: str, payload: dict[str, Any]) -> None:
        suffix = (
            " " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if payload else ""
        )
        say(f"[bind/protocol] event={name}{suffix}")

    def persist_result(
        *,
        phone: str,
        access_token: str = "",
        refresh_token: str = "",
        id_token: str = "",
        account_id: str = "",
        activation_id: str = "",
        sms_provider_name: str = "",
        phone_attempts: int = 0,
    ) -> None:
        if not persist:
            return
        used_sms_activation = bool(
            activation_id or sms_provider_name or int(phone_attempts or 0) > 0
        )
        binding_store.record_binding(
            email=account.email,
            phone=phone,
            sms_api=(
                f"platform:{sms_provider_name or getattr(provider, 'name', 'sms')}:protocol"
                if used_sms_activation else ""
            ),
            access_token=access_token,
            refresh_token=refresh_token,
            id_token=id_token,
            chatgpt_account_id=account_id,
            activation_id=activation_id,
            sms_provider=sms_provider_name,
            phone_attempts=phone_attempts,
            client_id=account.client_id,
            outlook_refresh_token=account.refresh_token,
            plan_type=plan_type,
        )

    try:
        from gpt_trial_protocol.codex_oauth import run_codex_oauth_protocol

        oauth = run_codex_oauth_protocol(
            email=account.email,
            password=account.password,
            outlook_refresh_token=account.refresh_token,
            outlook_client_id=account.client_id,
            sms_provider=provider,
            proxy=proxy,
            log_fn=say,
            on_event=emit,
            email_otp_timeout=max(30, int(email_otp_timeout or 180)),
            sms_otp_timeout=max(30, int(sms_otp_timeout or 30)),
            sms_max_attempts=max(1, int(sms_max_attempts or 1)),
            sms_max_otp_retries=max(0, int(sms_max_otp_retries or 0)),
        )
        oauth_ok = bool(oauth.get("ok"))
        flow_phone_bound = bool(oauth.get("phone_bound"))
        phone = str(oauth.get("phone") or existing_phone)
        phone_bound = flow_phone_bound or bool(existing_phone)
        ok = oauth_ok and (phone_bound or not require_phone)
        error = str(oauth.get("error") or "")
        if oauth_ok and require_phone and not phone_bound:
            error = "Codex OAuth completed but add-phone was not reached or completed"
        if oauth_ok:
            try:
                persist_result(
                    phone=phone,
                    access_token=str(oauth.get("access_token") or ""),
                    refresh_token=str(oauth.get("refresh_token") or ""),
                    id_token=str(oauth.get("id_token") or ""),
                    account_id=str(oauth.get("account_id") or ""),
                    activation_id=str(oauth.get("activation_id") or ""),
                    sms_provider_name=str(oauth.get("sms_provider") or ""),
                    phone_attempts=int(oauth.get("phone_attempts") or 0),
                )
            except Exception as exc:
                say(f"[bind/protocol] persist warning: {exc}")
        return {
            "ok": ok,
            "phone": phone,
            "phone_bound": phone_bound,
            "phone_bound_in_flow": flow_phone_bound,
            "error": error or ("" if ok else "bind_failed"),
            "access_token": str(oauth.get("access_token") or ""),
            "refresh_token": str(oauth.get("refresh_token") or ""),
            "id_token": str(oauth.get("id_token") or ""),
            "account_id": str(oauth.get("account_id") or ""),
            "stage": str(oauth.get("stage") or ("rt_ready" if oauth_ok else "")),
            "activation_id": str(oauth.get("activation_id") or ""),
            "sms_provider": str(oauth.get("sms_provider") or ""),
            "phone_attempts": int(oauth.get("phone_attempts") or 0),
            "sms_source": "platform",
            "bind_mode": "protocol",
        }
    except Exception as exc:
        partial = getattr(exc, "phone_result", {}) or {}
        partial_phone = str(partial.get("phone") or "")
        if partial_phone:
            try:
                persist_result(
                    phone=partial_phone,
                    activation_id=str(partial.get("activation_id") or ""),
                    sms_provider_name=str(partial.get("provider") or ""),
                    phone_attempts=int(partial.get("attempts") or 0),
                )
            except Exception as persist_exc:
                say(f"[bind/protocol] partial persist warning: {persist_exc}")
        return {
            "ok": False,
            "phone": partial_phone or existing_phone,
            "phone_bound": bool(partial_phone or existing_phone),
            "phone_bound_in_flow": bool(partial_phone),
            "error": f"protocol_bind exception: {type(exc).__name__}: {exc}",
            "access_token": "",
            "refresh_token": "",
            "id_token": "",
            "account_id": "",
            "stage": str(getattr(exc, "stage", "") or ""),
            "activation_id": str(partial.get("activation_id") or ""),
            "sms_provider": str(partial.get("provider") or ""),
            "phone_attempts": int(partial.get("attempts") or 0),
            "sms_source": "platform",
            "bind_mode": "protocol",
        }


def bind_accounts_with_protocol(
    *,
    accounts: list[AccountEntry],
    proxy: str = "",
    concurrency: int = 1,
    email_otp_timeout: int = 180,
    sms_otp_timeout: int = 30,
    sms_max_attempts: int = 3,
    sms_max_otp_retries: int = 2,
    persist: bool = True,
    plan_type: str = "",
    log_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Batch platform binding. Each account leases its own SMS activation."""

    if not accounts:
        raise ValueError("accounts is empty")
    say = log_fn or (lambda _m: None)
    try:
        from gpt_trial_protocol.sms import from_legacy_provider
        from sms_provider import get_sms_provider

        legacy = get_sms_provider(purpose="openai")
        if legacy is None:
            raise ValueError("Free/OpenAI SMS provider is not configured")
        provider = from_legacy_provider(legacy)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"SMS provider load failed: {exc}") from exc

    say(
        f"[bind/protocol] provider={getattr(provider, 'name', '?')} "
        f"service={getattr(legacy, 'service', '?')} "
        f"country={getattr(legacy, 'country', '?')}"
    )
    total = len(accounts)
    worker_count = min(max(int(concurrency or 1), 1), total, 32)
    results: list[dict[str, Any] | None] = [None] * total

    def run_one(idx: int, account: AccountEntry) -> dict[str, Any]:
        say(f"[bind/protocol] [{idx + 1}/{total}] start {account.email}")
        item = bind_account_with_protocol(
            account,
            proxy=proxy,
            sms_provider=provider,
            require_phone=True,
            email_otp_timeout=email_otp_timeout,
            sms_otp_timeout=sms_otp_timeout,
            sms_max_attempts=sms_max_attempts,
            sms_max_otp_retries=sms_max_otp_retries,
            persist=persist,
            plan_type=plan_type,
            log_fn=say,
        )
        item.update({
            "index": idx,
            "email": account.email,
            "bound_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        if item.get("ok"):
            say(f"[bind/protocol] [{idx + 1}/{total}] success {account.email}")
        elif item.get("phone_bound"):
            say(
                f"[bind/protocol] [{idx + 1}/{total}] phone bound but RT failed "
                f"{account.email}: {item.get('error')}"
            )
        else:
            say(
                f"[bind/protocol] [{idx + 1}/{total}] failed "
                f"{account.email}: {item.get('error')}"
            )
        return item

    if worker_count <= 1:
        for idx, account in enumerate(accounts):
            results[idx] = run_one(idx, account)
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            future_map = {
                pool.submit(run_one, idx, account): idx
                for idx, account in enumerate(accounts)
            }
            for future in as_completed(future_map):
                results[future_map[future]] = future.result()

    final = [item for item in results if item is not None]
    success_count = sum(1 for item in final if item.get("ok"))
    phone_stats: list[dict[str, Any]] = []
    for item in final:
        if not item.get("phone_bound_in_flow") or not item.get("phone"):
            continue
        phone_stats.append({
            "phone": item["phone"],
            "provider": getattr(provider, "name", ""),
            "activation_id": item.get("activation_id") or "",
            "used": 1,
            "success": 1,
            "failed": 0 if item.get("ok") else 1,
            "blacklisted": False,
        })
    return {
        "total": len(final),
        "success_count": success_count,
        "failure_count": len(final) - success_count,
        "concurrency": worker_count,
        "mode": "protocol",
        "phones": phone_stats,
        "results": final,
    }


# ============================================================
#  RT-only 提取：账号已经绑过号了，只跑 OAuth 拿 refresh_token
# ============================================================


def extract_refresh_token_only(
    *,
    accounts: list[AccountEntry],
    use_bitbrowser: bool = False,
    bb_proxy: dict | None = None,
    concurrency: int = 1,
    protocol: bool = False,
    proxy: str = "",
    email_otp_timeout: int = 180,
    log_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """对一批已经绑过手机号的 ChatGPT 账号跑一遍 Codex OAuth，拿 refresh_token。

    跟 PhoneBindingService.bind 共用同一套 worker/binder 设计，但：
      - 不需要 phone_lines / SMS API
      - OAuth 链路里不会路由到 add-phone（账号已绑过），直接 callback → 换 token
      - 成功后只更新 binding_store 的 token 字段，不动 phone

    Args:
        accounts: AccountEntry 列表（一般是 phone_binding.binding_store 里已有的账号）
        use_bitbrowser / bb_proxy: 浏览器模式
        concurrency: 并发数（不同账号之间）
        log_fn: 日志回调

    Returns:
        {
          total, success_count, failure_count, concurrency,
          results: [{email, ok, error?, refresh_token?, ...}, ...]
        }
    """
    if not accounts:
        raise ValueError("accounts is empty")
    say = log_fn or (lambda _m: None)

    results: list[dict[str, Any] | None] = [None] * len(accounts)
    worker_count = min(max(int(concurrency or 1), 1), len(accounts))
    total = len(accounts)

    def run_one(idx: int, account: AccountEntry) -> dict[str, Any]:
        say(f"[rt-only] [{idx + 1}/{total}] 开始: {account.email}")
        if protocol:
            result = bind_account_with_protocol(
                account,
                proxy=proxy,
                require_phone=False,
                email_otp_timeout=email_otp_timeout,
                persist=False,
                log_fn=say,
            )
        else:
            sentinel_entry = PhoneEntry(phone="", sms_api="")
            result = default_phone_binder(
                account, sentinel_entry,
                use_bitbrowser=use_bitbrowser,
                bb_proxy=bb_proxy,
                log_fn=say,
                rt_only=True,
            )
        ok = bool(result.get("ok"))
        if ok:
            try:
                binding_store.record_binding(
                    email=account.email,
                    access_token=str(result.get("access_token") or ""),
                    refresh_token=str(result.get("refresh_token") or ""),
                    id_token=str(result.get("id_token") or ""),
                    chatgpt_account_id=str(result.get("account_id") or ""),
                    client_id=account.client_id,
                    outlook_refresh_token=account.refresh_token,
                )
            except Exception as exc:
                say(f"[rt-only] [{idx + 1}/{total}] ⚠ 持久化失败（不致命）: {exc}")
            say(f"[rt-only] [{idx + 1}/{total}] ✓ 拿到 RT: {account.email}")
        else:
            say(f"[rt-only] [{idx + 1}/{total}] ✗ 失败: {account.email} {result.get('error')}")
        return {
            "index": idx,
            "email": account.email,
            "ok": ok,
            "error": str(result.get("error") or ""),
            "access_token": str(result.get("access_token") or ""),
            "refresh_token": str(result.get("refresh_token") or ""),
            "id_token": str(result.get("id_token") or ""),
            "account_id": str(result.get("account_id") or ""),
            "extracted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    if worker_count <= 1:
        for i, account in enumerate(accounts):
            results[i] = run_one(i, account)
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            future_map = {pool.submit(run_one, i, a): i for i, a in enumerate(accounts)}
            for future in as_completed(future_map):
                i = future_map[future]
                results[i] = future.result()

    final = [r for r in results if r is not None]
    success = sum(1 for r in final if r["ok"])
    return {
        "total": len(final),
        "success_count": success,
        "failure_count": len(final) - success,
        "concurrency": worker_count,
        "results": final,
    }


class PhoneBindingService:
    """批量绑定调度器。

    设计跟 _aBaiAutoplus-ref/application/phone_binding.py 同思路、扩展几项：
      - 容量校验：accounts <= len(phones) * MAX_ACCOUNTS_PER_PHONE
      - per-phone 锁：threading.Lock 进程内 + fcntl.flock 跨进程，同号串行
      - 跨手机号并发：worker_count = min(concurrency, len(accounts))
      - 号被占用（phone_number_in_use）时把号黑名单，剩余账号自动重分给其它号
      - 绑成功后写 binding_store + 追加 outlook_accounts_success.txt 第 5 列
      - 结果按 index 顺序返回
    """

    def __init__(self, binder: Binder | None = None,
                 *, persist: bool = True):
        self.binder = binder or default_phone_binder
        self.persist = persist

    def bind(
        self,
        *,
        accounts: list[AccountEntry],
        phone_lines: str,
        use_bitbrowser: bool = False,
        bb_proxy: dict | None = None,
        concurrency: int = 1,
        log_fn: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        if not accounts:
            raise ValueError("accounts is empty")
        entries = parse_phone_lines(phone_lines)
        capacity = len(entries) * MAX_ACCOUNTS_PER_PHONE
        if len(accounts) > capacity:
            raise ValueError(
                f"selected account count exceeds phone capacity: "
                f"accounts={len(accounts)} capacity={capacity} "
                f"(每号最多绑 {MAX_ACCOUNTS_PER_PHONE} 个)"
            )

        log = log_fn or (lambda _m: None)

        state_lock = threading.Lock()
        phone_use_count: dict[str, int] = {e.phone: 0 for e in entries}
        phone_blacklist: set[str] = set()
        phone_stats = {
            entry.phone: {"phone": entry.phone, "sms_api": entry.sms_api,
                          "used": 0, "success": 0, "failed": 0,
                          "blacklisted": False}
            for entry in entries
        }
        results: list[dict[str, Any] | None] = [None] * len(accounts)
        thread_locks = {entry.phone: threading.Lock() for entry in entries}

        worker_count = min(max(int(concurrency or 1), 1), len(accounts))
        total = len(accounts)

        def pick_phone() -> PhoneEntry | None:
            """挑一个仍有容量、且未被黑名单的号；选 use_count 最小的号均衡分摊。"""
            with state_lock:
                cands = [
                    e for e in entries
                    if e.phone not in phone_blacklist
                    and phone_use_count[e.phone] < MAX_ACCOUNTS_PER_PHONE
                ]
                if not cands:
                    return None
                cands.sort(key=lambda e: phone_use_count[e.phone])
                pick = cands[0]
                phone_use_count[pick.phone] += 1
                phone_stats[pick.phone]["used"] += 1
                return pick

        def revert_pick(entry: PhoneEntry, *, blacklist: bool = False):
            with state_lock:
                phone_use_count[entry.phone] = max(0, phone_use_count[entry.phone] - 1)
                phone_stats[entry.phone]["used"] = max(0, phone_stats[entry.phone]["used"] - 1)
                if blacklist:
                    phone_blacklist.add(entry.phone)
                    phone_stats[entry.phone]["blacklisted"] = True

        def run_one(idx: int, account: AccountEntry) -> dict[str, Any]:
            attempts = 0
            last_error = ""
            while attempts < len(entries):
                entry = pick_phone()
                if entry is None:
                    return {
                        "index": idx, "email": account.email, "phone": "",
                        "ok": False,
                        "error": last_error or "no_phone_available",
                        "access_token": "",
                        "bound_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                attempts += 1
                log(f"[{idx + 1}/{total}] 尝试绑 {account.email} -> {entry.phone}")

                # 进程内 + 跨进程双重锁
                with thread_locks[entry.phone], _phone_file_lock(entry.phone):
                    bind_result = self.binder(
                        account, entry,
                        use_bitbrowser=use_bitbrowser,
                        bb_proxy=bb_proxy,
                        log_fn=log,
                    ) or {}
                ok = bool(bind_result.get("ok"))
                error = str(bind_result.get("error") or "")

                if ok:
                    with state_lock:
                        phone_stats[entry.phone]["success"] += 1
                    if self.persist:
                        try:
                            binding_store.record_binding(
                                email=account.email,
                                phone=entry.phone,
                                sms_api=entry.sms_api,
                                access_token=str(bind_result.get("access_token") or ""),
                                refresh_token=str(bind_result.get("refresh_token") or ""),
                                id_token=str(bind_result.get("id_token") or ""),
                                client_id=account.client_id,
                                outlook_refresh_token=account.refresh_token,
                            )
                        except Exception as exc:
                            log(f"[{idx + 1}/{total}] ⚠ 持久化失败（不致命）: {exc}")
                    log(f"[{idx + 1}/{total}] ✓ 绑定成功 {account.email} -> {entry.phone}")
                    return {
                        "index": idx, "email": account.email, "phone": entry.phone,
                        "ok": True, "error": "",
                        "access_token": str(bind_result.get("access_token") or ""),
                        "refresh_token": str(bind_result.get("refresh_token") or ""),
                        "bound_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }

                last_error = error
                with state_lock:
                    phone_stats[entry.phone]["failed"] += 1
                phone_in_use = (
                    "phone_number_in_use" in error.lower()
                    or "in use" in error.lower()
                    or "already" in error.lower()
                )
                if phone_in_use:
                    log(f"[{idx + 1}/{total}] ⚠ {entry.phone} 已被占用，黑名单 + 换号")
                    revert_pick(entry, blacklist=True)
                    continue
                log(f"[{idx + 1}/{total}] ✗ 绑定失败 {account.email}: {error}")
                return {
                    "index": idx, "email": account.email, "phone": entry.phone,
                    "ok": False, "error": error or "unknown_error",
                    "access_token": "",
                    "bound_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            return {
                "index": idx, "email": account.email, "phone": "",
                "ok": False, "error": last_error or "all_phones_exhausted",
                "access_token": "",
                "bound_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        if worker_count <= 1:
            for i, account in enumerate(accounts):
                results[i] = run_one(i, account)
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                future_map = {
                    pool.submit(run_one, i, account): i
                    for i, account in enumerate(accounts)
                }
                for future in as_completed(future_map):
                    i = future_map[future]
                    results[i] = future.result()

        final = [item for item in results if item is not None]
        success_count = sum(1 for it in final if it["ok"])
        return {
            "total": len(final),
            "success_count": success_count,
            "failure_count": len(final) - success_count,
            "concurrency": worker_count,
            "phones": list(phone_stats.values()),
            "results": final,
        }


# ============================================================
#  写结果（每批次结果文件）
# ============================================================


def write_results(result: dict[str, Any]) -> Path:
    """把 batch 结果写到 output/phone_bind_results.txt（追加），
    成功项另写 phone_bind_success.txt（每行 email----phone）。
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    block: list[str] = []
    block.append("=" * 64)
    block.append(f"  绑定手机号批次 [{ts}]")
    block.append("=" * 64)
    block.append(f"总数: {result.get('total', 0)}    "
                 f"成功: {result.get('success_count', 0)}    "
                 f"失败: {result.get('failure_count', 0)}    "
                 f"并发: {result.get('concurrency', 1)}")
    block.append("")
    for ph in result.get("phones") or []:
        block.append(
            f"  [phone {ph.get('phone')}]  used={ph.get('used',0)} "
            f"success={ph.get('success',0)} failed={ph.get('failed',0)}"
        )
    block.append("")
    block.append("- 详情 -")
    for item in result.get("results") or []:
        flag = "✓" if item.get("ok") else "✗"
        line = f"  {flag} {item.get('email')} -> {item.get('phone')}"
        if not item.get("ok") and item.get("error"):
            line += f"   {item['error'][:120]}"
        block.append(line)
    block.append("=" * 64)
    block.append("")

    with open(PHONE_BIND_RESULT_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(block))
        f.write("\n")

    with open(PHONE_BIND_SUCCESS_FILE, "a", encoding="utf-8") as f:
        for item in result.get("results") or []:
            if item.get("ok"):
                if item.get("phone"):
                    f.write(f"{item['email']}----{item['phone']}----{item.get('bound_at','')}\n")

    return PHONE_BIND_RESULT_FILE
