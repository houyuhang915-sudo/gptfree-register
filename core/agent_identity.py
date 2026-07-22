"""Agent Identity helpers shared by registration and account export flows."""
from __future__ import annotations

import base64
import json
from typing import Any, Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


AUTH_API_BASE = "https://auth.openai.com/api/accounts"
AGENT_VERSION = "0.138.0-alpha.6"
AGENT_HARNESS_ID = "codex-cli"
RUNNING_LOCATION = "local"


def _create_session(proxy: str = ""):
    try:
        from curl_cffi.requests import Session

        session = Session(impersonate="safari18_0")
    except ImportError:
        import requests

        session = requests.Session()
        session.headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
        )
    session.trust_env = False
    normalized_proxy = str(proxy or "").strip()
    if normalized_proxy.startswith("socks5://"):
        normalized_proxy = "socks5h://" + normalized_proxy[len("socks5://"):]
    session.proxies = {"http": normalized_proxy, "https": normalized_proxy} if normalized_proxy else {
        "http": "", "https": "",
    }
    return session


def generate_ed25519_keypair() -> tuple[str, str]:
    """Return a PKCS8 private key and OpenSSH-formatted public key."""
    private_key = Ed25519PrivateKey.generate()
    private_key_b64 = base64.b64encode(private_key.private_bytes(
        encoding=Encoding.DER,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )).decode()
    public_key = private_key.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    header = b"ssh-ed25519"
    blob = (
        len(header).to_bytes(4, "big") + header
        + len(public_key).to_bytes(4, "big") + public_key
    )
    return private_key_b64, f"ssh-ed25519 {base64.b64encode(blob).decode()}"


def decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise ValueError("invalid JWT format")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(payload))
    return decoded if isinstance(decoded, dict) else {}


def extract_account_info(access_token: str) -> dict[str, str]:
    claims = decode_jwt_claims(access_token)
    auth_info = claims.get("https://api.openai.com/auth", {})
    profile = claims.get("https://api.openai.com/profile", {})
    return {
        "account_id": str(auth_info.get("chatgpt_account_id") or ""),
        "user_id": str(auth_info.get("chatgpt_user_id") or ""),
        "email": str(profile.get("email") or ""),
        "plan_type": str(auth_info.get("chatgpt_plan_type") or "free"),
    }


def build_auth_json(
    agent_runtime_id: str,
    private_key_b64: str,
    account_id: str,
    user_id: str,
    email: str,
    plan_type: str = "free",
) -> dict[str, Any]:
    return {
        "auth_mode": "agent_identity",
        "agent_identity": {
            "agent_runtime_id": agent_runtime_id,
            "agent_private_key": private_key_b64,
            "account_id": account_id,
            "chatgpt_user_id": user_id,
            "email": email,
            "plan_type": plan_type,
            "chatgpt_account_is_fedramp": False,
        },
    }


def register_agent_identity(
    access_token: str,
    *,
    proxy: str = "",
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Create an Agent Identity from an existing ChatGPT access token."""
    token = str(access_token or "").strip()
    if not token:
        return {"ok": False, "error": "missing access_token"}
    session = _create_session(proxy)
    try:
        if log_fn:
            log_fn("[agent-identity] 生成 Ed25519 密钥并注册 Agent Identity...")
        private_key_b64, public_key_ssh = generate_ed25519_keypair()
        response = session.post(
            f"{AUTH_API_BASE}/v1/agent/register",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            json={
                "abom": {
                    "agent_version": AGENT_VERSION,
                    "agent_harness_id": AGENT_HARNESS_ID,
                    "running_location": RUNNING_LOCATION,
                },
                "agent_public_key": public_key_ssh,
            },
            timeout=15,
        )
        if response.status_code != 200:
            raise RuntimeError(f"agent register failed: HTTP {response.status_code} {response.text[:300]}")
        payload = response.json()
        runtime_id = str(payload.get("agent_runtime_id") or "")
        if not runtime_id:
            raise RuntimeError("agent registration response has no runtime id")
        try:
            info = extract_account_info(token)
        except Exception:
            info = {}
        if log_fn:
            log_fn(f"[agent-identity] ready runtime_id={runtime_id[:20]}...")
        return {
            "ok": True,
            "agent_runtime_id": runtime_id,
            "agent_private_key": private_key_b64,
            "account_id": str(info.get("account_id") or ""),
            "user_id": str(info.get("user_id") or ""),
            "email": str(info.get("email") or ""),
            "plan_type": str(info.get("plan_type") or "free"),
        }
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        if log_fn:
            log_fn(f"[agent-identity] registration failed: {message}")
        return {"ok": False, "error": message}
    finally:
        try:
            session.close()
        except Exception:
            pass


__all__ = ["build_auth_json", "register_agent_identity"]
