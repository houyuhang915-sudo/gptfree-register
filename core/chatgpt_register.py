"""ChatGPT Mail Auth registration protocol (curl_cffi + Sentinel).

对照 ``~/Downloads/chatgpt_register``：
- TLS 指纹：curl_cffi impersonate=chrome136
- Sentinel：实时 SDK P + Turnstile + SO 双 header
- 流程：login_hint 初始化自动发码 → OTP 校验 → about-you → create_account → OAuth callback

OTP 仍走项目原版 ``email_provider.fetch_otp``（Graph→IMAP fallback），兼容 outlook 卡密。

对外 API 保持兼容：
    bot = ChatGPTRegister(outlook_creds, log_fn=..., proxy=...)
    result = bot.register()
    # {"status": "success"|"failed", "email", "password", "access_token", ...}
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import string
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from urllib.parse import urlencode

log = logging.getLogger("chatgpt_register")

FIRST_NAMES = [
    "James", "John", "Robert", "Michael", "David", "William", "Richard", "Joseph",
    "Thomas", "Chris", "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara",
    "Susan", "Jessica", "Sarah", "Karen", "Daniel", "Matthew", "Anthony", "Mark",
    "Donald", "Steven", "Paul", "Andrew", "Joshua", "Kenneth",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Wilson", "Anderson", "Taylor", "Thomas", "Moore",
    "Jackson", "Martin", "Lee", "Thompson", "White",
]


def _norm_email(value: str) -> str:
    return str(value or "").strip()


def random_name() -> str:
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def random_age() -> int:
    return random.randint(24, 36)


def random_password(length: int = 16) -> str:
    if length < 12:
        length = 12
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    special = "!@#$%^&*"
    must = [
        random.choice(upper),
        random.choice(lower),
        random.choice(digits),
        random.choice(special),
    ]
    all_chars = upper + lower + digits + special
    rest = random.choices(all_chars, k=length - len(must))
    pwd_list = must + rest
    random.shuffle(pwd_list)
    return "".join(pwd_list)


def birthdate_from_age(age: int) -> str:
    return (datetime.now() - timedelta(days=int(age) * 365)).strftime("%Y-%m-%d")


def _normalize_proxy(proxy: str) -> str:
    """curl_cffi 接受 http(s)/socks5 URL；空串原样返回。"""
    p = (proxy or "").strip()
    if not p:
        return ""
    if "://" not in p:
        # host:port 或 user:pass@host:port → 默认 http
        p = "http://" + p
    return p


def auth_step_requires_password(continue_url: str, page_type: str) -> bool:
    """Follow the server-selected auth branch instead of forcing password creation."""
    marker = f"{continue_url or ''} {page_type or ''}".lower()
    return "password" in marker


# ─────────────────────────────────────────────────────────────────────
# Sentinel provider（共享 session / 代理）
# ─────────────────────────────────────────────────────────────────────
class _SentinelWithProxy:
    """包装 sentinel_token.SentinelTokenProvider，注入 proxy 与共享 session。"""

    def __init__(self, impersonate: str = "chrome136", proxy: str = ""):
        from sentinel_token import SentinelTokenProvider as _Impl

        class _Provider(_Impl):
            def __init__(self, impersonate: str = "chrome136", cookies: dict = None, proxy: str = None):
                super().__init__(impersonate=impersonate, cookies=cookies)
                self._proxy = proxy or ""

            async def _get_session(self):
                if not self._session:
                    from curl_cffi import requests as _req
                    kwargs: dict[str, Any] = {"impersonate": self.impersonate, "timeout": 60}
                    if self._proxy:
                        kwargs["proxies"] = {"http": self._proxy, "https": self._proxy}
                    self._session = _req.AsyncSession(**kwargs)
                return self._session

            def set_session(self, session) -> None:
                self._session = session

            def set_cookies(self, cookies: dict) -> None:
                self._cookies = cookies or {}

        self._impl = _Provider(impersonate=impersonate, proxy=proxy or None)

    def __getattr__(self, name: str):
        return getattr(self._impl, name)


# ─────────────────────────────────────────────────────────────────────
# OpenAI Auth Client（新协议）
# ─────────────────────────────────────────────────────────────────────
class OpenAIAuthClient:
    BASE_URL = "https://auth.openai.com"
    CHATGPT_URL = "https://chatgpt.com"

    def __init__(
        self,
        *,
        impersonate: str = "chrome136",
        sentinel: Any = None,
        proxy: str = "",
    ):
        self.impersonate = impersonate
        self.proxy = _normalize_proxy(proxy)
        self.sentinel = sentinel or _SentinelWithProxy(impersonate=impersonate, proxy=self.proxy)
        self._session = None
        self.device_id: str = str(uuid.uuid4())
        self.cookies: dict = {}

    async def _get_session(self):
        if not self._session:
            from curl_cffi import requests as _req
            kwargs: dict[str, Any] = {"impersonate": self.impersonate, "timeout": 60}
            if self.proxy:
                kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
            self._session = _req.AsyncSession(**kwargs)
        return self._session

    async def share_session_with_sentinel(self) -> None:
        s = await self._get_session()
        set_session = getattr(self.sentinel, "set_session", None)
        if callable(set_session):
            set_session(s)

    def _common_headers(self, referer: str | None = None) -> dict:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if referer:
            headers["referer"] = referer
        return headers

    async def _add_sentinel_headers(
        self,
        headers: dict,
        flow: str,
        referer: str,
        *,
        force_refresh: bool = False,
        log_fn=None,
    ) -> dict:
        get_token = self.sentinel.get_token
        try:
            token = await get_token(flow, self.device_id, force_refresh=force_refresh)
        except TypeError:
            # 旧签名兼容
            if force_refresh:
                inv = getattr(self.sentinel, "invalidate_cache", None)
                if callable(inv):
                    inv()
            token = await get_token(flow, self.device_id)
        if not token:
            raise RuntimeError(f"sentinel get_token 失败 flow={flow}")
        # 不把内部标记字段塞进 header
        header_token = {k: v for k, v in token.items() if not str(k).startswith("_")}
        missing_t = bool(token.get("_turnstile_missing"))
        if log_fn:
            log_fn(
                f"  [sentinel] flow={flow} keys={list(header_token.keys())} "
                f"has_t={'t' in header_token} t_len={len(header_token.get('t') or '')} "
                f"missing_t={missing_t}"
            )
        if missing_t:
            raise RuntimeError(
                "sentinel turnstile(t) 生成失败：请确认已 npm install jsdom，"
                "且 sentinel_vm/sdk.js 或 ~/.codeium/windsurf/sentinel_sdk_full.js 存在"
            )
        headers["openai-sentinel-token"] = json.dumps(header_token)
        so_token = await self.sentinel.get_so_token(flow, self.device_id)
        if so_token:
            headers["openai-sentinel-so-token"] = json.dumps(so_token)
        return headers

    async def init_page_email(self, email: str) -> dict:
        """chatgpt.com → csrf → signin(login_hint) → authorize → /email-verification（自动发码）"""
        s = await self._get_session()

        await s.get(self.CHATGPT_URL)

        csrf_resp = await s.get(f"{self.CHATGPT_URL}/api/auth/csrf")
        if csrf_resp.status_code != 200:
            raise RuntimeError(f"CSRF 请求失败: {csrf_resp.status_code}")
        csrf_token = csrf_resp.json().get("csrfToken")
        if not csrf_token:
            raise RuntimeError("CSRF token 为空")

        params = urlencode({
            "prompt": "login",
            "screen_hint": "login_or_signup",
            "login_hint": email,
        })
        signin_resp = await s.post(
            f"{self.CHATGPT_URL}/api/auth/signin/openai?{params}",
            data={
                "callbackUrl": f"{self.CHATGPT_URL}/",
                "csrfToken": csrf_token,
                "json": "true",
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
            allow_redirects=False,
        )
        loc = ""
        try:
            loc = signin_resp.json().get("url", "")
        except Exception:
            loc = signin_resp.headers.get("location", "") or ""

        final_resp = None
        while loc:
            final_resp = await s.get(loc, allow_redirects=False)
            loc = final_resp.headers.get("location", "") or ""
            if not loc:
                break

        for cookie in s.cookies.jar:
            if cookie.name == "oai-did":
                self.device_id = cookie.value
                break
        self.cookies = {c.name: c.value for c in s.cookies.jar}
        return {
            "status": final_resp.status_code if final_resp else 0,
            "cookies": self.cookies,
            "device_id": self.device_id,
        }

    async def validate_email_otp(self, code: str) -> dict:
        s = await self._get_session()
        url = f"{self.BASE_URL}/api/accounts/email-otp/validate"
        referer = f"{self.BASE_URL}/email-verification"
        headers = self._common_headers(referer=referer)
        headers["accept"] = "application/json"
        resp = await s.post(url, json={"code": code}, headers=headers)
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text}

    async def register_password_email(
        self,
        email: str,
        password: str,
        *,
        force_refresh_sentinel: bool = False,
        log_fn=None,
    ) -> dict:
        """POST /api/accounts/user/register to set the ChatGPT account password.

        flow=username_password_create，referer=/create-account/password
        """
        s = await self._get_session()
        url = f"{self.BASE_URL}/api/accounts/user/register"
        referer = f"{self.BASE_URL}/create-account/password"
        headers = await self._add_sentinel_headers(
            self._common_headers(referer=referer),
            "username_password_create",
            referer,
            force_refresh=force_refresh_sentinel,
            log_fn=log_fn,
        )
        resp = await s.post(
            url,
            json={"password": password, "username": email},
            headers=headers,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"status": resp.status_code, "text": resp.text}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return data

    async def create_account(
        self,
        name: str,
        birthdate: str,
        *,
        force_refresh_sentinel: bool = False,
        log_fn=None,
    ) -> dict:
        s = await self._get_session()
        url = f"{self.BASE_URL}/api/accounts/create_account"
        referer = f"{self.BASE_URL}/about-you"
        headers = await self._add_sentinel_headers(
            self._common_headers(referer=referer),
            "oauth_create_account",
            referer,
            force_refresh=force_refresh_sentinel,
            log_fn=log_fn,
        )
        resp = await s.post(url, json={"name": name, "birthdate": birthdate}, headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {"status": resp.status_code, "text": resp.text}
        if isinstance(data, dict):
            data["_http_status"] = resp.status_code
        return data

    async def close(self) -> None:
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        close_fn = getattr(self.sentinel, "close", None)
        if callable(close_fn):
            try:
                await close_fn()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────
# 同步 OTP：email_provider.fetch_otp
# ─────────────────────────────────────────────────────────────────────
def _fetch_otp_sync(
    email: str,
    refresh_token: str,
    client_id: str = "",
    *,
    timeout: int = 90,
    log_fn: Optional[Callable[[str], None]] = None,
) -> str:
    _log = log_fn or log.info
    try:
        from email_provider import fetch_otp
    except Exception as exc:
        _log(f"[OTP] email_provider 加载失败: {exc}")
        return ""

    try:
        code = fetch_otp(
            email=email,
            refresh_token=refresh_token,
            client_id=client_id or "",
            method="graph",
            timeout=max(30, int(timeout)),
        )
    except Exception as exc:
        _log(f"[OTP] fetch_otp 异常: {type(exc).__name__}: {exc}")
        return ""

    code = (code or "").strip()
    if code:
        _log(f"[OTP] ✓ 拿到 OTP={code}")
    else:
        _log("[OTP] ✗ fetch_otp returned empty")
    return code


# ─────────────────────────────────────────────────────────────────────
# 公共入口
# ─────────────────────────────────────────────────────────────────────
class ChatGPTRegister:
    """注册一个新的 ChatGPT 账号（outlook 邮箱收 OTP）。

    用法：
        bot = ChatGPTRegister({
            "email": "...",
            "password": "...",
            "client_id": "...",
            "refresh_token": "...",
        }, proxy="socks5://...")
        result = bot.register()
    """

    def __init__(
        self,
        outlook_creds: dict,
        *,
        log_fn=None,
        proxy: str = "",
        otp_timeout: int = 90,
        impersonate: str = "firefox144",
        with_password: bool = True,
    ):
        self.outlook = outlook_creds or {}
        self.email = _norm_email(self.outlook.get("email"))
        if not self.email:
            raise ValueError("outlook_creds.email 不能为空")
        self._log_fn = log_fn or log.info
        self.proxy = _normalize_proxy(proxy or self.outlook.get("proxy") or "")
        self.otp_timeout = int(otp_timeout or 90)
        self.impersonate = impersonate or "chrome136"
        # True: try POST /user/register after OTP to set a ChatGPT password.
        self.with_password = bool(with_password)

        # 兼容字段
        self.password = ""
        self.access_token = ""
        self.session_token = ""
        self.device_id = ""

    def _l(self, msg: str) -> None:
        try:
            self._log_fn(msg)
        except Exception:
            log.info(msg)

    async def _read_existing_session(self, auth: OpenAIAuthClient) -> tuple[str, str, dict]:
        """Read a session created by OTP validation or an already-existing account."""
        s = await auth._get_session()
        resp = await s.get(f"{auth.CHATGPT_URL}/api/auth/session")
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        access_token = str(payload.get("accessToken") or payload.get("access_token") or "")
        session_token = str(payload.get("sessionToken") or "")
        if not session_token:
            try:
                for cookie in s.cookies.jar:
                    if cookie.name == "__Secure-next-auth.session-token":
                        session_token = str(cookie.value or "")
                        break
            except Exception:
                pass
        if access_token:
            payload["accessToken"] = access_token
        if session_token:
            payload["sessionToken"] = session_token
        return access_token, session_token, payload

    def _recovered_success(
        self,
        *,
        password: str,
        access_token: str,
        session_token: str,
        session_json: dict,
        name: str,
        birthdate: str,
        reason: str,
    ) -> dict:
        self.access_token = access_token
        self.session_token = session_token
        self._l(f"  ✓ OTP 会话恢复成功 reason={reason}")
        return {
            "email": self.email,
            "password": "",
            "session_token": session_token,
            "session_json": session_json,
            "access_token": access_token,
            "device_id": self.device_id,
            "status": "success",
            "password_set": False,
            "raw": {
                "name": name,
                "birthdate": birthdate,
                "password_set": False,
                "recovered_existing_session": reason,
            },
        }

    def register(self) -> dict:
        """跑完整协议注册流程（同步入口，内部 asyncio）。"""
        self._l("=" * 60)
        self._l(f"  [register/outlook-auth] {self.email}")
        self._l("=" * 60)

        client_id = _norm_email(self.outlook.get("client_id"))
        refresh_token = _norm_email(self.outlook.get("refresh_token"))
        if not refresh_token:
            return self._failed("missing_refresh_token: 没有 outlook refresh_token，无法收 OTP")

        password = random_password()
        name = random_name()
        age = random_age()
        birthdate = birthdate_from_age(age)
        self.password = password
        self._l(
            f"  身份: {name}  年龄: {age}  密码: {password[:4]}****  "
            f"proxy={'yes' if self.proxy else 'no'}  with_password={self.with_password}"
        )

        try:
            result = asyncio.run(
                self._register_async(
                    password=password,
                    name=name,
                    birthdate=birthdate,
                    client_id=client_id,
                    refresh_token=refresh_token,
                )
            )
        except RuntimeError as exc:
            # 已有 running loop（极少见）：丢到新线程跑
            if "asyncio.run()" in str(exc) or "running event loop" in str(exc).lower():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(
                        lambda: asyncio.run(
                            self._register_async(
                                password=password,
                                name=name,
                                birthdate=birthdate,
                                client_id=client_id,
                                refresh_token=refresh_token,
                            )
                        )
                    )
                    result = fut.result()
            else:
                return self._failed(f"asyncio RuntimeError: {exc}")
        except Exception as exc:
            return self._failed(f"{type(exc).__name__}: {exc}")

        return result

    async def _register_async(
        self,
        *,
        password: str,
        name: str,
        birthdate: str,
        client_id: str,
        refresh_token: str,
    ) -> dict:
        t0 = time.time()

        def _ts() -> str:
            return f"[{time.time() - t0:.1f}s]"

        auth = OpenAIAuthClient(impersonate=self.impersonate, proxy=self.proxy)
        try:
            # 1. 初始化（服务端自动发码）
            self._l(f"  {_ts()} [..] 初始化 OpenAI 页面 (login_hint=email)...")
            since = time.time()
            await auth.share_session_with_sentinel()
            try:
                init = await auth.init_page_email(self.email)
            except Exception as exc:
                return self._failed(f"init_page_email: {type(exc).__name__}: {exc}")

            self.device_id = init.get("device_id") or auth.device_id
            set_cookies = getattr(auth.sentinel, "set_cookies", None)
            if callable(set_cookies):
                set_cookies(init.get("cookies") or {})
            self._l(f"  {_ts()} [+] 设备ID: {(self.device_id or '')[:12]}...")

            # 2. 拉 OTP（同步 Graph/IMAP，丢线程池避免阻塞事件循环）
            self._l(f"  {_ts()} [..] 等待邮箱验证码 (timeout={self.otp_timeout}s)...")
            code = await asyncio.to_thread(
                _fetch_otp_sync,
                self.email,
                refresh_token,
                client_id,
                timeout=self.otp_timeout,
                log_fn=self._log_fn,
            )
            # since 仅作日志；email_provider 自己做时间窗口
            _ = since
            if not code:
                return self._failed(f"otp_timeout: {self.otp_timeout}s 内未收到验证码")

            # 3. 校验 OTP
            self._l(f"  {_ts()} [..] 提交邮箱验证码...")
            validate_result = await auth.validate_email_otp(code)
            if isinstance(validate_result, dict) and "error" in validate_result:
                err = validate_result.get("error") or {}
                err_code = err.get("code", "") if isinstance(err, dict) else str(err)
                return self._failed(f"otp_validate_failed: {err_code or validate_result}")
            self._l(f"  {_ts()} [+] 邮箱验证通过")

            continue_url = ""
            if isinstance(validate_result, dict):
                continue_url = (
                    validate_result.get("continue_url")
                    or (validate_result.get("page") or {}).get("payload", {}).get("url")
                    or ""
                )
            page_type = ""
            if isinstance(validate_result, dict):
                page_type = str((validate_result.get("page") or {}).get("type") or "")

            s = await auth._get_session()
            # 4a. 只执行服务端明确选择的密码分支。Passwordless/about-you
            # 流程里强行调用 user/register 会推进错 auth step。
            password_set = False
            need_password = self.with_password and auth_step_requires_password(
                continue_url,
                page_type,
            )
            if self.with_password and not need_password:
                self._l(f"  {_ts()} [..] 服务端选择 passwordless 分支，跳过设置密码")
            if need_password:
                if continue_url:
                    self._l(f"  {_ts()} [..] 导航 continue_url (password/about-you)...")
                    await s.get(
                        continue_url,
                        headers={"referer": f"{auth.BASE_URL}/email-verification"},
                    )
                self._l(f"  {_ts()} [..] 设置密码 (username_password_create)...")
                pw_result: dict = {}
                for pw_attempt in range(2):
                    try:
                        pw_result = await auth.register_password_email(
                            self.email,
                            password,
                            force_refresh_sentinel=(pw_attempt > 0),
                            log_fn=self._l,
                        )
                    except Exception as exc:
                        self._l(f"  [!] set_password 异常: {type(exc).__name__}: {exc}")
                        if pw_attempt < 1:
                            await asyncio.sleep(1)
                            continue
                        pw_result = {"error": {"code": "exception", "message": str(exc)}}
                    if isinstance(pw_result, dict) and "error" in pw_result:
                        err = pw_result.get("error") or {}
                        err_code = err.get("code", "") if isinstance(err, dict) else str(err)
                        err_msg = err.get("message", "") if isinstance(err, dict) else ""
                        self._l(
                            f"  [!] set_password error code={err_code} "
                            f"http={pw_result.get('_http_status','')} msg={str(err_msg)[:160]}"
                        )
                        if pw_attempt < 1:
                            await asyncio.sleep(1)
                            continue
                        if err_code in {"username_already_exists", "user_already_exists"}:
                            access_token, session_token, session_json = (
                                await self._read_existing_session(auth)
                            )
                            if access_token:
                                return self._recovered_success(
                                    password=password,
                                    access_token=access_token,
                                    session_token=session_token,
                                    session_json=session_json,
                                    name=name,
                                    birthdate=birthdate,
                                    reason=err_code,
                                )
                        # password 页强制失败；passwordless 路径可降级
                        if "password" in (continue_url or "").lower():
                            return self._failed(
                                f"set_password_failed: {err_code or pw_result}"
                                + (f" ({err_msg})" if err_msg else "")
                            )
                        self._l("  [!] 设密码失败，降级继续 about-you / create_account")
                        break
                    password_set = True
                    self._l(f"  {_ts()} [+] 密码已设置")
                    # 设密成功后可能返回新的 continue_url → about-you
                    if isinstance(pw_result, dict):
                        cont = (
                            pw_result.get("continue_url")
                            or (pw_result.get("page") or {}).get("payload", {}).get("url")
                            or ""
                        )
                        if cont:
                            continue_url = cont
                    break

            # 4b. 导航 about-you
            about_you_url = continue_url or ""
            if about_you_url and (
                "about-you" in about_you_url
                or "about_you" in about_you_url
                or not password_set
            ):
                self._l(f"  {_ts()} [..] 导航到 about-you...")
                await s.get(
                    about_you_url,
                    headers={
                        "referer": (
                            f"{auth.BASE_URL}/create-account/password"
                            if password_set
                            else f"{auth.BASE_URL}/email-verification"
                        )
                    },
                )
                self._l(f"  {_ts()} [+] 已访问 about-you")
            elif password_set and about_you_url and "about-you" not in about_you_url:
                # 设密后 continue 可能不是 about-you，仍访问一次标准 about-you 页
                self._l(f"  {_ts()} [..] 访问 about-you 页面...")
                await s.get(
                    f"{auth.BASE_URL}/about-you",
                    headers={"referer": f"{auth.BASE_URL}/create-account/password"},
                )
                self._l(f"  {_ts()} [+] 已访问 about-you")

            # 5. 创建账号（带 sentinel）
            self._l(f"  {_ts()} [..] 创建账号 (sentinel oauth_create_account)...")
            create_result: dict = {}
            create_ok = False
            for create_attempt in range(3):
                try:
                    create_result = await auth.create_account(
                        name,
                        birthdate,
                        force_refresh_sentinel=(create_attempt > 0),
                        log_fn=self._l,
                    )
                except Exception as exc:
                    self._l(f"  [!] create_account 异常: {type(exc).__name__}: {exc}")
                    if create_attempt < 2:
                        await asyncio.sleep(2)
                        continue
                    return self._failed(f"create_account_exception: {type(exc).__name__}: {exc}")
                if isinstance(create_result, dict) and "error" in create_result:
                    err = create_result.get("error") or {}
                    err_code = err.get("code", "") if isinstance(err, dict) else str(err)
                    err_msg = err.get("message", "") if isinstance(err, dict) else ""
                    http_st = create_result.get("_http_status", "")
                    self._l(
                        f"  [!] create_account error code={err_code} http={http_st} "
                        f"msg={str(err_msg)[:160]}"
                    )
                    if err_code == "registration_disallowed" and create_attempt < 2:
                        self._l(f"  [!] registration_disallowed, 刷新 sentinel 重试 ({create_attempt + 1}/3)")
                        await asyncio.sleep(2)
                        continue
                    if err_code in {
                        "invalid_auth_step",
                        "user_already_exists",
                        "username_already_exists",
                    }:
                        access_token, session_token, session_json = (
                            await self._read_existing_session(auth)
                        )
                        if access_token:
                            return self._recovered_success(
                                password=password,
                                access_token=access_token,
                                session_token=session_token,
                                session_json=session_json,
                                name=name,
                                birthdate=birthdate,
                                reason=err_code,
                            )
                    return self._failed(
                        f"create_account_failed: {err_code or create_result}"
                        + (f" ({err_msg})" if err_msg else "")
                    )
                create_ok = True
                break

            if not create_ok:
                return self._failed("create_account_failed: unknown")
            self._l(f"  {_ts()} [+] 账号创建成功")

            # 6. OAuth 回调 + session
            access_token = ""
            session_token = ""
            session_json: dict = {}
            continue_url = ""
            if isinstance(create_result, dict):
                continue_url = create_result.get("continue_url") or ""
            if continue_url:
                self._l(f"  {_ts()} [..] OAuth 回调...")
                s = await auth._get_session()
                cb_resp = await s.get(continue_url, allow_redirects=True)
                self._l(f"  {_ts()} [+] 回调状态: {cb_resp.status_code}")

                self._l(f"  {_ts()} [..] 获取 session...")
                sess_resp = await s.get(f"{auth.CHATGPT_URL}/api/auth/session")
                try:
                    sess_data = sess_resp.json()
                except Exception:
                    sess_data = {}
                if isinstance(sess_data, dict):
                    session_json = dict(sess_data)
                access_token = (sess_data.get("accessToken") or sess_data.get("access_token") or "")
                session_token = sess_data.get("sessionToken") or ""
                if not session_token:
                    try:
                        for cookie in s.cookies.jar:
                            if cookie.name == "__Secure-next-auth.session-token":
                                session_token = cookie.value
                                break
                    except Exception:
                        pass
                if access_token:
                    session_json["accessToken"] = access_token
                if session_token:
                    session_json["sessionToken"] = session_token
                if access_token:
                    self._l(f"  {_ts()} [+] accessToken: {access_token[:20]}...")
                else:
                    self._l(f"  {_ts()} [!] 未获取到 accessToken: {str(sess_resp.text)[:200]}")

            if not access_token:
                return self._failed("registered but no access_token returned")

            self.access_token = access_token
            self.session_token = session_token
            self._l(
                f"  ✓ 注册成功 access_token={access_token[:24]}... "
                f"password_set={password_set}"
            )
            return {
                "email": self.email,
                "password": password if password_set else "",
                "session_token": session_token,
                "session_json": session_json,
                "access_token": access_token,
                "device_id": self.device_id,
                "status": "success",
                "password_set": password_set,
                "raw": {
                    "name": name,
                    "birthdate": birthdate,
                    "password_set": password_set,
                    "create_result": {
                        k: create_result.get(k)
                        for k in ("continue_url", "page")
                        if isinstance(create_result, dict) and k in create_result
                    },
                },
            }
        finally:
            await auth.close()

    def _failed(self, error: str) -> dict:
        self._l(f"  ✗ 注册失败: {error}")
        return {
            "email": self.email,
            "password": self.password,
            "session_token": "",
            "access_token": "",
            "device_id": self.device_id or "",
            "status": "failed",
            "error": error,
        }


def register_account(outlook_creds: dict, *, log_fn=None, proxy: str = "") -> dict:
    """便捷函数：注册单个账号。"""
    bot = ChatGPTRegister(outlook_creds, log_fn=log_fn, proxy=proxy)
    return bot.register()


__all__ = [
    "ChatGPTRegister",
    "OpenAIAuthClient",
    "register_account",
    "random_name",
    "random_age",
    "auth_step_requires_password",
    "random_password",
    "birthdate_from_age",
]
