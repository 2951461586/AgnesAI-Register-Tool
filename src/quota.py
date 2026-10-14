"""本地注册配额保护（保守闸门，**不是**服务端权威计量）。

它解决什么问题
--------------
Agnes 实测有一层**按 IP 的注册尝试限流**：`/api/verification` 与
`/api/user/register` 共用一张计数，超了返回
`400 Too many registration attempts from this IP`（见 `common/config`）。

那张窗口是**服务端**的、按出口的、自愈的（`pipeline` 侧有滑动窗口 +
换出口重试来对付它）。本模块管的是**另一件事**：本地累计计数，
防"手滑跑 500 个"这类爆炸半径。

🔴 三条必须写清楚的限制
----------------------
1. **它不是服务端阈值**。默认上限 30 是"没有证据时的保守猜测"，不是测出来的
   触顶点。它的作用只是"防手滑"，不声称等于服务端上限。
2. **它是本地的**：换机器 / 删 `state/` 即重置。所以它拦得住"我自己跑太猛"，
   拦不住"别人也在跑"。
3. **它只在成功时计数**（`used` = 窗口内 status=success 的条数）。
   失败尝试另计（`attempts`），供复盘看"打了几枪"，但不进闸门 ——
   否则一次服务端抖动就能把额度白白烧掉。

窗口取 24h 的理由
-----------------
注册尝试限流的**窗口长度尚未测准**（见 docs/protocol.md）。既然真正的节流
由 `pipeline` 的按出口滑动窗口负责，本闸门的窗口取 24h 只保证
"一天内不会无限打"。等测准服务端窗口，再按实测改这里的默认值。
"""

import math
import time
from dataclasses import dataclass
from pathlib import Path

from common import config

from . import jsonlstore


class QuotaExceeded(RuntimeError):
    """本地闸门拦下了这一批。**一个请求都没发**。"""


# 环境变量覆盖名。测试靠它把状态钉在 tmp 目录 ——
# 🔴 不隔离的后果很具体：跑一次测试就往**真实** `state/` 写记录，而那个文件会
#    影响**下一次真实跑批**的闸门（实测踩过：测试把额度吃掉）。
STATE_ENV = "AR_QUOTA_STATE"

_STORE = jsonlstore.JsonlStore(
    STATE_ENV,
    Path(__file__).resolve().parents[1] / "state" / "register_quota.jsonl",
    label="配额计数",
)

# `_as_ts` 保留为模块级别名（宽容取时间戳，见 `jsonlstore.as_ts`）。
_as_ts = jsonlstore.as_ts


def state_path() -> Path:
    """计数文件。`AR_QUOTA_STATE` 可覆盖（自检脚本靠它做隔离）。"""
    return _STORE.path()


@dataclass
class QuotaStatus:
    used: int
    attempts: int
    limit: int
    window_h: float
    oldest_ts: float

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    @property
    def left(self) -> int:
        return max(0, self.limit - self.used)

    def minutes_until_free(self, now: float | None = None) -> int:
        """还要等几分钟才能空出一个名额（`oldest_ts` 滑出窗口那一刻）。

        窗口内一条记录都没有时返回 0。取整**向上**（`ceil`）—— 报"还差 0 分钟"
        但闸门仍然拦着，是最让人困惑的一种输出。

        ⚠ 不能用 `int(remain // 60) + 1`：`remain` 恰好是 60 的整数倍时会多报
          一分钟（实测：`oldest_ts == now`、窗口 1h 时报 61 而不是 60）。
        """
        if not self.oldest_ts:
            return 0
        now = time.time() if now is None else now
        remain = (self.oldest_ts + self.window_h * 3600) - now
        if remain <= 0:
            return 0
        return int(math.ceil(remain / 60.0))

    def describe(self) -> str:
        return (
            f"{self.used}/{self.limit} 个（窗口 {self.window_h:g}h，"
            f"失败尝试 {self.attempts - self.used} 次）"
        )


def status(now: float | None = None) -> QuotaStatus:
    """窗口内的计数。`now` 只为测试可注入，生产不该传。"""
    now = time.time() if now is None else now
    cutoff = now - config.REG_QUOTA_WINDOW_H * 3600
    entries = [e for e in _STORE.read() if _as_ts(e.get("ts")) >= cutoff]
    used = [e for e in entries if e.get("ok")]
    oldest = min((_as_ts(e.get("ts")) for e in used), default=0.0)
    return QuotaStatus(
        used=len(used),
        attempts=len(entries),
        limit=config.REG_QUOTA_MAX,
        window_h=config.REG_QUOTA_WINDOW_H,
        oldest_ts=oldest,
    )


def check(*, ignore: bool = False, now: float | None = None) -> QuotaStatus:
    """闸门。超了抛 `QuotaExceeded`（除非 `ignore`）。

    🔴 `ignore` 是**显式**的：默认拦。理由同 Intern —— "以为服务端恢复了"
      而实际没恢复时，放行会白烧一整批（而且每次都加深封禁）。
    """
    st = status(now=now)
    if st.exhausted and not ignore:
        raise QuotaExceeded(
            f"本地配额已达上限：{st.describe()}。"
            f"还要等约 {st.minutes_until_free(now)} 分钟才有名额。\n"
            f"  这是**本地保护**（src/quota.py），不是服务端拒绝 —— 未发出任何请求。\n"
            f"  选项：① 等窗口滑出；② 调大 AR_REG_QUOTA_MAX；\n"
            f"        ③ 确信服务端没在拦，再加 --ignore-quota。"
        )
    return st


def record(email: str, *, ok: bool, ts: float | None = None, note: str = "") -> None:
    """追加一条记录。**写失败只告警，不抛**。

    ⚠ 与台账相反：计数文件丢了只影响下一轮的保守程度，不该让整批任务失败
      （`proxypool` / `quota` 是同一策略，`ledger` 是另一策略）。
    """
    ok_written = _STORE.append(
        {
            "ts": round(time.time() if ts is None else ts, 3),
            "email": email,
            "ok": bool(ok),
            "note": note[:120],
        }
    )
    if ok_written:
        _maybe_compact()


def _maybe_compact() -> None:
    """行数远多于窗口所需时，重写文件只留窗口内的条目。

    🔴 只在**行数 > 4×上限**时才压缩：每次成功都重写一遍文件会让
      "写"变成 O(n) 并且放大"写到一半被杀"的窗口。
    """
    entries = _STORE.read()
    if len(entries) <= max(200, config.REG_QUOTA_MAX * 4):
        return
    cutoff = time.time() - config.REG_QUOTA_WINDOW_H * 3600
    keep = [e for e in entries if _as_ts(e.get("ts")) >= cutoff]
    _STORE.rewrite(keep)


def shortfall_hint(left: int, count: int, ignore: bool) -> str:
    """这一批跑不满时给一句**真话**。

    🔴 判据必须读 `ignore`：Intern 踩过 —— `--ignore-quota` 打开时提示仍然
      打"这一批会全部被跳过"，而实测 47/50 成功。**提示说假话**比没有提示更坏。
    """
    if count <= left or ignore:
        return ""
    return (
        f"  ⚠ 本批要 {count} 个，本地窗口内只剩 {left} 个名额 —— "
        f"多出来的会以 `quota_guard` 跳过（**不发请求**）。"
    )
