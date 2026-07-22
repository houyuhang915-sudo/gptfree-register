"""
SMS 接码 provider 抽象（PayPal 验证用）

支持两种后端：
  1. "62us"      — 现有的 62-us.com / a.62-us.com 方案，URL 里直接带 key，
                   GET 一次就给最近一条短信文本（需要预先买一个固定号码）。
  2. "smsbower"  — sms-activate 协议（smsbower.app 等），用
                   apikey 走 getNumber → getStatus → setStatus 流程，自动选号
                   自动还号。同协议平台（smshub、smsverified、tiger-sms、
                   hero-sms、onlinesim、smsverified）只需换 base_url 都能跑。

参考 GuJumpgate/FlowPilot/phone-sms/providers/hero-sms.js 的协议封装。

使用：
    from sms_provider import get_sms_provider
    p = get_sms_provider()
    activation = p.request_phone()
    if activation:
        try:
            phone = activation['phone']     # E.164: '+15555550100'
            # 把这个号码填到 PayPal 表单
            code = p.wait_otp(activation, deadline_s=120)
            if code:
                p.complete(activation)
        finally:
            if not code:
                p.cancel(activation)
"""
from __future__ import annotations

import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable

import config

log = logging.getLogger("sms_provider")

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


# ============================================================
#  Base interface
# ============================================================

class SmsProviderBase:
    name = "base"

    def request_phone(self) -> dict | None:
        """申请一个号码。

        Returns:
            dict 或 None。dict 形如：
            {
                "id": "12345678",          # 平台侧 activation id
                "phone": "+12025550100",   # E.164 格式
                "provider": "smsbower",
                "raw": "ACCESS_NUMBER:...",
            }
        """
        raise NotImplementedError

    def mark_sent(self, activation: dict) -> bool:
        """通知平台验证码已触发，开始等待短信。"""
        return True

    def request_resend(self, activation: dict) -> bool:
        """通知平台同一租约将接收一条重发短信。"""
        return True

    def wait_otp(self, activation: dict, deadline_s: int = 120,
                 poll: float = 5.0,
                 exclude_codes: Iterable[str] | None = None) -> str | None:
        raise NotImplementedError

    def complete(self, activation: dict) -> bool:
        """OTP 用完了，告诉平台释放（同号下次可能还能用 ≈ ACCESS_FINISH）"""
        return True

    def cancel(self, activation: dict) -> bool:
        """没收到码，把号还回去"""
        return True


# ============================================================
#  62-us.com — 原有实现包装
# ============================================================

class Sms62UsProvider(SmsProviderBase):
    name = "62us"

    def __init__(self, sms_url: str = ""):
        self.sms_url = (sms_url or "").strip()

    def request_phone(self) -> dict | None:
        # 62-us 不需要申请，号码是预先买好的，URL 里带的 key 已经固定到这个号
        if not self.sms_url:
            return None
        return {
            "id": "62us",
            "phone": "",            # 号码不在 API 里，由调用方从卡数据/config 拿
            "provider": self.name,
            "url": self.sms_url,
        }

    def wait_otp(self, activation: dict, deadline_s: int = 120,
                 poll: float = 3.0,
                 exclude_codes: Iterable[str] | None = None) -> str | None:
        url = activation.get("url") or self.sms_url
        if not url:
            return None
        excluded = _normalize_codes(exclude_codes)
        end = time.time() + deadline_s
        while time.time() < end:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                resp = urllib.request.urlopen(req, timeout=10, context=_SSL_CTX)
                text = (resp.read() or b"").decode("utf-8", errors="ignore").strip()
                if text.startswith("yes|"):
                    m = re.search(r"PayPal[^\d]*?(\d{6})", text, re.I)
                    if not m:
                        m = re.search(r"\b(\d{6})\b", text)
                    if m and m.group(1) not in excluded:
                        return m.group(1)
            except Exception as e:
                log.debug(f"  [sms-62us] poll err: {e}")
            time.sleep(poll)
        return None


# ============================================================
#  sms-activate 协议（smsbower / smshub / tiger-sms / hero-sms ...）
# ============================================================

