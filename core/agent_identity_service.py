"""Create or reuse Agent Identity credentials for existing account records."""

from __future__ import annotations

from typing import Any, Callable

import agent_identity_store
from agent_identity import register_agent_identity


LogFn = Callable[[str], None]


def _log(log_fn: LogFn | None, message: str) -> None:
    if log_fn:
        log_fn(message)


def _session_access_token(session_token: str, *, proxy: str = "") -> str:
    token = str(session_token or "").strip()
    if not token:
        return ""
    url = "https://chatgpt.com/api/auth/session"
    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
    }
    cookies = {"__Secure-next-auth.session-token": token}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        from curl_cffi import requests as curl_requests

        kwargs: dict[str, Any] = {
            "headers": headers,
            "cookies": cookies,
            "impersonate": "chrome",
            "timeout": 20,
        }
        if proxies:
            kwargs["proxies"] = proxies
        response = curl_requests.get(url, **kwargs)
    except ImportError:
        import requests

        response = requests.get(
            url,
            headers=headers,
            cookies=cookies,
            proxies=proxies,
            timeout=20,
        )
    if int(response.status_code) != 200:
        raise RuntimeError(f"session refresh HTTP {response.status_code}")
    payload = response.json()
    return str(payload.get("accessToken") or "") if isinstance(payload, dict) else ""


def _access_token_candidates(record: Any, *, proxy: str = "", log_fn: LogFn | None = None):
    seen: set[str] = set()

    def add(source: str, token: str, token_payload: dict | None = None) -> None:
        value = str(token or "").strip()
        if value and value not in seen:
            seen.add(value)
            candidates.append((source, value, token_payload or {}))

    candidates: list[tuple[str, str, dict]] = []
    codex_rt = str(getattr(record, "refresh_token", "") or "").strip()
    if codex_rt:
        try:
            from codex_oauth import refresh_access_token

            refreshed = refresh_access_token(codex_rt, proxy_url=proxy or None)
            add("codex_rt", str(refreshed.get("access_token") or ""), refreshed)
            _log(log_fn, "[agent-backfill] 已刷新 Codex access token")
        except Exception as exc:
            _log(log_fn, f"[agent-backfill] Codex RT 刷新未命中: {type(exc).__name__}: {exc}")

    session_token = str(getattr(record, "session_token", "") or "").strip()
    if session_token:
        try:
            add("session", _session_access_token(session_token, proxy=proxy))
            _log(log_fn, "[agent-backfill] 已从 ChatGPT session 获取 access token")
        except Exception as exc:
            _log(log_fn, f"[agent-backfill] Session 刷新未命中: {type(exc).__name__}: {exc}")

    add("stored_at", str(getattr(record, "access_token", "") or ""))
    return candidates


def _persist_refreshed_token(record: Any, token: str, payload: dict) -> None:
    try:
        from phone_binding import binding_store

        binding_store.record_binding(
            email=str(getattr(record, "email", "") or ""),
            access_token=token,
            refresh_token=str(payload.get("refresh_token") or getattr(record, "refresh_token", "") or ""),
            id_token=str(payload.get("id_token") or getattr(record, "id_token", "") or ""),
            chatgpt_account_id=str(getattr(record, "chatgpt_account_id", "") or ""),
            session_token=str(getattr(record, "session_token", "") or ""),
            plan_type=str(getattr(record, "plan_type", "") or "free"),
        )
    except Exception:
        pass


def ensure_agent_identity(
    record: Any,
    *,
    proxy: str = "",
    force: bool = False,
    log_fn: LogFn | None = None,
) -> dict[str, Any]:
    """Return an existing identity or create one from an account's freshest token."""
    email = str(getattr(record, "email", "") or "").strip()
    if not email:
        return {"ok": False, "error": "missing email"}

    existing = agent_identity_store.get(email)
    if existing and not force:
        return {"ok": True, "created": False, "token_source": "stored", **existing}

    candidates = _access_token_candidates(record, proxy=proxy, log_fn=log_fn)
    if not candidates:
        return {"ok": False, "error": "账号缺少可用的 access_token / session / Codex RT"}

    errors: list[str] = []
    for source, token, token_payload in candidates:
        _log(log_fn, f"[agent-backfill] 使用 {source} 创建 Agent Identity")
        result = register_agent_identity(token, proxy=proxy, log_fn=log_fn)
        if not result.get("ok"):
            errors.append(f"{source}: {result.get('error') or 'register_failed'}")
            continue
        try:
            stored = agent_identity_store.save(
                email=email,
                agent_runtime_id=str(result.get("agent_runtime_id") or ""),
                agent_private_key=str(result.get("agent_private_key") or ""),
                account_id=str(result.get("account_id") or getattr(record, "chatgpt_account_id", "") or ""),
                user_id=str(result.get("user_id") or getattr(record, "chatgpt_user_id", "") or ""),
                plan_type=str(result.get("plan_type") or getattr(record, "plan_type", "") or "free"),
            )
        except Exception as exc:
            return {"ok": False, "error": f"credential store: {type(exc).__name__}: {exc}"}
        if source != "stored_at":
            _persist_refreshed_token(record, token, token_payload)
        _log(log_fn, f"[agent-backfill] ✓ {email} Agent Identity 已保存")
        return {"ok": True, "created": True, "token_source": source, **stored}

    return {"ok": False, "error": "; ".join(errors[-3:]) or "agent registration failed"}


__all__ = ["ensure_agent_identity"]
