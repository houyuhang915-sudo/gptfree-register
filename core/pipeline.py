"""
GPT Pay Pipeline - 浏览器自动化编排（参照 GuJumpgate/FlowPilot 实现）

用 DrissionPage 驱动 Chromium，注入 JS 脚本：
- JS 端只暴露同步动作（不做长循环 / 长 sleep）
- Python 端通过 __gpt_*_getState 拿当前状态，单循环按状态派发动作

支持流程：
  signup()        ChatGPT 注册（OTP 邮箱验证）
  login()         ChatGPT 登录（已有账号 / OTP 登录）
  checkout()      调 backend-api 创建 checkout session 并导航过去
  pay_paypal()    完成 PayPal 支付（login / guest_checkout / verification / review / approval）

补充工具：
  resolve_stripe_long_url(session_id)  把 cs_live_xxx 解析成 Stripe 完整长链
                                       (https://checkout.stripe.com/c/pay/cs_live_xxx#fid=...)
"""
import json
import logging
import random
import re
import ssl
import string
import time
import urllib.parse
import urllib.request
from pathlib import Path

import config
import email_provider

log = logging.getLogger("pipeline")

JS_DIR = Path(__file__).parent / "js"

# Stripe API SSL 上下文（macOS 系统 Python 没装 root certs，跳过校验）
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# ============ Stripe 长链解析 ============

STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION = "2025-03-31.basil"
STRIPE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
# OpenAI 在 Stripe 上常用的两个 publishable_key。
KNOWN_STRIPE_PKS = [
    "pk_live_51Pj377KslHRdbaPgTJYjThzH3f5dt1N1vK7LUp0qh0yNSarhfZ6nfbG7FFlh8KLxVkvdMWN5o6Mc4Vda6NHaSnaV00C2Sbl8Zs",
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
]


def build_paypal_contact_recovery_url(*source_urls: str, country: str = "") -> str:
    """Build a short, stable PayPal signup URL from a recursive Hermes redirect.

    PayPal sometimes nests ``ulOnboardRedirect`` back into itself after hCaptcha.
    Only the checkout identifiers are needed to resume the contact step; carrying
    the nested redirect forward eventually produces a 414 response.
    """
    stable: dict[str, str] = {}
    for raw_url in source_urls:
        raw_url = str(raw_url or "").strip()
        if not raw_url:
            continue
        try:
            parsed = urllib.parse.urlsplit(raw_url[:32768])
            query_items = urllib.parse.parse_qsl(
                parsed.query,
                keep_blank_values=False,
                max_num_fields=200,
            )
        except (TypeError, ValueError):
            query_items = []
        for key, value in query_items:
            key_lower = key.lower()
            if key_lower in {"ssrt", "token", "ba_token", "ul", "locale.x", "country.x"}:
                stable.setdefault(key_lower, str(value or "").strip())

    token = stable.get("token") or stable.get("ba_token")
    if not token:
        for raw_url in source_urls:
            match = re.search(r"(?:ba_token|token)=((?:BA|EC)-[A-Za-z0-9-]+)", str(raw_url or ""))
            if match:
                token = match.group(1)
                break
    if not token:
        return ""

    target_country = str(country or stable.get("country.x") or "US").strip().upper()
    locale = stable.get("locale.x") or ("en_GB" if target_country == "GB" else "en_US")
    if target_country == "GB":
        locale = "en_GB"
    params = []
    if stable.get("ssrt"):
        params.append(("ssrt", stable["ssrt"]))
    params.extend([
        ("token", token),
        ("ul", stable.get("ul") or "1"),
        ("modxo_redirect_reason", "guest_user"),
        ("ulOnboardRedirect", "true"),
        ("locale.x", locale),
        ("country.x", target_country),
    ])
    return "https://www.paypal.com/pay/checkout/signup/contact?" + urllib.parse.urlencode(params)


def _stripe_init_call(session_id: str, pk: str, locale: str = "en-US") -> dict | None:
    """POST {STRIPE_API}/v1/payment_pages/{cs_id}/init 拿 init payload（含 stripe_hosted_url）。"""
    url = f"{STRIPE_API}/v1/payment_pages/{session_id}/init"
    body = urllib.parse.urlencode({
        "key": pk,
        "_stripe_version": STRIPE_VERSION,
        "browser_locale": locale,
    }).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "User-Agent": STRIPE_UA,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://js.stripe.com",
            "Referer": "https://js.stripe.com/",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=20, context=_SSL_CTX)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")[:200]
        except Exception:
            pass
        log.debug(f"  stripe init pk={pk[:24]} HTTP {e.code}: {body}")
    except Exception as e:
        log.debug(f"  stripe init err: {e}")
    return None


def resolve_stripe_long_url(session_or_url: str) -> dict:
    """把 cs_live_xxx / chatgpt.com hosted url 解析成 Stripe 完整长链。

    返回:
      {
        "session_id":  "cs_live_xxx",
        "short_url":   "https://checkout.stripe.com/c/pay/cs_live_xxx",
        "hosted_url":  "https://checkout.stripe.com/c/pay/cs_live_xxx#fid=...",  # 长链
        "publishable_key": "pk_live_xxx",
      }
    """
    raw = (session_or_url or "").strip()
    m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", raw)
    if not m:
        raise ValueError(f"无法从输入中提取 checkout_session_id: {raw[:120]}")
    sid = m.group(1)
    short = f"https://checkout.stripe.com/c/pay/{sid}"

    for pk in KNOWN_STRIPE_PKS:
        data = _stripe_init_call(sid, pk)
        if not data:
            continue
        hosted = data.get("stripe_hosted_url") or ""
        if hosted:
            return {
                "session_id": sid,
                "short_url": short,
                "hosted_url": hosted,
                "publishable_key": pk,
                "raw": data,
            }
        # 即便没拿到 hosted_url，至少 pk 探测成功，stripe 短链兜底
        return {
            "session_id": sid,
            "short_url": short,
            "hosted_url": short,
            "publishable_key": pk,
            "raw": data,
        }

    # 全部 PK 都不匹配
    return {
        "session_id": sid,
        "short_url": short,
        "hosted_url": short,
        "publishable_key": "",
        "raw": None,
    }


def _load_js(name: str) -> str:
    p = JS_DIR / name
    if not p.exists():
        raise FileNotFoundError(f"JS 文件不存在: {p}")
    return p.read_text(encoding="utf-8")


def _random_name():
    first = ["James", "John", "Robert", "Michael", "William", "David",
             "Richard", "Joseph", "Thomas", "Charles", "Mary", "Patricia",
             "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan",
             "Jessica", "Sarah", "Karen"]
    last = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
            "Miller", "Davis", "Rodriguez", "Martinez", "Wilson",
            "Anderson", "Taylor", "Thomas"]
    return random.choice(first), random.choice(last)


def _random_birthday():
    return {
        "year": str(random.randint(1985, 2000)),
        "month": f"{random.randint(1, 12):02d}",
        "day": f"{random.randint(1, 28):02d}",
    }


def _random_password(n: int = 16) -> str:
    """生成符合 PayPal 规则的密码：至少 1 大写 + 1 小写 + 1 数字 + 1 特殊字符。"""
    if n < 8:
        n = 8
    # PayPal 接受的特殊字符（不要用 ^&# 这类被前端转义/拦截的）
    specials = "!@$%*"
    must_have = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice(specials),
    ]
    pool = string.ascii_letters + string.digits + specials
    rest = [random.choice(pool) for _ in range(n - len(must_have))]
    chars = must_have + rest
    random.shuffle(chars)
    return "".join(chars)
# ============ 62-us.com 短信 OTP 拉取 ============

def fetch_sms_otp(sms_url: str, deadline_s: int = 120,
                  poll_interval: float = 3.0,
                  *,
                  provider=None, activation: dict | None = None) -> str | None:
    """从短信平台拉取最近一条 SMS 中的 6 位 OTP。

    向后兼容两种模式：
      1. 老的 62-us 模式：传 sms_url，从 URL 拉文本，正则抠 OTP（保持原行为）。
      2. 新的 sms-activate 模式：传 provider + activation，调 provider.wait_otp。

    并发安全：62-us 模式下基于 sms_url 哈希的文件锁；sms-activate 模式下 activation
    是平台分配的独立 id，天然隔离，不需要锁。
    """
    # sms-activate / smsbower 路径
    if provider is not None and activation is not None:
        try:
            return provider.wait_otp(activation, deadline_s=deadline_s)
        except Exception as e:
            log.warning(f"  [sms] provider.wait_otp 抛异常: {e}")
            return None

    # 老的 62-us URL 路径
    if not sms_url:
        return None
    # 文件锁：基于 sms_url 的哈希
    import hashlib, fcntl, tempfile
    lock_key = hashlib.md5(sms_url.encode()).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"gpt_pay_sms_{lock_key}.lock"
    log.info(f"  [sms] 申请锁 {lock_path.name} （等待其他 worker 释放手机号）")
    with open(lock_path, "w") as lock_fp:
        fcntl.flock(lock_fp, fcntl.LOCK_EX)
        log.info(f"  [sms] 拿到锁，开始拉 OTP")
        end = time.time() + deadline_s
        while time.time() < end:
            try:
                req = urllib.request.Request(sms_url, headers={"User-Agent": "Mozilla/5.0"})
                resp = urllib.request.urlopen(req, timeout=10, context=_SSL_CTX)
                text = (resp.read() or b"").decode("utf-8", errors="ignore").strip()
                # 已知返回格式（多家平台）：
                #   62-us:        'yes|<sms_text>|ok'  /  'NO|...|ok'
                #   headone.fit:  'OK|<code>|PayPal: ...セキュリティコード...'
                #   有验证码时一般含 6 位数字；没有时为 'NO' / 'NULL' / 空。
                low = text.lower()
                has_code = bool(text) and not (
                    low.startswith("no") or low.startswith("null") or low.startswith("wait")
                )
                if has_code:
                    # 优先抓紧跟 PayPal 字样的 6 位码，否则退回任意 6 位码
                    m = re.search(r"PayPal[^\d]*?(\d{6})", text, re.I)
                    if not m:
                        m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
                    if m:
                        log.info(f"  SMS OTP: {m.group(1)}")
                        return m.group(1)
            except Exception as e:
                log.debug(f"  SMS poll err: {e}")
            time.sleep(poll_interval)
        return None


