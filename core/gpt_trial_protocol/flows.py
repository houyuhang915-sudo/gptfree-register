from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from .chatgpt import ChatGPTProtocolClient
from .email_code import EmailCodeResult
from .errors import (
    NoPhoneAvailableError,
    PhoneNumberInUseError,
    PhoneOtpInvalidError,
    ProtocolResponseError,
)
from .models import AccountInput, CheckoutInput, CheckoutLink, PhoneBindResult, SessionInfo
from .risk_tokens import RiskTokenProvider
from .sms import SmsActivation, SmsProvider


class FreshCodeProvider(Protocol):
    def wait_for_fresh_code(self, email: str, *, not_before: datetime | None, timeout: float = ..., **kwargs: object) -> EmailCodeResult:
        ...


@dataclass(frozen=True)
class RegistrationResult:
    email: str
    session: SessionInfo
    create_account_result: dict


@dataclass(frozen=True)
class LoginResult:
    email: str
    session: SessionInfo
    validation_result: dict


def validation_continue_is_session_callback(validation: dict) -> bool:
    url = (
        validation.get("continue_url")
        or validation.get("continueUrl")
        or validation.get("redirect_url")
        or validation.get("redirectUrl")
        or validation.get("url")
        or ""
    )
    return "/api/auth/callback/" in str(url)


def protocol_error_is_invalid_auth_step(exc: ProtocolResponseError) -> bool:
    body = (exc.body or "").lower()
    return exc.status_code == 400 and ("invalid_auth_step" in body or "invalid authorization step" in body)


def _response_url(response: object) -> str:
    return str(getattr(response, "url", "") or "")


def auth_response_requires_passwordless_otp(response: object) -> bool:
    path = urlparse(_response_url(response)).path.rstrip("/")
    return path in {"/create-account/password", "/log-in/password", "/login/password"}


