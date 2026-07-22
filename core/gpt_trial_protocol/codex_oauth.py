from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Callable

from chatgpt_register import OpenAIAuthClient
from codex_oauth import CODEX_CLIENT_ID, CODEX_REDIRECT_URI, generate_oauth_url

from .errors import NoPhoneAvailableError
from .sms import SmsActivation, SmsProvider


log = logging.getLogger("gpt_trial_protocol.codex_oauth")

AUTH_BASE_URL = "https://auth.openai.com"
TOKEN_URLS = (
    f"{AUTH_BASE_URL}/api/oauth/oauth2/token",
    f"{AUTH_BASE_URL}/oauth/token",
)
REDIRECT_STATUSES = {301, 302, 303, 307, 308}

PHONE_IN_USE_MARKERS = (
    "phone_number_in_use",
    "phone number is already",
    "already in use",
    "already associated",
)
PHONE_RETRYABLE_SEND_MARKERS = (
    "invalid_phone_number",
    "phone_number_invalid",
    "invalid phone number",
    "phone_number_not_available",
    "phone number not available",
    "unsupported phone",
    "phone number is not supported",
)
PHONE_OTP_INVALID_MARKERS = (
    "phone_otp_invalid",
    "invalid_phone_otp",
    "wrong_phone_otp",
    "incorrect code",
    "invalid otp",
    "code_expired",
)


class CodexOAuthProtocolError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.message = message


@dataclass
class CodexOAuthProtocolResult:
    access_token: str
    refresh_token: str
    id_token: str
    account_id: str
    email: str
    expires_in: int
    phone_bound: bool = False
    phone: str = ""
    activation_id: str = ""
    sms_provider: str = ""
    phone_attempts: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "account_id": self.account_id,
            "email": self.email,
            "expires_in": self.expires_in,
            "phone_bound": self.phone_bound,
            "phone": self.phone,
            "activation_id": self.activation_id,
            "sms_provider": self.sms_provider,
            "phone_attempts": self.phone_attempts,
            "events": self.events,
        }


def _default_email_code_fetcher(
    email: str,
    refresh_token: str,
    client_id: str,
    timeout: int,
    not_before: float = 0.0,
    exclude_codes: Iterable[str] | None = None,
) -> str | None:
    import config
    import email_provider

    method = str(getattr(config, "OTP_METHOD", "graph") or "graph").strip().lower()
    token = str(refresh_token or "").strip()
    resolved_client_id = str(client_id or email_provider.DEFAULT_CLIENT_ID)
    threshold = max(0.0, float(not_before or 0.0) - 10.0)
    excluded = {str(code).strip() for code in (exclude_codes or ()) if str(code).strip()}

    if token.startswith(("http://", "https://")) or method == "relay":
        return email_provider.fetch_otp_relay(
            token,
            timeout=timeout,
            excluded_otps=excluded,
            require_fresh=False,
        )
    if method == "imap":
        return email_provider.fetch_otp_imap(
            email,
            token,
            resolved_client_id,
            timeout=timeout,
            threshold_ts=threshold,
        )
    try:
        return email_provider.fetch_otp_graph(
            email,
            token,
            resolved_client_id,
            timeout=timeout,
            after_ts=threshold,
        )
    except email_provider.GraphScopeMissingError:
        return email_provider.fetch_otp_imap(
            email,
            token,
            resolved_client_id,
            timeout=timeout,
            threshold_ts=threshold,
        )