class GPTPipeline:
    """DrissionPage 驱动的完整注册 / 登录 / 支付流程"""

    SIGNUP_URL = "https://chatgpt.com/"
    LOGIN_URL = "https://chatgpt.com/auth/login"

    def __init__(self, page):
        self.page = page
        self._js_files_registered = set()
        self._paypal_entry_url = ""
        self._paypal_clean_landing_url = ""
        self._paypal_country = ""
        self._paypal_uri_recoveries = 0

    # ============ JS 注入 / 执行 ============

    def _inject_js(self, js_file: str):
        """注入 JS：双重注入（CDP newDocument 钩子 + 当前页执行）。

        - newDocument 钩子保证页面 navigate / redirect 后状态机 helper 自动重新生效
          （否则注册完跳回 chatgpt.com 会丢 __gpt_getPageState 等函数）。
        - 当前页执行覆盖"还没 navigate 的当前文档"。

        历史变更：之前曾改成只在当前页注入想绕 CF Turnstile 检测，但实测 CF 主因
        是 anti_detect 脚本（已禁用），newDocument 钩子本身不触发 CF。
        """
        code = _load_js(js_file)
        # newDocument 钩子（对每次新文档加载自动注入）
        if js_file not in self._js_files_registered:
            try:
                self.page.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=code)
                self._js_files_registered.add(js_file)
            except Exception as e:
                log.debug(f"  CDP newDocument 注入失败 ({js_file}): {e}")
        # 当前页面立即执行
        try:
            self.page.run_js(code, as_expr=True)
        except Exception as e:
            log.debug(f"  当前页执行 JS 失败 ({js_file}): {e}")

    def _ensure_js(self, probe_expr: str, js_file: str):
        for _ in range(3):
            ok = self._eval(probe_expr)
            if ok:
                return
            self._inject_js(js_file)
            time.sleep(0.4)

    def _eval(self, expr: str, default=None):
        try:
            return self.page.run_js(expr, as_expr=True)
        except Exception as e:
            log.debug(f"  JS eval 失败: {e}  expr={expr[:60]}")
            return default

    # ============ 真键盘 / 真鼠标（CDP isTrusted=true，绕 OpenAI 反自动化检测） ============

    def _real_fill_input(self, css_selector: str, value: str,
                         clear: bool = True, timeout: float = 3.0) -> bool:
        """用 DrissionPage 原生 input（走 CDP Input.dispatchKeyEvent）填表单。

        OpenAI 注册表单对自动化敏感：纯 JS setter 触发的 input 事件 isTrusted=false，
        next-auth 端会拒签到 /api/auth/error。这里走 CDP 真键盘事件，浏览器会
        把 isTrusted 标记为 true，跟手动输入完全一致。

        Args:
            css_selector: 输入框 CSS（如 'input[name="email"]'）
            value: 要填的字符串
            clear: 是否先清空
            timeout: 等元素出现的超时
        """
        try:
            el = self.page.ele(f"css:{css_selector}", timeout=timeout)
            if not el:
                return False
            try:
                el.click()  # 真鼠标聚焦
            except Exception:
                pass
            time.sleep(0.15)
            try:
                el.input(value, clear=clear)
                return True
            except Exception as e:
                log.debug(f"  _real_fill_input ele.input 失败: {e}")
                # 退回 CDP Input.insertText（仍比 JS setter 真）
                try:
                    if clear:
                        self.page.actions.key_down("ControlOrCommand").type("a").key_up("ControlOrCommand")
                        self.page.actions.type("\b")
                    self.page.actions.type(value, interval=0.02)
                    return True
                except Exception as e2:
                    log.debug(f"  _real_fill_input fallback type 失败: {e2}")
                    return False
        except Exception as e:
            log.debug(f"  _real_fill_input 异常: {e}")
            return False

    def _real_click(self, css_selector: str, timeout: float = 3.0) -> bool:
        """走 DrissionPage 原生 click（CDP Input.dispatchMouseEvent，isTrusted=true）。"""
        try:
            el = self.page.ele(f"css:{css_selector}", timeout=timeout)
            if not el:
                return False
            try:
                el.click()
                return True
            except Exception as e:
                log.debug(f"  _real_click ele.click 失败: {e}")
                # 退回 actions.move + click
                try:
                    self.page.actions.move_to(el).click()
                    return True
                except Exception as e2:
                    log.debug(f"  _real_click move+click 失败: {e2}")
                    return False
        except Exception as e:
            log.debug(f"  _real_click 异常: {e}")
            return False

    def _real_press_enter(self) -> bool:
        """真键盘 Enter（CDP）。表单 submit 前提交比 click 按钮更稳。"""
        try:
            self.page.actions.key_down("ENTER").key_up("ENTER")
            return True
        except Exception as e:
            log.debug(f"  _real_press_enter 失败: {e}")
            return False

    # ============ 状态查询 ============

    def _get_signup_state(self) -> str:
        self._ensure_js("typeof __gpt_getPageState === 'function'", "signup.js")
        return self._eval("__gpt_getPageState()") or "unknown"

    def _get_paypal_stage(self) -> str:
        self._ensure_js("typeof __gpt_paypal_getStage === 'function'", "paypal.js")
        return self._eval("__gpt_paypal_getStage()") or "unknown"

    def _get_checkout_stage(self) -> str:
        self._ensure_js("typeof __gpt_checkout_getStage === 'function'", "checkout.js")
        return self._eval("__gpt_checkout_getStage()") or "unknown"

    def _wait_state(self, getter, targets, timeout: int = 30, poll: float = 0.8) -> str:
        if isinstance(targets, str):
            targets = [targets]
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            state = getter()
            if state in targets:
                return state
            if state != last:
                log.debug(f"  → state={state}")
                last = state
            time.sleep(poll)
        return getter()

    def navigate(self, url: str):
        log.info(f"导航: {url[:80]}...")
        self.page.get(url)
        time.sleep(2)

    # ============ 注册 ============

    def _try_get_auth_session(self) -> dict:
        """直接 fetch /api/auth/session，保留服务端返回的完整 Session JSON。"""
        # 必须在 chatgpt.com 域上调用（同源 cookie）
        if "chatgpt.com" not in (self.page.url or ""):
            return {}
        expr = (
            "fetch('/api/auth/session', { credentials: 'include' })"
            "  .then(r => r.json())"
            "  .then(d => (d && typeof d === 'object') ? d : {})"
            "  .catch(() => ({}))"
        )
        try:
            resp = self.page.run_cdp(
                "Runtime.evaluate",
                expression=expr,
                awaitPromise=True,
                returnByValue=True,
                timeout=15,
            )
            if resp and "result" in resp:
                v = resp["result"].get("value")
                if isinstance(v, dict) and v.get("accessToken"):
                    session_token = str(v.get("sessionToken") or "").strip()
                    if not session_token:
                        cookies = self.extract_chatgpt_cookies()
                        session_token = str(
                            cookies.get("__Secure-next-auth.session-token") or ""
                        ).strip()
                        if session_token:
                            v = dict(v)
                            v["sessionToken"] = session_token
                    return v
        except Exception as e:
            log.debug(f"  _try_get_auth_session CDP 失败: {e}")
        return {}

    def _try_get_access_token(self) -> str:
        """兼容旧调用：从完整 /api/auth/session 响应中取 accessToken。"""
        session_json = self._try_get_auth_session()
        if isinstance(session_json, dict):
            return str(session_json.get("accessToken") or "")
        return ""

    def extract_chatgpt_cookies(self) -> dict:
        """从浏览器导出 chatgpt.com / auth.openai.com 域的 cookies 为 dict。

        关键 cookie：
          - __Secure-next-auth.session-token  ← chatgpt.com next-auth 长效 session（7+ 天）
          - cf_clearance  ← Cloudflare 验证（CF Turnstile 通过后会有）
          - oai-did  ← OpenAI device ID

        返回：{name: value} dict。出错返回 {}。
        """
        out: dict = {}
        # 先用 Network.getCookies（当前 tab 域），再用 Storage.getCookies（所有域）兜底
        all_cks = []
        for try_all_domains in (False, True):
            try:
                cks = self.page.cookies(all_domains=try_all_domains, all_info=True) or []
                if cks:
                    all_cks = list(cks)
                    log.debug(f"  cookies(all_domains={try_all_domains}) 拿到 {len(cks)} 个")
                    break
            except Exception as e:
                log.debug(f"  cookies(all_domains={try_all_domains}) 失败: {e}")
        if not all_cks:
            # 兜底：CDP Storage.getCookies 直接调
            try:
                resp = self.page.run_cdp("Storage.getCookies")
                all_cks = (resp or {}).get("cookies", []) or []
                log.debug(f"  CDP Storage.getCookies 拿到 {len(all_cks)} 个")
            except Exception as e:
                log.debug(f"  CDP Storage.getCookies 失败: {e}")

        keep_domains = ("chatgpt.com", "auth.openai.com", "openai.com")
        for c in all_cks:
            try:
                # cookies(all_info=True) 返回 dict 列表；CDP 直接也是 dict
                if not isinstance(c, dict):
                    c = dict(c) if hasattr(c, "items") else {}
                name = c.get("name") or ""
                value = c.get("value") or ""
                domain = (c.get("domain") or "").lstrip(".").lower()
                if not name or not value:
                    continue
                if not any(d in domain for d in keep_domains):
                    continue
                out[name] = value
            except Exception:
                continue
        log.info(f"  extract_chatgpt_cookies: 总 {len(all_cks)} 个 cookie，"
                 f"chatgpt/openai 域 {len(out)} 个")
        if out:
            keys_preview = ", ".join(sorted(out.keys())[:8])
            log.info(f"    关键 cookie: {keys_preview}{'...' if len(out) > 8 else ''}")
        return out

    def signup(self, email: str, password: str, outlook_creds: dict = None) -> dict:
        log.info("=" * 60)
        log.info(f"  注册: {email}")
        log.info("=" * 60)

        self._inject_js("signup.js")
        self.navigate(self.SIGNUP_URL)
        time.sleep(2)

        self._eval("typeof __gpt_dismissCookie === 'function' && __gpt_dismissCookie()")

        deadline = time.time() + 360
        otp_done = False
        profile_done = False
        oauth_clicks = 0
        last_state = None
        same_state_since = time.time()
        last_warn_at = 0.0
        signup_entry_clicks = 0
        switch_email_clicks = 0
        last_action_at = 0.0

        first_name, last_name = _random_name()
        birthday = _random_birthday()
        full_name = f"{first_name} {last_name}"

        while time.time() < deadline:
            state = self._get_signup_state()

            if state != last_state:
                log.info(f"[state] {state}  url={self.page.url[:90]}")
                last_state = state
                same_state_since = time.time()
                last_warn_at = 0.0  # 状态变了，重置 warn 计时
            else:
                stuck_for = time.time() - same_state_since
                if stuck_for > 60:
                    # 同一状态卡 60s+，自动退出标记 stuck_timeout（让外层重试）
                    dbg = self._eval("__gpt_debug()") or {}
                    log.error(f"  ✗ 同状态({state})卡住 {int(stuck_for)}s，自动退出标记 stuck_timeout")
                    log.error(f"     debug={dbg}")
                    return {"email": email, "status": "stuck_timeout",
                            "state": state, "url": self.page.url}
                if stuck_for > 25 and time.time() - last_warn_at > 25:
                    dbg = self._eval("__gpt_debug()") or {}
                    log.warning(f"  同状态({state})卡住 {int(stuck_for)}s+, debug={dbg}")
                    last_warn_at = time.time()

            if state == "logged_in":
                log.info("  注册成功，已进入登录态")
                session_json = self._try_get_auth_session()
                tok = str(session_json.get("accessToken") or "")
                return {"email": email, "password": password,
                        "status": "success", "state": state,
                        "access_token": tok,
                        "session_token": str(session_json.get("sessionToken") or ""),
                        "session_json": session_json}

            if state == "add_phone_page":
                log.warning("  页面要求添加手机号，当前流程不处理")
                return {"email": email, "password": password,
                        "status": "add_phone_required"}

            if state == "add_email_page":
                log.warning("  页面要求添加邮箱，跳出")
                return {"email": email, "password": password,
                        "status": "add_email_required"}

            if state == "verification_error":
                log.error("  OpenAI 验证错误页（OTP 被拒 / 风控）")
                return {"email": email, "password": password,
                        "status": "verification_error",
                        "debug": self._eval("__gpt_debug()") or {}}

            if state == "auth_error":
                # /api/auth/error —— OpenAI 后端拒签的兜底页（不是 OTP 错，是请求本身被拒）
                # 常见原因：iCloud relay 邮箱被风控 / 该邮箱已被注册 / IP 在黑名单 / 请求异常
                detail = self._eval("__gpt_getAuthErrorDetail && __gpt_getAuthErrorDetail()") or {}
                log.error(f"  OpenAI auth/error 页：{detail}  url={self.page.url}")
                return {"email": email, "password": password,
                        "status": "auth_error",
                        "url": self.page.url,
                        "auth_error_detail": detail,
                        "debug": self._eval("__gpt_debug()") or {}}

            now = time.time()
            wait = 1.2 - (now - last_action_at)
            if wait > 0:
                time.sleep(wait)

            if state == "entry_home":
                if signup_entry_clicks >= 6:
                    log.error("  注册入口点击 6 次仍未跳转，放弃")
                    return {"email": email, "status": "signup_entry_failed"}
                self._eval("typeof __gpt_dismissCookie === 'function' && __gpt_dismissCookie()")
                r = self._eval("__gpt_clickSignupEntry()") or {}
                signup_entry_clicks += 1
                log.info(f"  [{signup_entry_clicks}] 点击注册入口: {r}")
                last_action_at = time.time()
                time.sleep(3)  # 给 modal hydrate 一点时间
                continue

            if state == "phone_entry":
                if switch_email_clicks >= 4:
                    return {"email": email, "status": "switch_email_failed"}
                r = self._eval("__gpt_switchToEmail()") or {}
                switch_email_clicks += 1
                log.info(f"  [{switch_email_clicks}] 切换到邮箱: {r}")
                last_action_at = time.time()
                time.sleep(2)
                continue

            if state == "email_entry":
                # OpenAI 现在用 chatgpt.com 内嵌的 Universal Login modal，不会跳出 chatgpt.com
                # 域名（旧版才跳到 auth.openai.com）。所以这里不再做"关浮层"操作。
                # 关键是输入要走真 CDP 键盘事件（isTrusted=true）。

                # 优先走 DrissionPage 原生 CDP 真键盘 + Enter 提交（isTrusted=true）
                # 失败再 fallback 到 JS（避免一些少见的浮窗 / shadow DOM 抓不到）
                EMAIL_SELECTORS = [
                    'input[name="email"]',
                    'input[autocomplete="email"]',
                    'input[autocomplete="username"]',
                    'input[type="email"]',
                    'input#email',
                ]
                real_filled = False
                for sel in EMAIL_SELECTORS:
                    if self._real_fill_input(sel, email):
                        real_filled = True
                        break
                if real_filled:
                    log.info(f"  邮箱已填（真键盘）: {email}")
                    time.sleep(0.4)
                    # 直接 Enter 提交（避免找按钮 click 失误）
                    self._real_press_enter()
                    last_action_at = time.time()
                else:
                    # 退回 JS（少数极端情况）
                    fr = self._eval(f"__gpt_fillEmail({json.dumps(email)})") or {}
                    if not fr.get("filled"):
                        log.warning(f"  填邮箱失败: {fr}")
                        last_action_at = time.time()
                        time.sleep(1)
                        continue
                    log.info(f"  邮箱已填（JS fallback）: {email}")
                    time.sleep(0.6)
                    self._eval("__gpt_clickContinue()")
                    last_action_at = time.time()
                self._wait_state(self._get_signup_state,
                                 ["password_page", "verification_page",
                                  "phone_entry", "logged_in",
                                  "add_phone_page", "add_email_page",
                                  "oauth_consent", "auth_error"], timeout=15)
                continue

            if state == "password_page":
                fr = self._eval(f"__gpt_fillPassword({json.dumps(password)})") or {}
                if not fr.get("filled"):
                    log.warning(f"  填密码失败: {fr}")
                    last_action_at = time.time()
                    time.sleep(1)
                    continue
                log.info("  密码已填")
                time.sleep(0.6)
                self._eval("__gpt_clickContinue()")
                last_action_at = time.time()
                self._wait_state(self._get_signup_state,
                                 ["verification_page", "profile_page",
                                  "oauth_consent", "logged_in",
                                  "add_phone_page", "add_email_page"],
                                 timeout=20)
                continue

            if state == "verification_page":
                if otp_done:
                    time.sleep(2)
                    last_action_at = time.time()
                    continue
                if not outlook_creds:
                    return {"email": email, "status": "otp_credentials_missing"}
                log.info("  开始拉取 OTP...")
                try:
                    code = email_provider.fetch_otp(
                        outlook_creds["email"],
                        outlook_creds["refresh_token"],
                        outlook_creds.get("client_id", ""),
                        method=config.OTP_METHOD,
                        timeout=config.OTP_TIMEOUT,
                    )
                except Exception as e:
                    log.error(f"  OTP 拉取失败: {e}")
                    return {"email": email, "status": "otp_fetch_failed",
                            "error": str(e)}
                log.info(f"  OTP: {code}")
                fr = self._eval(f"__gpt_fillOTP({json.dumps(code)})") or {}
                if not fr.get("filled"):
                    log.warning(f"  OTP 填写失败: {fr}")
                    last_action_at = time.time()
                    time.sleep(1.5)
                    continue
                otp_done = True
                time.sleep(0.6)
                self._eval("__gpt_clickContinue()")
                last_action_at = time.time()
                self._wait_state(self._get_signup_state,
                                 ["profile_page", "oauth_consent", "logged_in",
                                  "add_phone_page", "add_email_page",
                                  "verification_error"],
                                 timeout=30)
                continue

            if state == "profile_page":
                # 检测 OpenAI "糟糕，出错了！Operation timed out" 错误页 — 点重试 + 重置 profile_done
                err_check = self._eval(
                    "(function(){var t=(document.body?document.body.innerText:'');"
                    "var hasErr=/糟糕.*出错|Operation timed out|出错了|Try again/i.test(t);"
                    "if(!hasErr)return null;"
                    "var bs=document.querySelectorAll('button, [role=\"button\"], a');"
                    "for(var i=0;i<bs.length;i++){var b=bs[i];if(b.disabled)continue;"
                    "if(b.offsetParent===null)continue;"
                    "var bt=(b.textContent||b.value||'').trim();"
                    "if(/^(重试|Try again|Retry|刷新|Refresh)$/i.test(bt)){"
                    "b.scrollIntoView({block:'center'});b.click();return bt.slice(0,40);}}"
                    "return 'has_err_no_btn';})()"
                )
                if err_check:
                    if err_check == "has_err_no_btn":
                        log.warning("  profile_page 出现错误页但找不到重试按钮，刷新整页")
                        try:
                            self.page.refresh()
                        except Exception:
                            self.page.get(self.page.url)
                    else:
                        log.warning(f"  profile_page 错误页，已点 [{err_check}] 重试")
                    # 重置 profile_done，等页面 reload 后重新填表
                    profile_done = False
                    time.sleep(5)
                    last_action_at = time.time()
                    continue

                if profile_done:
                    # OpenAI 现在 profile 页可能有两步：
                    #   step 1: 名字+生日 → 继续
                    #   step 2: "你的年龄是多少？" → 完成帐户创建
                    # 第一次填完后 profile_done=True，但还需要点第二步的按钮
                    fb = self._eval(
                        "(function(){var bs=document.querySelectorAll('button, [role=\"button\"], input[type=\"submit\"]');"
                        "for(var i=0;i<bs.length;i++){var b=bs[i];if(b.disabled)continue;"
                        "if(b.offsetParent===null)continue;"
                        "var t=(b.textContent||b.value||'').trim();"
                        "if(/完成.*创建|完成.*帐户|Create.*Account|Continue|Submit|继续|下一步/i.test(t)){"
                        "b.scrollIntoView({block:'center'});b.click();return t.slice(0,40);}}"
                        "return null;})()"
                    )
                    if fb:
                        log.info(f"  profile step2 click: {fb}")
                    time.sleep(2)
                    last_action_at = time.time()
                    self._wait_state(self._get_signup_state,
                                     ["oauth_consent", "logged_in",
                                      "add_phone_page", "add_email_page",
                                      "auth_loading", "chatgpt_loading"],
                                     timeout=20)
                    continue
                fr = self._eval(
                    f"__gpt_fillProfile({json.dumps(full_name)}, {json.dumps(birthday)})"
                ) or {}
                log.info(f"  Profile: {full_name} 生日: {birthday}  填写: {fr}")
                profile_done = True
                # 等按钮 enabled，重试点提交（最多 5 次）
                clicked = None
                for ci in range(5):
                    time.sleep(1.0)
                    clicked = self._eval("__gpt_clickContinue()") or {}
                    log.info(f"  profile click#{ci+1}: {clicked}")
                    if clicked.get("clicked"):
                        break
                if not clicked or not clicked.get("clicked"):
                    # 兜底：直接 JS 找 "完成帐户创建" / "Create" / "Submit" 按钮 click
                    fb = self._eval(
                        "(function(){var bs=document.querySelectorAll('button, [role=\"button\"], input[type=\"submit\"]');"
                        "for(var i=0;i<bs.length;i++){var b=bs[i];if(b.disabled)continue;"
                        "if(b.offsetParent===null)continue;"
                        "var t=(b.textContent||b.value||'').trim();"
                        "if(/^(完成.*创建|完成.*帐户|Create.*Account|Submit|Continue|Next|继续|下一步)$/i.test(t)){"
                        "b.scrollIntoView({block:'center'});b.click();return t.slice(0,40);}}"
                        "return null;})()"
                    )
                    log.info(f"  profile fallback click: {fb}")
                last_action_at = time.time()
                self._wait_state(self._get_signup_state,
                                 ["oauth_consent", "logged_in",
                                  "add_phone_page", "add_email_page",
                                  "auth_loading", "chatgpt_loading"],
                                 timeout=20)
                continue

            if state == "oauth_consent":
                if oauth_clicks >= 4:
                    return {"email": email, "status": "oauth_consent_failed"}
                r = self._eval("__gpt_clickOAuthConfirm()") or {}
                oauth_clicks += 1
                log.info(f"  [{oauth_clicks}] OAuth 同意: {r}")
                last_action_at = time.time()
                self._wait_state(self._get_signup_state,
                                 ["logged_in", "add_phone_page",
                                  "add_email_page"], timeout=20)
                continue

            if state in ("auth_loading", "chatgpt_loading", "unknown"):
                # 检测 "糟糕，出错了！Operation timed out" 错误页 — 点重试
                try:
                    err_check = self._eval(
                        "(function(){var t=(document.body?document.body.innerText:'');"
                        "var hasErr=/糟糕.*出错|Operation timed out|出错了|Try again/i.test(t);"
                        "if(!hasErr)return null;"
                        "var bs=document.querySelectorAll('button, [role=\"button\"], a');"
                        "for(var i=0;i<bs.length;i++){var b=bs[i];if(b.disabled)continue;"
                        "if(b.offsetParent===null)continue;"
                        "var bt=(b.textContent||b.value||'').trim();"
                        "if(/^(重试|Try again|Retry|刷新|Refresh)$/i.test(bt)){"
                        "b.scrollIntoView({block:'center'});b.click();return bt.slice(0,40);}}"
                        "return 'has_err_no_btn';})()"
                    )
                    if err_check:
                        if err_check == "has_err_no_btn":
                            log.warning("  loading 阶段错误页，刷新整页")
                            try:
                                self.page.refresh()
                            except Exception:
                                self.page.get(self.page.url)
                        else:
                            log.warning(f"  loading 阶段错误页，已点 [{err_check}] 重试")
                        # 已经填过 profile 但被错误页打断 → 重置让它重新跑 profile
                        profile_done = False
                        time.sleep(5)
                        last_action_at = time.time()
                        continue
                except Exception:
                    pass

                # 已经跳到 chatgpt.com 主域 + profile 已提交 → fetch /api/auth/session 探活
                if profile_done and "chatgpt.com" in (self.page.url or ""):
                    session_json = self._try_get_auth_session()
                    tok = str(session_json.get("accessToken") or "")
                    if tok:
                        log.info(f"  /api/auth/session 拿到 access_token 长度={len(tok)}，注册成功")
                        return {"email": email, "password": password,
                                "status": "success",
                                "state": "session_active",
                                "access_token": tok,
                                "session_token": str(session_json.get("sessionToken") or ""),
                                "session_json": session_json}
                time.sleep(1.5)
                continue

            log.warning(f"  未知状态: {state}, 等待...")
            time.sleep(2)

        log.error("  注册总超时")
        return {"email": email, "status": "timeout", "state": last_state}

    # ============ 登录（已有账号，走密码或 OTP） ============

    def login(self, email: str, password: str = "",
              outlook_creds: dict = None,
              *, otp_timeout: int | None = None,
              total_deadline_s: int | None = None) -> dict:
        """登录已有 ChatGPT 账号。

        - 有密码：走 email + password
        - 没密码或密码错误：走 OTP（要 outlook_creds）

        Args:
            otp_timeout: 单次 fetch_otp 的最长等待秒数；None = config.OTP_TIMEOUT (240s)
            total_deadline_s: 整个 login 流程总超时；None = 240s
        """
        log.info("=" * 60)
        log.info(f"  登录: {email}")
        log.info("=" * 60)

        self._inject_js("signup.js")
        self.navigate(self.SIGNUP_URL)
        time.sleep(2)
        self._eval("typeof __gpt_dismissCookie === 'function' && __gpt_dismissCookie()")

        deadline = time.time() + (total_deadline_s if total_deadline_s and total_deadline_s > 0 else 240)
        last_state = None
        last_action = 0.0
        otp_done = False
        password_filled = False

        while time.time() < deadline:
            state = self._get_signup_state()
            if state != last_state:
                log.info(f"[login state] {state}  url={self.page.url[:90]}")
                last_state = state

            if state == "logged_in":
                log.info("  登录成功")
                return {"email": email, "password": password,
                        "status": "success"}

            if state == "verification_error":
                return {"email": email, "status": "verification_error"}

            now = time.time()
            if now - last_action < 1.2:
                time.sleep(1.2 - (now - last_action))

            if state == "entry_home":
                # 主页 → 点 "登录"（不是 "免费注册"）
                clicked = self._eval(
                    "(() => {"
                    "  const btns = document.querySelectorAll('button, a, [role=\"button\"], [role=\"link\"]');"
                    "  for (const el of btns) {"
                    "    const t = (el.textContent || el.value || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();"
                    "    if (/^(?:登录|登入|log\\s*in|sign\\s*in)$/i.test(t)) {"
                    "      const r = el.getBoundingClientRect();"
                    "      if (r.width > 0 && r.height > 0) { el.click(); return { clicked: true, text: t }; }"
                    "    }"
                    "  }"
                    "  return { clicked: false };"
                    "})()"
                ) or {}
                log.info(f"  点登录入口: {clicked}")
                last_action = time.time()
                time.sleep(3)
                continue

            if state == "phone_entry":
                self._eval("__gpt_switchToEmail()")
                last_action = time.time()
                time.sleep(2)
                continue

            if state == "email_entry":
                self._eval(f"__gpt_fillEmail({json.dumps(email)})")
                log.info(f"  邮箱已填: {email}")
                time.sleep(0.6)
                self._eval("__gpt_clickContinue()")
                last_action = time.time()
                self._wait_state(self._get_signup_state,
                                 ["password_page", "verification_page",
                                  "logged_in", "oauth_consent"],
                                 timeout=15)
                continue

            if state == "password_page":
                if not password:
                    # 没密码 → 切到 OTP 登录
                    log.info("  密码为空，尝试 OTP 登录")
                    self._eval(
                        "(() => {"
                        "  const re = /one[-\\s]*time|passcode|verification|use\\s+(?:a\\s+)?code|验证码|一次性/i;"
                        "  const els = document.querySelectorAll('button, a, [role=\"button\"], [role=\"link\"]');"
                        "  for (const e of els) { "
                        "    const t = (e.textContent || e.value || '').replace(/\\s+/g, ' ').trim();"
                        "    if (re.test(t) && e.offsetParent) { e.click(); return; }"
                        "  }"
                        "})()"
                    )
                    last_action = time.time()
                    time.sleep(3)
                    continue
                if password_filled:
                    time.sleep(2)
                    continue
                fr = self._eval(f"__gpt_fillPassword({json.dumps(password)})") or {}
                if fr.get("filled"):
                    password_filled = True
                    log.info("  密码已填")
                    time.sleep(0.6)
                    self._eval("__gpt_clickContinue()")
                last_action = time.time()
                self._wait_state(self._get_signup_state,
                                 ["verification_page", "logged_in",
                                  "oauth_consent", "verification_error"],
                                 timeout=20)
                continue

            if state == "verification_page":
                if otp_done:
                    time.sleep(2)
                    continue
                if not outlook_creds:
                    return {"email": email, "status": "otp_credentials_missing"}
                _otp_to = otp_timeout if otp_timeout and otp_timeout > 0 else config.OTP_TIMEOUT
                try:
                    code = email_provider.fetch_otp(
                        outlook_creds["email"],
                        outlook_creds["refresh_token"],
                        outlook_creds.get("client_id", ""),
                        method=config.OTP_METHOD,
                        timeout=_otp_to,
                    )
                except Exception as e:
                    return {"email": email, "status": "otp_fetch_failed",
                            "error": str(e)}
                if not code:
                    return {"email": email, "status": "otp_fetch_timeout",
                            "error": f"otp not received within {_otp_to}s"}
                log.info(f"  OTP: {code}")
                fr = self._eval(f"__gpt_fillOTP({json.dumps(code)})") or {}
                if fr.get("filled"):
                    otp_done = True
                    time.sleep(0.6)
                    self._eval("__gpt_clickContinue()")
                last_action = time.time()
                self._wait_state(self._get_signup_state,
                                 ["logged_in", "oauth_consent",
                                  "verification_error"], timeout=30)
                continue

            if state == "oauth_consent":
                self._eval("__gpt_clickOAuthConfirm()")
                last_action = time.time()
                self._wait_state(self._get_signup_state,
                                 ["logged_in"], timeout=15)
                continue

            time.sleep(1.5)

        return {"email": email, "status": "timeout"}

    # ============ Checkout 长链 ============

    def checkout(self, access_token: str = None,
                 payment_method: str = "paypal") -> dict:
        """创建 checkout session 并解析所有可能的支付链。

        payment_method:
            "paypal"        → checkout_ui_mode=hosted, processor=openai_ie, US/USD
                              OpenAI 返回 hosted_checkout_url = pay.openai.com/c/pay/hosted_cs_live_xxx
                              这是 PayPal 唯一长链，比特浏览器打开它即直接进入 hosted PayPal 页
            "paypal_custom" → checkout_ui_mode=custom, processor=openai_ie, US/USD
                              走美区 custom UI（chatgpt.com 内嵌 Stripe Elements）
                              preferred_url = chatgpt.com/checkout/openai_ie/cs_live_xxx (短链 ★)
                              没有 hosted PayPal 页，PayPal 选项作为页内按钮出现
            "gopay"         → checkout_ui_mode=custom, processor=openai_llc, ID/IDR

        返回字段:
            checkout_url           OpenAI 中转链 (chatgpt.com/checkout/{processor}/cs_live_xxx)
            chatgpt_checkout_url   chatgpt.com/checkout/openai_llc/cs_live_xxx (用于 custom UI 卡支付)
            hosted_checkout_url    pay.openai.com/c/pay/hosted_cs_live_xxx (PayPal 长链，仅 hosted 模式有)
            stripe_short_url       checkout.stripe.com/c/pay/cs_live_xxx
            stripe_long_url        checkout.stripe.com/c/pay/cs_live_xxx#fid=...
            preferred_url          paypal=hosted, paypal_custom=chatgpt.com/checkout/openai_ie 短链, gopay=openai_llc 短链
            session_id             cs_live_xxx
        """
        log.info(f"[支付] 创建 checkout session (payment_method={payment_method})...")

        if "chatgpt.com" not in self.page.url:
            self.navigate("https://chatgpt.com/")

        self._inject_js("checkout.js")
        time.sleep(2)

        if not access_token:
            access_token = self._eval("__gpt_getAccessToken()")
            if not access_token:
                return {"status": "error", "error": "no_access_token"}

        plan_name = config.PLAN_NAME if config.PLAN_NAME != "chatgptplus" else "chatgptplusplan"
        # PayPal（hosted / custom）必须 US/USD 才会出 PayPal 支付选项；
        # JP/JPY 返回的长链是"只能用银行卡"的版本。所以两种 paypal 模式都强制 US/USD
        # （不论 config.CHECKOUT_COUNTRY 是什么）。
        # CHECKOUT_COUNTRY 的作用范围：
        #   1. phone_pool（用 JP 号）
        #   2. browser_mgr 出口 IP 期望（用 JP IP）
        # 不影响 hosted/custom checkout 的 country/currency。
        if payment_method in ("paypal", "paypal_custom"):
            country = "US"
            currency = "USD"
        else:
            country = config.CHECKOUT_COUNTRY or "ID"
            currency = config.CHECKOUT_CURRENCY or "IDR"

        result = self._eval(
            f"__gpt_createCheckout({json.dumps(access_token)}, "
            f"{json.dumps(plan_name)}, "
            f"{json.dumps(country)}, "
            f"{json.dumps(currency)}, "
            f"{json.dumps(payment_method)})"
        )
        if not isinstance(result, dict):
            return {"status": "error", "error": f"bad_response: {result!r}"}

        # Fallback: 用 access_token JWT 判断 plan_type，已经是 Plus 就不算失败
        # 这能 catch 到 OpenAI 错误信息没写"already subscribed"但实际就是已订阅的情况
        def _check_plan_from_jwt(at):
            if not at or at.count(".") < 2:
                return ""
            import base64 as _b64
            try:
                payload = at.split(".")[1]
                payload += "=" * (-len(payload) % 4)
                decoded = _b64.urlsafe_b64decode(payload).decode("utf-8")
                jp = json.loads(decoded)
                return jp.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type", "")
            except Exception:
                return ""

        if result.get("error"):
            err_str = str(result["error"]).lower()
            # 账号已经是 Plus（之前跑过支付，OpenAI 后端拒绝再开订阅）
            if any(k in err_str for k in (
                "already subscribed", "already_subscribed",
                "already has an active subscription",
                "already_has_active_subscription",
                "active subscription already",
                "already a plus", "already_a_plus",
                "plus_already", "already_plus",
                "already on plan", "already_on_plan",
                "subscription already exists",
            )):
                log.info(f"  ✓ 账号已经是 Plus，无需再次支付（错误={result['error']}）")
                return {"status": "already_plus", "error": result["error"]}
            # 错误信息没匹配关键词，但 JWT 显示 plan_type=plus 也算
            plan = _check_plan_from_jwt(access_token)
            if plan == "plus":
                log.info(f"  ✓ JWT plan_type=plus，账号已是 Plus（错误={result['error']}）")
                return {"status": "already_plus", "error": result["error"]}
            return {"status": "error", "error": result["error"]}

        url = result.get("url", "")
        sid = result.get("sessionId", "")
        processor = result.get("processor", "openai_ie")
        hosted = result.get("hostedCheckoutUrl", "") or ""

        # Stripe 长链（卡支付备用，hosted=true 模式 OpenAI 不一定走 Stripe，所以这里是兜底）
        stripe_long = ""
        stripe_short = ""
        if sid:
            try:
                resolved = resolve_stripe_long_url(sid)
                stripe_short = resolved.get("short_url", "")
                stripe_long = resolved.get("hosted_url", "") or stripe_short
            except Exception as e:
                log.debug(f"  解析 Stripe 长链失败: {e}")

        chatgpt_alt_url = (
            f"https://chatgpt.com/checkout/openai_llc/{sid}" if sid else ""
        )

        # 不同 payment_method 选不同的 preferred URL：
        #   paypal        → hosted 长链 (pay.openai.com/c/pay/hosted_cs_live_xxx)
        #   paypal_custom → chatgpt.com/checkout/openai_ie/cs_live_xxx 短链（美区 custom UI）
        #   gopay         → chatgpt.com/checkout/openai_llc/cs_live_xxx 短链
        if payment_method == "paypal" and hosted:
            preferred = hosted
        else:
            preferred = url

        log.info(f"  session_id        : {sid}")
        log.info(f"  processor_entity  : {processor}")
        log.info(f"  checkout_url      : {url[:90]}")
        log.info(f"  chatgpt_alt_url   : {chatgpt_alt_url[:90]}")
        log.info(f"  hosted_checkout   : {hosted[:90] if hosted else '(none)'}")
        log.info(f"  stripe_short_url  : {stripe_short[:90]}")
        log.info(f"  stripe_long_url   : {stripe_long[:90]}{'…' if len(stripe_long) > 90 else ''}")
        log.info(f"  preferred_url     : {preferred[:90]}")

        if not preferred:
            return {"status": "error", "error": "no_url",
                    "session_id": sid, "stripe_long_url": stripe_long}

        # 默认让脚本侧 chromium 也跟着导航过去（你可以选择不做：直接复用浏览器看页面）
        # paypal_custom 短链 chatgpt.com/checkout/openai_xx/cs_live_xxx 是 React + Stripe Elements
        # 全套加载，单次 navigate + sleep 不一定真的渲染完，加一个"navigate + 等 URL 落定 + 检测 body 有内容"
        # 的多次重试逻辑。
        def _verified_navigate(target_url: str, max_retry: int = 3,
                               wait_url_s: int = 15, wait_body_s: int = 8) -> bool:
            for n in range(max_retry):
                try:
                    self.navigate(target_url)
                except Exception as ne:
                    log.warning(f"  navigate 异常 attempt {n + 1}: {ne}")
                # 等 URL 落到 cs_live
                t0 = time.time()
                url_ok = False
                while time.time() - t0 < wait_url_s:
                    cu = self.page.url or ""
                    if "cs_live_" in cu or "/c/pay/" in cu:
                        url_ok = True
                        break
                    time.sleep(0.5)
                if not url_ok:
                    log.warning(f"  attempt {n + 1}: URL 未落到 cs_live ({(self.page.url or '')[:80]})")
                    continue
                # 等 body 有可见内容（OpenAI custom UI: "サブスクリプション" / "Subscribe"）
                t0 = time.time()
                while time.time() - t0 < wait_body_s:
                    try:
                        bt = self._eval(
                            "(function(){var b=document.body;return b?b.innerText.length:0;})()"
                        )
                    except Exception:
                        bt = 0
                    if isinstance(bt, int) and bt > 200:
                        return True
                    time.sleep(0.5)
                log.warning(f"  attempt {n + 1}: body 内容太少（可能空白页），重试")
                time.sleep(1)
            return False

        nav_ok = _verified_navigate(preferred)
        if not nav_ok:
            log.warning(f"  ⚠ 多次 navigate 后页面仍未真正渲染，url={(self.page.url or '')[:90]}")
        # 给 React + Stripe Elements 最后 hydrate 时间
        if payment_method == "paypal_custom":
            time.sleep(8)
        else:
            time.sleep(4)
        return {
            "status": "ready",
            "url": url,
            "chatgpt_checkout_url": chatgpt_alt_url,
            "hosted_checkout_url": hosted,
            "stripe_short_url": stripe_short,
            "stripe_long_url": stripe_long,
            "preferred_url": preferred,
            "session_id": sid,
            "processor_entity": processor,
            "payment_method": payment_method,
        }

    # ============ Stripe Hosted Checkout 自动填账单 ============

    def _safe_input(self, css: str, value: str, clear: bool = True,
                     dismiss_autocomplete: bool = True) -> bool:
        """填字段（更稳的版本）：
          1) click 元素聚焦
          2) ESC 关地址联想下拉
          3) JS 清空 value
          4) 真键盘逐字符输入
          5) Tab 跳焦点（让 React 走 blur 校验）

        如果失败再降级到纯 JS setter。"""
        if value is None or value == "":
            return False
        try:
            el = self.page.ele(f"css:{css}", timeout=2)
            if not el:
                log.debug(f"  _safe_input: 元素不存在 {css}")
                return False

            # Step 1: 聚焦
            try:
                el.click()
            except Exception:
                try:
                    el.focus()
                except Exception:
                    pass
            time.sleep(0.15)

            # Step 2: 关地址联想 + Stripe 自动补全
            if dismiss_autocomplete:
                try:
                    self.page.actions.key_down("ESCAPE").key_up("ESCAPE")
                except Exception:
                    pass
                time.sleep(0.1)

            # Step 3: JS 清空 + select-all 真键盘清空 双保险
            self._eval(
                f"(function(){{var e=document.querySelector({json.dumps(css)});"
                f"if(!e)return;var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                f"s.call(e,'');"
                f"e.dispatchEvent(new Event('input',{{bubbles:true}}));}})()"
            )
            time.sleep(0.1)
            try:
                import platform
                modifier = "META" if platform.system() == "Darwin" else "CTRL"
                self.page.actions.key_down(modifier).key_down("a").key_up("a").key_up(modifier)
                time.sleep(0.08)
                self.page.actions.key_down("DELETE").key_up("DELETE")
            except Exception:
                pass
            time.sleep(0.1)

            # Step 4: 真键盘逐字符输入
            try:
                self.page.actions.type(str(value), interval=0.03)
            except Exception as e:
                log.debug(f"  actions.type err {css}: {e}")
                # 兜底：JS setter
                self._eval(
                    f"(function(){{var e=document.querySelector({json.dumps(css)});"
                    f"if(!e)return;var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                    f"s.call(e,{json.dumps(value)});"
                    f"e.dispatchEvent(new Event('input',{{bubbles:true}}));"
                    f"e.dispatchEvent(new Event('change',{{bubbles:true}}));}})()"
                )

            # Step 5: ESC 再关一次下拉，然后 Tab 离开（触发 blur）
            if dismiss_autocomplete:
                try:
                    self.page.actions.key_down("ESCAPE").key_up("ESCAPE")
                except Exception:
                    pass
                time.sleep(0.1)
            try:
                self.page.actions.key_down("TAB").key_up("TAB")
            except Exception:
                pass
            time.sleep(0.15)

            # 回读校验：如果仍是空 / 内容显著不对，记 warning
            actual = self._eval(
                f"(document.querySelector({json.dumps(css)}) || {{}}).value || ''"
            ) or ""
            if not actual:
                log.warning(f"  ⚠ {css} 填写后回读为空")
                return False
            return True
        except Exception as e:
            log.debug(f"  _safe_input err {css}: {e}")
            return False

    def _safe_select(self, css: str, value: str) -> bool:
        """select 元素的赋值（JS 设值 + change 事件）"""
        try:
            self._eval(
                f"(function(){{var e=document.querySelector({json.dumps(css)});"
                f"if(!e)return;var s=Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype,'value').set;"
                f"s.call(e,{json.dumps(value)});"
                f"e.dispatchEvent(new Event('change',{{bubbles:true}}));}})()"
            )
            return True
        except Exception as e:
            log.debug(f"  _safe_select err {css}: {e}")
            return False

    def _safe_select_region(self, css: str, candidate: str, country: str = "US") -> bool:
        """region (state/都道府県) select 模糊匹配。

        JP 的 #billingAdministrativeArea option 可能是英文（'Tokyo'）也可能是日文
        （'東京都'），option.value 经常是英文 ID。先精确匹配 value/text，
        再按子串匹配，最后试 React-friendly 的 fillInput 触发。

        返回是否成功匹配并 set 了一个 option。
        """
        if not candidate:
            return False
        try:
            ok = self._eval(
                "(function(css, cand){"
                "var sel=document.querySelector(css);"
                "if(!sel||!sel.options)return false;"
                "var c=String(cand||'').trim();"
                "var lc=c.toLowerCase();"
                "var norm=function(x){return String(x||'').trim().toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g,'');};"
                "var nc=norm(c);"
                "var matched=null, mode='';"
                # 1) 精确匹配 value
                "for(var i=0;i<sel.options.length;i++){"
                "  var o=sel.options[i];"
                "  var ov=String(o.value||'').trim();"
                "  if(ov===c||ov.toLowerCase()===lc){matched=o;mode='value';break;}"
                "} "
                # 2) 精确匹配 text（不区分大小写）
                "if(!matched){for(var i=0;i<sel.options.length;i++){"
                "  var o=sel.options[i];"
                "  if(String(o.text||'').trim().toLowerCase()===lc){matched=o;mode='text';break;}"
                "}}"
                "if(!matched){for(var i=0;i<sel.options.length;i++){"
                "  var o=sel.options[i];"
                "  if(norm(o.text)===nc||norm(o.value)===nc){matched=o;mode='norm';break;}"
                "}}"
                # 3) 子串匹配（cand 长度 > 2）
                "if(!matched && c.length>2){for(var i=0;i<sel.options.length;i++){"
                "  var o=sel.options[i];"
                "  var t=String(o.text||'').trim().toLowerCase();"
                "  if(t.indexOf(lc)>=0){matched=o;mode='substr';break;}"
                "}}"
                "if(!matched)return false;"
                "var setter=Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype,'value').set;"
                "setter.call(sel, matched.value);"
                "matched.selected=true;"
                "sel.dispatchEvent(new Event('input',{bubbles:true}));"
                "sel.dispatchEvent(new Event('change',{bubbles:true}));"
                "return {value: matched.value, text: matched.text, mode: mode};"
                "})(" + json.dumps(css) + "," + json.dumps(candidate) + ")"
            )
            if ok and isinstance(ok, dict):
                log.info(f"  region match {css}: {ok.get('text')!r} (value={ok.get('value')}, mode={ok.get('mode')})")
                return True
            return False
        except Exception as e:
            log.debug(f"  _safe_select_region err {css}: {e}")
            return False

    def fill_hosted_checkout_paypal(self, address: dict = None,
                                     country: str = "") -> dict:
        """在 hosted checkout 页选 PayPal、填账单、勾条款、点 Subscribe。

        Args:
            address: 账单地址 dict（可选）。
            country: 强制指定 hosted 页面账单国家。空字符串=使用 hosted 页面默认（US），
                     不读 config.CHECKOUT_COUNTRY（CHECKOUT_COUNTRY 现在只控制 phone_pool
                     和 BitBrowser 出口 IP，不影响 hosted checkout 长链）。

        注意 hosted checkout 长链是用 US/USD 创建的，PayPal 选项才会出现；
        因此默认账单国家也是 US，state select 走 50 州表。

        参考 hanyi0000/chatgpt-plus-automation-toolkit/modules/paypal_pay.py:fill_stripe
        关键: 使用 DrissionPage 元素的 input() 走 CDP 真键盘 (等价 Playwright Locator.fill)
        """
        try:
            from address_provider import (
                normalize_country_code as _norm_cc,
                jp_prefecture_aliases as _jp_aliases,
            )
        except ImportError:
            _norm_cc = lambda v: (v or "").upper()
            _jp_aliases = lambda v: [v]

        # 默认 US（保持原行为）；显式传 country 覆盖（hosted 页面 country dropdown 切到 JP 时
        # PayPal 选项可能消失，调用方要谨慎）。
        country = _norm_cc(country) or "US"
        log.info(f"[hosted] 账单国家: {country}")

        # 等页面 hydrate（参考实现 8s，我们用 6s）
        self._inject_js("checkout.js")
        time.sleep(6)

        # ========== 金额检测：必须是 $0.00（有 trial coupon），大于 0 直接标失败 ==========
        # 注意：JP 显示的是 ¥0 / 0 円，所以 currency 符号检测放宽
        amount_text = self._eval(
            "(function(){"
            "var els=document.querySelectorAll('[class*=\"Amount\"], [class*=\"amount\"], [class*=\"total\" i], [class*=\"due\" i]');"
            "for(var i=0;i<els.length;i++){var t=(els[i].textContent||'').trim();"
            "if(/(?:\\$|¥|￥|€)\\s*\\d/.test(t)||/\\d+\\s*円/.test(t))return t;}"
            # 兜底：扫全文找各币种金额
            "var body=document.body?document.body.innerText:'';"
            "var m=body.match(/(?:US)?(?:\\$|¥|￥|€)\\s*([\\d,]+(?:\\.\\d{2})?)/g) || body.match(/([\\d,]+)\\s*円/g);"
            "if(m&&m.length)return m[m.length-1];"
            "return null;})()"
        )
        if amount_text:
            import re as _re
            # 提取数字
            m = _re.search(r'[\d,]+(?:\.\d{2})?', amount_text)
            if m:
                amount_val = float(m.group().replace(",", ""))
                log.info(f"[hosted] 检测到金额: {amount_text} → {amount_val}")
                if amount_val > 0:
                    log.error(f"[hosted] ❌ 金额 {amount_val} > 0，没有 trial coupon，标记失败")
                    return {"status": "amount_not_zero", "amount": amount_val, "amount_text": amount_text}
                else:
                    log.info(f"[hosted] ✓ 金额 0，有 trial coupon")
            else:
                log.warning(f"[hosted] 金额文本无法解析: {amount_text}")
        else:
            log.warning("[hosted] 未检测到金额文本（继续跑）")

        # 解析最终地址：直接用调用方传进来的地址（卡自带的 US 地址）。
        # 不再调 address_provider —— 那是给国家化 hosted 页面用的；
        # 现在 hosted 长链是 US/USD，直接用 US 地址。
        addr = address if (isinstance(address, dict) and address.get("street")) else {
            "street": "123 Main St", "city": "New York",
            "state": "NY", "zip": "10001",
        }
        log.info(f"[hosted] 账单地址: {addr.get('street')} | {addr.get('city')} | {addr.get('state')} | {addr.get('zip')}")

        # 州/都道府県 标准化
        if country == "US":
            STATE_ABBR = {
                "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
                "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
                "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
                "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
                "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
                "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
                "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
                "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
                "new mexico": "NM", "new york": "NY", "north carolina": "NC",
                "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
                "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
                "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
                "vermont": "VT", "virginia": "VA", "washington": "WA",
                "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
            }
            state_raw = str(addr.get("state", "NY")).strip()
            state_value = STATE_ABBR.get(state_raw.lower(), state_raw)
            state_candidates = [state_value, state_raw]
        elif country == "JP":
            state_value = str(addr.get("state", "Tokyo")).strip() or "Tokyo"
            state_candidates = _jp_aliases(state_value)
        else:
            state_value = str(addr.get("state", "")).strip()
            state_candidates = [state_value]

        # ---------- 1) 选 PayPal ----------
        log.info("[hosted] 1/4 选 PayPal...")
        paypal_ok = False
        for attempt in range(5):
            r = self._eval("__gpt_selectPayPal()") or {}
            log.info(f"  selectPayPal #{attempt + 1}: {r}")
            time.sleep(2)
            if self._eval("__gpt_isPayPalSelected && __gpt_isPayPalSelected()"):
                paypal_ok = True
                log.info("  PayPal 已选中")
                break
        if not paypal_ok:
            return {"status": "paypal_select_failed"}

        time.sleep(2)

        # ---------- 2) 国家选择 ----------
        log.info(f"[hosted] 2/4 国家选 {country}...")
        country_changed = self._safe_select("#billingCountry", country)
        log.info(f"  country={country}: {country_changed}")
        time.sleep(2.5)

        # 验证国家是否切到目标
        country_val = self._eval(
            "(document.getElementById('billingCountry') || {}).value"
        )
        if country_val != country:
            log.warning(f"  国家仍为 {country_val}，再次尝试 → {country}")
            self._safe_select("#billingCountry", country)
            time.sleep(2)

        # ---------- 3) 点"手动输入地址" + 填地址 ----------
        log.info("[hosted] 3/4 填账单...")
        # 点"手动输入地址"（多语言：中 / 英 / 日）
        clicked_manual = self._eval(
            "(function(){var bs=document.querySelectorAll('button,a,[role=\"button\"]');"
            "for(var i=0;i<bs.length;i++){var t=(bs[i].textContent||'').trim();"
            "if(t==='手动输入地址'||/^enter address manually$/i.test(t)"
            "||/住所を手動で入力|手動で入力|手動入力/.test(t)){bs[i].click();return true;}}return false;})()"
        )
        log.info(f"  click [手动输入地址]: {clicked_manual}")
        time.sleep(2)

        # ESC 关掉所有地址联想下拉，确保字段是干净的
        try:
            self.page.actions.key_down("ESCAPE").key_up("ESCAPE")
        except Exception:
            pass
        time.sleep(0.5)

        log.info(f"  填: street={addr['street']} city={addr['city']} state={state_value} zip={addr['zip']}")

        # 字段填写（每填一个就关一次下拉，防止 Stripe 联想框污染下一个字段）
        FIELDS = [
            ("#billingAddressLine1", addr.get("street", ""), "street"),
            ("#billingLocality", addr.get("city", ""), "city"),
            ("#billingPostalCode", addr.get("zip", ""), "zip"),
        ]
        for css, val, label in FIELDS:
            self._safe_input(css, val, dismiss_autocomplete=True)
            time.sleep(0.6)

        # 重新校验字段；如果有的字段串了或为空，单独重填
        for css, expected, label in FIELDS:
            actual = self._eval(
                f"(document.querySelector({json.dumps(css)}) || {{}}).value || ''"
            ) or ""
            if actual != expected:
                log.warning(f"  {label} 不匹配：expected={expected[:40]} actual={actual[:60]}，重填")
                self._safe_input(css, expected, dismiss_autocomplete=True)
                time.sleep(0.4)

        # 州 / 都道府県 / 省 select：尝试 candidates 列表里每个值
        # （JP 模式下 select 可能 option text 是日文，我们 candidate 包含英日两套）
        area_filled = False
        for cand in state_candidates:
            if not cand:
                continue
            if self._safe_select_region("#billingAdministrativeArea", cand, country=country):
                area_filled = True
                break
        if not area_filled:
            log.warning(f"  state/region 没匹配上任何 candidate: {state_candidates}")
        time.sleep(0.6)

        # 勾选服务条款
        self._eval("__gpt_checkTerms && __gpt_checkTerms()")
        time.sleep(0.5)

        # 关掉地址自动补全下拉
        try:
            self.page.actions.key_down("ESCAPE").key_up("ESCAPE")
        except Exception:
            pass
        time.sleep(0.5)

        # 最终回读所有字段
        final_vals = self._eval(
            "(function(){return {"
            "country: (document.getElementById('billingCountry')||{}).value,"
            "street: (document.getElementById('billingAddressLine1')||{}).value,"
            "city: (document.getElementById('billingLocality')||{}).value,"
            "zip: (document.getElementById('billingPostalCode')||{}).value,"
            "state: (document.getElementById('billingAdministrativeArea')||{}).value,"
            "};})()"
        )
        log.info(f"  最终字段: {final_vals}")

        # 等待 submit 变 complete
        log.info("[hosted] 4/4 等 submit ready...")
        for i in range(10):
            cls = self._eval(
                "(document.querySelector('button[data-testid=\"hosted-payment-submit-button\"]')||{}).className"
            ) or ""
            if "SubmitButton--complete" in cls:
                log.info(f"  submit ready: {cls[:60]}")
                break
            time.sleep(1)
        else:
            log.warning(f"  submit 仍 incomplete: {cls[:60]}")
            # 仍尝试点击；OpenAI 有时 class 不变但能点
        time.sleep(1)

        # ---------- 4) 点 Subscribe，等跳转 ----------
        log.info("  click Subscribe...")
        subscribe_selectors = [
            'css:button[data-testid="hosted-payment-submit-button"]',
            'css:button.SubmitButton--complete',
            'css:button[type="submit"]',
        ]
        clicked = False
        for sel in subscribe_selectors:
            try:
                btn = self.page.ele(sel, timeout=2)
                if not btn:
                    continue
                btn.click()
                log.info(f"  click via {sel}")
                clicked = True
                break
            except Exception as e:
                log.debug(f"  {sel} click err: {e}")
        if not clicked:
            # 兜底用 JS 点
            self._eval("__gpt_clickSubscribe && __gpt_clickSubscribe()")

        # 等跳转 paypal.com（最多 60s，30s 没跳就再点一次）
        for attempt in range(2):
            for _ in range(30 if attempt == 0 else 30):
                if "paypal.com" in (self.page.url or ""):
                    log.info(f"  ✓ 已跳到 paypal.com: {self.page.url[:80]}")
                    return {"status": "submitted", "url": self.page.url}
                time.sleep(1)
            if attempt == 0:
                log.warning("  30s 未跳 paypal.com，再点一次 Subscribe")
                for sel in subscribe_selectors:
                    try:
                        btn = self.page.ele(sel, timeout=1)
                        if btn:
                            btn.click()
                            break
                    except Exception:
                        pass

        log.error(f"  60s 内未跳 PayPal, current_url={self.page.url[:120]}")
        return {"status": "submit_no_redirect", "url": self.page.url}

    # ============ Custom UI checkout（chatgpt.com/checkout/openai_ie/...）==========

    def fill_custom_checkout_paypal(self, address: dict = None,
                                    *, navigate_url: str = "",
                                    wait_redirect_s: int = 90) -> dict:
        """美区 custom UI 的 PayPal 自动化（payment_method=paypal_custom 用）。

        和 hosted 版 (`fill_hosted_checkout_paypal`) 的区别：
          - 这里的页面是 chatgpt.com/checkout/openai_ie/cs_live_xxx（短链）
          - 没有 PayPal accordion，PayPal 是页内 radio
          - Submit 按钮 testid 不一定是 hosted-payment-submit-button
          - 国家/地址字段大体复用 hosted 的 ID（OpenAI React 共用 UI 组件）

        Args:
            address: 账单地址（可选，默认用纽约 dummy 地址）
            navigate_url: 显式指定要去的 URL（一般是 checkout() 返回的 preferred_url）。
                          如果当前页面不是 custom checkout 会先跳过去。
            wait_redirect_s: 点完 Subscribe 等多少秒跳转 paypal.com / success
        """
        addr = address if (isinstance(address, dict) and address.get("street")) else {
            "street": "123 Main St", "city": "New York",
            "state": "NY", "zip": "10001",
        }
        log.info(f"[custom] 账单: {addr.get('street')} | {addr.get('city')} | "
                 f"{addr.get('state')} | {addr.get('zip')}")

        # 0) 确保已经在 custom checkout 页 + 页面真的渲染了内容
        cur_url = self.page.url or ""
        on_custom = "chatgpt.com/checkout/" in cur_url and "/cs_live_" in cur_url

        def _body_text_len() -> int:
            try:
                v = self._eval("(function(){var b=document.body;return b?b.innerText.length:0;})()")
                return int(v) if isinstance(v, (int, float)) else 0
            except Exception:
                return 0

        if not on_custom:
            target = navigate_url or ""
            if not target:
                return {"status": "not_on_custom_checkout", "url": cur_url}
            log.info(f"[custom] 跳转到 {target[:90]}")
            self.navigate(target)
            # 等页面真的跳过去（最多 20s）
            for _ in range(20):
                u = self.page.url or ""
                if "chatgpt.com/checkout/" in u and "/cs_live_" in u:
                    log.info(f"[custom] ✓ 已落到 {u[:90]}")
                    break
                time.sleep(1)
            time.sleep(2)
        else:
            log.info(f"[custom] 已在 {cur_url[:90]}")

        # 校验 body 真的有内容（防止"URL 对了但页面空白"的情况）；
        # 没内容就 reload + 等待，最多 reload 2 次
        for retry in range(3):
            blen = _body_text_len()
            log.info(f"  body 文本长度 = {blen}")
            if blen >= 200:
                break
            if retry < 2:
                log.warning(f"  ⚠ body 几乎空白（{blen} chars），刷新页面重试")
                try:
                    self.page.refresh()
                except Exception:
                    try:
                        self.page.get(self.page.url)
                    except Exception:
                        pass
                time.sleep(8)
            else:
                log.error(f"  ✗ 多次刷新后页面仍空白，url={self.page.url[:90]}")

        # 给 React + Stripe Elements 最后 hydrate 时间
        log.info("[custom] 等待页面 hydrate (12s)...")
        time.sleep(12)

        self._inject_js("checkout.js")
        time.sleep(2)

        # ---------- 1) 选 PayPal ----------
        # 关键：chatgpt.com/checkout/openai_llc 这套 custom UI 把
        # "カード / PayPal" tab 装在 Stripe Express Checkout 的跨域 iframe 里
        # （__privateStripeFrame*，src=js.stripe.com）。主页面 JS 找不到。
        # 必须穿透 iframe 用 DrissionPage 的 get_frame / get_frames + frame.ele(...)
        # 查找点击。
        log.info("[custom] 1/4 选 PayPal tab（含 iframe 穿透）...")

        def _click_paypal_in_frames() -> bool:
            """在所有 iframe 内查找 PayPal 文本元素并点击。"""
            try:
                frames = self.page.get_frames()
            except Exception as e:
                log.debug(f"  get_frames 失败: {e}")
                return False
            log.info(f"  发现 {len(frames)} 个 frame")
            for idx, fr in enumerate(frames):
                try:
                    src = ""
                    try:
                        src = fr.attr("src") or ""
                    except Exception:
                        pass
                    log.debug(f"  frame[{idx}] src={src[:80]}")
                    # frame.ele('text:PayPal') 在跨域 stripe iframe 里也能跑
                    for selector in (
                        'text:PayPal',
                        'css:[role="tab"]',
                        'css:button',
                        'css:label',
                        'css:[role="button"]',
                    ):
                        try:
                            if selector == 'text:PayPal':
                                el = fr.ele(selector, timeout=1.5)
                                if el:
                                    try:
                                        el.click()
                                        log.info(f"  ✓ frame[{idx}] {selector} 点击成功 src={src[:60]}")
                                        return True
                                    except Exception as ce:
                                        log.debug(f"  frame[{idx}] {selector} click 失败: {ce}")
                                continue
                            # 其它 selector：枚举全部，按文本过滤
                            try:
                                els = fr.eles(selector, timeout=1)
                            except Exception:
                                continue
                            for el in (els or []):
                                try:
                                    t = (el.text or "").strip()
                                except Exception:
                                    continue
                                if t and "paypal" in t.lower() and len(t) < 60:
                                    try:
                                        el.click()
                                        log.info(f"  ✓ frame[{idx}] {selector} text='{t[:30]}' src={src[:60]}")
                                        return True
                                    except Exception as ce:
                                        log.debug(f"  frame[{idx}] {selector} click 失败: {ce}")
                        except Exception as fe:
                            log.debug(f"  frame[{idx}] {selector} 异常: {fe}")
                except Exception as e:
                    log.debug(f"  frame[{idx}] 处理异常: {e}")
            return False

        paypal_ok = False
        # 加大重试次数到 12 次（每次 ~3s = 36s 给页面慢慢渲染）
        for attempt in range(12):
            stage = self._eval("__gpt_custom_getStage && __gpt_custom_getStage()") or ""
            log.info(f"  stage #{attempt + 1}: {stage}")
            if stage == "loading":
                # 第 4 次还在 loading，dump 一下 debug
                if attempt == 3:
                    dbg = self._eval("__gpt_custom_debug && __gpt_custom_debug()") or {}
                    log.info(f"  custom_debug: {dbg}")
                time.sleep(3)
                continue
            if stage in ("paypal_selected", "fill_billing", "submit_ready"):
                paypal_ok = True
                break
            # 优先穿透 iframe 找（这套 UI 的真情况）
            if _click_paypal_in_frames():
                time.sleep(2)
                if self._eval("__gpt_custom_isPaypalSelected && __gpt_custom_isPaypalSelected()"):
                    paypal_ok = True
                    log.info("  PayPal 已选中（iframe 内点击）")
                    break
                # iframe 点了但主页 stage 没变，再试 1s
                time.sleep(1)
                continue
            # iframe 找不到 → 退回主页面 JS（少数老 UI 用）
            r = self._eval("__gpt_custom_selectPayPal && __gpt_custom_selectPayPal()") or {}
            log.info(f"  custom_selectPayPal: {r}")
            time.sleep(2)
            if self._eval("__gpt_custom_isPaypalSelected && __gpt_custom_isPaypalSelected()"):
                paypal_ok = True
                log.info("  PayPal 已选中")
                break
        if not paypal_ok:
            dbg = self._eval("__gpt_custom_debug && __gpt_custom_debug()") or {}
            log.error(f"  ✗ PayPal tab 选择失败  debug={dbg}")
            return {"status": "paypal_select_failed",
                    "stage": self._eval("__gpt_custom_getStage && __gpt_custom_getStage()"),
                    "debug": dbg}

        time.sleep(1.5)

        # ---------- 2) 填账单地址 ----------
        # OpenAI custom UI 字段都在 Stripe iframe 里。策略：
        #   1. 先定位包含 input[autocomplete="name"] 的 frame（地址表单 frame）
        #   2. 在这一个 frame 里一次性找齐全部字段（缓存）
        #   3. 用 DrissionPage 原生 el.input(value, clear=True)（CDP 真键盘 + 清空）
        #   4. 都道府県是 combobox（不是 native select）：click 展开 + 选 option
        log.info("[custom] 2/4 填账单地址（东京随机地址）...")

        # ============ 东京都内随机日本地址 ============
        # 23 区 + 多个具体街道，每次随机抽。state 锁定"東京都"匹配 OpenAI dropdown option
        import random as _rd
        TOKYO_ADDR_POOL = [
            # 23 区中心商务/住宅地址（真实存在的街区组合）
            {"street": "Aoba 2-2-11", "city": "Toshima-ku", "zip": "171-0022"},
            {"street": "Marunouchi 1-9-2", "city": "Chiyoda-ku", "zip": "100-0005"},
            {"street": "Nishi-Shinjuku 2-8-1", "city": "Shinjuku-ku", "zip": "163-8001"},
            {"street": "Roppongi 6-10-1", "city": "Minato-ku", "zip": "106-6108"},
            {"street": "Akasaka 9-7-1", "city": "Minato-ku", "zip": "107-6238"},
            {"street": "Shibuya 2-21-1", "city": "Shibuya-ku", "zip": "150-0002"},
            {"street": "Ebisu 4-20-3", "city": "Shibuya-ku", "zip": "150-0013"},
            {"street": "Daikanyama 16-15", "city": "Shibuya-ku", "zip": "150-0034"},
            {"street": "Ueno 7-1-1", "city": "Taito-ku", "zip": "110-0005"},
            {"street": "Asakusa 2-3-1", "city": "Taito-ku", "zip": "111-0032"},
            {"street": "Ginza 4-6-16", "city": "Chuo-ku", "zip": "104-0061"},
            {"street": "Nihonbashi 1-4-1", "city": "Chuo-ku", "zip": "103-0027"},
            {"street": "Tsukiji 4-1-1", "city": "Chuo-ku", "zip": "104-0045"},
            {"street": "Shinagawa 2-16-3", "city": "Shinagawa-ku", "zip": "140-0001"},
            {"street": "Osaki 1-11-1", "city": "Shinagawa-ku", "zip": "141-0032"},
            {"street": "Meguro 2-4-2", "city": "Meguro-ku", "zip": "153-0063"},
            {"street": "Nakameguro 1-4-5", "city": "Meguro-ku", "zip": "153-0061"},
            {"street": "Sangenjaya 2-13-2", "city": "Setagaya-ku", "zip": "154-0024"},
            {"street": "Shimokitazawa 2-26-13", "city": "Setagaya-ku", "zip": "155-0031"},
            {"street": "Ikebukuro 1-1-25", "city": "Toshima-ku", "zip": "171-0014"},
            {"street": "Sugamo 1-12-1", "city": "Toshima-ku", "zip": "170-0002"},
            {"street": "Shinjuku 3-38-1", "city": "Shinjuku-ku", "zip": "160-0022"},
            {"street": "Kagurazaka 6-43", "city": "Shinjuku-ku", "zip": "162-0825"},
            {"street": "Akihabara 1-15-16", "city": "Chiyoda-ku", "zip": "101-0021"},
            {"street": "Kanda 1-9-1", "city": "Chiyoda-ku", "zip": "101-0044"},
            {"street": "Roppongi 7-7-7", "city": "Minato-ku", "zip": "106-0032"},
            {"street": "Azabudai 1-1-20", "city": "Minato-ku", "zip": "106-0041"},
            {"street": "Hiroo 1-1-39", "city": "Shibuya-ku", "zip": "150-0012"},
            {"street": "Ojima 6-2-15", "city": "Koto-ku", "zip": "136-0072"},
            {"street": "Toyosu 5-6-15", "city": "Koto-ku", "zip": "135-0061"},
        ]
        chosen = _rd.choice(TOKYO_ADDR_POOL)

        # 随机日本姓名（漢字）
        try:
            from _kana_helper import random_jp_name as _jp_name
            n = _jp_name() or {}
            full_name = f"{n.get('last_kanji', '山田')} {n.get('first_kanji', '太郎')}"
        except Exception:
            JP_LAST = ["山田", "佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "中村",
                       "小林", "加藤", "吉田", "山本", "斉藤", "松本", "井上", "木村"]
            JP_FIRST = ["太郎", "次郎", "翔太", "蓮", "陽菜", "結衣", "湊", "美月",
                        "海斗", "大樹", "颯太", "凛", "葵", "拓也", "悠人", "桜"]
            full_name = f"{_rd.choice(JP_LAST)} {_rd.choice(JP_FIRST)}"

        # state 用日文 "東京都"，OpenAI 的都道府県 dropdown option 是日文
        jp_addr = {
            "street": chosen["street"],
            "city": chosen["city"],
            "state": "東京都",
            "zip": chosen["zip"],
        }
        log.info(f"  JP 地址: {jp_addr['zip']} {jp_addr['state']} "
                 f"{jp_addr['city']} {jp_addr['street']}")
        log.info(f"  氏名: {full_name}")

        # ============ 找包含表单的 frame ============
        # OpenAI custom UI 不一定用 autocomplete 属性 —— 改成多 selector 探测，
        # 任一命中就认为这是表单 frame。
        FORM_PROBE_SELECTORS = [
            'input[autocomplete="name"]',
            'input[autocomplete="postal-code"]',
            'input[autocomplete="address-line1"]',
            'input[name="name"]',
            # 英文 placeholder（美区表单）
            'input[placeholder*="Full name" i]',
            'input[placeholder*="Address" i]',
            'input[placeholder*="ZIP" i]',
            'input[placeholder*="Postal" i]',
            'input[placeholder*="City" i]',
            'input[aria-label*="Full name" i]',
            'input[aria-label*="Address" i]',
            'input[aria-label*="ZIP" i]',
            # 中文 placeholder（截图显示是中文 UI："全名 / 地址第 1 行 / 城市"）
            'input[placeholder*="全名"]',
            'input[placeholder*="地址"]',
            'input[placeholder*="邮编"]',
            'input[placeholder*="城市"]',
            'input[placeholder*="姓名"]',
            # 日文 placeholder（保留兼容）
            'input[placeholder*="氏名"]',
            'input[placeholder*="郵便"]',
            'input[placeholder*="住所"]',
            'input[aria-label*="氏名"]',
            'input[aria-label*="郵便"]',
        ]

        def _probe_form_in(ctx) -> bool:
            for sel in FORM_PROBE_SELECTORS:
                try:
                    el = ctx.ele(f"css:{sel}", timeout=0.2)
                    if el:
                        return True
                except Exception:
                    pass
            return False

        target_frame = None
        for try_i in range(15):  # 等更久 30s（地址表单可能 hydrate 慢）
            # 主页直接探测
            try:
                if _probe_form_in(self.page):
                    target_frame = self.page
                    break
            except Exception:
                pass
            # 所有 frame 都探测
            try:
                frames = list(self.page.get_frames())
            except Exception:
                frames = []
            for f in frames:
                if _probe_form_in(f):
                    target_frame = f
                    break
            if target_frame:
                break
            time.sleep(2)

        if not target_frame:
            log.warning("  ✗ 找不到含表单的 frame（30s 内表单未渲染）")
            # 最后兜底：直接用主页
            target_frame = self.page
        else:
            log.info(f"  ✓ 定位到表单 frame ({'page' if target_frame is self.page else 'iframe'})")

        # 渐进式填表：每填一个等下个字段出现（OpenAI custom UI 的特点）
        def _wait_input(selectors: list[str], wait_s: float = 5.0):
            """在 target_frame 等某个 selector 命中的 input 出现。"""
            t0 = time.time()
            while time.time() - t0 < wait_s:
                for sel in selectors:
                    try:
                        el = target_frame.ele(f"css:{sel}", timeout=0.3)
                        if el:
                            return el
                    except Exception:
                        continue
                time.sleep(0.3)
            return None

        def _fill_one(selectors: list[str], value: str, label: str,
                      wait_s: float = 5.0) -> bool:
            """渐进式填字段 —— 必须用 click + actions.type 走真键盘事件，
            DrissionPage 的 el.input() 是 CDP Input.insertText，React controlled
            input 不会触发 onChange，UI 显示是空的 + 下个字段不会出现。"""
            el = _wait_input(selectors, wait_s=wait_s)
            if not el:
                log.warning(f"  ✗ {label} 没出现（{wait_s}s 内）")
                return False
            try:
                el.click()
                time.sleep(0.15)
                # 全选清空已有内容
                try:
                    self.page.actions.key_down("ControlOrCommand").type("a").key_up("ControlOrCommand")
                    time.sleep(0.05)
                    self.page.actions.key_down("BACKSPACE").key_up("BACKSPACE")
                    time.sleep(0.05)
                except Exception:
                    pass
                # 真键盘逐字符敲（每键 keydown/input/keyup 全套事件 → React onChange 必收到）
                self.page.actions.type(value, interval=0.025)
                log.info(f"  ✓ 填 {label} = {value[:30]}")
                return True
            except Exception as e:
                log.warning(f"  ✗ {label} 失败: {e}")
                return False

        # Step 1: 氏名
        _fill_one(
            ['input[autocomplete="name"]', 'input[name*="name"]',
             'input[placeholder*="氏名"]', 'input[aria-label*="氏名"]',
             'input[placeholder*="Full name"]'],
            full_name, "氏名", wait_s=5,
        )
        time.sleep(0.6)

        # Step 2: 郵便番号（填完氏名才出现）
        _fill_one(
            ['input[autocomplete="postal-code"]', 'input[name*="postal"]',
             'input[placeholder*="郵便"]', 'input[aria-label*="郵便"]'],
            jp_addr.get("zip", "100-0005"), "郵便番号", wait_s=6,
        )
        time.sleep(1.5)  # 等地址 autocomplete 候选弹出

        # Step 3: 处理地址 autocomplete 建议
        # 关键：填完邮编后 OpenAI 会弹出地址候选（截图实测 5 个 option，
        # 都是 "日本、〒106-XXXX 東京都港区六本木..." 这种）。选第一个即可一键
        # 自动填满都道府県/都市名/住所，省掉手填 3 个字段。
        # 但 option 的 bounding rect 是 0×0（absolute 定位），DrissionPage
        # actionability check 拒绝 click —— 改用键盘 ArrowDown + Enter 选中。
        log.info("  尝试用键盘选第一个地址 autocomplete 建议...")
        autocomplete_picked = False
        try:
            # 先确认 listbox 已展开（有 [role="option"] 出现）
            opts_count = 0
            t0 = time.time()
            while time.time() - t0 < 4:
                try:
                    opts = target_frame.eles('css:[role="option"]')
                    opts_count = len(opts) if opts else 0
                except Exception:
                    opts_count = 0
                if opts_count > 0:
                    break
                time.sleep(0.4)
            log.info(f"    autocomplete 候选数={opts_count}")
            if opts_count > 0:
                # 按 ↓ 高亮第一个 → Enter 选中
                self.page.actions.key_down("DOWN").key_up("DOWN")
                time.sleep(0.3)
                self.page.actions.key_down("ENTER").key_up("ENTER")
                time.sleep(1.0)
                autocomplete_picked = True
                log.info(f"  ✓ 选第一个 autocomplete 建议（自动填都道府県/都市名/住所）")
                # 清空"住所(2 行目)"——autocomplete 有时会自动填这一字段，按用户要求强制留空
                try:
                    addr2_el = target_frame.ele(
                        'css:input[autocomplete="address-line2"]', timeout=0.6
                    )
                    if addr2_el:
                        cur = (addr2_el.value or addr2_el.attr("value") or "").strip()
                        if cur:
                            addr2_el.click()
                            time.sleep(0.1)
                            try:
                                self.page.actions.key_down("ControlOrCommand").type("a").key_up("ControlOrCommand")
                                time.sleep(0.05)
                                self.page.actions.key_down("BACKSPACE").key_up("BACKSPACE")
                            except Exception:
                                pass
                            log.info(f"  ✓ 清空 住所(2行目)（之前 autocomplete 填了 '{cur[:30]}'）")
                except Exception as e:
                    log.debug(f"  清空住所2失败: {e}")
        except Exception as e:
            log.debug(f"  autocomplete 键盘选择失败: {e}")

        # Step 4: autocomplete 没成功 → 按 DOM 顺序枚举 frame 内可见 input
        # （selector 匹配往往失败 —— OpenAI 没有标准 autocomplete/placeholder 属性，
        # 但所有 input 在 DOM 里按表单顺序排列：
        #   美国: [Full name][Country select][Address line 1][Address line 2][City][State select][ZIP]
        #   日本: [氏名][Country select][郵便番号][都道府県 select/combobox][都市名][住所1][住所2]）
        if not autocomplete_picked:
            state_val = jp_addr.get("state", "")  # 美国是 "NY" 缩写；日本是 "東京都"
            log.info(f"  autocomplete 失败，按 DOM 顺序枚举 input 填后续字段")

            def _list_visible_inputs():
                """按 DOM 顺序拿到 frame 内所有可见 text/tel/select 控件。"""
                try:
                    inputs = target_frame.eles('css:input, select')
                except Exception:
                    return []
                out = []
                for el in (inputs or []):
                    try:
                        # 跳过 hidden / radio / checkbox / submit
                        t = (el.attr("type") or "").lower()
                        if t in ("hidden", "radio", "checkbox", "submit", "button", "file"):
                            continue
                        # 检查可见
                        try:
                            rect = el.rect.size
                            if not rect or rect[0] < 1 or rect[1] < 1:
                                continue
                        except Exception:
                            pass
                        out.append(el)
                    except Exception:
                        continue
                return out

            # ============ State select（美国） ============
            # 美国表单 state 是 native <select> 或 React combobox。优先尝试 native select：
            try:
                for ctx in [target_frame]:
                    for sel in (
                        'select[autocomplete="address-level1"]',
                        'select[name*="state"]',
                        'select[name*="region"]',
                        'select[aria-label*="State" i]',
                    ):
                        try:
                            sel_el = ctx.ele(f"css:{sel}", timeout=0.4)
                            if not sel_el:
                                continue
                            try:
                                sel_el.select(state_val)
                                log.info(f"  ✓ 选 State (native) = {state_val}")
                                raise StopIteration
                            except StopIteration:
                                raise
                            except Exception:
                                continue
                        except StopIteration:
                            raise
                        except Exception:
                            pass
            except StopIteration:
                pass

            inputs = _list_visible_inputs()
            log.info(f"    DOM 顺序可见 input/select 数量 = {len(inputs)}")
            empty_inputs = []
            for el in inputs:
                try:
                    cur_val = (el.value or el.attr("value") or "").strip()
                    if cur_val:
                        continue  # 已填，跳过
                    # 只填 input（不填 select；select 已在上面单独处理）
                    if (el.tag or "").lower() == "select":
                        continue
                    empty_inputs.append(el)
                except Exception:
                    continue
            log.info(f"    待填空字段数 = {len(empty_inputs)}")

            todo_values = [
                ("Address line 1", jp_addr.get("street", "")),
                ("City", jp_addr.get("city", "")),
                # 注意：日本 UI 的 state 也是 input/combobox，需要兜底；美国 UI 已在上面 select 处理
                ("State / Region", state_val),
            ]
            for i, (label, value) in enumerate(todo_values):
                if i >= len(empty_inputs):
                    log.info(f"  ⓘ {label} 找不到对应 input（可能已用 select 填或表单缺此字段）")
                    continue
                el = empty_inputs[i]
                try:
                    el.click()
                    time.sleep(0.15)
                    try:
                        self.page.actions.key_down("ControlOrCommand").type("a").key_up("ControlOrCommand")
                        time.sleep(0.05)
                        self.page.actions.key_down("BACKSPACE").key_up("BACKSPACE")
                    except Exception:
                        pass
                    self.page.actions.type(value, interval=0.025)
                    log.info(f"  ✓ 填 {label} = {value[:30]} (DOM-order #{i})")
                    time.sleep(0.5)
                except Exception as e:
                    log.warning(f"  ✗ {label} 填失败: {e}")
            # autocomplete 已自动填后续字段，但建议地址里的"街道号"通常是大致地址，
            # 仍需要补一下"住所(1行目)"的具体门牌号
            log.info("  autocomplete 已自动填都道府県/都市名/住所基础部分")
            # 给 React 时间渲染
            time.sleep(1.5)

        # 服务条款（如果有）
        try:
            self._eval("__gpt_checkTerms && __gpt_checkTerms()")
        except Exception:
            pass

        time.sleep(1)
        try:
            self.page.actions.key_down("ESCAPE").key_up("ESCAPE")
        except Exception:
            pass

        # ---------- 3) 校验所有字段都填好了 + 自动回填漏掉的 ----------
        log.info("[custom] 3/4 校验字段并回填漏掉的...")
        time.sleep(1.8)  # 等 React 渲染完 autocomplete 选中后的字段

        # 最终保险：强制清空"住所(2 行目)"（按用户要求始终留空）
        try:
            for ctx in [self.page] + (self.page.get_frames() if self.page else []):
                try:
                    a2 = ctx.ele('css:input[autocomplete="address-line2"]', timeout=0.3)
                    if not a2:
                        continue
                    cur = (a2.value or a2.attr("value") or "").strip()
                    if not cur:
                        continue
                    a2.click()
                    time.sleep(0.1)
                    try:
                        self.page.actions.key_down("ControlOrCommand").type("a").key_up("ControlOrCommand")
                        time.sleep(0.05)
                        self.page.actions.key_down("BACKSPACE").key_up("BACKSPACE")
                    except Exception:
                        pass
                    log.info(f"  ✓ 强制清空 住所(2行目) （原值='{cur[:30]}'）")
                    break
                except Exception:
                    continue
        except Exception as e:
            log.debug(f"  住所2 清空兜底失败: {e}")

        def _enum_visible_inputs():
            """按 DOM 顺序拿到 frame 内所有可见的 text/tel input（不含 select、checkbox）。"""
            try:
                inputs = target_frame.eles('css:input')
            except Exception:
                return []
            out = []
            for el in (inputs or []):
                try:
                    t = (el.attr("type") or "").lower()
                    if t in ("hidden", "radio", "checkbox", "submit", "button", "file"):
                        continue
                    try:
                        rect = el.rect.size
                        if not rect or rect[0] < 1 or rect[1] < 1:
                            continue
                    except Exception:
                        pass
                    out.append(el)
                except Exception:
                    continue
            return out

        def _read_field_label(el) -> str:
            """读取 input 的字段语义标签（优先 placeholder / aria-label / 前置 <label>）。"""
            for attr in ("placeholder", "aria-label", "name", "autocomplete"):
                try:
                    v = el.attr(attr)
                    if v:
                        return v.strip()
                except Exception:
                    pass
            # 取前一个 sibling 的文字（OpenAI 表单常见做法）
            try:
                txt = ""
                lbl = target_frame.ele(f'xpath://*[@id="{el.attr("id")}"]/preceding::*[1]', timeout=0.2)
                if lbl:
                    txt = (lbl.text or "").strip()
                if txt:
                    return txt
            except Exception:
                pass
            return ""

        # 期望值映射（按字段语义关键词，多语言：英文 / 中文 / 日文）
        # 注意优先级顺序：Name → ZIP → State → City → Address line 1
        # 美国 UI: Full name / ZIP / State (select) / City / Address line 1
        # 中文 UI: 全名 / 邮编 / 州/省 / 城市 / 地址第 1 行
        # 日文 UI: 氏名 / 郵便番号 / 都道府県 / 都市名 / 住所(1行目)
        EXPECTED_VALUES = [
            (["氏名", "お名前", "name", "Full name", "全名", "姓名"],
             full_name, "Full Name"),
            (["郵便", "postal", "zip", "邮编", "邮政"],
             jp_addr.get("zip", ""), "ZIP/Postal"),
            (["都道府県", "state", "region", "administrative", "address-level1", "州", "省"],
             jp_addr.get("state", ""), "State/Region"),
            (["都市", "city", "address-level2", "城市", "市区町村"],
             jp_addr.get("city", ""), "City"),
            (["住所 (1", "住所(1", "住所1", "address-line1", "line1", "address line 1",
              "地址第 1 行", "地址 1", "地址1"],
             jp_addr.get("street", ""), "Address Line 1"),
        ]

        def _match_field(label_text: str, keywords: list[str]) -> bool:
            t = (label_text or "").lower()
            for kw in keywords:
                if kw.lower() in t:
                    return True
            return False

        # Pass 1: 检查每个 input 的当前值
        inputs = _enum_visible_inputs()
        log.info(f"  当前可见 input 数={len(inputs)}")

        # 把每个 input 跟期望字段匹配上
        # 同一个 input 只匹配一个字段（按 EXPECTED_VALUES 顺序优先匹配）
        used_input_idxs = set()
        field_status = {}  # label -> (idx, current_value)

        for keywords, expected, label in EXPECTED_VALUES:
            for i, el in enumerate(inputs):
                if i in used_input_idxs:
                    continue
                lbl_text = _read_field_label(el)
                if _match_field(lbl_text, keywords):
                    try:
                        cur = (el.value or el.attr("value") or "").strip()
                    except Exception:
                        cur = ""
                    field_status[label] = (i, cur, el)
                    used_input_idxs.add(i)
                    break

        # Pass 2: 回填漏掉的字段
        missing = []
        for keywords, expected, label in EXPECTED_VALUES:
            tup = field_status.get(label)
            if not tup:
                # 没在 DOM 里识别出来 —— 可能 autocomplete 已自动填，或字段没出现
                # （如果是 autocomplete 已填，DOM 里会有同位置的 input 但 label 文本变了）
                continue
            idx, cur, el = tup
            if cur:
                log.info(f"  ✓ {label} 当前值={cur[:30]}")
                continue
            # 空 → 补填
            log.warning(f"  ⚠ {label} 为空，回填...")
            try:
                el.click()
                time.sleep(0.15)
                try:
                    self.page.actions.key_down("ControlOrCommand").type("a").key_up("ControlOrCommand")
                    time.sleep(0.05)
                    self.page.actions.key_down("BACKSPACE").key_up("BACKSPACE")
                except Exception:
                    pass
                self.page.actions.type(expected, interval=0.025)
                log.info(f"  ✓ 回填 {label} = {expected[:30]}")
                time.sleep(0.5)
            except Exception as e:
                log.warning(f"  ✗ {label} 回填失败: {e}")
                missing.append(label)

        # Pass 3: 如果还有 EXPECTED_VALUES 完全没在 DOM 里识别出来，说明字段可能没出现
        # 或者 label 提取失败，按 DOM 顺序枚举空 input 兜底
        unmatched_expected = [
            (kws, val, lbl) for kws, val, lbl in EXPECTED_VALUES
            if lbl not in field_status
        ]
        if unmatched_expected:
            log.info(f"  还有 {len(unmatched_expected)} 个字段未识别，按 DOM 顺序补填空 input")
            inputs = _enum_visible_inputs()  # 重新读
            empty_input_idxs = []
            for i, el in enumerate(inputs):
                try:
                    cur = (el.value or el.attr("value") or "").strip()
                    if not cur:
                        empty_input_idxs.append(i)
                except Exception:
                    continue
            log.info(f"    空 input 数 = {len(empty_input_idxs)}")
            for j, (kws, val, lbl) in enumerate(unmatched_expected):
                if j >= len(empty_input_idxs):
                    log.warning(f"  ✗ {lbl} 找不到空 input 位置")
                    missing.append(lbl)
                    continue
                el = inputs[empty_input_idxs[j]]
                try:
                    el.click()
                    time.sleep(0.15)
                    self.page.actions.type(val, interval=0.025)
                    log.info(f"  ✓ 兜底回填 {lbl} = {val[:30]} (DOM-order)")
                    time.sleep(0.4)
                except Exception as e:
                    log.warning(f"  ✗ {lbl} 兜底回填失败: {e}")
                    missing.append(lbl)

        if missing:
            log.warning(f"  最终仍缺: {missing}（继续点 Subscribe，让 OpenAI 自己提示）")
        else:
            log.info("  ✓ 所有必填字段已确认有值")

        # ---------- 4) 点 Subscribe ----------
        log.info("[custom] 4/4 点 Subscribe...")
        subscribe_selectors = [
            'css:button[data-testid="hosted-payment-submit-button"]',
            'css:button[data-testid="submit-button"]',
            'css:button.SubmitButton--complete',
            'css:form button[type="submit"]',
            'css:button[type="submit"]',
        ]
        clicked = False
        for sel in subscribe_selectors:
            try:
                btn = self.page.ele(sel, timeout=2)
                if not btn:
                    continue
                btn.click()
                log.info(f"  click via {sel}")
                clicked = True
                break
            except Exception as e:
                log.debug(f"  {sel} click err: {e}")
        if not clicked:
            r = self._eval("__gpt_custom_clickSubscribe && __gpt_custom_clickSubscribe()") or {}
            log.info(f"  __gpt_custom_clickSubscribe: {r}")
            clicked = bool(r and r.get("clicked"))

        # 等跳转 paypal.com / success（最多 wait_redirect_s 秒，一半时间没跳就重点一次）
        half = max(15, wait_redirect_s // 2)
        for attempt in range(2):
            for _ in range(half):
                u = self.page.url or ""
                if "paypal.com" in u:
                    log.info(f"  ✓ 跳到 paypal.com: {u[:90]}")
                    return {"status": "submitted", "url": u}
                if "/payments/success" in u or u.endswith("/success") or "/thanks" in u:
                    log.info(f"  ✓ 跳到 success: {u[:90]}")
                    return {"status": "submitted", "url": u}
                time.sleep(1)
            if attempt == 0:
                log.warning(f"  {half}s 未跳走，再点一次")
                for sel in subscribe_selectors:
                    try:
                        btn = self.page.ele(sel, timeout=1)
                        if btn:
                            btn.click()
                            break
                    except Exception:
                        pass

        log.error(f"  {wait_redirect_s}s 内未跳走, url={self.page.url[:120]}")
        return {"status": "submit_no_redirect", "url": self.page.url}

    # ============ PayPal 端 ============

    # ============ PayPal 端 - 参考 hanyi0000/chatgpt-plus-automation-toolkit fill_paypal ============

    def _pp_fill_first_visible(self, selectors: list[str], value: str, label: str = "") -> bool:
        """对一组 selector 顺序尝试 fill；命中第一个可见的就返回 True。
        参考 paypal_pay.py 的 page.locator(sel).first.fill(value) 模式。"""
        for sel in selectors:
            try:
                el = self.page.ele(f"css:{sel}", timeout=2)
                if not el:
                    continue
                try:
                    el.input(value, clear=True)
                    if label:
                        log.debug(f"  pp_fill [{label}] via {sel[:60]}")
                    return True
                except Exception as e:
                    log.debug(f"  pp_fill input err {sel}: {e}")
                    # 回退: JS setter
                    self._eval(
                        f"(function(){{var e=document.querySelector({json.dumps(sel)});"
                        f"if(!e)return;var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                        f"s.call(e,{json.dumps(value)});"
                        f"e.dispatchEvent(new Event('input',{{bubbles:true}}));"
                        f"e.dispatchEvent(new Event('change',{{bubbles:true}}));}})()"
                    )
                    return True
            except Exception as e:
                log.debug(f"  pp_fill ele err {sel}: {e}")
        return False

    def _pp_click_first_visible(self, selectors: list[str], label: str = "") -> bool:
        for sel in selectors:
            try:
                el = self.page.ele(f"css:{sel}", timeout=2)
                if not el:
                    continue
                el.click()
                if label:
                    log.debug(f"  pp_click [{label}] via {sel[:60]}")
                return True
            except Exception as e:
                log.debug(f"  pp_click err {sel}: {e}")
        return False

    def _select_paypal_tax_residency(self, country_code: str = "GB",
                                     country_name: str = "United Kingdom") -> str:
        """Select PayPal's native or custom tax-residency country control."""
        country_code = str(country_code or "GB").upper()
        country_name = str(country_name or "United Kingdom")
        last_result = "not_found"
        for _ in range(3):
            opened = self._eval(
                "(function(code,name){"
                "function vis(e){return e&&e.offsetParent!==null&&!e.disabled;}"
                "function norm(s){return String(s||'').trim().toLowerCase();}"
                "function ctx(e){var out=[];if(e.id){var ls=Array.from(document.querySelectorAll('label'));"
                "var l=ls.find(function(x){return x.htmlFor===e.id;});if(l)out.push(l.innerText||l.textContent||'');}"
                "var n=e;for(var i=0;n&&i<4;i++,n=n.parentElement){"
                "var txt=String(n.innerText||'');out.push(n.id||'',n.getAttribute&&n.getAttribute('name')||'',"
                "n.getAttribute&&n.getAttribute('data-testid')||'',"
                "n.getAttribute&&n.getAttribute('aria-label')||'',txt.length<300?txt:'');}"
                "return norm(out.join(' '));}"
                "function isTax(e){var t=ctx(e);return /tax.{0,20}residen|residen.{0,20}tax|country.{0,20}tax|crs/.test(t);}"
                "var sels=Array.from(document.querySelectorAll('select'));"
                "for(var i=0;i<sels.length;i++){var s=sels[i];if(!vis(s)||!isTax(s))continue;"
                "var opts=Array.from(s.options||[]),m=null;"
                "for(var j=0;j<opts.length;j++){var o=opts[j],v=String(o.value||'').toUpperCase(),t=norm(o.text);"
                "if(v===code||v==='UK'||t===norm(name)||t.indexOf(norm(name))>=0){m=o;break;}}"
                "if(!m)continue;var set=Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype,'value').set;"
                "set.call(s,m.value);m.selected=true;s.dispatchEvent(new Event('input',{bubbles:true}));"
                "s.dispatchEvent(new Event('change',{bubbles:true}));s.dispatchEvent(new Event('blur',{bubbles:true}));"
                "return 'selected_native:'+String(m.text||m.value);"
                "}"
                "var controls=Array.from(document.querySelectorAll('[role=\"combobox\"],button,[aria-haspopup=\"listbox\"],[data-testid*=\"tax\" i]'));"
                "for(var k=0;k<controls.length;k++){var c=controls[k];if(!vis(c)||!isTax(c))continue;"
                "try{c.scrollIntoView({block:'center'});}catch(e){}try{c.click();return 'opened_custom';}catch(e){}}"
                "return 'not_found';"
                "})(" + json.dumps(country_code) + "," + json.dumps(country_name) + ")"
            ) or "not_found"
            last_result = str(opened)
            if last_result.startswith("selected_native:"):
                log.info(f"  tax residency: {last_result}")
                return last_result
            if last_result == "not_found":
                time.sleep(0.35)
                continue

            time.sleep(0.5)
            selected = self._eval(
                "(function(code,name){"
                "function vis(e){return e&&e.offsetParent!==null&&!e.disabled;}"
                "function norm(s){return String(s||'').trim().toLowerCase();}"
                "var nodes=Array.from(document.querySelectorAll('[role=\"option\"],[role=\"listbox\"] li,[role=\"menu\"] li,[data-value]'));"
                "var target=null;for(var i=0;i<nodes.length;i++){var e=nodes[i];if(!vis(e))continue;"
                "var t=norm(e.innerText||e.textContent),v=String(e.getAttribute('data-value')||e.getAttribute('value')||'').toUpperCase();"
                "if(v===code||v==='UK'||t===norm(name)||t.indexOf(norm(name))===0){target=e;break;}}"
                "if(!target)return 'option_not_found';try{target.scrollIntoView({block:'nearest'});}catch(e){}"
                "try{target.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));"
                "target.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));target.click();"
                "return 'selected_custom:'+String(target.innerText||target.textContent||target.getAttribute('data-value')||'');}"
                "catch(e){return 'option_click_failed:'+e.message;}"
                "})(" + json.dumps(country_code) + "," + json.dumps(country_name) + ")"
            ) or "option_not_found"
            last_result = str(selected)
            if last_result.startswith("selected_custom:"):
                log.info(f"  tax residency: {last_result}")
                time.sleep(0.35)
                return last_result
            time.sleep(0.35)

        tax_field_present = bool(self._eval(
            "(function(){var t=document.body?document.body.innerText:'';"
            "return /country\\s+of\\s+tax\\s+residency|tax\\s+residency/i.test(t);})()"
        ))
        if not tax_field_present:
            return "not_present"
        log.warning(f"  tax residency 未选择: {last_result}")
        return "selection_failed:" + last_result

    def _fill_paypal_guest_form(self, card: dict, addr: dict, paypal_email: str,
                                 paypal_password: str) -> dict:
        """填 paypal.com/checkoutweb/signup 这个 guest 卡填写页。
        所有字段都是 #id 精准定位 + 真键盘清空+输入。

        注意：guest 卡填写页用的是卡所在国家（US 卡 → US 地址）。
        即便上一步 hosted checkout 选了 JP，落到这里也要切回 US 才能匹配
        '#billingState' 的 50 州 select。
        """
        log.info("[paypal-guest] 填写卡 + 地址表单")

        # PayPal guest 表单的 country：
        #   - 优先用 config.CHECKOUT_COUNTRY / BILLING_COUNTRY（JP 模式就要 JP）
        #   - 没配就回退到 addr['country']（一般是 US 卡的 US 地址）
        try:
            import config as _cfg
            _explicit_country = (
                card.get("paypal_country")
                or card.get("billing_country")
                or card.get("checkout_country")
                or ""
            )
            _bc = (str(_explicit_country) or getattr(_cfg, "BILLING_COUNTRY", "") or "").strip().upper()
            if not _bc or _bc == "AUTO":
                _bc = (str(card.get("checkout_country") or "") or getattr(_cfg, "CHECKOUT_COUNTRY", "") or "").strip().upper()
            guest_country = _bc or (addr.get("country") or "US").upper()
        except Exception:
            guest_country = (addr.get("country") or "US").upper()
        log.info(f"[paypal-guest] 期望 country={guest_country} (国家化 billing/phone 配方)")
        # ★★★ 切 PayPal 表单的 billing country
        # IP=US 时 PayPal 默认会把 #billingCountry 选成 US。我们必须强切到 JP，
        # 切了之后 phone 字段左侧前缀会自动变 +81，再填 JP 本土 10 位数字（去掉 +81 / 0 前导）
        try:
            cur = self._eval(
                "(document.getElementById('billingCountry')||document.querySelector('select[name=\"country\"]')||{}).value"
            )
            log.info(f"  [paypal-guest] 当前 country select = {cur!r}")
            if cur and cur != guest_country:
                log.info(f"  guest country switch: {cur} → {guest_country}")
                self._safe_select("#billingCountry", guest_country)
                self._safe_select("select[name=\"country\"]", guest_country)
                # ★ 多重 selector fallback（mobile）
                self._eval(
                    "(function(target){"
                    "  var sels=['#billingCountry','select[name=\"country\"]',"
                    "    'select[id*=\"country\" i]','select[autocomplete=\"country\"]',"
                    "    'select[data-testid*=\"country\" i]'];"
                    "  for(var i=0;i<sels.length;i++){"
                    "    var s=document.querySelector(sels[i]);"
                    "    if(!s)continue;"
                    "    s.value=target;"
                    "    s.dispatchEvent(new Event('change',{bubbles:true}));"
                    "  }"
                    "})(" + json.dumps(guest_country) + ")"
                )
                time.sleep(2.0)
                # 校验
                cur2 = self._eval(
                    "(document.getElementById('billingCountry')||document.querySelector('select[name=\"country\"]')||{}).value"
                )
                log.info(f"  [paypal-guest] country switch 后 = {cur2!r}")
            elif cur == guest_country:
                log.info(f"  [paypal-guest] country 已经是 {guest_country}，跳过切换")
        except Exception as _e:
            log.debug(f"  guest country switch err: {_e}")

        # ★★★ phone country code dropdown（如果存在）
        # PayPal 手机版表单可能有独立的 phone country dropdown（#phoneType /
        # 类似 country code select），需要单独切到目标国家。
        phone_country_rules = {
            "JP": {"dial": "81", "name_re": "JAPAN|日本"},
            "BR": {"dial": "55", "name_re": "BRAZIL|BRASIL|巴西"},
            "GB": {"dial": "44", "name_re": "UNITED KINGDOM|GREAT BRITAIN|BRITAIN|UK|英国"},
        }
        if guest_country in phone_country_rules:
            try:
                _rule = phone_country_rules[guest_country]
                phone_cc_set = self._eval(
                    "(function(country, dial, nameRe){"
                    "  var sels=['#phoneType','select[name=\"phoneType\"]',"
                    "    'select[id*=\"phone-country\" i]','select[id*=\"phoneCountry\" i]',"
                    "    'select[name*=\"phoneCountry\" i]',"
                    "    'select[data-testid*=\"phone-country\" i]'];"
                    "  var re = new RegExp(nameRe, 'i');"
                    "  for(var i=0;i<sels.length;i++){"
                    "    var s=document.querySelector(sels[i]);"
                    "    if(!s)continue;"
                    "    var opts=Array.from(s.options||[]);"
                    "    for(var j=0;j<opts.length;j++){"
                    "      var o=opts[j];"
                    "      var v=String(o.value||'').toUpperCase();"
                    "      var t=String(o.text||'').toUpperCase();"
                    "      if(v===country||v==='+'+dial||v===dial||re.test(t)||t.indexOf('+'+dial)>=0){"
                    "        s.value=o.value;"
                    "        s.dispatchEvent(new Event('change',{bubbles:true}));"
                    "        return 'set:'+sels[i]+'='+o.value+' ('+(o.text||'').slice(0,30)+')';"
                    "      }"
                    "    }"
                    "  }"
                    "  return 'no_phone_country_select';"
                    "})(" + json.dumps(guest_country) + "," + json.dumps(_rule["dial"]) + "," + json.dumps(_rule["name_re"]) + ")"
                )
                log.info(f"  [paypal-guest] phone country: {phone_cc_set}")
            except Exception as _e:
                log.debug(f"  phone country select err: {_e}")

        first_name = card.get("first_name") or "James"
        last_name = card.get("last_name") or "Smith"
        gb_identity = card.get("_gb_identity") if isinstance(card.get("_gb_identity"), dict) else None
        if guest_country == "GB":
            try:
                if not gb_identity:
                    from gb_identity import generate_gb_identity_for_paypal
                    gb_identity = generate_gb_identity_for_paypal()
                first_name = gb_identity["first_name"]
                last_name = gb_identity["last_name"]
                addr = dict(gb_identity["address"])
                log.info(
                    f"  [GB] 资料: {first_name} {last_name} | "
                    f"DOB={gb_identity['date_of_birth_dmy']} | "
                    f"{addr.get('street')}, {addr.get('city')}, "
                    f"{addr.get('state')} {addr.get('zip')}"
                )
            except Exception as _e:
                log.warning(f"  gb_identity 资料抽取失败，沿用卡姓名/账单地址: {_e}")
        br_identity = None
        br_street_number = ""
        br_district = ""
        if guest_country == "BR":
            try:
                from br_identity import generate_br_identity_for_paypal
                br_identity = generate_br_identity_for_paypal()
                first_name = br_identity["first_name"]
                last_name = br_identity["last_name"]
                if (addr or {}).get("country", "").upper() != "BR":
                    addr = br_identity["address"]
                addr = dict(addr or {})

                def _infer_br_district(city: str, street_line: str) -> str:
                    city_l = (city or "").lower()
                    street_l = (street_line or "").lower()
                    if "paulista" in street_l or "augusta" in street_l:
                        return "Bela Vista"
                    if "oscar freire" in street_l:
                        return "Jardins"
                    if "atlântica" in street_l or "atlantica" in street_l:
                        return "Copacabana"
                    if "pirajá" in street_l or "piraja" in street_l:
                        return "Ipanema"
                    if "boa viagem" in street_l:
                        return "Boa Viagem"
                    if "beira mar" in street_l:
                        return "Meireles"
                    if "tancredo neves" in street_l:
                        return "Caminho das Árvores"
                    if "brasília" in city_l or "brasilia" in city_l:
                        return "Asa Sul"
                    if city_l:
                        return "Centro"
                    return "Centro"

                street_line = str(addr.get("street") or br_identity.get("billing_line1") or "").strip()
                number_match = re.search(r"(?:,\s*|\s+)(\d{1,5}[A-Za-z]?)\b", street_line)
                if number_match:
                    br_street_number = number_match.group(1)
                    addr["street"] = (
                        street_line[:number_match.start()].rstrip(" ,")
                        + street_line[number_match.end():].strip()
                    ).strip(" ,") or street_line
                else:
                    br_street_number = str(random.randint(10, 2999))
                    addr["street"] = street_line
                addr["number"] = addr.get("number") or br_street_number
                br_district = (
                    addr.get("district")
                    or addr.get("neighborhood")
                    or _infer_br_district(addr.get("city", ""), street_line)
                )
                addr["district"] = br_district
                log.info(
                    f"  [BR] 资料: {br_identity['first_name']} {br_identity['last_name']} | "
                    f"DOB={br_identity['date_of_birth']} | CPF={br_identity['cpf']} | "
                    f"{addr.get('street')} Nº {addr.get('number')} {br_district} "
                    f"{br_identity['billing_city']}/"
                    f"{br_identity['billing_state']} {br_identity['billing_postal_code']}"
                )
            except Exception as _e:
                log.debug(f"  br_identity 资料抽取失败: {_e}，沿用卡姓名/账单地址")
        card_number = (card.get("cardNumber") or card.get("number") or "").replace(" ", "")
        card_expiry = card.get("cardExpiry") or card.get("expiry") or ""
        card_cvv = card.get("cardCvv") or card.get("cvv") or ""

        phone_raw = (card.get("phone") or "").strip()
        # 多国本土格式：US 去 +1，JP 去 +81 后补 0，其它去 +。
        # 用 phone_pool.to_local_phone 统一处理（fallback 到原 US 逻辑）。
        try:
            from phone_pool import to_local_phone as _to_local
            phone_local = _to_local(phone_raw, country=guest_country)
        except ImportError:
            phone_local = phone_raw.lstrip("+1") if phone_raw.startswith("+1") else phone_raw.lstrip("+")
            phone_local = "".join(ch for ch in phone_local if ch.isdigit())
            if phone_local.startswith("1") and len(phone_local) == 11:
                phone_local = phone_local[1:]

        # 真键盘填字段：click → Cmd/Ctrl+A → Backspace → type
        # 这个逻辑等价于 Playwright `page.locator(sel).fill(value)` 的内部实现
        # ★★★ Mobile-friendly fallback selectors：
        # 桌面 PayPal 表单字段都有固定 id（#cardNumber 等），手机版用 React 动态 id +
        # data-testid / autocomplete / aria-label / inputmode 标识。下面这个表把每个
        # 桌面字段名映射到 mobile 友好的 selector 列表（按优先级），keyboard_fill 找不到
        # #id 时按顺序兜底。
        _FIELD_FALLBACK = {
            "email": [
                "input[autocomplete='email']",
                "input[type='email']",
                "input[name*='email' i]",
                "input[id*='email' i]",
                "input[data-testid*='email' i]",
                "input[aria-label*='email' i]",
                "input[aria-label*='メール']",
            ],
            "phone": [
                "input[autocomplete='tel']",
                "input[autocomplete='tel-national']",
                "input[name*='phone' i]",
                "input[id*='phone' i]",
                "input[type='tel'][inputmode='tel']",
                "input[data-testid*='phone' i]",
                "input[aria-label*='phone' i]",
                "input[aria-label*='電話']",
            ],
            "cardNumber": [
                "input[autocomplete='cc-number']",
                "input[name*='cardNumber' i]",
                "input[id*='cardNumber' i]",
                "input[data-testid*='cardNumber' i]",
                "input[data-testid='card-number-input']",
                "input[aria-label*='card number' i]",
                "input[aria-label*='カード番号']",
                "input[placeholder*='カード番号']",
                "input[type='tel'][inputmode='numeric'][maxlength='19']",
            ],
            "cardExpiry": [
                "input[autocomplete='cc-exp']",
                "input[name*='cardExpiry' i]",
                "input[id*='cardExpiry' i]",
                "input[data-testid*='expir' i]",
                "input[data-testid*='exp-date' i]",
                "input[aria-label*='expir' i]",
                "input[aria-label*='有効期限']",
                "input[placeholder*='MM/YY' i]",
                "input[placeholder*='月/年']",
            ],
            "cardCvv": [
                "input[autocomplete='cc-csc']",
                "input[name*='cardCvv' i]",
                "input[name*='cvv' i]",
                "input[name*='cvc' i]",
                "input[id*='cardCvv' i]",
                "input[id*='cvv' i]",
                "input[id*='cvc' i]",
                "input[data-testid*='cvv' i]",
                "input[data-testid*='csc' i]",
                "input[data-testid*='cvc' i]",
                "input[aria-label*='security code' i]",
                "input[aria-label*='CVV' i]",
                "input[aria-label*='セキュリティコード']",
            ],
            "billingLine1": [
                "input[autocomplete='address-line1']",
                "input[autocomplete='street-address']",
                "input[name*='billingLine1' i]",
                "input[name*='address-line1' i]",
                "input[name*='street' i]",
                "input[id*='billingLine1' i]",
                "input[id*='billingAddressLine1' i]",
                "input[data-testid*='address-line1' i]",
                "input[aria-label*='address line 1' i]",
                "input[aria-label*='番地']",
                "input[aria-label*='町名']",
            ],
            "billingLine2": [
                "input[autocomplete='address-line2']",
                "input[name*='billingLine2' i]",
                "input[name*='address-line2' i]",
                "input[id*='billingLine2' i]",
                "input[id*='billingAddressLine2' i]",
                "input[data-testid*='address-line2' i]",
                "input[aria-label*='address line 2' i]",
                "input[aria-label*='建物']",
            ],
            "billingNumber": [
                "input[name='billingAddressNumber']",
                "input[id='billingAddressNumber']",
                "input[name*='addressNumber' i]",
                "input[id*='addressNumber' i]",
                "input[name*='billingNumber' i]",
                "input[id*='billingNumber' i]",
                "input[name*='streetNumber' i]",
                "input[id*='streetNumber' i]",
                "input[aria-label='Nº']",
                "input[placeholder='Nº']",
                "input[aria-label*='número' i]",
                "input[placeholder*='número' i]",
                "input[aria-label*='numero' i]",
                "input[placeholder*='numero' i]",
            ],
            "billingNeighborhood": [
                "input[name*='neighborhood' i]",
                "input[id*='neighborhood' i]",
                "input[name*='district' i]",
                "input[id*='district' i]",
                "input[name*='bairro' i]",
                "input[id*='bairro' i]",
                "input[aria-label*='bairro' i]",
                "input[placeholder*='bairro' i]",
                "input[aria-label*='distrito' i]",
                "input[placeholder*='distrito' i]",
            ],
            "billingCity": [
                "input[autocomplete='address-level2']",
                "input[name*='billingCity' i]",
                "input[name*='city' i]",
                "input[id*='billingCity' i]",
                "input[id*='city' i]",
                "input[data-testid*='city' i]",
                "input[aria-label*='city' i]",
                "input[aria-label*='市区町村']",
            ],
            "billingPostalCode": [
                "input[autocomplete='postal-code']",
                "input[name*='billingPostalCode' i]",
                "input[name*='postal' i]",
                "input[name*='zip' i]",
                "input[id*='billingPostalCode' i]",
                "input[id*='postalCode' i]",
                "input[id*='zip' i]",
                "input[data-testid*='postal' i]",
                "input[data-testid*='zip' i]",
                "input[aria-label*='postal' i]",
                "input[aria-label*='zip' i]",
                "input[aria-label*='郵便番号']",
                "input[inputmode='numeric'][maxlength='8']",
            ],
            "firstName": [
                "input[autocomplete='given-name']",
                "input[name*='firstName' i]",
                "input[id*='firstName' i]",
                "input[id='firstName']",
                "input[data-testid*='first-name' i]",
                "input[aria-label*='first name' i]",
                "input[aria-label='名']",
            ],
            "lastName": [
                "input[autocomplete='family-name']",
                "input[name*='lastName' i]",
                "input[id*='lastName' i]",
                "input[id='lastName']",
                "input[data-testid*='last-name' i]",
                "input[aria-label*='last name' i]",
                "input[aria-label='姓']",
            ],
            "countrySpecificFirstName": [
                "input[name*='countrySpecificFirstName' i]",
                "input[id='countrySpecificFirstName']",
                "input[data-testid*='kana-first' i]",
                "input[aria-label*='メイ']",
                "input[aria-label*='名（カナ）']",
            ],
            "countrySpecificLastName": [
                "input[name*='countrySpecificLastName' i]",
                "input[id='countrySpecificLastName']",
                "input[data-testid*='kana-last' i]",
                "input[aria-label*='セイ']",
                "input[aria-label*='姓（カナ）']",
            ],
            "dateOfBirth": [
                "input[autocomplete='bday']",
                "input[name*='dateOfBirth' i]",
                "input[id='dateOfBirth']",
                "input[id*='dateOfBirth' i]",
                "input[data-testid*='date-of-birth' i]",
                "input[data-testid*='dob' i]",
                "input[aria-label*='date of birth' i]",
                "input[aria-label*='生年月日']",
                "input[placeholder*='DD/MM/YYYY' i]",
                "input[placeholder*='DD/MM/AAAA' i]",
                "input[placeholder*='YYYY/MM/DD' i]",
            ],
            "taxId": [
                "input[name*='tax' i]",
                "input[id*='tax' i]",
                "input[name*='cpf' i]",
                "input[id*='cpf' i]",
                "input[name*='document' i]",
                "input[id*='document' i]",
                "input[name*='national' i]",
                "input[id*='national' i]",
                "input[data-testid*='cpf' i]",
                "input[data-testid*='tax' i]",
                "input[aria-label*='CPF' i]",
                "input[placeholder*='CPF' i]",
            ],
            "password": [
                "input[type='password']",
                "input[autocomplete='new-password']",
                "input[name='password']",
                "input[id='password']",
                "input[data-testid*='password' i]",
                "input[aria-label*='password' i]",
                "input[aria-label*='パスワード']",
            ],
        }

        def _resolve_field_id(field_name: str, enable_fuzzy: bool = False) -> str:
            """根据字段名找元素，返回它真实的 dom id（如果有）。

            优先 #fieldName 命中（桌面），找不到走 fallback selector 列表（手机版）。
            如果命中元素没有 id，给它注入一个临时 id（``__pp_<field>``）便于后续 keyboard_fill
            的 document.getElementById 操作。
            返回真实/注入后的 id；找不到任何元素返回空串。
            """
            sels = [f"#{field_name}"] + _FIELD_FALLBACK.get(field_name, [])
            sels_json = json.dumps(sels)
            tmp_id = f"__pp_{field_name}"
            real_id = self._eval(
                "(function(sels, tmp, field){"
                "  function vis(el){return el && el.offsetParent !== null && !el.disabled && !el.readOnly && el.type !== 'hidden';}"
                "  function clean(s){return String(s||'').toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g,'');}"
                "  function labelText(el){"
                "    var out=[];"
                "    if(el.id){var lab=document.querySelector('label[for="'+CSS.escape(el.id)+'"]');if(lab)out.push(lab.textContent||'');}"
                "    var wrap=el.closest('label');if(wrap)out.push(wrap.textContent||'');"
                "    var p=el.closest('label,.field,.form-group,.input-group,div');"
                "    if(p){var q=p.querySelector('label,.label,span');if(q)out.push(q.textContent||'');}"
                "    return out.join(' ');"
                "  }"
                "  function textOf(el){return clean([el.id,el.name,el.autocomplete,el.placeholder,el.getAttribute('aria-label'),el.getAttribute('data-testid'),el.getAttribute('data-field'),labelText(el)].join(' '));}"
                "  function match(field, t){"
                "    var negCard=/card|cartao|cart[aã]o|cc-number|cvv|cvc/;"
                "    if(field==='email')return /\\b(e-?mail|email)\\b/.test(t);"
                "    if(field==='phone')return /\\b(phone|mobile|cell|tel|telefone|celular)\\b/.test(t)&&!/cpf|document|doc|card|cartao|cart[aã]o/.test(t);"
                "    if(field==='firstName')return /(first.?name|given.?name|primeiro nome)/.test(t)&&!/full|completo|last|family|sobrenome/.test(t);"
                "    if(field==='lastName')return /(last.?name|family.?name|surname|sobrenome)/.test(t);"
                "    if(field==='cardNumber')return /(card.?number|number.?card|cartao.*numero|numero.*cartao|cc.?number|credit.?card|pan)/.test(t);"
                "    if(field==='cardExpiry')return /(expir|expiry|expiration|validade|vencimento|mm.*yy|mes.*ano)/.test(t)&&!/cvv|cvc|security|seguranca/.test(t);"
                "    if(field==='cardCvv')return /(cvv|cvc|csc|security.*code|codigo.*seguranca|cod.*seg)/.test(t);"
                "    if(field==='billingLine1')return /(address.?line.?1|street|endereco|logradouro|rua)/.test(t)&&!/city|cidade|state|estado|cep|postal|zip|numero|number/.test(t);"
                "    if(field==='billingNumber')return /(numero|numero|nº|n°|house.*number|address.*number|billing.*number)/.test(t)&&!/phone|telefone|cpf|document|doc/.test(t)&&!negCard.test(t);"
                "    if(field==='billingNeighborhood')return /(bairro|district|distrito|neighbou?rhood)/.test(t);"
                "    if(field==='billingCity')return /(city|cidade|municipio)/.test(t);"
                "    if(field==='billingPostalCode')return /(cep|postal.?code|codigo.*postal|zip|postal)/.test(t);"
                "    if(field==='dateOfBirth')return /(date.*birth|birth.*date|birthday|dob|bday|nascimento|data.*nascimento)/.test(t);"
                "    if(field==='taxId')return /(cpf|documento|document|tax.?id|taxid|national|identity)/.test(t);"
                "    return false;"
                "  }"
                "  for (var i = 0; i < sels.length; i++) {"
                "    var el = document.querySelector(sels[i]);"
                "    if (vis(el)) {"
                "      var attrs = [el.id, el.name, el.autocomplete, el.placeholder, el.getAttribute('aria-label'), el.getAttribute('data-testid')].join(' ');"
                "      if (field==='billingNumber' && /card|cart[aã]o|cc-number/i.test(attrs)) continue;"
                "      if (!el.id) { el.id = tmp; }"
                "      return el.id;"
                "    }"
                "  }"
                "  if(!arguments[3])return '';"
                "  var fields=Array.from(document.querySelectorAll('input,textarea'));"
                "  for(var j=0;j<fields.length;j++){"
                "    var f=fields[j];if(!vis(f))continue;"
                "    if(f.value && String(f.value).trim() && field!=='password')continue;"
                "    var txt=textOf(f);if(!match(field, txt))continue;"
                "    if(!f.id)f.id=tmp;"
                "    return f.id;"
                "  }"
                "  return '';"
                "})(" + sels_json + ", " + json.dumps(tmp_id) + ", " + json.dumps(field_name) + ", " + json.dumps(bool(enable_fuzzy)) + ")"
            )
            return str(real_id or "")

        def keyboard_fill(css_id: str, value: str, label: str = "") -> bool:
            if not value:
                log.debug(f"  [{label or css_id}] 空值跳过")
                return False
            try:
                # ★ Mobile-friendly：先按 #css_id 找；找不到走 fallback selector 列表
                resolved_id = _resolve_field_id(css_id, enable_fuzzy=(guest_country == "BR"))
                if not resolved_id:
                    log.warning(f"  [{label or css_id}] 元素不存在（mobile fallback 也没命中）")
                    return False
                if resolved_id != css_id:
                    log.info(f"  [{label or css_id}] mobile selector 命中 → 临时 id={resolved_id}")
                # 后续操作都用 resolved_id 而不是 css_id
                fid = resolved_id
                el = self.page.ele(f"css:#{fid}", timeout=2)
                if not el:
                    log.warning(f"  [{label or css_id}] 元素查询返回空（resolved_id={fid}）")
                    return False

                # Step 1: focus 元素
                try:
                    el.focus()
                except Exception:
                    try:
                        el.click()
                    except Exception:
                        pass
                time.sleep(0.15)

                # Step 2: 用 JS 直接清空 value（避免 React state 干扰）
                self._eval(
                    f"(function(){{var e=document.getElementById({json.dumps(fid)});"
                    f"if(!e)return;var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                    f"s.call(e,'');"
                    f"e.dispatchEvent(new Event('input',{{bubbles:true}}));}})()"
                )
                time.sleep(0.1)

                # Step 3: 真键盘 select-all + delete（防止 React revert）
                # macOS 用 Meta（Cmd），Windows/Linux 用 Ctrl
                import platform
                modifier = "META" if platform.system() == "Darwin" else "CTRL"
                try:
                    self.page.actions.key_down(modifier).key_down("a").key_up("a").key_up(modifier)
                    time.sleep(0.1)
                    self.page.actions.key_down("DELETE").key_up("DELETE")
                except Exception:
                    pass
                time.sleep(0.1)

                # Step 4: 真键盘逐字符输入（CDP Input.dispatchKeyEvent，跟人手输入一样）
                try:
                    self.page.actions.type(value, interval=0.02)
                except Exception as e:
                    log.warning(f"  [{label or css_id}] actions.type 失败: {e}")
                    # JS 兜底
                    self._eval(
                        f"(function(){{var e=document.getElementById({json.dumps(fid)});"
                        f"if(!e)return;var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                        f"s.call(e,{json.dumps(value)});"
                        f"e.dispatchEvent(new Event('input',{{bubbles:true}}));"
                        f"e.dispatchEvent(new Event('change',{{bubbles:true}}));}})()"
                    )

                # Step 5: 触发 blur 让 React 走完校验
                try:
                    self.page.actions.key_down("TAB").key_up("TAB")
                except Exception:
                    pass
                time.sleep(0.15)

                # 回读校验
                actual = self._eval(
                    f"(document.getElementById({json.dumps(fid)}) || {{}}).value || ''"
                )
                if actual == value:
                    log.info(f"  ✓ [{label or css_id}] = {value[:30]}")
                    return True
                # 数字类字段去非数字后比较（PayPal 会自动加分隔符）
                def _norm_quick(s): return "".join(c for c in s if c.isdigit())
                if _norm_quick(actual) == _norm_quick(value) and _norm_quick(value):
                    log.info(f"  ✓ [{label or css_id}] = {value[:30]} (格式化后匹配)")
                    return True

                # 真键盘失败（焦点跑了 / React 拦了），用 JS React-friendly 兜底
                self._eval(
                    f"(function(){{var e=document.getElementById({json.dumps(fid)});"
                    f"if(!e)return;e.focus();"
                    f"var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                    f"s.call(e,{json.dumps(value)});"
                    f"e.dispatchEvent(new Event('input',{{bubbles:true}}));"
                    f"e.dispatchEvent(new Event('change',{{bubbles:true}}));"
                    f"e.dispatchEvent(new Event('blur',{{bubbles:true}}));}})()"
                )
                time.sleep(0.2)
                actual2 = self._eval(
                    f"(document.getElementById({json.dumps(fid)}) || {{}}).value || ''"
                )
                if actual2 == value:
                    log.info(f"  ✓ [{label or css_id}] = {value[:30]} (JS 兜底)")
                    return True
                # 数字类字段（phone/cardNumber/cardExpiry）PayPal 会自动加分隔符，
                # 去掉非数字/斜杠后比较，避免误报 ⚠
                def _norm(s): return "".join(c for c in s if c.isdigit())
                if _norm(actual2) == _norm(value) and _norm(value):
                    log.info(f"  ✓ [{label or css_id}] = {value[:30]} (格式化后匹配)")
                    return True
                log.warning(f"  ⚠ [{label or css_id}] 期望={value[:20]} 实际={actual2[:30]}")
                return False
            except Exception as e:
                log.warning(f"  [{label or css_id}] 填写异常: {e}")
                return False

        def ensure_credit_card_selected() -> str:
            try:
                return str(self._eval(
                    "(function(){"
                    "  function vis(el){return el && el.offsetParent!==null;}"
                    "  function clickIt(el){try{el.scrollIntoView({block:'center'});}catch(e){}"
                    "    try{el.click();return true;}catch(e){return false;}}"
                    "  var rx=/\\bcredit\\b|cr[eé]dito/i;"
                    "  var radios=Array.from(document.querySelectorAll('input[type=\"radio\"], [role=\"radio\"]'));"
                    "  for(var i=0;i<radios.length;i++){"
                    "    var r=radios[i];if(!vis(r))continue;"
                    "    var ctx=(r.closest('label,[role=\"group\"],fieldset,div')||r).innerText||'';"
                    "    var attrs=[r.value,r.id,r.name,r.getAttribute('aria-label')].join(' ');"
                    "    if(rx.test(ctx)||rx.test(attrs)){"
                    "      if(r.checked||r.getAttribute('aria-checked')==='true')return 'already_credit';"
                    "      return clickIt(r)||clickIt(r.closest('label')) ? 'clicked_credit' : 'credit_click_failed';"
                    "    }"
                    "  }"
                    "  var labels=Array.from(document.querySelectorAll('label,button,[role=\"button\"],span,div'));"
                    "  for(var j=0;j<labels.length;j++){var el=labels[j];"
                    "    if(!vis(el))continue;var t=(el.innerText||el.textContent||'').trim();"
                    "    if(t.length<40 && rx.test(t))return clickIt(el)?'clicked_credit_label':'credit_label_click_failed';"
                    "  }"
                    "  return 'credit_not_found';"
                    "})()"
                ) or "")
            except Exception as e:
                return f"credit_err:{e}"

        def accept_required_terms() -> str:
            try:
                return str(self._eval(
                    "(function(){"
                    "  function vis(el){return el && el.offsetParent!==null;}"
                    "  function clickIt(el){try{el.scrollIntoView({block:'center'});}catch(e){}"
                    "    try{el.click();return true;}catch(e){return false;}}"
                    "  var boxes=Array.from(document.querySelectorAll('input[type=\"checkbox\"], [role=\"checkbox\"]'));"
                    "  var required=/contrato|usu[aá]rio|declara[cç][aã]o|privacidade|maior de idade|user agreement|privacy/i;"
                    "  var promo=/promo[cç][oõ]es|ofertas|marketing|promotions|offers/i;"
                    "  var fallback=null;"
                    "  for(var i=0;i<boxes.length;i++){"
                    "    var b=boxes[i];if(!vis(b))continue;"
                    "    var checked=b.checked||b.getAttribute('aria-checked')==='true';"
                    "    if(checked)continue;"
                    "    var scope=b.closest('label,div,li,section')||b;"
                    "    var text=(scope.innerText||scope.textContent||'');"
                    "    if(required.test(text))return clickIt(b)||clickIt(scope)?'clicked_terms':'terms_click_failed';"
                    "    if(!fallback && !promo.test(text))fallback={box:b,scope:scope};"
                    "  }"
                    "  if(fallback)return clickIt(fallback.box)||clickIt(fallback.scope)?'clicked_first_checkbox':'checkbox_click_failed';"
                    "  return 'terms_not_found';"
                    "})()"
                ) or "")
            except Exception as e:
                return f"terms_err:{e}"

        results = {}
        if guest_country == "BR":
            results["credit"] = ensure_credit_card_selected()
            log.info(f"  credit radio: {results['credit']}")
            time.sleep(0.3)
        results["email"] = keyboard_fill("email", paypal_email, "email")
        time.sleep(0.4)
        results["phone"] = keyboard_fill("phone", phone_local, "phone")
        time.sleep(0.4)
        results["cardNumber"] = keyboard_fill("cardNumber", card_number, "cardNumber")
        time.sleep(0.4)
        # PayPal 期望 MM/YY，输入 0230 (2030/2 → 02/30)
        exp_normalized = card_expiry.replace(" ", "").replace("/", "")
        if len(exp_normalized) == 4:
            exp_normalized = exp_normalized[:2] + "/" + exp_normalized[2:]
        results["cardExpiry"] = keyboard_fill("cardExpiry", exp_normalized, "cardExpiry")
        time.sleep(0.4)
        results["cardCvv"] = keyboard_fill("cardCvv", card_cvv, "cardCvv")
        time.sleep(0.4)
        # JP 模式跳过英文 firstName/lastName 填写，下面 JP 分支会用 React-friendly
        # 方式直接写日本姓名（漢字+片假名同源）。
        if guest_country != "JP":
            results["firstName"] = keyboard_fill("firstName", first_name, "firstName")
            time.sleep(0.3)
            results["lastName"] = keyboard_fill("lastName", last_name, "lastName")
            time.sleep(0.3)

        # JP guest 表单特有字段：漢字+片假名姓名 + 生年月日
        # 关键：PayPal JP guest 表单的真实字段 ID 是：
        #   #firstName / #lastName             → 漢字版（已被前面的 keyboard_fill 填了英文）
        #   #countrySpecificFirstName / Last   → 片假名版
        #   #dateOfBirth                       → 生年月日（YYYY/MM/DD）
        # 漢字版和片假名版必须**字面对应**（PayPal 风控会判匹配），所以 JP 模式下
        # 把英文姓名替换成日本姓名（漢字+片假名同源），从池里随机抽。
        if guest_country == "JP":
            # ★★ 资料来源升级到 jp_identity.generate_jp_identity_for_paypal()
            # 一次性拿到 漢字+片假名+生年月日+邮编+都道府県+市+街道+邮箱+密码（同一个池）
            # 失败时退回 _kana_helper.random_jp_name()（只姓名）
            jp_full = None
            try:
                from jp_identity import generate_jp_identity_for_paypal
                jp_full = generate_jp_identity_for_paypal(card_brand="JCB")
                jp = {
                    "first_kanji": jp_full["first_kanji"],
                    "first_kana": jp_full["first_kana"],
                    "last_kanji": jp_full["last_kanji"],
                    "last_kana": jp_full["last_kana"],
                }
                log.info(
                    f"  [JP] 资料: {jp_full['last_kanji']}{jp_full['first_kanji']} "
                    f"({jp_full['last_kana']} {jp_full['first_kana']}) | "
                    f"DOB={jp_full['date_of_birth']} | "
                    f"〒{jp_full['billing_postal_code']} {jp_full['billing_state']} "
                    f"{jp_full['billing_city']} {jp_full['billing_line1']} | "
                    f"卡 BIN {jp_full['card_bin']} ({jp_full['card_brand']})"
                )
            except Exception as _e:
                log.debug(f"  jp_identity 资料抽取失败: {_e} → 回退 random_jp_name")
                try:
                    from _kana_helper import random_jp_name
                    jp = random_jp_name()
                except Exception as _e2:
                    log.debug(f"  random_jp_name 失败: {_e2}")
                    jp = {"first_kanji": "翔太", "first_kana": "ショウタ",
                          "last_kanji": "佐藤", "last_kana": "サトウ"}
                log.info(f"  [JP] 抽到姓名: {jp['last_kanji']}{jp['first_kanji']} ({jp['last_kana']} {jp['first_kana']})")

            # 用 React-friendly 的 JS setter 重新写 firstName/lastName（覆盖前面填的英文），
            # 顺便填假名字段。技术参考 aBaiAutoplus _force_fill_input_by_id：
            #   1. el._valueTracker.setValue('') 清掉 React 内部 valueTracker
            #   2. prototype value setter 写值
            #   3. dispatch input/change/blur
            # ★ 这里 elem_id 既可以是桌面固定 id（'firstName' 等），也可以走 fallback
            #   selector 匹配（手机版动态 id），通过 _resolve_field_id 拿到真实/注入后的 id
            def _react_force_fill(elem_id, value, label):
                if not value:
                    return False
                # ★ Mobile-friendly：先 resolve（id 找不到走 fallback selectors）
                resolved = _resolve_field_id(elem_id)
                if not resolved:
                    log.warning(f"  ⚠ [{label}] 元素不存在（mobile fallback 也没命中）")
                    return False
                if resolved != elem_id:
                    log.info(f"  [{label}] mobile selector 命中 → 临时 id={resolved}")
                ok = self._eval(
                    "(function(id, val){"
                    "  var el = document.getElementById(id);"
                    "  if (!el) return 'no_element';"
                    "  if (el.disabled || el.getAttribute('aria-disabled') === 'true') return 'disabled';"
                    "  var proto = window.HTMLInputElement.prototype;"
                    "  var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
                    "  try { el.focus(); } catch(e) {}"
                    "  try { if (el._valueTracker) el._valueTracker.setValue(''); } catch(e) {}"
                    "  setter.call(el, val);"
                    "  try { el.setAttribute('value', val); } catch(e) {}"
                    "  el.dispatchEvent(new Event('input', { bubbles: true }));"
                    "  el.dispatchEvent(new Event('change', { bubbles: true }));"
                    "  el.dispatchEvent(new Event('blur', { bubbles: true }));"
                    "  return 'ok:' + String(el.value || '');"
                    "})(" + json.dumps(resolved) + "," + json.dumps(value) + ")"
                )
                ok_str = str(ok or "")
                if ok_str.startswith("ok:" + value):
                    log.info(f"  ✓ [{label}] = {value}")
                    return True
                log.warning(f"  ⚠ [{label}] 期望={value} 写入结果={ok_str[:60]}")
                return False
            # JP 段先存住目标值，等地址 + 滚动后再填（懒加载段需要滚动触发渲染）
            self._jp_payload = jp
            self._jp_full_identity = jp_full  # ★ 完整资料（dob/邮编/地址/邮箱/密码）
            self._jp_react_force_fill = _react_force_fill

        results["billingLine1"] = keyboard_fill("billingLine1", addr.get("street", ""), "street")
        time.sleep(0.3)
        if guest_country == "BR":
            results["billingNumber"] = keyboard_fill(
                "billingNumber", addr.get("number") or br_street_number, "Nº")
            time.sleep(0.3)
            results["billingNeighborhood"] = keyboard_fill(
                "billingNeighborhood", addr.get("district") or br_district, "Distrito/Bairro")
            time.sleep(0.3)
        results["billingCity"] = keyboard_fill("billingCity", addr.get("city", ""), "city")
        time.sleep(0.3)
        results["billingPostalCode"] = keyboard_fill("billingPostalCode", addr.get("zip", ""), "zip")
        time.sleep(0.3)
        # state/都道府県 select：按 country 走候选列表
        try:
            from address_provider import jp_prefecture_aliases as _jp_aliases
        except ImportError:
            _jp_aliases = lambda v: [v]

        state_raw = (addr.get("state") or "").strip()
        if guest_country == "JP":
            state_candidates = _jp_aliases(state_raw or "Tokyo")
        elif guest_country == "BR":
            try:
                from br_identity import br_state_aliases as _br_state_aliases
                state_candidates = _br_state_aliases(state_raw) or [state_raw.upper(), state_raw]
            except Exception:
                state_candidates = [state_raw.upper(), state_raw, addr.get("stateName", "")]
        else:
            state_value = state_raw.upper()
            state_candidates = [state_value, state_raw]

        state_selectors = ["#billingState"]
        if guest_country == "BR":
            state_selectors.extend([
                "select[name*='state' i]",
                "select[id*='state' i]",
                "select[name*='administrative' i]",
                "select[id*='administrative' i]",
                "select[autocomplete='address-level1']",
                "select[data-testid*='state' i]",
                "select[data-testid*='region' i]",
                "select[aria-label*='estado' i]",
            ])

        matched_state = False
        for sel in state_selectors:
            for cand in state_candidates:
                if cand and self._safe_select_region(sel, cand, country=guest_country):
                    matched_state = True
                    results["billingState"] = cand
                    break
            if matched_state:
                break
        if not matched_state:
            log.warning(f"  state/region 没匹配上任何 candidate: {state_candidates}")
            results["billingState"] = False
        log.info(f"  state: {results['billingState']} (country={guest_country})")
        time.sleep(0.3)

        # ============ JP 懒加载段：scrollIntoView 触发后填 dateOfBirth + 漢字/片假名姓名 ============
        # PayPal JP guest 表单的 password / dateOfBirth / firstName / lastName /
        # countrySpecificFirstName/Last 都在"懒加载段"，必须把页面滚到底才会渲染。
        # 参考 aBaiAutoplus _fill_paypal_unified_guest_form：先地址，后滚动，最后填这一组。
        if guest_country == "JP" and getattr(self, "_jp_payload", None):
            jp = self._jp_payload
            _react_force_fill = self._jp_react_force_fill

            # 触发懒加载渲染：滚到底 + 等 React 挂载
            try:
                self._eval("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            time.sleep(0.6)

            # 等 dateOfBirth 元素出现（最多 8 秒）
            for attempt in range(16):
                if self._eval("!!document.getElementById('dateOfBirth')"):
                    break
                time.sleep(0.5)

            # 生年月日：PayPal 用 YYYY/MM/DD 格式（aBaiAutoplus 也用这种）
            # ★★ 优先用 jp_identity 生成的 DOB（跟姓名/地址等同源），失败回退随机
            jp_full = getattr(self, "_jp_full_identity", None) or {}
            dob_value = jp_full.get("date_of_birth", "")
            if not dob_value:
                # 随机 1985-1999 年，月 1-12，日 1-28（避开 2/29 / 4/31 等非法日期）
                import secrets as _secrets
                _year = 1985 + _secrets.randbelow(15)
                _month = 1 + _secrets.randbelow(12)
                _day = 1 + _secrets.randbelow(28)
                dob_value = f"{_year:04d}/{_month:02d}/{_day:02d}"
            results["dob"] = _react_force_fill("dateOfBirth", dob_value, "dateOfBirth")
            time.sleep(0.3)

            # 漢字版 firstName/lastName
            # 注意：PayPal 的 firstName=名（given name），lastName=姓（family name）
            results["firstName_kanji"] = _react_force_fill(
                "firstName", jp["first_kanji"], "firstName(名-漢字)")
            results["lastName_kanji"] = _react_force_fill(
                "lastName", jp["last_kanji"], "lastName(姓-漢字)")
            time.sleep(0.3)

            # 等片假名字段挂载
            for attempt in range(16):
                exists = self._eval(
                    "(function(){"
                    "var f=document.getElementById('countrySpecificFirstName');"
                    "var l=document.getElementById('countrySpecificLastName');"
                    "return !!(f && l);"
                    "})()"
                )
                if exists:
                    break
                time.sleep(0.5)

            results["kana_first"] = _react_force_fill(
                "countrySpecificFirstName", jp["first_kana"], "countrySpecificFirstName(名-片假名)")
            results["kana_last"] = _react_force_fill(
                "countrySpecificLastName", jp["last_kana"], "countrySpecificLastName(姓-片假名)")
            time.sleep(0.5)

            # 重新校验：React 重渲染会把先填的字段清空。最多 2 轮补填。
            verify_fields = [
                ("firstName", jp["first_kanji"]),
                ("lastName", jp["last_kanji"]),
                ("countrySpecificFirstName", jp["first_kana"]),
                ("countrySpecificLastName", jp["last_kana"]),
                ("dateOfBirth", dob_value),
            ]
            for verify_round in range(2):
                refilled = 0
                for fid, fval in verify_fields:
                    cur = self._eval(
                        "(function(id){var e=document.getElementById(id);"
                        "return e ? String(e.value||'') : '__noel__';})(" + json.dumps(fid) + ")"
                    )
                    if cur != fval:
                        log.info(f"  [JP][round {verify_round}] {fid} 当前={cur!r} 期望={fval!r}，重填")
                        _react_force_fill(fid, fval, fid)
                        refilled += 1
                if refilled == 0:
                    break
                time.sleep(0.4)
            time.sleep(0.3)

        # BR guest 表单可能要求出生日期 / CPF。字段不存在时 keyboard_fill 会记录并跳过。
        if guest_country == "BR" and br_identity:
            try:
                self._eval("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            time.sleep(0.6)
            dob_value = br_identity.get("date_of_birth_dmy") or br_identity.get("date_of_birth", "")
            try:
                dob_hint = self._eval(
                    "(function(){"
                    "var e=document.getElementById('dateOfBirth')"
                    "||document.querySelector('input[name*=\"dateOfBirth\" i],input[id*=\"dateOfBirth\" i],input[autocomplete=\"bday\"]');"
                    "return e ? String(e.getAttribute('placeholder')||e.getAttribute('aria-label')||'') : '';"
                    "})()"
                ) or ""
                if "yyyy" in str(dob_hint).lower() and str(dob_hint).lower().find("yyyy") < str(dob_hint).lower().find("dd"):
                    dob_value = br_identity.get("date_of_birth") or dob_value
            except Exception:
                pass
            results["dob"] = keyboard_fill("dateOfBirth", dob_value, "dateOfBirth")
            time.sleep(0.3)
            results["cpf"] = keyboard_fill("taxId", br_identity.get("cpf", ""), "CPF")
            time.sleep(0.3)
        # password 字段：用 JS React-friendly 方式直接填（避免 actions.type 在 % 等特殊字符上被截断）
        try:
            ok = self._eval(
                f"(function(){{var e=document.getElementById('password');"
                f"if(!e)return false;e.focus();"
                f"var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                f"s.call(e,{json.dumps(paypal_password)});"
                f"e.dispatchEvent(new Event('input',{{bubbles:true}}));"
                f"e.dispatchEvent(new Event('change',{{bubbles:true}}));"
                f"e.dispatchEvent(new Event('blur',{{bubbles:true}}));"
                f"return e.value;}})()"
            )
            if ok == paypal_password:
                log.info(f"  ✓ [password] = {paypal_password[:30]}")
                results["password"] = True
            else:
                log.warning(f"  ⚠ [password] JS 填后实际={(ok or '')[:30]}, 用 keyboard 兜底")
                results["password"] = keyboard_fill("password", paypal_password, "password")
        except Exception as e:
            log.warning(f"  [password] JS 填失败: {e}")
            results["password"] = keyboard_fill("password", paypal_password, "password")
        time.sleep(1)
        if guest_country == "GB" and gb_identity:
            try:
                self._eval("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            for _ in range(16):
                if _resolve_field_id("dateOfBirth"):
                    break
                time.sleep(0.25)
            results["dob"] = keyboard_fill(
                "dateOfBirth", gb_identity.get("date_of_birth_dmy", ""), "dateOfBirth")
            time.sleep(0.35)
            results["taxResidency"] = self._select_paypal_tax_residency(
                gb_identity.get("tax_residency_country", "GB"),
                gb_identity.get("tax_residency_name", "United Kingdom"),
            )
            time.sleep(0.35)
        if guest_country == "BR":
            results["terms"] = accept_required_terms()
            log.info(f"  terms checkbox: {results['terms']}")
            time.sleep(0.5)

        # 验证字段
        if guest_country == "JP":
            check = self._eval(
                "(function(){var ids=['email','phone','cardNumber','cardExpiry','cardCvv',"
                "'firstName','lastName','countrySpecificFirstName','countrySpecificLastName',"
                "'dateOfBirth','billingLine1','billingCity','billingPostalCode','password'];"
                "var out={};ids.forEach(function(id){var e=document.getElementById(id);"
                "out[id]=e?(e.value||'').slice(0,30):'(missing)';});"
                "var s=document.getElementById('billingState');"
                "out['billingState']=s?s.value:'(missing)';return out;})()"
            )
        elif guest_country == "BR":
            check = self._eval(
                "(function(){var ids=['email','phone','cardNumber','cardExpiry','cardCvv',"
                "'firstName','lastName','dateOfBirth','billingLine1','billingCity',"
                "'billingPostalCode','taxId','password','billingNumber','billingNeighborhood'];"
                "var out={};ids.forEach(function(id){var e=document.getElementById(id)||document.getElementById('__pp_'+id);"
                "out[id]=e?(e.value||'').slice(0,30):'(missing)';});"
                "var s=document.getElementById('billingState');"
                "out['billingState']=s?s.value:'(missing)';return out;})()"
            )
        elif guest_country == "GB":
            check = self._eval(
                "(function(){var ids=['email','phone','cardNumber','cardExpiry','cardCvv',"
                "'firstName','lastName','dateOfBirth','billingLine1','billingCity','billingPostalCode','password'];"
                "var out={};ids.forEach(function(id){var e=document.getElementById(id)||document.getElementById('__pp_'+id);"
                "out[id]=e?(e.value||'').slice(0,30):'(missing)';});"
                "var s=document.getElementById('billingState');out['billingState']=s?s.value:'(missing)';"
                "var ts=Array.from(document.querySelectorAll('select')).find(function(e){"
                "var p=e.parentElement,t=[e.id,e.name,e.getAttribute('data-testid'),p&&p.innerText].join(' ');"
                "return /tax.{0,20}residen|residen.{0,20}tax|crs/i.test(t);});"
                "out['taxResidency']=ts?(ts.options[ts.selectedIndex]||{}).text||ts.value:"
                "(document.body.innerText.indexOf('Country of tax residency is required')<0?'selected/custom':'(missing)');"
                "return out;})()"
            )
        else:
            check = self._eval(
                "(function(){var ids=['email','phone','cardNumber','cardExpiry','cardCvv',"
                "'firstName','lastName','billingLine1','billingCity','billingPostalCode','password'];"
                "var out={};ids.forEach(function(id){var e=document.getElementById(id);"
                "out[id]=e?(e.value||'').slice(0,30):'(missing)';});"
                "var s=document.getElementById('billingState');"
                "out['billingState']=s?s.value:'(missing)';return out;})()"
            )
        log.info(f"  填写结果: {check}")
        return results

    # ============ PayPal "Confirm you're human" slider ============

    def _probe_paypal_slider_challenge(self) -> dict:
        """Locate PayPal's drag-to-the-right challenge on the page or in an iframe."""
        probe_js = r"""
        (function() {
          function visible(el) {
            if (!el || el.offsetParent === null) return false;
            var r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          }
          function meta(el) {
            if (!el) return '';
            return [el.id, el.className, el.getAttribute('role'),
              el.getAttribute('aria-label'), el.getAttribute('title'),
              el.getAttribute('data-testid')].join(' ').toLowerCase();
          }
          function blueBackground(el) {
            var value = getComputedStyle(el).backgroundColor || '';
            var m = value.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/i);
            if (!m) return false;
            var red = Number(m[1]), green = Number(m[2]), blue = Number(m[3]);
            return blue > 120 && blue > red * 1.25 && blue > green * 1.05;
          }
          function findTrack(el) {
            var er = el.getBoundingClientRect(), node = el.parentElement;
            var best = null, bestScore = -1;
            for (var depth = 0; node && depth < 6; depth++, node = node.parentElement) {
              if (!visible(node)) continue;
              var r = node.getBoundingClientRect();
              if (r.width < Math.max(230, er.width * 2) || r.height < er.height * 0.75 || r.height > 150) continue;
              var score = (6 - depth) + (/slider|track|range|drag/.test(meta(node)) ? 10 : 0);
              if (score > bestScore) { best = node; bestScore = score; }
            }
            return best;
          }

          var bodyText = String(document.body ? document.body.innerText : '');
          var challengeRe = /confirm you(?:'|\u2019)?re human|move the slider(?: all the way)? to the right|prove you(?:'|\u2019)?re human|deslice|mueva el control|スライダー|本人確認|人机验证/i;
          if (!challengeRe.test(bodyText)) return {present: false};

          var phraseRect = null;
          var textNodes = Array.from(document.querySelectorAll('p,div,span,h1,h2,h3'));
          for (var i = 0; i < textNodes.length; i++) {
            var own = String(textNodes[i].innerText || textNodes[i].textContent || '').trim();
            if (own.length <= 180 && /move the slider|deslice|mueva el control|スライダー/i.test(own)) {
              phraseRect = textNodes[i].getBoundingClientRect();
              break;
            }
          }

          var candidates = Array.from(document.querySelectorAll(
            'button,[role="button"],[role="slider"],input[type="range"],div,span'
          ));
          var best = null, bestTrack = null, bestScore = -1;
          for (var j = 0; j < candidates.length; j++) {
            var el = candidates[j];
            if (!visible(el)) continue;
            var r = el.getBoundingClientRect();
            if (r.width < 38 || r.width > 180 || r.height < 32 || r.height > 110) continue;
            var ownMeta = meta(el), ancestorMeta = '';
            var parent = el.parentElement;
            for (var d = 0; parent && d < 4; d++, parent = parent.parentElement) ancestorMeta += ' ' + meta(parent);
            var semantic = /slider|thumb|handle|range|drag/.test(ownMeta + ancestorMeta);
            var blue = blueBackground(el);
            var belowPrompt = !phraseRect || r.top >= phraseRect.bottom - 12;
            if (!semantic && !blue) continue;
            if (!belowPrompt && !semantic) continue;
            var track = findTrack(el);
            if (!track) continue;
            var tr = track.getBoundingClientRect();
            var nearLeft = r.left <= tr.left + tr.width * 0.38;
            var score = (semantic ? 12 : 0) + (blue ? 9 : 0) +
              (el.tagName === 'BUTTON' ? 3 : 0) + (belowPrompt ? 3 : 0) +
              (nearLeft ? 4 : 0) + 5;
            if (score > bestScore) { best = el; bestTrack = track; bestScore = score; }
          }

          var refresh = null;
          var refreshNodes = Array.from(document.querySelectorAll('button,[role="button"],[aria-label],[title]'));
          for (var k = 0; k < refreshNodes.length; k++) {
            var rm = meta(refreshNodes[k]);
            if (visible(refreshNodes[k]) && /refresh|reload|retry|new challenge|再試行|更新/.test(rm)) {
              refresh = refreshNodes[k];
              break;
            }
          }

          document.querySelectorAll('#__gpt_paypal_slider_knob').forEach(function(el) { el.removeAttribute('id'); });
          document.querySelectorAll('#__gpt_paypal_slider_refresh').forEach(function(el) { el.removeAttribute('id'); });
          if (best) best.id = '__gpt_paypal_slider_knob';
          if (refresh) refresh.id = '__gpt_paypal_slider_refresh';
          var br = best ? best.getBoundingClientRect() : null;
          var tr = bestTrack ? bestTrack.getBoundingClientRect() : null;
          return {
            present: true,
            has_knob: !!best,
            has_refresh: !!refresh,
            knob: br ? {x: br.left, y: br.top, width: br.width, height: br.height} : null,
            track: tr ? {x: tr.left, y: tr.top, width: tr.width, height: tr.height} : null
          };
        })()
        """

        contexts: list[object] = []
        queue: list[tuple[object, int]] = [(self.page, 0)]
        seen: set[tuple] = set()
        while queue and len(contexts) < 32:
            context, depth = queue.pop(0)
            stable_key = (
                getattr(context, "tab_id", None),
                getattr(context, "_frame_id", None),
                getattr(context, "_target_id", None),
            )
            context_key = stable_key if any(stable_key) else ("object", id(context))
            if context_key in seen:
                continue
            seen.add(context_key)
            contexts.append(context)
            if depth >= 4:
                continue
            try:
                children = list(context.get_frames())
            except Exception as exc:
                log.debug(f"  [slider] frame traversal depth={depth} error: {exc}")
                children = []
            queue.extend((child, depth + 1) for child in children)

        first_detected = None
        for index, context in enumerate(contexts):
            try:
                info = context.run_js(probe_js, as_expr=True) or {}
            except Exception as exc:
                log.debug(f"  [slider] context[{index}] probe error: {exc}")
                continue
            if not isinstance(info, dict) or not info.get("present"):
                continue
            challenge = {
                **info,
                "context": context,
                "context_index": index,
                "knob_element": None,
                "refresh_element": None,
            }
            if info.get("has_knob"):
                try:
                    challenge["knob_element"] = context.ele(
                        "css:#__gpt_paypal_slider_knob", timeout=0.6)
                except Exception:
                    pass
            if info.get("has_refresh"):
                try:
                    challenge["refresh_element"] = context.ele(
                        "css:#__gpt_paypal_slider_refresh", timeout=0.4)
                except Exception:
                    pass
            if challenge.get("knob_element") is not None:
                return challenge
            first_detected = first_detected or challenge
        return first_detected or {"present": False}

    def _recover_paypal_uri_too_long(self) -> bool:
        """Resume the modular signup route after PayPal creates a recursive URL."""
        if self._paypal_uri_recoveries >= 2:
            return False
        try:
            current_url = str(self.page.url or "")
        except Exception:
            current_url = ""
        body_error = bool(self._eval(
            "(function(){var t=(document.body&&document.body.innerText)||'';"
            "return /(?:Error:\\s*)?URI Too Long|Request-URI Too Large|414 Request/i.test(t);})()"
        ))
        recursive_redirect = (
            len(current_url) > 3500
            or current_url.lower().count("ulonboardredirect") > 1
        )
        if not body_error and not recursive_redirect:
            return False

        recovery_url = build_paypal_contact_recovery_url(
            current_url,
            self._paypal_clean_landing_url,
            self._paypal_entry_url,
            country=self._paypal_country,
        )
        if not recovery_url:
            log.warning("  [paypal] URI Too Long detected, but no stable checkout token was found")
            return False
        self._paypal_uri_recoveries += 1
        log.warning(
            f"  [paypal] URI Too Long/recursive redirect detected "
            f"(url_len={len(current_url)}); resuming with normalized contact URL"
        )
        try:
            self.page.get(recovery_url)
            self._paypal_clean_landing_url = recovery_url
            time.sleep(2.0)
            return True
        except Exception as exc:
            log.warning(f"  [paypal] normalized contact navigation failed: {exc}")
            return False

    def _is_paypal_contact_step(self) -> bool:
        return bool(self._eval(
            "(function(){"
            "var p=location.pathname||'';"
            "var email=document.querySelector('input[name=\"email\"],input[autocomplete=\"email\"]');"
            "var first=document.querySelector('input[name=\"firstName\"],input[autocomplete=\"given-name\"]');"
            "var last=document.querySelector('input[name=\"lastName\"],input[autocomplete=\"family-name\"]');"
            "var phone=document.querySelector('input[name=\"phone.nationalNumber\"],input[type=\"tel\"]');"
            "return /\\/pay\\/checkout\\/signup\\/contact/i.test(p)||!!(email&&first&&last&&phone);"
            "})()",
            default=False,
        ))

    def _handle_paypal_contact_step(self, *, email: str, first_name: str,
                                    last_name: str, phone: str, country: str) -> bool:
        """Fill PayPal's modular mobile contact step and continue to card entry."""
        if not self._is_paypal_contact_step():
            return False
        try:
            from phone_pool import to_local_phone
            phone_local = to_local_phone(phone, country=country)
        except Exception:
            phone_local = "".join(ch for ch in str(phone or "") if ch.isdigit())
        if not phone_local:
            log.error("[paypal-contact] mobile number is empty")
            return False

        log.info(f"[paypal-contact] filling modular contact step (country={country})")
        country_result = self._eval(
            "(function(target){"
            "var selects=Array.from(document.querySelectorAll('select'));"
            "for(var i=0;i<selects.length;i++){var s=selects[i],opts=Array.from(s.options||[]);"
            "var opt=opts.find(function(o){return String(o.value).toUpperCase()===target||String(o.text).trim().toUpperCase()===target;});"
            "if(!opt)continue;"
            "var set=Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype,'value').set;"
            "set.call(s,opt.value);s.dispatchEvent(new Event('input',{bubbles:true}));"
            "s.dispatchEvent(new Event('change',{bubbles:true}));return opt.value;}"
            "return '';})(" + json.dumps(country) + ")",
            default="",
        )
        log.info(f"  contact country selected: {country_result or '(not found)'}")
        time.sleep(0.8)

        values = {
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
            "phone.nationalNumber": phone_local,
        }
        filled = self._eval(
            "(function(values){"
            "var out={};Object.keys(values).forEach(function(name){"
            "var e=document.querySelector('input[name='+JSON.stringify(name)+']');"
            "if(!e&&name==='email')e=document.querySelector('input[autocomplete=\"email\"]');"
            "if(!e&&name==='firstName')e=document.querySelector('input[autocomplete=\"given-name\"]');"
            "if(!e&&name==='lastName')e=document.querySelector('input[autocomplete=\"family-name\"]');"
            "if(!e&&name==='phone.nationalNumber')e=document.querySelector('input[type=\"tel\"]');"
            "if(!e){out[name]=null;return;}"
            "e.focus();var set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
            "set.call(e,values[name]);e.dispatchEvent(new Event('input',{bubbles:true}));"
            "e.dispatchEvent(new Event('change',{bubbles:true}));e.dispatchEvent(new Event('blur',{bubbles:true}));"
            "out[name]=e.value;});return out;})(" + json.dumps(values) + ")",
            default={},
        )
        missing = [name for name, value in values.items() if not (filled or {}).get(name)]
        if missing:
            log.error(f"  [paypal-contact] fields not filled: {', '.join(missing)}")
            return False

        try:
            submit = self.page.ele("css:button[type=submit]", timeout=2)
            if submit:
                submit.click()
            else:
                raise RuntimeError("submit button not found")
        except Exception:
            clicked = self._eval(
                "(function(){var b=document.querySelector('button[type=\"submit\"]');"
                "if(!b||b.disabled)return false;b.click();return true;})()",
                default=False,
            )
            if not clicked:
                log.error("  [paypal-contact] Continue button not available")
                return False
        log.info("  click [contact Continue]")
        return self._wait_for_paypal_card_transition(
            60,
            source="contact Continue",
            card_probe_js=(
                "!!document.querySelector('#cardNumber,input[name=\"cardNumber\"],"
                "input[autocomplete=\"cc-number\"],[data-testid*=\"cardNumber\" i]')"
            ),
        )

    def _drag_paypal_slider(self, challenge: dict) -> bool:
        knob = challenge.get("knob_element")
        context = challenge.get("context") or self.page
        knob_rect = challenge.get("knob") or {}
        track_rect = challenge.get("track") or {}
        if knob is None or not track_rect:
            return False
        start_offset = float(knob_rect.get("x") or 0) - float(track_rect.get("x") or 0)
        distance = (
            float(track_rect.get("width") or 0)
            - float(knob_rect.get("width") or 0)
            - start_offset
            - 4.0
        )
        if distance < 60:
            log.warning(f"  [slider] invalid drag distance: {distance:.1f}")
            return False

        actions = getattr(context, "actions", None) or self.page.actions
        try:
            actions.move_to(knob, duration=random.uniform(0.25, 0.45))
            time.sleep(random.uniform(0.08, 0.18))
            actions.hold()
            points = random.randint(11, 16)
            previous_x = 0.0
            previous_y = 0.0
            for step in range(1, points + 1):
                progress = step / points
                eased = 1 - (1 - progress) ** 2.2
                target_x = distance * eased
                target_y = 0.0 if step == points else random.uniform(-1.8, 1.8)
                actions.move(
                    offset_x=target_x - previous_x,
                    offset_y=target_y - previous_y,
                    duration=random.uniform(0.035, 0.085),
                )
                previous_x = target_x
                previous_y = target_y
            time.sleep(random.uniform(0.12, 0.25))
            actions.release()
            log.info(
                f"  [slider] dragged {distance:.1f}px in {points} steps "
                f"(context={challenge.get('context_index', 0)})"
            )
            return True
        except Exception as exc:
            try:
                actions.release()
            except Exception:
                pass
            log.warning(f"  [slider] drag failed: {exc}")
            return False

    def _handle_paypal_slider_challenge(self, max_attempts: int = 3,
                                        verify_timeout: float = 8.0) -> dict:
        challenge = self._probe_paypal_slider_challenge()
        if not challenge.get("present"):
            return {"detected": False, "solved": False, "attempts": 0}

        log.info("[paypal-slider] detected Confirm you're human challenge")
        last_reason = "knob_not_found"
        for attempt in range(1, max(1, max_attempts) + 1):
            if challenge.get("knob_element") is None:
                last_reason = "knob_not_found"
                log.warning(f"  [slider] attempt {attempt}: drag handle not found")
            elif self._drag_paypal_slider(challenge):
                last_reason = "challenge_still_visible"
                deadline = time.time() + max(0.5, verify_timeout)
                while time.time() < deadline:
                    time.sleep(0.4)
                    current = self._probe_paypal_slider_challenge()
                    if not current.get("present"):
                        log.info(f"[paypal-slider] solved on attempt {attempt}")
                        return {"detected": True, "solved": True, "attempts": attempt}
                    challenge = current
            else:
                last_reason = "drag_failed"

            if attempt >= max(1, max_attempts):
                break
            refresh = challenge.get("refresh_element")
            try:
                if refresh is not None:
                    refresh.click()
                    log.info("  [slider] refreshed challenge")
                else:
                    self.page.refresh(ignore_cache=False)
                    log.info("  [slider] refreshed PayPal page")
            except Exception as exc:
                log.debug(f"  [slider] refresh failed: {exc}")
            time.sleep(1.2)
            challenge = self._probe_paypal_slider_challenge()
            if not challenge.get("present"):
                return {"detected": True, "solved": True, "attempts": attempt}

        log.warning(f"[paypal-slider] unresolved after {max_attempts} attempt(s): {last_reason}")
        return {
            "detected": True,
            "solved": False,
            "attempts": max(1, max_attempts),
            "reason": last_reason,
        }

    def _wait_for_paypal_card_transition(self, timeout_seconds: float, *,
                                         source: str, card_probe_js: str) -> bool:
        """Wait for the card step while handling an interstitial slider first."""
        deadline = time.time() + max(1.0, timeout_seconds)
        slider_attempted = False
        while time.time() < deadline:
            time.sleep(1)
            if self._recover_paypal_uri_too_long():
                slider_attempted = False
                continue
            challenge = self._probe_paypal_slider_challenge()
            if challenge.get("present"):
                if not slider_attempted:
                    slider_attempted = True
                    result = self._handle_paypal_slider_challenge(max_attempts=3)
                    log.info(
                        f"  {source} slider: solved={result.get('solved')} "
                        f"attempts={result.get('attempts')}"
                    )
                # Do not treat a card form mounted behind the challenge as ready.
                continue

            current_url = self.page.url or ""
            if "/checkoutweb/signup" in current_url:
                log.info(f"  ✓ {source} → 经典 signup: {current_url[:80]}")
                return True
            if self._eval(card_probe_js):
                log.info(f"  ✓ {source} 同页 hydrate 出卡片表单: {current_url[:80]}")
                return True
        return False

    # ============ reCAPTCHA / hCaptcha 解决（YesCaptcha） ============

    def _detect_captcha(self) -> dict:
        """探测当前页面有没有 captcha challenge，返回 {provider, site_key, page_url, invisible, enterprise, version}"""
        info = self._eval(
            """
            (function() {
                var frames = Array.from(document.querySelectorAll('iframe')).map(function(f) {
                    return {src: f.src || '', title: f.title || '', w: f.clientWidth, h: f.clientHeight};
                });
                // 普通 google recaptcha frame: gstatic.com/recaptcha
                // PayPal 自家包装: paypalobjects.com/.../recaptcha_v2.html?siteKey=xxx
                var hasRecaptchaFrame = frames.some(function(f) {
                    return /recaptcha/i.test(f.src);
                });
                var hasHcaptchaFrame = frames.some(function(f) { return /hcaptcha/i.test(f.src); });
                if (!hasRecaptchaFrame && !hasHcaptchaFrame) {
                    if (!document.querySelector('div.g-recaptcha[data-sitekey], div.h-captcha[data-sitekey], [data-sitekey]')) {
                        return null;
                    }
                }
                var siteKey = '';
                var enterprise = false;
                var invisible = false;
                var provider = hasHcaptchaFrame ? 'hcaptcha' : 'recaptcha';

                // 1) DOM 上的 [data-sitekey]
                var widget = document.querySelector('.g-recaptcha[data-sitekey], .h-captcha[data-sitekey], [data-sitekey]');
                if (widget) {
                    siteKey = widget.getAttribute('data-sitekey') || '';
                    var sz = (widget.getAttribute('data-size') || '').toLowerCase();
                    invisible = sz === 'invisible';
                }

                // 2) iframe src 里的 sitekey/siteKey/k 参数（PayPal 用 siteKey=）
                if (!siteKey) {
                    for (var i = 0; i < frames.length; i++) {
                        var src = frames[i].src || '';
                        if (!/recaptcha/i.test(src)) continue;
                        // PayPal: ?siteKey=xxx
                        var m1 = src.match(/[?&]siteKey=([^&]+)/i);
                        if (m1) { siteKey = decodeURIComponent(m1[1]); break; }
                        // Google: ?k=xxx
                        var m2 = src.match(/[?&]k=([^&]+)/);
                        if (m2) { siteKey = decodeURIComponent(m2[1]); break; }
                    }
                }
                if (!siteKey && hasHcaptchaFrame) {
                    for (var j = 0; j < frames.length; j++) {
                        var s = frames[j].src || '';
                        if (!/hcaptcha/i.test(s)) continue;
                        var m3 = s.match(/[?&]sitekey=([^&]+)/);
                        if (m3) { siteKey = decodeURIComponent(m3[1]); break; }
                    }
                }

                // 3) 兜底：扫所有 script src 里的 recaptcha key 参数
                if (!siteKey) {
                    var allText = document.documentElement.outerHTML;
                    var m4 = allText.match(/siteKey["'\\s:=]+([0-9A-Za-z_-]{30,50})/);
                    if (m4) siteKey = m4[1];
                }

                // 检测 enterprise / invisible 来自 frame URL
                for (var k = 0; k < frames.length; k++) {
                    var fs = frames[k].src || '';
                    if (fs.indexOf('recaptcha/enterprise') >= 0) enterprise = true;
                    if (/invisible/i.test(fs)) invisible = true;
                }

                return {
                    provider: provider,
                    siteKey: siteKey,
                    pageUrl: location.href,
                    invisible: invisible,
                    enterprise: enterprise,
                };
            })()
            """
        )
        return info or {}

    def _inject_captcha_token(self, token: str, provider: str = "recaptcha"):
        """把 token 注入到页面 g-recaptcha-response / h-captcha-response 并触发 callback。"""
        injection = (
            "(function(token, provider){"
            "var touch=function(el){if(!el)return;el.value=token;el.innerHTML=token;"
            "try{el.dispatchEvent(new Event('input',{bubbles:true}));}catch(e){}"
            "try{el.dispatchEvent(new Event('change',{bubbles:true}));}catch(e){}};"
            "var ensure=function(sel,id,name){var el=document.querySelector(sel);"
            "if(!el){el=document.createElement('textarea');"
            "if(id)el.id=id;if(name)el.name=name;el.style.display='none';document.body.appendChild(el);}"
            "touch(el);return el;};"
            "ensure('#g-recaptcha-response','g-recaptcha-response','g-recaptcha-response');"
            "ensure('textarea[name=\"g-recaptcha-response\"]','','g-recaptcha-response');"
            "ensure('textarea[name=\"h-captcha-response\"]','','h-captcha-response');"
            "Array.from(document.querySelectorAll('[name*=\"captcha-response\" i],textarea[id*=\"captcha\" i]')).forEach(touch);"
            "Array.from(document.querySelectorAll('[data-callback]')).forEach(function(el){"
            "var cb=el.getAttribute('data-callback');if(!cb)return;"
            "var fn=window[cb];if(typeof fn==='function'){try{fn(token);}catch(e){}}});"
            "if(window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients){"
            "var clients=window.___grecaptcha_cfg.clients;"
            "for(var cid in clients){var stack=[clients[cid]];"
            "while(stack.length){var obj=stack.pop();if(!obj||typeof obj!=='object')continue;"
            "for(var k in obj){var v=obj[k];"
            "if(typeof v==='function' && /callback/i.test(k)){try{v(token);}catch(e){}}"
            "else if(v && typeof v==='object'){stack.push(v);}}}}}"
            "return true;})(" + json.dumps(token) + "," + json.dumps(provider) + ")"
        )
        self._eval(injection)

    def _solve_captcha_with_yescaptcha(self, max_attempts: int = 2) -> bool:
        """检测 + 解 + 注入。成功返回 True。"""
        api_key = (getattr(config, "CAPTCHA_API_KEY", "") or "").strip()
        if not api_key:
            log.warning("[captcha] CAPTCHA_API_KEY 未配置，跳过")
            return False

        try:
            import recaptcha_solver as rs
        except Exception as e:
            log.warning(f"[captcha] recaptcha_solver 模块导入失败: {e}")
            return False

        for attempt in range(1, max_attempts + 1):
            info = self._detect_captcha()
            if not info or not info.get("siteKey"):
                log.info(f"[captcha] attempt#{attempt}: 没探测到 captcha，跳过")
                return False

            provider = info.get("provider", "recaptcha")
            site_key = info["siteKey"]
            page_url = info.get("pageUrl") or self.page.url
            invisible = bool(info.get("invisible"))
            enterprise = bool(info.get("enterprise"))

            log.info(f"[captcha] attempt#{attempt}: provider={provider} sitekey={site_key[:25]}... "
                     f"invisible={invisible} enterprise={enterprise}")

            try:
                if provider == "hcaptcha":
                    token = rs.solve_hcaptcha(api_key, site_key, page_url, invisible=invisible)
                else:
                    token = rs.solve_recaptcha_v2(
                        api_key, site_key, page_url,
                        invisible=invisible, enterprise=enterprise,
                    )
            except Exception as e:
                log.warning(f"[captcha] 求解失败 attempt#{attempt}: {e}")
                if attempt >= max_attempts:
                    return False
                time.sleep(2)
                continue

            log.info(f"[captcha] 拿到 token (len={len(token)})，注入...")
            self._inject_captcha_token(token, provider=provider)
            time.sleep(3)

            # 校验：看 captcha 还在不在
            still_present = self._eval(
                "!!document.querySelector('iframe[src*=\"recaptcha\"], iframe[src*=\"hcaptcha\"]') && "
                "!!document.querySelector('.g-recaptcha, .h-captcha')"
            )
            # 看 g-recaptcha-response 已经填进去
            response_filled = self._eval(
                "(function(){var t=document.querySelector('textarea[name=\"g-recaptcha-response\"], #g-recaptcha-response');"
                "return t && t.value && t.value.length > 50;})()"
            )
            if response_filled:
                log.info("[captcha] token 已注入到 textarea")
                return True

            log.warning(f"[captcha] attempt#{attempt}: 注入完没生效，重试")
            time.sleep(2)

        return False


    def pay_paypal(self, card_config: dict = None,
                   paypal_account: dict = None,
                   billing_address: dict = None,
                   paypal_password: str = "") -> dict:
        """完整 PayPal 流程（参照 hanyi0000 项目 paypal_pay.fill_paypal）。

        在 paypal.com/checkoutweb/signup 这个 guest 卡页面上：
          1) 用 #id 精准填所有字段
          2) 点 "Agree & Create Account"
          3) 等结果（OTP / 跳回 chatgpt / 失败）
        """
        log.info("[paypal] 进入 PayPal 自动化...")
        # 等浏览器跳到 paypal.com
        for _ in range(20):
            if "paypal.com" in (self.page.url or ""):
                break
            time.sleep(1)
        if "paypal.com" not in (self.page.url or ""):
            return {"status": "not_on_paypal", "url": self.page.url}

        current_landing = str(self.page.url or "")
        if not self._paypal_clean_landing_url and len(current_landing) < 2500:
            self._paypal_clean_landing_url = current_landing

        time.sleep(3)

        # 解析 card / address
        c = dict(card_config or {})
        addr = billing_address or c.get("address") or {
            "street": "123 Main St", "city": "New York", "state": "NY", "zip": "10001",
        }

        # 国家化模式：如果 PayPal 目标国家不是卡地址国家，就用 address_provider
        # 拉/生成目标国家账单地址。卡仍可来自 cards.txt，但表单 country/address 要一致。
        paypal_country = "US"
        gb_identity = None
        try:
            import config as _cfg
            _explicit_country = (
                c.get("paypal_country")
                or c.get("billing_country")
                or c.get("checkout_country")
                or ""
            )
            _bc = (str(_explicit_country) or getattr(_cfg, "BILLING_COUNTRY", "") or "").strip().upper()
            if not _bc or _bc == "AUTO":
                _bc = (str(c.get("checkout_country") or "") or getattr(_cfg, "CHECKOUT_COUNTRY", "") or "").strip().upper()
            paypal_country = _bc or "US"
            self._paypal_country = paypal_country
            if paypal_country == "GB":
                from gb_identity import generate_gb_identity_for_paypal
                gb_identity = generate_gb_identity_for_paypal()
                c["first_name"] = gb_identity["first_name"]
                c["last_name"] = gb_identity["last_name"]
                c["_gb_identity"] = gb_identity
                addr = dict(gb_identity["address"])
                log.info(
                    f"[paypal] GB 成组资料: {gb_identity['name']} | "
                    f"DOB={gb_identity['date_of_birth_dmy']} | "
                    f"{addr['street']} | {addr['city']} | {addr['zip']}"
                )
            elif paypal_country and paypal_country != "US" and (addr or {}).get("country", "US").upper() != paypal_country:
                from address_provider import get_billing_address as _get_addr
                use_remote = bool(getattr(_cfg, "USE_MEIGUODIZHI", True))
                country_addr = _get_addr(paypal_country, base=None, prefer_remote=use_remote)
                log.info(f"[paypal] country={paypal_country}，覆盖账单地址: {country_addr.get('street')} | {country_addr.get('city')} | {country_addr.get('zip')} (source={country_addr.get('_source')})")
                addr = country_addr
        except Exception as _addr_e:
            log.warning(f"[paypal] 国家化地址覆盖失败，沿用原 addr: {_addr_e}")

        target_email = (paypal_account or {}).get("email") or ""
        if not target_email:
            # 优先用 card_config 里 chatgpt_email（=注册的 outlook 邮箱），PayPal 不会拉黑真实邮箱
            target_email = (card_config or {}).get("chatgpt_email") or ""
        if not target_email:
            target_email = ((gb_identity or {}).get("email")
                            or "guest" + str(random.randint(10000000, 99999999)) + "@gmail.com")
        if not paypal_password:
            paypal_password = ((paypal_account or {}).get("password")
                               or (gb_identity or {}).get("password")
                               or _random_password(14))

        log.info(f"[paypal] target_email={target_email} card=...{c.get('cardNumber', c.get('number', ''))[-4:]}")
        log.info(f"[paypal] addr={addr}")

        # ============ SMS provider 申请号码（smsbower 模式才走） ============
        # 62us 模式 / 没配置 SMS_PROVIDER 时不动；smsbower 模式下从平台抢一个号
        # 写到 c['phone']，让后续 _fill_paypal_guest_form 把它填进 PayPal 表单。
        sms_activation = None
        sms_provider_inst = None
        sms_source = str(c.get("sms_source") or "custom").strip().lower()
        try:
            from sms_provider import get_sms_provider
            sms_provider_inst = get_sms_provider(card=c)
            if sms_source != "platform" and sms_provider_inst is not None and sms_provider_inst.name != "62us":
                log.info(f"  [sms] sms_source={sms_source}，跳过平台自动选号，使用固定号码")
                sms_provider_inst = None
            if sms_provider_inst is not None and sms_provider_inst.name != "62us":
                log.info(f"  [sms] 用 provider={sms_provider_inst.name}, 申请号码...")
                # 余额检查（如果支持）
                if hasattr(sms_provider_inst, "get_balance"):
                    bal = sms_provider_inst.get_balance()
                    if bal is not None:
                        log.info(f"  [sms] {sms_provider_inst.name} 余额: {bal}")
                act = sms_provider_inst.request_phone()
                if act and act.get("phone"):
                    log.info(f"  [sms] 拿到号码: {act['phone']} (id={act['id']})")
                    c = dict(c)            # 不污染调用方传进来的 dict
                    c["phone"] = act["phone"]
                    sms_activation = act
                else:
                    log.error(f"  [sms] {sms_provider_inst.name} 申请号码失败，回退到原 phone")
                    sms_provider_inst = None
        except ImportError:
            sms_provider_inst = None
        except Exception as e:
            log.warning(f"  [sms] provider init err: {e}")
            sms_provider_inst = None

        # 实例上保留 activation，方便后续 stage 拿
        self._sms_activation = sms_activation
        self._sms_provider = sms_provider_inst

        # PayPal can show the slider on /pay before any login/signup controls
        # are mounted. Handle it before classifying the current PayPal route.
        initial_slider = self._handle_paypal_slider_challenge(max_attempts=3)
        if initial_slider.get("detected"):
            log.info(
                f"[paypal] initial slider result: solved={initial_slider.get('solved')} "
                f"attempts={initial_slider.get('attempts')}"
            )
            if initial_slider.get("solved"):
                time.sleep(1.0)

        self._recover_paypal_uri_too_long()
        if self._is_paypal_contact_step():
            contact_ok = self._handle_paypal_contact_step(
                email=target_email,
                first_name=c.get("first_name") or "James",
                last_name=c.get("last_name") or "Smith",
                phone=c.get("phone") or "",
                country=paypal_country,
            )
            if not contact_ok:
                return {"status": "contact_step_no_card", "url": self.page.url}

        # 路径 0：/agreements/approve?ba_token=... → BA token 授权页。
        # 这个页面会先弹 hCaptcha 滑块（YesCaptcha 扩展自动解），
        # 解完后通常跳到 modxo 极简 email 页。
        # 我们的策略：等 URL 变（或出现下一阶段的输入框），不再傻点 Approve。
        cur_url = self.page.url or ""
        if "/agreements/approve" in cur_url or "/agreements/billing-approve" in cur_url:
            log.info("[paypal] 在 /agreements/approve 授权页")
            time.sleep(3)
            # 先判断这页是哪种形态
            #   1) 滑块挑战 (Confirm you're human / Move the slider)
            #   2) 直接给 email 输入框 (modxo 入口的多语言版本)
            #   3) 经典 Login + Create an Account 按钮
            page_kind = self._eval(
                "(function(){"
                # 只算"可见"的 input
                "var visible=function(el){return el && el.offsetParent!==null;};"
                "var em=document.querySelector('input[type=\"email\"], input[name*=\"mail\" i], input[id*=\"mail\" i]');"
                "var pw=document.querySelector('input[type=\"password\"]');"
                "var emV=visible(em), pwV=visible(pw);"
                # email-only 形态：邮箱可见 + 密码不可见（即使 DOM 有也算）
                "if(emV && !pwV)return 'email_only';"
                # 滑块挑战
                "var t=(document.body?document.body.innerText:'').toLowerCase();"
                "if(/confirm you'?re human|move the slider|prove you'?re human|verifique que|deslice|本人(?:確認|认证)|スライダー|ロボットでない/i.test(t))return 'slider';"
                # login 形态：email + password 都可见
                "if(emV && pwV)return 'login';"
                "return 'unknown';})()"
            )
            log.info(f"  approve 页形态: {page_kind}")

            if page_kind == 'email_only':
                # 直接当 modxo 邮箱步处理
                log.info("  当作 modxo 邮箱页处理")
                typed = self._eval(
                    "(function(em){"
                    "var inp=document.querySelector('input[type=\"email\"], input[name*=\"mail\" i], input[id*=\"mail\" i]');"
                    "if(!inp)return false;"
                    "inp.focus();"
                    "var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                    "setter.call(inp,em);"
                    "inp.dispatchEvent(new Event('input',{bubbles:true}));"
                    "inp.dispatchEvent(new Event('change',{bubbles:true}));"
                    "inp.dispatchEvent(new Event('blur',{bubbles:true}));"
                    "return inp.value;})(" + json.dumps(target_email) + ")"
                )
                log.info(f"  email 已填: {typed}")
                time.sleep(2)
                cont = self._eval(
                    "(function(){var bs=document.querySelectorAll('button');"
                    "for(var i=0;i<bs.length;i++){var b=bs[i];if(b.disabled)continue;"
                    "if(b.offsetParent===null)continue;"
                    "var t=(b.textContent||'').trim();"
                    # 多语言: Continue / Siguiente / Suivant / 继续 / Next / 次へ / 続行 / 支払いを続ける
                    "if(/Continue|Siguiente|Suivant|Next|继续|^下一步$|次へ|続行|つぎへ|支払い(?:を|に)?続ける|お支払いに進む/i.test(t)){b.scrollIntoView({block:'center'});b.click();return t;}}return null;})()"
                )
                log.info(f"  click Continue: {cont}")
                # 等 hydrate 跳到 signup
                if not self._wait_for_paypal_card_transition(
                    45,
                    source="approve email",
                    card_probe_js=(
                        "!!document.getElementById('cardNumber') || "
                        "!!document.querySelector('input[name=\"cardNumber\"], input[autocomplete=\"cc-number\"]')"
                    ),
                ):
                    log.warning(f"  45s 未进入卡填写, url={self.page.url[:120]}")
            elif page_kind == 'slider' or page_kind == 'unknown':
                slider_result = self._handle_paypal_slider_challenge(max_attempts=3)
                if slider_result.get("detected"):
                    log.info(
                        f"  approve slider: solved={slider_result.get('solved')} "
                        f"attempts={slider_result.get('attempts')}"
                    )
                # The extension/manual path remains available while waiting for
                # the route transition if the local drag did not clear it.
                log.info("  等待滑块完成 + 进入下一步")
                wait_started = time.time()
                while time.time() - wait_started < 120:
                    u = self.page.url or ""
                    if "modxo" in u or "vaulted_not_recurring" in u or "Pay_With_Card" in u:
                        log.info(f"  ✓ approve → modxo: {u[:80]}")
                        break
                    if "/checkoutweb/signup" in u:
                        log.info(f"  ✓ approve → 经典 signup: {u[:80]}")
                        break
                    u_lower = u.lower()
                    host = u.split("//", 1)[-1].split("/", 1)[0].lower()
                    if "redirect_status=succeeded" in u_lower:
                        log.info(f"  ✓ approve 直接跳回（succeeded）: {u[:120]}")
                        return {"status": "success", "url": u}
                    if ("redirect_status=failed" in u_lower
                            or "redirect_status=canceled" in u_lower
                            or "redirect_status=cancelled" in u_lower):
                        log.error(f"  ✗ approve 跳回带失败标记: {u[:120]}")
                        return {"status": "paypal_canceled_or_failed", "url": u}
                    if ("openai.com" in host or "chatgpt.com" in host) and "/return" in u:
                        log.info(f"  ✓ approve /return 跳回: {u[:120]}")
                        return {"status": "success", "url": u}
                    time.sleep(3)
                else:
                    log.warning(f"  approve 阶段 120s 没前进, url={self.page.url[:120]}")

        # 重新读 URL（可能已经从 approve 跳到下一阶段）
        cur_url = self.page.url or ""

        # 是不是 guest checkout signup 页面？
        # PayPal 入口实际有 N 种：
        #   /pay?token=...                   → "Pay with PayPal" 登录页（带 Create an Account 按钮）
        #   /pay/?...modxo_vaulted_not_recurring-Pay_With_Card → 新版极简: 只一个 email 框
        #   /checkoutweb/signup?...          → guest 卡填写页（有 #cardNumber + #billingLine1）
        #   /pay/billing?...                 → 新版直接卡填写页（也有 #cardNumber 等）
        #   /webapps/hermes/...              → 已登录后的 review/approval 页
        #   /agreements/approve?ba_token=... → 已处理（上方）
        # 先等 hydrate（最多 30s — 手机版 PayPal hydrate 比桌面慢，给足时间）
        is_guest_signup = False
        hydrate_start = time.time()
        # ★★★ 手机版 PayPal /checkoutweb/signup DOM 跟桌面不同：
        #   桌面: #cardNumber / #billingLine1 / #firstName 等固定 id
        #   手机: 用 React 动态生成 id（如 "field-cardNumber-xxx"）+ data-testid /
        #         autocomplete / inputmode 属性。最稳的检测是 autocomplete="cc-number"
        #         (W3C 标准，桌面手机一致)，加上 data-testid / aria-label / type 兜底。
        _GUEST_CARD_DETECT = (
            "(function(){"
            "  function vis(el){return el && el.offsetParent !== null;}"
            "  var sels = ["
            "    '#cardNumber',"
            "    'input[name=\"cardNumber\"]',"
            "    'input[autocomplete=\"cc-number\"]',"
            "    'input[autocomplete*=\"cc-number\" i]',"
            "    'input[id*=\"cardNumber\" i]',"
            "    'input[id*=\"card-number\" i]',"
            "    'input[id*=\"card_number\" i]',"
            "    'input[name*=\"cardNumber\" i]',"
            "    'input[data-testid*=\"cardNumber\" i]',"
            "    'input[data-testid*=\"card-number\" i]',"
            "    'input[data-testid=\"card-number-input\"]',"
            "    'input[aria-label*=\"card number\" i]',"
            "    'input[aria-label*=\"カード番号\"]',"
            "    'input[placeholder*=\"カード番号\"]',"
            "    'input[type=\"tel\"][inputmode=\"numeric\"]'"
            "  ];"
            "  for (var i = 0; i < sels.length; i++) {"
            "    var el = document.querySelector(sels[i]);"
            "    if (vis(el)) return true;"
            "  }"
            "  return false;"
            "})()"
        )
        while time.time() - hydrate_start < 30:
            is_guest_signup = self._eval(_GUEST_CARD_DETECT)
            if is_guest_signup:
                break
            time.sleep(1)

        # 优先识别新版 modxo 极简 guest 流: 只有一个 email 输入框
        is_modxo_email_step = False
        if not is_guest_signup:
            is_modxo_email_step = self._eval(
                "(function(){"
                "var url=location.href;"
                # URL 含 modxo / vaulted_not_recurring / Pay_With_Card
                "if(!/modxo|vaulted_not_recurring|Pay_With_Card/i.test(url))return false;"
                # 页面有 email input + Continue 按钮 + 标题 'Create a PayPal account'
                "var em=document.querySelector('input[type=\"email\"], input[name*=\"mail\" i], input[id*=\"mail\" i]');"
                "if(!em)return false;"
                "var t=document.body?document.body.innerText:'';"
                "if(/Continue to Payment|Create a PayPal account|PayPalアカウント(?:を作成|の開設)|お支払いに進む/i.test(t))return true;"
                "return false;})()"
            )

        if is_modxo_email_step:
            log.info("[paypal] modxo 极简流: 先输 email → Continue to Payment")
            email_typed = self._eval(
                "(function(em){"
                "var inp=document.querySelector('input[type=\"email\"], input[name*=\"mail\" i], input[id*=\"mail\" i]');"
                "if(!inp)return false;"
                "inp.focus();"
                "var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                "setter.call(inp,em);"
                "inp.dispatchEvent(new Event('input',{bubbles:true}));"
                "inp.dispatchEvent(new Event('change',{bubbles:true}));"
                "inp.dispatchEvent(new Event('blur',{bubbles:true}));"
                "return inp.value;})(" + json.dumps(target_email) + ")"
            )
            log.info(f"  modxo email 已填: {email_typed}")
            time.sleep(2)
            # 点 Continue to Payment
            clicked_cont = self._eval(
                "(function(){var bs=document.querySelectorAll('button');"
                "for(var i=0;i<bs.length;i++){var b=bs[i];if(b.disabled)continue;"
                "var t=(b.textContent||'').trim();"
                "if(/Continue to Payment|Continue|继续|次へ|続行|つぎへ|支払い(?:を|に)?続ける|お支払いに進む/i.test(t)){b.scrollIntoView({block:'center'});b.click();return t;}}return null;})()"
            )
            log.info(f"  click [Continue]: {clicked_cont}")
            # 等待跳到下一步表单页
            if not self._wait_for_paypal_card_transition(
                30,
                source="modxo",
                card_probe_js=(
                    "!!document.querySelector('#cardNumber, [data-testid*=\"cardNumber\" i], "
                    "input[name=\"cardNumber\"]')"
                ),
            ):
                log.error(f"  modxo 30s 未进入卡填写页, url={self.page.url[:120]}")
                return {"status": "modxo_no_card_step", "url": self.page.url}
            time.sleep(3)
            is_guest_signup = self._eval(
                "!!document.getElementById('cardNumber') && !!document.getElementById('billingLine1')"
            )

        if not is_guest_signup:
            # 看是不是 /pay 登录页
            on_login_page = self._eval(
                "(function(){var p=location.pathname;"
                "if(/^\\/pay\\/?$/i.test(p))return true;"
                "if(/^\\/signin/.test(p))return true;"
                # 同 page_kind：必须 email 和 password 都可见
                "var visible=function(el){return el && el.offsetParent!==null;};"
                "var em=document.querySelector('input[type=\"email\"], input[name=\"login_email\"]');"
                "var pw=document.querySelector('input[type=\"password\"]');"
                "return visible(em) && visible(pw);})()"
            )
            if on_login_page:
                log.info("[paypal] 在 PayPal 登录页 (/pay)，点 Create an Account 切到 guest 注册")
                clicked_create = self._eval(
                    "(function(){"
                    "var bs=document.querySelectorAll('button, a, [role=\"button\"]');"
                    "for(var i=0;i<bs.length;i++){var b=bs[i];"
                    "var t=(b.textContent||'').trim();"
                    "if(/^create\\s+(?:an\\s+)?account$|^创建.*帐户$|注册$|^アカウントを開設(?:する)?$|^アカウント作成$|^新規(?:登録|作成)$/i.test(t)){"
                    "b.scrollIntoView({block:'center'});b.click();return t;}}"
                    "return null;})()"
                )
                log.info(f"  click [Create an Account]: {clicked_create}")
                # 等转到下一步：可能是经典 /checkoutweb/signup，也可能是新版 modxo email 页
                landed = None
                post_create_slider_attempted = False
                for _ in range(20):
                    time.sleep(1)
                    cur = self.page.url or ""
                    if not post_create_slider_attempted:
                        slider_probe = self._probe_paypal_slider_challenge()
                        if slider_probe.get("present"):
                            post_create_slider_attempted = True
                            slider_result = self._handle_paypal_slider_challenge(max_attempts=3)
                            log.info(
                                f"  post-create slider: solved={slider_result.get('solved')} "
                                f"attempts={slider_result.get('attempts')}"
                            )
                            if slider_result.get("solved"):
                                continue
                    if "/checkoutweb/signup" in cur:
                        landed = "classic"
                        log.info(f"  ✓ 已跳到经典 guest 注册页: {cur[:80]}")
                        break
                    if "modxo" in cur or "vaulted_not_recurring" in cur or "Pay_With_Card" in cur:
                        # modxo 极简流的 "Create a PayPal account" 邮箱页
                        is_modxo_form = self._eval(
                            "(function(){"
                            "var t=document.body?document.body.innerText:'';"
                            "if(!/Create a PayPal account|Continue to Payment|PayPalアカウント(?:を作成|の開設)|お支払いに進む/i.test(t))return false;"
                            "return !!document.querySelector('input[type=\"email\"], input[name*=\"mail\" i], input[id*=\"mail\" i]');"
                            "})()"
                        )
                        if is_modxo_form:
                            landed = "modxo"
                            log.info(f"  ✓ 已跳到 modxo 极简注册页: {cur[:80]}")
                            break
                if not landed:
                    log.error(f"  10s 内没跳到 signup 页, url={self.page.url[:120]}")
                    return {"status": "create_account_no_redirect", "url": self.page.url}

                if landed == "modxo":
                    # 走 modxo 流：填 email → Continue to Payment → 等卡片表单出现
                    log.info("[paypal-modxo] 填 email + Continue to Payment")
                    typed = self._eval(
                        "(function(em){"
                        "var inp=document.querySelector('input[type=\"email\"], input[name*=\"mail\" i], input[id*=\"mail\" i]');"
                        "if(!inp)return false;"
                        "inp.focus();"
                        "var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                        "setter.call(inp,em);"
                        "inp.dispatchEvent(new Event('input',{bubbles:true}));"
                        "inp.dispatchEvent(new Event('change',{bubbles:true}));"
                        "inp.dispatchEvent(new Event('blur',{bubbles:true}));"
                        "return inp.value;})(" + json.dumps(target_email) + ")"
                    )
                    log.info(f"  modxo email 已填: {typed}")
                    time.sleep(2)
                    cont = self._eval(
                        "(function(){var bs=document.querySelectorAll('button');"
                        "for(var i=0;i<bs.length;i++){var b=bs[i];if(b.disabled)continue;"
                        "var t=(b.textContent||'').trim();"
                        "if(/Continue to Payment|^Continue$|^继续$|^次へ$|^続行$|^お支払いに進む$|^支払い(?:を|に)?続ける$/i.test(t)){b.scrollIntoView({block:'center'});b.click();return t;}}return null;})()"
                    )
                    log.info(f"  click [Continue to Payment]: {cont}")
                    # 等卡片表单
                    if not self._wait_for_paypal_card_transition(
                        45,
                        source="modxo Continue to Payment",
                        card_probe_js=(
                            "!!document.querySelector('#cardNumber') || "
                            "!!document.querySelector('input[name=\"cardNumber\"]') || "
                            "!!document.querySelector('[data-testid*=\"cardNumber\" i]')"
                        ),
                    ):
                        log.error(f"  modxo 45s 未出现卡片表单, url={self.page.url[:120]}")
                        return {"status": "modxo_no_card_step", "url": self.page.url}

                # 等页面 hydrate（modxo 跳过来的 signup 页 hydrate 比较慢，多等一会儿）
                hydrate_started = time.time()
                while time.time() - hydrate_started < 45:
                    is_guest_signup = self._eval(_GUEST_CARD_DETECT)
                    has_address = self._eval(
                        "(function(){"
                        "  var sels = ["
                        "    '#billingLine1',"
                        "    'input[id*=\"billingLine1\" i]',"
                        "    'input[id*=\"billingAddressLine1\" i]',"
                        "    'input[name*=\"billingLine1\" i]',"
                        "    'input[name*=\"address\" i]',"
                        "    'input[autocomplete=\"address-line1\" i]',"
                        "    'input[autocomplete=\"street-address\" i]',"
                        "    'input[data-testid*=\"address-line1\" i]',"
                        "    'input[aria-label*=\"address line 1\" i]',"
                        "    'input[aria-label*=\"番地\"]'"
                        "  ];"
                        "  for (var i = 0; i < sels.length; i++) {"
                        "    if (document.querySelector(sels[i])) return true;"
                        "  }"
                        "  return false;"
                        "})()"
                    )
                    if is_guest_signup and has_address:
                        log.info("  ✓ guest 表单 hydrate 完成")
                        break
                    time.sleep(2)

        if not is_guest_signup:
            # 失败前 dump 一份页面状态，方便适配手机版 selector
            try:
                dump = self._eval(
                    "(function(){"
                    "  var inputs = Array.from(document.querySelectorAll('input,select,textarea'));"
                    "  var info = inputs.slice(0, 40).map(function(el){"
                    "    return {"
                    "      tag: el.tagName,"
                    "      id: el.id || null,"
                    "      name: el.getAttribute('name') || null,"
                    "      type: el.getAttribute('type') || null,"
                    "      autocomplete: el.getAttribute('autocomplete') || null,"
                    "      inputmode: el.getAttribute('inputmode') || null,"
                    "      datatestid: el.getAttribute('data-testid') || null,"
                    "      arialabel: el.getAttribute('aria-label') || null,"
                    "      placeholder: el.getAttribute('placeholder') || null,"
                    "      visible: el.offsetParent !== null"
                    "    };"
                    "  });"
                    "  return JSON.stringify({url: location.href, count: inputs.length, fields: info});"
                    "})()"
                )
                log.warning(f"  [paypal] 仍不在 guest checkout 页, DOM dump: {str(dump)[:1500]}")
            except Exception as _e:
                log.debug(f"  [paypal] DOM dump 失败: {_e}")
            log.warning(f"  仍不在 guest checkout 页，url={self.page.url[:120]}")
            return {"status": "not_guest_checkout", "url": self.page.url}

        # 填表
        fill_results = self._fill_paypal_guest_form(
            card=c,
            addr=addr,
            paypal_email=target_email,
            paypal_password=paypal_password,
        )
        if paypal_country == "GB":
            tax_result = str(fill_results.get("taxResidency") or "")
            required_ok = bool(fill_results.get("dob")) and (
                tax_result.startswith("selected_") or tax_result == "not_present"
            )
            if not required_ok:
                log.error(
                    "[paypal] GB 必填资料仍未完成: "
                    f"dateOfBirth={fill_results.get('dob')!r}, taxResidency={tax_result!r}"
                )
                return {
                    "status": "gb_profile_fields_incomplete",
                    "url": self.page.url,
                    "fields": fill_results,
                }

        # 点 "Agree & Create Account"
        log.info("[paypal] 提交 (Agree & Create Account)")
        clicked = self._eval(
            "(function(){var bs=document.querySelectorAll('button');"
            "for(var i=0;i<bs.length;i++){var b=bs[i];if(b.disabled)continue;"
            "var t=(b.textContent||'').trim();"
            "if(/Agree.*Create Account|Create Account|Concordar.*Criar|Criar conta|Continuar|Pagar|Assinar|Enviar|Pr[oó]ximo|同意.*创建|同意して(?:続行|続ける|続く|アカウント(?:を作成|の開設)|開設する)|同意して.*続|アカウントを作成して続ける/i.test(t)){"
            "b.scrollIntoView({block:'center'});b.click();return t.slice(0,40);}}"
            "var card=document.querySelector('#cardNumber,input[name=\"cardNumber\"],input[autocomplete=\"cc-number\"]');"
            "var form=card&&card.closest('form');var submit=form&&form.querySelector('button[type=\"submit\"],input[type=\"submit\"]');"
            "if(submit&&!submit.disabled){submit.scrollIntoView({block:'center'});submit.click();return 'form_submit';}"
            "return null;})()"
        )
        log.info(f"  click: {clicked}")
        time.sleep(5)

        # Slider detection runs in the result loop; reCAPTCHA/hCaptcha can still
        # be completed by the installed extension in parallel.
        log.info("[paypal] 已提交，进入结果轮询（含 PayPal slider 检测）")
        time.sleep(3)

        # 等结果
        deadline = time.time() + 180
        _last_token_reclick = 0.0
        _last_slider_attempt = 0.0
        while time.time() < deadline:
            url = self.page.url or ""
            host = url.split("//", 1)[-1].split("/", 1)[0].lower()
            url_lower = url.lower()

            # 成功条件（严格）：必须看到 redirect_status=succeeded
            # 或 URL 跳到 chatgpt/openai 域 + 没有 redirect_status=failed/canceled 标记
            if "redirect_status=succeeded" in url_lower:
                log.info(f"  ✓ PayPal 支付成功 (redirect_status=succeeded): {url[:120]}")
                return {"status": "success", "url": url}

            if "paypal.com" in host and time.time() - _last_slider_attempt > 15:
                slider_probe = self._probe_paypal_slider_challenge()
                if slider_probe.get("present"):
                    _last_slider_attempt = time.time()
                    slider_result = self._handle_paypal_slider_challenge(max_attempts=3)
                    log.info(
                        f"  result-loop slider: solved={slider_result.get('solved')} "
                        f"attempts={slider_result.get('attempts')}"
                    )
                    if slider_result.get("solved"):
                        time.sleep(1.0)
                        continue

            # 人机被扩展解了（textarea 有 token）但还停在 paypal 签约页 → 再点一次 Agree 提交。
            # 每次最多 8s 点一次，避免狂点。
            if ("paypal.com" in host) and (time.time() - _last_token_reclick > 8):
                token_ready = self._eval(
                    "(function(){var t=document.querySelector('textarea[name=\"g-recaptcha-response\"], #g-recaptcha-response');"
                    "return !!(t && t.value && t.value.length > 50);})()"
                )
                if token_ready:
                    log.info("  ✓ 检测到 reCAPTCHA token，已注入，再点一次 Agree 提交")
                    self._eval(
                        "(function(){var bs=document.querySelectorAll('button');"
                        "for(var i=0;i<bs.length;i++){var b=bs[i];if(b.disabled)continue;"
                        "var t=(b.textContent||'').trim();"
                        "if(/Agree.*Create Account|Create Account|同意.*创建|同意して(?:続行|続ける|続く|アカウント(?:を作成|の開設)|開設する)|同意して.*続|アカウントを作成して続ける/i.test(t)){b.scrollIntoView({block:'center'});b.click();return t;}}return null;})()"
                    )
                    _last_token_reclick = time.time()
                    time.sleep(3)

            on_target_host = ("chatgpt.com" in host or "openai.com" in host)
            has_failure_marker = (
                "redirect_status=failed" in url_lower
                or "redirect_status=canceled" in url_lower
                or "redirect_status=cancelled" in url_lower
            )
            # ★★ 用户要求暂时注释掉这个早退失败：跳回 chatgpt 即使带 failed 标记也继续轮询
            # if on_target_host and has_failure_marker:
            #     log.error(f"  ✗ PayPal 跳回但带失败标记: {url[:120]}")
            #     return {"status": "paypal_canceled_or_failed", "url": url}
            if on_target_host and has_failure_marker:
                log.warning(f"  ⚠ PayPal 跳回带失败标记（暂忽略，继续等）: {url[:120]}")

            # ★★★ PayPal「支払方法を登録」/ "Add a payment method" 弹窗
            # OTP 验证完后 PayPal 偶尔会插这个弹窗让你额外注册信用卡/银行账号。
            # 这跟当前订阅扣款无关，点右上角 ❌ 关闭即可（叉掉后 PayPal 会自己跳到下一步）。
            #
            # 检测下面这些 URL（任一命中都点 ❌）：
            #   1. paypal.com/myaccount/money/flow/accounts/new?flow=...            （新路径，本次实测）
            #   2. paypal.com/myaccount/money/funding/* / addcard / linkbank        （兜底）
            #
            # ★★ 注意：billingwithoutpurchase 不在这里！它不是要叉的弹窗，是叉完之后
            # PayPal 跳转到的「最终同意页」，需要点「同意して続行」按钮（见下方分支）。
            popup_keywords = (
                "/myaccount/money/flow/accounts/new",
                "/myaccount/money/flow/accounts",
                "/myaccount/money/funding",
                "/myaccount/wallet",
            )
            if any(k in url_lower for k in popup_keywords):
                log.info(f"  [paypal] 检测到「注册支付方法」弹窗页 ({url[:80]}...)，点右上角 ❌ 关闭")
                closed = self._eval(
                    "(function(){"
                    "  function vis(el){return el && el.offsetParent!==null;}"
                    "  function clickIt(el){"
                    "    try{el.scrollIntoView({block:'center'});}catch(e){}"
                    "    try{el.click();return 'clicked:'+(el.tagName||'')+':'+(el.getAttribute('aria-label')||el.getAttribute('data-testid')||el.textContent||'').slice(0,40);}"
                    "    catch(e){return 'click_err:'+e.message;}"
                    "  }"
                    # 1. aria-label / data-testid 匹配 close / 閉じる / Cancel
                    "  var labels=['close','cancel','閉じる','閉じ','取消','キャンセル','關閉','关闭','dismiss'];"
                    "  var btns=Array.from(document.querySelectorAll("
                    "    'button,a,[role=\"button\"],[data-testid*=\"close\" i],[data-test*=\"close\" i],[aria-label]'"
                    "  ));"
                    "  for(var i=0;i<btns.length;i++){"
                    "    var b=btns[i];if(!vis(b))continue;"
                    "    var al=String(b.getAttribute('aria-label')||'').toLowerCase();"
                    "    var dt=String(b.getAttribute('data-testid')||'').toLowerCase();"
                    "    var ti=String(b.getAttribute('title')||'').toLowerCase();"
                    "    var hit=false;"
                    "    for(var j=0;j<labels.length;j++){"
                    "      if(al.indexOf(labels[j])>=0||dt.indexOf(labels[j])>=0||ti.indexOf(labels[j])>=0){hit=true;break;}"
                    "    }"
                    "    if(hit) return clickIt(b);"
                    "  }"
                    # 2. 按文本（svg 内 X 图标常带 aria-label='close'，否则按按钮文本本身）
                    "  for(var i=0;i<btns.length;i++){"
                    "    var b=btns[i];if(!vis(b))continue;"
                    "    var t=(b.textContent||'').trim().toLowerCase();"
                    "    if(t==='×'||t==='✕'||t==='✖'||t==='x'){return clickIt(b);}"
                    "  }"
                    # 3. CSS class 命中（PayPal 的 close 按钮通常有 'close' 关键字）
                    "  var cls=document.querySelector('button[class*=\"close\" i],[role=\"button\"][class*=\"close\" i]');"
                    "  if(vis(cls)) return clickIt(cls);"
                    # 4. SVG icon close（手机版常见，按钮里只有 svg 没文字）
                    "  var svgs=document.querySelectorAll('svg[aria-label*=\"close\" i],svg[data-testid*=\"close\" i]');"
                    "  for(var i=0;i<svgs.length;i++){"
                    "    var s=svgs[i];var p=s.closest('button,a,[role=\"button\"]');"
                    "    if(p && vis(p)) return clickIt(p);"
                    "  }"
                    # 5. 兜底：按几何位置找右上角的圆形按钮（页头 200px 内 + 离右边 100px 内）
                    "  var topRight=null;var bestScore=0;"
                    "  for(var i=0;i<btns.length;i++){"
                    "    var b=btns[i];if(!vis(b))continue;"
                    "    var r=b.getBoundingClientRect();"
                    "    if(r.top<200 && r.right>window.innerWidth-120 && r.width<60 && r.height<60){"
                    "      var score=r.right - r.top;"  # 越靠右上角分数越高
                    "      if(score>bestScore){bestScore=score;topRight=b;}"
                    "    }"
                    "  }"
                    "  if(topRight) return clickIt(topRight);"
                    "  return 'no_close_btn_found';"
                    "})()"
                )
                log.info(f"  [paypal] close 弹窗结果: {closed}")
                time.sleep(2.5)
                # 关闭后等 URL 跳转走（任一 popup_keywords 都不在了就算关上了）
                for _ in range(10):
                    nu = (self.page.url or "").lower()
                    if not any(k in nu for k in popup_keywords):
                        log.info(f"  [paypal] 弹窗已关闭，URL 跳到 {self.page.url[:120]}")
                        break
                    time.sleep(1)
                continue  # 继续主轮询

            # ★★★ PayPal「最终同意页」/ Agree-and-Continue 页
            # 叉掉「注册支付方法」弹窗后，PayPal 会跳到这个页面：
            #   paypal.com/checkoutweb/billingwithoutpurchase?token=EC-xxxxxxxx
            # 这个页面有一个蓝色「同意して続行」/「Agree and Continue」大按钮，
            # 点击后才会真正完成支付，跳回 chatgpt /return 或 ?redirect_status=succeeded。
            agree_keywords = (
                "/checkoutweb/billingwithoutpurchase",
                "billingwithoutpurchase",
            )
            if any(k in url_lower for k in agree_keywords):
                log.info(f"  [paypal] 检测到「最终同意页」({url[:80]}...)，点「同意して続行」")
                # 等表单 hydrate 一下（按钮可能延迟挂上）
                time.sleep(1.5)
                clicked = self._eval(
                    "(function(){"
                    "  function vis(el){return el && el.offsetParent!==null;}"
                    "  var bs=Array.from(document.querySelectorAll("
                    "    'button,a,[role=\"button\"],input[type=\"submit\"]'"
                    "  ));"
                    # 1. 文本匹配：日 / 中 / 英全部覆盖
                    "  var rx=/同意して(?:続行|続ける|続く|支払う)|同意し続行|"
                    "Agree\\s*(?:and\\s*Continue|&\\s*Continue|to\\s*Continue|Continue)|"
                    "Agree\\s*and\\s*Pay|Continue\\s*and\\s*Pay|"
                    "同意并继续|同意并支付|同意继续|"
                    "Pay\\s*now|Pay\\s*Now/i;"
                    "  for(var i=0;i<bs.length;i++){"
                    "    var b=bs[i];if(b.disabled)continue;if(!vis(b))continue;"
                    "    var t=(b.textContent||b.value||'').trim();"
                    "    if(t && rx.test(t)){"
                    "      try{b.scrollIntoView({block:'center'});}catch(e){}"
                    "      try{b.click();return 'click_text:'+t.slice(0,40);}"
                    "      catch(e){return 'err_text:'+e.message;}"
                    "    }"
                    "  }"
                    # 2. data-testid / id 匹配 agree / continue / submit
                    "  var c=document.querySelector("
                    "    '[data-testid*=\"agree\" i],[data-testid*=\"continue\" i],"
                    "    [data-testid*=\"submit\" i],[data-testid*=\"confirmButton\" i],"
                    "    button[id*=\"agree\" i],button[id*=\"continue\" i],"
                    "    button[id*=\"submit\" i],button[id*=\"confirmButton\" i]'"
                    "  );"
                    "  if(c && vis(c) && !c.disabled){"
                    "    try{c.scrollIntoView({block:'center'});}catch(e){}"
                    "    try{c.click();return 'click_attr:'+(c.getAttribute('data-testid')||c.id||'').slice(0,40);}"
                    "    catch(e){return 'err_attr:'+e.message;}"
                    "  }"
                    # 3. 兜底：找页面上最大的蓝色 button（PayPal 主按钮风格）
                    "  var biggest=null;var bestArea=0;"
                    "  for(var i=0;i<bs.length;i++){"
                    "    var b=bs[i];if(b.disabled)continue;if(!vis(b))continue;"
                    "    var r=b.getBoundingClientRect();"
                    "    if(r.width>200 && r.height>30 && r.top>200){"
                    "      var a=r.width*r.height;"
                    "      if(a>bestArea){bestArea=a;biggest=b;}"
                    "    }"
                    "  }"
                    "  if(biggest){"
                    "    try{biggest.scrollIntoView({block:'center'});}catch(e){}"
                    "    try{biggest.click();return 'click_biggest:'+(biggest.textContent||'').trim().slice(0,40);}"
                    "    catch(e){return 'err_biggest:'+e.message;}"
                    "  }"
                    "  return 'no_agree_btn';"
                    "})()"
                )
                log.info(f"  [paypal] 点同意按钮结果: {clicked}")
                time.sleep(3)
                # 等跳转走（不在 billingwithoutpurchase 了就算成功）
                for _ in range(15):
                    nu = (self.page.url or "").lower()
                    if not any(k in nu for k in agree_keywords):
                        log.info(f"  [paypal] 同意完成，URL 跳到 {self.page.url[:120]}")
                        break
                    time.sleep(1)
                continue  # 继续主轮询，等待 chatgpt /return

            # PayPal genericError 页（账号被限制/风控拦截）
            # URL 形如 paypal.com/checkoutweb/genericError?code=UkVTVFJJQ1RFRF9VU0VS (RESTRICTED_USER)
            # ★★ 用户要求暂时注释掉这个早退封控，让流程继续走（可能 OTP 后会自己跳走 / 还能挽回）
            #     如果要恢复封控早退，把下面 if 块取消注释即可
            # if "genericerror" in url_lower or "/genericError" in url:
            #     log.error(f"  ✗ PayPal genericError（账号被限制/风控拦截）: {url[:120]}")
            #     return {"status": "paypal_restricted", "url": url}
            if "genericerror" in url_lower or "/genericError" in url:
                # 仅打印警告，不退出（让主轮询继续等其他状态变化）
                log.warning(f"  ⚠ PayPal genericError 页（暂忽略，继续等流程跳转）: {url[:120]}")

            if on_target_host and "/return" in url:
                # /return 路径无 redirect_status 也算成功（PayPal 经典签约流）
                log.info(f"  ✓ PayPal /return 跳回: {url[:120]}")
                return {"status": "success", "url": url}

            # 错误检测：看页面有没有错误文案
            err = self._eval(
                "(function(){var t=(document.body?document.body.innerText:'').toLowerCase();"
                "if(/we weren't able to add this card|无法添加此卡|try a different card|添加此卡失败/i.test(t)){"
                "return 'card_declined';}"
                "if(/this card has already been added to another paypal account/i.test(t)){"
                "return 'card_already_used';}"
                "if(/this is required/i.test(t)){return 'fields_missing';}"
                "return null;})()"
            )
            if err == "card_declined":
                # ★★ 用户要求暂时注释掉早退：卡被拒也继续等（也许 PayPal 自己会重试）
                log.warning("  ⚠ 卡被拒提示（暂忽略，继续等流程跳转）")
                # log.error("  卡被拒")
                # return {"status": "card_declined", "url": self.page.url}
            if err == "card_already_used":
                log.warning("  ⚠ 这张卡已经绑过 PayPal 账号（暂忽略，继续等）")
                # log.error("  这张卡已经绑过 PayPal 账号")
                # return {"status": "card_already_used", "url": self.page.url}
            if err == "fields_missing":
                # 跟踪首次出现 fields_missing 的时间，30s 内修不好就放弃
                if not hasattr(self, '_fields_missing_since'):
                    self._fields_missing_since = time.time()
                    log.warning("  仍有必填字段为空，尝试重填必填资料 + 重提交")
                    # 检查所有必填字段，把空的字段补回去
                    expected = {
                        "email": target_email,
                        "password": paypal_password,
                        "billingPostalCode": (addr or {}).get("zip", ""),
                        "billingLine1": (addr or {}).get("street", ""),
                        "billingCity": (addr or {}).get("city", ""),
                    }
                    if paypal_country == "GB" and gb_identity:
                        expected["dateOfBirth"] = gb_identity.get("date_of_birth_dmy", "")
                    for fid, fval in expected.items():
                        if not fval:
                            continue
                        cur = self._eval(
                            f"(document.getElementById({json.dumps(fid)}) || {{}}).value || ''"
                        )
                        if cur == fval:
                            continue
                        log.warning(f"  补填 {fid}: 当前={cur[:20]!r} 目标={fval[:20]!r}")
                        self._eval(
                            f"(function(){{var id={json.dumps(fid)};"
                            f"var p=document.getElementById(id)||document.getElementById('__pp_'+id);"
                            f"if(!p&&id==='dateOfBirth')p=document.querySelector('input[name*=\"dateOfBirth\" i],input[autocomplete=\"bday\"]');"
                            f"if(!p)return false;p.focus();"
                            f"var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
                            f"s.call(p,{json.dumps(fval)});"
                            f"p.dispatchEvent(new Event('input',{{bubbles:true}}));"
                            f"p.dispatchEvent(new Event('change',{{bubbles:true}}));"
                            f"p.dispatchEvent(new Event('blur',{{bubbles:true}}));"
                            f"return p.value;}})()"
                        )
                        time.sleep(0.4)
                    if paypal_country == "GB":
                        self._select_paypal_tax_residency("GB", "United Kingdom")
                    time.sleep(1)
                    # 再点 Agree
                    self._eval(
                        "(function(){var bs=document.querySelectorAll('button');"
                        "for(var i=0;i<bs.length;i++){var b=bs[i];if(b.disabled)continue;"
                        "var t=(b.textContent||'').trim();"
                        "if(/Agree.*Create Account|Create Account|同意して(?:続行|続ける|続く|アカウント(?:を作成|の開設)|開設する)|同意して.*続|アカウントを作成して続ける/i.test(t)){b.click();return t;}}return null;})()"
                    )
                    time.sleep(3)
                elif time.time() - self._fields_missing_since > 30:
                    log.warning(f"  ⚠ 必填字段空了 {int(time.time() - self._fields_missing_since)}s（暂忽略，继续等）")
                    self._fields_missing_since = None
                    # ★★ 用户要求暂时注释掉早退：fields missing 30s 不解决也继续等
                    # log.error(f"  必填字段空了 {int(time.time() - self._fields_missing_since)}s，放弃")
                    # return {"status": "fields_missing_timeout", "url": self.page.url}
            else:
                # 错误消失了，重置计时
                if hasattr(self, '_fields_missing_since'):
                    self._fields_missing_since = None

            # SMS OTP / approval / review
            self._inject_js("paypal.js")
            stage = self._get_paypal_stage()
            if stage == "verification":
                log.info("  PayPal SMS 验证")
                # 优先用 self._sms_provider + activation（smsbower 等），没有就回退 62us URL
                provider = getattr(self, "_sms_provider", None)
                activation = getattr(self, "_sms_activation", None)
                if provider is not None and activation is not None:
                    log.info(f"  用 SMS provider={provider.name} activation_id={activation.get('id')}")
                    code = fetch_sms_otp("", deadline_s=180,
                                         provider=provider, activation=activation)
                else:
                    sms_url = c.get("sms_url") or config.PAYPAL_SMS_URL
                    if not sms_url:
                        return {"status": "paypal_otp_missing_config"}
                    log.info(f"  用 SMS URL: {sms_url[:60]}...")
                    code = fetch_sms_otp(sms_url, deadline_s=120)
                if not code:
                    # smsbower 拿不到码就 cancel 把号还回去
                    if provider is not None and activation is not None:
                        try:
                            provider.cancel(activation)
                        except Exception:
                            pass
                    return {"status": "paypal_otp_timeout"}
                # smsbower 拿到码后立即 setStatus(6) 完成
                if provider is not None and activation is not None:
                    try:
                        provider.complete(activation)
                    except Exception:
                        pass
                    # 标记已结算，避免函数 return 时再 cancel 一次
                    self._sms_activation = None
                self._eval(f"__gpt_paypal_fillVerification({json.dumps(code)})")
                time.sleep(3)
                continue
            elif stage == "review":
                self._eval("__gpt_paypal_consent()")
                time.sleep(3)
                continue
            elif stage == "approval":
                self._eval("__gpt_paypal_approve()")
                time.sleep(3)
                continue

            time.sleep(2)

        # 主循环退出（loop 跑完）：如果 smsbower activation 还没结算，cancel 还号
        try:
            act = getattr(self, "_sms_activation", None)
            prv = getattr(self, "_sms_provider", None)
            if act is not None and prv is not None:
                log.info(f"  [sms] pay_paypal 退出但 activation 未结算，cancel(id={act.get('id')})")
                prv.cancel(act)
                self._sms_activation = None
        except Exception as _e:
            log.debug(f"  [sms] cancel on exit err: {_e}")

        return {"status": "paypal_pending", "url": self.page.url,
                "stage": self._get_paypal_stage()}

    # ============ 绑定手机号（add-phone） ============

    def _get_phone_bind_state(self) -> str:
        self._ensure_js("typeof __gpt_pb_state === 'function'", "phone_bind.js")
        return self._eval("__gpt_pb_state()") or "unknown"

    def bind_phone_to_account(self, email: str, password: str,
                              phone: str, sms_api: str = "",
                              *,
                              outlook_creds: dict | None = None,
                              log_fn=None,
                              max_otp_attempts: int = 3,
                              otp_deadline_s: int = 180,
                              sms_provider=None,
                              sms_activation: dict | None = None) -> dict:
        """给已存在的 ChatGPT 账号绑定手机号 + 同时拿 Codex refresh_token。

        ★ 不再先在 chatgpt.com 登录 ★ 直接走 codex_oauth 单链路：
            navigate auth_url
              → email_entry 输入邮箱
              → verification_page 邮箱 OTP（用 outlook_creds 抓码）
              → add_phone（填手机 → SMS OTP → submit）
              → consent
              → callback?code=...
              → POST /oauth/token 拿 access_token + refresh_token

        Args:
            outlook_creds: outlook 邮箱凭据，必填
                shape: {"email":..., "refresh_token":..., "client_id":..., "password":...}

        Returns:
            { ok: bool, phone, error?, access_token?, refresh_token?, id_token?, stage }
        """
        say = log_fn or (lambda _m: None)
        say(f"[bind] 开始: {email} -> {phone}")

        try:
            from codex_oauth import run_codex_oauth_on_page
        except Exception as exc:
            return {"ok": False, "phone": phone,
                    "error": f"codex_oauth_import_failed: {exc}", "stage": "import"}

        oauth_result = run_codex_oauth_on_page(
            self,
            email=email,
            log_fn=say,
            phone=phone,
            sms_api=sms_api or "",
            sms_provider=sms_provider,
            sms_activation=sms_activation,
            max_otp_attempts=max_otp_attempts,
            otp_deadline_s=otp_deadline_s,
            outlook_creds=outlook_creds,
        )
        if not oauth_result or not oauth_result.get("access_token"):
            err = (oauth_result or {}).get("error") or "oauth_failed_no_token"
            return {
                "ok": False, "phone": phone,
                "error": err,
                "stage": (oauth_result or {}).get("stage") or "oauth",
            }

        say(f"[bind] ✓ OAuth 完成；access_token={len(oauth_result.get('access_token',''))} "
            f"refresh_token={len(oauth_result.get('refresh_token',''))}")
        return {
            "ok": True,
            "phone": phone,
            "stage": "done",
            "access_token": oauth_result.get("access_token") or "",
            "refresh_token": oauth_result.get("refresh_token") or "",
            "id_token": oauth_result.get("id_token") or "",
            "chatgpt_account_id": oauth_result.get("account_id") or "",
        }

    def _finalize_phone_bind(self, say, *, phone: str = "") -> dict:
        """[Deprecated] 老的"先绑号，跳到 chatgpt.com，再单独跑 OAuth"流程。

        现在 bind_phone_to_account 直接走 OAuth 单链路，OAuth 内部处理 add_phone
        会同时拿到 token 和绑定状态。这个方法保留只是为了向后兼容外部调用。
        """
        token = self._try_get_access_token()
        return {"ok": True, "phone": phone, "access_token": token, "stage": "done"}

    # ============ 全链路 ============

    def full_run(self, email: str, password: str,
                 outlook_creds: dict = None, card_config: dict = None,
                 paypal_account: dict = None,
                 mode: str = "register",
                 payment_method: str = "paypal",
                 stop_after: str = "") -> dict:
        """完整流程。
        payment_method:
            "paypal"        hosted PayPal 长链 (pay.openai.com)
            "paypal_custom" 美区 custom UI (chatgpt.com/checkout/openai_ie/cs_live_xxx 短链)
            "gopay"         印尼 custom UI (chatgpt.com/checkout/openai_llc/cs_live_xxx 短链)
        stop_after:
            "register"  注册成功就返回（不进入支付）
            "checkout"  拿到 checkout 长链/短链就返回
            "hosted"    custom/hosted checkout 提交完就返回（不跑 paypal 端登录授权）
            ""          一直跑到 paypal
        """
        if mode == "login":
            reg = self.login(email, password, outlook_creds=outlook_creds)
        else:
            reg = self.signup(email, password, outlook_creds)

        if reg.get("status") != "success":
            return reg

        if stop_after == "register":
            return reg

        chk = self.checkout(access_token=reg.get("access_token"),
                            payment_method=payment_method)
        if chk.get("status") != "ready":
            return {**reg, "checkout_status": chk.get("status"),
                    "checkout_error": chk.get("error")}

        if stop_after == "checkout":
            return {**reg,
                    "checkout_url": chk.get("url"),
                    "chatgpt_checkout_url": chk.get("chatgpt_checkout_url", ""),
                    "hosted_checkout_url": chk.get("hosted_checkout_url", ""),
                    "stripe_short_url": chk.get("stripe_short_url", ""),
                    "stripe_long_url": chk.get("stripe_long_url", ""),
                    "preferred_url": chk.get("preferred_url", ""),
                    "session_id": chk.get("session_id", ""),
                    "processor_entity": chk.get("processor_entity", ""),
                    "payment_method": chk.get("payment_method", ""),
                    "checkout_status": "ready"}

        # 根据 payment_method 选不同的 checkout 自动化
        if payment_method == "paypal_custom":
            host_res = self.fill_custom_checkout_paypal(
                navigate_url=chk.get("preferred_url") or chk.get("url") or ""
            )
        else:
            host_res = self.fill_hosted_checkout_paypal()

        if host_res.get("status") not in ("submitted", "left_checkout"):
            return {**reg, "checkout_url": chk.get("url"),
                    "preferred_url": chk.get("preferred_url", ""),
                    "payment_method": chk.get("payment_method", ""),
                    "hosted_status": host_res.get("status")}

        if stop_after == "hosted":
            return {**reg, "checkout_url": chk.get("url"),
                    "preferred_url": chk.get("preferred_url", ""),
                    "payment_method": chk.get("payment_method", ""),
                    "hosted_status": host_res.get("status")}

        pay = self.pay_paypal(card_config, paypal_account)
        return {**reg,
                "checkout_url": chk.get("url"),
                "preferred_url": chk.get("preferred_url", ""),
                "payment_method": chk.get("payment_method", ""),
                "hosted_status": host_res.get("status"),
                "payment_status": pay.get("status"),
                "payment_url": pay.get("url"),
                }
