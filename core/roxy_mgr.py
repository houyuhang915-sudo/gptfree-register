"""RoxyBrowser 指纹浏览器适配（替代 BitBrowser）。

RoxyBrowser API 文档: https://roxybrowser.com/docs/api-documentation/api-endpoint.html
- 默认端口: 127.0.0.1:50000
- 鉴权: 请求 header 加 ``token: <你的 API Token>``（在 RoxyBrowser 客户端 → 设置 → 开 API 后复制）

config.local.py 需要的配置:
    ROXY_API_PORT = 50000
    ROXY_API_TOKEN = "xxxx"          # 在 RoxyBrowser 客户端复制
    ROXY_WORKSPACE_ID = 1            # 工作区 ID（首次跑可以用 list_workspaces() 查）
    ROXY_DEFAULT_PROXY = {           # 同 BitBrowser 的 BITBROWSER_PROXY 格式
        "host": "global.rotgb.711proxy.com",
        "port": 10000,
        "user": "USER674021-zone-custom-region-US",
        "password": "81a96c",
        "proxyType": "SOCKS5",       # 大写！roxy 接受 HTTP/HTTPS/SOCKS5
    }
"""
from __future__ import annotations

import base64
import json
import logging
import os
import select
import shutil
import signal
import socket
import subprocess
import re
import struct
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from DrissionPage import ChromiumPage, ChromiumOptions

import config

log = logging.getLogger("roxy_mgr")

_LOCAL_PROXY_BRIDGES: dict[str, "_Socks5HttpBridge"] = {}
_DOH_IPV4_CACHE: dict[str, tuple[float, list[str]]] = {}
_DOH_IPV4_CACHE_LOCK = threading.Lock()
_LUMI_CONFIG_KEY = b"402ead7d23b43b6d1e0528d4f99c59bd"
_LUMI_CONFIG_IV = b"3a105229aa31"


class RoxyBrowserError(RuntimeError):
    pass


class _Socks5ConnectError(RuntimeError):
    def __init__(self, code: int, host: str):
        super().__init__(f"SOCKS5 connect failed for {host}: REP={code}")
        self.code = code
        self.host = host


# =============================================================================
# 与 browser_mgr 共享的工具（指纹模板 / sticky session 注入）
# =============================================================================
try:
    from browser_mgr import (
        _IOS_USER_AGENTS,
        _ANDROID_USER_AGENTS,
        _inject_sticky_session,
        _resolve_fingerprint_profile,
    )
except Exception:
    _IOS_USER_AGENTS = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    )
    _ANDROID_USER_AGENTS = (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
    )

    def _inject_sticky_session(proxy_dict, sess_time_min=5):
        return proxy_dict

    def _resolve_fingerprint_profile():
        return "auto"


# =============================================================================
# RoxyBrowser HTTP API 客户端
# =============================================================================
def _api_base() -> str:
    port = int(getattr(config, "ROXY_API_PORT", 50000))
    return f"http://127.0.0.1:{port}"


def _api_headers() -> dict:
    token = str(getattr(config, "ROXY_API_TOKEN", "") or "").strip()
    if not token:
        raise RoxyBrowserError(
            "config.ROXY_API_TOKEN 未设置。请在 RoxyBrowser 客户端 → 设置 → 开启 API → 复制 token，"
            "然后写入 config.local.py 的 ROXY_API_TOKEN。"
        )
    return {"Content-Type": "application/json", "token": token}


def _post(path: str, body: dict, timeout: int = 30) -> dict:
    r = requests.post(
        _api_base() + path,
        json=body,
        headers=_api_headers(),
        timeout=timeout,
        proxies={"http": None, "https": None},
    )
    try:
        return r.json()
    except Exception:
        raise RoxyBrowserError(f"roxy api {path} 返回非 JSON: {r.status_code} {r.text[:300]}")


def _get(path: str, params: dict | None = None, timeout: int = 30) -> dict:
    r = requests.get(
        _api_base() + path,
        params=params or {},
        headers=_api_headers(),
        timeout=timeout,
        proxies={"http": None, "https": None},
    )
    try:
        return r.json()
    except Exception:
        raise RoxyBrowserError(f"roxy api {path} 返回非 JSON: {r.status_code} {r.text[:300]}")


def _is_success_response(data: dict) -> bool:
    return isinstance(data, dict) and data.get("code") in (0, "0", None)


def _response_rows(data: dict) -> list[dict]:
    def walk(value: Any) -> list[dict]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if not isinstance(value, dict):
            return []
        for key in ("rows", "list", "items", "data"):
            rows = walk(value.get(key))
            if rows:
                return rows
        if any(value.get(key) for key in ("dirId", "id", "http", "ws", "websocket", "driver", "webdriver", "webdriver_path")):
            return [value]
        return []

    if isinstance(data, dict) and "data" in data:
        rows = walk(data.get("data"))
        if rows:
            return rows
    return walk(data)


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def roxy_health() -> bool:
    """检查 RoxyBrowser API 可达。"""
    try:
        data = _get("/health", timeout=8)
        return _is_success_response(data)
    except Exception as e:
        log.debug(f"  [Roxy] health err: {e}")
        return False


def roxy_workspace_id() -> int:
    """从 config 取 workspace id；没配则取列表第一个。"""
    wid = int(getattr(config, "ROXY_WORKSPACE_ID", 0) or 0)
    if wid > 0:
        return wid
    try:
        data = _get("/browser/workspace", {"page_index": 1, "page_size": 1000})
        rows = _response_rows(data)
        if rows:
            wid = int(rows[0].get("id") or rows[0].get("workspaceId") or 1)
            log.info(f"  [Roxy] 自动选 workspace id={wid}")
            return wid
        msg = (data or {}).get("msg") or (data or {}).get("message") or ""
        if msg:
            log.warning(f"  [Roxy] workspace 列表不可用: {msg}，回退 workspaceId=1")
    except Exception as e:
        log.warning(f"  [Roxy] workspace 列表不可用: {e}，回退 workspaceId=1")
    return 1


# =============================================================================
# Profile 创建 / 打开 / 关闭 / 删除（API 跟 bb_* 对齐）
# =============================================================================
def roxy_list_profiles(workspace_id: int = 1) -> list[dict]:
    rows: list[dict] = []
    for wid in (workspace_id, 0, roxy_workspace_id()):
        try:
            data = _get("/browser/list", {"workspaceId": wid, "page_index": 1, "page_size": 1000}, timeout=20)
        except Exception:
            continue
        rows = _response_rows(data)
        if rows:
            return rows
    return rows


def roxy_profile_meta(dir_id: str) -> dict:
    normalized_id = str(dir_id or "").strip()
    if not normalized_id:
        return {}
    for row in roxy_list_profiles():
        row_id = _first_text(row.get("dirId"), row.get("dir_id"), row.get("id"))
        if row_id == normalized_id:
            return row
    return {}


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _roxy_browser_root() -> Path:
    return Path.home() / "Library/Application Support/RoxyBrowser"


