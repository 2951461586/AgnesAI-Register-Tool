"""槽位代理池的契约用例。

🔴 这里钉的是**报告输出边界**，不是池的调度细节：`SlotStats.label` 会进
   收尾报告，而报告是最容易被贴进 issue / 截图的东西 —— 标签里不能出现
   代理凭据。
"""

from src.proxypool import SlotPool, SlotStats


def test_acquire_prefers_the_slot_the_caller_calls_usable():
    """🔴 回归（实测 2026-10-14）：`used` 是**进程内**计数 ⇒ 新进程里全是 0
    ⇒ 排序退化成**列表顺序** ⇒ 先借到上一批刚把窗口打满的那几个。

    现象：一批 7 个跑完后立刻再跑，4 次尝试全撞在满窗口上，以 `quota_guard`
    收场 —— 而池子里其实还有 3 个出口是空的。
    """
    pool = SlotPool(["full-1", "full-2", "full-3", "free-1", "free-2"])
    got = pool.acquire(prefer=lambda u: u.startswith("free"))
    assert got == "free-1", f"应优先挑可用的，实际借到 {got!r}"


def test_acquire_prefer_still_hands_out_a_slot_when_none_is_usable():
    """全不可用时**仍然借得出** —— 不能变成死锁/抛异常。

    "没得换"是调用方（`provision` 的重试循环）要判的事，池子不该替它决定。
    """
    pool = SlotPool(["a", "b"])
    assert pool.acquire(prefer=lambda u: False) == "a"


def test_acquire_without_prefer_keeps_least_used_order():
    """默认行为不变：最少使用优先（`used` 相同时按列表顺序）。"""
    pool = SlotPool(["a", "b"])
    assert pool.acquire() == "a"
    assert pool.acquire() == "b"


def test_label_uses_port_for_url_form():
    # 原来的好行为：`http://127.0.0.1:7901` → `7901`
    assert SlotStats(url="http://127.0.0.1:7901").label == "7901"


def test_label_never_leaks_password_in_host_port_user_pass_form():
    """🔴 回归：`host:port:user:pass` 的**末段是密码**。

    旧实现 `url.rsplit(":", 1)[-1]` 会把密码当标签打进报告 —— 实测 10 个
    槽位全部把代理密码原样打了出来（`xum33c6k` 那一列）。
    """
    st = SlotStats(url="us.proxy.example:2000:acct-region-SG-sid-abc:t0psecret")
    label = st.label
    assert "t0psecret" not in label
    assert "acct-region-SG-sid-abc" not in label
    assert label.startswith("us.proxy.example:2000")


def test_label_never_leaks_userinfo_in_url_form():
    st = SlotStats(url="http://acct-region-SG-sid-abc:t0psecret@proxy.example:8080")
    label = st.label
    assert "t0psecret" not in label
    assert "acct-region-SG-sid-abc" not in label


def test_label_distinguishes_slots_on_same_host():
    """同 host:port 的多个槽位必须有**不同**标签，否则报告里这一栏是废的。

    标签用稳定短哈希区分 —— 不能用 `hash()`（每个进程加盐，跨次运行不稳定）。
    """
    a = SlotStats(url="us.proxy.example:2000:acct-sid-AAA:pw")
    b = SlotStats(url="us.proxy.example:2000:acct-sid-BBB:pw")
    assert a.label != b.label
    # 稳定性：同一个 URL 必须永远得到同一个标签
    assert a.label == SlotStats(url="us.proxy.example:2000:acct-sid-AAA:pw").label
