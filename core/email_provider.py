"""
邮箱 OTP 拉取模块
支持 Hotmail/Outlook (Microsoft Graph API) 和 IMAP XOAUTH2
参考: FlowPilot/hotmail-utils.js + Gpt-Agreement-Payment/outlook_pool.py
"""
import imaplib
import json
import logging
import re
import ssl
import time
import urllib.parse
import urllib.request
import urllib.error

# 跳过 SSL 验证 (macOS 证书问题)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

log = logging.getLogger("email")

GRAPH_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_MAIL_URL = "https://graph.microsoft.com/v1.0/me/messages"
IMAP_HOST = "outlook.office365.com"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"

# Thunderbird public OAuth client_id
DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"

# OTP 提取正则 (对齐 Gpt-Agreement-Payment/webui/backend/outlook_pool.py 实现)
OTP_PATTERNS = [
    re.compile(
        r"(?:code(?:\s*is)?|verification|one[-\s]*time|verify|kode|verifikasi"
        r"|代码|验证码|驗證碼|代码为)[^\d<>]{0,80}(\d{6})\b",
        re.I,
    ),
    re.compile(r"chatgpt[^\d<>]{0,80}(\d{6})", re.I),
    re.compile(r"openai[^\d<>]{0,80}(\d{6})", re.I),
    # 兜底：任意 6 位数字
    re.compile(r"\b(\d{6})\b"),
]


def _is_hex_color_context(text: str, idx: int) -> bool:
    if idx > 0 and text[idx - 1] == "#":
        return True
    before = text[max(0, idx - 30):idx]
    return bool(
        re.search(
            r"(?:color|background|bgcolor|fill|stroke)\s*[:=]\s*[\"']?#?\s*$",
            before, re.I,
        )
    )


def _extract_otp(text: str) -> str | None:
    """从邮件正文（含 HTML）中提取 6 位数字 OTP，跳过颜色码 / sendgrid 跟踪 ID。"""
    for pat in OTP_PATTERNS:
        for m in pat.finditer(text):
            idx = m.start(1)
            if _is_hex_color_context(text, idx):
                continue
            # 跳过 sendgrid 跟踪链接里的数字 (u20216706 / em7877 等)
            before = text[max(0, idx - 30):idx]
            if re.search(r"\bu\d{0,4}$|\.ct\.sendgrid\.net|/ls/click|em\d{0,4}\.tm\.", before, re.I):
                continue
            return m.group(1)
    return None


# ========== Hotmail/Outlook via Microsoft Graph API ==========


class GraphScopeMissingError(Exception):
    """refresh_token 不带 Graph scope（卡密默认 IMAP-only），自动 fallback 到 IMAP。"""
    pass


class EmailCredentialInvalidError(RuntimeError):
    """Mailbox refresh token is permanently invalid and should not be polled."""


