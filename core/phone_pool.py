"""
日区/多区接码号池（跨进程文件锁）。

跟 card_pool / proxy_pool 同款机制：
  - 池来源：默认 jp_phones.txt（每行 `phone----sms_url`）
  - claim_phone() 阻塞抢一个 free 号；多 worker 并发跑互不串台
  - release_phone() 还回去
  - 锁文件：/tmp/gpt_pay_phone_<hash>.lock

设计目标：
  - 只是号码池，不做 SMS 拉取（拉码仍走 pipeline.fetch_sms_otp）
  - 跟 sms_provider.py 是不同的两套：
      sms_provider 是 apikey 自动选号（smsbower 等）
      phone_pool   是预购固定号 + 文件锁串行化

使用：
    from phone_pool import claim_phone, release_phone
    p = claim_phone(country='JP', timeout=60)
    if p:
        try:
            # p['phone']     '+8109078965056'
            # p['sms_url']   'https://vg.headone.fit/sms?...'
            # p['phone_local'] '09078965056'  (剥 +81 后本土号)
            ...
        finally:
            release_phone(p)
"""
from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("phone_pool")

LOCK_DIR = Path(tempfile.gettempdir())
LOCK_PREFIX = "gpt_pay_phone_"

# 池文件按国家拆：jp_phones.txt / us_phones.txt / ...
_POOL_FILES = {
    "JP": Path(__file__).parent / "jp_phones.txt",
    "US": Path(__file__).parent / "us_phones.txt",
    "BR": Path(__file__).parent / "br_phones.txt",
    "GB": Path(__file__).parent / "gb_phones.txt",
}


# ============================================================
#  Phone formatting
# ============================================================

def to_local_phone(phone: str, country: str = "") -> str:
    """E.164 phone → PayPal 表单本地格式。

    - US '+12025550100' → '2025550100'  （去 +1，10 位）
    - JP '+8109078965056' / '+819078965056' → '09078965056' （去 +81，前面补 0；最终 11 位 0XXXXXXXXXX）
    - BR '+5511987654321' → '11987654321' （去 +55，保留 DDD + 9 位手机号）
    - GB '+447911123456' → '7911123456'（去 +44 和本地前导 0）
    - 其他国家：去 +，保留 digits

    PayPal guest 表单 phone 字段会按当前 country select 自动加国码前缀显示，
    所以这里要给它本土格式（不带国码）。
    """
    s = str(phone or "").strip()
    if not s:
        return ""
    digits = "".join(ch for ch in s if ch.isdigit())
    cc = (country or "").upper().strip()

    # JP 本土号：09xxxxxxxxx 共 11 位 / 080/090/070/050 开头
    # E.164 是 +81 + 9XXXXXXXXX (10 位，去掉本土的前导 0)
    # 输入可能是：
    #   +8109078965056        — 错误格式但也常见，digits=8109078965056 (13 位，前面 81+09078...)
    #   +819078965056         — 标准 E.164，digits=819078965056 (12 位)
    #   8109078965056 / 09078965056 — 用户填的本土号
    if cc == "JP" or s.startswith("+81") or digits.startswith("81"):
        # 去掉国际 81 前缀
        d = digits
        if d.startswith("81"):
            d = d[2:]
        # PayPal JP guest 表单 phone 字段：表单 UI 已有 "+81" 前缀，
        # 只需输入后面的部分（如 7094653787 / 9078965056），**不加前导 0**。
        # 如果号码本身以 0 开头（旧式本土格式 09xxxxxxxxx），去掉前导 0。
        if d.startswith("0"):
            d = d[1:]
        return d

    # US (default)
    if cc == "US" or s.startswith("+1"):
        d = digits
        if d.startswith("1") and len(d) == 11:
            d = d[1:]
        return d

    # BR 本土号：DDD(2) + 手机 9 位，常见 E.164 是 +55 11 98765-4321。
    # 表单国家为 BR 后通常已有 +55 前缀，所以只填 11987654321。
    if cc == "BR" or s.startswith("+55") or digits.startswith("55"):
        d = digits
        if d.startswith("55") and len(d) >= 12:
            d = d[2:]
        return d

    # UK PayPal 表单已显示 +44，只填写 national significant number。
    if cc == "GB" or s.startswith("+44"):
        d = digits
        if d.startswith("44"):
            d = d[2:]
        if d.startswith("0"):
            d = d[1:]
        return d

    # 其它：仅返回数字
    return digits


