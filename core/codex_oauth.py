"""
Codex CLI OAuth — 给已登录的 ChatGPT 账号跑一遍 PKCE OAuth code → token 交换，
拿到真正的 OpenAI access_token / refresh_token / id_token。

参考实现：_aBaiAutoplus-ref/platforms/chatgpt/oauth.py + browser_register._do_codex_oauth
做了精简：复用本项目的 GPTPipeline + DrissionPage，不引入 Playwright / curl_cffi。

流程：
  1. 拼 PKCE auth_url（OPENAI_AUTH/oauth/authorize，client=Codex CLI）
  2. 浏览器导航过去（要求已登录态；未登录则先 login）
  3. 等 OpenAI 重定向到 redirect_uri （http://localhost:1455/auth/callback?code=...）
       浏览器到达本机 localhost 端口时会断（页面打不开）；
       我们直接读 page.url 抠 ?code & ?state
  4. POST OPENAI_AUTH/oauth/token 用 grant_type=authorization_code 换 token

使用：
    from pipeline import GPTPipeline
    from codex_oauth import run_codex_oauth_on_page
    page = ... (登录态 ChatGPT)
    tokens = run_codex_oauth_on_page(GPTPipeline(page), email)
    # tokens = {access_token, refresh_token, id_token, account_id, expires_in}

公开 API:
    generate_oauth_url() -> (auth_url, state, code_verifier)
    exchange_code(code, code_verifier) -> dict
    run_codex_oauth_on_page(pipeline, email) -> dict | None

集成进 phone_binding.default_phone_binder：绑定手机号成功后立刻跑一次 OAuth，
把 refresh_token 写入 binding_store。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import ssl
import time
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("codex_oauth")

# OpenAI Auth Hydra endpoint（跟 ref constants.py 对齐）
OPENAI_AUTH = "https://auth.openai.com"
TOKEN_URL = f"{OPENAI_AUTH}/oauth/token"

# Codex CLI client（公开 PKCE client）
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
CODEX_SCOPE = "openid email profile offline_access"

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sha256_b64url_no_pad(s: str) -> str:
    return _b64url_no_pad(hashlib.sha256(s.encode("ascii")).digest())


def _decode_jwt(token: str) -> dict:
    if not token or token.count(".") < 2:
        return {}
    seg = token.split(".")[1]
    pad = "=" * (-len(seg) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(seg + pad).decode("utf-8"))
    except Exception:
        return {}


def generate_oauth_url(*,
                       client_id: str = CODEX_CLIENT_ID,
                       redirect_uri: str = CODEX_REDIRECT_URI,
                       scope: str = CODEX_SCOPE,
                       login_hint: str = "") -> dict:
    """生成 PKCE 授权链接 + state + code_verifier。

    login_hint: 传 email 时 OpenAI 会直接定位到该账号，**绕过 choose-an-account 页**。
    """
    state = secrets.token_urlsafe(16)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _sha256_b64url_no_pad(code_verifier)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    # 关键：login_hint 让已登录态直接命中目标账号，跳过 choose-an-account。
    # 不再带 prompt=login（那会强制重新登录 / 弹账号选择器）。
    if login_hint:
        params["login_hint"] = login_hint
    auth_url = f"{OPENAI_AUTH}/oauth/authorize?{urllib.parse.urlencode(params)}"
    return {
        "auth_url": auth_url,
        "state": state,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
    }


def _parse_callback(url: str) -> dict:
    """从 redirect URL 抠 code/state；支持 fragment / query。"""
    if not url:
        return {"code": "", "state": "", "error": ""}
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    fragment = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)
    for k, v in fragment.items():
        if k not in query:
            query[k] = v
    return {
        "code": (query.get("code", [""])[0] or "").strip(),
        "state": (query.get("state", [""])[0] or "").strip(),
        "error": (query.get("error", [""])[0] or "").strip(),
        "error_description": (query.get("error_description", [""])[0] or "").strip(),
    }


def exchange_code(*,
                  code: str,
                  code_verifier: str,
                  client_id: str = CODEX_CLIENT_ID,
                  redirect_uri: str = CODEX_REDIRECT_URI,
                  proxy_url: str | None = None,
                  timeout: int = 30) -> dict:
    """POST /oauth/token，拿 access_token + refresh_token + id_token。"""
    return _post_token({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }, proxy_url=proxy_url, timeout=timeout)


def refresh_access_token(refresh_token: str,
                         *,
                         client_id: str = CODEX_CLIENT_ID,
                         scope: str = CODEX_SCOPE,
                         proxy_url: str | None = None,
                         timeout: int = 30) -> dict:
    """用 refresh_token 换一份新的 access_token + id_token。

    OpenAI 的 access_token 实际生命周期非常短（即使 JWT exp 写得比较长，
    服务端也会吊销），下游 sub2api / cockpit / any2api 直接拿 access_token
    调 API 时如果 token 不新鲜会 401。所以导出这些格式之前应该刷一次。

    返回:
        {access_token, refresh_token (一般不变), id_token, expires_in, ...}
        失败抛 RuntimeError。
    """
    if not refresh_token:
        raise ValueError("refresh_token is required")
    return _post_token({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
        "scope": scope,
    }, proxy_url=proxy_url, timeout=timeout)


def _post_token(form: dict, *, proxy_url: str | None, timeout: int) -> dict:
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        },
    )
    if proxy_url:
        proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        https_handler = urllib.request.HTTPSHandler(context=_SSL_CTX)
        opener = urllib.request.build_opener(proxy_handler, https_handler)
        resp = opener.open(req, timeout=timeout)
    else:
        resp = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)
    raw = resp.read().decode("utf-8", errors="ignore")
    return json.loads(raw)


def _wait_for_oauth_callback(page, expected_state: str, timeout_s: int = 60,
                             poll: float = 1.0) -> dict | None:
    """等 page.url 出现 code= 或 出现 ERR_CONNECTION_REFUSED 错误页（localhost 接不到）。

    返回解析后的 callback dict 或 None（超时/出错）。
    """
    deadline = time.time() + timeout_s
    last_url = ""
    while time.time() < deadline:
        url = ""
        try:
            url = page.url or ""
        except Exception:
            pass
        if url and url != last_url:
            log.debug(f"  oauth url tick: {url[:120]}")
            last_url = url
        if url:
            cb = _parse_callback(url)
            if cb["code"]:
                if expected_state and cb["state"] and cb["state"] != expected_state:
                    log.warning(f"  state mismatch: got {cb['state'][:12]} != {expected_state[:12]}")
                return cb
            if cb["error"]:
                log.warning(f"  oauth error in URL: {cb['error']} {cb['error_description'][:120]}")
                return cb
        # 也检查 page DOM 是否泄露了 code（OpenAI 偶尔不重定向直接渲染）
        try:
            doc_url = page.run_js(
                "(function(){"
                "var a=document.querySelector('a[href*=\"code=\"]');"
                "return a?a.href:'';"
                "})()",
                as_expr=True,
            ) or ""
            if "code=" in doc_url:
                cb = _parse_callback(doc_url)
                if cb["code"]:
                    return cb
        except Exception:
            pass
        time.sleep(poll)
    return None


def run_codex_oauth_on_page(pipeline, email: str = "",
                            *, log_fn=None, timeout_s: int = 60,
                            proxy_url: str | None = None,
                            phone: str = "",
                            sms_api: str = "",
                            sms_provider=None,
                            sms_activation: dict | None = None,
                            max_otp_attempts: int = 3,
                            otp_deadline_s: int = 180,
                            outlook_creds: dict | None = None) -> dict | None:
    """直接走 Codex OAuth 链路完成 [登录 + 可选绑号 + 取 token]。

    ★ 不需要先在 chatgpt.com 登录 ★
    auth.openai.com 跟 chatgpt.com 是两套 session，在 chatgpt.com 上的登录态
    不会带过来——OAuth 链路里 OpenAI 仍会要求一次邮箱 OTP（你看到的"检查你的
    收件箱"页就是这个）。所以更简单的做法是：浏览器**全新无登录态**直接 navigate
    auth_url，链路内部依次处理：

        email_entry 输入邮箱
          → verification_page 邮箱 OTP（用 outlook_creds 抓码）
            → 可能的 add_phone（带 phone/sms_api 时绑号，没传则报错）
              → consent
                → callback?code=...
                  → POST /oauth/token 拿 access_token + refresh_token

    Args:
        pipeline: 已初始化的 GPTPipeline 实例（page 不需要登录态）
        email: 账号邮箱（必填，作为 login_hint + 邮箱输入框值）
        outlook_creds: outlook 邮箱凭据，必填，shape:
            {"email": ..., "refresh_token": ..., "client_id": ..., "password": ...}
        phone, sms_api: 仅当账号没绑号、需要在 OAuth 链路内 add_phone 时用
        sms_provider + sms_activation: 平台取号模式（smsbower 等），与 sms_api URL 二选一
        max_otp_attempts: SMS OTP 重试次数（仅 add_phone 时）
        otp_deadline_s: 单次 SMS OTP 等待秒数
        timeout_s: 等 callback 的最长秒数（不含 OTP / add_phone 自身的等待）
        proxy_url: 仅用于 token 交换 HTTP 请求

    Returns:
        {access_token, refresh_token, id_token, account_id, email,
         expires_in, expires_at_unix, phone_bound, raw}
        失败 {"error": ..., "stage": ...}
    """
    say = log_fn or (lambda m: log.info(m))
    page = pipeline.page

    if not email:
        return {"error": "email_required", "stage": "init"}
    if not outlook_creds or not outlook_creds.get("refresh_token"):
        say("[codex-oauth] outlook_creds 缺失或没有 refresh_token，无法在 OAuth 链路里抓邮箱 OTP")
        return {"error": "outlook_creds_required", "stage": "init"}

    # login_hint=email 让 OpenAI 直接定位账号，跳过 choose-an-account 页
    oauth_start = generate_oauth_url(login_hint=email or "")
    say(f"[codex-oauth] 直跑 OAuth: state={oauth_start['state'][:16]}… login_hint={email}")

    # 导航到 auth_url
    try:
        page.get(oauth_start["auth_url"])
    except Exception as exc:
        say(f"[codex-oauth] 导航 auth_url 异常: {exc}")
    time.sleep(2)

    phone_bound_in_flow = False
    # 主状态机：每轮检查 URL→callback / DOM→需要交互 / 等待
    # +240s 给邮箱 OTP（outlook OTP_TIMEOUT 默认 240）
    deadline = time.time() + max(timeout_s, 60) + (otp_deadline_s * max_otp_attempts if phone else 0) + 240
    last_url = ""
    consent_clicks = 0
    consent_seen_at = 0.0    # 第一次看到 consent URL 的时间
    choose_account_clicks = 0
    access_denied_retries = 0
    email_filled = False
    email_otp_done = False
    used_email_otps: set[str] = set()
    while time.time() < deadline:
        url = ""
        try:
            url = page.url or ""
        except Exception:
            pass
        if url and url != last_url:
            say(f"[codex-oauth] url tick: {url[:120]}")
            last_url = url

        # 1) URL 已经包含 callback ?code=... → 跳出去换 token
        cb = _parse_callback(url) if url else {"code": "", "state": "", "error": ""}
        if cb.get("code"):
            break
        if cb.get("error"):
            err_code = cb["error"]
            err_desc = cb.get("error_description", "")
            say(f"[codex-oauth] OAuth 错误 in URL: {err_code} {err_desc}")
            # access_denied + "consent verifier ... already been used" → 自动重试一次
            # 这种通常是因为账号之前已经授权过 codex，OpenAI 自动 redirect 时
            # verifier 已被消耗；我们再重新 navigate auth_url 拿到新的 verifier 即可。
            looks_recoverable = (
                err_code == "access_denied"
                and ("verifier" in err_desc.lower() or "already been used" in err_desc.lower())
            )
            if looks_recoverable and access_denied_retries < 2:
                access_denied_retries += 1
                say(f"[codex-oauth] consent verifier 失效，重新跑 OAuth (retry {access_denied_retries}/2)")
                # 生成全新 PKCE state，重新 navigate
                oauth_start = generate_oauth_url(login_hint=email or "")
                say(f"[codex-oauth] 新 state={oauth_start['state'][:16]}…")
                try:
                    page.get(oauth_start["auth_url"])
                except Exception:
                    pass
                time.sleep(2)
                last_url = ""
                consent_seen_at = 0.0
                continue
            return {"error": err_code, "stage": "callback",
                    "error_description": err_desc}

        # 2) DOM 探：是否到了 add_phone / consent / login_password 等需要我们动手的页面
        try:
            pipeline._inject_js("phone_bind.js")
        except Exception:
            pass
        # 先看 phone_bind.js 的状态机（包含 choose_account / add_phone）
        pb_state = pipeline._eval("__gpt_pb_state && __gpt_pb_state()") or "unknown"
        # 再看 signup.js 的状态机
        try:
            signup_state = pipeline._get_signup_state()
        except Exception:
            signup_state = "unknown"

        # 2a) choose-an-account 页：直接挑当前 email 那行
        if pb_state == "choose_account" or "/choose-an-account" in url or "/choose-account" in url:
            choose_account_clicks += 1
            if choose_account_clicks > 4:
                say(f"[codex-oauth] choose-an-account 卡死 {choose_account_clicks} 次，放弃")
                return {"error": "choose_account_stuck", "stage": "choose_account"}
            say(f"[codex-oauth] 进 choose-an-account 页 ({choose_account_clicks}/4)，挑账号 {email!r}")
            try:
                res = pipeline._eval(f"__gpt_pb_pickAccount({json.dumps(email or '')})") or {}
            except Exception as exc:
                res = {"clicked": False, "error": str(exc)}
            say(f"[codex-oauth] pickAccount: {res}")

            # 等点击生效（最多 5s）
            time.sleep(2)
            new_url = (page.url or "")
            if "/choose-an-account" in new_url or "/choose-account" in new_url:
                # 还在选账户页，硬刷一下 OAuth；带 prompt=login 强制走密码页
                say("[codex-oauth] 选账户没跳，重新跳 auth_url（带 prompt=login）")
                try:
                    forced_url = oauth_start["auth_url"] + "&prompt=login"
                    page.get(forced_url)
                except Exception:
                    pass
                time.sleep(3)
            continue

        # 2b) 邮箱输入页（OAuth 链路内的 login_email；不在 chatgpt.com 上）
        if signup_state == "email_entry" and "auth.openai.com" in url:
            if not email_filled:
                say(f"[codex-oauth] email_entry → 填邮箱 {email}")
                try:
                    pipeline._eval(f"__gpt_fillEmail({json.dumps(email)})")
                    time.sleep(0.4)
                    pipeline._eval("__gpt_clickContinue()")
                except Exception as exc:
                    say(f"[codex-oauth] 填邮箱异常: {exc}")
                email_filled = True
                time.sleep(2.5)
                continue
            time.sleep(1)
            continue

        # 2c) 邮箱 OTP 验证页（"检查你的收件箱"）
        # 优先于 add_phone 检测：OAuth 链路里先出邮箱 OTP，过了再考虑 add_phone
        is_email_verif = (
            signup_state == "verification_page"
            and "auth.openai.com" in url
            and "/email-verification" in url
        )
        if is_email_verif:
            if not email_otp_done:
                say("[codex-oauth] verification_page → 抓 outlook OTP")
                code = None
                try:
                    import email_provider
                    import config as _cfg
                    code = email_provider.fetch_otp(
                        outlook_creds["email"],
                        outlook_creds["refresh_token"],
                        outlook_creds.get("client_id", ""),
                        method=getattr(_cfg, "OTP_METHOD", "graph"),
                        timeout=getattr(_cfg, "OTP_TIMEOUT", 240),
                    )
                except Exception as exc:
                    say(f"[codex-oauth] 抓邮箱 OTP 失败: {exc}")
                    return {"error": f"email_otp_fetch_failed: {exc}", "stage": "email_otp"}
                if not code:
                    return {"error": "email_otp_timeout", "stage": "email_otp"}
                if code in used_email_otps:
                    say(f"[codex-oauth] 邮箱 OTP {code} 已用过，等下一条")
                    time.sleep(8)
                    continue
                used_email_otps.add(code)
                say(f"[codex-oauth] 邮箱 OTP: {code}")
                try:
                    pipeline._eval(f"__gpt_fillOTP({json.dumps(code)})")
                    time.sleep(0.4)
                    pipeline._eval("__gpt_clickContinue()")
                except Exception as exc:
                    say(f"[codex-oauth] 填邮箱 OTP 异常: {exc}")
                email_otp_done = True
                time.sleep(2.5)
                continue
            time.sleep(1)
            continue

        # 2d) password_page：我们没真密码，让它显式失败让外层重试，
        # 或者尝试切到 OTP 登录（点 "使用一次性验证码登录" 之类的链接）
        if signup_state == "password_page" and "auth.openai.com" in url:
            say("[codex-oauth] password_page → 试切 OTP 登录")
            try:
                pipeline._eval(
                    "(() => {"
                    "  const re = /one[-\\s]*time|passcode|verification|use\\s+(?:a\\s+)?code|验证码|一次性/i;"
                    "  const els = document.querySelectorAll('button, a, [role=\"button\"]');"
                    "  for (const e of els) {"
                    "    const t = (e.textContent || '').replace(/\\s+/g, ' ').trim();"
                    "    if (re.test(t) && e.offsetParent) { e.click(); return; }"
                    "  }"
                    "})()"
                )
            except Exception:
                pass
            time.sleep(3)
            continue

        # 2e) add_phone：在 OAuth 链路内用 phone+sms_api 绑号（或平台 provider+activation）
        if pb_state in ("add_phone_input", "add_phone_otp") or signup_state == "add_phone_page" or "/add-phone" in url:
            has_platform = sms_provider is not None and bool(sms_activation)
            if not phone or (not sms_api and not has_platform):
                say("[codex-oauth] 需要 add_phone 但没传 phone/sms_api（或平台 activation），放弃")
                return {"error": "add_phone_required_but_no_phone", "stage": "add_phone"}
            say(f"[codex-oauth] OAuth 路由到 add_phone：{url[:120]}")
            inner = _handle_add_phone_in_oauth(
                pipeline, phone, sms_api,
                max_otp_attempts=max_otp_attempts,
                otp_deadline_s=otp_deadline_s,
                say=say,
                sms_provider=sms_provider,
                sms_activation=sms_activation,
            )
            if not inner.get("ok"):
                return {"error": inner.get("error") or "add_phone_failed",
                        "stage": "add_phone"}
            phone_bound_in_flow = True
            # 绑成功后页面会自动继续 OAuth；回主循环等下一次 tick
            time.sleep(2)
            continue

        # 2f) oauth consent
        # 重要：账号若之前已授权过，OpenAI 会**自动**跳过 consent 直接 redirect 到 callback。
        # 我们的过早点击 + 它的自动跳转 = consent verifier 被用两次 → access_denied。
        # 所以先给它 3 秒让 OpenAI 自跳；超时还在 consent 才主动点 Continue。
        if signup_state == "oauth_consent" or "/codex/consent" in url or "/sign-in-with-chatgpt" in url:
            if consent_seen_at == 0.0:
                consent_seen_at = time.time()
                say(f"[codex-oauth] 进 consent 页，等 3s 看是否自动 redirect")
                time.sleep(3)
                continue
            # 3s 后还在 consent → 主动点
            if time.time() - consent_seen_at < 3.0:
                time.sleep(0.8)
                continue
            if consent_clicks >= 3:
                say(f"[codex-oauth] consent 点击 {consent_clicks} 次仍未跳转，放弃")
                return {"error": "consent_stuck", "stage": "consent"}
            consent_clicks += 1
            say(f"[codex-oauth] consent 主动点击 ({consent_clicks}/3)")
            try:
                pipeline._eval("__gpt_clickOAuthConfirm()")
            except Exception:
                pass
            time.sleep(2)
            continue

        # 旧的"登录态丢失"兜底已不需要——上面 2b/2c/2d 已经覆盖 email_entry /
        # verification_page / password_page。这里只兜底 unknown 状态，等下一轮。
        time.sleep(1.5)

    # 出主循环：再尝试用最后一次抓到的 URL（万一 break 是因为 cb.code 命中）
    cb_final = _parse_callback(last_url) if last_url else {"code": "", "state": ""}
    if not cb_final.get("code"):
        say("[codex-oauth] 主循环超时，未拿到 callback code")
        return {"error": "callback_timeout", "stage": "callback"}

    say(f"[codex-oauth] 拿到 code，长度={len(cb_final['code'])} state_match={cb_final.get('state') == oauth_start['state']}")
    try:
        token_resp = exchange_code(
            code=cb_final["code"],
            code_verifier=oauth_start["code_verifier"],
            proxy_url=proxy_url,
        )
    except Exception as exc:
        say(f"[codex-oauth] token 交换失败: {exc}")
        return {"error": f"token_exchange_failed: {exc}", "stage": "token_exchange"}

    access_token = (token_resp.get("access_token") or "").strip()
    refresh_token = (token_resp.get("refresh_token") or "").strip()
    id_token = (token_resp.get("id_token") or "").strip()
    expires_in = int(token_resp.get("expires_in") or 0)

    if not access_token:
        say(f"[codex-oauth] token 响应缺 access_token: {str(token_resp)[:200]}")
        return {"error": "no_access_token_in_response", "stage": "token_exchange"}

    claims = _decode_jwt(id_token)
    auth = claims.get("https://api.openai.com/auth") or {}
    account_id = str(auth.get("chatgpt_account_id") or "")
    email_resolved = email or str(claims.get("email") or "")

    say(f"[codex-oauth] ✓ access_token({len(access_token)}) refresh_token({len(refresh_token)}) "
        f"account={account_id} email={email_resolved} phone_bound_in_flow={phone_bound_in_flow}")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "account_id": account_id,
        "email": email_resolved,
        "expires_in": expires_in,
        "expires_at_unix": int(time.time()) + max(expires_in, 0),
        "phone_bound": phone_bound_in_flow,
        "raw": token_resp,
    }


def _handle_add_phone_in_oauth(pipeline, phone: str, sms_api: str,
                               *, max_otp_attempts: int, otp_deadline_s: int,
                               say,
                               sms_provider=None,
                               sms_activation: dict | None = None) -> dict:
    """在 OAuth 链路内的 auth.openai.com/add-phone 页面填号 + 提交 OTP。

    跟 ref 的 _handle_add_phone_challenge 同思路，但用 DrissionPage + 我们自己的
    js/phone_bind.js helpers。这里的成功标志：page.url 不再含 /add-phone（OAuth
    自然继续）。
    """
    page = pipeline.page

    # 等 add-phone 页 form 渲染好
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            pipeline._inject_js("phone_bind.js")
        except Exception:
            pass
        cur = pipeline._eval("__gpt_pb_state && __gpt_pb_state()") or "unknown"
        if cur in ("add_phone_input", "add_phone_otp"):
            break
        time.sleep(1.5)
    else:
        return {"ok": False, "error": "add_phone_form_not_ready"}

    # ---- 提交手机号（如果还在 input 状态） ----
    if pipeline._eval("__gpt_pb_state()") == "add_phone_input":
        submit = pipeline._eval(
            f"__gpt_pb_submitPhone({json.dumps(phone)})"
        ) or {}
        say(f"[codex-oauth/add-phone] submitPhone: {submit}")
        if not submit.get("ok"):
            return {"ok": False,
                    "error": f"submit_phone_failed: {submit.get('reason') or submit}"}
        # 等 OTP 输入框出现
        d2 = time.time() + 30
        while time.time() < d2:
            cur = pipeline._eval("__gpt_pb_state()")
            if cur == "add_phone_otp":
                break
            if cur == "phone_in_use":
                return {"ok": False, "error": "phone_number_in_use"}
            if cur == "left_add_phone":
                return {"ok": True}
            time.sleep(1.5)
        else:
            return {"ok": False, "error": "wait_otp_input_timeout"}

    # ---- 拉 SMS OTP + 提交 ----
    from pipeline import fetch_sms_otp
    used_codes: set[str] = set()
    last_error = ""
    use_platform = sms_provider is not None and bool(sms_activation)
    for attempt in range(max_otp_attempts):
        if use_platform:
            say(
                f"[codex-oauth/add-phone] OTP 尝试 {attempt + 1}/{max_otp_attempts}: "
                f"平台拉码 provider={getattr(sms_provider, 'name', '?')} "
                f"id={sms_activation.get('id')}"
            )
            code = fetch_sms_otp(
                "",
                deadline_s=otp_deadline_s,
                provider=sms_provider,
                activation=sms_activation,
            )
        else:
            say(f"[codex-oauth/add-phone] OTP 尝试 {attempt + 1}/{max_otp_attempts}: 拉码 {sms_api[:60]}...")
            code = fetch_sms_otp(sms_api, deadline_s=otp_deadline_s)
        if not code:
            last_error = "otp_fetch_timeout"
            try:
                pipeline._eval("__gpt_pb_resend && __gpt_pb_resend()")
            except Exception:
                pass
            time.sleep(3)
            continue
        if code in used_codes:
            time.sleep(8)
            continue
        used_codes.add(code)
        say(f"[codex-oauth/add-phone] 拿到 OTP: {code}")

        otp_res = pipeline._eval(
            f"__gpt_pb_submitOtp({json.dumps(code)})"
        ) or {}
        say(f"[codex-oauth/add-phone] submitOtp: {otp_res}")
        if not otp_res.get("ok"):
            last_error = f"submit_otp_failed: {otp_res.get('reason') or otp_res}"
            continue

        # 等离开 add-phone（OAuth 链路继续）
        d3 = time.time() + 30
        while time.time() < d3:
            cur = pipeline._eval("__gpt_pb_state()")
            url = page.url or ""
            if "/add-phone" not in url and cur != "add_phone_input" and cur != "add_phone_otp":
                say("[codex-oauth/add-phone] ✓ 已离开 add-phone，OAuth 继续")
                return {"ok": True}
            if cur == "otp_invalid":
                last_error = "otp_invalid"
                break
            if cur == "phone_in_use":
                return {"ok": False, "error": "phone_number_in_use"}
            time.sleep(1.5)
    return {"ok": False, "error": last_error or "otp_max_attempts"}