def _refresh_access_token(
    refresh_token: str,
    client_id: str,
    scope: str,
    *,
    email: str = "",
    classify_graph_scope: bool = False,
) -> str:
    """Refresh an OAuth token and persist Microsoft RT rotation in the vault."""
    vault = None
    claim = None
    if email:
        try:
            from account_vault import (
                AccountNotFound,
                RefreshBusy,
                VaultError,
                get_account_vault,
            )

            vault = get_account_vault()
            claim = vault.claim_refresh(email)
            refresh_token = str(claim["refresh_token"])
            client_id = str(claim["client_id"] or client_id)
        except AccountNotFound:
            vault = None
            claim = None
        except RefreshBusy:
            raise
        except VaultError as exc:
            raise EmailCredentialInvalidError(
                f"邮箱 refresh_token 已失效: {email}"
            ) from exc

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "scope": scope,
    }).encode()
    req = urllib.request.Request(GRAPH_TOKEN_URL, data=body)
    try:
        resp = urllib.request.urlopen(req, timeout=15, context=_SSL_CTX)
        data = json.loads(resp.read())
        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError("Microsoft token refresh response omitted access_token")
        new_refresh_token = str(data.get("refresh_token") or refresh_token).strip()
        if vault is not None and claim is not None:
            if not vault.finalize_refresh(
                int(claim["account_id"]), str(claim["lease"]), new_refresh_token
            ):
                raise RuntimeError("Microsoft token refresh lease expired")
        return str(access_token)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        err_lower = err_body.lower()
        scope_markers = (
            "invalid_scope",
            "aadsts65001",
            "aadsts70011",
            "aadsts900144",
            "scope",
            "consent",
        )
        scope_missing = (
            classify_graph_scope
            and e.code == 400
            and any(marker in err_lower for marker in scope_markers)
        )
        invalid_grant = e.code == 400 and "invalid_grant" in err_lower and not scope_missing
        if vault is not None and claim is not None:
            vault.release_refresh(
                int(claim["account_id"]), str(claim["lease"]), invalid=invalid_grant
            )
        if scope_missing:
            raise GraphScopeMissingError(
                "Graph scope missing (refresh_token only supports IMAP)"
            ) from e
        if invalid_grant:
            raise EmailCredentialInvalidError(
                f"Microsoft refresh_token invalid_grant: {email or 'unknown'}"
            ) from e
        raise RuntimeError(f"Microsoft token refresh failed: HTTP {e.code}") from e
    except BaseException:
        if vault is not None and claim is not None:
            vault.release_refresh(int(claim["account_id"]), str(claim["lease"]))
        raise


def _get_access_token_graph(refresh_token: str, client_id: str, email: str = "") -> str:
    """用 refresh_token 换取 Graph API access_token

    抛 ``GraphScopeMissingError`` 表示 token 缺 ``Mail.Read`` scope（即"卡密只有
    IMAP 权限"），调用方应该自动切到 IMAP 模式。
    """
    return _refresh_access_token(
        refresh_token,
        client_id,
        "https://graph.microsoft.com/Mail.Read offline_access",
        email=email,
        classify_graph_scope=True,
    )


def fetch_otp_graph(email: str, refresh_token: str, client_id: str,
                    timeout: int = 120, after_ts: float = 0) -> str:
    """
    通过 Microsoft Graph API 拉取 OTP（适用于 Hotmail/Outlook 邮箱）。
    after_ts: 仅接受此时刻之后到达的邮件，避免命中过期 OTP。
              默认 = 调用时刻 - 30s（容错 OpenAI 投递快于浏览器跳转的窗口）。
    """
    if not after_ts:
        after_ts = time.time() - 30
    deadline = time.time() + timeout
    seen = set()
    cached_token = ""

    log.info(f"[OTP-Graph] 监听 {email} ({timeout}s)...")

    while time.time() < deadline:
        try:
            if not cached_token:
                cached_token = _get_access_token_graph(refresh_token, client_id, email=email)

            # 查询最近的 OpenAI 邮件
            filter_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(after_ts))
            params = urllib.parse.urlencode({
                "$filter": f"receivedDateTime ge {filter_ts}",
                "$top": "10",
                "$orderby": "receivedDateTime desc",
                "$select": "subject,from,body,receivedDateTime",
            })
            url = f"{GRAPH_MAIL_URL}?{params}"

            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {cached_token}",
                "Accept": "application/json",
            })
            resp = urllib.request.urlopen(req, timeout=15, context=_SSL_CTX)
            data = json.loads(resp.read())

            for msg in data.get("value", []):
                msg_id = msg.get("id", "")
                if msg_id in seen:
                    continue

                sender = (msg.get("from", {}).get("emailAddress", {}).get("address", "") or "").lower()
                subject = msg.get("subject", "")
                body_text = msg.get("body", {}).get("content", "")

                # 过滤 OpenAI 邮件
                is_openai = any(x in sender for x in
                                ("openai.com", "auth.openai", "tm.openai", "chatgpt.com"))
                if not is_openai:
                    continue

                # 排除已知坏的
                if "tm1.openai" in sender:
                    seen.add(msg_id)
                    continue

                # 提取 OTP
                combined = f"{subject} {body_text}"
                otp = _extract_otp(combined)
                if otp:
                    log.info(f"[OTP-Graph][{email}] 找到: {otp}")
                    return otp

                seen.add(msg_id)

        except (GraphScopeMissingError, EmailCredentialInvalidError):
            # 卡密 token 没 Graph scope → 立刻向上抛，让 fetch_otp 自动 fallback IMAP
            raise
        except urllib.error.HTTPError as e:
            if e.code == 401:
                cached_token = ""
            log.warning(f"[OTP-Graph][{email}] HTTP {e.code}")
        except Exception as e:
            log.warning(f"[OTP-Graph][{email}] 异常: {e}")

        time.sleep(5)

    raise TimeoutError(f"OTP 拉取超时 ({timeout}s)")


