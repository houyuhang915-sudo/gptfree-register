"""
账号 Plus 状态批量检查（多档降级，最大努力拿到权威结果）。

为什么"只解 JWT"不够：
  access_token JWT 里 chatgpt_plan_type 字段是**签发当时**的快照。账号在
  签发后才支付成功（free → plus），本地 JWT 不会自己更新。

判定逻辑（按优先级降级）：

  Tier 1 [快] 有 codex refresh_token：
       POST /oauth/token grant_type=refresh_token → 拿全新 ID_TOKEN
       OpenAI 后端在 token 交换时实时校 subscription，**权威**。

  Tier 2 [中] 没 codex RT 但有 outlook / relay 凭据：
       走 gpt_trial_protocol 的 HTTP 协议登录（邮箱 OTP）
       → GET /api/auth/session 拿登录态 JWT → 解 plan_type。同样**权威**。

  Tier 3 [中] 仅有 access_token：
       GET chatgpt.com/backend-api/me（Bearer AT）实时探测账号是否存活；
       200=存活并解 JWT plan；401/403=当前 AT 已失效，需后续复核。
       AT 是短期凭据，401/403 本身不能证明账号已停用。

  Tier 4：以上都不行 → 标 unknown_no_creds / no_token

  其它：refresh_token 失败但响应明确撤销 → rt_revoked

公开 API:
    bulk_check(*, emails, refresh_first, use_browser_fallback,
               concurrency, log_fn) -> dict

CLI: scripts/check_plan.py
WebUI: /api/plan-check/* （webui.py 里）

注意：Tier 2 不再起浏览器，但仍会触发邮箱 OTP 和 OpenAI 登录接口；
concurrency 默认控制在 2-3，避免 outlook IMAP / OpenAI 风控。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

log = logging.getLogger("plan_check")

OUTPUT_DIR = Path(__file__).parent / "output"
PLAN_CHECK_FILE = OUTPUT_DIR / "plan_check_results.json"
BANNED_REPORT_FILE = OUTPUT_DIR / "banned_accounts_sorted.tsv"


# ============================================================
#  JWT helpers (重复 account_exports 里的，避免循环 import)
# ============================================================

def _decode_jwt_payload(token: str) -> dict:
    if not token or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    pad = "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload + pad).decode("utf-8"))
    except Exception:
        return {}


def _jwt_auth_claims(token: str) -> dict:
    payload = _decode_jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth") or {}
    return auth if isinstance(auth, dict) else {}


def _jwt_exp(token: str) -> int:
    payload = _decode_jwt_payload(token)
    try:
        return int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return 0


# 已知的 Plus 类 plan_type（其它非特殊类型视为 free）
_PLUS_PLAN_TYPES = {"plus", "team", "enterprise", "edu", "pro"}
_K12_PLAN_TYPES = {"k12"}


# ============================================================
#  Helpers
# ============================================================

def _parse_iso_to_unix(s: str) -> int:
    """支持 '2026-07-01T08:56:46+00:00' / '...Z' 两种格式。"""
    if not s:
        return 0
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return 0


def _now_iso() -> str:
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


# ============================================================
#  Tier 2: 协议登录拿权威 access_token
# ============================================================


# 协议登录时单条邮箱 OTP 的最长等待秒数。
# 超过这个时间没拿到 OTP 就归类为 browser_login_failed（不算 plus 也不算 free）。
# 设短一点（默认 15s）避免一个慢账号卡住整批。
PLAN_CHECK_OTP_TIMEOUT_S = 15
PLAN_CHECK_LOGIN_DEADLINE_S = 60   # protocol login 总超时（含网络请求）

def _probe_access_token_alive(access_token: str, *, log_fn=None,
                               proxy: str | None = None) -> tuple[bool, str, dict]:
    """用 access_token 调 /backend-api/me 做存活探测。

    Returns:
        (alive, reason, me_json)
        alive=True  → HTTP 200，账号可用
        alive=False → 401/403/其它错误；reason 描述原因
    """
    say = log_fn or (lambda _m: None)
    token = (access_token or "").strip()
    proxy_url = str(proxy or "").strip()
    if not token:
        return False, "empty_access_token", {}

    # JWT 已过 exp 也先打，给后端最终裁决（有时 clock skew 仍可用）
    try:
        from curl_cffi import requests as _req
    except Exception:
        import requests as _req  # type: ignore

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "Referer": "https://chatgpt.com/",
        "Origin": "https://chatgpt.com",
    }
    url = "https://chatgpt.com/backend-api/me"
    try:
        kwargs = {"headers": headers, "timeout": 25}
        # curl_cffi
        if hasattr(_req, "Session") and "curl_cffi" in getattr(_req, "__name__", ""):
            if proxy_url:
                kwargs["proxy"] = proxy_url
            sess = _req.Session(impersonate="chrome136")
            try:
                resp = sess.get(url, **kwargs)
            finally:
                try:
                    sess.close()
                except Exception:
                    pass
        else:
            if proxy_url:
                kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
            resp = _req.get(url, **kwargs)
    except Exception as exc:
        say(f"  [plan_check/at] /backend-api/me 网络异常: {type(exc).__name__}: {exc}")
        return False, f"network: {type(exc).__name__}: {exc}", {}

    code = int(getattr(resp, "status_code", 0) or 0)
    if code == 200:
        try:
            data = resp.json() if hasattr(resp, "json") else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        say(f"  [plan_check/at] /backend-api/me 200 email={data.get('email') or data.get('email_address') or '?'}")
        return True, "", data
    if code in (401, 403):
        say(f"  [plan_check/at] /backend-api/me {code} → 当前 access token 失效，待复核账号状态")
        return False, f"http_{code}", {}
    body = ""
    try:
        body = (resp.text or "")[:160]
    except Exception:
        pass
    say(f"  [plan_check/at] /backend-api/me unexpected http={code} body={body}")
    return False, f"http_{code}", {}


def _check_via_protocol_login(*, email: str, outlook_creds: dict,
                              log_fn=None,
                              otp_timeout_s: int = PLAN_CHECK_OTP_TIMEOUT_S,
                              login_deadline_s: int = PLAN_CHECK_LOGIN_DEADLINE_S,
                              proxy: str | None = None) -> tuple[str, str]:
    """用 HTTP 协议登录现有 ChatGPT 账号，拿 /api/auth/session access_token。

    复用 gpt_trial_protocol 的 login_existing_account + LocalEmailCodeProvider，
    不启动浏览器自动化。
    """
    say = log_fn or log.info
    try:
        from gpt_trial_protocol.chatgpt import ChatGPTProtocolClient
        from gpt_trial_protocol.flows import ProtocolRegistrarFlow
        from gpt_trial_protocol.http_client import ProtocolHttpClient
        from gpt_trial_protocol.local_email_code import LocalEmailCodeProvider
        from gpt_trial_protocol.models import BrowserProfile, ProtocolConfig

        rt = (outlook_creds.get("refresh_token") or "").strip()
        cid = (outlook_creds.get("client_id") or "").strip()
        if not rt:
            return "", "missing_outlook_refresh_token"

        config = ProtocolConfig(
            timeout=float(login_deadline_s),
            trace_dir=None,
            profile=BrowserProfile(),
        )
        code_provider = LocalEmailCodeProvider(
            refresh_token=rt,
            client_id=cid,
            method="graph",
            relay_require_fresh=True,
        )

        def _event(name: str, payload: dict) -> None:
            if name in {"auth_csrf_pending", "signin_redirect_pending", "waiting_otp",
                        "otp_received", "otp_validated", "session_ready"}:
                say(f"  [plan_check/protocol] {email} {name}")

        with ProtocolHttpClient(
            timeout=float(login_deadline_s),
            proxy=str(proxy or "").strip() or None,
            trace_dir=None,
            trace_name="plan_check",
            backend="curl_cffi",
        ) as http:
            flow = ProtocolRegistrarFlow(ChatGPTProtocolClient(config, http))
            login = flow.login_existing_account(
                email,
                code_provider=code_provider,
                timeout=float(otp_timeout_s),
                on_event=_event,
            )
            token = (login.session.access_token or "").strip()
            if not token:
                return "", "session_token_missing"
            say(f"  [plan_check/protocol] {email} 拿到 access_token len={len(token)}")
            return token, ""
    except TimeoutError:
        say(f"  [plan_check/protocol] {email} 邮箱 OTP {otp_timeout_s}s 超时")
        return "", "otp_timeout"
    except Exception as exc:
        say(f"  [plan_check/protocol] {email} 协议登录失败: {type(exc).__name__}: {str(exc)[:120]}")
        return "", f"{type(exc).__name__}: {exc}"

def _check_via_browser_login(*, email: str, outlook_creds: dict,
                             log_fn=None,
                             otp_timeout_s: int = PLAN_CHECK_OTP_TIMEOUT_S,
                             login_deadline_s: int = PLAN_CHECK_LOGIN_DEADLINE_S) -> tuple[str, str]:
    """起一个浏览器，用 outlook 凭据登录 ChatGPT，fetch /api/auth/session 拿权威 token。

    重用 pipeline.GPTPipeline.login + _try_get_access_token，跟注册流程同款。
    每个账号起一个本地 chromium。

    OTP 超过 otp_timeout_s 没收到 → 视为登录失败。

    Returns:
        (access_token, fail_reason)
        access_token 非空 = 成功；空 = 失败，fail_reason 给原因（"otp_timeout" / "login_failed:..." / "exception"）
    """
    say = log_fn or log.info
    page = None
    try:
        from browser_mgr import open_local_chromium
        page = open_local_chromium(window_index=0)
        from pipeline import GPTPipeline
        pipeline = GPTPipeline(page)
        # password="" 让 login 走邮箱 OTP 路径；OTP 超时 / 总超时都设短
        login_result = pipeline.login(
            email, "",
            outlook_creds=outlook_creds,
            otp_timeout=otp_timeout_s,
            total_deadline_s=login_deadline_s,
        )
        status = login_result.get("status")
        if status != "success":
            err = login_result.get("error") or ""
            # otp_fetch_timeout 是新增的状态码：login() 里 fetch_otp 返回空时抛
            reason = status if status else "login_failed"
            if status == "otp_fetch_timeout":
                say(f"  [plan_check/browser] {email} 邮箱 OTP {otp_timeout_s}s 超时")
            else:
                say(f"  [plan_check/browser] {email} 登录失败: {reason} {err[:80]}")
            return "", reason
        token = pipeline._try_get_access_token() or ""
        if not token:
            say(f"  [plan_check/browser] {email} 登录成功但 fetch session 拿不到 token")
            return "", "session_token_missing"
        say(f"  [plan_check/browser] {email} 登录后拿到 access_token len={len(token)}")
        return token, ""
    except Exception as exc:
        say(f"  [plan_check/browser] {email} 浏览器异常: {exc}")
        return "", f"exception: {type(exc).__name__}"
    finally:
        try:
            if page is not None:
                time.sleep(1)
                page.quit()
        except Exception:
            pass


# ============================================================
#  单账号检查（多档降级）
# ============================================================

# status 码：
#   "plus"           → 确认 Plus（refresh / 协议登录 / AT 探测后 + plan_type ∈ PLUS）
#   "k12"            → 确认 K12
#   "free"           → 确认 Free
#   "plus_expired"   → 之前是 Plus 但 active_until 已过去
#   "rt_revoked"     → 有 RT 但 OpenAI 撤销了（明确失败响应）
#   "unknown_no_creds"→ 既没有 codex RT 也没有 outlook 凭据，也没有 AT
#   "protocol_login_failed" → 协议登录失败（OTP 拿不到 / 风控等）
#   "browser_login_failed" → 旧浏览器登录失败（历史结果兼容）
#   "account_deactivated" → 协议登录明确返回账号已删除/停用
#   "token_dead"     → AT 实时探测 401/403；仅代表当前凭据失效，待复核
#   "no_token"       → 啥也没有
#   "error"          → 其它异常


def _check_one(*, email: str,
               access_token: str,
               refresh_token: str = "",
               outlook_creds: dict | None = None,
               local_plan_type: str = "",
               refresh_first: bool = True,
               use_browser_fallback: bool = True,
               proxy: str | None = None,
               log_fn=None) -> dict:
    """对一个账号做 plan 检查，返回标准化结果。"""
    say = log_fn or (lambda _m: None)
    result = {
        "email": email,
        "is_plus": False,
        "is_k12": False,
        "status": "no_token",
        "plan_type": "",
        "tier": 0,                          # 1=codex RT, 2=protocol, 3=AT probe, 0=none
        "active_until": "",
        "active_start": "",
        "subscription_last_checked": "",
        "last_checked": _now_iso(),
        "expires_at": "",
        "expired": False,
        "refreshed": False,
        "protocol_login_used": False,
        "browser_login_used": False,
        "at_probe_used": False,
        "refresh_failed": False,
        "refresh_error": "",
        "error": "",
        "network_route": "managed_proxy" if str(proxy or "").strip() else "direct",
    }

    if not access_token and not refresh_token and not (outlook_creds and outlook_creds.get("refresh_token")):
        result["error"] = "no_token_no_creds"
        result["status"] = "unknown_no_creds"
        return result

    cur_at = access_token
    id_token = ""
    is_local_k12 = (local_plan_type or "").strip().lower() == "k12"
    live_verified = False  # 是否已通过实时接口确认存活

    # ---- Tier 1: codex refresh_token ----
    if refresh_first and refresh_token and not is_local_k12:
        try:
            from codex_oauth import refresh_access_token
            new_tokens = refresh_access_token(refresh_token, proxy_url=proxy)
            new_at = (new_tokens.get("access_token") or "").strip()
            new_id = (new_tokens.get("id_token") or "").strip()
            if new_at:
                cur_at = new_at
                id_token = new_id
                result["refreshed"] = True
                result["tier"] = 1
                live_verified = True
                # 持久化
                try:
                    from phone_binding import binding_store
                    new_rt = (new_tokens.get("refresh_token") or "").strip() or refresh_token
                    binding_store.record_binding(
                        email=email,
                        access_token=cur_at,
                        refresh_token=new_rt,
                        id_token=id_token,
                    )
                except Exception:
                    pass
            else:
                result["refresh_failed"] = True
                result["refresh_error"] = "refresh_no_access_token_in_response"
        except Exception as exc:
            result["refresh_failed"] = True
            result["refresh_error"] = f"{type(exc).__name__}: {exc}"
            log.debug(f"  [plan_check] {email} codex refresh 失败: {exc}")

    # ---- Tier 2: 协议登录 ----
    if not live_verified and use_browser_fallback and not is_local_k12:
        has_outlook = outlook_creds and outlook_creds.get("refresh_token")
        if has_outlook:
            say(f"[plan_check] Tier 2 (protocol login): {email}")
            tok, fail_reason = _check_via_protocol_login(
                email=email,
                outlook_creds=outlook_creds,
                log_fn=say,
                proxy=proxy,
            )
            if tok:
                cur_at = tok
                id_token = ""
                result["protocol_login_used"] = True
                result["tier"] = 2
                live_verified = True
                # 持久化
                try:
                    from phone_binding import binding_store
                    binding_store.record_binding(
                        email=email,
                        access_token=cur_at,
                        client_id=(outlook_creds or {}).get("client_id", ""),
                        outlook_refresh_token=(outlook_creds or {}).get("refresh_token", ""),
                    )
                except Exception:
                    pass
            else:
                # 协议登录明确说账号已删/停用 → 直接定论，别再拿旧 AT 探测误导
                fr = str(fail_reason or "")
                if "account_deactivated" in fr or "deleted or deactivated" in fr.lower():
                    result["status"] = "account_deactivated"
                    # A protocol-login response is the only path that marks
                    # an account as deactivated.  Keep its provenance so a
                    # later AT-only poll cannot be mistaken for this verdict.
                    result["deactivation_source"] = "protocol_login"
                    result["error"] = f"account_deactivated: protocol_login: {fr[:300]}"
                    return result
                # 其它协议失败：若还有 AT 可探测则继续；否则直接失败
                if not (cur_at or "").strip():
                    result["status"] = "protocol_login_failed"
                    result["error"] = f"protocol_login_failed: {fail_reason}"
                    return result
                result["error"] = f"protocol_login_failed: {fail_reason}"
                say(f"[plan_check] Tier 2 失败 ({fail_reason})，降级 AT 探测: {email}")

    # ---- Tier 3: access_token 实时存活探测 ----
    if not live_verified and (cur_at or "").strip() and not is_local_k12:
        say(f"[plan_check] Tier 3 (AT /backend-api/me): {email}")
        alive, reason, _me = _probe_access_token_alive(cur_at, log_fn=say, proxy=proxy)
        result["at_probe_used"] = True
        if alive:
            result["tier"] = 3
            live_verified = True
            # 把探测到的 AT 写回 binding，方便导出/下次检查
            try:
                from phone_binding import binding_store
                binding_store.record_binding(
                    email=email,
                    access_token=cur_at,
                    client_id=(outlook_creds or {}).get("client_id", ""),
                    outlook_refresh_token=(outlook_creds or {}).get("refresh_token", ""),
                )
            except Exception:
                pass
        else:
            # access_token is short lived.  A 401/403 proves this credential
            # is unusable, not that the underlying account is deactivated.
            # The automatic RT/AT-only poll must therefore leave this as an
            # inconclusive token failure for a later status-poll retry.
            if reason.startswith("http_401") or reason.startswith("http_403"):
                result["status"] = "token_dead"
                result["error"] = f"token_dead: at_probe_failed: {reason}"
                return result
            # 网络类错误：若之前协议也失败，标 error；否则保留旧 error 并返回
            if result.get("error"):
                result["status"] = "protocol_login_failed"
                result["error"] = f"{result['error']}; at_probe_failed: {reason}"
                return result
            result["status"] = "error"
            result["error"] = f"at_probe_failed: {reason}"
            return result

    # ---- 解 JWT 判定 ----
    # 仅在 live_verified 后才允许用 JWT 判定 free/plus（避免旧快照冒充「正常登录确认」）
    if not live_verified:
        if not result["error"]:
            result["status"] = "rt_revoked" if (refresh_token and result["refresh_failed"]) else "no_token"
            result["error"] = result["refresh_error"] or "no_live_verification"
        return result

    if not cur_at:
        if not result["error"]:
            result["status"] = "rt_revoked" if (refresh_token and result["refresh_failed"]) else "no_token"
            result["error"] = result["refresh_error"] or "no_access_token"
        return result

    # access_token 自身过期时间
    exp_unix = _jwt_exp(cur_at)
    if exp_unix:
        result["expires_at"] = datetime.fromtimestamp(exp_unix, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        result["expired"] = exp_unix < int(time.time())

    # 优先解 ID_TOKEN（含订阅时间），否则 access_token
    auth = _jwt_auth_claims(id_token) if id_token else {}
    if not auth:
        auth = _jwt_auth_claims(cur_at)

    plan_type = str(auth.get("chatgpt_plan_type") or "").lower().strip()
    if is_local_k12:
        plan_type = "k12"
    result["plan_type"] = plan_type
    result["active_until"] = str(auth.get("chatgpt_subscription_active_until") or "")
    result["active_start"] = str(auth.get("chatgpt_subscription_active_start") or "")
    result["subscription_last_checked"] = str(auth.get("chatgpt_subscription_last_checked") or "")

    is_k12 = plan_type in _K12_PLAN_TYPES
    if is_k12:
        result["status"] = "k12"
        result["is_k12"] = True
        result["is_plus"] = False
        return result

    is_plus = plan_type in _PLUS_PLAN_TYPES

    # 订阅是否已结束
    if is_plus and result["active_until"]:
        until_unix = _parse_iso_to_unix(result["active_until"])
        if until_unix and until_unix < int(time.time()):
            is_plus = False
            result["status"] = "plus_expired"
        else:
            result["status"] = "plus"
    elif is_plus:
        result["status"] = "plus"
    else:
        result["status"] = "free"

    result["is_plus"] = is_plus
    return result


# ============================================================
#  批量检查
# ============================================================


def _gather_candidates(emails: list[str] | None = None) -> list[dict]:
    """合并账号池 + binding_store + at_export_5parts_dashes + outlook_accounts_success.txt 生成
    [{"email", "access_token", "refresh_token", "outlook_creds"}] 列表。

    outlook_creds shape: {"email", "refresh_token", "client_id", "password"}
    用于 Tier 2 协议登录。
    """
    out: dict[str, dict] = {}

    def merge_candidate(email: str, incoming: dict, *, prefer_tokens: bool = False) -> None:
        key = (email or incoming.get("email") or "").strip().lower()
        if not key:
            return
        current = out.setdefault(key, {
            "email": incoming.get("email") or email or key,
            "access_token": "",
            "refresh_token": "",
            "outlook_creds": {},
        })
        if not current.get("email") and incoming.get("email"):
            current["email"] = incoming["email"]
        for field in ("access_token", "refresh_token"):
            value = (incoming.get(field) or "").strip()
            if value and (prefer_tokens or not current.get(field)):
                current[field] = value
        local_plan_type = (incoming.get("local_plan_type") or "").strip().lower()
        if local_plan_type and (prefer_tokens or not current.get("local_plan_type")):
            current["local_plan_type"] = local_plan_type
        incoming_creds = incoming.get("outlook_creds") or {}
        current_creds = current.setdefault("outlook_creds", {})
        for field in ("email", "refresh_token", "client_id", "password"):
            value = (incoming_creds.get(field) or "").strip()
            if value and not current_creds.get(field):
                current_creds[field] = value

    # 0) outlook_accounts.txt / icloud_accounts.txt —— 账号管理页会列出这里的账号，
    # 检查时也必须纳入候选，否则会出现"检查完成 0 个"的秒返回。
    try:
        import accounts_pool
        for a in accounts_pool.load_pool():
            if not a.email:
                continue
            merge_candidate(a.email, {
                "email": a.email,
                "access_token": "",
                "refresh_token": "",
                "outlook_creds": a.to_outlook_creds(),
            })
    except Exception as exc:
        log.debug(f"  [plan_check] 读 accounts_pool 失败: {exc}")

    # 1) binding_store —— token 通常最新，但不要覆盖账号池里的 OTP/relay 凭据。
    try:
        from phone_binding import binding_store
        for email_lower, entry in binding_store.load_all().items():
            if not isinstance(entry, dict):
                continue
            email = entry.get("email") or email_lower
            merge_candidate(email, {
                "email": email,
                "access_token": entry.get("access_token") or "",
                "refresh_token": entry.get("refresh_token") or "",
                "local_plan_type": entry.get("plan_type") or "",
                "outlook_creds": {
                    "email": email,
                    "refresh_token": entry.get("outlook_refresh_token") or "",
                    "client_id": entry.get("client_id") or "",
                    "password": "",
                },
            }, prefer_tokens=True)
    except Exception as exc:
        log.debug(f"  [plan_check] 读 binding_store 失败: {exc}")

    # 2) account_exports —— Free / Plus / K12 成功账号（success.txt + 导出）
    #    Export files are registration snapshots. They may fill missing fields,
    #    but must not replace a newer token persisted by binding_store.
    try:
        import account_exports
        for rec in account_exports.load_records():
            if not rec.email:
                continue
            plan = (rec.plan_type or "").strip().lower()
            merge_candidate(rec.email, {
                "email": rec.email,
                "access_token": rec.access_token or "",
                "refresh_token": rec.refresh_token or "",
                "local_plan_type": plan,
                "outlook_creds": {
                    "email": rec.email,
                    "refresh_token": rec.outlook_refresh_token or "",
                    "client_id": rec.outlook_client_id or "",
                    "password": rec.password or "",
                },
            })
    except Exception as exc:
        log.debug(f"  [plan_check] 读 account_exports 记录失败: {exc}")

    # 3) outlook_accounts_success.txt —— Plus 成功账号 4 段（email/password/client_id/refresh_token）
    success_file = OUTPUT_DIR / "outlook_accounts_success.txt"
    if success_file.exists():
        for line in success_file.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split("----")
            if len(parts) < 4:
                continue
            em = parts[0].strip()
            pwd = parts[1].strip() if len(parts) > 1 else ""
            cid = parts[2].strip() if len(parts) > 2 else ""
            rt = parts[3].strip() if len(parts) > 3 else ""
            if not em or not rt:
                continue
            merge_candidate(em, {
                "email": em,
                "access_token": "",
                "refresh_token": "",
                "outlook_creds": {"email": em, "refresh_token": rt,
                                   "client_id": cid, "password": pwd},
            })

    # 4) at_export_5parts_dashes.txt —— 老的注册产物，没 RT 但有 access_token
    p5 = OUTPUT_DIR / "at_export_5parts_dashes.txt"
    if p5.exists():
        for line in p5.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split("----")
            if len(parts) < 5:
                continue
            em = parts[0].strip()
            cid = parts[2].strip() if len(parts) > 2 else ""
            outlook_rt = parts[3].strip() if len(parts) > 3 else ""
            at = parts[4].strip()
            if not em or not at:
                continue
            merge_candidate(em, {
                "email": em,
                "access_token": at,
                "refresh_token": "",
                "outlook_creds": {"email": em, "refresh_token": outlook_rt,
                                   "client_id": cid, "password": ""},
            })

    # 5) 过滤
    if emails:
        wanted = {e.strip().lower() for e in emails if e.strip()}
        out = {k: v for k, v in out.items() if k in wanted}
        present = set(out)
        for em in emails:
            key = em.strip().lower()
            if key and key not in present:
                out[key] = {
                    "email": em.strip(),
                    "access_token": "",
                    "refresh_token": "",
                    "outlook_creds": {},
                }

    return list(out.values())


def bulk_check(
    *,
    emails: list[str] | None = None,
    only_with_token: bool = True,
    refresh_first: bool = True,
    use_browser_fallback: bool = True,
    concurrency: int = 4,
    proxy: str | None = None,
    log_fn=None,
) -> dict:
    """批量检查 Plus 状态。

    Args:
        emails: 限定到这些邮箱；不传时检查所有候选
        only_with_token: True 跳过既无 access_token 也无 outlook_creds 的账号
        refresh_first: Tier 1 用 codex RT 刷
        use_browser_fallback: 兼容旧参数名；Tier 2 没 codex RT 时走协议登录抓权威 token
        concurrency: 浏览器 fallback 时建议 ≤2，纯 refresh 模式可以 8+
        proxy: 可选 HTTP/SOCKS 代理；用于 RT 刷新、协议登录和 AT 探测
        log_fn: 日志回调

    Returns:
        {
          total, plus, k12, free, plus_expired, errors,
          tier1, tier2,                # 分别用了哪个 tier 拿到结果
          results: [...]
        }
    """
    say = log_fn or (lambda m: log.info(m))
    candidates = _gather_candidates(emails=emails)
    if not candidates:
        return {"total": 0, "plus": 0, "k12": 0, "free": 0, "plus_expired": 0,
                "errors": 0, "tier1": 0, "tier2": 0, "tier3": 0, "results": []}

    if only_with_token and not emails:
        candidates = [
            c for c in candidates
            if c.get("access_token") or c.get("refresh_token")
            or (
                use_browser_fallback
                and (c.get("outlook_creds") or {}).get("refresh_token")
            )
        ]

    say(f"[plan-check] 待检查 {len(candidates)} 个账号 "
        f"(refresh_first={refresh_first} protocol_fallback={use_browser_fallback} "
        f"concurrency={concurrency} route={'managed_proxy' if proxy else 'direct'})")

    results: list[dict | None] = [None] * len(candidates)

    def run_one(idx: int, c: dict) -> dict:
        try:
            r = _check_one(
                email=c["email"],
                access_token=c.get("access_token") or "",
                refresh_token=c.get("refresh_token") or "",
                outlook_creds=c.get("outlook_creds"),
                local_plan_type=c.get("local_plan_type") or "",
                refresh_first=refresh_first,
                use_browser_fallback=use_browser_fallback,
                proxy=proxy,
                log_fn=say,
            )
        except Exception as exc:
            r = {
                "email": c["email"], "is_plus": False, "status": "error",
                "plan_type": "", "tier": 0,
                "error": f"{type(exc).__name__}: {exc}",
                "last_checked": _now_iso(),
            }
        FLAGS = {
            "plus": "✓ Plus",
            "k12": "✓ K12",
            "free": "· Free",
            "plus_expired": "⏰ Plus 过期",
            "rt_revoked": "✗ RT 撤销",
            "no_token": "✗ 无 token",
            "token_dead": "✗ AT 失效",
            "protocol_login_failed": "✗ 协议登录失败",
            "browser_login_failed": "✗ 浏览器登录失败",
            "unknown_no_creds": "? 无凭据",
            "error": "✗ 错误",
        }
        flag = FLAGS.get(r.get("status"), r.get("status") or "?")
        suffix = ""
        tier = r.get("tier", 0)
        if tier == 1:
            suffix = "  [tier1: codex-RT]"
        elif tier == 2:
            suffix = "  [tier2: protocol-login]"
        elif tier == 3:
            suffix = "  [tier3: at-probe]"
        elif r.get("refresh_failed"):
            suffix = f"  [refresh_failed: {(r.get('refresh_error') or '')[:60]}]"
        say(f"  [{idx + 1}/{len(candidates)}] {flag:18s} {r['email']}  plan={r.get('plan_type') or '-'}{suffix}")
        return r

    if concurrency <= 1:
        for i, c in enumerate(candidates):
            results[i] = run_one(i, c)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            future_map = {pool.submit(run_one, i, c): i for i, c in enumerate(candidates)}
            for f in as_completed(future_map):
                i = future_map[f]
                results[i] = f.result()

    final = [r for r in results if r is not None]

    def _count(status_set):
        return sum(1 for r in final if r.get("status") in status_set)

    return {
        "total": len(final),
        "plus": _count({"plus"}),
        "k12": _count({"k12"}),
        "free": _count({"free"}),
        "plus_expired": _count({"plus_expired"}),
        "errors": _count({"error", "rt_revoked", "no_token", "token_dead",
                          "account_deactivated",
                          "protocol_login_failed",
                          "browser_login_failed", "unknown_no_creds"}),
        "tier1": sum(1 for r in final if r.get("tier") == 1),
        "tier2": sum(1 for r in final if r.get("tier") == 2),
        "tier3": sum(1 for r in final if r.get("tier") == 3),
        "results": final,
    }


# ============================================================
#  落地
# ============================================================


_CONFIRMED_PLAN_STATUSES = {"plus", "free", "k12", "plus_expired"}
_INCONCLUSIVE_STATUSES = {
    "error",
    "rt_revoked",
    "no_token",
    "token_dead",
    "protocol_login_failed",
    "browser_login_failed",
    "unknown_no_creds",
}


def _http_auth_failure_reason(result: dict | None) -> str:
    """Return an HTTP auth failure code without inferring account state."""
    if not isinstance(result, dict):
        return ""
    text = " ".join(str(result.get(field) or "").lower() for field in (
        "ban_reason",
        "error",
        "probe_error",
        "refresh_error",
    ))
    if "http_401" in text:
        return "http_401"
    if "http_403" in text:
        return "http_403"
    return ""


def _at_probe_auth_failure_reason(result: dict | None) -> str:
    """Identify a 401/403 emitted by the access-token probe.

    An access token expires independently of the account.  The old workbench
    persisted those token failures as ``account_deactivated``; detect that
    legacy shape here so it can be migrated without weakening an explicit
    protocol-login deactivation verdict.
    """
    if not isinstance(result, dict):
        return ""
    source = str(result.get("deactivation_source") or "").strip().lower()
    if source in {"protocol", "protocol_login", "protocol-login"}:
        return ""

    status = str(result.get("status") or "").strip().lower()
    primary_error = str(result.get("error") or "").lower()
    # Older protocol rows do not have ``deactivation_source``. Their primary
    # error remains authoritative even after a later AT-only probe updates
    # ``probe_error``.
    if (
        status == "account_deactivated"
        and "at_probe_failed" not in primary_error
        and ("protocol_login" in primary_error or "deleted or deactivated" in primary_error)
    ):
        return ""

    reason = _http_auth_failure_reason(result)
    if not reason:
        return ""
    text = " ".join(str(result.get(field) or "").lower() for field in (
        "error",
        "probe_error",
        "ban_reason",
    ))
    probe_status = str(result.get("probe_status") or "").strip().lower()
    if (
        source in {"access_token", "at", "at_probe", "token_probe"}
        or bool(result.get("at_probe_used"))
        or "at_probe_failed" in text
        or status == "token_dead"
        or probe_status == "token_dead"
    ):
        return reason
    return ""


def _is_account_deactivated(result: dict) -> bool:
    """Return true only for a clear deactivation result, never an AT 401/403."""
    if _at_probe_auth_failure_reason(result):
        return False
    status = str(result.get("status") or "").strip().lower()
    error = str(result.get("error") or "").lower()
    return status == "account_deactivated" or (
        status in {"protocol_login_failed", "browser_login_failed"}
        and (
            "account_deactivated" in error
            or "deleted or deactivated" in error
        )
    )


def _confirmed_plan_from(result: dict, previous: dict | None = None) -> tuple[str, str]:
    """Find the latest confirmed plan retained by a persisted result pair."""
    for candidate in (result, previous or {}):
        plan = str(candidate.get("last_confirmed_plan_type") or "").strip().lower()
        status = str(candidate.get("status") or "").strip().lower()
        if not plan and status in _CONFIRMED_PLAN_STATUSES:
            plan = str(candidate.get("plan_type") or status).strip().lower()
        if plan in _CONFIRMED_PLAN_STATUSES:
            confirmed_at = str(
                candidate.get("confirmed_at") or candidate.get("last_checked") or ""
            )
            return plan, confirmed_at
    return "", ""


def _normalize_at_probe_token_failure(result: dict, previous: dict | None = None) -> dict:
    """Downgrade a current or legacy AT 401/403 to an inconclusive result."""
    saved = dict(result)
    raw_status = str(saved.get("status") or "").strip().lower()
    raw_probe_status = str(saved.get("probe_status") or raw_status).strip().lower()
    confirmed_plan, confirmed_at = _confirmed_plan_from(saved, previous)
    original_error = str(saved.get("probe_error") or saved.get("error") or "")
    token_error = original_error.replace("account_deactivated:", "token_dead:")

    # A historical confirmed plan remains useful context.  Otherwise expose
    # the credential failure directly as ``token_dead``/待复核.
    if raw_status == "account_deactivated" or raw_status not in _CONFIRMED_PLAN_STATUSES:
        if confirmed_plan:
            saved["status"] = confirmed_plan
            saved["plan_type"] = confirmed_plan
            if confirmed_at:
                saved["confirmed_at"] = confirmed_at
                saved["last_confirmed_plan_type"] = confirmed_plan
        else:
            saved["status"] = "token_dead"
            saved["plan_type"] = ""
            saved["is_plus"] = False
            saved["is_k12"] = False

    if raw_probe_status == "account_deactivated" or raw_status == "account_deactivated":
        saved["raw_probe_status"] = raw_probe_status
    saved["probe_status"] = "token_dead"
    saved["probe_error"] = token_error
    saved["error"] = token_error
    for field in ("ban_reason", "banned_at", "last_banned_at", "deactivation_source"):
        saved.pop(field, None)
    return saved


def _with_probe_metadata(result: dict) -> dict:
    """Return a persisted result with the latest token/login probe attached."""
    saved = dict(result)
    saved["probe_status"] = str(
        result.get("probe_status") or result.get("status") or ""
    )
    saved["probe_error"] = str(
        result.get("probe_error")
        or result.get("error")
        or result.get("refresh_error")
        or ""
    )
    saved["probe_checked_at"] = str(
        result.get("probe_checked_at") or result.get("last_checked") or _now_iso()
    )
    saved["probe_tier"] = int(
        result.get("probe_tier")
        if result.get("probe_tier") is not None
        else (result.get("tier") or 0)
    )
    if saved.get("status") in _CONFIRMED_PLAN_STATUSES:
        saved["confirmed_at"] = str(
            result.get("confirmed_at") or result.get("last_checked") or _now_iso()
        )
    return saved


def _normalize_banned_result(result: dict, previous: dict | None = None) -> dict:
    """Persist only explicit deactivation verdicts as banned.

    Historical snapshots may contain an ``account_deactivated`` status created
    solely from an AT probe 401/403.  Convert those entries back to a token
    failure before any deactivation merge or banned-report logic sees them.
    """
    saved = dict(result)
    previous = previous if isinstance(previous, dict) else None
    if _at_probe_auth_failure_reason(saved):
        return _normalize_at_probe_token_failure(saved, previous)

    explicit_deactivation = str(saved.get("status") or "").strip().lower() == "account_deactivated"
    if not _is_account_deactivated(saved):
        return saved

    raw_status = str(saved.get("status") or "").strip().lower()
    raw_probe_status = str(saved.get("probe_status") or raw_status).strip().lower()
    confirmed_plan = str(saved.get("last_confirmed_plan_type") or "").strip().lower()
    if not confirmed_plan and raw_status in _CONFIRMED_PLAN_STATUSES:
        confirmed_plan = str(saved.get("plan_type") or raw_status).strip().lower()
    if not confirmed_plan and previous:
        confirmed_plan = str(previous.get("last_confirmed_plan_type") or "").strip().lower()
        previous_status = str(previous.get("status") or "").strip().lower()
        if not confirmed_plan and previous_status in _CONFIRMED_PLAN_STATUSES:
            confirmed_plan = str(previous.get("plan_type") or previous_status).strip().lower()

    probe_checked_at = str(
        saved.get("probe_checked_at") or saved.get("last_checked") or _now_iso()
    )
    detected_at = str(saved.get("banned_at") or "")
    if not detected_at:
        detected_at = str(saved.get("last_checked") or probe_checked_at) if explicit_deactivation else probe_checked_at
    if previous and _is_account_deactivated(previous):
        detected_at = str(previous.get("banned_at") or previous.get("last_checked") or detected_at)

    last_banned_at = max(
        str(saved.get("last_banned_at") or ""),
        str((previous or {}).get("last_banned_at") or ""),
        probe_checked_at,
    )
    original_error = str(saved.get("probe_error") or saved.get("error") or "")
    if raw_probe_status and raw_probe_status != "account_deactivated":
        saved["raw_probe_status"] = raw_probe_status
    saved.update({
        "status": "account_deactivated",
        "plan_type": "",
        "is_plus": False,
        "is_k12": False,
        "ban_reason": str(
            saved.get("ban_reason")
            or saved.get("deactivation_source")
            or "account_deactivated"
        ),
        "banned_at": detected_at,
        "last_banned_at": last_banned_at,
        "probe_status": "account_deactivated",
        "probe_error": original_error,
        "probe_checked_at": probe_checked_at,
    })
    if confirmed_plan:
        saved["last_confirmed_plan_type"] = confirmed_plan
        confirmed_at = str(
            saved.get("confirmed_at")
            or (previous or {}).get("confirmed_at")
            or (previous or {}).get("last_checked")
            or ""
        )
        if confirmed_at:
            saved["confirmed_at"] = confirmed_at
    return saved


def _merge_persisted_result(previous: dict | None, current: dict) -> dict:
    """Merge a probe without letting an inconclusive result erase a known plan.

    A successful refresh/login/AT probe and an explicit protocol deactivation
    response are authoritative.  AT 401/403 is only a credential failure, so
    it updates probe metadata without erasing a known plan.
    """
    previous = _normalize_banned_result(previous) if previous else None
    current_saved = _normalize_banned_result(_with_probe_metadata(current), previous)
    if not previous:
        return current_saved

    previous_status = str(previous.get("status") or "").strip().lower()
    current_status = str(current.get("status") or "").strip().lower()
    previous_confirmed = previous_status in _CONFIRMED_PLAN_STATUSES
    previous_deactivated = _is_account_deactivated(previous)

    if (
        (previous_confirmed or previous_deactivated)
        and current_status in _INCONCLUSIVE_STATUSES
        and not _is_account_deactivated(current)
    ):
        saved = dict(previous)
        if previous_confirmed:
            saved["confirmed_at"] = str(
                previous.get("confirmed_at")
                or previous.get("last_checked")
                or ""
            )
        for field in (
            "probe_status",
            "probe_error",
            "probe_checked_at",
            "probe_tier",
        ):
            saved[field] = current_saved[field]
        return saved

    return current_saved


def _format_banned_at_beijing(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return text


def _write_banned_report(results: list[dict]) -> Path:
    banned = [row for row in results if _is_account_deactivated(row)]
    banned.sort(
        key=lambda row: str(row.get("banned_at") or row.get("last_checked") or ""),
        reverse=True,
    )
    lines = ["封号时间(北京时间)\t账号\t历史套餐\t封号原因"]
    for row in banned:
        lines.append("\t".join((
            _format_banned_at_beijing(row.get("banned_at") or row.get("last_checked") or ""),
            str(row.get("email") or ""),
            str(row.get("last_confirmed_plan_type") or "-"),
            str(row.get("ban_reason") or "account_deactivated"),
        )))
    BANNED_REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return BANNED_REPORT_FILE


def write_results(result: dict) -> Path:
    """把这一批结果**合并**到 output/plan_check_results.json。

    按 email 做 upsert：本次检查的账号覆盖旧结果，没检查到的账号原样保留。
    避免「同步检查 1 个账号」把其它 95 个的历史结果擦掉。
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1) 读旧结果
    old_results: list[dict] = []
    if PLAN_CHECK_FILE.exists():
        try:
            old = json.loads(PLAN_CHECK_FILE.read_text(encoding="utf-8"))
            old_results = old.get("results") or []
        except Exception:
            old_results = []

    by_email: dict[str, dict] = {}
    for r in old_results:
        if isinstance(r, dict) and r.get("email"):
            normalized = _normalize_banned_result(r)
            by_email[r["email"].lower()] = normalized

    # 2) 用本次结果 upsert。旧 AT 的失败只更新 probe_*，不覆盖已确认套餐。
    new_results = result.get("results") or []
    for r in new_results:
        if isinstance(r, dict) and r.get("email"):
            key = r["email"].lower()
            by_email[key] = _merge_persisted_result(by_email.get(key), r)

    merged = list(by_email.values())
    merged.sort(key=lambda r: (r.get("email") or "").lower())

    # 3) 重新统计（基于合并后的全量）
    def _count(status_set, src=merged):
        return sum(1 for r in src if r.get("status") in status_set)

    snapshot = {
        "checked_at": _now_iso(),
        # 基于全量重算的统计（这就是前端 strip 看到的数）
        "total": len(merged),
        "plus": _count({"plus"}),
        "k12": _count({"k12"}),
        "free": _count({"free"}),
        "plus_expired": _count({"plus_expired"}),
        "errors": _count({"error", "rt_revoked", "no_token", "token_dead",
                          "account_deactivated",
                          "protocol_login_failed",
                          "browser_login_failed", "unknown_no_creds"}),
        "tier1": sum(1 for r in merged if r.get("tier") == 1),
        "tier2": sum(1 for r in merged if r.get("tier") == 2),
        "tier3": sum(1 for r in merged if r.get("tier") == 3),
        # 这一批的子集统计（log 里展示）
        "this_batch": {
            "total": result.get("total", 0),
            "plus": result.get("plus", 0),
            "k12": result.get("k12", 0),
            "free": result.get("free", 0),
            "plus_expired": result.get("plus_expired", 0),
            "errors": result.get("errors", 0),
            "tier1": result.get("tier1", 0),
            "tier2": result.get("tier2", 0),
            "emails": [r.get("email") for r in new_results],
        },
        "results": merged,
    }
    PLAN_CHECK_FILE.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_banned_report(merged)

    # 4) 文本日志（追加）— 只记本次跑的 batch
    txt_log = OUTPUT_DIR / "plan_check_log.txt"
    batch = snapshot["this_batch"]
    lines = ["=" * 64,
             f"  Plan check [{snapshot['checked_at']}] (本次 batch)",
             "=" * 64,
             f"本次: 总 {batch['total']}    Plus {batch['plus']}    K12 {batch['k12']}    Free {batch['free']}    "
             f"过期 {batch['plus_expired']}    错误 {batch['errors']}    "
             f"(tier1={batch['tier1']} tier2={batch['tier2']})",
             f"全量: 总 {snapshot['total']}    Plus {snapshot['plus']}    K12 {snapshot['k12']}    Free {snapshot['free']}    "
             f"过期 {snapshot['plus_expired']}    错误 {snapshot['errors']}",
             ""]
    for r in new_results:
        FLAGS = {"plus": "✓", "k12": "K", "free": "·", "plus_expired": "⏰",
                 "rt_revoked": "✗", "no_token": "✗",
                 "account_deactivated": "✗", "protocol_login_failed": "✗",
                 "browser_login_failed": "✗", "unknown_no_creds": "?",
                 "error": "✗"}
        flag = FLAGS.get(r.get("status"), "?")
        tier = r.get("tier", 0)
        tier_tag = f"[T{tier}]" if tier else "    "
        line = f"  {flag} {tier_tag} {r.get('email','?'):40s}  status={r.get('status','?')}  plan={r.get('plan_type','-') or '-'}"
        if r.get("active_until"):
            line += f"  until={r['active_until'][:10]}"
        if r.get("error"):
            line += f"   err={r['error'][:120]}"
        lines.append(line)
    lines.append("")
    with open(txt_log, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return PLAN_CHECK_FILE


def load_last_results() -> dict:
    if not PLAN_CHECK_FILE.exists():
        return {}
    try:
        return json.loads(PLAN_CHECK_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
