"""窗口布局 helper：按 worker_index 算窗口大小 + 位置。

每个 worker 同时开两个窗口：
  - 本地 Chromium（注册阶段）   → 上半屏
  - BitBrowser（支付阶段）      → 下半屏

布局（屏幕 1280x800 logic px，4 worker 双行能塞下）：
  worker 0: chromium (0,0)         bitbrowser (0,420)
  worker 1: chromium (510,0)       bitbrowser (510,420)
  worker 2: chromium (1020,0)      bitbrowser (1020,420)
  worker 3: chromium (0,820)       bitbrowser (510,820)   ← 再环绕

用法：
    from window_layout import worker_slot, chromium_geom, bitbrowser_geom

    idx = worker_slot()   # 从 env WORKER_INDEX 读，没设就 0
    w, h, x, y = chromium_geom(idx)
"""
from __future__ import annotations

import os

# 每个窗口尺寸（Chrome/Chromium 最小可接受 ≈ 480×400）
# 6 并发 3×2 网格在 MacBook Air 13" (1470×920 logical) 刚好铺满
WIN_W = 480
WIN_H = 410

# 间距（窗口紧贴）
COL_GAP = 480      # 等于窗口宽度，窗口贴边
ROW_GAP = 410      # 等于窗口高度

# 一行最多几个窗口（3×2 网格 = 6 并发）
COLS = 3


def worker_slot() -> int:
    """从 WORKER_INDEX env 读 slot，没设就 0。"""
    try:
        return int(os.environ.get("WORKER_INDEX", "0"))
    except ValueError:
        return 0


def chromium_geom(idx: int) -> tuple[int, int, int, int]:
    """返回 (w, h, x, y)。每个 worker 一个窗口位置。"""
    col = idx % COLS
    row = idx // COLS
    x = col * COL_GAP
    y = row * ROW_GAP
    return WIN_W, WIN_H, x, y


def bitbrowser_geom(idx: int) -> tuple[int, int, int, int]:
    """返回 (w, h, x, y)。BitBrowser 跟同 worker 的 chromium 重叠（叠在上面）。
    注册阶段 chromium 是活跃的，支付阶段 bitbrowser 上来覆盖；视觉上每个 worker 只占一个槽位。"""
    return chromium_geom(idx)
