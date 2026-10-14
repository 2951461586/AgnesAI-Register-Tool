"""槽位代理池：把"一个出口"变成"N 个出口"。

为什么需要它（**理由是实测事实，不是"多出口更安全"**）
--------------------------------------------------
Agnes 的注册尝试限流实测是**按客户端 IP** 的（`/api/verification` 与
`/api/user/register` 共用一张计数，消息
`Too many registration attempts from this IP`）。所以：

    --producers 4                → 4 个账号在同一个出口上各发一次验证码
                                   ⇒ 撞的是**同一张**窗口，第 N 个起 400
    --producers 4 + 5 个出口      → 每个出口各有一张窗口 ⇒ 吞吐 ≈ ×5

只调并发是没用的 —— 那是同一张窗口里的名额。**出口数量才是那个乘数**，
这一点与 Intern-Register-Tool 的结论同形（那边也写着"真正的约束是有多少个
不同出口 IP"），理由也同形：都是 **IP 维度的注册节流**。

🔴 刻意**不做**冷却 / 指数退避（对比 Intern 的 `IR_PROXY_COOLDOWN`）
-----------------------------------------------------------------
那边的退避是为"出口已被封"设计的：被封的出口再打只是加深封禁，所以要晾着。
Agnes 这边观察到的是**窗口限流**（自愈，不是封禁），而窗口长度还没测准 ——
在没有证据时加冷却就是猜，而且它会直接降低吞吐（把可用出口晾着）。
所以这里只**记账**（`ok` / `failed` / `last_error`），让报告能回答
"哪个出口失败多"；等真观测到封禁再谈退避。

⚠ 也不做"健康检查式"的预检：`acquire()` 拿到一个已经死掉的端口时，
失败会**记在那个账号的账上**（`error_kind=network`），报告里能看出来。
预检会多打一次请求，而且"TCP 能连"并不等于"代理可用"（Intern 实测：
本机 Clash TUN 下连一个不可达端口只要 0.02s —— 那是连到本地虚拟网卡）。
"""

import threading
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass

from src.redact import redact_url


class SlotPoolEmpty(RuntimeError):
    """等不到空闲槽位（超时）。调用方按失败记账，**不是**整批终止。"""


@dataclass
class SlotStats:
    url: str
    used: int = 0
    ok: int = 0
    failed: int = 0
    last_error: str = ""
    last_used: float = 0.0

    @property
    def label(self) -> str:
        """给报告用的短标签 —— 🔴 **不能泄凭据**。

        旧实现是 `url.rsplit(":", 1)[-1]`，对 `http://127.0.0.1:7901` 恰好
        给出 `7901`（想要的行为），但对 `host:port:user:pass`（`config.proxies()`
        接受的第二种形态）最后一段是**密码** —— 实测泄漏过：收尾报告里
        每个槽位的"标签"就是代理密码，而报告是最容易被贴进 issue / 截图的东西。

        规则：
          · 末段是**纯数字**（端口）⇒ 就用它（保留原来的好行为）；
          · 否则退回**脱敏后**的 `host:port`，并用一个**稳定短哈希**区分同 host
            的多个槽位 —— 否则 10 个槽位在报告里长得一模一样，这一栏就废了。
        """
        shown = redact_url(self.url)
        tail = shown.rsplit(":", 1)[-1]
        if tail.isdigit():
            return tail
        hostpart = shown.split("://", 1)[-1].split("@")[-1]
        base = ":".join(hostpart.split(":")[:2]) or hostpart
        return f"{base}#{zlib.crc32(self.url.encode('utf-8')) % 1000:03d}"


