"""
Pluggable SMS provider interface for the protocol-only add-phone flow.

The add-phone HTTP path needs three things from a SMS platform:
  1. Acquire a phone number (E.164, ready to receive SMS)
  2. Wait for the OpenAI verification SMS, return the 4-8 digit code
  3. Release the activation (success / cancel)

We keep the contract minimal so users can plug any provider — the bundled
adapter in this module wraps the project-level ``sms_provider.SmsProviderBase``
(``smsbower`` / ``smshub`` / ``hero-sms`` / ``62us`` etc.), but anything
implementing the :class:`SmsProvider` Protocol works.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable


log = logging.getLogger("gpt_trial_protocol.sms")


@dataclass
class SmsActivation:
    """A leased phone-number/activation from an SMS platform.

    ``phone`` MUST be E.164 (``+15555550100``) — the OpenAI add-phone API
    expects the leading ``+``. ``raw`` carries provider-specific payload
    (e.g. activation id, statusAction, base url) that the provider may need
    when polling or releasing.
    """

    phone: str
    activation_id: str | None = None
    provider: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SmsProvider(Protocol):
    """Protocol for SMS platforms used by the add-phone flow.

    Implementations should be re-entrant per call (no shared state between
    activations) so the orchestrator can retry / pick a fresh number when
    OpenAI rejects a phone (e.g. ``phone_number_in_use``).
    """

    name: str

    def request_phone(self) -> SmsActivation | None:
        """Lease a fresh phone number. Return None when no number is available."""
        ...

    def mark_sent(self, activation: SmsActivation) -> bool:
        """Tell the platform that OpenAI accepted the send request."""
        ...

    def request_resend(self, activation: SmsActivation) -> bool:
        """Tell the platform to expect another SMS on the same lease."""
        ...

    def wait_for_otp(
        self,
        activation: SmsActivation,
        *,
        timeout: float = 30.0,
        exclude_codes: Iterable[str] | None = None,
    ) -> str | None:
        """Poll the platform until the OpenAI SMS arrives. Return digits or None."""
        ...

    def complete(self, activation: SmsActivation) -> bool:
        """Mark activation as successfully consumed (refund-blocking)."""
        ...

    def cancel(self, activation: SmsActivation) -> bool:
        """Release activation as unused — refund eligible on most platforms."""
        ...


# ---------------------------------------------------------------------------
# Adapter for the project-level ``sms_provider.SmsProviderBase`` family.
#
# That base class predates the Protocol above. Its signatures are slightly
# different (dict-shaped activations, snake_case ``deadline_s`` etc.).
# This adapter normalizes the contract so we can drop in 62us / smsbower /
# smshub / hero-sms / tiger-sms etc. without rewriting them.
# ---------------------------------------------------------------------------


class LegacySmsProviderAdapter:
    """Wraps a ``sms_provider.SmsProviderBase`` instance as a SmsProvider."""

    def __init__(self, legacy: Any) -> None:
        self._legacy = legacy
        self.name = getattr(legacy, "name", "legacy")

    def request_phone(self) -> SmsActivation | None:
        record = self._legacy.request_phone()
        if not record:
            return None
        phone = str(record.get("phone") or "").strip()
        if not phone:
            log.warning("sms provider %s returned activation without phone: %r", self.name, record)
        return SmsActivation(
            phone=phone,
            activation_id=str(record.get("id") or "") or None,
            provider=str(record.get("provider") or self.name),
            raw=dict(record),
        )

    def mark_sent(self, activation: SmsActivation) -> bool:
        return self._call_lifecycle("mark_sent", activation)

    def request_resend(self, activation: SmsActivation) -> bool:
        return self._call_lifecycle("request_resend", activation)

    def wait_for_otp(
        self,
        activation: SmsActivation,
        *,
        timeout: float = 30.0,
        exclude_codes: Iterable[str] | None = None,
    ) -> str | None:
        deadline_s = int(max(1.0, timeout))
        excluded = _normalize_codes(exclude_codes)
        if not excluded:
            return self._legacy.wait_otp(activation.raw, deadline_s=deadline_s)
        try:
            return self._legacy.wait_otp(
                activation.raw,
                deadline_s=deadline_s,
                exclude_codes=excluded,
            )
        except TypeError as exc:
            if "exclude_codes" not in str(exc):
                raise
            code = self._legacy.wait_otp(activation.raw, deadline_s=deadline_s)
            return code if code and str(code).strip() not in excluded else None

    def complete(self, activation: SmsActivation) -> bool:
        try:
            return bool(self._legacy.complete(activation.raw))
        except Exception:
            return False

    def cancel(self, activation: SmsActivation) -> bool:
        try:
            return bool(self._legacy.cancel(activation.raw))
        except Exception:
            return False

    def _call_lifecycle(self, method_name: str, activation: SmsActivation) -> bool:
        method = getattr(self._legacy, method_name, None)
        if not callable(method):
            return True
        try:
            return bool(method(activation.raw))
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Generic HTTP-callable provider — for users who already operate an in-house
# rental gateway that exposes a single GET URL returning the latest SMS body.
#
# Configuration is intentionally minimal:
#   acquire_url:   GET → returns ``+15551234567`` or JSON {"phone": "..."}.
#                  If empty, ``static_phone`` is reused for every activation.
#   poll_url:      GET → returns the latest SMS body (any text); the regex
#                  ``code_regex`` is searched against it for the OTP.
#   release_url:   optional POST/GET fired on complete/cancel.
# ---------------------------------------------------------------------------


@dataclass
class HttpSmsConfig:
    name: str = "http"
    acquire_url: str | None = None
    static_phone: str | None = None
    poll_url: str = ""
    release_url_complete: str | None = None
    release_url_cancel: str | None = None
    code_regex: str = r"\b(\d{4,8})\b"
    poll_interval: float = 4.0
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 20.0
    accept_json: bool = True


class HttpSmsProvider:
    """A tiny HTTP-callable provider: configure 1-3 URLs and a regex."""

    def __init__(self, config: HttpSmsConfig, *, fetcher: Callable[..., Any] | None = None) -> None:
        self.config = config
        self.name = config.name
        self._fetch = fetcher or _default_fetcher

    def request_phone(self) -> SmsActivation | None:
        if self.config.static_phone and not self.config.acquire_url:
            return SmsActivation(phone=self.config.static_phone, provider=self.name)
        if not self.config.acquire_url:
            log.warning("HttpSmsProvider %s: neither acquire_url nor static_phone configured", self.name)
            return None
        body = self._fetch(self.config.acquire_url, method="GET", headers=self.config.headers, timeout=self.config.timeout)
        phone = _coerce_phone(body)
        if not phone:
            log.warning("HttpSmsProvider %s: acquire_url returned no phone: %r", self.name, body[:200] if isinstance(body, str) else body)
            return None
        return SmsActivation(phone=phone, provider=self.name, raw={"acquire_body": body})

    def mark_sent(self, activation: SmsActivation) -> bool:
        return True

    def request_resend(self, activation: SmsActivation) -> bool:
        return True

    def wait_for_otp(
        self,
        activation: SmsActivation,
        *,
        timeout: float = 30.0,
        exclude_codes: Iterable[str] | None = None,
    ) -> str | None:
        if not self.config.poll_url:
            return None
        import re

        pattern = re.compile(self.config.code_regex)
        excluded = _normalize_codes(exclude_codes)
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            try:
                url = self.config.poll_url.format(phone=activation.phone or "")
                body = self._fetch(url, method="GET", headers=self.config.headers, timeout=self.config.timeout)
                text = body if isinstance(body, str) else str(body)
                match = pattern.search(text)
                if match:
                    code = match.group(1) if match.lastindex else match.group(0)
                    if code not in excluded:
                        return code
            except Exception as exc:  # noqa: BLE001 — provider quirks are expected
                log.debug("HttpSmsProvider %s poll error: %s", self.name, exc)
            time.sleep(self.config.poll_interval)
        return None

    def complete(self, activation: SmsActivation) -> bool:
        return self._fire_release(activation, self.config.release_url_complete)

    def cancel(self, activation: SmsActivation) -> bool:
        return self._fire_release(activation, self.config.release_url_cancel)

    def _fire_release(self, activation: SmsActivation, url: str | None) -> bool:
        if not url:
            return True
        try:
            self._fetch(url.format(phone=activation.phone or "", id=activation.activation_id or ""), method="GET", headers=self.config.headers, timeout=self.config.timeout)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("HttpSmsProvider %s release error: %s", self.name, exc)
            return False


def _default_fetcher(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, timeout: float = 20.0) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers=dict(headers or {}), method=method.upper())
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec — caller-provided URL
        data = resp.read() or b""
    return data.decode("utf-8", errors="replace")


def _coerce_phone(body: Any) -> str:
    if not body:
        return ""
    text = body.strip() if isinstance(body, str) else str(body).strip()
    if not text:
        return ""
    if text.startswith("{") or text.startswith("["):
        import json
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict):
            for key in ("phone", "phone_number", "number", "msisdn"):
                if data.get(key):
                    return _normalize_e164(str(data[key]))
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                for key in ("phone", "phone_number", "number", "msisdn"):
                    if first.get(key):
                        return _normalize_e164(str(first[key]))
    return _normalize_e164(text.splitlines()[0])


def _normalize_e164(text: str) -> str:
    raw = "".join(ch for ch in text if ch.isdigit() or ch == "+")
    if not raw:
        return ""
    if raw.startswith("+"):
        return raw
    return "+" + raw


def _normalize_codes(codes: Iterable[str] | None) -> set[str]:
    return {str(code).strip() for code in (codes or ()) if str(code).strip()}


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def from_legacy_provider(legacy: Any) -> SmsProvider:
    """Wrap a ``sms_provider.SmsProviderBase`` so it satisfies SmsProvider."""
    return LegacySmsProviderAdapter(legacy)


def load_legacy_default(card: Any | None = None,
                        purpose: str | None = None) -> SmsProvider | None:
    """Convenience: load the project-level default provider via ``config``."""
    try:
        from sms_provider import get_sms_provider  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return None
    legacy = get_sms_provider(card, purpose=purpose)
    if legacy is None:
        return None
    return LegacySmsProviderAdapter(legacy)