def _roxy_profile_dir(dir_id: str) -> Path:
    return _roxy_browser_root() / "browser-cache" / str(dir_id or "").strip()


def _roxy_chrome_executable(core_version: str = "") -> Path:
    root = _roxy_browser_root() / "chrome-bin"
    version_hint = str(core_version or "").split(".", 1)[0].strip()
    candidates: list[Path] = []
    if version_hint:
        candidates.append(root / version_hint / "RoxyChrome.app/Contents/MacOS/RoxyChrome")
    for version_dir in sorted((p for p in root.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: p.name, reverse=True):
        candidates.append(version_dir / "RoxyChrome.app/Contents/MacOS/RoxyChrome")
    for path in candidates:
        if path.exists():
            return path
    raise RoxyBrowserError("本地 RoxyChrome 内核不存在，请先在 RoxyBrowser 中安装一次浏览器内核")


def _wait_for_debug_port(port: int, timeout_seconds: float = 25.0) -> None:
    deadline = time.time() + max(1.0, float(timeout_seconds or 25.0))
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
            if response.status_code < 400:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.5)
    raise RoxyBrowserError(f"本地 RoxyChrome 已启动但 DevTools 端口不可达: {last_error}")


def _debug_addr_if_reachable(port: str | int) -> str:
    port_text = str(port or "").strip()
    if not port_text.isdigit():
        return ""
    try:
        response = requests.get(f"http://127.0.0.1:{port_text}/json/version", timeout=2)
        if response.status_code < 400:
            return f"127.0.0.1:{port_text}"
    except Exception:
        return ""
    return ""


def _local_roxychrome_processes(profile_dir: Path) -> list[tuple[int, str]]:
    profile_text = str(profile_dir)
    try:
        output = subprocess.check_output(["ps", "axo", "pid=,command="], text=True, timeout=5)
    except Exception:
        return []
    processes: list[tuple[int, str]] = []
    for line in output.splitlines():
        if "RoxyChrome" not in line or profile_text not in line:
            continue
        pid_text, _, command = line.strip().partition(" ")
        if not pid_text.isdigit():
            continue
        processes.append((int(pid_text), command))
    return processes


def _running_local_debug_addr(profile_dir: Path) -> str:
    for _pid, command in _local_roxychrome_processes(profile_dir):
        if "--remote-debugging-port=" not in command:
            continue
        match = re.search(r"--remote-debugging-port=(\d+)", command)
        if not match:
            continue
        debug_addr = _debug_addr_if_reachable(match.group(1))
        if debug_addr:
            return debug_addr
    return ""


def roxy_close_local_profile(dir_id: str) -> bool:
    normalized_id = str(dir_id or "").strip()
    if not normalized_id:
        return False
    profile_dir = _roxy_profile_dir(normalized_id)
    processes = _local_roxychrome_processes(profile_dir)
    ok = False
    for pid, command in processes:
        if "--type=" in command:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            ok = True
        except Exception:
            continue
    _close_local_proxy_bridge(normalized_id)
    return ok


class _Socks5HttpBridge:
    """Expose an authenticated SOCKS5 upstream as a local HTTP proxy for Chrome."""

    def __init__(self, proxy: dict):
        self.proxy = proxy
        self.server: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.url = ""

    def start(self) -> "_Socks5HttpBridge":
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(64)
        self.url = f"http://127.0.0.1:{self.server.getsockname()[1]}"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self

    def close(self) -> None:
        self.stop_event.set()
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass

    def _serve(self) -> None:
        assert self.server is not None
        while not self.stop_event.is_set():
            try:
                client, _addr = self.server.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            first = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
            parts = first.split()
            if len(parts) < 3:
                return
            method, target = parts[0].upper(), parts[1]
            upstream = self._connect_socks(target if method == "CONNECT" else self._plain_target(target, head))
            if method == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                upstream.sendall(self._rewrite_plain(head, target))
            self._relay(client, upstream)
        except Exception as exc:
            log.warning(f"  [Roxy] 本地代理桥连接失败: {exc}")
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except Exception:
                pass
        finally:
            for sock in (client, upstream):
                try:
                    if sock:
                        sock.close()
                except Exception:
                    pass

    def _connect_socks(self, target: str) -> socket.socket:
        host, port = target.rsplit(":", 1)
        try:
            return self._connect_socks_once(host, int(port))
        except _Socks5ConnectError as exc:
            enabled = bool(getattr(config, "ROXY_SOCKS_REJECT_DOH_FALLBACK", False))
            if exc.code != 2 or not enabled or self._is_ip_address(host):
                raise

        resolved = _resolve_doh_ipv4(host)
        if not resolved:
            raise _Socks5ConnectError(2, host)
        log.warning(
            f"  [Roxy] SOCKS 上游拒绝域名 {host}，改用 DoH 地址 "
            f"{resolved[0]}（TLS SNI 保持原域名）"
        )
        last_error: Exception | None = None
        for ip in resolved:
            try:
                return self._connect_socks_once(ip, int(port))
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise _Socks5ConnectError(2, host)

    def _connect_socks_once(self, host: str, port: int) -> socket.socket:
        upstream = socket.create_connection((self.proxy["host"], int(self.proxy["port"])), timeout=30)
        try:
            user = str(self.proxy.get("user") or "").encode()
            pwd = str(self.proxy.get("password") or "").encode()
            upstream.sendall(b"\x05\x01\x02")
            if self._recv_exact(upstream, 2) != b"\x05\x02":
                raise RuntimeError("SOCKS5 auth method rejected")
            upstream.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(pwd)]) + pwd)
            if self._recv_exact(upstream, 2) != b"\x01\x00":
                raise RuntimeError("SOCKS5 auth failed")

            try:
                address = b"\x01" + socket.inet_pton(socket.AF_INET, host)
            except OSError:
                try:
                    address = b"\x04" + socket.inet_pton(socket.AF_INET6, host)
                except OSError:
                    host_b = host.encode("idna")
                    address = b"\x03" + bytes([len(host_b)]) + host_b
            upstream.sendall(b"\x05\x01\x00" + address + struct.pack("!H", port))
            resp = self._recv_exact(upstream, 4)
            if resp[1] != 0:
                raise _Socks5ConnectError(resp[1], host)
            atyp = resp[3]
            if atyp == 1:
                self._recv_exact(upstream, 4)
            elif atyp == 3:
                self._recv_exact(upstream, self._recv_exact(upstream, 1)[0])
            elif atyp == 4:
                self._recv_exact(upstream, 16)
            self._recv_exact(upstream, 2)
            return upstream
        except Exception:
            upstream.close()
            raise

    @staticmethod
    def _recv_exact(sock: socket.socket, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise RuntimeError("SOCKS5 upstream closed the connection")
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def _is_ip_address(host: str) -> bool:
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                socket.inet_pton(family, host)
                return True
            except OSError:
                continue
        return False

    @staticmethod
    def _plain_target(target: str, head: bytes) -> str:
        if target.startswith(("http://", "https://")):
            from urllib.parse import urlparse
            parsed = urlparse(target)
            return f"{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"host:"):
                return line.split(b":", 1)[1].strip().decode("latin1")
        return target

    @staticmethod
    def _rewrite_plain(head: bytes, target: str) -> bytes:
        if not target.startswith(("http://", "https://")):
            return head
        from urllib.parse import urlparse
        parsed = urlparse(target)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        lines = head.split(b"\r\n")
        parts = lines[0].split()
        lines[0] = b" ".join([parts[0], path.encode("latin1"), parts[2]])
        return b"\r\n".join(lines)

    @staticmethod
    def _relay(left: socket.socket, right: socket.socket) -> None:
        while True:
            readable, _, _ = select.select([left, right], [], [], 60)
            if not readable:
                return
            for src in readable:
                dst = right if src is left else left
                data = src.recv(65536)
                if not data:
                    return
                dst.sendall(data)


def _resolve_doh_ipv4(host: str) -> list[str]:
    normalized = str(host or "").strip().lower()
    now = time.time()
    with _DOH_IPV4_CACHE_LOCK:
        cached = _DOH_IPV4_CACHE.get(normalized)
        if cached and cached[0] > now:
            return list(cached[1])
    try:
        session = requests.Session()
        session.trust_env = False
        response = session.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": normalized, "type": "A"},
            headers={"accept": "application/dns-json"},
            timeout=10,
        )
        response.raise_for_status()
        answers = response.json().get("Answer") or []
        addresses = [
            str(answer.get("data") or "").strip()
            for answer in answers
            if answer.get("type") == 1 and _Socks5HttpBridge._is_ip_address(str(answer.get("data") or ""))
        ]
    except Exception as exc:
        log.warning(f"  [Roxy] DoH 解析 {normalized} 失败: {exc}")
        return []
    with _DOH_IPV4_CACHE_LOCK:
        _DOH_IPV4_CACHE[normalized] = (now + 300, addresses)
    return addresses


