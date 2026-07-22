from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx

from .errors import ProtocolResponseError


SENSITIVE_HEADER_NAMES = {"authorization", "cookie", "set-cookie", "x-csrf-token"}
IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS"}
FAST_CURL_FALLBACK_MARKERS = (
    "curl: (35)",
    "tls connect error",
    "ssl_error_syscall",
    "unexpected_eof_while_reading",
    "connection reset",
    "operation timed out",
)
SAFE_CURL_FALLBACK_POST_PATHS = ("/backend-api/payments/checkout",)


def _httpx_supports_http2() -> bool:
    try:
        import h2  # noqa: F401  — required transitively by httpx for http/2
        return True
    except ImportError:
        return False


_HTTPX_HTTP2 = _httpx_supports_http2()


def _safe_headers(headers: Any, *, sensitive: bool) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in dict(headers or {}).items():
        out[str(key)] = str(value) if sensitive or str(key).lower() not in SENSITIVE_HEADER_NAMES else "***"
    return out


def _body_record(data: bytes | None, *, limit: int, sensitive: bool) -> dict[str, Any] | None:
    if data is None:
        return None
    text = data.decode("utf-8", errors="replace")
    if not sensitive:
        text = text.replace("\r", "\\r")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "truncated": len(text) > limit,
        "text": text[:limit],
    }


