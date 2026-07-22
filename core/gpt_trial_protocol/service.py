from __future__ import annotations

import json
import random
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .chatgpt import ChatGPTProtocolClient
from .email_code import CUSTOM_EMAIL_DOMAIN, DEFAULT_EMAIL_CODE_BASE_URL, EmailCodeClient, EmailCodeProvider
from .errors import NoPhoneAvailableError, PhoneOtpInvalidError, ProtocolResponseError
from .flows import ProtocolRegistrarFlow
from .http_client import ProtocolHttpClient
from .local_email_code import LocalEmailCodeProvider, can_use_local_for
from .models import AccountInput, BrowserProfile, CheckoutInput, ProtocolConfig
from .sentinel_http import SentinelHttpTokenProvider
from .sms import SmsProvider


@dataclass(frozen=True)
class EmailItem:
    email: str
    note_password: str | None = None
    client_id: str | None = None      # outlook 第 3 段（OAuth client_id）
    refresh_token: str | None = None  # outlook 第 4 段；icloud 行写 relay URL 也走这里


@dataclass(frozen=True)
class RunOptions:
    proxy: str | None = None
    login_existing: bool = False
    email_type: str = "auto"
    email_code_base_url: str | None = None
    email_code_provider: str = EmailCodeProvider.AUTO.value
    timeout: float = 30.0
    code_timeout: float = 90.0
    checkout_country: str = "US"
    checkout_currency: str = "USD"
    trace_dir: Path | None = None
    trace_sensitive: bool = False
    backend: str = "curl_cffi"
    birthdate: str = "2000-01-01"
    display_name_prefix: str = "Lu"
    session_output_dir: Path | None = None


@dataclass(frozen=True)
class FreeRunOptions:
    """Options for free (no-checkout) registration optionally followed by add-phone.

    ``sms_provider`` carries an already-built provider; ``run_one_free`` will
    skip the add-phone step when None. Other fields mirror :class:`RunOptions`.
    """
    proxy: str | None = None
    login_existing: bool = False
    email_type: str = "auto"
    email_code_base_url: str | None = None
    email_code_provider: str = EmailCodeProvider.AUTO.value
    timeout: float = 30.0
    code_timeout: float = 90.0
    trace_dir: Path | None = None
    trace_sensitive: bool = False
    backend: str = "curl_cffi"
    birthdate: str = "2000-01-01"
    display_name_prefix: str = "Lu"
    session_output_dir: Path | None = None
    sms_provider: SmsProvider | None = None
    sms_max_attempts: int = 3
    sms_otp_timeout: float = 30.0
    sms_max_otp_retries: int = 2


def parse_email_items(text: str) -> list[EmailItem]:
    normalized = text.replace(";", "\n")
    items: list[EmailItem] = []
    for raw in normalized.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        # outlook_accounts.txt 风格：email----password----client_id----refresh_token
        # icloud 风格：email----https://relay-url（refresh_token 字段写 URL）
        if "----" in line:
            parts = line.split("----")
            email = parts[0].strip()
            note = parts[1].strip() if len(parts) > 1 else ""
            client_id = parts[2].strip() if len(parts) > 2 else ""
            refresh_token = parts[3].strip() if len(parts) > 3 else ""
            # icloud：第二段直接是 URL，没有 password/client_id/refresh_token，把 URL 当 refresh_token
            if note.startswith(("http://", "https://")) and not refresh_token:
                refresh_token = note
                note = ""
            if email:
                items.append(EmailItem(
                    email=email,
                    note_password=note or None,
                    client_id=client_id or None,
                    refresh_token=refresh_token or None,
                ))
            continue
        if "|" in line:
            email, note = line.split("|", 1)
            items.append(EmailItem(email=email.strip(), note_password=note.strip() or None))
        else:
            items.append(EmailItem(email=line))
    return items


def generate_email_prefix(*, prefix: str = "lu") -> str:
    from datetime import datetime

    suffix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    return f"{prefix}{datetime.now().strftime('%H%M%S')}{suffix}"


def normalize_email_item(item: EmailItem, *, email_type: str = "auto") -> EmailItem:
    kind = email_type.lower().strip()
    email = item.email.strip()
    base_kwargs = dict(
        note_password=item.note_password,
        client_id=item.client_id,
        refresh_token=item.refresh_token,
    )
    if kind == "auto":
        if "@" not in email:
            raise ValueError(f"bare email name requires --email-type icloud or --email-type custom: {email}")
        return EmailItem(email=email, **base_kwargs)
    if kind == "icloud":
        if "@" not in email:
            email = f"{email}@icloud.com"
        return EmailItem(email=email, **base_kwargs)
    if kind == "custom":
        if "@" not in email:
            email = f"{email}@{CUSTOM_EMAIL_DOMAIN}"
        return EmailItem(email=email, **base_kwargs)
    raise ValueError(f"unknown email type: {email_type}")


