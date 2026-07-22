"""
导出产物自动推送到下游网关。

支持：
  - any2api (admin.json POST)
  - sub2api (兼容多家：标准 admin endpoint，配置成 base_url + admin_token 即可)
  - 通用 webhook (任意 HTTPS endpoint，POST JSON body)

参考 _aBaiAutoplus-ref/core/any2api_sync.py 的同款思路；
本项目精简为 stateless：每次都把 account_exports.export(...) 的 artifact
POST 上去，由下游决定 merge / replace。

配置走 config.py / config.local.py，键都带 GATEWAY_ 前缀：

    # any2api
    GATEWAY_ANY2API_URL = "https://your-any2api.example.com"
    GATEWAY_ANY2API_TOKEN = "..."          # 必填，admin password / token

    # sub2api 同协议（可选第二个）
    GATEWAY_SUB2API_URL = "https://your-sub2api.example.com"
    GATEWAY_SUB2API_TOKEN = "..."
    GATEWAY_SUB2API_AGENT_PATH = "/api/v1/admin/accounts/import/codex-session"
    GATEWAY_SUB2API_GROUP_IDS = "2"

    # 通用 webhook
    GATEWAY_WEBHOOK_URL = "https://example.com/api/import"
    GATEWAY_WEBHOOK_TOKEN = "Bearer ..."   # 直接拼到 Authorization header

公开 API:
    list_targets()                      → list[dict] 当前可用网关
    push(target_key, artifact)          → 推送单个 artifact
    push_export(target_key, format_key, selection)  → 一站式：导出 + 推送
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import config

log = logging.getLogger("gateway_push")

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


@dataclass
class GatewayTarget:
    key: str
    label: str
    base_url: str
    admin_token: str
    # 推送什么内容：admin.json 风格（cover sub2api/any2api）或 raw（任意 JSON body）
    style: str = "admin_json"
    # admin_json 推哪个 path（默认 /api/admin/import）
    path: str = "/api/admin/import"
    # 推荐的导出格式 key（account_exports.EXPORT_FORMATS）
    suggested_format: str = "any2api"
    # Sub2API Agent Identity 专用导入端点。
    agent_path: str = ""


def list_targets() -> list[GatewayTarget]:
    """读 config.* 找出当前可用的 gateway。"""
    targets: list[GatewayTarget] = []

    any2api_url = (getattr(config, "GATEWAY_ANY2API_URL", "") or "").strip()
    any2api_token = (getattr(config, "GATEWAY_ANY2API_TOKEN", "") or "").strip()
    if any2api_url:
        targets.append(GatewayTarget(
            key="any2api",
            label="Any2API",
            base_url=any2api_url.rstrip("/"),
            admin_token=any2api_token,
            style="admin_json",
            path=getattr(config, "GATEWAY_ANY2API_PATH", "/api/admin/import") or "/api/admin/import",
            suggested_format="any2api",
        ))

    sub2api_url = (getattr(config, "GATEWAY_SUB2API_URL", "") or "").strip()
    sub2api_token = (getattr(config, "GATEWAY_SUB2API_TOKEN", "") or "").strip()
    if sub2api_url:
        targets.append(GatewayTarget(
            key="sub2api",
            label="Sub2API",
            base_url=sub2api_url.rstrip("/"),
            admin_token=sub2api_token,
            style="admin_json",
            path=getattr(config, "GATEWAY_SUB2API_PATH", "/api/admin/import") or "/api/admin/import",
            suggested_format="sub2api",
            agent_path=getattr(
                config,
                "GATEWAY_SUB2API_AGENT_PATH",
                "/api/v1/admin/accounts/import/codex-session",
            ) or "/api/v1/admin/accounts/import/codex-session",
        ))

    hook_url = (getattr(config, "GATEWAY_WEBHOOK_URL", "") or "").strip()
    hook_token = (getattr(config, "GATEWAY_WEBHOOK_TOKEN", "") or "").strip()
    if hook_url:
        targets.append(GatewayTarget(
            key="webhook",
            label="Webhook",
            base_url=hook_url.rstrip("/"),
            admin_token=hook_token,
            style="raw",
            path="",
            suggested_format=getattr(config, "GATEWAY_WEBHOOK_FORMAT", "json") or "json",
        ))

    return targets


def get_target(target_key: str) -> GatewayTarget:
    targets = list_targets()
    for t in targets:
        if t.key == target_key:
            return t
    raise ValueError(f"unknown gateway target: {target_key}")


def _http_post_json(url: str, payload: bytes, *, headers: dict, timeout: int = 30) -> dict:
    """统一 POST JSON。返回 {status, body, ok}。"""
    req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)
        body = resp.read().decode("utf-8", errors="ignore")
        return {"status": resp.status, "ok": 200 <= resp.status < 300, "body": body[:5000]}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        return {"status": exc.code, "ok": False, "body": body[:5000]}
    except Exception as exc:
        return {"status": 0, "ok": False, "body": f"{type(exc).__name__}: {exc}"[:300]}


def push(target_key: str, content: str | bytes,
         *, content_type: str = "application/json") -> dict:
    """把已经生成的 artifact content 推到指定 gateway。

    返回 {ok, status, body, target}。
    """
    target = get_target(target_key)
    if isinstance(content, str):
        body = content.encode("utf-8")
    else:
        body = content

    if target.style == "admin_json":
        url = f"{target.base_url}{target.path}"
        headers = {
            "Content-Type": content_type,
            "Authorization": f"Bearer {target.admin_token}" if target.admin_token else "",
        }
        # 兼容 Any2API 风格的 X-Admin-Token header
        if target.admin_token:
            headers["X-Admin-Token"] = target.admin_token
        # 移除空 header 值
        headers = {k: v for k, v in headers.items() if v}
        log.info(f"[gateway] POST {url} ({len(body)} bytes)")
        result = _http_post_json(url, body, headers=headers)
    elif target.style == "raw":
        url = target.base_url
        headers = {"Content-Type": content_type}
        if target.admin_token:
            # webhook token 直接当 Authorization 用（用户写 'Bearer xxx' 就 'Bearer xxx'）
            tok = target.admin_token
            headers["Authorization"] = tok if " " in tok else f"Bearer {tok}"
        log.info(f"[gateway] webhook POST {url} ({len(body)} bytes)")
        result = _http_post_json(url, body, headers=headers)
    else:
        raise ValueError(f"unsupported style: {target.style}")

    return {
        "ok": result["ok"],
        "status": result["status"],
        "body": result["body"],
        "target": target.key,
        "url": url,
    }


def push_export(target_key: str,
                format_key: str | None = None,
                *,
                selection=None,
                refresh_tokens: bool | None = None) -> dict:
    """一站式：用 account_exports 生成 artifact，再推到 gateway。"""
    import account_exports

    target = get_target(target_key)
    fmt = format_key or target.suggested_format
    if target.key == "sub2api" and fmt in {"codex_auth", "sub2api_agent"}:
        return push_agent_identities(selection=selection)
    artifact = account_exports.export(fmt, selection, refresh_tokens=refresh_tokens)
    result = push(
        target_key,
        artifact.content,
        content_type=artifact.media_type or "application/octet-stream",
    )
    result["filename"] = artifact.filename
    result["format"] = fmt
    return result


def _sub2api_group_ids() -> list[int]:
    raw = getattr(config, "GATEWAY_SUB2API_GROUP_IDS", "2")
    if isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = str(raw or "2").split(",")
    out: list[int] = []
    for value in values:
        try:
            group_id = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if group_id > 0 and group_id not in out:
            out.append(group_id)
    return out or [2]


def test_sub2api() -> dict:
    """使用账号列表端点同时验证 Sub2API 地址和 x-api-key。"""
    target = get_target("sub2api")
    if not target.admin_token:
        raise ValueError("Sub2API API Key 为空")
    url = f"{target.base_url}/api/v1/admin/accounts"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{target.base_url}/admin/accounts",
        "x-api-key": target.admin_token,
    }
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=20, context=_SSL_CTX)
        body = response.read().decode("utf-8", errors="ignore")
        return {
            "ok": 200 <= response.status < 300,
            "status": response.status,
            "body": body[:1000],
            "url": url,
        }
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        return {"ok": False, "status": exc.code, "body": body[:1000], "url": url}
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "body": f"{type(exc).__name__}: {exc}"[:300],
            "url": url,
        }


def push_agent_identities(*, selection=None, records=None) -> dict:
    """Import Agent Identity auth JSON through Sub2API's codex-session endpoint.

    Sub2API does not consume the bare auth.json at the normal accounts endpoint.
    It expects a wrapper whose ``content`` field is the serialized auth.json.
    """
    import account_exports

    target = get_target("sub2api")
    if not target.admin_token:
        raise ValueError("Sub2API API Key 为空")
    source = records if records is not None else account_exports.load_records()
    selected = account_exports.select_records(source, selection)
    selected = [record for record in selected if record.has_agent_identity]
    if not selected:
        raise ValueError("no Agent Identity records match selection")

    path = target.agent_path or "/api/v1/admin/accounts/import/codex-session"
    url = f"{target.base_url}{path}"
    group_ids = _sub2api_group_ids()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{target.base_url}/admin/accounts",
        "x-api-key": target.admin_token,
    }
    headers = {key: value for key, value in headers.items() if value}

    results: list[dict[str, Any]] = []
    for record in selected:
        auth_json = account_exports._agent_auth_json(record)
        payload = {
            "content": json.dumps(auth_json, ensure_ascii=False),
            "name": record.email or "codex-agent",
            "update_existing": True,
            "group_ids": group_ids,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        log.info(f"[gateway] Sub2API Agent Identity import {record.email} → {url}")
        response = _http_post_json(url, body, headers=headers)
        results.append({
            "email": record.email,
            "ok": bool(response.get("ok")),
            "status": int(response.get("status") or 0),
            "body": str(response.get("body") or "")[:1000],
        })

    succeeded = sum(1 for item in results if item["ok"])
    failed = len(results) - succeeded
    statuses = [item["status"] for item in results if item["status"]]
    return {
        "ok": failed == 0,
        "status": statuses[-1] if statuses else 0,
        "body": json.dumps({
            "total": len(results),
            "succeeded": succeeded,
            "failed": failed,
            "results": results,
        }, ensure_ascii=False),
        "target": "sub2api",
        "url": url,
        "format": "sub2api_agent",
        "filename": "sub2api_agent_import.json",
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }
