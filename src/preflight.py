"""跑批前的**链路健康预检**。

为什么做成模块（而不是只留在探针脚本里）
======================================
本会话**手工**做过 3 次"跑批前先体检链路"，其中一次正好避开坏时段 ——
当时全池 58% 失败，跑下去就是白烧 4 个域名窗口（= 一小时的产能，且
**不可再生**）。手工做意味着"想起来才做"，而想不起来的代价是那一小时。

所以这里把判据收成一份可复用的实现，供两处调用：
  · `run.py --preflight`  —— 跑批前的闸门（不通过就不发注册请求）
  · `tools/probes/probe_slots.py` —— 人工体检

🔴 判据（本会话实测总结，**别只看失败率**）
========================================
1. **看失败率，不是失败数** —— 同样多的失败数可以是"用得少、每次都失败"与
   "用得多、偶尔失败"，数一样、含义相反；
2. **看横向** —— 多数出口**一起**坏 ⇒ 链路/平台侧的坏时段，**不是**坏槽位。
   实测依据：某一轮里 10 个老槽位 17~50%，而**刚加的新代理也 42%** ——
   新出口不可能"是坏槽位"，所以那一轮的高低全是链路侧的；
3. **样本量要够** —— <10 次不能下结论。实测：同一出口 5 次 `0/5` 全 SSLError，
   几分钟后 `15/15` 全好。所以本模块对 `repeat` 小的情况会**明说"结论不可靠"**，
   而不是假装它是结论。

⚠ 探测用的是**只读**请求（`/api/user/self` 不带鉴权 ⇒ 401），
  **不消耗**注册窗口 —— 所以它可以在跑批前随便跑。
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests

from common import config

# 探测目标：只读 + 无鉴权 ⇒ 401。**不是**注册类端点，所以不消耗注册窗口。
PROBE_PATH = "/api/user/self"

# 默认"链路不健康"的阈值：全池失败率超过它就不建议跑。
DEFAULT_MAX_FAILURE = 0.20

# 默认每个出口打几次。⚠ 取得小是为了让预检足够快（11 个出口 × 3 次 ≈ 10s），
# 代价是结论的置信度低 —— 所以 `verdict()` 会在样本量不足时**主动说明**。
DEFAULT_REPEAT = 3

# 样本量到这个数才敢拿"单个出口的失败率"说事。
RELIABLE_SAMPLE = 10


def label(slot: str | None) -> str:
    """报告用的短标签：`host:port#短哈希`。

    🔴 不能直接用 `redact_url(slot)`：本项目 `.slots.txt` 的槽位是"同 host:port、
      不同 sid"，脱敏后**全都长成一样** ⇒ 表格里看不出是哪个出口在失败
      （而那正是这个模块要回答的问题）。这里复用 `SlotPool` 的同款标签逻辑。
    """
    if not slot:
        return "(直连)"
    from .proxypool import SlotStats

    return SlotStats(url=slot).label


@dataclass
class SlotHealth:
    """一个出口的体检结果。"""

    slot: str | None
    ok: int
    repeat: int
    errs: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return label(self.slot)

    @property
    def rate(self) -> float:
        return 1.0 - self.ok / self.repeat if self.repeat else 0.0


def probe_slot(slot: str | None, repeat: int = DEFAULT_REPEAT) -> SlotHealth:
    """打 `repeat` 次只读请求，统计失败率。**不消耗注册窗口**。"""
    px = config.proxies(slot) if slot else None
    ok, errs = 0, {}
    for _ in range(max(1, repeat)):
        s = requests.Session()
        # 🔴 必须关 `trust_env`，否则环境代理会**静默盖掉**槽位（见 config.apply_proxy）。
        s.trust_env = False
        s.headers.update({"User-Agent": config.USER_AGENT})
        try:
            s.get(f"{config.PLATFORM_BASE}{PROBE_PATH}", proxies=px, timeout=25)
            ok += 1
        except Exception as ex:  # noqa: BLE001
            name = type(ex).__name__
            errs[name] = errs.get(name, 0) + 1
    return SlotHealth(slot=slot, ok=ok, repeat=max(1, repeat), errs=errs)


@dataclass
class Preflight:
    """一次预检的全部结果 + 判据。"""

    results: list

    @property
    def repeat(self) -> int:
        return self.results[0].repeat if self.results else 0

    @property
    def total(self) -> int:
        return sum(r.repeat for r in self.results)

    @property
    def total_ok(self) -> int:
        return sum(r.ok for r in self.results)

    @property
    def failure_rate(self) -> float:
        return 1.0 - self.total_ok / self.total if self.total else 0.0

    @property
    def elevated(self) -> list:
        """失败率过半的出口（"疑似坏槽位"的候选）。"""
        return [r for r in self.results if r.rate >= 0.5]

    def verdict(self, *, max_failure: float = DEFAULT_MAX_FAILURE) -> str:
        """`""` = 放行；否则返回**为什么不该跑**（给人看的一句话）。"""
        if not self.results:
            return ""  # 没有出口可查（直连）⇒ 放行
        if self.failure_rate > max_failure:
            # 🔴 区分"多数一起坏"与"个别坏" —— 这是本模块最有价值的判断。
            share = len(self.elevated) / len(self.results)
            if share >= 0.5:
                why = (
                    f"**多数出口一起坏**（{len(self.elevated)}/{len(self.results)} 个失败率过半）"
                    f" ⇒ 这更像链路/平台侧的**坏时段**，换出口也没用"
                )
            else:
                why = (
                    f"全池失败率 {self.failure_rate:.0%}（{len(self.elevated)} 个出口过半）"
                    f" ⇒ 建议换时间点再跑"
                )
            return why
        return ""

    def describe(self, *, max_failure: float = DEFAULT_MAX_FAILURE) -> str:
        """给终端看的多行摘要。"""
        lines = [f"链路预检：{self.total_ok}/{self.total} 成功，失败率 {self.failure_rate:.0%}"]
        for r in sorted(self.results, key=lambda x: x.ok):
            mark = "  ← 过半失败" if r.rate >= 0.5 else ""
            lines.append(f"    {r.label:<28} {r.ok}/{r.repeat}  {r.errs or ''}{mark}")
        if self.repeat < RELIABLE_SAMPLE:
            # ⚠ 主动说明置信度，而不是让读者误以为这是结论。
            lines.append(
                f"    ⚠ 每出口只打了 {self.repeat} 次 ⇒ 样本量不足，"
                f"**单个**出口的数字不可当作结论（{RELIABLE_SAMPLE}+ 次才算）"
            )
        why = self.verdict(max_failure=max_failure)
        lines.append(f"    {'⛔ ' + why if why else '✅ 可以跑'}")
        return "\n".join(lines)


def run(
    slots, *, repeat: int = DEFAULT_REPEAT, probe=probe_slot, max_workers: int = 11
) -> Preflight:
    """并行体检每个出口。

    `probe` 是**注入点**：默认走真的 HTTP，用例传假的 —— 这样判据逻辑
    （`Preflight` / `verdict`）可以**离线**测，不必打网络。
    """
    targets = list(slots) or [None]
    with ThreadPoolExecutor(max_workers=min(len(targets), max(1, max_workers))) as ex:
        results = list(ex.map(lambda s: probe(s, repeat), targets))
    return Preflight(results=results)


def enabled_slots() -> list:
    """预检要查哪些出口：配了槽位池就用池子，否则查单代理 / 直连。"""
    try:
        return config.proxy_slots()
    except (ValueError, OSError):
        # 槽位文件读不到 ⇒ 交给 `config.validate()` 在启动阶段报错，这里不重复。
        return []


__all__ = [
    "DEFAULT_MAX_FAILURE",
    "DEFAULT_REPEAT",
    "RELIABLE_SAMPLE",
    "Preflight",
    "SlotHealth",
    "enabled_slots",
    "label",
    "probe_slot",
    "run",
]
