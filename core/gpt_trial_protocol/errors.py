from __future__ import annotations


class ProtocolError(RuntimeError):
    """Base error for protocol-first ChatGPT flows."""


class ProtocolResponseError(ProtocolError):
    def __init__(self, method: str, url: str, status_code: int, body: str | None = None) -> None:
        detail = body[:500] if body else ""
        super().__init__(f"{method} {url} failed with HTTP {status_code}: {detail}")
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body


class MissingFieldError(ProtocolError):
    def __init__(self, field: str, source: str) -> None:
        super().__init__(f"missing field {field!r} from {source}")
        self.field = field
        self.source = source


class UnsupportedProtocolStep(ProtocolError):
    """Raised when a step requires data that cannot be produced by this project."""


class PhoneNumberInUseError(ProtocolError):
    """The phone number is already bound to another account; pick a fresh one."""

    def __init__(self, phone_number: str, body: str | None = None) -> None:
        super().__init__(f"phone_number_in_use: {phone_number}")
        self.phone_number = phone_number
        self.body = body


class PhoneOtpInvalidError(ProtocolError):
    """The submitted SMS OTP was rejected by OpenAI."""

    def __init__(self, code: str, body: str | None = None) -> None:
        super().__init__(f"phone_otp_invalid: {code}")
        self.code = code
        self.body = body


class NoPhoneAvailableError(ProtocolError):
    """The SMS provider could not allocate a phone number."""