# ========== Hotmail/Outlook via IMAP XOAUTH2 ==========

def _get_access_token_imap(refresh_token: str, client_id: str, email: str = "") -> str:
    """用 refresh_token 换取 IMAP access_token"""
    return _refresh_access_token(
        refresh_token,
        client_id,
        IMAP_SCOPE,
        email=email,
    )


def fetch_otp_imap(email: str, refresh_token: str, client_id: str,
                   timeout: int = 240, threshold_ts: float = 0) -> str:
    """
    通过 IMAP XOAUTH2 拉取 OTP
    扫描 INBOX + Junk + Spam
    """
    import email as _email_lib

    if not threshold_ts:
        threshold_ts = time.time() - 30
    deadline = time.time() + timeout
    seen = set()
    cached_token = ""
    cached_at = 0.0
    folders = None

    log.info(f"[OTP-IMAP] 监听 {email} ({timeout}s)...")

    while time.time() < deadline:
        try:
            if not cached_token or time.time() - cached_at > 3000:
                cached_token = _get_access_token_imap(refresh_token, client_id, email=email)
                cached_at = time.time()

            M = imaplib.IMAP4_SSL(IMAP_HOST, 993, ssl_context=_SSL_CTX)
            auth_str = f"user={email}\x01auth=Bearer {cached_token}\x01\x01"
            typ, _ = M.authenticate("XOAUTH2", lambda x: auth_str.encode())
            if typ != "OK":
                raise RuntimeError("IMAP XOAUTH2 auth failed")

            if folders is None:
                try:
                    _, listing = M.list()
                    names = {}
                    for raw in (listing or []):
                        if not raw:
                            continue
                        s = raw.decode(errors="ignore") if isinstance(raw, bytes) else str(raw)
                        m = re.search(r'"([^"]+)"\s*$', s) or re.search(r"\s(\S+)\s*$", s)
                        if m:
                            nm = m.group(1).strip('"')
                            names[nm.lower()] = nm
                    picked = []
                    for cand in ["INBOX", "Junk", "Junk Email", "Spam"]:
                        real = names.get(cand.lower())
                        if real and real not in picked:
                            picked.append(real)
                    for k, v in names.items():
                        if any(x in k for x in ("junk", "spam", "bulk")) and v not in picked:
                            picked.append(v)
                    if "INBOX" not in picked:
                        picked.insert(0, "INBOX")
                    folders = picked
                except Exception:
                    folders = ["INBOX", "Junk", "Junk Email", "Spam"]
                log.info(f"  文件夹: {folders}")

            for folder in folders:
                try:
                    sel_arg = f'"{folder}"' if " " in folder else folder
                    typ, _ = M.select(sel_arg, readonly=True)
                    if typ != "OK":
                        continue
                except Exception:
                    continue

                try:
                    typ, data = M.search(None, "ALL")
                    if typ != "OK":
                        continue
                    msg_ids = (data[0] or b"").split()
                    for msg_id in reversed(msg_ids[-8:]):
                        mid = msg_id.decode()
                        if mid in seen:
                            continue
                        typ2, msg_data = M.fetch(msg_id, "(RFC822.HEADER RFC822.TEXT)")
                        if typ2 != "OK":
                            continue

                        raw_header = b""
                        raw_body = b""
                        for part in (msg_data or []):
                            if isinstance(part, tuple):
                                if isinstance(part[0], bytes) and b"RFC822.HEADER" in part[0]:
                                    raw_header = part[1]
                                elif isinstance(part[0], bytes) and b"RFC822.TEXT" in part[0]:
                                    raw_body = part[1]
                                elif isinstance(part[1], bytes) and not raw_header:
                                    raw_header = part[1]

                        if not raw_header:
                            continue

                        msg = _email_lib.message_from_bytes(raw_header + b"\n\n" + raw_body)
                        from_addr = (msg.get("From", "") or "").lower()

                        is_openai = any(x in from_addr for x in
                                        ("openai.com", "auth.openai", "tm.openai", "chatgpt.com"))
                        if not is_openai:
                            continue
                        if "tm1.openai" in from_addr:
                            seen.add(mid)
                            continue

                        # 提取 body
                        body_parts = []
                        if msg.is_multipart():
                            for p in msg.walk():
                                if p.get_content_type() in ("text/plain", "text/html"):
                                    try:
                                        payload = p.get_payload(decode=True)
                                        charset = p.get_content_charset() or "utf-8"
                                        body_parts.append(payload.decode(charset, errors="ignore"))
                                    except Exception:
                                        pass
                        else:
                            try:
                                payload = msg.get_payload(decode=True)
                                charset = msg.get_content_charset() or "utf-8"
                                body_parts.append(payload.decode(charset, errors="ignore"))
                            except Exception:
                                pass

                        otp = _extract_otp("\n".join(body_parts))
                        if otp:
                            log.info(f"[OTP-IMAP] 找到: {otp}")
                            try:
                                M.logout()
                            except Exception:
                                pass
                            return otp
                        seen.add(mid)
                except Exception:
                    pass

            try:
                M.logout()
            except Exception:
                pass
        except EmailCredentialInvalidError:
            raise
        except Exception as e:
            log.warning(f"[OTP-IMAP][{email}] 异常: {e}")

        time.sleep(4)

    raise TimeoutError(f"OTP 拉取超时 ({timeout}s)")


