from __future__ import annotations

import contextlib
import base64
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from .models import BrowserProfile, ProtocolConfig
from .risk_tokens import RiskTokenBundle
from .chatgpt import SentinelHeaders


SENTINEL_BOOTSTRAP_URL = "https://sentinel.openai.com/backend-api/sentinel/sdk.js"
SENTINEL_FRAME_URL = "https://sentinel.openai.com/backend-api/sentinel/frame.html"
DEFAULT_SENTINEL_SDK_URL = "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js"
SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"

DEFAULT_FLOW_BY_PURPOSE = {
    "register": "oauth_create_account",
}


def decoded_vm_error(value: str) -> str:
    """Return a readable SDK VM error carried inside a base64 proof."""
    token = str(value or "").strip()
    if not token:
        return ""
    try:
        decoded = base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode("utf-8")
    except Exception:
        return ""
    if re.match(r"^\d+:\s*(?:TypeError|ReferenceError|Error):", decoded):
        return decoded[:300]
    return ""


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _is_transient_sentinel_transport_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "curl: (35)",
            "ssl_error_syscall",
            "ssl_connect",
            "tls connect error",
            "openssl_internal",
            "connection reset",
            "connection closed",
            "recv failure",
            "operation timed out",
            "connection timed out",
            "proxyerror",
        )
    )