def _proxy_needs_local_bridge(proxy: dict | None) -> bool:
    if not proxy or not proxy.get("host") or not proxy.get("port"):
        return False
    protocol = str(proxy.get("proxyType") or proxy.get("protocol") or "").strip().lower()
    return protocol in ("socks5", "socks5h")


def _local_proxy_bridge_is_live(dir_id: str) -> bool:
    bridge = _LOCAL_PROXY_BRIDGES.get(str(dir_id or "").strip())
    if not bridge or not bridge.server or not bridge.thread:
        return False
    try:
        return bridge.server.fileno() >= 0 and bridge.thread.is_alive()
    except Exception:
        return False


def _start_local_proxy_bridge(dir_id: str, proxy: dict) -> dict:
    normalized_id = str(dir_id or "").strip()
    _close_local_proxy_bridge(normalized_id)
    bridge = _Socks5HttpBridge(dict(proxy)).start()
    _LOCAL_PROXY_BRIDGES[normalized_id] = bridge
    port = int(bridge.url.rsplit(":", 1)[1])
    log.info(f"  [Roxy] 已启动本地 HTTP/SOCKS 桥 dirId={normalized_id} port={port}")
    return {
        "proxyType": "HTTP",
        "protocol": "HTTP",
        "host": "127.0.0.1",
        "port": port,
        "user": "",
        "password": "",
    }


def _close_local_proxy_bridge(dir_id: str) -> None:
    bridge = _LOCAL_PROXY_BRIDGES.pop(str(dir_id or "").strip(), None)
    if bridge:
        bridge.close()


def _read_lumi_config(profile_dir: Path) -> dict:
    path = profile_dir / "lumi.conf"
    if not path.exists():
        return {}
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        encrypted = base64.b64decode(path.read_bytes())
        payload = AESGCM(_LUMI_CONFIG_KEY).decrypt(_LUMI_CONFIG_IV, encrypted, None)
        data = json.loads(payload)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        raise RoxyBrowserError(f"读取 Roxy lumi.conf 失败: {exc}") from exc


def _write_lumi_config(profile_dir: Path, data: dict) -> None:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        path = profile_dir / "lumi.conf"
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encrypted = AESGCM(_LUMI_CONFIG_KEY).encrypt(_LUMI_CONFIG_IV, payload, None)
        temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp_path.write_bytes(base64.b64encode(encrypted))
        temp_path.chmod(0o600)
        temp_path.replace(path)
    except Exception as exc:
        raise RoxyBrowserError(f"写入 Roxy lumi.conf 失败: {exc}") from exc


def _patch_lumi_proxy(profile_dir: Path, proxy: dict | None) -> bool:
    """Update RoxyChrome's native proxy config, which overrides Chromium flags."""
    path = profile_dir / "lumi.conf"
    if not path.exists():
        return False
    data = _read_lumi_config(profile_dir)
    if proxy and proxy.get("host") and proxy.get("port"):
        protocol = str(proxy.get("proxyType") or proxy.get("protocol") or "socks5").lower()
        data["fproxy"] = {
            "type": protocol,
            "host": str(proxy["host"]).strip(),
            "port": int(proxy["port"]),
            "username": str(proxy.get("user") or ""),
            "password": str(proxy.get("password") or ""),
            "proxyByPassList": "",
        }
    else:
        data.pop("fproxy", None)
    _write_lumi_config(profile_dir, data)
    return True


def _patch_lumi_mobile_fingerprint(profile_dir: Path, fingerprint_profile: str) -> bool:
    """Keep Roxy's native navigator/screen config aligned with the mobile launch."""
    path = profile_dir / "lumi.conf"
    if not path.exists():
        return False
    profile = str(fingerprint_profile or "iphone").strip().lower()
    if profile not in ("iphone", "ios", "android"):
        return False

    data = _read_lumi_config(profile_dir)
    navigator = data.get("navigator") if isinstance(data.get("navigator"), dict) else {}
    screen = data.get("screen") if isinstance(data.get("screen"), dict) else {}
    if profile in ("iphone", "ios"):
        navigator.update({"platform": "iPhone", "maxTouchPoints": 5, "plugins": []})
        screen.update({
            "pixelDepth": 24,
            "colorDepth": 24,
            "width": 393,
            "height": 852,
            "availWidth": 393,
            "availHeight": 814,
            "devicePixelRatio": 3,
        })
    else:
        navigator.update({"platform": "Linux armv81", "maxTouchPoints": 5, "plugins": []})
        screen.update({
            "pixelDepth": 24,
            "colorDepth": 24,
            "width": 412,
            "height": 915,
            "availWidth": 412,
            "availHeight": 875,
            "devicePixelRatio": 2.625,
        })
    data["navigator"] = navigator
    data["screen"] = screen
    _write_lumi_config(profile_dir, data)
    return True


