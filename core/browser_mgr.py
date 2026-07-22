"""
浏览器管理 - 支持 BitBrowser（比特指纹浏览器）和本地 Chromium。

参考 kiro-pro-batch/browser.py，改为我们项目的两套用途：
  1. 本地 Chromium（无痕模式）   → 跑注册 / 拿 access_token / 创建 checkout 长链
  2. BitBrowser（指纹隔离）      → 用长链跑支付，每个账号一个独立指纹+代理

两套浏览器**完全隔离**，不共享 cookie/profile/指纹，避免 OpenAI 把注册环境和支付环境关联起来风控。

用法:
    from browser_mgr import open_local_chromium, open_bitbrowser_with_url

    # 注册阶段
    page = open_local_chromium(window_index=0)
    pipeline = GPTPipeline(page)
    reg = pipeline.signup(...)
    chk = pipeline.checkout(payment_method='paypal')
    long_url = chk['preferred_url']

    # 支付阶段（换比特）
    pay_page, browser_id = open_bitbrowser_with_url(
        long_url, name=f'pay_{email}',
        proxy={'host': '...', 'port': 1080, 'user': '...', 'password': '...'},
    )
    # 在 pay_page 上做 PayPal 流程...
    close_bitbrowser(browser_id)
"""
from __future__ import annotations

import json
import logging
import platform
import shutil
import subprocess
import time
from pathlib import Path

import requests
from DrissionPage import ChromiumPage, ChromiumOptions

import config

log = logging.getLogger("browser_mgr")


# =============================================================================
# BitBrowser 指纹模板：手机环境 / 桌面环境
# 通过 config.BITBROWSER_FINGERPRINT_PROFILE 切换：
#   - "auto" / "" / "desktop" → 当前默认（空 fingerprint + randomFingerprint=True，
#                                  BB 内部完全随机，主要给桌面 UA）
#   - "iphone"                → iOS 17 Safari iPhone
#   - "android"               → Android 13/14 Chrome 手机
# =============================================================================

# iPhone Safari UA 池（iOS 17 主流版本）
_IOS_USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
]

# Android Chrome UA 池
_ANDROID_USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S918U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
]


def _build_iphone_fingerprint() -> dict:
    """BitBrowser iPhone 指纹模板（iOS 17 Safari）。

    AdsPower 同款配方：锁住 OS=iPhone，但 canvas/webGL/audio/字体 等次级指纹
    每次随机噪声（"1" 模式）。这样每次开 ephemeral profile 都是全新指纹，
    PayPal/Stripe Radar 看不到关联。
    """
    import random as _rnd
    return {
        "ostype": "Mobile",
        "os": "iPhone",
        "coreVersion": "126",  # WebKit-Chromium 内核（BB 桌面端模拟移动）
        "version": "126",
        "userAgent": _rnd.choice(_IOS_USER_AGENTS),
        "platformVersion": "17.0.0",
        "isIpCreateTimeZone": True,
        "isIpCreatePosition": True,
        "isIpCreateDisplayLanguage": True,
        "isIpCreateLanguage": True,
        # 移动设备分辨率（iPhone 14/15）
        "resolutionType": "1",
        "resolution": "390 x 844",
        "openWidth": 390,
        "openHeight": 844,
        "devicePixelRatio": 3,
        # ★★★ 次级指纹「噪声/随机」模式（"1" = 每次新值）：
        # - canvas/webGL/audio/字体/媒体设备 哈希都不一样 → Radar 没法关联多次开窗
        "webRTC": "3",          # 替换：用代理出口 IP 作为 WebRTC IP
        "fontType": "1",        # 1 = 噪声字体哈希
        "canvas": "1",          # 1 = canvas 噪声
        "webGL": "1",           # 1 = WebGL 噪声
        "webGLMeta": "1",       # 1 = WebGL 厂商/型号噪声
        "audioContext": "1",    # 1 = AudioContext 噪声
        "mediaDevice": "1",     # 1 = MediaDevices 列表噪声
        "speechVoices": "1",    # 1 = SpeechSynthesis 列表噪声
        "hardwareConcurrency": str(_rnd.choice([4, 6])),
        "deviceMemory": str(_rnd.choice([4, 6, 8])),
        "deviceInfoEnabled": True,
        "clientRectNoiseEnabled": True,
        "doNotTrack": "0",
        "colorDepth": 32,
        "navigatorVendor": "Apple Computer, Inc.",
        "coreProduct": "chrome",
        "windowSizeLimit": True,
    }


def _build_android_fingerprint() -> dict:
    """BitBrowser Android 指纹模板（Android 14 Chrome）。同样 canvas/webGL 走噪声模式。"""
    import random as _rnd
    return {
        "ostype": "Mobile",
        "os": "Android",
        "coreVersion": "126",
        "version": "126",
        "userAgent": _rnd.choice(_ANDROID_USER_AGENTS),
        "platformVersion": "14.0.0",
        "isIpCreateTimeZone": True,
        "isIpCreatePosition": True,
        "isIpCreateDisplayLanguage": True,
        "isIpCreateLanguage": True,
        # Android 旗舰常见分辨率（Pixel 8 / Galaxy S23）
        "resolutionType": "1",
        "resolution": "412 x 915",
        "openWidth": 412,
        "openHeight": 915,
        "devicePixelRatio": 2.625,
        # 次级指纹「噪声」
        "webRTC": "3",
        "fontType": "1",
        "canvas": "1",
        "webGL": "1",
        "webGLMeta": "1",
        "audioContext": "1",
        "mediaDevice": "1",
        "speechVoices": "1",
        "hardwareConcurrency": str(_rnd.choice([6, 8])),
        "deviceMemory": str(_rnd.choice([4, 6, 8])),
        "deviceInfoEnabled": True,
        "clientRectNoiseEnabled": True,
        "doNotTrack": "0",
        "colorDepth": 24,
        "navigatorVendor": "Google Inc.",
        "coreProduct": "chrome",
        "windowSizeLimit": True,
    }


