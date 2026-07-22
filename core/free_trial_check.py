"""Check whether a ChatGPT account can use the Plus free-trial campaign."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

from gpt_trial_protocol.http_client import ProtocolHttpClient
from gpt_trial_protocol.models import BrowserProfile


CAMPAIGN_ID = "plus-1-month-free"
CHECK_URL = "https://chatgpt.com/backend-api/promo_campaign/check_coupon"
_ELIGIBLE_STATES = {"eligible"}
_INELIGIBLE_STATES = {
    "ineligible",
    "not_eligible",
    "redeemed",
    "already_redeemed",
    "expired",
    "unavailable",
}
_FREE_PLAN_TYPES = {"free"}


def token_plan_type(access_token: str) -> str:
    """Read the account plan snapshot embedded in an access token."""
    token = str(access_token or "").strip()
    if token.count(".") < 2:
        return ""
    payload = token.split(".")[1]
    try:
        decoded = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8")
        )
    except Exception:
        return ""
    auth = decoded.get("https://api.openai.com/auth") or {}
    if not isinstance(auth, dict):
        return ""
    return str(auth.get("chatgpt_plan_type") or "").strip().lower()


def token_is_free_account(access_token: str) -> bool:
    return token_plan_type(access_token) in _FREE_PLAN_TYPES


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _base_result() -> dict[str, Any]:
    return {
        "status": "unknown",
        "eligible": None,
        "campaign_id": CAMPAIGN_ID,
        "state": "",
        "checked_at": _checked_at(),
        "error": "",
    }


def _classify(payload: dict[str, Any]) -> tuple[str, bool | None, str]:
    state = str(
        payload.get("state")
        or payload.get("status")
        or payload.get("eligibility")
        or ""
    ).strip().lower()
    explicit = payload.get("eligible")
    if state in _ELIGIBLE_STATES or explicit is True:
        return "eligible", True, state or "eligible"
    if state in _INELIGIBLE_STATES or explicit is False:
        return "not_eligible", False, state or "not_eligible"
    return "unknown", None, state


def check_free_trial(
    access_token: str,
    *,
    proxy: str = "",
    session_token: str = "",
    device_id: str = "",
    timeout: float = 15.0,
    http: Any = None,
) -> dict[str, Any]:
    """Return a normalized three-state result for the Plus trial campaign."""
    result = _base_result()
    token = str(access_token or "").strip()
    if not token:
        result["error"] = "missing_access_token"
        return result

    profile = BrowserProfile(device_id=str(device_id or "").strip() or None)
    headers = profile.api_headers(token)
    headers.update({
        "accept": "application/json",
        "origin": "https://chatgpt.com",
        "referer": "https://chatgpt.com/",
        "x-openai-target-path": "/backend-api/promo_campaign/check_coupon",
        "x-openai-target-route": "/backend-api/promo_campaign/check_coupon",
    })
    if session_token:
        headers["cookie"] = f"__Secure-next-auth.session-token={session_token}"

    owned_http = http is None
    client = http or ProtocolHttpClient(
        timeout=timeout,
        proxy=str(proxy or "").strip() or None,
        trace_name="free_trial_check",
    )
    try:
        response = client.get(
            CHECK_URL,
            params={
                "coupon": CAMPAIGN_ID,
                "is_coupon_from_query_param": "false",
            },
            headers=headers,
            timeout=timeout,
        )
        if int(response.status_code) != 200:
            result["error"] = f"http_{response.status_code}"
            return result
        payload = response.json()
        if not isinstance(payload, dict):
            result["error"] = "invalid_response"
            return result
        status, eligible, state = _classify(payload)
        result.update({
            "status": status,
            "eligible": eligible,
            "state": state,
        })
        redemption = payload.get("redemption")
        if isinstance(redemption, dict):
            result["redeemed_by_user"] = bool(redemption.get("redeemed_by_user"))
            result["redeemed_by_workspace"] = bool(redemption.get("redeemed_by_workspace"))
        if status == "unknown":
            result["error"] = "unrecognized_state"
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        return result
    finally:
        if owned_http:
            try:
                client.close()
            except Exception:
                pass


__all__ = [
    "CAMPAIGN_ID",
    "CHECK_URL",
    "check_free_trial",
    "token_is_free_account",
    "token_plan_type",
]