class SentinelTransport(Protocol):
    def get_text(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        ...

    def post_json(self, url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None) -> dict[str, Any]:
        ...

    def cookie_header(self) -> str:
        ...

    def close(self) -> None:
        ...


def sentinel_headers(profile: BrowserProfile, *, referer: str | None = None, content_type: str | None = None) -> dict[str, str]:
    headers = profile.browser_headers(referer=referer, content_type=content_type)
    headers.update(
        {
            "accept": "*/*",
            "sec-fetch-site": "same-origin" if referer and "sentinel.openai.com" in referer else "cross-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
    )
    return headers


class HttpxSentinelTransport:
    def __init__(self, *, profile: BrowserProfile, proxy: str | None = None, timeout: float = 30.0) -> None:
        kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True, "headers": {"user-agent": profile.user_agent}}
        if proxy:
            kwargs["proxy"] = proxy
        self.client = httpx.Client(**kwargs)

    def get_text(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        response = self.client.get(url, headers=headers)
        response.raise_for_status()
        return response.text

    def post_json(self, url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"))
        response = self.client.post(url, content=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"Sentinel returned non-object JSON from {url}")
        return data

    def cookie_header(self) -> str:
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.client.cookies.jar)

    def close(self) -> None:
        self.client.close()


class CurlSentinelTransport:
    def __init__(self, *, profile: BrowserProfile, proxy: str | None = None, timeout: float = 30.0, cookie_jar: Path | None = None) -> None:
        self.profile = profile
        self.proxy = proxy
        self.timeout = timeout
        if cookie_jar is None:
            tmp = tempfile.NamedTemporaryFile(prefix="sentinel-cookies-", suffix=".txt", delete=False)
            tmp.close()
            self.cookie_jar = Path(tmp.name)
            self._owns_cookie_jar = True
        else:
            self.cookie_jar = cookie_jar
            self.cookie_jar.parent.mkdir(parents=True, exist_ok=True)
            self.cookie_jar.touch(exist_ok=True)
            self._owns_cookie_jar = False

    def _run(self, args: list[str], *, input_text: str | None = None) -> tuple[int, str]:
        base = [
            "curl",
            "-sS",
            "-L",
            "--max-time",
            str(int(self.timeout)),
            "--connect-timeout",
            str(max(3, min(10, int(self.timeout)))),
            "--retry",
            "2",
            "--retry-delay",
            "1",
            "--retry-max-time",
            str(int(self.timeout)),
            "--retry-all-errors",
            "-b",
            str(self.cookie_jar),
            "-c",
            str(self.cookie_jar),
            "-w",
            "\n%{http_code}",
        ]
        if self.proxy:
            base.extend(["--proxy", self.proxy])
        completed = subprocess.run(base + args, text=True, input=input_text, capture_output=True, timeout=self.timeout + 10)
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout)[:1000])
        body, _, status_text = completed.stdout.rpartition("\n")
        try:
            status = int(status_text.strip())
        except ValueError as exc:
            raise RuntimeError(f"curl did not return HTTP status: {completed.stdout[-200:]}") from exc
        return status, body

    @staticmethod
    def _header_args(headers: dict[str, str] | None) -> list[str]:
        args: list[str] = []
        for key, value in (headers or {}).items():
            args.extend(["-H", f"{key}: {value}"])
        return args

    def get_text(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        status, body = self._run(self._header_args(headers) + [url])
        if status >= 400:
            raise RuntimeError(f"GET {url} returned HTTP {status}: {body[:300]}")
        return body

    def post_json(self, url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"))
        status, response_body = self._run(
            self._header_args(headers) + ["-X", "POST", "--data-binary", "@-", url],
            input_text=body,
        )
        if status >= 400:
            raise RuntimeError(f"POST {url} returned HTTP {status}: {response_body[:300]}")
        data = json.loads(response_body)
        if not isinstance(data, dict):
            raise RuntimeError(f"Sentinel returned non-object JSON from {url}")
        return data

    def cookie_header(self) -> str:
        if not self.cookie_jar.exists():
            return ""
        cookies: list[str] = []
        for line in self.cookie_jar.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies.append(f"{parts[5]}={parts[6]}")
        return "; ".join(cookies)

    def close(self) -> None:
        if self._owns_cookie_jar:
            with contextlib.suppress(FileNotFoundError):
                self.cookie_jar.unlink()


class SentinelSdkExecutor:
    @staticmethod
    def _profile_payload(
        profile: BrowserProfile,
        *,
        page_url: str,
        referer: str,
    ) -> dict[str, Any]:
        return {
            "url": page_url,
            "referer": referer,
            "navigator": {
                "userAgent": profile.user_agent,
                "language": profile.language,
                "languages": [
                    profile.language,
                    profile.language.split("-", 1)[0],
                    "en-US",
                    "en",
                ],
                "platform": "Win32",
                "vendor": "Google Inc.",
                "hardwareConcurrency": 8,
                "deviceMemory": 8,
                "maxTouchPoints": 0,
            },
            "screen": {
                "width": 1920,
                "height": 1080,
                "availWidth": 1920,
                "availHeight": 1040,
                "colorDepth": 24,
                "pixelDepth": 24,
            },
            "historyLength": 3,
        }

    def proof(
        self,
        *,
        sdk_source: str,
        sdk_url: str,
        flow: str,
        profile: BrowserProfile,
        document_cookie: str,
        page_url: str = "https://auth.openai.com/about-you",
        referer: str = "https://auth.openai.com/email-verification",
    ) -> str:
        result = self._run_node(
            {
                "mode": "proof",
                "sdk": sdk_source,
                "sdkUrl": sdk_url,
                "flow": flow,
                "userAgent": profile.user_agent,
                "language": profile.language,
                "documentCookie": document_cookie,
                "profile": self._profile_payload(
                    profile,
                    page_url=page_url,
                    referer=referer,
                ),
            }
        )
        proof = result.get("proof")
        if not isinstance(proof, str) or not proof:
            raise RuntimeError(f"Sentinel SDK did not produce requirements proof: {result}")
        return proof

    def token(
        self,
        *,
        sdk_source: str,
        sdk_url: str,
        flow: str,
        profile: BrowserProfile,
        document_cookie: str,
        cached_chat_req: dict[str, Any],
        cached_proof: str,
        page_url: str = "https://auth.openai.com/about-you",
        referer: str = "https://auth.openai.com/email-verification",
    ) -> str:
        return self.solve(
            sdk_source=sdk_source,
            sdk_url=sdk_url,
            flow=flow,
            profile=profile,
            document_cookie=document_cookie,
            cached_chat_req=cached_chat_req,
            cached_proof=cached_proof,
            page_url=page_url,
            referer=referer,
        )["token"]

    def solve(
        self,
        *,
        sdk_source: str,
        sdk_url: str,
        flow: str,
        profile: BrowserProfile,
        document_cookie: str,
        cached_chat_req: dict[str, Any],
        cached_proof: str,
        behavior_duration_ms: int = 4200,
        page_url: str = "https://auth.openai.com/about-you",
        referer: str = "https://auth.openai.com/email-verification",
    ) -> dict[str, str]:
        result = self._run_node(
            {
                "mode": "token",
                "sdk": sdk_source,
                "sdkUrl": sdk_url,
                "flow": flow,
                "userAgent": profile.user_agent,
                "language": profile.language,
                "documentCookie": document_cookie,
                "cachedChatReq": cached_chat_req,
                "cachedProof": cached_proof,
                "behaviorDurationMs": max(0, int(behavior_duration_ms)),
                "profile": self._profile_payload(
                    profile,
                    page_url=page_url,
                    referer=referer,
                ),
            }
        )
        token = result.get("token")
        if not isinstance(token, str) or not token:
            raise RuntimeError(f"Sentinel SDK did not produce token: {result}")
        so_token = result.get("soToken")
        try:
            main_payload = json.loads(token)
        except Exception as exc:
            raise RuntimeError(f"Sentinel SDK returned invalid main token JSON: {exc}") from exc
        if not isinstance(main_payload, dict):
            raise RuntimeError("Sentinel SDK returned a non-object main token")
        expected = {
            "c": str(cached_chat_req.get("token") or ""),
            "id": str(profile.device_id or cookie_value(document_cookie, "oai-did") or ""),
            "flow": flow,
        }
        for key, value in expected.items():
            if value and str(main_payload.get(key) or "") != value:
                raise RuntimeError(f"Sentinel main token {key} binding mismatch")
        if not str(main_payload.get("p") or ""):
            raise RuntimeError("Sentinel main token is missing enforcement P")
        if (cached_chat_req.get("turnstile") or {}).get("required"):
            turnstile = str(main_payload.get("t") or "")
            if not turnstile:
                raise RuntimeError("Sentinel main token is missing required Turnstile proof")
            vm_error = decoded_vm_error(turnstile)
            if vm_error:
                raise RuntimeError(f"Sentinel Turnstile VM returned an error proof: {vm_error}")
        if (cached_chat_req.get("so") or {}).get("required"):
            if not isinstance(so_token, str) or not so_token:
                raise RuntimeError("Sentinel SDK is missing required SO token")
            try:
                so_payload = json.loads(so_token)
            except Exception as exc:
                raise RuntimeError(f"Sentinel SDK returned invalid SO token JSON: {exc}") from exc
            if not isinstance(so_payload, dict) or not str(so_payload.get("so") or ""):
                raise RuntimeError("Sentinel SDK returned an empty SO proof")
            for key, value in expected.items():
                if value and str(so_payload.get(key) or "") != value:
                    raise RuntimeError(f"Sentinel SO token {key} binding mismatch")
        return {
            "token": token,
            "so_token": so_token if isinstance(so_token, str) else "",
        }

    def _run_node(self, payload: dict[str, Any]) -> dict[str, Any]:
        worker = Path(__file__).resolve().parent.parent / "sentinel_vm" / "runtime_worker.js"
        if not worker.exists():
            raise RuntimeError(f"Sentinel runtime worker missing: {worker}")
        completed = subprocess.run(
            ["node", str(worker)],
            input=json.dumps(payload, separators=(",", ":")),
            text=True,
            capture_output=True,
            timeout=90,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout)[:1200])
        data = json.loads(completed.stdout)
        if not isinstance(data, dict):
            raise RuntimeError("Sentinel node VM returned non-object JSON")
        return data


@dataclass
class SentinelHttpTokenProvider:
    config: ProtocolConfig
    proxy: str | None = None
    transport_kind: str = "curl"
    transport: SentinelTransport | None = None
    executor: SentinelSdkExecutor | None = None
    device_id: str | None = None

    def get_openai_sentinel(self, *, purpose: str) -> RiskTokenBundle:
        flow = DEFAULT_FLOW_BY_PURPOSE.get(purpose)
        if not flow:
            raise ValueError(f"unknown OpenAI sentinel purpose: {purpose}")
        transport_kinds = [self.transport_kind]
        if self.transport is None and self.transport_kind == "curl" and _env_flag("SENTINEL_HTTPX_FALLBACK", default=True):
            transport_kinds.append("httpx")
        last_exc: BaseException | None = None
        for index, transport_kind in enumerate(transport_kinds):
            transport = self.transport or self._build_transport(transport_kind)
            close_transport = self.transport is None
            try:
                return self._get_openai_sentinel_with_transport(
                    purpose=purpose,
                    flow=flow,
                    transport=transport,
                    transport_kind=transport_kind,
                )
            except Exception as exc:
                last_exc = exc
                if index >= len(transport_kinds) - 1 or not _is_transient_sentinel_transport_error(exc):
                    raise
            finally:
                if close_transport:
                    with contextlib.suppress(Exception):
                        transport.close()
        if last_exc:
            raise last_exc
        raise RuntimeError("Sentinel token acquisition did not run")

    def _get_openai_sentinel_with_transport(
        self,
        *,
        purpose: str,
        flow: str,
        transport: SentinelTransport,
        transport_kind: str,
    ) -> RiskTokenBundle:
        sdk_url, sdk_source = self._load_sdk(transport)
        document_cookie = merge_cookie_header(
            transport.cookie_header(),
            {"oai-did": self.device_id or self.config.profile.device_id or str(uuid.uuid4())},
        )
        executor = self.executor or SentinelSdkExecutor()
        proof = executor.proof(
            sdk_source=sdk_source,
            sdk_url=sdk_url,
            flow=flow,
            profile=self.config.profile,
            document_cookie=document_cookie,
        )
        init_payload = {"p": proof, "id": cookie_value(document_cookie, "oai-did"), "flow": flow}
        init_payload = {key: value for key, value in init_payload.items() if value is not None}
        cached_chat_req = transport.post_json(
            SENTINEL_REQ_URL,
            init_payload,
            headers=sentinel_headers(
                self.config.profile,
                referer=f"{SENTINEL_FRAME_URL}?sv={sentinel_version_from_url(sdk_url) or ''}",
                content_type="text/plain;charset=UTF-8",
            )
            | {"origin": "https://sentinel.openai.com"},
        )
        solved = executor.solve(
            sdk_source=sdk_source,
            sdk_url=sdk_url,
            flow=flow,
            profile=self.config.profile,
            document_cookie=document_cookie,
            cached_chat_req=cached_chat_req,
            cached_proof=proof,
        )
        return RiskTokenBundle(
            sentinel=SentinelHeaders(
                token=solved["token"],
                so_token=solved.get("so_token") or None,
            ),
            source=f"sentinel-http:{transport_kind}",
            purpose=purpose,
            request_url="https://auth.openai.com/api/accounts/create_account",
            captured_headers={
                "openai-sentinel-token": solved["token"],
                **(
                    {"openai-sentinel-so-token": solved["so_token"]}
                    if solved.get("so_token") else {}
                ),
            },
        )

    def _build_transport(self, transport_kind: str | None = None) -> SentinelTransport:
        kind = transport_kind or self.transport_kind
        if kind == "httpx":
            return HttpxSentinelTransport(profile=self.config.profile, proxy=self.proxy, timeout=self.config.timeout)
        if kind == "curl":
            return CurlSentinelTransport(profile=self.config.profile, proxy=self.proxy, timeout=self.config.timeout)
        raise ValueError(f"unknown Sentinel transport: {kind}")

    def _load_sdk(self, transport: SentinelTransport) -> tuple[str, str]:
        bootstrap = transport.get_text(
            SENTINEL_BOOTSTRAP_URL,
            headers=sentinel_headers(self.config.profile, referer="https://auth.openai.com/"),
        )
        sdk_url = extract_sentinel_sdk_url(bootstrap) or DEFAULT_SENTINEL_SDK_URL
        sdk_source = transport.get_text(
            sdk_url,
            headers=sentinel_headers(self.config.profile, referer="https://auth.openai.com/"),
        )
        return sdk_url, sdk_source


def extract_sentinel_sdk_url(source: str) -> str | None:
    match = re.search(r"https://sentinel\.openai\.com/sentinel/[^'\"<>]+/sdk\.js", source)
    return match.group(0) if match else None


def sentinel_version_from_url(url: str) -> str | None:
    match = re.search(r"/sentinel/([^/]+)/sdk\.js", url)
    return match.group(1) if match else None


def merge_cookie_header(cookie_header: str, extra: dict[str, str]) -> str:
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if key:
            cookies[key] = value
    cookies.update({key: value for key, value in extra.items() if value})
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def cookie_value(cookie_header: str, name: str) -> str | None:
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if key == name:
            return value
    return None


NODE_SENTINEL_VM = r"""
const vm = require('node:vm');
const { TextEncoder } = require('node:util');
const cryptoMod = require('node:crypto');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const listeners = {message: []};
let iframeObj = null;
let capturedProof = null;
const context = {
  console: {log(){}, error(){}, warn(){}},
  setTimeout, clearTimeout, Promise, URL, URLSearchParams,
  Math, Date, JSON, Array, Object, String, Number, Error, Map, WeakMap,
  Uint8Array, TextEncoder,
  btoa: (s) => Buffer.from(String(s), 'binary').toString('base64'),
  atob: (s) => Buffer.from(String(s), 'base64').toString('binary'),
  unescape, encodeURIComponent, decodeURIComponent,
  crypto: cryptoMod.webcrypto,
  performance: {
    now: () => performance.now(),
    timeOrigin: performance.timeOrigin,
    memory: {jsHeapSizeLimit: 4294967296},
  },
  screen: {width: 1365, height: 900},
  navigator: {
    userAgent: input.userAgent,
    language: input.language || 'ja-JP',
    languages: [input.language || 'ja-JP', 'ja', 'en-US', 'en'],
    hardwareConcurrency: 4,
  },
};
context.window = context;
context.globalThis = context;
context.top = context;
context.location = {href: 'https://auth.openai.com/about-you', search: ''};
context.addEventListener = (type, cb) => { (listeners[type] ||= []).push(cb); };
context.postMessage = () => {};
context.document = {
  cookie: input.documentCookie || '',
  currentScript: {src: input.sdkUrl},
  scripts: [{src: input.sdkUrl}],
  documentElement: {getAttribute: () => null},
  body: {
    appendChild: (el) => {
      setTimeout(() => { (el._load || []).forEach((cb) => cb()); }, 0);
      return el;
    },
  },
  createElement: (tag) => {
    if (tag !== 'iframe') return {style: {}, addEventListener(){}};
    iframeObj = {
      style: {},
      _load: [],
      addEventListener: (type, cb) => {
        if (type === 'load') iframeObj._load.push(cb);
      },
    };
    iframeObj.contentWindow = {
      postMessage: async (msg, origin) => {
        capturedProof = msg.p;
        const result = input.mode === 'token'
          ? {cachedChatReq: input.cachedChatReq, cachedProof: input.cachedProof || msg.p}
          : null;
        const event = {source: iframeObj.contentWindow, data: {type: 'response', requestId: msg.requestId, result}, origin};
        for (const cb of listeners.message || []) cb(event);
      },
    };
    return iframeObj;
  },
};

vm.createContext(context);
vm.runInContext(input.sdk, context, {timeout: 5000});

(async () => {
  if (input.mode === 'proof') {
    await context.SentinelSDK.init(input.flow);
    console.log(JSON.stringify({proof: capturedProof}));
    return;
  }
  const token = await context.SentinelSDK.token(input.flow);
  console.log(JSON.stringify({token}));
})().catch((err) => {
  console.error(err && err.stack || String(err));
  process.exit(2);
});
"""