def _local_profile_has_live_proxy(dir_id: str, profile_dir: Path) -> bool:
    """Return whether a directly launched local profile still has a usable proxy."""
    try:
        fproxy = _read_lumi_config(profile_dir).get("fproxy") or {}
    except RoxyBrowserError:
        fproxy = {}
    if isinstance(fproxy, dict) and fproxy.get("host") and fproxy.get("port"):
        host = str(fproxy["host"]).strip()
        port = int(fproxy["port"])
        if host not in ("127.0.0.1", "localhost"):
            return True
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except Exception:
            pass

    bridge = _LOCAL_PROXY_BRIDGES.get(str(dir_id or "").strip())
    bridge_endpoint = ""
    if bridge and bridge.server and bridge.thread:
        try:
            if bridge.server.fileno() >= 0 and bridge.thread.is_alive():
                bridge_endpoint = bridge.url.removeprefix("http://").removeprefix("https://").rstrip("/")
        except Exception:
            bridge_endpoint = ""

    for _pid, command in _local_roxychrome_processes(profile_dir):
        if "--type=" in command:
            continue
        match = re.search(r"--proxy-server=(?:https?://)?([^\s]+)", command)
        if not match:
            continue
        endpoint = match.group(1).rstrip("/")
        if endpoint.startswith(("127.0.0.1:", "localhost:")):
            if bridge_endpoint and endpoint != bridge_endpoint:
                continue
            try:
                host, port = endpoint.rsplit(":", 1)
                with socket.create_connection((host, int(port)), timeout=0.4):
                    return True
            except Exception:
                continue
        return True
    return False


def _local_proxy_args(dir_id: str, proxy: dict | None) -> list[str]:
    normalized_id = str(dir_id or "").strip()
    _close_local_proxy_bridge(normalized_id)
    effective_proxy = _inject_sticky_session(dict(proxy)) if proxy and proxy.get("host") else None
    profile_dir = _roxy_profile_dir(normalized_id)
    if effective_proxy and _proxy_needs_local_bridge(effective_proxy):
        bridge_proxy = _start_local_proxy_bridge(normalized_id, effective_proxy)
        if _patch_lumi_proxy(profile_dir, bridge_proxy):
            log.info(
                f"  [Roxy] 已更新 lumi.conf 使用本地 HTTP 桥 "
                f"{bridge_proxy['host']}:{bridge_proxy['port']}"
            )
            return []
    if _patch_lumi_proxy(profile_dir, effective_proxy):
        if effective_proxy:
            protocol = str(effective_proxy.get("proxyType") or effective_proxy.get("protocol") or "socks5").upper()
            log.info(
                f"  [Roxy] 已更新 lumi.conf 原生代理: {protocol} "
                f"{effective_proxy.get('host')}:{effective_proxy.get('port')}"
            )
        else:
            log.info("  [Roxy] 已清除 lumi.conf 原生代理")
        return []
    return _proxy_args(effective_proxy)


def _proxy_args(proxy: dict | None) -> list[str]:
    if not proxy or not proxy.get("host"):
        return []
    protocol = str(proxy.get("proxyType") or proxy.get("protocol") or "socks5").lower()
    if protocol == "socks5":
        protocol = "socks5"
    host = str(proxy.get("host") or "").strip()
    port = str(proxy.get("port") or "").strip()
    if not host or not port:
        return []
    args = [f"--proxy-server={protocol}://{host}:{port}"]
    user = str(proxy.get("user") or "").strip()
    pwd = str(proxy.get("password") or "").strip()
    if user or pwd:
        args.append(f"--proxy-auth={user}:{pwd}")
    return args


def _local_chrome_args(profile_dir: Path, port: int, *, dir_id: str = "", profile: str = "iphone", proxy: dict | None = None, width: str = "1000", height: str = "1000") -> list[str]:
    normalized_profile = str(profile or "iphone").strip().lower()
    ua = _IOS_USER_AGENTS[0] if normalized_profile in ("iphone", "ios") else _ANDROID_USER_AGENTS[0]
    _patch_lumi_mobile_fingerprint(profile_dir, normalized_profile)
    args = [
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={width},{height}",
        f"--user-agent={ua}",
    ]
    if normalized_profile in ("iphone", "ios"):
        args.extend([
            "--lumi-platform=iPhone",
            "--touch-events=enabled",
            "--lang=en-US",
            "--time-zone-for-testing=America/New_York",
        ])
    elif normalized_profile == "android":
        args.extend([
            "--lumi-platform=Android",
            "--touch-events=enabled",
        ])
    args.extend(_local_proxy_args(dir_id, proxy))
    return args


def roxy_create_local_profile(name: str = "", proxy: dict | None = None, *, fingerprint_profile: str = "iphone") -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip()).strip("_") or "roxy_local"
    dir_id = f"local-claude-{int(time.time())}-{safe_name[:24]}"
    profile_dir = _roxy_profile_dir(dir_id)
    template_id = str(getattr(config, "ROXY_PROFILE_ID", "") or "").strip()
    template_dir = _roxy_profile_dir(template_id) if template_id else Path("")
    if template_dir.exists():
        def ignore(_dir: str, names: list[str]) -> set[str]:
            return {
                name for name in names
                if name in {
                    "DevToolsActivePort",
                    "Current Session",
                    "Current Tabs",
                    "Last Session",
                    "Last Tabs",
                    "RunningChromeVersion",
                    "Session Storage",
                    "Sessions",
                    "SingletonLock",
                    "SingletonSocket",
                    "SingletonCookie",
                    "lockfile",
                    "roxy_tabs",
                }
                or name.endswith(("-journal", "-wal", "-shm", ".tmp"))
                or name.startswith(("Session_", "Singleton", "Tabs_", ".org.chromium."))
            }

        def copy_existing(src: str, dst: str, *, follow_symlinks: bool = True) -> str:
            try:
                return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)
            except FileNotFoundError:
                log.debug(f"  [Roxy] 模板运行时文件已消失，跳过: {src}")
                return dst

        try:
            shutil.copytree(template_dir, profile_dir, ignore=ignore, copy_function=copy_existing)
        except shutil.Error as exc:
            missing_only = True
            for item in exc.args[0] if exc.args else []:
                message = str(item[2] if len(item) >= 3 else item)
                if "No such file or directory" not in message:
                    missing_only = False
                    break
            if not missing_only:
                raise
            log.debug(f"  [Roxy] 模板复制时跳过已消失运行时文件: {exc}")
    else:
        profile_dir.mkdir(parents=True, exist_ok=False)
    return dir_id


