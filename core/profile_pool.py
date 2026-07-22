"""BitBrowser profile 池分配器（跨进程文件锁）。

每个 worker claim 一个独立 profile（已经在 BitBrowser 里手动克隆 + 装好 YesCaptcha 扩展）。
跑完释放，下次给别的 worker 用。

profile id 列表写死在这里，要扩容就在 BitBrowser UI 里再克隆几个 + 加到 PROFILE_IDS。

用法:
    from profile_pool import claim_profile, release_profile
    profile_id = claim_profile()
    if not profile_id:
        raise RuntimeError("profile 池空")
    try:
        ...用这个 id 跑流程...
    finally:
        release_profile(profile_id)
"""
from __future__ import annotations

import fcntl
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("profile_pool")

LOCK_DIR = Path(tempfile.gettempdir())
LOCK_PREFIX = "gpt_pay_profile_"

# BitBrowser profile 池（这些都是从 seq 1 工作台克隆出来的，带 YesCaptcha 扩展）
# 只放 BitBrowser 里**实际存在**的窗口 id。2026/06/01 更新：清掉失效 id，
# 同步成当前 6 个真实窗口（全部日区 JP 代理）。
# claim_profile() 会随机分配，并在分配前再次过滤掉不存在的窗口（_existing_profile_ids）。
PROFILE_IDS = [
    "934df1a321a744968d1e0b0713d4fdfd",
    "541a6b98af80483e9321e92dc54e4ebc",
    "fa9d15f2af554562bff2715c0306a706",
    "a339bd4ee0d34698affb076bc4043b9e",
    "4735db7c2352438382eabeb965b23f63",
    "6cbb0c158f5c451ab259c55d684a517a",
]


def _lock_path(pid: str) -> Path:
    return LOCK_DIR / f"{LOCK_PREFIX}{pid[:16]}.lock"


_held_locks: dict[str, object] = {}


def _existing_profile_ids() -> list[str]:
    """返回 PROFILE_IDS 里**在 BitBrowser 中实际存在**的窗口 id（保序）。

    池里写了 13 个 id，但 BitBrowser 实际可能只克隆了几个。随机分配前先过滤，
    否则可能抽到不存在的 id 导致 open 直接抛错。BitBrowser 不可用 / 查询失败时
    回退到完整 PROFILE_IDS（不阻塞）。
    """
    try:
        from browser_mgr import bb_health, bb_list_windows
        if not bb_health():
            return list(PROFILE_IDS)
        live = {w.get("id") for w in bb_list_windows()}
        existing = [pid for pid in PROFILE_IDS if pid in live]
        if existing:
            return existing
        log.warning("  [profile_pool] PROFILE_IDS 里没有一个在 BitBrowser 中存在，回退到全列表")
        return list(PROFILE_IDS)
    except Exception as e:
        log.debug(f"  [profile_pool] 查询存在窗口失败，回退全列表: {e}")
        return list(PROFILE_IDS)


def claim_profile(timeout: float = 600) -> Optional[str]:
    """阻塞申请一个空闲 profile id。

    随机打乱顺序再抢（跟 card_pool / phone_pool 同款）：
    避免多 worker / 多次单跑都从队首拿同一个窗口，实现并发时各 worker
    随机分到不同窗口。
    """
    import random as _rnd
    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates = _existing_profile_ids()
        shuffled = list(candidates)
        _rnd.shuffle(shuffled)
        for pid in shuffled:
            if pid in _held_locks:
                continue
            lp = _lock_path(pid)
            try:
                fp = open(lp, "w")
                fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fp.write(f"{os.getpid()}\n{time.strftime('%H:%M:%S')}")
                fp.flush()
                _held_locks[pid] = fp
                log.info(f"  [profile_pool] 申请到 profile {pid[:16]}... （存在窗口 {len(candidates)} 个）")
                return pid
            except (IOError, OSError):
                try:
                    fp.close()
                except Exception:
                    pass
                continue
        log.info(f"  [profile_pool] 所有可用 profile 都被占（{len(candidates)} 个存在），等 5s 重试")
        time.sleep(5)
    return None


def release_profile(profile_id: str):
    if not profile_id:
        return
    fp = _held_locks.pop(profile_id, None)
    if fp is None:
        return
    try:
        fcntl.flock(fp, fcntl.LOCK_UN)
        fp.close()
    except Exception:
        pass
    try:
        _lock_path(profile_id).unlink(missing_ok=True)
    except Exception:
        pass
    log.info(f"  [profile_pool] 已释放 profile {profile_id[:16]}...")
