"""域名预留：一个账号在某个收信域名上占的格子（发码 1 格 + 注册 1 格）。

（从 `src/pipeline/limiter.py` 拆出；对外仍由 `limiter` / `pipeline` 转出。）
"""

# 本模块不依赖 `config` / 网络 / `AccountRecord`：只保证账目对得上。


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


__all__ = [
    "DomainReservation",
]
