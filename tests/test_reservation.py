"""`DomainReservation` 的离线用例（零网络、零流水线）。

为什么单独测这一个类
====================
预留的逻辑原本是"`Ticket` 上两个裸字段 + 一个自由函数 + 19 处引用"，
跨 `provision` / `finish` / `run_batch` 三个函数。**本会话在那套写法里引入过
两个 bug**，形态完全一样：某条失败路径忘了归还 / 忘了扣。

抽成对象之后，那三条不变量可以**注入假 limiter 直接验**，不必跑整条流水线
（那里要造假邮箱、假客户端、假槽位池，而且失败路径很难构造）。这是 P2 的
主要收益 —— 不只是"少几处引用"，而是**不变量变得可独立验证**。

三条不变量（见 `DomainReservation` 的 docstring）：
  1. 先扣后发 —— `consume()` 必须在请求**之前**调
  2. 补足而不是重占 —— `reserve()` 只补差额
  3. 归还幂等 —— `release()` 可以无脑调
"""

from typing import cast

import pytest

from src import pipeline as P
from src.pipeline import DomainReservation, WindowBusy


def _ticket() -> P.Ticket:
    """只关心预留的 Ticket。

    ⚠ `client` 是**必填**字段，而这里用不到它 ⇒ 用 `cast` 明确表示
      "这是个替身"（同 `tests/test_pipeline.py` 里的做法）。
    """
    return P.Ticket(rec=P.AccountRecord(), client=cast("P.AgnesClient", object()))


class FakeLimiter:
    """记账用的假限速器：只关心"被占了几格"。"""

    def __init__(self, room: int = 99):
        self.room_left = room
        self.events = 0

    def acquire_many(self, n, max_wait=None):
        if n > self.room_left:
            # 与真实 `SlidingWindow.acquire_many` 的契约一致：**一格都不占**
            raise WindowBusy(f"装不下 {n} 格", wait_s=0.0)
        self.room_left -= n
        self.events += n

    def release(self, n):
        self.events -= n
        self.room_left += n


def test_reserve_occupies_the_full_need():
    lim = FakeLimiter()
    r = DomainReservation(need=2)
    r.reserve("a.com", lim)
    assert lim.events == 2
    assert r.held == 2
    assert r.domain == "a.com"
    assert r.missing_for("a.com") == 0


def test_consume_does_not_touch_the_limiter():
    """🔴 不变量 1：`consume()` 只改**本地账**，不再去动 limiter。

    因为格子早在 `acquire_many` 时就记进窗口了；`consume()` 表达的是
    "这一格从'预留'变成'已用'"，而不是"退掉一格"。
    """
    lim = FakeLimiter()
    r = DomainReservation(need=2)
    r.reserve("a.com", lim)
    r.consume()
    assert lim.events == 2  # 窗口里仍是 2 格（发码那一枪确实发生了）
    assert r.held == 1  # 但只剩 1 格还留给将来
    assert r.missing_for("a.com") == 1


def test_reserve_tops_up_instead_of_re_taking():
    """🔴 不变量 2：同一域名上只补差额。

    场景：发码被服务端限流后重试。重试会 `consume()` 掉一格，这时只该补 1 格
    —— 无脑再占 2 格会把注册那一格也挤掉，等于白重试（码烧了还是进不去）。
    """
    lim = FakeLimiter()
    r = DomainReservation(need=2)
    r.reserve("a.com", lim)  # 2 格
    r.consume()  # 发码用掉 1 格 ⇒ 剩 1 格给注册
    r.reserve("a.com", lim)  # 重试：只补 1 格
    assert lim.events == 3  # 2 + 1（不是 2 + 2）
    assert r.held == 2  # 重试那一枪 + 注册那一枪
    assert r.missing_for("a.com") == 0


def test_reserve_on_a_new_domain_releases_the_old_one():
    """🔴 不变量 3 的一部分：换域名时**自动**归还旧域名。

    这条以前是调用方手写的（`provision` 里那句 `_release_domain_reservation`），
    而那正是出 bug 的地方之一 —— 忘了写就泄漏，写两次就重复归还。
    """
    a, b = FakeLimiter(), FakeLimiter()
    r = DomainReservation(need=2)
    r.reserve("a.com", a)
    r.consume()  # 发码用掉 1 格
    r.reserve("b.com", b)  # 换域名
    assert a.events == 1, "旧域名上只剩那一格已用的（1 格没用上的必须还回去）"
    assert b.events == 2
    assert r.domain == "b.com"
    assert r.held == 2


def test_release_is_idempotent():
    """🔴 不变量 3：`release()` 可无脑调 —— `run_batch` 的所有收尾路径都靠它。"""
    lim = FakeLimiter()
    r = DomainReservation(need=2)
    r.reserve("a.com", lim)
    r.release()
    r.release()
    r.release()
    assert lim.events == 0
    assert r.held == 0
    assert r.domain == ""
    # 没预留过时调也不抛
    DomainReservation(need=2).release()


def test_release_returns_only_the_unused_part():
    """已 `consume()` 的那一格**不该**还回去（它对应一次真实请求）。"""
    lim = FakeLimiter()
    r = DomainReservation(need=2)
    r.reserve("a.com", lim)
    r.consume()
    r.release()
    assert lim.events == 1, "还回去的只能是'没用上'的那 1 格"


def test_window_busy_leaves_no_partial_reservation():
    """`acquire_many` 是原子的 ⇒ 装不下时**一格都不占**，账目不能脏。

    占一半是最坏的：调用方会以为占成功了，而实际只够发一枪。
    """
    lim = FakeLimiter(room=1)  # 只装得下 1 格，而我们要 2 格
    r = DomainReservation(need=2)
    with pytest.raises(WindowBusy):
        r.reserve("a.com", lim)
    assert lim.events == 0
    assert r.held == 0
    assert r.domain == ""


def test_missing_for_a_different_domain_is_the_whole_need():
    """在**别的**域名上等于从零开始 —— `provision` 用它算"还差几枪"。"""
    lim = FakeLimiter()
    r = DomainReservation(need=2)
    r.reserve("a.com", lim)
    assert r.missing_for("a.com") == 0
    assert r.missing_for("b.com") == 2


def test_each_ticket_gets_its_own_reservation():
    """🔴 `Ticket.reservation` 必须用 `default_factory`。

    共享一份默认值会让所有 Ticket **串账** —— 一条账号的预留被另一条释放掉，
    而现象是"偶尔多放行 / 偶尔误拦"，极难回溯。
    """
    t1, t2 = _ticket(), _ticket()
    assert t1.reservation is not t2.reservation
    t1.reservation.reserve("a.com", FakeLimiter())
    assert t1.reservation.held == 2
    assert t2.reservation.held == 0