class SlotPool:
    """线程安全的槽位池。空池 = 单代理行为（`acquire()` 返回 `None`）。

    🔴 空池**不报错**：没配 `AR_PROXY_SLOTS` 时整条链路照跑（走 `AR_PROXY`
      或直连）。让"没配池子"变成启动失败，会让这个可选功能变成必填。
    """

    def __init__(self, slots, *, timeout: float = 240.0):
        self._all = list(slots or [])
        self._free = list(self._all)
        self._stats = {u: SlotStats(url=u) for u in self._all}
        self._timeout = float(timeout)
        self._cv = threading.Condition()

    def __len__(self) -> int:
        return len(self._all)

    def __bool__(self) -> bool:
        return bool(self._all)

    # ── 借出 / 归还 ────────────────────────────────────────────
    def acquire(self, timeout: float | None = None, prefer=None):
        """借一个槽位（**最少使用优先**）。空池返回 `None`。

        🔴 最少使用优先，而不是"按顺序取第一个空闲的"：注册限流是**按出口**的，
          轮询取用会让出口 1 在别人都闲着的时候先把窗口用满，
          总吞吐反而更低。按使用次数排序才能把 N 张窗口都用上。

        ⚠ 排序键里带 `last_used`：`used` 相同时让"最久没用过的"先上，
          这样新加的槽位不会一直排在队尾。

        🔴 `prefer(url)` 的返回值被**前置**到排序键上（真值优先）。
          存在的理由是 **`used` 是进程内计数**：新进程启动时全是 0 ⇒ 排序
          退化成**列表顺序** ⇒ 它会先借到上一批刚把窗口打满的那几个，
          白转一圈才发现打不动。
          实测（2026-10-14）：一批 7 个跑完后立刻再跑，4 次尝试全撞在满窗口上，
          以 `quota_guard` 收场 —— 而池子里其实还有 3 个出口是空的。
          调用方传 `prefer` 去查**落盘**的窗口状态（`windowstore`），
          把"进程内记忆"补成"跨进程记忆"。

        ⚠ 回调是在**池锁内**被调用的（排序发生在 `with self._cv` 里）。
          所以它必须是**纯查询**（本项目传的是 `wait_time()`，会读 `state/` 的
          小文件）。锁内做 I/O 是刻意的取舍：放锁排序会让两个线程
          同时看到"这个槽位可用"，然后一起拿走。
        """
        if not self._all:
            return None
        limit = self._timeout if timeout is None else float(timeout)
        deadline = time.time() + limit
        with self._cv:
            while not self._free:
                remain = deadline - time.time()
                if remain <= 0:
                    raise SlotPoolEmpty(
                        f"等 {limit:.0f}s 没等到空闲槽位（{len(self._all)} 个都在用）"
                    )
                self._cv.wait(remain)
            if prefer is None:
                self._free.sort(key=lambda u: (self._stats[u].used, self._stats[u].last_used))
            else:
                self._free.sort(
                    key=lambda u: (
                        not prefer(u),
                        self._stats[u].used,
                        self._stats[u].last_used,
                    )
                )
            url = self._free.pop(0)
            self._stats[url].used += 1
            self._stats[url].last_used = time.time()
            return url

    def release(self, url, *, ok: bool | None = None, error: str = "") -> None:
        """归还槽位。`ok=None` 表示"没跑完"（不计成功也不计失败）。"""
        if not url:
            return
        with self._cv:
            st = self._stats.get(url)
            if st is not None:
                if ok is None:
                    pass  # 没跑完（被本地闸门拦下 / 中途抛异常）—— 不计成败
                elif ok:
                    st.ok += 1
                else:
                    st.failed += 1
                    st.last_error = str(error)[:200]
            if url not in self._free:
                self._free.append(url)
            self._cv.notify()

    @contextmanager
    def slot(self, timeout: float | None = None):
        """`with pool.slot() as url:` —— 出错也会归还（否则一个异常就把出口漏掉）。"""
        url = self.acquire(timeout)
        try:
            yield url
        finally:
            self.release(url)

    # ── 报告 ──────────────────────────────────────────────────
    def stats(self) -> list:
        return [self._stats[u] for u in self._all]

    def describe(self) -> str:
        if not self._all:
            return "未配置（单出口）"
        parts = [
            f"{s.label}:{s.used}" + (f"(✗{s.failed})" if s.failed else "") for s in self.stats()
        ]
        return f"{len(self._all)} 个出口 · " + " ".join(parts)