class SmsActivateProvider(SmsProviderBase):
    """sms-activate 协议封装。

    base_url 形如 'https://smsbower.app/stubs/handler_api.php'

    协议常量参考：
      action=getBalance        → 'ACCESS_BALANCE:<float>'
      action=getNumber         → 'ACCESS_NUMBER:<id>:<phone>'  (phone 不带 +)
      action=getNumberV2       → JSON {'activationId':..., 'phoneNumber':...}
                                 或 'NO_NUMBERS' / 'NO_BALANCE' / 'BAD_KEY' / ...
      action=getStatus&id=     → 'STATUS_WAIT_CODE'
                                 'STATUS_OK:<code>'
                                 'STATUS_WAIT_RETRY:<last_code>'
                                 'STATUS_WAIT_RESEND'
                                 'STATUS_CANCEL'
      action=setStatus&status= 1=等待 / 3=再发 / 6=完成并加入黑名单 /
                              8=取消 (同 ACCESS_CANCEL) / 6=已收码完成

    备注：
      - 'service' 用 sms-activate 标准短码，PayPal = 'pp'。
      - 'country' 是数字 id，smsbower 国家表跟 sms-activate 一致（详见
        SmsBower 控制台/官方 API 文档）。常见：0=俄罗斯 / 12=美国 /
        16=英国 / 36=加拿大 / 22=印度 / 6=印尼 / 14=Brazil / 52=泰国 等。
    """

    name = "smsbower"

    DEFAULT_BASE_URL = "https://smsbower.app/stubs/handler_api.php"
    DEFAULT_SERVICE = "pp"          # PayPal
    DEFAULT_COUNTRY = 12            # USA
    REQUEST_TIMEOUT = 20

    # setStatus 状态码
    STATUS_READY = 1
    STATUS_RESEND = 3
    # Historical aliases retained for callers that referenced these constants.
    STATUS_READY_TO_RESEND = STATUS_READY
    STATUS_RECEIVED_PUSH_CONFIRMATION = STATUS_RESEND
    STATUS_OK_FINISH = 6
    STATUS_CANCEL = 8

    def __init__(self,
                 api_key: str,
                 base_url: str = "",
                 service: str = "",
                 country: int | str = "",
                 max_price: float | str = "",
                 operator: str = "",
                 use_v2: bool | str = True,
                 provider_ids: str = "",
                 except_provider_ids: str = "",
                 phone_exception: str = "",
                 ref: str = ""):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or self.DEFAULT_BASE_URL).strip().rstrip("?")
        self.service = (service or self.DEFAULT_SERVICE).strip() or self.DEFAULT_SERVICE
        try:
            self.country = int(country) if str(country).strip() else self.DEFAULT_COUNTRY
        except (TypeError, ValueError):
            self.country = self.DEFAULT_COUNTRY
        self.max_price = str(max_price).strip() if max_price not in (None, "") else ""
        self.operator = (operator or "").strip()
        self.use_v2 = self._truthy(use_v2)
        self.provider_ids = (provider_ids or "").strip()
        self.except_provider_ids = (except_provider_ids or "").strip()
        self.phone_exception = (phone_exception or "").strip()
        self.ref = (ref or "").strip()
        self.last_price_snapshot: dict = {}

    @staticmethod
    def _truthy(value: bool | str | int | None) -> bool:
        if isinstance(value, bool):
            return value
        return str(value if value is not None else "").strip().lower() not in (
            "0", "false", "no", "off", ""
        )

    # ----- 内部：HTTP -----

    def _http_get(self, params: dict) -> str:
        """GET handler_api.php?api_key=...&action=...

        smsbower 在 BAD_KEY 时返回 HTTP 401 + JSON {"status":0,"message":"No access"}，
        其他平台一般返回 HTTP 200 + 'BAD_KEY' 文本。我们把两种都映射成 'BAD_KEY' 字符串
        让上层统一识别。
        """
        if not self.api_key:
            raise RuntimeError("smsbower API key 未配置")
        q = {"api_key": self.api_key}
        for k, v in params.items():
            if v is None or v == "":
                continue
            q[k] = str(v)
        url = self.base_url + "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.REQUEST_TIMEOUT, context=_SSL_CTX) as resp:
                text = resp.read().decode("utf-8", errors="ignore").strip()
            return text
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="ignore").strip()
            except Exception:
                pass
            # smsbower 401 JSON → 标准化成 BAD_KEY
            if e.code == 401 and body.startswith("{"):
                try:
                    data = json.loads(body)
                    msg = str(data.get("message") or data.get("msg") or "")
                    if "no access" in msg.lower() or data.get("status") == 0:
                        return "BAD_KEY"
                except json.JSONDecodeError:
                    pass
            log.warning(f"  [smsbower] HTTP {e.code}: {body[:120]}")
            raise

    @staticmethod
    def _to_e164(country_id: int, raw_phone: str) -> str:
        """API 返回的号码可能是 '12025550100' 也可能是 '+12025550100'。
        统一加 '+' 前缀。"""
        s = str(raw_phone or "").strip()
        if not s:
            return ""
        if s.startswith("+"):
            return s
        # 纯数字直接加 +
        if s.isdigit():
            return "+" + s
        # 其他情况原样
        return s

    # ----- 公开 API -----

    def get_balance(self) -> float | None:
        try:
            text = self._http_get({"action": "getBalance"})
        except Exception as e:
            log.warning(f"  [smsbower] getBalance err: {e}")
            return None
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {}
            for key in ("balance", "Balance", "ACCESS_BALANCE"):
                if key in data:
                    try:
                        return float(str(data[key]).replace(",", "."))
                    except (TypeError, ValueError):
                        pass
        m = re.search(r"ACCESS_BALANCE:([\d.]+)", text, re.I)
        if not m:
            log.warning(f"  [smsbower] getBalance unexpected: {text[:80]}")
            return None
        try:
            return float(m.group(1))
        except ValueError:
            return None

    @staticmethod
    def _json_dict(text: str) -> dict | None:
        s = (text or "").strip()
        if not s.startswith("{"):
            return None
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def get_price_snapshot(self) -> dict:
        """Return the platform's current price/count for this country/service."""
        try:
            text = self._http_get({
                "action": "getPrices",
                "country": self.country,
                "service": self.service,
            })
        except Exception as exc:
            log.debug("  [smsbower] getPrices err: %s", exc)
            return {}
        data = self._json_dict(text)
        if not data:
            return {}
        country_prices = data.get(str(self.country)) or data.get(self.country)
        if not isinstance(country_prices, dict):
            return {}
        service_price = country_prices.get(self.service)
        if not isinstance(service_price, dict):
            return {}
        snapshot = {
            "country": self.country,
            "service": self.service,
            "cost": service_price.get("cost"),
            "count": service_price.get("count"),
        }
        self.last_price_snapshot = snapshot
        return snapshot

    def list_service_countries(self, service: str = "") -> list[dict]:
        """Merge platform country metadata with current service price/stock."""
        service_code = str(service or self.service).strip() or self.service
        countries_text = self._http_get({"action": "getCountries"})
        prices_text = self._http_get({
            "action": "getPrices",
            "service": service_code,
        })
        countries = self._json_dict(countries_text) or {}
        prices = self._json_dict(prices_text) or {}
        rows: list[dict] = []
        for raw_id, country_prices in prices.items():
            if not isinstance(country_prices, dict):
                continue
            price = country_prices.get(service_code)
            if not isinstance(price, dict):
                continue
            metadata = countries.get(str(raw_id)) or {}
            if not isinstance(metadata, dict):
                metadata = {}
            try:
                country_id = int(metadata.get("id", raw_id))
            except (TypeError, ValueError):
                continue
            rows.append({
                "id": country_id,
                "name_zh": str(metadata.get("chn") or "").strip(),
                "name_en": str(metadata.get("eng") or "").strip(),
                "cost": price.get("cost"),
                "count": price.get("count"),
            })
        rows.sort(key=lambda row: (
            str(row.get("name_en") or row.get("name_zh") or "").lower(),
            int(row["id"]),
        ))
        return rows

    def _log_price_snapshot(self) -> None:
        snapshot = self.get_price_snapshot()
        if not snapshot:
            return
        cost = snapshot.get("cost")
        count = snapshot.get("count")
        detail = f"平台聚合参考报价={cost} USD 总库存={count}"
        if self.provider_ids:
            detail += (
                f"；指定供应商={self.provider_ids}"
                "（聚合报价不代表指定供应商价格档）"
            )
            if self.max_price:
                detail += f" maxPrice={self.max_price}"
            log.warning("  [smsbower] %s", detail)
            return
        if self.max_price:
            try:
                below_price = float(self.max_price) < float(cost)
            except (TypeError, ValueError):
                below_price = False
            if below_price:
                log.warning(
                    "  [smsbower] %s；配置 maxPrice=%s 低于当前报价",
                    detail,
                    self.max_price,
                )
                return
            detail += f" maxPrice={self.max_price}"
        log.warning("  [smsbower] %s", detail)

    def _request_params(self, action: str) -> dict:
        params = {
            "action": action,
            "service": self.service,
            "country": self.country,
        }
        if self.operator:
            params["operator"] = self.operator
        if self.max_price:
            # This setting is a ceiling. SMSBower's ``fixPrice`` selects an
            # exact price tier, so values just above the cheapest tier can
            # incorrectly return NO_NUMBERS. ``maxPrice`` has the intended
            # <= semantics and is accepted by its handler API.
            params["maxPrice"] = self.max_price
        if self.provider_ids:
            if "smsbower.app" in self.base_url:
                params["priorityProviderIds"] = self.provider_ids
            else:
                params["providerIds"] = self.provider_ids
        if self.except_provider_ids:
            params["exceptProviderIds"] = self.except_provider_ids
        if self.phone_exception:
            params["phoneException"] = self.phone_exception
        if self.ref:
            params["ref"] = self.ref
        return params

    def _parse_activation_json(self, text: str) -> dict | None:
        data = self._json_dict(text)
        if not data:
            return None
        act_id = data.get("activationId") or data.get("activation_id") or data.get("id")
        phone = data.get("phoneNumber") or data.get("phone") or data.get("number")
        if not act_id or not phone:
            return None
        country = data.get("countryCode") or data.get("country") or self.country
        return {
            "id": str(act_id).strip(),
            "phone": self._to_e164(self.country, str(phone).strip()),
            "provider": self.name,
            "service": self.service,
            "country": country,
            "statusAction": "getStatus",
            "activationCost": data.get("activationCost"),
            "canGetAnotherSms": data.get("canGetAnotherSms"),
            "raw": text,
            "raw_json": data,
        }

    def _handle_request_error(self, text: str) -> bool:
        """Return True when the error is final and should stop fallback attempts."""
        data = self._json_dict(text)
        if data:
            msg = str(data.get("message") or data.get("error") or data.get("status") or "")
            up = msg.upper()
        else:
            up = text.strip().upper()
        if not up:
            return False
        if "NO_NUMBERS_INCREASE_MAX_PRICE" in up:
            log.warning(f"  [smsbower] 当前限价内无号，可调高 SMSBOWER_MAX_PRICE: {text[:120]}")
            self._log_price_snapshot()
            return True
        if "NO_NUMBERS" in up:
            log.warning(f"  [smsbower] 当前无可用号 (country={self.country} service={self.service})")
            self._log_price_snapshot()
            return True
        if "NO_BALANCE" in up:
            log.error(f"  [smsbower] 余额不足，当前余额={self.get_balance()}")
            return True
        if "BAD_KEY" in up or "NO ACCESS" in up:
            log.error("  [smsbower] api_key 无效")
            return True
        if any(flag in up for flag in ("BAD_SERVICE", "WRONG_SERVICE", "BAD_ACTION")):
            log.error(f"  [smsbower] 服务/动作配置错误: {text[:120]}")
            return True
        if up.startswith("ERROR_SQL") or up.startswith("BANNED"):
            log.warning(f"  [smsbower] 平台错误: {text[:120]}")
            return True
        return False

    def request_phone(self) -> dict | None:
        frontend_priced = bool("smsbower.app" in self.base_url and (self.max_price or self.provider_ids))
        if frontend_priced:
            actions = ["getNumber", "getNumberV2"]
        else:
            actions = ["getNumberV2", "getNumber"] if self.use_v2 else ["getNumber", "getNumberV2"]
        for action in actions:
            params = self._request_params(action)
            try:
                text = self._http_get(params)
            except Exception as e:
                log.warning(f"  [smsbower] {action} err: {e}")
                continue
            log.info(f"  [smsbower] {action} response: {text[:120]}")

            activation = self._parse_activation_json(text)
            if activation:
                return activation

            m = re.match(r"^ACCESS_NUMBER:([^:]+):(.+)$", text, re.I)
            if m:
                act_id = m.group(1).strip()
                phone = self._to_e164(self.country, m.group(2).strip())
                return {
                    "id": act_id,
                    "phone": phone,
                    "provider": self.name,
                    "service": self.service,
                    "country": self.country,
                    "statusAction": "getStatus",
                    "raw": text,
                }

            if self._handle_request_error(text):
                return None
            # 不认识的就回去试另一个 action
        return None

    def wait_otp(self, activation: dict, deadline_s: int = 120,
                 poll: float = 5.0,
                 exclude_codes: Iterable[str] | None = None) -> str | None:
        if not activation or not activation.get("id"):
            return None
        excluded = _normalize_codes(exclude_codes)
        action = activation.get("statusAction") or "getStatus"
        if action == "getStatusV2":
            action = "getStatus"
        end = time.time() + deadline_s
        last_seen = ""
        while time.time() < end:
            try:
                text = self._http_get({"action": action, "id": activation["id"]})
            except Exception as e:
                log.debug(f"  [smsbower] wait_otp err: {e}")
                time.sleep(poll)
                continue

            if text != last_seen:
                log.info(f"  [smsbower] {action}: {text[:120]}")
                last_seen = text

            up = text.strip().upper()
            if up.startswith("STATUS_OK:"):
                # STATUS_OK:<code>
                code = text.split(":", 1)[1].strip()
                m = re.search(r"\b(\d{4,8})\b", code)
                if m:
                    parsed = m.group(1)
                    if parsed not in excluded:
                        return parsed
                elif code and code not in excluded:
                    return code
                time.sleep(poll)
                continue

            if (up == "STATUS_WAIT_CODE"
                    or up == "STATUS_WAIT_RESEND"
                    or up.startswith("STATUS_WAIT_RETRY:")):
                time.sleep(poll)
                continue

            if up in ("STATUS_CANCEL", "STATUS_CANCELLED", "STATUS_FINISH"):
                log.warning("  [smsbower] activation 已取消")
                return None

            # 部分兼容平台可能返回 JSON: {status:'STATUS_OK', code:'...'}
            data = self._json_dict(text)
            if data:
                st = str(data.get("status") or data.get("Status") or "").upper()
                if st == "STATUS_OK" or data.get("code") or data.get("sms"):
                    c = str(data.get("code") or data.get("sms") or data.get("message") or "")
                    m = re.search(r"\b(\d{4,8})\b", c)
                    if m and m.group(1) not in excluded:
                        return m.group(1)
                    time.sleep(poll)
                    continue
                elif st in ("STATUS_WAIT_CODE", "STATUS_WAIT_RESEND") or st.startswith("STATUS_WAIT_RETRY"):
                    time.sleep(poll)
                    continue
                elif st in ("STATUS_CANCEL", "STATUS_CANCELLED", "STATUS_FINISH"):
                    return None

            time.sleep(poll)
        return None

    def _set_status(self, activation: dict, status_code: int) -> bool:
        if not activation or not activation.get("id"):
            return False
        try:
            text = self._http_get({
                "action": "setStatus",
                "id": activation["id"],
                "status": status_code,
            })
        except Exception as e:
            log.debug(f"  [smsbower] setStatus({status_code}) err: {e}")
            return False
        log.info(f"  [smsbower] setStatus({status_code}): {text[:80]}")
        up = text.strip().upper()
        if up.startswith("ACCESS_"):
            return True
        data = self._json_dict(text)
        if data:
            status = str(data.get("status") or data.get("message") or "").upper()
            return status.startswith("ACCESS_") or status in ("1", "TRUE", "OK")
        return False

    def complete(self, activation: dict) -> bool:
        return self._set_status(activation, self.STATUS_OK_FINISH)

    def cancel(self, activation: dict) -> bool:
        return self._set_status(activation, self.STATUS_CANCEL)

    def mark_sent(self, activation: dict) -> bool:
        return self._set_status(activation, self.STATUS_READY)

    def request_resend(self, activation: dict) -> bool:
        return self._set_status(activation, self.STATUS_RESEND)


