"""槽位代理池的契约用例。

🔴 这里钉的是**报告输出边界**，不是池的调度细节：`SlotStats.label` 会进
   收尾报告，而报告是最容易被贴进 issue / 截图的东西 —— 标签里不能出现
   代理凭据。
"""

from src.proxypool import SlotStats


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
