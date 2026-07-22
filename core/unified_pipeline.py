"""三步全流程编排：协议注册 → PayPal 提链 → PayPal 自动化。

整套流程（每个 outlook 账号独立跑一次）：

    [step 1] chatgpt_register.ChatGPTRegister(outlook_creds).register()
                ↓ 拿 access_token + session_token
    [step 2] paypal_extract.run_extraction(LongLinkRequest(accessToken=..., link_type='paypal_redirect'))
                ↓ 拿 long_url（pm-redirects.stripe.com 短链 / paypal BA 长链）
    [step 3] paypal_only.run_paypal_only(long_url, email=..., browser=...)
                ↓ 浏览器自动跑表单 → paid

关键约定（用户拍板）：

- 失败直接 return，让外层换下一个 outlook（不做 fallback / retry）
- 支持 ``skip_register=True`` 直接从 step 2 开始（已经有 access_token 的情况）
- ``log_fn`` 接到上层（CLI / webui）的日志面板，让 step_log 可以流式推
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

log = logging.getLogger("unified_pipeline")


def _noop_log(msg: str) -> None:
    print(msg, flush=True)


def _browser_proxy_from_url(proxy_url: str) -> dict | None:
    """Convert a standard proxy URL into the browser-manager proxy shape."""
    value = (proxy_url or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if not parsed.hostname or not parsed.port:
        raise ValueError("代理 URL 缺少 host/port")
    scheme = (parsed.scheme or "http").lower()
    proxy_type = "SOCKS5" if scheme in {"socks5", "socks5h"} else scheme.upper()
    return {
        "host": parsed.hostname,
        "port": int(parsed.port),
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "proxyType": proxy_type,
    }


def _browser_register(
    *,
    outlook_email: str,
    outlook_password: str,
    refresh_token: str,
    client_id: str,
    browser_choice: str = "bitbrowser",
    proxy_url: str = "",
    log_fn: Optional[Callable[[str], None]] = None,
    window_index: Optional[int] = None,
) -> tuple[str, str, dict, str]:
    """用浏览器自动化跑 ChatGPT 注册（pipeline.GPTPipeline.signup）。

    Returns: (access_token, session_token, raw_register_result_dict, error_msg)
    error_msg 非空 = 注册失败。

    window_index: 屏幕槽位（0-based，含 0），None 时读 WORKER_INDEX。
    Free 浏览器并发 6 时按 3×2 网格铺窗。

    流程：
      1. 起 BitBrowser / RoxyBrowser ephemeral / 复用 profile（按 browser_choice）
      2. 创建 GPTPipeline(page) → signup(email, password, outlook_creds)
      3. signup 内部走 chatgpt.com 注册 UI，碰到 OTP 邮箱验证码就用 outlook_creds
         调 email_provider.fetch_otp 拉取
      4. 成功后从浏览器调 fetch /api/auth/session 拿 access_token
      5. 退出前关掉/删除 profile（避免堆积窗口）
    """
    log = log_fn or _noop_log

    if not outlook_email:
        return "", "", {}, "outlook_email 为空"
    if not (refresh_token or "").strip():
        return "", "", {}, "outlook refresh_token 为空，无法拉 OTP 邮件"

    # 按 browser_choice 起浏览器
    page = None
    profile_id = ""
    proxy_bridge = None
    cleanup = lambda: None
    slot = window_index

    try:
        browser_proxy = _browser_proxy_from_url(proxy_url)
        bc = (browser_choice or "bitbrowser").strip().lower()
        if bc == "roxy":
            try:
                from roxy_mgr import (
                    open_roxy_with_url,
                    roxy_close_profile,
                    roxy_delete_local_profile,
                    roxy_delete_profile,
                )
            except Exception as e:
                return "", "", {}, f"roxy_mgr 加载失败: {e}"
            log(f"[全流程][browser-reg] 起 RoxyBrowser… slot={slot}")
            page, profile_id = open_roxy_with_url(
                url="",
                proxy=browser_proxy,
                goto=False,
                force_ephemeral=bool(browser_proxy),
            )
            def cleanup():
                try:
                    page.quit()
                except Exception:
                    pass
                try:
                    roxy_close_profile(profile_id)
                except Exception:
                    pass
                try:
                    if str(profile_id).startswith("local-"):
                        roxy_delete_local_profile(profile_id)
                    elif browser_proxy:
                        roxy_delete_profile(profile_id)
                except Exception:
                    pass
        elif bc == "chromium":
            try:
                from browser_mgr import open_local_chromium
            except Exception as e:
                return "", "", {}, f"chromium 启动失败: {e}"

            def cleanup():
                try:
                    if page is not None:
                        page.quit()
                except Exception:
                    pass
                try:
                    if proxy_bridge is not None:
                        proxy_bridge.close()
                except Exception:
                    pass

            chromium_proxy = (proxy_url or "").strip()
            parsed_proxy = urlparse(chromium_proxy) if chromium_proxy else None
            if parsed_proxy and (parsed_proxy.username is not None or parsed_proxy.password is not None):
                try:
                    from paylink_proxy import ProxyChainServer
                except Exception as e:
                    return "", "", {}, f"认证代理桥接加载失败: {e}"
                proxy_bridge = ProxyChainServer("", chromium_proxy, log=log)
                proxy_bridge.__enter__()
                chromium_proxy = proxy_bridge.url
                log("[全流程][browser-reg] 认证代理已接入本地桥接")

            log(f"[全流程][browser-reg] 起本地 Chromium… slot={slot}")
            page = open_local_chromium(window_index=slot, proxy=chromium_proxy)
            profile_id = ""
        else:
            # 默认 BitBrowser
            try:
                from browser_mgr import (
                    open_bitbrowser_with_url, bb_close_window, bb_delete_window, bb_health,
                )
                import config as _cfg
            except Exception as e:
                return "", "", {}, f"browser_mgr 加载失败: {e}"
            if not bb_health():
                return "", "", {}, "BitBrowser 未运行（127.0.0.1:54345 不可达）"
            log(f"[全流程][browser-reg] 起 BitBrowser ephemeral 窗口… slot={slot}")
            pay_proxy = browser_proxy or getattr(_cfg, "BITBROWSER_PROXY", None)
            name = f"reg_{outlook_email[:24]}_{int(time.time())}"
            page, profile_id = open_bitbrowser_with_url(
                "", name=name, proxy=pay_proxy, goto=False,
                window_index=slot,
            )
            def cleanup():
                try:
                    page.quit()
                except Exception:
                    pass
                try:
                    bb_close_window(profile_id)
                except Exception:
                    pass
                try:
                    bb_delete_window(profile_id)
                except Exception:
                    pass

        # 跑 signup
        try:
            from pipeline import GPTPipeline
        except Exception as e:
            return "", "", {}, f"pipeline.GPTPipeline 加载失败: {e}"

        gp = GPTPipeline(page)
        # outlook_creds 用 GPTPipeline.signup 拉 OTP
        outlook_creds = {
            "email": outlook_email,
            "client_id": client_id,
            "refresh_token": refresh_token,
        }
        # 密码：用户给了用用户的，没给就生成一个
        password = (outlook_password or "").strip()
        if not password:
            import random as _rd
            import string as _str
            chars = _str.ascii_letters + _str.digits + "!@#$%"
            password = "".join(_rd.choice(chars) for _ in range(16))
            log(f"[全流程][browser-reg] 自动生成 chatgpt 密码（{len(password)} 位）")

        try:
            register_result = gp.signup(outlook_email, password, outlook_creds)
        except Exception as e:
            return "", "", {}, f"signup 异常: {type(e).__name__}: {e}"

        if not isinstance(register_result, dict):
            return "", "", {}, f"signup 返回非 dict: {register_result!r}"

        status = str(register_result.get("status", "")).strip()
        if status != "success":
            return "", "", register_result, f"signup status={status}"

        access_token = (register_result.get("access_token") or "").strip()
        if not access_token:
            return "", "", register_result, "signup 成功但没拿到 access_token"

        # session_token 浏览器注册一般不返回，留空（paylink 不强制需要）
        session_token = (register_result.get("session_token") or "").strip()

        return access_token, session_token, register_result, ""
    except Exception as e:
        return "", "", {}, f"_browser_register 异常: {type(e).__name__}: {e}"
    finally:
        cleanup()
