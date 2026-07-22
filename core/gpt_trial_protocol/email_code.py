from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import os
from typing import Any

import httpx


DEFAULT_EMAIL_CODE_BASE_URL = os.getenv("GPT_TRIAL_EMAIL_CODE_BASE_URL", "").rstrip("/")
CUSTOM_EMAIL_DOMAIN = os.getenv("GPT_TRIAL_CUSTOM_EMAIL_DOMAIN", "example-mail.invalid")


class EmailCodeProvider(str, Enum):
    AUTO = "auto"
    EXTRACT_JSON = "extract_json"
    OPENAI_CODE_JSON = "openai_code_json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class EmailCodeResult:
    email: str
    latest_code: str | None
    latest_time: datetime | None
    raw: dict[str, Any]

    @classmethod
    def from_extract_json_payload(cls, payload: dict[str, Any]) -> "EmailCodeResult":
        latest = payload.get("latest") if isinstance(payload.get("latest"), dict) else {}
        return cls(
            email=str(payload.get("email") or ""),
            latest_code=payload.get("latestCode"),
            latest_time=parse_datetime(latest.get("date")),
            raw=payload,
        )

    @classmethod
    def from_openai_code_json_payload(cls, payload: dict[str, Any]) -> "EmailCodeResult":
        return cls(
            email=str(payload.get("recipient") or ""),
            latest_code=payload.get("code"),
            latest_time=parse_datetime(payload.get("receivedAt")),
            raw=payload,
        )

    def is_fresh(self, *, not_before: datetime | None, max_age_seconds: float) -> bool:
        if not self.latest_code or self.latest_time is None:
            return False
        if not_before and self.latest_time < not_before - timedelta(seconds=5):
            return False
        age = (utc_now() - self.latest_time.astimezone(timezone.utc)).total_seconds()
        return 0 <= age <= max_age_seconds


class EmailCodeClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 20.0,
        *,
        provider: str | EmailCodeProvider = EmailCodeProvider.AUTO,
        trust_env: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.provider = EmailCodeProvider(provider)
        self.client = httpx.Client(timeout=timeout, trust_env=trust_env)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "EmailCodeClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def extract(self, email: str, *, refresh: bool = False, limit: int = 20) -> EmailCodeResult:
        provider = self._provider_for_email(email)
        if provider is EmailCodeProvider.OPENAI_CODE_JSON:
            return self._extract_openai_code_json(email)
        return self._extract_extract_json(email, refresh=refresh, limit=limit)

    def _extract_extract_json(self, email: str, *, refresh: bool = False, limit: int = 20) -> EmailCodeResult:
        params: dict[str, Any] = {"email": email, "limit": limit}
        if refresh:
            params["refresh"] = 1
        response = self.client.get(f"{self._base_url(EmailCodeProvider.EXTRACT_JSON)}/api/v1/extract", params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(f"email code endpoint failed: {payload}")
        return EmailCodeResult.from_extract_json_payload(payload)

    def _extract_openai_code_json(self, email: str) -> EmailCodeResult:
        response = self.client.get(f"{self._base_url(EmailCodeProvider.OPENAI_CODE_JSON)}/v1/openai-code", params={"recipient": email})
        if response.status_code == 404:
            payload = response.json()
            if not isinstance(payload, dict):
                payload = {"recipient": email, "code": None, "receivedAt": None}
            return EmailCodeResult.from_openai_code_json_payload(payload)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("code"):
            raise RuntimeError(f"openai-code JSON endpoint failed: {payload}")
        return EmailCodeResult.from_openai_code_json_payload(payload)

    def _provider_for_email(self, email: str) -> EmailCodeProvider:
        if self.provider is not EmailCodeProvider.AUTO:
            return self.provider
        if email.lower().endswith(f"@{CUSTOM_EMAIL_DOMAIN.lower()}"):
            return EmailCodeProvider.OPENAI_CODE_JSON
        return EmailCodeProvider.EXTRACT_JSON

    def _base_url(self, provider: EmailCodeProvider) -> str:
        base_url = self.base_url or DEFAULT_EMAIL_CODE_BASE_URL
        if not base_url:
            raise RuntimeError(
                "email code base URL is required in the public build; set --email-code-base-url "
                "or GPT_TRIAL_EMAIL_CODE_BASE_URL for your own provider"
            )
        return base_url.rstrip("/")

    def wait_for_fresh_code(
        self,
        email: str,
        *,
        not_before: datetime | None,
        timeout: float = 90.0,
        interval: float = 2.0,
        refresh_interval: float = 8.0,
        max_age_seconds: float = 180.0,
    ) -> EmailCodeResult:
        deadline = time.monotonic() + timeout
        next_refresh = 0.0
        last_error: Exception | None = None
        while True:
            now = time.monotonic()
            refresh = now >= next_refresh
            if refresh:
                next_refresh = now + refresh_interval
            try:
                result = self.extract(email, refresh=refresh)
                last_error = None
                if result.is_fresh(not_before=not_before, max_age_seconds=max_age_seconds):
                    return result
            except Exception as exc:
                last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if last_error is not None:
                    raise TimeoutError(f"fresh email code not found for {email}; last error: {last_error}") from last_error
                raise TimeoutError(f"fresh email code not found for {email}")
            time.sleep(min(interval, remaining))
