"""本地闸门：滑动窗口、域名预留、限流器工厂、换出口。

为什么单独一个模块
------------------
这几样东西只依赖 `config` / `windowstore` / `redact`，**不认识** `AccountRecord`
与 `Ticket` —— 所以它们是最容易切出去、也最该切出去的一块：闸门的规则
（窗口怎么算、预留怎么记）与"账号长什么样"是两件事。

⚠ 拆分是**纯搬移**（逐字），没有改任何逻辑。137+ 个用例是这次搬移的安全网，
  其中包含本会话两个 bug 的回归用例（并发烧码、网络失败扣 0 格）。
"""

import hashlib
import threading
import time

from common import config

from .. import windowstore as _windows
from ..proxypool import SlotPoolEmpty
from ..redact import redact_url

# 一个账号要打几枪**注册类**请求。
#
# 🔴 **真源在 `common/config.py`**（那里有完整的量化表：MAX=3 → 1 个账号）。
#    这里只**转出**，不另写一个 `2`：`config.validate()/lint()` 要用它判断
#    "窗口是不是连一个账号都装不下"，而 `common/` 是更低的层、不能反向 import
#    `src/`。同一个事实写两处必然漂移，而这里的漂移形态是"静默少一半容量"。
REQUESTS_PER_ACCOUNT = config.REQUESTS_PER_ACCOUNT


class DomainReservation:
    """一条账号在**某个收信域名**上占的格子（发码 1 格 + 注册 1 格）。

    为什么单独一个对象（本会话的真实教训）
    ------------------------------------
    预留原本是"`Ticket` 上两个裸字段 + 一个自由函数 + **19 处引用**"，
    跨 `provision` / `finish` / `run_batch` 三个函数。我在这套东西里引入过
    **两个 bug**，形态完全一样：**某条失败路径忘了归还 / 忘了扣**。

    所以三条不变量封进对象，调用方不用记得：

    1. **先扣后发** —— `consume()` 必须在 `send_code` **之前**调。
       网络层失败（`RemoteDisconnected` / `SSLEOFError`）的请求**可能已落地**，
       服务端可能已经记了这一枪。"成功后扣"的话，那种失败会把这一格也还回去
       ⇒ 本地比服务端**松**，而"宁可少打"是本地闸门的一贯要求。
       （实测踩到：一次跑批 3 条 `send_code` 撞网络错误，域名窗口**各扣 0 格**。）
    2. **补足而不是重占** —— `reserve()` 只补差额。
       发码重试会吃掉一格预留，这时只补 1 格就够；无脑再占 2 格会把注册那
       一格也挤掉，等于白重试。
    3. **归还幂等** —— `release()` 可以无脑调。
       所以 `run_batch` 的**所有**收尾路径都能调一次，不用分类讨论。

    🔴 它存的是**拿到格子的那个 limiter 对象**，而不是靠域名去反查 —— 于是
      本类**不依赖** `domain_limiter()`，单测里可以注入假 limiter 直接验
      这三条不变量，不必跑整条流水线。

    ⚠ 它**不**决定"换不换域名 / 退不退避"—— 那是 `provision` 的策略。
      这里只保证账目对得上。
    """

    __slots__ = ("_need", "_domain", "_held", "_limiter")

    def __init__(self, need: int):
        self._need = max(1, int(need))
        self._domain = ""
        self._held = 0
        self._limiter = None

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def held(self) -> int:
        """还没用掉的格数。`> 0` 表示 `finish` 的首次注册已经有着落。"""
        return self._held

    def missing_for(self, domain: str) -> int:
        """在 `domain` 上还差几格（换域名时等于整个 `need`）。"""
        if domain != self._domain:
            return self._need
        return max(0, self._need - self._held)

    def reserve(self, domain: str, limiter, max_wait: float | None = None) -> None:
        """在 `domain` 上**补足**到 `need` 格。

        ⚠ 换域名时会把旧域名上没用上的先还回去（不变量 3）。
        ⚠ `limiter.acquire_many()` 是**原子**的：它抛 `WindowBusy` 时一格都没占，
          所以这里不用做"半个预留"的清理。
        """
        if domain != self._domain:
            self.release()
        gap = self.missing_for(domain)
        if gap <= 0:
            return
        limiter.acquire_many(gap, max_wait)
        self._domain = domain
        self._limiter = limiter
        self._held += gap

    def consume(self) -> None:
        """一枪**即将发出** ⇒ 先扣一格（不变量 1）。"""
        self._held = max(0, self._held - 1)

    def release(self) -> None:
        """把没用上的还回去。**幂等**（不变量 3）。"""
        if self._limiter is not None and self._held > 0:
            self._limiter.release(self._held)
        self._domain = ""
        self._held = 0
        self._limiter = None