def effective_email_code_provider(options: RunOptions, email: str) -> str:
    if options.email_code_provider != EmailCodeProvider.AUTO.value:
        return options.email_code_provider
    if options.email_type == "custom" or email.lower().endswith(f"@{CUSTOM_EMAIL_DOMAIN.lower()}"):
        return EmailCodeProvider.OPENAI_CODE_JSON.value
    if options.email_type == "icloud":
        return EmailCodeProvider.EXTRACT_JSON.value
    return EmailCodeProvider.AUTO.value


def random_display_name(prefix: str = "Lu") -> str:
    length = random.randint(5, 8)
    suffix = "".join(random.choice(string.ascii_letters) for _ in range(length))
    return f"{prefix}{suffix}"


def run_one(item: EmailItem, options: RunOptions) -> dict[str, Any]:
    item = normalize_email_item(item, email_type=options.email_type)
    checkout = CheckoutInput(country=options.checkout_country, currency=options.checkout_currency)
    trace_dir = options.trace_dir / sanitize_email(item.email) if options.trace_dir else None
    config = ProtocolConfig(
        timeout=options.timeout,
        code_receiver_base_url=options.email_code_base_url or DEFAULT_EMAIL_CODE_BASE_URL,
        trace_dir=trace_dir,
        profile=BrowserProfile(),
    )
    with ProtocolHttpClient(
        timeout=options.timeout,
        proxy=options.proxy,
        trace_dir=trace_dir,
        trace_name="chatgpt",
        trace_sensitive=options.trace_sensitive,
        backend=options.backend,
    ) as http, EmailCodeClient(
        base_url=options.email_code_base_url,
        provider=effective_email_code_provider(options, item.email),
        timeout=20.0,
    ) as code_client:
        chatgpt = ChatGPTProtocolClient(config, http)
        flow = ProtocolRegistrarFlow(chatgpt)
        if options.login_existing:
            login = flow.login_existing_account(item.email, code_provider=code_client, timeout=options.code_timeout)
            session = login.session
            access_token = session.access_token
            registration_info: dict[str, Any] = {"mode": "login", "validation": login.validation_result}
        else:
            account = AccountInput(
                email=item.email,
                display_name=random_display_name(options.display_name_prefix),
                birthdate=options.birthdate,
            )
            sentinel = SentinelHttpTokenProvider(config=config, proxy=options.proxy)
            registration = flow.register_account_with_risk_provider(
                account,
                risk_provider=sentinel,
                code_provider=code_client,
                timeout=options.code_timeout,
            )
            session = registration.session
            access_token = session.access_token
            registration_info = {"mode": "register", "createAccount": registration.create_account_result}

        if not access_token:
            raise RuntimeError("ChatGPT session did not contain accessToken")

        session_output_path = write_session_output(item.email, session, options.session_output_dir)

        link = flow.checkout_link(access_token, checkout)
        return {
            "ok": True,
            "email": item.email,
            "stage": "checkout_link",
            "registration": registration_info,
            "checkoutUrl": link.url,
            "checkoutSessionId": link.checkout_session_id,
            "processorEntity": link.processor_entity,
            "rawCheckout": link.raw,
            "sessionOutputPath": session_output_path,
        }