class ProtocolHttpClient:
    """HTTP client with browser-like trace output and optional curl_cffi backend."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        proxy: str | None = None,
        trace_dir: Path | None = None,
        trace_name: str = "chatgpt",
        trace_sensitive: bool = False,
        body_limit: int = 65536,
        backend: str = "curl_cffi",
        impersonate: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.proxy = proxy
        self.trace_dir = trace_dir
        self.trace_name = trace_name
        self.trace_sensitive = trace_sensitive
        self.body_limit = body_limit
        self._seq = 0
        self.backend = "httpx"
        if backend in {"curl", "curl_cffi", "curl-cffi"}:
            try:
                from curl_cffi import requests as curl_requests
            except ModuleNotFoundError:
                backend = "httpx"
            else:
                self.backend = "curl_cffi"
                self.client = curl_requests.Session(impersonate=impersonate or "chrome")
                return
        kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": False, "http2": _HTTPX_HTTP2}
        if proxy:
            kwargs["proxy"] = proxy
        self.client = httpx.Client(**kwargs)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "ProtocolHttpClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _write_trace(self, event: dict[str, Any]) -> None:
        if self.trace_dir is None:
            return
        self._seq += 1
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        path = self.trace_dir / f"{self.trace_name}.jsonl"
        payload = {"seq": self._seq, "ts": time.time(), **event}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self.backend == "curl_cffi":
            return self._curl_request(method, url, **kwargs)
        return self._httpx_request(method, url, **kwargs)

    def _httpx_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        request = self.client.build_request(method, url, **kwargs)
        self._write_trace(
            {
                "kind": "request",
                "method": request.method,
                "url": str(request.url),
                "headers": _safe_headers(request.headers, sensitive=self.trace_sensitive),
                "body": _body_record(request.content if request.content else None, limit=self.body_limit, sensitive=self.trace_sensitive),
            }
        )
        max_attempts = 3 if request.method in IDEMPOTENT_METHODS else 1
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.client.send(request)
                break
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                self._write_trace({"kind": "network_error", "method": request.method, "url": str(request.url), "attempt": attempt, "error": str(exc)})
                if attempt >= max_attempts:
                    raise
                time.sleep(0.8 * attempt)
        self._write_trace(
            {
                "kind": "response",
                "method": request.method,
                "url": str(response.url),
                "status": response.status_code,
                "headers": _safe_headers(response.headers, sensitive=self.trace_sensitive),
                "body": _body_record(response.content, limit=self.body_limit, sensitive=self.trace_sensitive),
            }
        )
        return response

    def _curl_request(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", None)
        data = kwargs.pop("data", None)
        json_payload = kwargs.pop("json", None)
        params = kwargs.pop("params", None)
        timeout = _effective_curl_timeout(method.upper(), kwargs.pop("timeout", self.timeout))
        request_url = _url_with_params(url, params)
        body = _body_preview(data=data, json_payload=json_payload)
        self._write_trace(
            {
                "kind": "request",
                "method": method.upper(),
                "url": request_url,
                "headers": _safe_headers(headers or {}, sensitive=self.trace_sensitive),
                "body": _body_record(body, limit=self.body_limit, sensitive=self.trace_sensitive),
            }
        )
        max_attempts = 3 if method.upper() in IDEMPOTENT_METHODS else 1
        for attempt in range(1, max_attempts + 1):
            try:
                raw = self.client.request(
                    method.upper(),
                    url,
                    params=params,
                    data=data,
                    json=json_payload,
                    headers=headers,
                    timeout=timeout,
                    allow_redirects=False,
                    proxy=self.proxy,
                    default_headers=False,
                    **kwargs,
                )
                response = _CurlCompatResponse(raw, method=method.upper(), url=request_url)
                break
            except Exception as exc:
                self._write_trace({"kind": "network_error", "method": method.upper(), "url": request_url, "attempt": attempt, "error": str(exc)})
                fallback_allowed = method.upper() in IDEMPOTENT_METHODS or _is_safe_curl_fallback_post(method, request_url)
                if (fallback_allowed and _is_fast_curl_fallback_error(exc)) or attempt >= max_attempts:
                    if fallback_allowed:
                        return self._httpx_fallback(method.upper(), url, params=params, data=data, json_payload=json_payload, headers=headers, timeout=timeout, original_error=exc)
                    raise
                time.sleep(0.8 * attempt)
        self._write_trace(
            {
                "kind": "response",
                "method": method.upper(),
                "url": str(response.url),
                "status": response.status_code,
                "headers": _safe_headers(response.headers, sensitive=self.trace_sensitive),
                "body": _body_record(response.content, limit=self.body_limit, sensitive=self.trace_sensitive),
            }
        )
        return response

    def _httpx_fallback(
        self,
        method: str,
        url: str,
        *,
        params: Any,
        data: Any,
        json_payload: Any,
        headers: Any,
        timeout: float,
        original_error: Exception,
    ) -> httpx.Response:
        self._write_trace({"kind": "transport_fallback", "method": method, "url": _url_with_params(url, params), "from": "curl_cffi", "to": "httpx", "reason": str(original_error)})
        kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": False, "http2": _HTTPX_HTTP2}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        with httpx.Client(**kwargs) as client:
            request = client.build_request(method, url, params=params, data=data, json=json_payload, headers=headers)
            response = client.send(request)
        self._write_trace(
            {
                "kind": "response",
                "method": method,
                "url": str(response.url),
                "status": response.status_code,
                "headers": _safe_headers(response.headers, sensitive=self.trace_sensitive),
                "body": _body_record(response.content, limit=self.body_limit, sensitive=self.trace_sensitive),
            }
        )
        return response

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)


def require_ok(response: httpx.Response) -> httpx.Response:
    if response.status_code >= 400:
        body = response.text if response.content else None
        raise ProtocolResponseError(response.request.method, str(response.request.url), response.status_code, body)
    return response


def json_or_empty(response: httpx.Response) -> dict[str, Any]:
    if not response.content:
        return {}
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


class _CurlCompatResponse:
    def __init__(self, raw: Any, *, method: str, url: str) -> None:
        self.raw = raw
        self.status_code = int(raw.status_code)
        self.headers = raw.headers
        self.content = raw.content or b""
        self.text = raw.text
        self.url = str(raw.url)
        self.request = SimpleNamespace(method=method, url=url)

    def json(self) -> Any:
        return self.raw.json()


def _url_with_params(url: str, params: Any) -> str:
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    if isinstance(params, str):
        return url + separator + params
    return url + separator + urlencode(params, doseq=True)


def _body_preview(*, data: Any, json_payload: Any) -> bytes | None:
    if json_payload is not None:
        return json.dumps(json_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if data is None:
        return None
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, dict):
        return urlencode(data, doseq=True).encode("utf-8")
    return str(data).encode("utf-8")


def _is_fast_curl_fallback_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in FAST_CURL_FALLBACK_MARKERS)


def _is_safe_curl_fallback_post(method: str, url: str) -> bool:
    return method.upper() == "POST" and any(path in url for path in SAFE_CURL_FALLBACK_POST_PATHS)


def _effective_curl_timeout(method: str, timeout: float) -> float:
    if method.upper() not in IDEMPOTENT_METHODS:
        return timeout
    try:
        cap = float(os.environ.get("PROTOCOL_CURL_GET_TIMEOUT_SECONDS", "30"))
    except ValueError:
        cap = 30.0
    return min(float(timeout), cap) if cap > 0 else timeout