# ============================================================
#  Factory
# ============================================================

def _normalize_codes(codes: Iterable[str] | None) -> set[str]:
    return {str(code).strip() for code in (codes or ()) if str(code).strip()}


def _configured_value(name: str, fallback: object = "") -> object:
    value = getattr(config, name, None)
    if value is None or (isinstance(value, str) and not value.strip()):
        return fallback
    return value


def get_sms_provider(card: dict | None = None,
                     purpose: str | None = None) -> SmsProviderBase | None:
    """根据 config.SMS_PROVIDER 选 provider 实例。

    Args:
        card: 当前卡（可选）。62-us 模式下从卡里取 sms_url，smsbower 模式下忽略。
        purpose: ``"openai"`` 时优先读取 FREE_* 独立号池配置；其他值保持
            原有全局 SMS 配置行为。

    Returns:
        provider 实例，配置缺失时返回 None。
    """
    is_openai = str(purpose or "").strip().lower() == "openai"
    global_name = _configured_value("SMS_PROVIDER", "62us")
    free_provider_name = str(getattr(config, "FREE_SMS_PROVIDER", "") or "").strip()
    configured_name = _configured_value("FREE_SMS_PROVIDER", global_name) if is_openai else global_name
    name = str(configured_name or "62us").strip().lower()

    if name in ("62us", "62-us", "62us.com", "a.62-us.com"):
        sms_url = ""
        if card:
            sms_url = card.get("sms_url") or ""
        sms_url = sms_url or getattr(config, "PAYPAL_SMS_URL", "")
        if not sms_url:
            log.warning("  [sms_provider] 62us 模式但没拿到 sms_url")
            return None
        return Sms62UsProvider(sms_url=sms_url)

    if name in ("smsbower", "smshub", "sms-activate", "tigersms", "tiger-sms",
                "smsverified", "onlinesim", "hero-sms", "herosms"):
        def setting(suffix: str, default: object = "", *, independent_filter: bool = False) -> object:
            global_value = _configured_value(f"SMSBOWER_{suffix}", default)
            if not is_openai:
                return global_value
            # Once an explicit Free/OpenAI provider is selected, blank filter
            # fields mean "no filter". Inheriting a PayPal provider ID or price
            # silently narrows a different country/service and causes false
            # NO_NUMBERS responses.
            if independent_filter and free_provider_name:
                value = getattr(config, f"FREE_SMSBOWER_{suffix}", "")
                return "" if value is None else value
            return _configured_value(f"FREE_SMSBOWER_{suffix}", global_value)

        api_key = str(setting("API_KEY", "") or "").strip()
        if not api_key:
            key_name = "FREE_SMSBOWER_API_KEY/SMSBOWER_API_KEY" if is_openai else "SMSBOWER_API_KEY"
            log.error(f"  [sms_provider] {key_name} 未配置")
            return None
        # 不同平台默认 base_url 不一样
        defaults = {
            "smsbower":     SmsActivateProvider.DEFAULT_BASE_URL,
            "smshub":       "https://smshub.org/stubs/handler_api.php",
            "tigersms":     "https://api.tiger-sms.com/stubs/handler_api.php",
            "tiger-sms":    "https://api.tiger-sms.com/stubs/handler_api.php",
            "smsverified":  "https://activate-api.smsverified.com/stubs/handler_api.php",
            "onlinesim":    "https://onlinesim.io/stubs/handler_api.php",
            "hero-sms":     "https://hero-sms.com/stubs/handler_api.php",
            "herosms":      "https://hero-sms.com/stubs/handler_api.php",
        }
        base_url = str(setting("BASE_URL", defaults.get(name, defaults["smsbower"])) or defaults.get(name, defaults["smsbower"])).strip()
        if is_openai:
            service = str(_configured_value("FREE_SMSBOWER_SERVICE", "dr") or "dr").strip()
        else:
            service = str(setting("SERVICE", "pp") or "pp").strip()
        country = setting("COUNTRY", 12)
        max_price = setting("MAX_PRICE", "", independent_filter=True) or ""
        operator = str(setting("OPERATOR", "", independent_filter=True) or "").strip()
        use_v2 = setting("USE_V2", True)
        provider_ids = setting("PROVIDER_IDS", "", independent_filter=True) or ""
        except_provider_ids = setting("EXCEPT_PROVIDER_IDS", "", independent_filter=True) or ""
        phone_exception = setting("PHONE_EXCEPTION", "", independent_filter=True) or ""
        ref = setting("REF", "", independent_filter=True) or ""
        return SmsActivateProvider(
            api_key=api_key,
            base_url=base_url,
            service=service,
            country=country,
            max_price=max_price,
            operator=operator,
            use_v2=use_v2,
            provider_ids=provider_ids,
            except_provider_ids=except_provider_ids,
            phone_exception=phone_exception,
            ref=ref,
        )

    log.error(f"  [sms_provider] 未知 SMS_PROVIDER: {name!r}")
    return None