# ── 按出口的滑动窗口限速器 ────────────────────────────────────────
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

        🔴 存在的理由：窗口可能是**小时级**的（IP 维度实测 > 45 分钟），
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
      中止。抛它而不是抱着窗口睡死 —— 窗口长度未知（域名维度默认 3600s），
      睡满它等于把这条账号链挂住，批量场景下就是整批假死。

    ⚠ `wait_s` 是"窗口还要等多久"，给报告/日志用（好让人知道是该等、
      该换维度，还是该收工）。
    """

    def __init__(self, message: str, *, wait_s: float = 0.0):
        super().__init__(message)
        self.wait_s = wait_s


# 限速器。**按维度分桶**。
#
# 🔴 服务端有**两道**注册尝试窗口（IP 维度 + 域名维度，见 config），所以本地
#    也要两道，**两者都要过**：
#      · `register_limiter(slot)` —— IP 维度，按出口分桶（多出口 = 多张窗口）
#      · `domain_limiter(domain)` —— 域名维度，**全局**分桶（多域名 = 多张窗口）
#    只配一个就会从另一个维度撞服务端。
#
# 🔴 两张窗口的事件都**落盘**（`windowstore`），因为进程内的窗口每次启动即清零
#    —— 而本工具的实际用法是一天内多次短跑（台账实测 24 次）。
_REGISTER_LIMITERS: dict = {}
_DOMAIN_LIMITERS: dict = {}
_DOMAIN_USED: dict = {}
_LIMITER_LOCK = threading.Lock()


def _slot_key(slot: str | None) -> str:
    """槽位 → 窗口存储键。**绝不能把代理串原样写进 `state/`**。

    🔴 槽位串是 `scheme://user:pass@host:port`（或 `host:port:user:pass`），
      直接当键就是把**代理账密**持久化到磁盘。`state/` 虽在 `.gitignore` 中，
      但那是"不被提交"，不是"不该落盘" —— 与 `SlotStats.label` 是同一类事故
      （那边实测把密码打进了收尾报告）。

    形态 `ip:<脱敏 host:port>#<短哈希>`，两个部分都必需：
      · 保留 `host:port` —— 排查时能对上报告里的槽位标签；
      · 末尾哈希区分**同 host:port 的不同槽位** —— 本项目 `.slots.txt` 的 10 个
        槽位恰好是"同 host:port、不同 sid"，只留脱敏串会让它们**塌成一个键**，
        于是 10 张窗口变成 1 张（吞吐掉 10 倍，而且**不报错**）。
    """
    if not slot:
        return "ip:direct"
    shown = redact_url(slot)
    # `host:port:user:pass` → `host:port:***:***`；`http://u:p@h:port` → `http://***@h:port`
    hostpart = shown.split("://", 1)[-1].split("@")[-1]
    hostport = ":".join(hostpart.split(":")[:2]) or hostpart
    digest = hashlib.sha256(slot.encode("utf-8")).hexdigest()[:8]
    return f"ip:{hostport}#{digest}"


def register_limiter(slot: str | None = None) -> SlidingWindow:
    """取**某个出口**的注册尝试（验证码 + 注册）限速器（懒建 —— 测试改了 config 也能生效）。"""
    key = slot or ""
    with _LIMITER_LOCK:
        lim = _REGISTER_LIMITERS.get(key)
        if lim is None:
            lim = SlidingWindow(
                config.REGISTER_MAX_PER_WINDOW,
                config.REGISTER_WINDOW_S,
                key=_slot_key(slot),
            )
            _REGISTER_LIMITERS[key] = lim
        return lim


def domain_limiter(domain: str) -> SlidingWindow:
    """取**某个收信域名**的注册尝试限速器（**全局**，跨出口共享）。

    🔴 它与 `register_limiter` 是**两道独立的闸门**：服务端的域名维度窗口
      不是按出口计的（实测：用全新出口打已用过的域名仍然命中），
      所以本地也必须全局分桶 —— 按出口分桶等于放行 N 倍。
    """
    key = (domain or "").lower()
    with _LIMITER_LOCK:
        lim = _DOMAIN_LIMITERS.get(key)
        if lim is None:
            # ⚠ 域名进键是安全的（它不是凭据，台账里的 `mail_domain` 本就是明文）；
            #   出口才需要 `_slot_key()` 脱敏。
            lim = SlidingWindow(
                config.DOMAIN_MAX_PER_WINDOW,
                config.DOMAIN_WINDOW_S,
                key=f"domain:{key}",
            )
            _DOMAIN_LIMITERS[key] = lim
        return lim


def pick_domain(domains, *, exclude: str | None = None) -> str:
    """在域名池里挑一个（**最少使用优先**），尽量避开 `exclude`。

    与槽位池同一个理由：轮换取用会让某个域名先把窗口用满，总吞吐反而更低。
    """
    cands = [d for d in (domains or []) if d != exclude] or list(domains or [])
    if not cands:
        return ""
    with _LIMITER_LOCK:
        d = min(cands, key=lambda x: (_DOMAIN_USED.get(x, 0), x))
        _DOMAIN_USED[d] = _DOMAIN_USED.get(d, 0) + 1
        return d


def _pick_domain_with_room(domains, *, exclude: str, need: int) -> str:
    """挑一个**装得下 `need` 格**的域名（最少使用优先）。没有就返回 `""`。

    🔴 与 `pick_domain` 的差别：那个只按"最少使用"挑，会挑到一个**同样满**的
      域名 ⇒ 白白多转一圈，而每转一圈都消耗一次尝试（最后以 `quota_guard`
      收场，日志里看上去像"域名换了个遍都不行"）。这个先筛"装得下"。
    """
    cands = [d for d in (domains or []) if d != exclude and domain_limiter(d).room() >= need]
    if not cands:
        return ""
    with _LIMITER_LOCK:
        d = min(cands, key=lambda x: (_DOMAIN_USED.get(x, 0), x))
        _DOMAIN_USED[d] = _DOMAIN_USED.get(d, 0) + 1
        return d


def _is_domain_limit(ex) -> bool:
    """这次限流是不是**域名维度**的（换出口没用，得换域名）。"""
    return "email domain" in str(ex).lower()


def _is_ip_limit(ex) -> bool:
    """这次限流是不是**IP 维度**的（换域名没用，得换出口）。"""
    return "from this ip" in str(ex).lower()


def _register_backoff_s() -> float:
    """换不动维度时退避多久（秒）。**封顶 45s**。

    🔴 不取"等满一个窗口"：窗口长度尚未测准（IP 维度实测 > 45 分钟），
      按账号等满一个窗口会把批量拖死。封顶后重试仍失败，就把这条记录标成
      `quota` 失败 —— 那是**诚实**的失败（试过了，被限流），比挂着不动好。
    """
    per_ip = config.REGISTER_WINDOW_S / max(1, config.REGISTER_MAX_PER_WINDOW)
    per_dom = config.DOMAIN_WINDOW_S / max(1, config.DOMAIN_MAX_PER_WINDOW)
    return max(5.0, min(45.0, max(per_ip, per_dom)))


def _rotate_slot(slot_pool, holder, *, error: str) -> bool:
    """把这条链换到**另一个出口**。换成了返回 `True`。

    🔴 为什么是换出口，而不是原地重试：注册限流是**按 IP** 的。原地重试撞的
      还是同一张窗口；换一个出口就是一张**全新**的窗口。

    ⚠ 旧出口要**归还**（并按失败记账）—— 否则池子会逐渐少一个，而现象是
      "批量慢慢变慢/超时"，不是"池子漏了"。
    ⚠ `holder.proxy` 必须一起改：消费者最后归还的是 `holder.proxy` 指向的那个，
      不改就会**双重归还旧出口、而新出口永远漏在外面**。
    """
    if not slot_pool or len(slot_pool) < 2:
        return False
    old = holder.proxy
    try:
        new = slot_pool.acquire(timeout=5)
    except SlotPoolEmpty:
        return False
    if not new or new == old:
        # 只借到同一个（或没借到）⇒ 换不动，让调用方退避重试。
        if new:
            slot_pool.release(new)
        return False
    slot_pool.release(old, ok=False, error=f"quota {error[:120]}")
    holder.proxy = new
    return True