# ============================================================
#  Pool loading
# ============================================================

def _load_pool(country: str) -> list[dict]:
    """从 <cc>_phones.txt 读号码池。"""
    cc = (country or "JP").upper()
    f = _POOL_FILES.get(cc) or (Path(__file__).parent / f"{cc.lower()}_phones.txt")
    if not f.exists():
        return []
    out = []
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) < 2:
            log.warning(f"  [phone_pool] {f.name} 第 {i+1} 行格式错: {line[:80]}")
            continue
        phone = parts[0].strip()
        sms_url = parts[1].strip()
        if not phone or not sms_url:
            continue
        out.append({
            "country": cc,
            "phone": phone,
            "phone_local": to_local_phone(phone, cc),
            "sms_url": sms_url,
        })
    return out


# ============================================================
#  Claim / release
# ============================================================

_held_locks: dict[str, object] = {}


def pick_phone(country: str = "JP") -> Optional[dict]:
    """挑一个号但**不加独占锁**（一个号可多账号共享，跟 US PAYPAL_PHONE 同逻辑）。

    多个 worker 可同时拿到同一个号；真正的 SMS 拉取冲突由 pipeline.fetch_sms_otp
    里基于 sms_url 的短锁串行化（只锁拉码那几秒），不会阻塞整个账号流程。

    池里有多个号时随机挑一个分摊负载。返回 None 表示池为空（调用方回退到 config）。
    用 pick_phone 拿到的号**不需要** release（没加锁）。
    """
    pool = _load_pool(country)
    if not pool:
        log.warning(f"  [phone_pool] {country} 池为空（{country.lower()}_phones.txt 不存在或全空）")
        return None
    import random as _rnd
    p = _rnd.choice(pool)
    log.info(f"  [phone_pool] 选用 {country} 号 {p['phone']}（local={p['phone_local']}，共享模式）")
    return dict(p)


def _lock_key(phone: str) -> str:
    return hashlib.md5(phone.encode("utf-8")).hexdigest()[:16]


def _lock_path(phone: str) -> Path:
    return LOCK_DIR / f"{LOCK_PREFIX}{_lock_key(phone)}.lock"


def claim_phone(country: str = "JP", timeout: float = 60) -> Optional[dict]:
    """阻塞抢一个空闲号。timeout 秒内拿不到返回 None。"""
    pool = _load_pool(country)
    if not pool:
        log.warning(f"  [phone_pool] {country} 池为空（{country.lower()}_phones.txt 不存在或全空）")
        return None
    log.info(f"  [phone_pool] {country} 池大小: {len(pool)}")

    deadline = time.time() + timeout
    import random as _rnd
    while time.time() < deadline:
        shuffled = list(pool)
        _rnd.shuffle(shuffled)
        for p in shuffled:
            phone = p["phone"]
            if phone in _held_locks:
                continue
            lp = _lock_path(phone)
            try:
                fp = open(lp, "w")
                fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fp.write(f"{os.getpid()}\n{time.strftime('%H:%M:%S')}\n{phone[-6:]}")
                fp.flush()
                _held_locks[phone] = fp
                log.info(f"  [phone_pool] 申请到 {country} 号 {phone}（local={p['phone_local']}）")
                return dict(p)
            except (IOError, OSError):
                try:
                    fp.close()
                except Exception:
                    pass
                continue
        log.info(f"  [phone_pool] {country} 全部号码被占（{len(pool)}），等 5s 重试")
        time.sleep(5)
    return None


def release_phone(phone_bundle: dict | None):
    if not phone_bundle:
        return
    phone = phone_bundle.get("phone")
    fp = _held_locks.pop(phone, None)
    if fp is None:
        return
    try:
        fcntl.flock(fp, fcntl.LOCK_UN)
        fp.close()
    except Exception:
        pass
    try:
        _lock_path(phone).unlink(missing_ok=True)
    except Exception:
        pass
    log.info(f"  [phone_pool] 已释放号 {phone}")


def stats(country: str = "JP") -> dict:
    """返回当前号池占用情况。"""
    pool = _load_pool(country)
    rows = []
    for p in pool:
        phone = p["phone"]
        in_use = False
        try:
            fp = open(_lock_path(phone), "w")
            try:
                fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fp, fcntl.LOCK_UN)
            except (IOError, OSError):
                in_use = True
            fp.close()
        except Exception:
            pass
        rows.append({"phone": phone, "in_use": in_use})
    return {"country": country, "total": len(pool), "phones": rows}