def roxy_delete_local_profile(dir_id: str) -> bool:
    normalized_id = str(dir_id or "").strip()
    if not normalized_id.startswith("local-claude-"):
        return False
    roxy_close_local_profile(normalized_id)
    profile_dir = _roxy_profile_dir(normalized_id)
    try:
        shutil.rmtree(profile_dir)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def roxy_open_local_profile(dir_id: str, proxy: dict | None = None, fingerprint_profile: str = "iphone") -> tuple[str, str]:
    normalized_id = str(dir_id or "").strip()
    if not normalized_id:
        raise RoxyBrowserError("open local profile 缺少 dirId")
    profile_dir = _roxy_profile_dir(normalized_id)
    if not profile_dir.exists():
        raise RoxyBrowserError(f"Roxy local profile 目录不存在: {profile_dir}")
    running_debug = _running_local_debug_addr(profile_dir)
    if running_debug:
        proxy_required = bool(proxy and proxy.get("host"))
        bridge_live = not _proxy_needs_local_bridge(proxy) or _local_proxy_bridge_is_live(normalized_id)
        if (not proxy_required or _local_profile_has_live_proxy(normalized_id, profile_dir)) and bridge_live:
            log.info(f"  [Roxy] local profile {normalized_id} 已由本地 RoxyChrome 打开，直接附加 debug={running_debug}")
            return running_debug, ""
        log.warning(
            f"  [Roxy] local profile {normalized_id} 的浏览器仍在运行，但代理桥已失效；"
            "关闭遗留进程并重建代理"
        )
        roxy_close_local_profile(normalized_id)
        for _ in range(20):
            if not _running_local_debug_addr(profile_dir):
                break
            time.sleep(0.1)
    meta = roxy_profile_meta(normalized_id)
    exe = _roxy_chrome_executable(str(meta.get("coreVersion") or ""))
    port = _pick_free_port()
    finger = meta.get("fingerInfo") if isinstance(meta.get("fingerInfo"), dict) else {}
    normalized_profile = str(fingerprint_profile or "iphone").strip().lower()
    default_width = "390" if normalized_profile in ("iphone", "ios") else "412"
    default_height = "844" if normalized_profile in ("iphone", "ios") else "915"
    width = _first_text(finger.get("openWidth"), finger.get("resolutionX"), default_width)
    height = _first_text(finger.get("openHeight"), finger.get("resolutionY"), default_height)
    args = [str(exe)] + _local_chrome_args(
        profile_dir,
        port,
        dir_id=normalized_id,
        profile=fingerprint_profile,
        proxy=proxy,
        width=width,
        height=height,
    )
    startup_param = str(finger.get("startupParam") or "").strip()
    if startup_param:
        args.extend(startup_param.split())
    # Avoid restoring stale Roxy dashboard tabs copied from the template profile.
    args.append("about:blank")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    _wait_for_debug_port(port)
    debug_addr = f"127.0.0.1:{port}"
    log.info(f"  [Roxy] 已直启 local profile {normalized_id} debug={debug_addr}")
    return debug_addr, ""


def roxy_create_profile(
    name: str,
    proxy: dict | None = None,
    *,
    fingerprint_profile: str = "",
    workspace_id: int = 0,
) -> str:
    """创建 RoxyBrowser profile，返回 dirId（profile ID 字符串）。

    Args:
        name: profile 名（windowName）
        proxy: dict {host, port, user, password, proxyType: SOCKS5/HTTP/HTTPS}
        fingerprint_profile: "iphone" / "android" / "auto"，默认读 config
    """
    if not workspace_id:
        workspace_id = roxy_workspace_id()
    profile = (fingerprint_profile or _resolve_fingerprint_profile() or "auto").lower()

    payload: dict[str, Any] = {
        "workspaceId": workspace_id,
        "windowName": name,
        "windowRemark": "gpt_pay auto-created",
    }

    # ★★★ 指纹环境（手机 / 桌面）
    finger: dict[str, Any] = {
        "isLanguageBaseIp": True,
        "isDisplayLanguageBaseIp": True,
        "isTimeZone": True,
        "isPositionBaseIp": True,
        "randomFingerprint": True,        # 启动时随机所有指纹参数（canvas/webGL 等）
        "clearCacheFile": True,
        "clearCookie": True,
        "clearLocalStorage": True,
        "forbidSavePassword": True,
        "webRTC": 0,                      # 替换为代理出口 IP
        "canvas": True,
        "webGL": True,
        "webGLInfo": True,
        "audioContext": True,
        "speechVoices": True,
        "fontType": True,
        "deviceInfo": True,
        "macInfo": True,
    }
    if profile in ("iphone", "ios"):
        payload["os"] = "IOS"
        payload["osVersion"] = "17.0"
        payload["coreVersion"] = "130"
        finger["resolutionType"] = True
        finger["resolutionX"] = "390"
        finger["resolutionY"] = "844"
        finger["openWidth"] = "390"
        finger["openHeight"] = "844"
        log.info(f"  [Roxy] 指纹环境: 📱 iPhone (iOS 17 Safari)")
    elif profile == "android":
        payload["os"] = "Android"
        payload["osVersion"] = "14"
        payload["coreVersion"] = "130"
        finger["resolutionType"] = True
        finger["resolutionX"] = "412"
        finger["resolutionY"] = "915"
        finger["openWidth"] = "412"
        finger["openHeight"] = "915"
        log.info(f"  [Roxy] 指纹环境: 📱 Android (Chrome)")
    else:
        payload["os"] = "Windows"
        payload["osVersion"] = "11"
        payload["coreVersion"] = "130"
        log.debug(f"  [Roxy] 指纹环境: 桌面 Windows 11")
    payload["fingerInfo"] = finger

    # ★★★ 代理（注入 711 sticky session）
    if proxy and proxy.get("host"):
        proxy = _inject_sticky_session(dict(proxy))
        protocol = str(proxy.get("proxyType") or "SOCKS5").upper()
        if protocol not in ("HTTP", "HTTPS", "SOCKS5"):
            protocol = "SOCKS5"
        payload["proxyInfo"] = {
            "moduleId": 0,
            "proxyMethod": "custom",
            "proxyCategory": protocol,
            "ipType": "IPV4",
            "protocol": protocol,
            "host": str(proxy["host"]),
            "port": str(proxy["port"]),
            "proxyUserName": str(proxy.get("user") or ""),
            "proxyPassword": str(proxy.get("password") or ""),
            "checkChannel": "IPRust.io",
        }
        log.info(f"  [Roxy] 注入代理: {protocol} {proxy['host']}:{proxy['port']} user={proxy.get('user', '')[:48]}")
    else:
        payload["proxyInfo"] = {"proxyMethod": "custom", "proxyCategory": "noproxy"}

    data = _post("/browser/create", payload)
    if not _is_success_response(data):
        raise RoxyBrowserError(f"create profile 失败: {data}")
    dir_id = (data.get("data") or {}).get("dirId") or (data.get("data") or {}).get("id") or ""
    if not dir_id:
        raise RoxyBrowserError(f"create profile 没返回 dirId: {data}")
    log.info(f"  [Roxy] 已创建 profile dirId={dir_id} name={name}")
    return str(dir_id)


