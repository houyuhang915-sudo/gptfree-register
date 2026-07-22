"""
Adapter: project-level ``email_provider.fetch_otp`` → ``FreshCodeProvider``.

Lets the protocol-only Free flow reuse this project's existing
Outlook (Graph API) / iCloud relay OTP fetching code, so users don't need to
operate a separate ``GPT_TRIAL_EMAIL_CODE_BASE_URL`` HTTP service.

Two inputs work transparently:
    * ``refresh_token`` looks like an HTTP URL → routed to ``fetch_otp_relay``
      (iCloud relay servers, 62us-style mailbox bridges, etc.)
    * ``refresh_token`` is a Microsoft Graph refresh token → routed to
      ``fetch_otp_graph`` with the supplied ``client_id``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any


log = logging.getLogger("gpt_trial_protocol.local_email_code")


class LocalEmailCodeProvider:
    """Implements the :class:`FreshCodeProvider` Protocol against the local mailbox."""

    def __init__(
        self,
        *,
        refresh_token: str,
        client_id: str | None = None,
        method: str = "graph",
        relay_require_fresh: bool = True,
    ) -> None:
        self.refresh_token = (refresh_token or "").strip()
        self.client_id = (client_id or "").strip()
        self.method = method
        self.relay_require_fresh = relay_require_fresh
        if not self.refresh_token:
            raise ValueError("LocalEmailCodeProvider requires refresh_token (or relay URL)")

    # FreshCodeProvider protocol — keep the kwargs flexible (the protocol uses
    # **kwargs for forward compatibility).
    def wait_for_fresh_code(
        self,
        email: str,
        *,
        not_before: datetime | None = None,
        timeout: float = 90.0,
        **_unused: object,
    ) -> "EmailCodeResult":
        from email_provider import fetch_otp  # type: ignore[import-not-found]

        from .email_code import EmailCodeResult

        # 把 not_before 转成 unix ts；fetch_otp_graph 用它过滤"基线之前的邮件"
        after_ts = not_before.astimezone(timezone.utc).timestamp() if not_before else 0.0

        # graph 模式直接传 after_ts；relay 模式忽略它（自己做基线快照对比）
        rt = self.refresh_token
        if rt.startswith(("http://", "https://")):
            method = "relay"
        else:
            method = self.method or "graph"

        log.info("LocalEmailCodeProvider · email=%s method=%s timeout=%ss",
                 email, method, int(timeout))

        if method == "relay":
            code = fetch_otp(
                email=email,
                refresh_token=rt,
                client_id=self.client_id,
                method="relay",
                timeout=int(max(5.0, timeout)),
                relay_require_fresh=self.relay_require_fresh,
            )
        elif method == "imap":
            code = fetch_otp(
                email=email,
                refresh_token=rt,
                client_id=self.client_id,
                method="imap",
                timeout=int(max(5.0, timeout)),
            )
        else:
            # graph 走这里；fetch_otp_graph 直接吃 after_ts，不需要 wrapper 自己做 baseline
            from email_provider import fetch_otp_graph  # type: ignore[import-not-found]
            code = fetch_otp_graph(
                email=email,
                refresh_token=rt,
                client_id=self.client_id or _default_client_id(),
                timeout=int(max(5.0, timeout)),
                after_ts=after_ts,
            )

        if not code:
            return EmailCodeResult(email=email, latest_code=None, latest_time=None, raw={})

        return EmailCodeResult(
            email=email,
            latest_code=str(code),
            latest_time=datetime.now(timezone.utc),
            raw={"source": "local_email_provider", "method": method},
        )


def _default_client_id() -> str:
    try:
        from email_provider import DEFAULT_CLIENT_ID  # type: ignore[import-not-found]
        return DEFAULT_CLIENT_ID
    except Exception:
        return ""


def can_use_local_for(item: Any) -> bool:
    """Return True iff the EmailItem carries credentials we can drive locally."""
    return bool(getattr(item, "refresh_token", "") or "")