def _resolve_fingerprint_profile() -> str:
    """读取 config.BITBROWSER_FINGERPRINT_PROFILE，规范化别名。"""
    raw = (getattr(config, "BITBROWSER_FINGERPRINT_PROFILE", "auto") or "auto").strip().lower()
    aliases = {
        "": "auto",
        "auto": "auto",
        "default": "auto",
        "desktop": "auto",
        "pc": "auto",
        "iphone": "iphone",
        "ios": "iphone",
        "ipad": "iphone",
        "mobile-ios": "iphone",
        "mobile_ios": "iphone",
        "android": "android",
        "mobile-android": "android",
        "mobile_android": "android",
        "phone": "iphone",  # 没指定则默认 iphone
        "mobile": "iphone",
    }
    return aliases.get(raw, "auto")


# =============================================================================
# 代理 sticky session 动态注入
# 711proxy 等代理商支持 user 段加 ``-session-{随机}-sessTime-5-sessAuto-1``
# 让本次会话黏在新出口 IP（5 分钟内不变），下一次创建窗口时又是新 IP。
# 这是 AdsPower 「换动态 SID」 同款效果。
# =============================================================================
import re as _re_proxy
import random as _rnd_proxy


def _inject_sticky_session(proxy_dict: dict, sess_time_min: int = 5) -> dict:
    """给 711proxy / cliproxy 等支持 sticky session 的代理 user 段注入随机 sid。

    其他代理（不支持的）原样返回。
    输入示例：{"user": "USER674021-zone-custom-region-JP", "host": "...rotgb...", ...}
    输出示例：{"user": "USER674021-zone-custom-region-JP-session-43271089-sessTime-5-sessAuto-1", ...}
    """
    if not isinstance(proxy_dict, dict):
        return proxy_dict
    out = dict(proxy_dict)
    user = str(out.get("user") or "").strip()
    host = str(out.get("host") or "").lower()
    if not user:
        return out
    # 只在 711proxy / 类似商支持的格式里注入
    if "711proxy" not in host and "rotgb" not in host:
        return out
    # 已经有 session-xxxxx 标记的不重复注入
    if _re_proxy.search(r"[-_]session[-_]", user):
        return out
    sid = "".join(_rnd_proxy.choices("0123456789", k=8))
    new_user = f"{user}-session-{sid}-sessTime-{sess_time_min}-sessAuto-1"
    out["user"] = new_user
    log.info(f"  [BitBrowser] 注入新 sticky sid={sid}（user 段）")
    return out



# ============================================================
# 本地 Chromium（无痕）— 注册 / 长链生成专用
# ============================================================