def run_one_free(item: EmailItem, options: FreeRunOptions) -> dict[str, Any]:
    """Register a free ChatGPT account via HTTP protocol, optionally bind phone.

    The flow:
      1. signup OTP (or login OTP for ``login_existing``) → access_token
      2. (optional) leave session intact, bind a phone via SmsProvider
      3. write artefacts, return summary dict.

    Unlike :func:`run_one`, no Plus / checkout call is made.
    """
    item = normalize_email_item(item, email_type=options.email_type)
    trace_dir = options.trace_dir / sanitize_email(item.email) if options.trace_dir else None
    config = ProtocolConfig(
        timeout=options.timeout,
        code_receiver_base_url=options.email_code_base_url or DEFAULT_EMAIL_CODE_BASE_URL,
        trace_dir=trace_dir,
        profile=BrowserProfile(),
    )

    phone_events: list[dict[str, Any]] = []

    def _phone_logger(event: str, payload: dict[str, Any]) -> None:
        phone_events.append({"event": event, **payload})
        # 同步写到 stdout，让 webui 终端能看到
        print(json.dumps({"event": "phone_progress", "stage": event, **payload},
                         ensure_ascii=False, separators=(",", ":")), flush=True)

    register_events: list[dict[str, Any]] = []

    def _register_logger(event: str, payload: dict[str, Any]) -> None:
        register_events.append({"event": event, **payload})
        print(json.dumps({"event": "register_progress", "stage": event, **payload},
                         ensure_ascii=False, separators=(",", ":")), flush=True)

    with ProtocolHttpClient(
        timeout=options.timeout,
        proxy=options.proxy,
        trace_dir=trace_dir,
        trace_name="chatgpt",
        trace_sensitive=options.trace_sensitive,
        backend=options.backend,
    ) as http, EmailCodeClient(
        base_url=options.email_code_base_url,
        provider=effective_email_code_provider_free(options, item.email),
        timeout=20.0,
    ) as code_client:
        chatgpt = ChatGPTProtocolClient(config, http)
        flow = ProtocolRegistrarFlow(chatgpt)

        # 选邮件验证码 provider：
        #   - item 带 refresh_token / relay URL → 走本地 email_provider（Outlook Graph
        #     或 iCloud relay），不需要外部 base URL
        #   - 否则走远端 EmailCodeClient（需要 GPT_TRIAL_EMAIL_CODE_BASE_URL）
        if can_use_local_for(item):
            code_provider: Any = LocalEmailCodeProvider(
                refresh_token=item.refresh_token or "",
                client_id=item.client_id or "",
            )
        else:
            code_provider = code_client

        if options.login_existing:
            login = flow.login_existing_account(item.email, code_provider=code_provider, timeout=options.code_timeout, on_event=_register_logger)
            session = login.session
            access_token = session.access_token
            registration_info: dict[str, Any] = {"mode": "login", "validation": login.validation_result}
        else:
            account = AccountInput(
                email=item.email,
                display_name=random_display_name(options.display_name_prefix),
                birthdate=options.birthdate,
            )
            sentinel = SentinelHttpTokenProvider(config=config, proxy=options.proxy)
            registration = flow.register_account_with_risk_provider(
                account,
                risk_provider=sentinel,
                code_provider=code_provider,
                timeout=options.code_timeout,
                on_event=_register_logger,
            )
            session = registration.session
            access_token = session.access_token
            registration_info = {"mode": "register", "createAccount": registration.create_account_result}

        if not access_token:
            raise RuntimeError("ChatGPT session did not contain accessToken")

        session_output_path = write_session_output(item.email, session, options.session_output_dir)

        result: dict[str, Any] = {
            "ok": True,
            "email": item.email,
            "stage": "registered",
            "registration": registration_info,
            "accessToken": access_token,
            "sessionOutputPath": session_output_path,
            "phone": None,
        }

        if options.sms_provider is None:
            return result

        try:
            bind = flow.bind_phone_with_provider(
                options.sms_provider,
                max_attempts=options.sms_max_attempts,
                otp_timeout=options.sms_otp_timeout,
                max_otp_retries=options.sms_max_otp_retries,
                on_event=_phone_logger,
            )
        except (NoPhoneAvailableError, PhoneOtpInvalidError, ProtocolResponseError) as exc:
            result["stage"] = "phone_failed"
            result["ok"] = False
            result["phoneError"] = str(exc)
            result["phoneEvents"] = phone_events
            return result

        result["stage"] = "phone_bound"
        result["phone"] = {
            "number": bind.phone_number,
            "activationId": bind.activation_id,
            "provider": bind.provider,
            "continueUrl": bind.continue_url,
            "pageType": bind.page_type,
            "attempts": bind.attempts,
        }
        result["phoneEvents"] = phone_events
        return result


def effective_email_code_provider_free(options: FreeRunOptions, email: str) -> str:
    if options.email_code_provider != EmailCodeProvider.AUTO.value:
        return options.email_code_provider
    if options.email_type == "custom" or email.lower().endswith(f"@{CUSTOM_EMAIL_DOMAIN.lower()}"):
        return EmailCodeProvider.OPENAI_CODE_JSON.value
    if options.email_type == "icloud":
        return EmailCodeProvider.EXTRACT_JSON.value
    return EmailCodeProvider.AUTO.value


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def sanitize_email(email: str) -> str:
    """Convert an email into a safe filesystem-friendly slug.

    Caps length to 80 chars and appends a short hash suffix when truncated, so
    different overlong inputs don't collide on disk and we never trigger
    ``[Errno 63] File name too long`` on macOS/APFS (255-byte path limit).
    """
    raw = email.replace("@", "-at-").replace("/", "_").replace("\\", "_")
    raw = "".join(ch if (ch.isalnum() or ch in "-_.+=") else "_" for ch in raw)
    if len(raw) <= 80:
        return raw
    import hashlib
    digest = hashlib.sha1(email.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{raw[:70]}_{digest}"


def write_session_output(email: str, session: object, output_dir: Path | None) -> str:
    if not output_dir:
        return ""
    raw = getattr(session, "raw", None)
    if not isinstance(raw, dict):
        return ""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{sanitize_email(email)}.json"
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)