def roxy_open_profile(dir_id: str, *, proxy: dict | None = None,
                      fingerprint_profile: str = "iphone") -> tuple[str, str]:
    """打开 profile，返回 (debuggerAddress, driverPath)。

    debuggerAddress 形如 ``127.0.0.1:52314``，driverPath 是绝对路径。
    """
    normalized_id = str(dir_id or "").strip()
    if not normalized_id:
        raise RoxyBrowserError("open profile 缺少 dirId")

    status = roxy_profile_status(normalized_id)
    if status:
        debug_addr = _debug_addr_from_connection(status)
        driver_path = _first_text(status.get("driver"), status.get("webdriver"), status.get("webdriver_path"))
        if debug_addr:
            proxy_required = bool(proxy and proxy.get("host"))
            proxy_live = (
                not normalized_id.startswith("local-")
                or not proxy_required
                or _local_profile_has_live_proxy(normalized_id, _roxy_profile_dir(normalized_id))
            )
            bridge_live = not _proxy_needs_local_bridge(proxy) or _local_proxy_bridge_is_live(normalized_id)
            if proxy_live and bridge_live:
                log.info(f"  [Roxy] profile {normalized_id} 已打开，直接附加 debug={debug_addr}")
                return debug_addr, driver_path
            log.warning(f"  [Roxy] profile {normalized_id} 已打开，但本地代理已失效；准备重启")
            roxy_close_profile(normalized_id)
            time.sleep(1)

    proxy_override = None
    if proxy and _proxy_needs_local_bridge(proxy):
        proxy_override = _start_local_proxy_bridge(normalized_id, _inject_sticky_session(dict(proxy)))
        profile_dir = _roxy_profile_dir(normalized_id)
        if profile_dir.exists():
            _patch_lumi_proxy(profile_dir, proxy_override)

    payload: dict[str, Any] = {
        "dirId": normalized_id,
        "args": ["--remote-allow-origins=*"],
        # The local patched Roxy API can list profiles without team rights, but
        # its occupancy check may still return "用户没有该团队权限". Skip that
        # preflight and let the local launcher decide whether it can attach.
        "forceOpen": True,
        "headless": False,
    }
    if proxy_override:
        payload["proxyOverride"] = {
            "protocol": "HTTP",
            "proxyCategory": "HTTP",
            "host": proxy_override["host"],
            "port": str(proxy_override["port"]),
            "proxyUserName": "",
            "proxyPassword": "",
        }
    if not normalized_id.startswith("local-"):
        payload["workspaceId"] = roxy_workspace_id()

    data = _post("/browser/open", payload, timeout=120)
    if not _is_success_response(data):
        status = roxy_profile_status(normalized_id)
        debug_addr = _debug_addr_from_connection(status) if status else ""
        if debug_addr:
            proxy_required = bool(proxy and proxy.get("host"))
            proxy_live = (
                not normalized_id.startswith("local-")
                or not proxy_required
                or _local_profile_has_live_proxy(normalized_id, _roxy_profile_dir(normalized_id))
            )
            bridge_live = not _proxy_needs_local_bridge(proxy) or _local_proxy_bridge_is_live(normalized_id)
            if proxy_live and bridge_live:
                driver_path = _first_text(status.get("driver"), status.get("webdriver"), status.get("webdriver_path"))
                log.info(f"  [Roxy] open 返回错误但窗口已打开，直接附加 debug={debug_addr}")
                return debug_addr, driver_path
        if normalized_id.startswith("local-"):
            msg = str((data or {}).get("msg") or (data or {}).get("message") or "")
            log.warning(f"  [Roxy] API open local profile 失败，改用本地 RoxyChrome 直启: {msg}")
            return roxy_open_local_profile(
                normalized_id,
                proxy=proxy,
                fingerprint_profile=fingerprint_profile or "iphone",
            )
        raise RoxyBrowserError(f"open profile {normalized_id} 失败: {data}")
    payload_data = (data or {}).get("data") if isinstance((data or {}).get("data"), dict) else data
    debug_addr = _first_text(
        payload_data.get("http"),
        payload_data.get("debuggerAddress"),
        payload_data.get("debugAddress"),
        payload_data.get("address"),
    )
    driver_path = _first_text(payload_data.get("driver"), payload_data.get("webdriver"), payload_data.get("webdriver_path"))
    if not debug_addr:
        status = roxy_profile_status(normalized_id)
        debug_addr = _debug_addr_from_connection(status) if status else ""
        if status:
            driver_path = driver_path or _first_text(status.get("driver"), status.get("webdriver"), status.get("webdriver_path"))
    if not debug_addr:
        raise RoxyBrowserError(f"open profile {normalized_id} 没返回 http debug 地址: {data}")
    log.info(f"  [Roxy] 已打开 profile {normalized_id} debug={debug_addr}")
    return debug_addr, driver_path


def roxy_close_profile(dir_id: str) -> bool:
    normalized_id = str(dir_id or "").strip()
    try:
        try:
            data = _post("/browser/close", {"dirId": normalized_id}, timeout=20)
            if _is_success_response(data):
                return True
            log.debug(f"  [Roxy] API close {normalized_id} 返回错误: {data}")
        except Exception as e:
            log.debug(f"  [Roxy] close {normalized_id} err: {e}")
        if normalized_id.startswith("local-"):
            return roxy_close_local_profile(normalized_id)
        return False
    finally:
        _close_local_proxy_bridge(normalized_id)


def roxy_delete_profile(dir_id: str, workspace_id: int = 0) -> bool:
    normalized_id = str(dir_id or "").strip()
    if normalized_id.startswith("local-claude-"):
        return roxy_delete_local_profile(normalized_id)
    try:
        if not workspace_id:
            workspace_id = roxy_workspace_id()
        _post("/browser/delete", {"workspaceId": workspace_id, "dirIds": [normalized_id]}, timeout=20)
        return True
    except Exception as e:
        log.debug(f"  [Roxy] delete {normalized_id} err: {e}")
        return False


def roxy_clear_local_cache(dir_id: str) -> bool:
    """清 profile 本地缓存（cookies/localStorage/cache 等）。"""
    try:
        _post("/browser/clear_local_cache", {"dirIds": [dir_id]}, timeout=30)
        return True
    except Exception as e:
        log.warning(f"  [Roxy] clear_local_cache {dir_id} 失败: {e}")
        return False


def roxy_clear_server_cache(dir_id: str, workspace_id: int = 0) -> bool:
    """清 profile 云端备份的 cookies/storage（防止下次开窗自动同步回来）。"""
    try:
        if not workspace_id:
            workspace_id = roxy_workspace_id()
        _post("/browser/clear_server_cache", {"workspaceId": workspace_id, "dirIds": [dir_id]}, timeout=30)
        return True
    except Exception as e:
        log.warning(f"  [Roxy] clear_server_cache {dir_id} 失败: {e}")
        return False


