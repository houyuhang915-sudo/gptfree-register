from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .chatgpt import SentinelHeaders


@dataclass(frozen=True)
class RiskTokenBundle:
    sentinel: SentinelHeaders
    source: str
    purpose: str | None = None
    request_url: str | None = None
    captured_headers: dict[str, str] | None = None


class RiskTokenProvider(Protocol):
    def get_openai_sentinel(self, *, purpose: str) -> RiskTokenBundle:
        ...


class StaticRiskTokenProvider:
    def __init__(self, *, token: str, so_token: str | None = None, source: str = "static") -> None:
        self.bundle = RiskTokenBundle(SentinelHeaders(token=token, so_token=so_token), source=source)

    def get_openai_sentinel(self, *, purpose: str) -> RiskTokenBundle:
        return RiskTokenBundle(
            sentinel=self.bundle.sentinel,
            source=self.bundle.source,
            purpose=purpose,
            captured_headers=self.bundle.captured_headers,
        )
