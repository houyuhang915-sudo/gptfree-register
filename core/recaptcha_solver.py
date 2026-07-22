"""YesCaptcha reCAPTCHA / hCaptcha 求解器。

API 文档：https://yescaptcha.atlassian.net/wiki/spaces/YESCAPTCHA/overview

支持类型：
    - NoCaptchaTaskProxyless         reCAPTCHA v2 (普通 / invisible)
    - RecaptchaV2EnterpriseTaskProxyless  reCAPTCHA v2 enterprise
    - RecaptchaV3TaskProxyless       reCAPTCHA v3
    - HCaptchaTaskProxyless          hCaptcha
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("captcha")

YESCAPTCHA_BASE = "https://api.yescaptcha.com"
CREATE_URL = f"{YESCAPTCHA_BASE}/createTask"
RESULT_URL = f"{YESCAPTCHA_BASE}/getTaskResult"


class CaptchaError(RuntimeError):
    pass


def _post(url: str, payload: dict, timeout: int = 30) -> dict:
    resp = requests.post(url, json=payload, timeout=timeout,
                         proxies={"http": None, "https": None})
    try:
        return resp.json()
    except Exception:
        raise CaptchaError(f"非 JSON 响应: {resp.text[:200]}")


def _create_task(api_key: str, task: dict) -> str:
    """创建任务，返回 taskId"""
    payload = {"clientKey": api_key, "task": task}
    data = _post(CREATE_URL, payload)
    if data.get("errorId", 1) != 0:
        raise CaptchaError(f"createTask 失败: {data.get('errorCode')}/{data.get('errorDescription')}")
    return str(data["taskId"])


def _wait_result(api_key: str, task_id: str,
                 timeout: int = 180, initial_wait: int = 5,
                 poll_interval: int = 3) -> dict:
    """轮询取结果，返回 solution 对象"""
    time.sleep(initial_wait)
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = _post(RESULT_URL, {"clientKey": api_key, "taskId": task_id})
        if data.get("errorId", 1) != 0:
            raise CaptchaError(
                f"getTaskResult 失败: {data.get('errorCode')}/{data.get('errorDescription')}"
            )
        status = data.get("status")
        if status == "ready":
            return data.get("solution", {}) or {}
        # status == "processing" → 继续轮询
        time.sleep(poll_interval)
    raise CaptchaError(f"task {task_id} 超时 ({timeout}s)")


def solve_recaptcha_v2(api_key: str, site_key: str, page_url: str,
                       *, invisible: bool = False, enterprise: bool = False,
                       data_s: str = "", timeout: int = 180) -> str:
    """求解 reCAPTCHA v2，返回 g-recaptcha-response token"""
    task = {
        "type": "RecaptchaV2EnterpriseTaskProxyless" if enterprise else "NoCaptchaTaskProxyless",
        "websiteURL": page_url,
        "websiteKey": site_key,
    }
    if invisible:
        task["isInvisible"] = True
    if data_s:
        task["dataS"] = data_s

    log.info(f"[YesCaptcha] v2 task: site_key={site_key[:25]} pageurl={page_url[:80]} "
             f"invisible={invisible} enterprise={enterprise}")
    tid = _create_task(api_key, task)
    log.info(f"[YesCaptcha] taskId={tid}")
    sol = _wait_result(api_key, tid, timeout=timeout)
    token = sol.get("gRecaptchaResponse", "")
    if not token:
        raise CaptchaError(f"v2 solution 缺 gRecaptchaResponse: {sol}")
    log.info(f"[YesCaptcha] v2 token len={len(token)}")
    return token


def solve_recaptcha_v3(api_key: str, site_key: str, page_url: str,
                       *, action: str = "verify", min_score: float = 0.3,
                       enterprise: bool = False, timeout: int = 180) -> str:
    task = {
        "type": "RecaptchaV3TaskProxyless",
        "websiteURL": page_url,
        "websiteKey": site_key,
        "pageAction": action,
        "minScore": min_score,
    }
    if enterprise:
        task["isEnterprise"] = True
    log.info(f"[YesCaptcha] v3 task: site_key={site_key[:25]} action={action}")
    tid = _create_task(api_key, task)
    sol = _wait_result(api_key, tid, timeout=timeout)
    token = sol.get("gRecaptchaResponse", "")
    if not token:
        raise CaptchaError(f"v3 solution 缺 gRecaptchaResponse: {sol}")
    return token


def solve_hcaptcha(api_key: str, site_key: str, page_url: str,
                   *, invisible: bool = False, timeout: int = 180) -> str:
    task = {
        "type": "HCaptchaTaskProxyless",
        "websiteURL": page_url,
        "websiteKey": site_key,
    }
    if invisible:
        task["isInvisible"] = True
    tid = _create_task(api_key, task)
    sol = _wait_result(api_key, tid, timeout=timeout)
    token = sol.get("gRecaptchaResponse", "")
    if not token:
        raise CaptchaError(f"hcaptcha solution 缺 gRecaptchaResponse: {sol}")
    return token


def get_balance(api_key: str) -> float:
    data = _post(f"{YESCAPTCHA_BASE}/getBalance", {"clientKey": api_key})
    if data.get("errorId", 1) != 0:
        raise CaptchaError(f"balance 查询失败: {data}")
    return float(data.get("balance", 0))