def roxy_random_env(dir_id: str, workspace_id: int = 0) -> bool:
    """让 RoxyBrowser 给 profile 重新随机一份指纹（canvas/webGL/audio 等都换新值）。"""
    try:
        if not workspace_id:
            workspace_id = roxy_workspace_id()
        _post("/browser/random_env", {"workspaceId": workspace_id, "dirId": dir_id}, timeout=20)
        return True
    except Exception as e:
        log.debug(f"  [Roxy] random_env {dir_id} err: {e}")
        return False


def nuke_browser_cookies(page: ChromiumPage, *, sites_to_wipe: tuple[str, ...] = ()) -> None:
    """运行时彻底清空 ChromiumPage 的 cookies + storage（CDP 层 + DrissionPage 层双保险）。

    适用场景：复用窗口打开后，即使 Roxy server-side clear API 漏掉了什么
    （比如内存中的 session cookie、IndexedDB、cache storage），这里再用 CDP
    的 ``Network.clearBrowserCookies`` + ``Storage.clearDataForOrigin`` 兜底清一次。

    sites_to_wipe: 想强制按 origin 清的 site list（如 ``("https://www.paypal.com",)``）。
                   留空 = 只清全局 cookies + cache，不指定 origin 走 storage api。
    """
    if page is None:
        return

    # 1. 全局清 cookies（CDP）
    try:
        page.run_cdp("Network.clearBrowserCookies")
        log.info("  [Roxy] CDP Network.clearBrowserCookies → ✓")
    except Exception as e:
        log.warning(f"  [Roxy] CDP clearBrowserCookies err: {e}")

    # 2. 清 cache（CDP）
    try:
        page.run_cdp("Network.clearBrowserCache")
        log.info("  [Roxy] CDP Network.clearBrowserCache → ✓")
    except Exception as e:
        log.warning(f"  [Roxy] CDP clearBrowserCache err: {e}")

    # 3. DrissionPage 层 cookies（兜底）
    try:
        page.set.cookies.clear()
        log.info("  [Roxy] page.set.cookies.clear() → ✓")
    except Exception as e:
        log.debug(f"  [Roxy] page.set.cookies.clear err: {e}")

    # 4. 按 origin 清 storage（针对 PayPal/Stripe 这种会写大量 storage 的站）
    if sites_to_wipe:
        for origin in sites_to_wipe:
            try:
                page.run_cdp(
                    "Storage.clearDataForOrigin",
                    origin=origin,
                    storageTypes=("appcache,cookies,file_systems,indexeddb,"
                                  "local_storage,shader_cache,websql,service_workers,"
                                  "cache_storage"),
                )
                log.info(f"  [Roxy] CDP clearDataForOrigin {origin} → ✓")
            except Exception as e:
                log.debug(f"  [Roxy] CDP clearDataForOrigin {origin} err: {e}")


def roxy_profile_status(dir_id: str) -> dict:
    """看 profile 是否已打开（防止重复 open 报错）。"""
    normalized_id = str(dir_id or "").strip()
    if not normalized_id:
        return {}
    for params in ({"dirIds": normalized_id}, {}):
        try:
            data = _get("/browser/connection_info", params, timeout=10)
            rows = _response_rows(data)
            for row in rows:
                row_id = _first_text(row.get("dirId"), row.get("dir_id"), row.get("id"))
                if not row_id or row_id == normalized_id:
                    return row
        except Exception as e:
            log.debug(f"  [Roxy] connection_info {normalized_id} err: {e}")
    return {}