# ========== 统一入口 ==========

def fetch_otp(email: str, refresh_token: str, client_id: str = "",
              method: str = "graph", timeout: int = 120,
              *, relay_require_fresh: bool = False) -> str:
    """
    统一 OTP 拉取入口

    method:
      - "graph"  Microsoft Graph API（Outlook / Hotmail，需 refresh_token + client_id）
      - "imap"   IMAP XOAUTH2（同上）
      - "relay"  iCloud 隐私邮箱接码 URL（GET 直接拿 OTP，refresh_token 字段填取码 URL）

    自动识别：refresh_token 看着像 http(s):// 时无视 method，强走 relay。

    relay_require_fresh:
      默认 False：拉到第一封含 OTP 的邮件就返回（注册场景 OpenAI 只发 1 封，
                  接码服务通常返回的就是这一封，立即用最稳）。
      True：把第一次响应当基线丢掉，必须等内容变化（新邮件）才返回。仅用于
            "已有旧邮件、必须等新邮件"的特殊场景（如登录复用同一邮箱多次）。
    """
    rt = (refresh_token or "").strip()

    # 自动识别：URL 形态的 "refresh_token" 一律走 relay（兼容 icloud 行直接当 outlook 行用的情况）
    if rt.startswith(("http://", "https://")) or method == "relay":
        return fetch_otp_relay(rt, timeout=timeout, require_fresh=relay_require_fresh)

    if not client_id:
        client_id = DEFAULT_CLIENT_ID

    if method == "imap":
        return fetch_otp_imap(email, refresh_token, client_id, timeout=timeout)

    # 默认 graph：失败时自动检测 scope 缺失并 fallback 到 IMAP
    try:
        return fetch_otp_graph(email, refresh_token, client_id, timeout=timeout)
    except GraphScopeMissingError as e:
        log.warning(
            f"[email] {email} refresh_token 没有 Graph scope（卡密只有 IMAP 权限）"
            f"，自动切到 IMAP 模式重试。详情：{e}"
        )
        return fetch_otp_imap(email, refresh_token, client_id, timeout=timeout)