def open_local_chromium(window_index: int | None = None,
                       chrome_path: str = "",
                       user_data_dir: str = "",
                       proxy: str = "") -> ChromiumPage:
    """启动本地 Chromium（每次新建临时 profile，无痕）。

    window_index: 屏幕槽位（0-based，含 0）。None 时读 WORKER_INDEX env。
    返回 DrissionPage 的 ChromiumPage 对象。
    """
    co = ChromiumOptions()
    if not chrome_path:
        chrome_path = config.CHROME_PATH or _resolve_default_chrome_path()
    if chrome_path:
        co.set_browser_path(chrome_path)

    if user_data_dir:
        co.set_user_data_path(user_data_dir)
    else:
        co.auto_port()

    if proxy:
        co.set_proxy(proxy)
    elif config.PROXY_ENABLED and config.PROXY_HOST:
        scheme = "http"
        prefix = ""
        if config.PROXY_USER:
            prefix = f"{config.PROXY_USER}:{config.PROXY_PASS}@"
        co.set_proxy(f"{scheme}://{prefix}{config.PROXY_HOST}:{config.PROXY_PORT}")

    co.set_argument("--incognito")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--disable-infobars")
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")

    # 窗口尺寸 + 位置：3×2 网格（window_layout，含 slot=0）
    try:
        from window_layout import chromium_geom, worker_slot
        idx = worker_slot() if window_index is None else int(window_index)
        w, h, win_x, win_y = chromium_geom(idx)
    except Exception:
        # fallback: 老逻辑
        idx = int(window_index or 0)
        win_x = (idx % 5) * 520
        win_y = (idx // 5) * 480
        w, h = 1024, 768
    co.set_argument(f"--window-size={w},{h}")
    co.set_argument(f"--window-position={win_x},{win_y}")

    return ChromiumPage(co)


def _resolve_default_chrome_path() -> str:
    if platform.system() == "Darwin":
        for p in [
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]:
            if Path(p).exists():
                return p
    elif platform.system() == "Windows":
        import os
        for p in [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]:
            if Path(p).exists():
                return p
    return ""


# ============================================================
# BitBrowser API（比特指纹浏览器）
# ============================================================
# BitBrowser 默认监听 127.0.0.1:54345，REST API 文档:
# https://doc2.bitbrowser.cn/jiekou/ben-di-fu-wu-zhi-nan.html

class BitBrowserError(RuntimeError):
    pass


def _bb_post(path: str, payload: dict, timeout: int = 30) -> dict:
    base = getattr(config, "BITBROWSER_API", "http://127.0.0.1:54345").rstrip("/")
    # 强制不走系统代理，避免 clash 等本地代理把 127.0.0.1 也代理走
    resp = requests.post(f"{base}{path}", json=payload, timeout=timeout,
                         proxies={"http": None, "https": None})
    try:
        data = resp.json()
    except Exception:
        raise BitBrowserError(f"BitBrowser 返回非 JSON: {resp.text[:200]}")
    if not data.get("success"):
        raise BitBrowserError(f"BitBrowser API 失败: {data}")
    return data.get("data") or {}


def bb_health() -> bool:
    """检查 BitBrowser 客户端是否运行。
    BitBrowser 7.x 没有 /health 端点，用 /browser/list 探活。重试 3 次容错。"""
    base = getattr(config, "BITBROWSER_API", "http://127.0.0.1:54345").rstrip("/")
    last_err = None
    for _ in range(3):
        try:
            resp = requests.post(f"{base}/browser/list",
                                 json={"page": 0, "pageSize": 1},
                                 timeout=10,
                                 proxies={"http": None, "https": None})
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
                time.sleep(1)
                continue
            data = resp.json()
            if data.get("success"):
                return True
            last_err = data
            time.sleep(1)
        except Exception as e:
            last_err = e
            time.sleep(1)
    log.warning(f"  bb_health 失败: {last_err}")
    return False


def bb_list_windows(page: int = 0, page_size: int = 100) -> list[dict]:
    """列出所有已有的 BitBrowser 窗口。

    每个 dict 含: id / seq / name / remark / lastIp / lastCountry / host / port
                  proxyType / proxyUserName / status / closeTime
    """
    data = _bb_post("/browser/list", {"page": page, "pageSize": page_size})
    return data.get("list", [])


def bb_find_window(name: str = "", country: str = "",
                   ip_substring: str = "",
                   browser_id: str = "") -> dict | None:
    """按 name / country / IP 子串匹配一个已有窗口（取第一个命中）。

    匹配优先级: id > name > ip_substring > country
    全空时返回 None。
    """
    name = (name or "").strip()
    country = (country or "").strip()
    ip = (ip_substring or "").strip()
    bid = (browser_id or "").strip()
    if not (name or country or ip or bid):
        return None

    for w in bb_list_windows():
        if bid and w.get("id") == bid:
            return w
    if not (name or country or ip):
        return None
    for w in bb_list_windows():
        if name and name.lower() in (w.get("name") or "").lower():
            return w
        if name and name.lower() in (w.get("remark") or "").lower():
            return w
        if ip and ip in (w.get("lastIp") or ""):
            return w
        if country and country.lower() in (w.get("lastCountry") or "").lower():
            return w
    return None


def bb_summarize_windows() -> list[dict]:
    """简化版列窗（只挑核心字段，方便 CLI 输出）。"""
    items = []
    for w in bb_list_windows():
        items.append({
            "id": w.get("id"),
            "seq": w.get("seq"),
            "name": w.get("name") or "(无名)",
            "remark": w.get("remark", ""),
            "ip": w.get("lastIp", ""),
            "country": w.get("lastCountry", ""),
            "proxy": f"{w.get('proxyType', '')}://{w.get('host', '')}:{w.get('port', '')}",
            "status": w.get("status"),
            "close_time": w.get("closeTime", ""),
        })
    return items


def bb_get_window_detail(browser_id: str) -> dict:
    """读取 profile 详情（含代理 host/port/user/password）。"""
    return _bb_post("/browser/detail", {"id": browser_id}) or {}


def bb_extract_proxy_from(browser_id: str) -> dict | None:
    """从已有 profile 抽出代理配置 dict（可用于新建 profile）。"""
    try:
        d = bb_get_window_detail(browser_id)
    except Exception as e:
        log.warning(f"  [BitBrowser] 读 profile detail 失败: {e}")
        return None
    if not d or d.get("proxyMethod") not in (2, "2"):
        return None
    return {
        "proxyType": d.get("proxyType", "socks5"),
        "host": d.get("host", ""),
        "port": int(d.get("port", 0)),
        "user": d.get("proxyUserName", ""),
        "password": d.get("proxyPassword", ""),
    }


def bb_create_window(name: str,
                    proxy: dict | None = None,
                    group_id: str = "",
                    remark: str = "",
                    country: str = "") -> str:
    """在 BitBrowser 创建一个浏览器窗口（每次都新建，确保指纹独立）。

    proxy 字典:
        {"host": "...", "port": 1080, "user": "...", "password": "...",
         "proxyType": "socks5"}        # 默认 socks5，可改 http/https
    country: 给指纹一个国家提示（影响时区/语言）
    返回 browser_id（BitBrowser 内部 ID）。
    """
    payload = {
        "name": name,
        "remark": remark,
    }

    # ★★★ 指纹环境：根据 config.BITBROWSER_FINGERPRINT_PROFILE 选模板
    # - "auto"/"desktop" → 当前默认（空 + randomFingerprint=True，BB 全自动随机桌面）
    # - "iphone"         → iOS 17 iPhone Safari ⚠ 仅用于协议提链，不可用于 hosted 自动化
    # - "android"        → Android 14 Chrome 手机 ⚠ 同上
    profile = _resolve_fingerprint_profile()
    if profile == "iphone":
        payload["browserFingerPrint"] = _build_iphone_fingerprint()
        payload["randomFingerprint"] = False
        log.info(f"  [BitBrowser] 指纹环境: 📱 iPhone (iOS Safari)")
    elif profile == "android":
        payload["browserFingerPrint"] = _build_android_fingerprint()
        payload["randomFingerprint"] = False
        log.info(f"  [BitBrowser] 指纹环境: 📱 Android (Chrome)")
    else:
        # kiro-pro-batch 同款：空 fingerprint + randomFingerprint=True
        # 让 BitBrowser 完全自动随机所有指纹参数（UA/canvas/webGL/时区/语言等），
        # 内部算法保证各参数一致性（UA 和 OS 不会矛盾），比手动配更可靠。
        payload["browserFingerPrint"] = {}
        payload["randomFingerprint"] = True
        log.debug(f"  [BitBrowser] 指纹环境: 桌面随机（auto）")

    if country:
        payload["country"] = country
    if group_id:
        payload["groupId"] = group_id

    # kiro 风格：优先用传入的 proxy dict，否则直接读 config 顶层字段
    if proxy is None and getattr(config, "PROXY_HOST", ""):
        proxy = {
            "host": config.PROXY_HOST,
            "port": int(config.PROXY_PORT),
            "user": config.PROXY_USER,
            "password": config.PROXY_PASS,
            "proxyType": "socks5",
        }

    if proxy:
        # ★★★ 每次都给 711proxy / rotgb 注入新 sticky session id（动态 SID）
        # 让本次 ephemeral profile 拿到一个全新出口 IP，下次又是新的，
        # PayPal/Stripe Radar 看不到 IP 关联（AdsPower 同款效果）。
        proxy = _inject_sticky_session(proxy)
        ptype = proxy.get("proxyType", "socks5")
        payload.update({
            "proxyMethod": 2,        # 2 = 自定义代理
            "proxyType": ptype,
            "host": proxy["host"],
            "port": str(proxy["port"]),
        })
        if proxy.get("user"):
            payload["proxyUserName"] = proxy["user"]
            payload["proxyPassword"] = proxy.get("password", "")
    else:
        payload["proxyMethod"] = 0   # 0 = 不使用代理

    data = _bb_post("/browser/update", payload)
    return data["id"]


def bb_clear_cache(browser_id: str, except_extensions: bool = True) -> bool:
    """清掉指纹窗口的缓存 + cookies + localStorage，except_extensions=True 保留扩展。
    必须在窗口关闭状态下调用。返回 True 表示清成功。"""
    endpoint = "/cache/clear/exceptExtensions" if except_extensions else "/cache/clear"
    try:
        _bb_post(endpoint, {"ids": [browser_id]}, timeout=30)
        log.info(f"  [BitBrowser] 缓存已清 ({endpoint})")
        return True
    except Exception as e:
        log.warning(f"  [BitBrowser] 清缓存失败（不致命）: {e}")
        return False


def bb_clear_cookies(browser_id: str) -> bool:
    """彻底清 cookies — 多层清理：
      1. /browser/cookies/clear  saveSynced=False  （删除当前 profile cookie，不回写云端备份）
      2. /browser/update/partial  cookie="[]" url="" otherCookie=""
                                  clearCookiesBeforeLaunch=True syncCookies=False
         （彻底刷掉 profile 配置里"启动注入 cookie"字段 + 关掉云端同步）
    同时把 url（启动指定网址）清掉，避免每次开都自动跳到旧的 PayPal/Stripe URL。
    """
    ok = True
    # 1. /browser/cookies/clear — saveSynced=True：清当前 cookies 但保留云端同步关系，
    #    跟用户的"同步 Cookie"开关协作（用户开了同步=希望保持同步，我们只是清当前内容）
    try:
        _bb_post("/browser/cookies/clear",
                 {"browserId": browser_id, "saveSynced": True},
                 timeout=30)
        log.info(f"  [BitBrowser] cookies/clear API 已调 (saveSynced=True)")
    except Exception as e:
        log.debug(f"  cookies/clear 失败: {e}")
        ok = False
    # 2. profile 配置层全字段刷新：
    #    - cookie=""              清"启动注入 cookie"（用 "" 才符合 BitBrowser GUI 校验；
    #                              "[]" 虽然 API 接受但 GUI 会标红"格式错误"）
    #    - otherCookie=""         清额外 cookie
    #    - url=""                 清"启动指定网址"
    #    - clearCookiesBeforeLaunch=True  下次开窗时 BitBrowser 自动再清一遍
    #    注意：syncCookies 不动，保持用户原设置（关掉会让 BitBrowser GUI 红灯报错且
    #    用户失去云端 cookie 同步能力，跟"清干净"是两回事）。
    try:
        payload = {
            "ids": [browser_id],
            "cookie": "",
            "otherCookie": "",
            "url": "",
            "clearCookiesBeforeLaunch": True,
        }
        _bb_post("/browser/update/partial", payload, timeout=30)
        log.info(f"  [BitBrowser] profile 配置已刷新 (cookie='' / clearCookiesBeforeLaunch=true)")
    except Exception as e:
        log.warning(f"  [BitBrowser] update/partial 失败: {e}")
        ok = False
    return ok


def cleanup_all_profiles(profile_ids=None) -> int:
    """启动时一次性清理所有 BitBrowser profile 的 cookies + 缓存（保留扩展）。

    这是多并发 run_parallel 启动前同款逻辑，抽出来给 run_parallel 和 test_full 共用：
    跑之前把池里每个 profile 关窗 → bb_clear_cache → bb_clear_cookies。
    清理只在**开始**做一次，不再放进 open_bitbrowser_with_url 里每次开窗都清。

    profile_ids 不传时用 profile_pool.PROFILE_IDS。返回成功清理的 profile 数。
    """
    if profile_ids is None:
        try:
            from profile_pool import PROFILE_IDS as profile_ids
        except Exception as e:
            log.warning(f"  [cleanup] 读不到 PROFILE_IDS: {e}")
            return 0
    if not bb_health():
        log.warning("  [cleanup] BitBrowser 未运行，跳过启动清理")
        return 0
    log.info(f"启动前清理所有 BitBrowser profile 的 cookies/缓存（{len(profile_ids)} 个）...")
    cleaned = 0
    try:
        windows = {w["id"]: w for w in bb_list_windows()}
    except Exception as e:
        log.warning(f"  [cleanup] 列窗口失败: {e}")
        windows = {}
    for pid in profile_ids:
        w = windows.get(pid)
        if not w:
            continue
        try:
            if w.get("status") == 1:
                bb_close_window(pid)
                time.sleep(2)
            bb_clear_cache(pid, except_extensions=True)
            bb_clear_cookies(pid)
            cleaned += 1
            time.sleep(0.5)   # 避免 BitBrowser rate limit
        except Exception as e:
            log.warning(f"  [cleanup] 清理 profile {pid[:16]}... 失败（不致命）: {e}")
    log.info(f"已清理 {cleaned} 个 profile")
    return cleaned


def bb_open_window(browser_id: str,
                  window_index: int | None = None,
                  args: list[str] | None = None,
                  load_yescaptcha: bool = True,
                  incognito: bool = False,
                  window_mode: str = "headed") -> str:
    """打开 BitBrowser 窗口，返回 CDP 调试地址（http://127.0.0.1:port）。

    window_index: 屏幕槽位（0-based，含 0）。None 时读 WORKER_INDEX env。
    window_mode:
        "headed"   — 正常显示窗口（调试用）
        "hidden"   — 窗口移到屏幕外（-32000,-32000），仍真实渲染（推荐 PayPal，反爬通过率高）
        "headless" — --headless=new（性能最好，hCaptcha 容易识别）
    参考 aBaiAutoplus bitbrowser_hidden 模式。
    """
    try:
        from window_layout import bitbrowser_geom, worker_slot
        idx = worker_slot() if window_index is None else int(window_index)
        w, h, win_x, win_y = bitbrowser_geom(idx)
    except Exception:
        idx = int(window_index or 0)
        win_x = (idx % 4) * 480
        win_y = (idx // 4) * 460
        w, h = 1024, 768

    mode = (window_mode or "hidden").strip().lower()
    if mode == "headless":
        base_args = ["--headless=new", f"--window-size={w},{h}"]
        log.info("  [BitBrowser] headless 模式")
    elif mode == "hidden":
        # 窗口移到屏幕外但仍真实渲染 — aBaiAutoplus 推荐 PayPal 用这个
        base_args = [f"--window-size={w},{h}", "--window-position=-32000,-32000"]
        log.info("  [BitBrowser] hidden 模式（屏幕外真实渲染，推荐 PayPal）")
    else:
        base_args = [f"--window-size={w},{h}", f"--window-position={win_x},{win_y}"]
        log.info("  [BitBrowser] headed 模式（正常显示）")
    if load_yescaptcha:
        # YesCaptcha 已经通过 BitBrowser 扩展中心 / chrome://extensions 手动安装到 profile，
        # 不再用命令行 --load-extension（BitBrowser 屏蔽了这个参数）。
        # 这里只 log 一行作为提醒。
        log.info("  [BitBrowser] YesCaptcha 由 profile 持久化（手动装在 chrome://extensions）")
    if args:
        base_args.extend(args)

    data = _bb_post("/browser/open", {
        "id": browser_id,
        "args": base_args,
        "queue": True,
    }, timeout=120)   # ephemeral 新建 profile 首次启动 Chrome 需要更长时间
    debug_addr = data.get("http")
    if not debug_addr:
        raise BitBrowserError(f"BitBrowser open 没返回 http: {data}")
    return debug_addr


def bb_close_window(browser_id: str):
    try:
        _bb_post("/browser/close", {"id": browser_id}, timeout=15)
    except Exception as e:
        log.warning(f"BitBrowser close 失败（不致命）: {e}")


def bb_delete_window(browser_id: str):
    """关掉窗口后再调一次 delete 把 profile 也清掉。"""
    try:
        time.sleep(2)
        _bb_post("/browser/delete", {"id": browser_id}, timeout=15)
    except Exception as e:
        log.warning(f"BitBrowser delete 失败（不致命）: {e}")


# ============================================================
# 高层接口：拿到长链 → 用 BitBrowser 打开
# ============================================================

def open_bitbrowser_with_url(url: str,
                             name: str = "",
                             proxy: dict | None = None,
                             window_index: int | None = None,
                             goto: bool = True,
                             browser_id: str = "",
                             auto_close: bool = False) -> tuple[ChromiumPage, str]:
    """启动 BitBrowser 窗口 + 附加 DrissionPage + 打开 url。

    window_index: 屏幕槽位（0-based，含 0）。None 时读 WORKER_INDEX env。

    复用现有窗口（推荐，已配好代理）:
        page, bid = open_bitbrowser_with_url(url, browser_id="<已有窗口 id>")

    新建独立指纹窗口:
        page, bid = open_bitbrowser_with_url(url, name="...", proxy={...})

    返回 (page, browser_id)。
    """
    if not bb_health():
        raise BitBrowserError(
            "BitBrowser 未运行 / 未监听 127.0.0.1:54345。"
            "请先启动 BitBrowser 客户端，并在「设置 → 本地 API」开启 API 服务。"
        )

    if browser_id:
        # ============ 复用已有窗口模式（旧行为） ============
        windows = bb_list_windows()
        match = next((w for w in windows if w.get("id") == browser_id), None)
        if not match:
            raise BitBrowserError(
                f"BitBrowser 里找不到 id={browser_id} 的窗口。"
                f"\n现有窗口: {[w.get('id') for w in windows]}"
            )
        log.info(f"  [BitBrowser] 复用窗口 id={browser_id}"
                 f"  ip={match.get('lastIp')}  地区={match.get('lastCountry')}")
        bid = browser_id
        # 复用前先关掉窗口（让窗口干净地重开）。
        # 清缓存 + cookies 不在这里做——已统一移到「启动时」cleanup_all_profiles()
        # 一次性清理（跟多并发 run_parallel 启动前同款），避免每次开窗都清。
        if match.get("status") == 1:
            log.info("  [BitBrowser] 复用前先关掉窗口")
            bb_close_window(bid)
            time.sleep(3)
        log.info("  [BitBrowser] 跳过开窗清理（清理已在启动时统一做）")
        time.sleep(1)
        ephemeral = False
    else:
        # ============ Ephemeral 模式（推荐）============
        # 每次都新建一个全新指纹 profile（带代理），跑完删掉。
        # 这样：
        #   - 指纹完全随机（canvas/webGL/UA/字体/分辨率/字体哈希都换）
        #   - cookies/storage 全新（profile 还没启动过）
        #   - 代理 IP 重新拨号（如果是动态 IP 池）
        #   - 跑完 delete → 不留痕迹
        if not name:
            name = f"gpt_pay_{int(time.time())}"
        # ★★★ proxy 解析优先级：
        #   1. 调用方明确传入的 proxy
        #   2. config.BITBROWSER_PROXY（推荐，支持自动注入 sticky sid）
        #   3. config.PROXY_HOST / PROXY_USER（旧 schema）
        #   4. 都没有 → 裸连
        if proxy is None:
            try:
                import config as _cfg
                bp = getattr(_cfg, "BITBROWSER_PROXY", None)
                if isinstance(bp, dict) and bp.get("host"):
                    proxy = dict(bp)
            except Exception:
                pass
        # ★★★ country hint（影响指纹时区/语言）→ 必须跟代理出口一致，
        # 否则会出现 "时区日本 + IP 美国" 的不一致组合，PayPal Radar 容易识破。
        # 解析顺序：从 proxy.user 段里抽 region-XX 当作 hint，否则用 BILLING/CHECKOUT。
        try:
            import config as _cfg
            import re as _re_cfg
            # ★ 先看 proxy user 段里的 region-XX（最准，跟实际出口一致）
            _proxy_region = ""
            if proxy:
                m = _re_cfg.search(r"region-([A-Za-z]{2})(?:-|$)", str(proxy.get("user") or ""))
                if m:
                    _proxy_region = m.group(1).upper()
            if _proxy_region:
                _country_hint = _proxy_region.lower()
            else:
                _bc = (getattr(_cfg, "BILLING_COUNTRY", "") or "").strip().upper()
                if not _bc or _bc == "AUTO":
                    _bc = (getattr(_cfg, "CHECKOUT_COUNTRY", "") or "US").strip().upper()
                _country_hint = (_bc or "US").lower()
        except Exception:
            _country_hint = "us"
        bid = bb_create_window(name=name, proxy=proxy, country=_country_hint)
        log.info(f"  [BitBrowser] 已创建临时 profile id={bid} name={name}"
                 f"  proxy={(proxy or {}).get('host', '直连')} country_hint={_country_hint}")
        ephemeral = True

    debug_addr = bb_open_window(bid, window_index=window_index, incognito=False)
    log.info(f"  [BitBrowser] 调试地址: {debug_addr}")
    time.sleep(3)

    co = ChromiumOptions()
    co.set_address(debug_addr)
    browser = ChromiumPage(co)

    # 普通窗口已打开（BitBrowser profile 的指纹/代理/YesCaptcha 扩展生效）。
    # 用 osascript 发系统级 Cmd+Shift+N 给 BitBrowser 窗口，打开无痕窗口。
    # CDP Input.dispatchKeyEvent 只能发页面级事件，无法触发浏览器级快捷键，
    # 所以必须走 macOS 系统级 AppleScript。
    incog_tab = None
    try:
        tabs_before = {t.tab_id for t in browser.get_tabs()}

        # 用 osascript 激活 BitBrowser 窗口并发 Cmd+Shift+N
        import subprocess as _sp
        _sp.run([
            "osascript", "-e",
            'tell application "System Events" to keystroke "n" using {command down, shift down}'
        ], timeout=5, capture_output=True)
        time.sleep(2.5)  # 等无痕窗口打开

        tabs_after = browser.get_tabs()
        new_tabs = [t for t in tabs_after if t.tab_id not in tabs_before]
        if new_tabs:
            incog_tab = new_tabs[-1]
            log.info(f"  [BitBrowser] Cmd+Shift+N 无痕窗口已打开（tab_id={incog_tab.tab_id[:12]}…）")
        else:
            log.warning("  [BitBrowser] Cmd+Shift+N 未检测到新 tab，回退到普通 tab")
    except Exception as e:
        log.warning(f"  [BitBrowser] Cmd+Shift+N 失败（回退到普通 tab）: {e}")

    if incog_tab is None:
        incog_tab = browser.latest_tab
        log.info("  [BitBrowser] 使用普通 tab（无痕窗口未能打开）")
    else:
        log.info("  [BitBrowser] 普通窗口 + 无痕 tab（指纹/代理来自 profile，cookies 隔离）")

    # ============================================================
    # 反检测脚本：已禁用！
    # ============================================================
    # 之前在无痕 tab 上注入了一段 navigator.webdriver=false / plugins 伪造，
    # 实测在 chatgpt.com / cloudflare turnstile 上反而**触发**人机验证：
    #   1. BitBrowser 自身已经处理了 webdriver，我们再 defineProperty 覆盖
    #      会让 Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver')
    #      的 getter 函数 toString() 跟原生不一致 → Cloudflare 探测到伪造
    #   2. Page.addScriptToEvaluateOnNewDocument 本身就是 CF 风控信号之一
    #   3. 我们伪造的 plugins (2 个 PDF Viewer) 跟真实 Chrome (5+ plugin) 不符
    # 让 BitBrowser 自己的指纹接管即可（手动操作不弹 CF 就证明它够用）。
    # 要恢复，把下面 `if False:` 改回 `if True:`。
    if False:
        try:
            anti_detect_js = r"""
            (function(){
                try {
                    Object.defineProperty(Navigator.prototype, 'webdriver', {
                        get: function(){ return false; },
                        configurable: true
                    });
                } catch(e){}
                try {
                    if (window.navigator && 'webdriver' in window.navigator) {
                        delete window.navigator.webdriver;
                    }
                } catch(e){}
                try {
                    if (!window.chrome) window.chrome = {};
                    if (!window.chrome.runtime) window.chrome.runtime = {};
                } catch(e){}
            })();
            """
            try:
                incog_tab.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=anti_detect_js)
                log.info("  [BitBrowser] 反检测脚本已注入到无痕 tab")
            except Exception:
                browser.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=anti_detect_js)
                log.info("  [BitBrowser] 反检测脚本已注入 (browser-level)")
        except Exception as e:
            log.warning(f"  [BitBrowser] 反检测注入失败: {e}")
    else:
        log.info("  [BitBrowser] 反检测脚本已禁用（让 BitBrowser 自身指纹接管，更稳）")

    # 复用 profile 模式深度清理（第三层）已按用户要求关闭。
    # 现在只保留第二层：BitBrowser 配置层 bb_clear_cache + bb_clear_cookies（上面已做）。
    # 要恢复 CDP origin 级深度清理，把 `if False:` 改回 `if not ephemeral:`。
    if False and not ephemeral:
        log.info("  [BitBrowser] CDP 深度清理 PayPal/Stripe/OpenAI cookies+storage+ServiceWorker...")
        try:
            paypal_origins = [
                "https://www.paypal.com", "https://paypal.com",
                "https://www.paypalobjects.com", "https://paypalobjects.com",
                "https://checkout.stripe.com", "https://stripe.com",
                "https://pay.openai.com", "https://www.openai.com",
                "https://chatgpt.com", "https://www.chatgpt.com",
            ]
            # storageTypes='all' 包含：cookies / local_storage / session_storage /
            #   indexeddb / websql / cache_storage / service_workers / file_systems /
            #   shader_cache / appcache 等所有类型
            for origin in paypal_origins:
                try:
                    browser.run_cdp("Storage.clearDataForOrigin",
                                    origin=origin,
                                    storageTypes="all")
                except Exception:
                    pass

            # 全局清 ServiceWorker（PayPal 用 SW 缓存反欺诈脚本，是"无痕没事"的关键）
            try:
                browser.run_cdp("ServiceWorker.enable")
            except Exception:
                pass
            for origin in paypal_origins:
                try:
                    browser.run_cdp("ServiceWorker.unregister", scopeURL=origin + "/")
                except Exception:
                    pass

            # 全局清 cache + 禁用 cache
            try:
                browser.run_cdp("Network.clearBrowserCache")
                browser.run_cdp("Network.clearBrowserCookies")
                browser.run_cdp("Network.setCacheDisabled", cacheDisabled=True)
            except Exception:
                pass

            for d in [".paypal.com", "paypal.com", ".paypalobjects.com",
                      ".stripe.com", "stripe.com",
                      ".openai.com", "openai.com",
                      ".chatgpt.com", "chatgpt.com"]:
                try:
                    browser.run_cdp("Network.deleteCookies", domain=d)
                except Exception:
                    pass
            log.info("  [BitBrowser] CDP 深度清理完成")
        except Exception as e:
            log.warning(f"  [BitBrowser] CDP 深度清理失败（不致命）: {e}")

    # 验证代理：探测出口 IP 国家 + PayPal 是否拉黑
    # 复用 profile 模式：profile 自带代理（动态 IP），关窗再开会换 IP
    # ephemeral 模式：profile 是临时建的，proxy 是显式传进来的
    profile_has_proxy = bool(proxy)
    if not profile_has_proxy:
        try:
            d = bb_get_window_detail(bid)
            profile_has_proxy = (d.get("proxyMethod") in (2, "2"))
        except Exception:
            profile_has_proxy = False

    # ===========================================================
    # IP 探测 + PayPal block 检测（暂时关掉，跑全程不再做这步）
    # 要恢复，把 `if False:` 改回 `if profile_has_proxy:`
    # ===========================================================
    if False and profile_has_proxy:
        max_ip_attempts = 5
        # 从 config 读期望国家：BILLING_COUNTRY > CHECKOUT_COUNTRY > 默认 US
        try:
            import config as _cfg
            _bc = (getattr(_cfg, "BILLING_COUNTRY", "") or "").strip().upper()
            if not _bc or _bc == "AUTO":
                _bc = (getattr(_cfg, "CHECKOUT_COUNTRY", "") or "US").strip().upper()
            required_country = _bc or "US"
        except Exception:
            required_country = "US"
        log.info(f"  [BitBrowser] 期望出口国家: {required_country}")
        for ip_attempt in range(1, max_ip_attempts + 1):
            try:
                page0 = incog_tab    # 走无痕 context，避免 PayPal 复用旧 cookies/SW
                # 一次请求拿 IP + country（ipinfo 返回 JSON）
                exit_ip = "?"
                country = "?"
                try:
                    page0.get("https://ipinfo.io/json", timeout=20)
                    time.sleep(2)
                    ip_text = page0.html or ""
                    import re as _re
                    m_ip = _re.search(r'"ip"\s*:\s*"([\d\.]+)"', ip_text)
                    m_co = _re.search(r'"country"\s*:\s*"([A-Z]{2})"', ip_text)
                    if m_ip:
                        exit_ip = m_ip.group(1)
                    if m_co:
                        country = m_co.group(1)
                except Exception as e:
                    log.warning(f"  [BitBrowser] ipinfo 探测失败 attempt {ip_attempt}: {e}")
                # 备用：ipapi.co
                if country == "?":
                    try:
                        page0.get("https://ipapi.co/json/", timeout=20)
                        time.sleep(2)
                        ip_text = page0.html or ""
                        import re as _re
                        m_ip = _re.search(r'"ip"\s*:\s*"([\d\.]+)"', ip_text)
                        m_co = _re.search(r'"country_code"\s*:\s*"([A-Z]{2})"', ip_text) \
                                or _re.search(r'"country"\s*:\s*"([A-Z]{2})"', ip_text)
                        if m_ip and exit_ip == "?":
                            exit_ip = m_ip.group(1)
                        if m_co:
                            country = m_co.group(1)
                    except Exception as e:
                        log.warning(f"  [BitBrowser] ipapi 探测失败 attempt {ip_attempt}: {e}")

                log.info(f"  [BitBrowser] 出口 IP={exit_ip} country={country} (attempt {ip_attempt}/{max_ip_attempts})")

                # 国家拿不到（country=?）—— ipinfo / ipapi 都被代理 block 了，
                # 不能盲目通过。当 IP 也拿不到时关窗重开换 IP；IP 拿到了但 country 没拿到
                # 就放过去，让后面 PayPal 探测来定。
                if country == "?" and exit_ip == "?":
                    log.warning(f"  [BitBrowser] 出口 IP/country 都拿不到（代理可能没就绪），关窗重开 attempt {ip_attempt}/{max_ip_attempts}")
                    if ip_attempt < max_ip_attempts:
                        bb_close_window(bid)
                        time.sleep(4)
                        debug_addr = bb_open_window(bid, window_index=window_index, incognito=False)
                        log.info(f"  [BitBrowser] 重开 调试地址: {debug_addr}")
                        time.sleep(3)
                        co = ChromiumOptions()
                        co.set_address(debug_addr)
                        browser = ChromiumPage(co)
                        incog_tab = browser.latest_tab
                        page0 = incog_tab
                        continue
                    # 用尽次数还不行就硬跑，至少看下 PayPal 能不能加载

                # 国家不是预期国家 → 关窗重开换 IP
                if country != "?" and country != required_country:
                    log.warning(f"  [BitBrowser] 国家 {country} != {required_country}，关窗重开换 IP")
                    if ip_attempt < max_ip_attempts:
                        bb_close_window(bid)
                        time.sleep(4)
                        debug_addr = bb_open_window(bid, window_index=window_index, incognito=False)
                        log.info(f"  [BitBrowser] 重开 调试地址: {debug_addr}")
                        time.sleep(3)
                        co = ChromiumOptions()
                        co.set_address(debug_addr)
                        browser = ChromiumPage(co)
                        incog_tab = browser.latest_tab
                        page0 = incog_tab
                        continue
                    else:
                        raise BitBrowserError(f"5 次代理出口都不是 {required_country}（最后是 {country}），放弃")

                # 探测 PayPal 是否会 block — 检测多语言 block 文案
                # 用 /signup 而不是 / 首页，因为首页可能不 block 但支付页面 block
                # PayPal Cloudflare 反欺诈对 /signup /signin /agreements 这一类反欺诈最严
                raw_html = ""
                page_text = ""
                for probe_url in (
                    "https://www.paypal.com/signin",
                    "https://www.paypal.com/",
                ):
                    try:
                        page0.get(probe_url, timeout=30)
                        time.sleep(4)
                        raw_html = page0.html or ""
                        page_text = raw_html.lower()
                        break
                    except Exception as e:
                        log.warning(f"  [BitBrowser] PayPal 探测 {probe_url} 失败: {e}")

                # 英文：you have been blocked / couldn't load the security challenge
                # 中文：您已被屏蔽 / 无法加载安全验证
                # 日文：あなたはブロックされています / セキュリティチャレンジを読み込めませんでした
                # 西/葡：has sido bloqueado / você foi bloqueado
                blocked = (
                    "you have been blocked" in page_text
                    or "couldn't load the security challenge" in page_text
                    or "您已被屏蔽" in raw_html
                    or "已被屏蔽" in raw_html
                    or "无法加载安全" in raw_html
                    or "ブロックされています" in raw_html
                    or "セキュリティチャレンジを読み込" in raw_html
                    or "ha sido bloqueado" in page_text
                    or "você foi bloqueado" in page_text
                    or "vous avez été bloqué" in page_text
                )

                if blocked:
                    log.warning(f"  [BitBrowser] IP {exit_ip} 被 PayPal 拉黑（attempt {ip_attempt}/{max_ip_attempts}），关窗换 IP")
                    if ip_attempt < max_ip_attempts:
                        bb_close_window(bid)
                        time.sleep(4)
                        debug_addr = bb_open_window(bid, window_index=window_index, incognito=False)
                        log.info(f"  [BitBrowser] 重开 调试地址: {debug_addr}")
                        time.sleep(3)
                        co = ChromiumOptions()
                        co.set_address(debug_addr)
                        browser = ChromiumPage(co)
                        incog_tab = browser.latest_tab
                        page0 = incog_tab
                        continue
                    else:
                        raise BitBrowserError("5 次都拿到被 PayPal 拉黑的 IP，放弃")

                log.info(f"  [BitBrowser] ✓ IP {exit_ip} ({country}) 通过 PayPal 检测")
                break
            except BitBrowserError:
                raise
            except Exception as e:
                log.warning(f"  [BitBrowser] IP 探测异常 attempt {ip_attempt}: {e}")
                if ip_attempt >= max_ip_attempts:
                    log.warning("  [BitBrowser] 探测多次失败，跳过验证直接跑")
                    break
                time.sleep(2)

    if goto and url:
        page = incog_tab    # 把无痕 tab 作为后续 pipeline 的工作 tab
        log.info(f"  [BitBrowser] 跳转到长链: {url[:100]}")
        page.get(url)
        time.sleep(3)
    else:
        page = incog_tab

    return page, bid


# 导出便捷别名（兼容 kiro-pro-batch 的命名风格）
create_bitbrowser = bb_create_window
open_bitbrowser = bb_open_window
close_bitbrowser = bb_close_window
delete_bitbrowser = bb_delete_window