def _response_json(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _response_error(response: Any) -> str:
    status = int(getattr(response, "status_code", 0) or 0)
    text = str(getattr(response, "text", "") or "")
    data = _response_json(response)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
        if code or message:
            return f"HTTP {status} code={code or 'unknown'} message={message[:240]}"
    if error:
        return f"HTTP {status} error={str(error)[:240]}"
    code = str(data.get("code") or "").strip()
    message = str(data.get("message") or data.get("detail") or "").strip()
    if code or message:
        return f"HTTP {status} code={code or 'unknown'} message={message[:240]}"
    return f"HTTP {status} body={text[:300]}"


def _page_type(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    page = payload.get("page") or {}
    return str(page.get("type") or "").strip() if isinstance(page, dict) else ""


def _normalize_auth_url(value: str, base: str = AUTH_BASE_URL) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text.startswith("http://") or text.startswith("https://") else urllib.parse.urljoin(base, text)


def _continue_url(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    direct = (
        payload.get("continue_url")
        or payload.get("continueUrl")
        or payload.get("redirect_url")
        or payload.get("redirectUrl")
        or payload.get("url")
        or ""
    )
    if direct:
        return _normalize_auth_url(str(direct))
    page = payload.get("page") or {}
    if isinstance(page, dict):
        inner = page.get("payload") or {}
        if isinstance(inner, dict) and inner.get("url"):
            return _normalize_auth_url(str(inner["url"]))
    return ""


def _next_url(payload: dict[str, Any] | None) -> str:
    direct = _continue_url(payload)
    if direct:
        return direct
    page = _page_type(payload).lower()
    page_paths = {
        "login_password": "/log-in/password",
        "create_account_password": "/create-account/password",
        "email_otp_send": "/api/accounts/email-otp/send",
        "email_otp_verification": "/email-verification",
        "add_phone": "/add-phone",
        "phone_otp_select_channel": "/phone-otp/select-channel",
        "phone_otp_verification": "/phone-verification",
        "oauth_consent": "/sign-in-with-chatgpt/codex/consent",
    }
    path = page_paths.get(page, "")
    return _normalize_auth_url(path) if path else ""


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(segment).decode("utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) <= 4:
        return "****"
    return f"+***{digits[-4:]}"


class CodexOAuthProtocolFlow:
    """Pure HTTP Codex OAuth, including optional SMS add-phone verification."""

    def __init__(
        self,
        *,
        email: str,
        password: str,
        outlook_refresh_token: str,
        outlook_client_id: str = "",
        sms_provider: SmsProvider | None = None,
        proxy: str = "",
        impersonate: str = "firefox144",
        email_otp_timeout: int = 180,
        sms_otp_timeout: int = 30,
        sms_max_attempts: int = 3,
        sms_max_otp_retries: int = 2,
        log_fn: Callable[[str], Any] | None = None,
        on_event: Callable[[str, dict[str, Any]], Any] | None = None,
        auth_factory: Callable[[], Any] | None = None,
        email_code_fetcher: Callable[[str, str, str, int], str | None] | None = None,
    ) -> None:
        self.email = str(email or "").strip()
        self.password = str(password or "")
        self.outlook_refresh_token = str(outlook_refresh_token or "").strip()
        self.outlook_client_id = str(outlook_client_id or "").strip()
        self.sms_provider = sms_provider
        self.proxy = str(proxy or "").strip()
        self.impersonate = impersonate or "firefox144"
        self.email_otp_timeout = max(30, int(email_otp_timeout or 180))
        self.sms_otp_timeout = max(30, int(sms_otp_timeout or 30))
        self.sms_max_attempts = max(1, int(sms_max_attempts or 1))
        self.sms_max_otp_retries = max(0, int(sms_max_otp_retries or 0))
        self.log_fn = log_fn or log.info
        self.on_event = on_event
        self.auth_factory = auth_factory or (
            lambda: OpenAIAuthClient(impersonate=self.impersonate, proxy=self.proxy)
        )
        self.email_code_fetcher = email_code_fetcher or _default_email_code_fetcher
        self.events: list[dict[str, Any]] = []
        self._email_otp_requested_at = 0.0
        self._used_email_codes: set[str] = set()
        self._phone_result: dict[str, Any] = {}

    def _say(self, message: str) -> None:
        try:
            self.log_fn(message)
        except Exception:
            log.info(message)

    def _emit(self, event: str, **payload: Any) -> None:
        record = {"event": event, **payload}
        self.events.append(record)
        if self.on_event:
            try:
                self.on_event(event, payload)
            except Exception as exc:
                log.debug("Codex OAuth event callback failed: %s", exc)

    async def run(self) -> CodexOAuthProtocolResult:
        if not self.email:
            raise CodexOAuthProtocolError("input", "email is required")
        if not self.password and not self.outlook_refresh_token:
            raise CodexOAuthProtocolError("input", "password or Outlook refresh_token is required")

        auth = self.auth_factory()
        try:
            await auth.share_session_with_sentinel()
            session = await auth._get_session()
            oauth = generate_oauth_url(login_hint=self.email)
            oauth_url = self._with_query(oauth["auth_url"], prompt="login")
            expected_state = str(oauth["state"])
            self._emit("oauth_started", email=self.email)
            self._say("[codex-protocol] 启动 Codex OAuth")
            current_url, _ = await self._navigate(session, oauth_url)

            for _step in range(36):
                callback = self._parse_callback(current_url) if self._is_callback(current_url) else {}
                if callback.get("code"):
                    if callback.get("state") != expected_state:
                        raise CodexOAuthProtocolError("callback", "OAuth state mismatch")
                    tokens = await self._exchange_code(
                        session,
                        code=str(callback["code"]),
                        code_verifier=str(oauth["code_verifier"]),
                    )
                    refresh_token = str(tokens.get("refresh_token") or "")
                    if not refresh_token:
                        raise CodexOAuthProtocolError("token_exchange", "response missing refresh_token")
                    access_token = str(tokens.get("access_token") or "")
                    id_token = str(tokens.get("id_token") or "")
                    claims = _decode_jwt_payload(id_token) or _decode_jwt_payload(access_token)
                    auth_claims = claims.get("https://api.openai.com/auth") or {}
                    account_id = str(
                        auth_claims.get("chatgpt_account_id")
                        or claims.get("chatgpt_account_id")
                        or ""
                    )
                    self._emit("rt_ready", account_id=account_id)
                    self._say("[codex-protocol] OAuth token exchange 完成，已获取 RT")
                    return CodexOAuthProtocolResult(
                        access_token=access_token,
                        refresh_token=refresh_token,
                        id_token=id_token,
                        account_id=account_id,
                        email=str(claims.get("email") or self.email),
                        expires_in=int(tokens.get("expires_in") or 0),
                        phone_bound=bool(self._phone_result),
                        phone=str(self._phone_result.get("phone") or ""),
                        activation_id=str(self._phone_result.get("activation_id") or ""),
                        sms_provider=str(self._phone_result.get("provider") or ""),
                        phone_attempts=int(self._phone_result.get("attempts") or 0),
                        events=list(self.events),
                    )
                if callback.get("error"):
                    raise CodexOAuthProtocolError(
                        "callback",
                        f"{callback.get('error')}: {callback.get('error_description') or ''}",
                    )

                parsed = urllib.parse.urlparse(current_url)
                path = parsed.path.rstrip("/") or "/"

                if path in {"/log-in", "/choose-an-account", "/choose-account"}:
                    payload = await self._authorize_continue(auth, session)
                    current_url = _next_url(payload)
                    if not current_url:
                        raise CodexOAuthProtocolError("authorize_continue", f"missing next step: {payload}")
                    continue

                if path in {"/log-in/password", "/create-account/password"}:
                    payload = await self._password_or_otp(auth, session, current_url)
                    current_url = _next_url(payload) or _normalize_auth_url("/email-verification")
                    continue

                if path == "/api/accounts/email-otp/send":
                    payload = await self._send_email_otp(session, current_url)
                    current_url = _next_url(payload) or _normalize_auth_url("/email-verification")
                    continue

                if path == "/email-verification":
                    payload = await self._validate_email_otp(session)
                    current_url = _next_url(payload)
                    if not current_url:
                        raise CodexOAuthProtocolError("email_otp", f"missing continue_url: {payload}")
                    continue

                if path == "/add-phone":
                    current_url, self._phone_result = await self._bind_add_phone(session, auth)
                    continue

                if path in {"/phone-otp/select-channel", "/phone-verification"}:
                    # A previous interrupted bind can leave the account on a phone
                    # OTP page, but a new process no longer owns that old SMS lease.
                    # Replace the pending number with a newly leased one instead of
                    # sending another code to a number this run cannot read.
                    self._emit("pending_phone_replaced", page=path)
                    self._say("[codex-protocol] 检测到历史手机号验证状态，重新取号覆盖")
                    current_url, self._phone_result = await self._bind_add_phone(session, auth)
                    continue

                if "/sign-in-with-chatgpt/" in path or "/consent" in path or "/workspace" in path:
                    current_url = await self._select_workspace_or_org(session, auth, current_url)
                    continue

                next_url, response = await self._navigate(session, current_url)
                if next_url != current_url:
                    current_url = next_url
                    continue
                payload = _response_json(response) if response is not None else {}
                candidate = _next_url(payload)
                if candidate and candidate != current_url:
                    current_url = candidate
                    continue
                raise CodexOAuthProtocolError(
                    "oauth_navigation",
                    f"unhandled state url={current_url} status={getattr(response, 'status_code', 0)}",
                )

            raise CodexOAuthProtocolError("oauth_navigation", "state machine exceeded 36 steps")
        except BaseException as exc:
            if self._phone_result:
                try:
                    setattr(exc, "phone_result", dict(self._phone_result))
                    setattr(exc, "protocol_events", list(self.events))
                except Exception:
                    pass
            raise
        finally:
            await auth.close()

    @staticmethod
    def _with_query(url: str, **updates: str) -> str:
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        params.update({key: value for key, value in updates.items() if value})
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(params)))

    @staticmethod
    def _parse_callback(url: str) -> dict[str, str]:
        parsed = urllib.parse.urlparse(str(url or ""))
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        return {
            "code": str((query.get("code") or [""])[0] or ""),
            "state": str((query.get("state") or [""])[0] or ""),
            "error": str((query.get("error") or [""])[0] or ""),
            "error_description": str((query.get("error_description") or [""])[0] or ""),
        }

    @staticmethod
    def _is_callback(url: str) -> bool:
        target = urllib.parse.urlparse(str(url or ""))
        expected = urllib.parse.urlparse(CODEX_REDIRECT_URI)
        return (
            target.scheme,
            target.hostname,
            target.port,
            target.path.rstrip("/"),
        ) == (
            expected.scheme,
            expected.hostname,
            expected.port,
            expected.path.rstrip("/"),
        )

    async def _navigate(self, session: Any, start_url: str) -> tuple[str, Any | None]:
        current = _normalize_auth_url(start_url)
        referer = "https://chatgpt.com/"
        for _hop in range(16):
            if self._is_callback(current):
                return current, None
            response = await session.get(
                current,
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "referer": referer,
                },
                allow_redirects=False,
            )
            location = str(response.headers.get("location") or "").strip()
            if int(response.status_code) in REDIRECT_STATUSES and location:
                next_url = urllib.parse.urljoin(current, location)
                if self._is_callback(next_url):
                    return next_url, response
                referer, current = current, next_url
                continue
            return str(getattr(response, "url", "") or current), response
        raise CodexOAuthProtocolError("oauth_navigation", "redirect chain exceeded 16 hops")

    async def _authorize_continue(self, auth: Any, session: Any) -> dict[str, Any]:
        device_id = self._cookie_value(session, "oai-did")
        if device_id:
            auth.device_id = device_id
        referer = _normalize_auth_url("/log-in")
        headers = await auth._add_sentinel_headers(
            auth._common_headers(referer=referer),
            "authorize_continue",
            referer,
            log_fn=self._say,
        )
        self._email_otp_requested_at = time.time()
        response = await session.post(
            _normalize_auth_url("/api/accounts/authorize/continue"),
            headers=headers,
            json={
                "username": {"kind": "email", "value": self.email},
                "screen_hint": "login",
            },
        )
        if response.status_code != 200:
            raise CodexOAuthProtocolError("authorize_continue", _response_error(response))
        payload = _response_json(response)
        self._emit("login_identified", page_type=_page_type(payload))
        return payload

    async def _password_or_otp(self, auth: Any, session: Any, referer: str) -> dict[str, Any]:
        if self.password:
            try:
                headers = await auth._add_sentinel_headers(
                    auth._common_headers(referer=referer),
                    "login_password",
                    referer,
                    force_refresh=True,
                    log_fn=self._say,
                )
                response = await session.post(
                    _normalize_auth_url("/api/accounts/password/verify"),
                    headers=headers,
                    json={"password": self.password},
                )
                if response.status_code == 200:
                    payload = _response_json(response)
                    self._emit("password_validated", page_type=_page_type(payload))
                    return payload
                self._say(f"[codex-protocol] 密码登录未通过，切换邮箱 OTP: {_response_error(response)}")
            except Exception as exc:
                self._say(f"[codex-protocol] 密码登录异常，切换邮箱 OTP: {exc}")

        response = await session.post(
            _normalize_auth_url("/api/accounts/passwordless/send-otp"),
            headers=auth._common_headers(referer=referer),
            json={},
        )
        if response.status_code != 200:
            response = await session.post(
                _normalize_auth_url("/api/accounts/email-otp/resend"),
                headers=auth._common_headers(referer=_normalize_auth_url("/email-verification")),
                json={},
            )
        if response.status_code != 200:
            raise CodexOAuthProtocolError("email_otp_send", _response_error(response))
        self._email_otp_requested_at = time.time()
        self._emit("email_otp_sent")
        payload = _response_json(response)
        return payload or {"continue_url": "/email-verification"}

    async def _send_email_otp(self, session: Any, referer: str) -> dict[str, Any]:
        response = await session.get(
            _normalize_auth_url("/api/accounts/email-otp/send"),
            headers={"accept": "application/json", "referer": referer},
        )
        if response.status_code != 200:
            raise CodexOAuthProtocolError("email_otp_send", _response_error(response))
        self._email_otp_requested_at = time.time()
        self._emit("email_otp_sent")
        payload = _response_json(response)
        return payload or {"continue_url": "/email-verification"}

    async def _validate_email_otp(self, session: Any) -> dict[str, Any]:
        if not self.outlook_refresh_token:
            raise CodexOAuthProtocolError("email_otp", "Outlook refresh_token is required")
        last_error = ""
        for attempt in range(2):
            if self.email_code_fetcher is _default_email_code_fetcher:
                code = await asyncio.to_thread(
                    self.email_code_fetcher,
                    self.email,
                    self.outlook_refresh_token,
                    self.outlook_client_id,
                    self.email_otp_timeout,
                    self._email_otp_requested_at,
                    set(self._used_email_codes),
                )
            else:
                code = await asyncio.to_thread(
                    self.email_code_fetcher,
                    self.email,
                    self.outlook_refresh_token,
                    self.outlook_client_id,
                    self.email_otp_timeout,
                )
            code = str(code or "").strip()
            if not code:
                raise CodexOAuthProtocolError("email_otp", "email OTP timeout")
            if code in self._used_email_codes:
                last_error = "email provider returned a previously rejected OTP"
                await asyncio.sleep(3)
                continue
            response = await session.post(
                _normalize_auth_url("/api/accounts/email-otp/validate"),
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
                    "origin": AUTH_BASE_URL,
                    "referer": _normalize_auth_url("/email-verification"),
                },
                json={"code": code},
            )
            if response.status_code == 200:
                self._emit("email_otp_validated")
                return _response_json(response)
            self._used_email_codes.add(code)
            last_error = _response_error(response)
            if attempt == 0:
                resend_response = await session.post(
                    _normalize_auth_url("/api/accounts/email-otp/resend"),
                    headers={"referer": _normalize_auth_url("/email-verification")},
                    json={},
                )
                if resend_response.status_code != 200:
                    raise CodexOAuthProtocolError("email_otp_resend", _response_error(resend_response))
                self._email_otp_requested_at = time.time()
        raise CodexOAuthProtocolError("email_otp", last_error or "email OTP rejected")

    async def _bind_add_phone(self, session: Any, auth: Any) -> tuple[str, dict[str, Any]]:
        if self.sms_provider is None:
            raise CodexOAuthProtocolError("add_phone", "SMS provider is not configured")
        provider = self.sms_provider
        last_error = ""
        phone_requests = 0

        for phone_attempt in range(1, self.sms_max_attempts + 1):
            phone_requests = phone_attempt
            activation = await asyncio.to_thread(provider.request_phone)
            if activation is None or not activation.phone:
                last_error = "SMS provider returned no phone"
                self._emit(
                    "provider_no_phone",
                    attempt=phone_attempt,
                    max_attempts=self.sms_max_attempts,
                )
                break
            phone = activation.phone
            terminal_action = ""
            self._emit(
                "phone_acquired",
                attempt=phone_attempt,
                phone=_mask_phone(phone),
                provider=activation.provider or provider.name,
            )
            self._say(
                f"[codex-protocol] 平台取号 {_mask_phone(phone)} "
                f"provider={activation.provider or provider.name} attempt={phone_attempt}/{self.sms_max_attempts}"
            )
            try:
                send_response = await session.post(
                    _normalize_auth_url("/api/accounts/add-phone/send"),
                    headers=self._phone_headers(auth, "/add-phone"),
                    json={"phone_number": phone},
                )
                if send_response.status_code != 200:
                    error_text = _response_error(send_response)
                    if any(marker in error_text.lower() for marker in PHONE_IN_USE_MARKERS):
                        last_error = "phone_number_in_use"
                        self._emit("phone_in_use", attempt=phone_attempt, phone=_mask_phone(phone))
                        await self._provider_call(provider, "cancel", activation, attempts=2)
                        terminal_action = "cancelled"
                        continue
                    if any(marker in error_text.lower() for marker in PHONE_RETRYABLE_SEND_MARKERS):
                        last_error = error_text
                        self._emit("phone_rejected", attempt=phone_attempt, phone=_mask_phone(phone))
                        await self._provider_call(provider, "cancel", activation, attempts=2)
                        terminal_action = "cancelled"
                        continue
                    raise CodexOAuthProtocolError("add_phone_send", error_text)

                send_payload = _response_json(send_response)
                send_next_url = _next_url(send_payload)
                if urllib.parse.urlparse(send_next_url).path.rstrip("/") == "/phone-otp/select-channel":
                    await self._select_sms_channel(session, auth)

                if not await self._provider_call(provider, "mark_sent", activation, attempts=2):
                    raise CodexOAuthProtocolError(
                        "sms_provider",
                        "provider did not accept setStatus(1) after OpenAI sent the OTP",
                    )
                self._emit("sms_sent", attempt=phone_attempt, phone=_mask_phone(phone))
                used_codes: set[str] = set()
                validate_payload: dict[str, Any] | None = None
                phone_wait_started = time.monotonic()
                phone_wait_deadline = phone_wait_started + self.sms_otp_timeout

                for otp_attempt in range(self.sms_max_otp_retries + 1):
                    remaining = phone_wait_deadline - time.monotonic()
                    if remaining <= 0:
                        last_error = "sms_number_timeout"
                        self._emit(
                            "phone_timeout",
                            attempt=phone_attempt,
                            otp_attempt=otp_attempt + 1,
                            limit_seconds=self.sms_otp_timeout,
                            phone=_mask_phone(phone),
                        )
                        break
                    code = await asyncio.to_thread(
                        self._wait_sms_code,
                        provider,
                        activation,
                        used_codes,
                        remaining,
                    )
                    if not code:
                        remaining = phone_wait_deadline - time.monotonic()
                        if remaining <= 0:
                            last_error = "sms_number_timeout"
                            self._emit(
                                "phone_timeout",
                                attempt=phone_attempt,
                                otp_attempt=otp_attempt + 1,
                                limit_seconds=self.sms_otp_timeout,
                                phone=_mask_phone(phone),
                            )
                            self._say(
                                f"[codex-protocol] 单号等待超过 {self.sms_otp_timeout}s，取消并换号"
                            )
                            break
                        last_error = "sms_otp_timeout"
                        self._emit(
                            "otp_timeout",
                            attempt=phone_attempt,
                            otp_attempt=otp_attempt + 1,
                            phone=_mask_phone(phone),
                        )
                        if otp_attempt < self.sms_max_otp_retries:
                            await self._resend_phone_otp(session, auth, provider, activation)
                            continue
                        break

                    used_codes.add(code)
                    self._emit(
                        "otp_received",
                        attempt=phone_attempt,
                        otp_attempt=otp_attempt + 1,
                        code_length=len(code),
                    )
                    validate_response = await session.post(
                        _normalize_auth_url("/api/accounts/phone-otp/validate"),
                        headers=self._phone_headers(auth, "/phone-verification"),
                        json={"code": code},
                    )
                    if validate_response.status_code == 200:
                        validate_payload = _response_json(validate_response)
                        break
                    error_text = _response_error(validate_response)
                    last_error = error_text
                    if any(marker in error_text.lower() for marker in PHONE_OTP_INVALID_MARKERS):
                        self._emit(
                            "otp_invalid",
                            attempt=phone_attempt,
                            otp_attempt=otp_attempt + 1,
                        )
                        if (
                            otp_attempt < self.sms_max_otp_retries
                            and time.monotonic() < phone_wait_deadline
                        ):
                            await self._resend_phone_otp(session, auth, provider, activation)
                            continue
                        if time.monotonic() >= phone_wait_deadline:
                            last_error = "sms_number_timeout"
                            self._emit(
                                "phone_timeout",
                                attempt=phone_attempt,
                                otp_attempt=otp_attempt + 1,
                                limit_seconds=self.sms_otp_timeout,
                                phone=_mask_phone(phone),
                            )
                        break
                    raise CodexOAuthProtocolError("phone_otp_validate", error_text)

                if validate_payload is None:
                    await self._provider_call(provider, "cancel", activation, attempts=2)
                    terminal_action = "cancelled"
                    continue

                completed = await self._provider_call(provider, "complete", activation, attempts=2)
                terminal_action = "completed"
                if not completed:
                    self._emit("provider_complete_failed", activation_id=activation.activation_id or "")
                next_url = _next_url(validate_payload)
                if not next_url:
                    next_url = _normalize_auth_url("/sign-in-with-chatgpt/codex/consent")
                self._emit("phone_bound", phone=_mask_phone(phone), attempts=phone_attempt)
                self._say(f"[codex-protocol] 手机验证完成 {_mask_phone(phone)}")
                return next_url, {
                    "phone": phone,
                    "activation_id": activation.activation_id or "",
                    "provider": activation.provider or provider.name,
                    "attempts": phone_attempt,
                }
            finally:
                if not terminal_action:
                    await self._provider_call(provider, "cancel", activation, attempts=2)

        raise NoPhoneAvailableError(
            f"Codex add-phone stopped after {phone_requests} phone request attempt(s) "
            f"(configured max {self.sms_max_attempts}); "
            f"last_error={last_error or 'unknown'}"
        )

    def _wait_sms_code(
        self,
        provider: SmsProvider,
        activation: SmsActivation,
        used_codes: set[str],
        timeout: float | None = None,
    ) -> str | None:
        wait_timeout = max(
            1.0,
            float(self.sms_otp_timeout if timeout is None else timeout),
        )
        try:
            return provider.wait_for_otp(
                activation,
                timeout=wait_timeout,
                exclude_codes=set(used_codes),
            )
        except TypeError:
            code = provider.wait_for_otp(activation, timeout=wait_timeout)
            return None if code in used_codes else code

    async def _provider_call(
        self,
        provider: SmsProvider,
        method_name: str,
        activation: SmsActivation,
        *,
        attempts: int = 1,
    ) -> bool:
        method = getattr(provider, method_name, None)
        if not callable(method):
            return True
        last_error = ""
        for _attempt in range(max(1, attempts)):
            try:
                if bool(await asyncio.to_thread(method, activation)):
                    return True
                last_error = "provider returned false"
            except Exception as exc:
                last_error = str(exc)[:160]
        self._emit(
            f"provider_{method_name}_failed",
            activation_id=activation.activation_id or "",
            error=last_error,
        )
        return False

    async def _resend_phone_otp(
        self,
        session: Any,
        auth: Any,
        provider: SmsProvider,
        activation: SmsActivation,
    ) -> None:
        response = await session.post(
            _normalize_auth_url("/api/accounts/phone-otp/resend"),
            headers=self._phone_headers(auth, "/phone-verification"),
            json={},
        )
        if response.status_code != 200:
            raise CodexOAuthProtocolError("phone_otp_resend", _response_error(response))
        if not await self._provider_call(provider, "request_resend", activation, attempts=2):
            raise CodexOAuthProtocolError(
                "sms_provider",
                "provider did not accept setStatus(3) after OpenAI resent the OTP",
            )
        self._emit("otp_resent", activation_id=activation.activation_id or "")

    @staticmethod
    def _phone_headers(auth: Any, referer_path: str) -> dict[str, str]:
        headers = auth._common_headers(referer=_normalize_auth_url(referer_path))
        headers.update({"accept": "application/json", "origin": AUTH_BASE_URL})
        if getattr(auth, "device_id", ""):
            headers["oai-device-id"] = auth.device_id
        return headers

    async def _select_sms_channel(self, session: Any, auth: Any) -> dict[str, Any]:
        response = await session.post(
            _normalize_auth_url("/api/accounts/phone-otp/send"),
            headers=self._phone_headers(auth, "/phone-otp/select-channel"),
            json={"channel": "sms"},
        )
        if response.status_code != 200:
            raise CodexOAuthProtocolError("phone_channel", _response_error(response))
        self._emit("phone_channel_selected", channel="sms")
        return _response_json(response)

    @staticmethod
    def _cookie_value(session: Any, name: str) -> str:
        try:
            value = session.cookies.get(name, "")
            if value:
                return str(value)
        except Exception:
            pass
        try:
            for cookie in session.cookies.jar:
                if cookie.name == name:
                    return str(cookie.value)
        except Exception:
            pass
        return ""

    @classmethod
    def _workspace_records(cls, session: Any) -> list[dict[str, Any]]:
        cookie = cls._cookie_value(session, "oai-client-auth-session")
        if not cookie:
            return []
        for segment in cookie.split(".")[:2]:
            try:
                decoded = json.loads(
                    base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)).decode("utf-8")
                )
            except Exception:
                continue
            if isinstance(decoded, dict):
                workspaces = decoded.get("workspaces") or []
                if isinstance(workspaces, list) and workspaces:
                    return [item for item in workspaces if isinstance(item, dict)]
                workspace_id = str(decoded.get("workspace_id") or "")
                if workspace_id:
                    return [{"id": workspace_id}]
        return []

    async def _select_workspace_or_org(
        self,
        session: Any,
        auth: Any,
        consent_url: str,
    ) -> str:
        current, response = await self._navigate(session, consent_url)
        if self._is_callback(current):
            return current
        workspaces = self._workspace_records(session)
        if not workspaces and response is not None:
            text = str(getattr(response, "text", "") or "")
            import re

            match = re.search(r'"workspace(?:_id|Id|s)".{0,1200}?"id"\s*[:;,]\s*"([0-9a-fA-F-]{36})"', text)
            if match:
                workspaces = [{"id": match.group(1)}]
        if not workspaces:
            raise CodexOAuthProtocolError("workspace", "no workspace found in OAuth session")
        personal = next(
            (item for item in workspaces if str(item.get("kind") or "").lower() == "personal"),
            workspaces[0],
        )
        workspace_id = str(personal.get("id") or "").strip()
        if not workspace_id:
            raise CodexOAuthProtocolError("workspace", "workspace id is empty")
        headers = auth._common_headers(referer=consent_url)
        headers.update({"accept": "application/json", "origin": AUTH_BASE_URL})
        ws_response = await session.post(
            _normalize_auth_url("/api/accounts/workspace/select"),
            headers=headers,
            json={"workspace_id": workspace_id},
            allow_redirects=False,
        )
        if ws_response.status_code >= 400:
            raise CodexOAuthProtocolError("workspace", _response_error(ws_response))
        data = _response_json(ws_response)
        next_url = str(ws_response.headers.get("location") or "").strip() or _continue_url(data)
        orgs = ((data.get("data") or {}).get("orgs") or []) if isinstance(data, dict) else []
        if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict) and orgs[0].get("id"):
            org = orgs[0]
            body = {"org_id": str(org["id"])}
            projects = org.get("projects") or []
            if isinstance(projects, list) and projects and isinstance(projects[0], dict) and projects[0].get("id"):
                body["project_id"] = str(projects[0]["id"])
            org_response = await session.post(
                _normalize_auth_url("/api/accounts/organization/select"),
                headers=headers,
                json=body,
                allow_redirects=False,
            )
            if org_response.status_code >= 400:
                raise CodexOAuthProtocolError("organization", _response_error(org_response))
            org_data = _response_json(org_response)
            next_url = str(org_response.headers.get("location") or "").strip() or _continue_url(org_data) or next_url
        if not next_url:
            raise CodexOAuthProtocolError("workspace", "workspace selection returned no continue_url")
        self._emit("workspace_selected", workspace_id=workspace_id)
        next_url, _ = await self._navigate(session, urllib.parse.urljoin(consent_url, next_url))
        return next_url

    async def _exchange_code(
        self,
        session: Any,
        *,
        code: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        form = {
            "grant_type": "authorization_code",
            "client_id": CODEX_CLIENT_ID,
            "code": code,
            "redirect_uri": CODEX_REDIRECT_URI,
            "code_verifier": code_verifier,
        }
        last_error = ""
        for token_url in TOKEN_URLS:
            response = await session.post(
                token_url,
                headers={
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": AUTH_BASE_URL,
                    "referer": _normalize_auth_url("/sign-in-with-chatgpt/codex/consent"),
                },
                data=form,
            )
            if response.status_code == 200:
                return _response_json(response)
            last_error = f"{token_url}: {_response_error(response)}"
        raise CodexOAuthProtocolError("token_exchange", last_error or "all token endpoints failed")


def run_codex_oauth_protocol(**kwargs: Any) -> dict[str, Any]:
    """Synchronous entry point used by the Free batch runner."""

    flow = CodexOAuthProtocolFlow(**kwargs)

    async def _run() -> dict[str, Any]:
        return (await flow.run()).as_dict()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(_run())).result()


__all__ = [
    "CodexOAuthProtocolError",
    "CodexOAuthProtocolFlow",
    "CodexOAuthProtocolResult",
    "run_codex_oauth_protocol",
]