class ProtocolRegistrarFlow:
    def __init__(self, chatgpt: ChatGPTProtocolClient) -> None:
        self.chatgpt = chatgpt

    def _open_auth_and_trigger_passwordless_if_needed(self, auth: object) -> object:
        response = self.chatgpt.open_auth_url(auth)  # type: ignore[arg-type]
        if auth_response_requires_passwordless_otp(response):
            sent = self.chatgpt.send_passwordless_otp(referer=_response_url(response))
            self.chatgpt.open_continue_url(sent, fallback=self.chatgpt.auth_url("/email-verification"))
        return response

    def register_account_with_risk_provider(
        self,
        account: AccountInput,
        *,
        risk_provider: RiskTokenProvider,
        code_provider: FreshCodeProvider,
        timeout: float = 90.0,
        on_event: "Callable[[str, dict], None] | None" = None,
    ) -> RegistrationResult:
        emit = on_event or (lambda _name, _payload: None)
        not_before = datetime.now(timezone.utc)
        emit("auth_csrf_pending", {"email": account.email})
        auth = self.chatgpt.start_openai_signin(account.email, mode="register")
        emit("signin_redirect_pending", {"email": account.email})
        self._open_auth_and_trigger_passwordless_if_needed(auth)
        emit("waiting_otp", {"email": account.email, "timeout": timeout})
        code = code_provider.wait_for_fresh_code(account.email, not_before=not_before, timeout=timeout).latest_code
        if not code:
            raise TimeoutError(f"fresh code not found for {account.email}")
        emit("otp_received", {"email": account.email, "code_length": len(code)})
        validation = self.chatgpt.validate_email_otp(code)
        emit("otp_validated", {"email": account.email})
        self.chatgpt.open_continue_url(validation, fallback=self.chatgpt.auth_url("/about-you"))
        if validation_continue_is_session_callback(validation):
            session = self.chatgpt.get_session()
            if session.access_token:
                emit("session_ready", {"email": account.email, "via": "otp_continue"})
                return RegistrationResult(
                    email=account.email,
                    session=session,
                    create_account_result={"skipped": True, "reason": "otp_continue_created_session"},
                )
        emit("sentinel_pending", {"email": account.email})
        bundle = risk_provider.get_openai_sentinel(purpose="register")
        emit("create_account_pending", {"email": account.email})
        try:
            created = self.chatgpt.create_account(account, sentinel=bundle.sentinel)
        except ProtocolResponseError as exc:
            if not protocol_error_is_invalid_auth_step(exc):
                raise
            session = self.chatgpt.get_session()
            if not session.access_token:
                raise
            emit("session_ready", {"email": account.email, "via": "invalid_auth_step_recovery"})
            return RegistrationResult(
                email=account.email,
                session=session,
                create_account_result={"skipped": True, "reason": "invalid_auth_step_after_otp_continue"},
            )
        self.chatgpt.open_continue_url(created)
        session = self.chatgpt.get_session()
        emit("session_ready", {"email": account.email, "via": "create_account"})
        return RegistrationResult(email=account.email, session=session, create_account_result=created)

    def login_existing_account(
        self,
        email: str,
        *,
        code_provider: FreshCodeProvider,
        timeout: float = 90.0,
        on_event: "Callable[[str, dict], None] | None" = None,
    ) -> LoginResult:
        emit = on_event or (lambda _name, _payload: None)
        not_before = datetime.now(timezone.utc)
        emit("auth_csrf_pending", {"email": email})
        auth = self.chatgpt.start_openai_signin(email, mode="register")
        emit("signin_redirect_pending", {"email": email})
        self._open_auth_and_trigger_passwordless_if_needed(auth)
        emit("waiting_otp", {"email": email, "timeout": timeout})
        code = code_provider.wait_for_fresh_code(email, not_before=not_before, timeout=timeout).latest_code
        if not code:
            raise TimeoutError(f"fresh code not found for {email}")
        emit("otp_received", {"email": email, "code_length": len(code)})
        validation = self.chatgpt.validate_email_otp(code)
        emit("otp_validated", {"email": email})
        self.chatgpt.open_continue_url(validation, fallback=self.chatgpt.chatgpt_url("/"))
        session = self.chatgpt.get_session()
        emit("session_ready", {"email": email})
        return LoginResult(email=email, session=session, validation_result=validation)

    def checkout_link(self, access_token: str, checkout: CheckoutInput = CheckoutInput()) -> CheckoutLink:
        return self.chatgpt.generate_checkout_link(access_token, checkout)

    # ------------------------------------------------------------------
    # add-phone (pure HTTP). Drives a SmsProvider through the lease →
    # send → wait-for-otp → validate cycle, with retries on
    # ``phone_number_in_use`` / invalid OTP.
    # ------------------------------------------------------------------

    def bind_phone_with_provider(
        self,
        sms_provider: SmsProvider,
        *,
        max_attempts: int = 3,
        otp_timeout: float = 30.0,
        max_otp_retries: int = 2,
        on_event: "Callable[[str, dict], None] | None" = None,
    ) -> PhoneBindResult:
        """Bind a phone number using a SmsProvider.

        - ``max_attempts``: how many fresh phone numbers to try in total.
          Each ``phone_number_in_use`` rejection counts as one attempt and
          immediately rotates to a new lease.
        - ``otp_timeout``: per-attempt wall clock for SMS arrival.
        - ``max_otp_retries``: when an OTP arrives but OpenAI rejects it as
          invalid/expired, retry that many additional resends on the *same*
          phone number before giving up the lease.
        - ``on_event``: optional callback ``(event_name, payload)`` for
          progress logging from the orchestrator.

        Returns :class:`PhoneBindResult` on success. Raises
        :class:`NoPhoneAvailableError` / :class:`PhoneOtpInvalidError` /
        :class:`ProtocolResponseError` on terminal failure.
        """
        emit = on_event or (lambda _name, _payload: None)
        attempts = 0
        last_error: str | None = None

        while attempts < max_attempts:
            attempts += 1
            activation = sms_provider.request_phone()
            if activation is None or not activation.phone:
                last_error = "sms_provider returned no phone"
                emit("provider_no_phone", {"attempt": attempts})
                break
            phone = activation.phone
            emit("phone_acquired", {"attempt": attempts, "phone": phone, "provider": activation.provider or sms_provider.name})

            send_response: dict[str, Any]
            try:
                send_response = self.chatgpt.add_phone_send(phone)
            except PhoneNumberInUseError as exc:
                emit("phone_in_use", {"attempt": attempts, "phone": phone})
                sms_provider.cancel(activation)
                last_error = str(exc)
                continue
            except ProtocolResponseError as exc:
                # Non-200 with a body that didn't look like phone_in_use.
                # Release lease and rethrow — likely a sentinel/auth issue.
                emit("send_error", {"attempt": attempts, "phone": phone, "status": exc.status_code, "body": (exc.body or "")[:200]})
                sms_provider.cancel(activation)
                raise

            emit("sms_pending", {"attempt": attempts, "phone": phone})

            otp_attempt = 0
            validate_response: dict[str, Any] | None = None
            while otp_attempt <= max_otp_retries:
                otp_attempt += 1
                code = sms_provider.wait_for_otp(activation, timeout=otp_timeout)
                if not code:
                    emit("otp_timeout", {"attempt": attempts, "phone": phone, "otp_attempt": otp_attempt})
                    last_error = "otp_timeout"
                    break

                emit("otp_received", {"attempt": attempts, "phone": phone, "code_length": len(code)})
                try:
                    validate_response = self.chatgpt.phone_otp_validate(code)
                    break
                except PhoneOtpInvalidError as exc:
                    last_error = str(exc)
                    emit("otp_invalid", {"attempt": attempts, "phone": phone, "otp_attempt": otp_attempt})
                    if otp_attempt > max_otp_retries:
                        break
                    # Resend on the same lease and try again
                    self.chatgpt.phone_otp_resend()
                    continue
                except ProtocolResponseError as exc:
                    emit("validate_error", {"attempt": attempts, "phone": phone, "status": exc.status_code, "body": (exc.body or "")[:200]})
                    sms_provider.cancel(activation)
                    raise

            if validate_response is not None:
                sms_provider.complete(activation)
                continue_url = self.chatgpt.extract_continue_url(validate_response)
                page_type = self.chatgpt.extract_page_type(validate_response)
                emit("phone_bound", {"phone": phone, "continue_url": continue_url, "page_type": page_type})
                return PhoneBindResult(
                    phone_number=phone,
                    activation_id=activation.activation_id,
                    provider=activation.provider or sms_provider.name,
                    continue_url=continue_url or None,
                    page_type=page_type or None,
                    attempts=attempts,
                    raw={
                        "send": send_response,
                        "validate": validate_response,
                    },
                )

            # All OTP attempts on this lease exhausted — cancel & rotate.
            sms_provider.cancel(activation)

        if last_error and "phone_otp_invalid" in last_error:
            raise PhoneOtpInvalidError(code="", body=last_error)
        raise NoPhoneAvailableError(
            f"add_phone exhausted after {attempts} attempt(s); last_error={last_error or 'unknown'}"
        )