# ========== iCloud / 任意 GET-URL 接码协议 ==========

def fetch_otp_relay(relay_url: str, *, timeout: int = 120,
                    poll_interval: float = 3.0,
                    excluded_otps: set[str] | None = None,
                    require_fresh: bool = True) -> str:
    """从接码 URL 拉 OTP 邮件文本，抠 6 位数字。

    跟 pipeline.fetch_sms_otp 同款做法：周期性 GET URL，看到含 6 位码的内容就返回。
    适用：
      - iCloud 隐私邮箱卖家发货格式 `email----https://api.example/xxx`
      - 任何 "GET URL → 邮件正文（含 OTP）" 的接码服务

    Args:
        relay_url: 取码 URL
        timeout:   总等待秒数
        poll_interval: 两次 GET 间隔
        excluded_otps: 忽略这些 OTP（防止重复拿到上一次的旧码）
        require_fresh: True 时第一次响应当作"基线快照"不返回，必须等到内容变化（新邮件到达）
                       才返回新 OTP。这能避免：ChatGPT 还没发新邮件，我们就把上次的旧
                       OTP 提交回去导致 verification_error。
                       False 时第一次拉到内容立即返回（旧的也接受，谨慎使用）。

    Returns:
        6 位 OTP 字符串；超时返回 ""。
    """
    if not relay_url:
        return ""
    excluded_otps = set(excluded_otps or set())
    end = time.time() + max(timeout, 5)
    last_text = ""
    baseline_text = ""
    baseline_otp = ""
    log.info(f"  [relay] 拉 OTP {relay_url[:80]}...  (require_fresh={require_fresh})")
    while time.time() < end:
        try:
            req = urllib.request.Request(
                relay_url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "*/*",
                },
            )
            resp = urllib.request.urlopen(req, timeout=15, context=_SSL_CTX)
            text = (resp.read() or b"").decode("utf-8", errors="ignore")
        except Exception as e:
            log.debug(f"  [relay] poll err: {e}")
            time.sleep(poll_interval)
            continue

        # 卖家接口常见的"暂无邮件"应答：跳过
        low = text.strip().lower()
        if not low or low.startswith(("no", "null", "wait", "empty", "{}", "[]", "false")):
            time.sleep(poll_interval)
            continue
        if text == last_text:
            time.sleep(poll_interval)
            continue
        last_text = text

        otp = _extract_otp(text)
        if not otp:
            time.sleep(poll_interval)
            continue

        # 第一次拿到：如果 require_fresh，把它当基线，不返回；
        # 同时把这个 OTP 加进 excluded（任何后续相同 OTP 也忽略）
        if require_fresh and not baseline_text:
            baseline_text = text
            baseline_otp = otp
            excluded_otps.add(otp)
            log.info(f"  [relay] 基线邮件 OTP={otp}（视为旧码，等新邮件…）")
            time.sleep(poll_interval)
            continue

        if otp in excluded_otps:
            log.debug(f"  [relay] OTP={otp} 已用过 / 是基线，等下一封")
            time.sleep(poll_interval)
            continue

        log.info(f"  [relay] 新 OTP: {otp}")
        return otp

    if baseline_otp:
        log.warning(f"  [relay] {timeout}s 内只拿到基线 OTP={baseline_otp}，没等到新邮件；"
                    f"如果你确认 ChatGPT 没重发，可以传 require_fresh=False 强制用基线")
    else:
        log.warning(f"  [relay] {timeout}s 内没拿到 OTP")
    return ""
