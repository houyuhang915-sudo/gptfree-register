from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.7727.50 Safari/537.36"
)


@dataclass(frozen=True)
class BrowserProfile:
    user_agent: str = DEFAULT_USER_AGENT
    sec_ch_ua: str = '"Chromium";v="147", "Not=A?Brand";v="24", "Google Chrome";v="147"'
    sec_ch_ua_platform: str = '"Windows"'
    sec_ch_ua_mobile: str = "?0"
    language: str = "ja-JP"
    timezone: str = "Asia/Tokyo"
    device_id: str | None = None
    session_id: str | None = None

    def language_header(self) -> str:
        primary = self.language or "en-US"
        root = primary.split("-", 1)[0]
        if primary.lower() == "en-us":
            return "en-US,en;q=0.9"
        if root and root.lower() != "en":
            return f"{primary},{root};q=0.9,en-US;q=0.8,en;q=0.7"
        return f"{primary},en;q=0.9"

    def browser_headers(
        self,
        *,
        referer: str | None = None,
        content_type: str | None = None,
        accept: str = "*/*",
        origin: str | None = None,
        fetch_site: str | None = None,
        fetch_mode: str | None = None,
        fetch_dest: str | None = None,
        priority: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "accept": accept,
            "accept-language": self.language_header(),
            "user-agent": self.user_agent,
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": self.sec_ch_ua_mobile,
            "sec-ch-ua-platform": self.sec_ch_ua_platform,
        }
        if referer:
            headers["referer"] = referer
        if origin:
            headers["origin"] = origin
        if content_type:
            headers["content-type"] = content_type
        if fetch_site:
            headers["sec-fetch-site"] = fetch_site
        if fetch_mode:
            headers["sec-fetch-mode"] = fetch_mode
        if fetch_dest:
            headers["sec-fetch-dest"] = fetch_dest
        if priority:
            headers["priority"] = priority
        return headers

    @staticmethod
    def fetch_site_from_referer(referer: str | None, *, target_url: str | None = None) -> str:
        if not referer:
            return "none"
        if not target_url:
            return "same-origin"
        ref = urlparse(referer)
        target = urlparse(target_url)
        if (ref.scheme, ref.hostname, ref.port) == (target.scheme, target.hostname, target.port):
            return "same-origin"
        ref_parts = (ref.hostname or "").split(".")[-2:]
        target_parts = (target.hostname or "").split(".")[-2:]
        if ref_parts and ref_parts == target_parts:
            return "same-site"
        return "cross-site"

    def xhr_headers(
        self,
        *,
        referer: str | None = None,
        content_type: str | None = None,
        accept: str = "*/*",
        origin: str | None = None,
        fetch_site: str | None = None,
    ) -> dict[str, str]:
        return self.browser_headers(
            referer=referer,
            content_type=content_type,
            accept=accept,
            origin=origin,
            fetch_site=fetch_site or self.fetch_site_from_referer(referer),
            fetch_mode="cors",
            fetch_dest="empty",
            priority="u=1, i",
        )

    def api_headers(self, access_token: str, *, referer: str = "https://chatgpt.com/") -> dict[str, str]:
        headers = self.browser_headers(referer=referer)
        headers["authorization"] = f"Bearer {access_token}"
        headers["oai-language"] = self.language
        headers["x-openai-target-path"] = "/"
        headers["x-openai-target-route"] = "conversation"
        headers["x-oai-is"] = "true"
        if self.device_id:
            headers["oai-device-id"] = self.device_id
        if self.session_id:
            headers["oai-session-id"] = self.session_id
        return headers


@dataclass(frozen=True)
class ProtocolConfig:
    chatgpt_base_url: str = "https://chatgpt.com"
    auth_base_url: str = "https://auth.openai.com"
    code_receiver_base_url: str = ""
    trace_dir: Path | None = None
    timeout: float = 30.0
    profile: BrowserProfile = field(default_factory=BrowserProfile)


@dataclass(frozen=True)
class AccountInput:
    email: str
    display_name: str | None = None
    birthdate: str | None = None


@dataclass(frozen=True)
class CheckoutInput:
    country: str = "US"
    currency: str = "USD"
    plan_name: str = "chatgptplusplan"
    promo_campaign_id: str = "plus-1-month-free"


@dataclass(frozen=True)
class AuthStart:
    url: str
    csrf_token: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class SessionInfo:
    access_token: str | None
    expires: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class CheckoutLink:
    url: str
    checkout_session_id: str | None
    processor_entity: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class PhoneBindResult:
    """Outcome of an HTTP-protocol add-phone flow."""
    phone_number: str
    activation_id: str | None
    provider: str | None
    continue_url: str | None
    page_type: str | None
    attempts: int
    raw: dict[str, Any]