def _debug_addr_from_connection(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ("http", "debuggerAddress", "debugAddress", "address"):
        value = str(row.get(key) or "").strip()
        if not value:
            continue
        if "://" in value:
            try:
                parts = urlsplit(value)
                if parts.hostname and parts.port:
                    return f"{parts.hostname}:{parts.port}"
            except Exception:
                pass
        return value.replace("http://", "").replace("https://", "")
    ws = str(row.get("ws") or row.get("websocket") or row.get("webSocketDebuggerUrl") or "").strip()
    if ws:
        m = re.search(r"(?:127\.0\.0\.1|localhost):(\d+)", ws)
        if m:
            return "127.0.0.1:" + m.group(1)
    port = str(row.get("port") or row.get("debugPort") or row.get("debug_port") or "").strip()
    if port.isdigit():
        return "127.0.0.1:" + port
    return ""


def roxy_attach_open_profile(dir_id: str = "") -> tuple[ChromiumPage, str]:
    """附加到已经手动打开的 Roxy 窗口，不调用 /browser/open。"""
    normalized_id = str(dir_id or "").strip()
    params = {}
    if normalized_id:
        params["dirIds"] = normalized_id
    data = _get("/browser/connection_info", params, timeout=10)
    rows = _response_rows(data)
    if normalized_id:
        rows = [
            r for r in rows
            if _first_text(r.get("dirId"), r.get("dir_id"), r.get("id")) in ("", normalized_id)
        ]
    for row in rows:
        debug_addr = _debug_addr_from_connection(row)
        if not debug_addr:
            continue
        co = ChromiumOptions()
        co.set_address(debug_addr)
        page = ChromiumPage(co)
        attached_id = _first_text(row.get("dirId"), row.get("dir_id"), row.get("id"), normalized_id)
        log.info(f"  [Roxy] 已附加到手动打开窗口 dirId={attached_id} debug={debug_addr}")
        return page, attached_id
    raise RoxyBrowserError("没有可附加的已打开 Roxy 窗口；请先在 Roxy 客户端手动点击“打开窗口”")


def roxy_select_web_tab(page, prefer_url: str = ""):
    """Select the real web tab from a Roxy window that also auto-opens DevTools."""
    try:
        tabs = list(page.get_tabs())
    except Exception:
        return page
    preferred = str(prefer_url or "").lower()
    ranked = []
    for index, tab in enumerate(tabs):
        try:
            url = str(tab.url or "")
        except Exception:
            url = ""
        url_lower = url.lower()
        if url_lower.startswith("devtools://"):
            continue
        score = 0
        if url_lower.startswith(("https://", "http://")):
            score += 20
        if preferred and preferred in url_lower:
            score += 100
        if "paypal.com" in url_lower:
            score += 30
        if url_lower.startswith(("about:", "chrome://")):
            score -= 20
        ranked.append((score, -index, tab, url))
    if not ranked:
        return page
    _score, _order, selected, selected_url = max(ranked, key=lambda item: (item[0], item[1]))
    try:
        current_id = getattr(page, "tab_id", "")
        selected_id = getattr(selected, "tab_id", "")
        if selected_id and selected_id != current_id:
            log.info(f"  [Roxy] 切换到真实网页标签页: {selected_url[:100]}")
    except Exception:
        pass
    return selected


# =============================================================================
# 高阶接口：与 browser_mgr.open_bitbrowser_with_url 对齐
# =============================================================================
def open_roxy_with_url(
    url: str = "",
    name: str = "",
    proxy: dict | None = None,
    *,
    fingerprint_profile: str = "",
    goto: bool = True,
    dir_id: str = "",
    force_ephemeral: bool = False,
) -> tuple[ChromiumPage, str]:
    """开 RoxyBrowser profile 并附加 DrissionPage。

    两种模式：
      1. **复用模式**（推荐）：传 ``dir_id`` 或 config 设了 ``ROXY_PROFILE_ID``，
         用现有 profile（已配好的指纹 + 代理），开窗前自动清 cache/cookies/storage
         + 重新随机指纹（canvas/webGL/audio）→ 软无痕效果。返回的 dir_id 是这个固定 ID。
      2. **Ephemeral 模式**：dir_id 不传 + ``ROXY_PROFILE_ID`` 没配，每次新建一份
         临时 profile（带传入的 proxy + iPhone 指纹），跑完调用方自己 close + delete。

    Returns (page, dir_id, is_reusable):
        - page: DrissionPage ChromiumPage
        - dir_id: profile ID（复用模式 = 固定 ID；ephemeral 模式 = 新建 ID）
    """
    if not roxy_health():
        raise RoxyBrowserError(
            "RoxyBrowser API 未运行（127.0.0.1:50000 不可达）。"
            "在 RoxyBrowser 客户端 → 设置 → 开启 API。"
        )

    if proxy is None:
        proxy = (
            getattr(config, "ROXY_DEFAULT_PROXY", None)
            or getattr(config, "BITBROWSER_PROXY", None)
        )

    # 决定走「复用」还是「ephemeral」
    fixed_id = "" if force_ephemeral else (dir_id or str(getattr(config, "ROXY_PROFILE_ID", "") or "")).strip()
    clear_cache = bool(getattr(config, "ROXY_CLEAR_CACHE_BEFORE_OPEN", True))

    if fixed_id:
        # ★★ 复用模式：用预先配好的窗口（保留指纹 + 代理），开窗前清缓存做"软无痕"
        log.info(f"  [Roxy] 复用 profile dirId={fixed_id}（软无痕模式：开窗前清 cache/cookies）")

        # 1. 如果 local patched profile 已手动打开，直接附加，避免 close/open 触发权限错误
        status = roxy_profile_status(fixed_id)
        if status and fixed_id.startswith("local-"):
            proxy_required = bool(proxy and proxy.get("host"))
            bridge_live = not _proxy_needs_local_bridge(proxy) or _local_proxy_bridge_is_live(fixed_id)
            if (not proxy_required or _local_profile_has_live_proxy(fixed_id, _roxy_profile_dir(fixed_id))) and bridge_live:
                log.info(f"  [Roxy] local profile {fixed_id} 已打开，直接附加")
                page, attached_id = roxy_attach_open_profile(fixed_id)
                nuke_browser_cookies(
                    page,
                    sites_to_wipe=(
                        "https://www.paypal.com",
                        "https://paypal.com",
                        "https://checkout.stripe.com",
                        "https://pm-redirects.stripe.com",
                        "https://chatgpt.com",
                        "https://pay.openai.com",
                    ),
                )
                if goto and url:
                    try:
                        page.get(url)
                    except Exception as e:
                        log.warning(f"  [Roxy] navigate {url[:80]} err: {e}")
                    time.sleep(2)
                return page, attached_id
            log.warning(f"  [Roxy] local profile {fixed_id} 的代理已失效，不直接附加")

        # 2. 如果 profile 还在运行（上次没正常关），先关掉
        if status:
            log.info(f"  [Roxy] profile {fixed_id} 还开着（pid={status.get('pid')}），先关掉")
            roxy_close_profile(fixed_id)
            time.sleep(2)

        # 3. 清 local + server cache + cookies + storage
        if clear_cache and not fixed_id.startswith("local-"):
            log.info(f"  [Roxy] 清本地缓存 dirId={fixed_id}")
            roxy_clear_local_cache(fixed_id)
            log.info(f"  [Roxy] 清云端缓存 dirId={fixed_id}")
            roxy_clear_server_cache(fixed_id)
            time.sleep(1)

        # 4. 重新随机指纹（local patched 窗口跳过云端 random_env）
        if not fixed_id.startswith("local-"):
            log.info(f"  [Roxy] 随机化指纹 dirId={fixed_id}")
            roxy_random_env(fixed_id)
            time.sleep(1)

        # 5. 打开 profile
        debug_addr, _driver = roxy_open_profile(
            fixed_id,
            proxy=proxy,
            fingerprint_profile=fingerprint_profile or "iphone",
        )

        co = ChromiumOptions()
        co.set_address(debug_addr)
        page = ChromiumPage(co)

        # 5. ★ 运行时 CDP 兜底清 cookies + cache + PayPal/Stripe 的 storage
        # 即使第 2 步的 server-side clear API 漏掉了什么（内存中的 session cookie /
        # IndexedDB / cache_storage 等），这里再用 CDP 强制清一次。
        nuke_browser_cookies(
            page,
            sites_to_wipe=(
                "https://www.paypal.com",
                "https://paypal.com",
                "https://checkout.stripe.com",
                "https://pm-redirects.stripe.com",
                "https://chatgpt.com",
                "https://pay.openai.com",
            ),
        )

        if goto and url:
            try:
                page.get(url)
            except Exception as e:
                log.warning(f"  [Roxy] navigate {url[:80]} err: {e}")
            time.sleep(2)
        return page, fixed_id

    # ============== Ephemeral 模式 ==============
    if not name:
        name = f"gpt_pay_{int(time.time())}"

    log.info(f"  [Roxy] ephemeral 新建临时 profile name={name}")
    try:
        new_id = roxy_create_profile(name=name, proxy=proxy, fingerprint_profile=fingerprint_profile)
        debug_addr, _driver = roxy_open_profile(new_id)
    except Exception as exc:
        log.warning(f"  [Roxy] API 创建临时 profile 失败，改用本地 RoxyChrome 临时 profile: {exc}")
        new_id = roxy_create_local_profile(name=name, proxy=proxy, fingerprint_profile=fingerprint_profile or "iphone")
        debug_addr, _driver = roxy_open_local_profile(new_id, proxy=proxy, fingerprint_profile=fingerprint_profile or "iphone")

    co = ChromiumOptions()
    co.set_address(debug_addr)
    page = ChromiumPage(co)

    # ★ ephemeral 也兜底清一次（理论上新 profile 是干净的，但 Roxy 偶尔会复用某些缓存
    # 数据来加速创建，CDP 强制清一遍最稳）
    nuke_browser_cookies(
        page,
        sites_to_wipe=(
            "https://www.paypal.com",
            "https://paypal.com",
            "https://checkout.stripe.com",
            "https://pm-redirects.stripe.com",
            "https://chatgpt.com",
            "https://pay.openai.com",
        ),
    )

    if goto and url:
        try:
            page.get(url)
        except Exception as e:
            log.warning(f"  [Roxy] navigate {url[:80]} err: {e}")
        time.sleep(2)

    return page, new_id
