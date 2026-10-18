"""滑动窗口原语：`SlidingWindow` + `WindowBusy`（线程安全、可落盘）。

（从 `src/pipeline/limiter.py` 拆出；对外仍由 `limiter` / `pipeline` 转出。）
"""

import threading
import time

from .. import windowstore as _windows


class SlidingWindow:
    """滑动窗口限速器（**线程安全**）。

    为什么需要它（实测依据，不是防御性编程）
    ------------------------------------
    Agnes 的注册尝试限流是**按 IP** 的，而 `/api/verification` 与
    `/api/user/register` **共用同一张计数**。所以本地闸门必须与平台的判据同构：

    🔴 用**滑动窗口**而不是"最小间隔"：平台限的是"窗口内最多 N 次"，
      不是"每 X 秒一次"。用固定间隔时，只要有一次请求比预期快，
      就可能把第 N+1 次挤进窗口而撞限流。滑动窗口不会出现这种偏差。

    🔴 窗口事件**落盘**（`windowstore`），`key` 非空时生效。
      为什么必须落盘：本工具的实际用法是**一天内多次短跑**，而进程内的
      窗口每次启动即清零 ⇒ 本地闸门形同不存在（实测后果：17 条撞服务端墙的
      `quota` 失败）。`key=None` 保留纯内存语义（测试 / 不需要持久化的场景）。
    """

    def __init__(self, max_events: int, window_s: float, *, key: str | None = None):
        self.max_events = max(1, int(max_events))
        self.window_s = float(window_s)
        # 空串 = 纯内存。**不落盘**（`windowstore` 对键的内容不作假设，
        # 但"没有键"这件事必须在这里就被翻译成"不写文件"）。
        self.key = key or ""
        self._times: list[float] = []
        self._lock = threading.Lock()

    def _events(self, now: float) -> list[float]:
        """窗口内的事件（**升序**）。`key` 为空 ⇒ 进程内内存。

        🔴 每次都**重新读**（而不是构造时读一次）：另一个进程可能刚写了新事件，
          而"看不到别人的事件"正是这次要修的那个缺陷。
        """
        if not self.key:
            self._times = [t for t in self._times if now - t < self.window_s]
            return self._times
        return _windows.events(self.key, self.window_s, now)

    def acquire(self, max_wait: float | None = None) -> float:
        """等到可以发一个请求为止，返回**实际等了多久**（秒）。

        ⚠ 持锁期间 `sleep`：这是有意的 —— 放锁会让两个线程同时看到
          "还有名额"然后一起发出去，限速就失效了。代价是并发度被这个
          上限压成 N/window，而那正是平台的真实上限。

        `max_wait` 给定时是**封顶**：窗口打满且还要等更久，就**不睡、
        不占名额**，直接抛 `WindowBusy`。存在的理由见那边 —— 预检与
        `acquire()` 之间别的线程可能刚好把窗口打满，没有封顶的话这一次
        就会抱着**长度未知**的窗口睡死在锁里。
        """
        with self._lock:
            now = time.time()
            times = self._events(now)
            wait = 0.0
            if len(times) >= self.max_events:
                wait = (times[0] + self.window_s) - now
                if wait > 0:
                    if max_wait is not None and wait > max_wait:
                        raise WindowBusy(
                            f"本地滑动窗口已满，还要等 {wait:.0f}s（封顶 {max_wait:g}s，等不起）",
                            wait_s=wait,
                        )
                    time.sleep(wait)
                # ⚠ 睡完必须**重新取时间与事件**：`now` 变了，而且睡着的这段时间
                #   里别的线程/进程可能又占掉了名额（后者只在落盘模式下可见）。
                now = time.time()
                times = self._events(now)
            if self.key:
                _windows.record(self.key, now)
            else:
                times.append(now)
                self._times = times
            return max(0.0, wait)

    def wait_time(self) -> float:
        """还要等多久才能发一个（0 = 现在就能发）。**不消费名额**。

        🔴 存在的理由：窗口可能是**小时级**的（长度见 `docs/protocol.md` §3），
          而 `acquire()` 会一直 `sleep` 到窗口滑出 —— 批量场景下那是灾难。
          调用方先用 `wait_time()` 看一眼：等得起就 `acquire()`，
          等不起就换维度（换出口 / 换域名）。
        """
        with self._lock:
            now = time.time()
            times = self._events(now)
            if len(times) < self.max_events:
                return 0.0
            return max(0.0, (times[0] + self.window_s) - now)

    def room(self) -> int:
        """还剩几格（**不消费**）。用于"这条账号需要 n 格，装得下吗"。"""
        with self._lock:
            return max(0, self.max_events - len(self._events(time.time())))

    def acquire_many(self, n: int, max_wait: float | None = None) -> float:
        """一次占 `n` 格（**原子**：检查与记账在同一把锁里）。

        🔴 为什么必须"一次占 n 格"，而不是调 n 次 `acquire()`：
          一个账号要打 `REQUESTS_PER_ACCOUNT` 枪注册类请求，而**域名维度在
          注册时换不了**（邮箱已定死）。分两次占的话，第一次成功、第二次可能
          已被别的线程/进程抢走 —— 而那时验证码已经发出去了：**码烧了、
          账号拿不到**。
          实测（2026-10-14，5 账号跑批）：3 个账号 `code='ok(5 polls)'` 而
          `register='failed' [quota_guard]`，两个域名各 2/2 打满 —— 就是这个竞态。

        原子占 n 格把这件事变成"**要么 n 格都拿到、要么一枪不发**"。

        ⚠ 睡醒后**再查一次**：睡着的这段时间里别的线程/进程可能已经把名额抢走。
          那次不占格、直接抛 `WindowBusy` —— 占一半比不占更坏。
        """
        n = max(1, int(n))
        with self._lock:
            now = time.time()
            times = self._events(now)
            wait = 0.0
            if len(times) + n > self.max_events:
                if not times:
                    # 上限本身就装不下 n 格（配置矛盾）—— 等多久都没用。
                    raise WindowBusy(
                        f"本地窗口上限 {self.max_events} 格装不下 {n} 格（配置矛盾）",
                        wait_s=0.0,
                    )
                wait = (times[0] + self.window_s) - now
                if wait > 0:
                    if max_wait is not None and wait > max_wait:
                        raise WindowBusy(
                            f"本地滑动窗口装不下 {n} 格"
                            f"（已用 {len(times)}/{self.max_events}，还要等 {wait:.0f}s，"
                            f"封顶 {max_wait:g}s，等不起）",
                            wait_s=wait,
                        )
                    time.sleep(wait)
                now = time.time()
                times = self._events(now)
                if len(times) + n > self.max_events:
                    raise WindowBusy(
                        f"本地滑动窗口装不下 {n} 格（睡醒后仍只剩 "
                        f"{self.max_events - len(times)} 格）",
                        wait_s=max(0.0, (times[0] + self.window_s) - now) if times else 0.0,
                    )
            for _ in range(n):
                if self.key:
                    _windows.record(self.key, now)
                else:
                    times.append(now)
            if not self.key:
                self._times = times
            return max(0.0, wait)

    def release(self, n: int = 1) -> None:
        """归还 `n` 格（预留没用上时）。**不抛**（失败只告警）。

        🔴 与 `acquire_many` 配对：账号在发码之后死掉（网络 / 等不到码 /
          注册被拒）时，为它预留的那一格不能一直占着 —— 一个域名一小时只有
          2 格，泄漏几次就把域名锁死。
        """
        n = max(0, int(n))
        if n <= 0:
            return
        with self._lock:
            if self.key:
                _windows.drop(self.key, n)
                return
            for _ in range(min(n, len(self._times))):
                self._times.pop()


class WindowBusy(Exception):
    """本地滑动窗口已满、且等不起（`acquire(max_wait=...)` 撞到封顶）。

    🔴 与 `_quota.QuotaExceeded`（爆炸半径上限）同族：都是**本地守卫**主动
      中止。抛它而不是抱着窗口睡死 —— 窗口长度未测准（见 `docs/protocol.md` §3；域名维度默认 3600s），
      睡满它等于把这条账号链挂住，批量场景下就是整批假死。

    ⚠ `wait_s` 是"窗口还要等多久"，给报告/日志用（好让人知道是该等、
      该换维度，还是该收工）。
    """

    def __init__(self, message: str, *, wait_s: float = 0.0):
        super().__init__(message)
        self.wait_s = wait_s


__all__ = [
    "SlidingWindow",
    "WindowBusy",
]
