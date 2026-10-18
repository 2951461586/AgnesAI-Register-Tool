"""captcha provider 的配置（TCaptcha 点选；默认开、headless）。

（从 `common/config.py` 拆出；对外仍由 `common.config` 包统一转出。）
"""

import os

from ._env import _env_bool, _env_int
from .site import CONSOLE_BASE

# ── captcha（TCaptcha 点选；默认开、headless）─────────────────────
# 🔴 2026-10-17：主流水线已接入可选 captcha provider（`src/captcha.py`）。
#    “零浏览器”不再是默认事实：默认会启一个 **headless** Chromium 解点选，
#    再把 `ticket`/`randstr` 交给 `send_code`。
#    需要可选依赖：`pip install -e ".[captcha]"`。
#    关掉：`AR_CAPTCHA_ENABLED=0`（发码会被服务端 400 拒绝）。
CAPTCHA_ENABLED = _env_bool("AR_CAPTCHA_ENABLED", True)
CAPTCHA_HEADLESS = _env_bool("AR_CAPTCHA_HEADLESS", True)
# 单次求解总超时（等 captcha 图片）。
# 🔴 默认 **45s**（曾经是 90s）：live 实测里“等图超时”占了失败的大头，
#    而 90s × 2 次重试 = 267s/账号 —— 大部分时间花在**已经坏掉的出口**上。
#    现在改成“一次 45s → 换出口重试”（换出口在 `provision` 的重试循环里做）。
CAPTCHA_TIMEOUT_S = _env_int("AR_CAPTCHA_TIMEOUT_S", 45)
# 提示图里的图标数（当前 TCaptcha 点选是 3）。
CAPTCHA_TARGETS = max(1, _env_int("AR_CAPTCHA_TARGETS", 3))
# **同一出口内**的重试次数。默认 **0** —— 重试交给 `provision` 的换出口循环，
# 那才能换到一个**可能不一样**的链路；在同一个坏出口上重开浏览器是纯浪费时间。
CAPTCHA_RETRIES = max(0, _env_int("AR_CAPTCHA_RETRIES", 0))
# **同出口**重解次数。默认 **2 次尝试**（= 重解 1 次）。
# 🔴 实测定这个值：单次求解成功率 ~50%，且失败与出口**无关**（直连/代理一样），
#    所以「同出口再拿一张新题」比「换出口」便宜（不用再等 45s 超时）。
#    尝试 1→2 是把 50% 抬到 75%，2→3 只再 +12.5%，而每轮 ~20s ⇒ 收益递减。
CAPTCHA_RESOLVE_ATTEMPTS = max(1, _env_int("AR_CAPTCHA_RESOLVE_ATTEMPTS", 2))
# captcha 浏览器是否也走**槽位代理**。
# 🔴 默认 **0（直连）** —— 2026-10-17 实测（`tools/probes/probe_captcha_egress.py`）：
#    · 直连与代理的**求解成功率一样**（各 1/2），失败全是 solver 的 errorCode=50；
#    · 但直连**快一倍**（23~26s vs 49~54s）；
#    · 直连解出的 ticket 拿到**另一个出口**去发码，服务端**收了**（200）
#      ⇒ `uip` 不一致**没有被拒**（单样本，见 `docs/protocol.md` §2②）。
#    所以默认直连：省一半时间，且少一个“浏览器跑在抖动出口上”的故障面。
#    想回退成 old 行为（强绑同一出口）：AR_CAPTCHA_USE_PROXY=1。
CAPTCHA_USE_PROXY = _env_bool("AR_CAPTCHA_USE_PROXY", False)
# 入口页。留空 = `<CONSOLE_BASE>/login`。
CAPTCHA_ENTRY_URL = os.getenv("AR_CAPTCHA_ENTRY_URL", "").strip() or f"{CONSOLE_BASE}/login"
# 失败留图目录（默认空 = 不留）。设成 `.workbuddy-ai/captcha/failures` 可收集
# 失败样本用于调 solver。目录必须在 `.gitignore` 里。
CAPTCHA_DEBUG_DIR = os.getenv("AR_CAPTCHA_DEBUG_DIR", "").strip()


__all__ = [
    "CAPTCHA_ENABLED",
    "CAPTCHA_HEADLESS",
    "CAPTCHA_TIMEOUT_S",
    "CAPTCHA_TARGETS",
    "CAPTCHA_RETRIES",
    "CAPTCHA_RESOLVE_ATTEMPTS",
    "CAPTCHA_USE_PROXY",
    "CAPTCHA_ENTRY_URL",
    "CAPTCHA_DEBUG_DIR",
]
