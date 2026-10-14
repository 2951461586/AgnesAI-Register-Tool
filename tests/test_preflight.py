"""`preflight` 的离线用例（零网络）。

为什么值得单独测
================
预检的**判据**是这个模块唯一有价值的东西，而它很容易写错 —— 写错的方向是
"把好时段判成坏时段"（于是该跑的时候不敢跑）或反过来（于是白烧注册窗口）。
两种错的代价都不小，而它们都**不会报错**。

所以这里用**注入的假 probe** 把判据单独测：不碰网络，也不依赖任何真实链路状态。

🔴 判据的核心不是"失败率高低"，而是"**多数出口一起坏**吗"：
   个别坏槽位不该拦住整批（撞到它换出口就行）；
   多数一起坏才是链路/平台侧的坏时段（换出口也没用）。
"""

import pytest

from src import preflight as pf


def _h(ok, repeat=4, slot=None):
    """造一个 `SlotHealth`（errs 自动补齐，只关心数字）。"""
    errs = {} if ok == repeat else {"SSLError": repeat - ok}
    return pf.SlotHealth(slot=slot, ok=ok, repeat=repeat, errs=errs)


def _pf(oks, repeat=4):
    return pf.Preflight(results=[_h(o, repeat) for o in oks])


# ── 基本统计 ──────────────────────────────────────────────────────
def test_rates_and_totals():
    p = _pf([4, 4, 0, 2])
    assert p.total == 16
    assert p.total_ok == 10
    assert p.failure_rate == pytest.approx(1 - 10 / 16)
    assert p.repeat == 4


def test_elevated_is_half_or_worse():
    p = _pf([4, 2, 1, 0])  # 失败率 0% / 50% / 75% / 100%
    assert [h.ok for h in p.elevated] == [2, 1, 0]


def test_empty_preflight_is_allowed():
    """没有出口可查（直连）⇒ 放行。把"查不了"当成"不健康"会让工具没法用。"""
    p = pf.Preflight(results=[])
    assert p.verdict() == ""
    assert p.failure_rate == 0.0


# ── 判据：这三条是核心 ────────────────────────────────────────────
def test_healthy_link_is_allowed():
    assert _pf([4] * 10).verdict() == ""


def test_a_single_bad_slot_does_not_block_the_batch():
    """🔴 个别坏槽位**不该**拦住整批 —— 撞到它换出口就行。

    这条是"误拦"方向的守卫：如果判据只看"有没有出口坏"，那本会话里
    每一个坏时段都会被拦下，工具就没法用了。
    """
    p = _pf([0] + [4] * 9)  # 1/10 全坏 ⇒ 全池 10%
    # ⚠ 用 `approx`：`1 - 36/40` 在二进制浮点下是 0.09999999999999998，
    #   而精确比较会让这条用例因**无关的**精度问题红（第一版就这么挂的）。
    assert p.failure_rate == pytest.approx(0.1)
    assert p.verdict() == ""


def test_majority_failing_is_reported_as_a_link_wide_spell():
    """🔴 多数出口**一起**坏 ⇒ 链路/平台侧坏时段（换出口也没用）。

    实测依据：某一轮 10 个老槽位 17~50%，而**刚加的新代理也 42%** ——
    新出口不可能"是坏槽位"，所以那一轮的高低全是链路侧的。
    """
    p = _pf([0] * 8 + [4, 4])  # 8/10 过半
    why = p.verdict()
    assert why, "多数出口一起坏必须拦"
    assert "多数出口一起坏" in why
    assert "坏时段" in why


def test_high_pool_rate_without_majority_is_still_blocked():
    """没有"过半"但全池失败率仍高 ⇒ 也拦，只是理由不同（措辞要区分）。"""
    p = _pf([1, 1, 1, 1] + [4] * 6)  # 全池 30%，4/10 过半
    why = p.verdict()
    assert why
    assert "多数出口一起坏" not in why  # 措辞必须区分，否则读者分不清该换什么
    assert "换时间点" in why


def test_threshold_is_configurable():
    p = _pf([3, 3, 3, 3])  # 全池 25%
    assert p.verdict(max_failure=0.20) != ""
    assert p.verdict(max_failure=0.50) == ""


# ── 描述文本 ──────────────────────────────────────────────────────
def test_describe_admits_small_samples():
    """🔴 样本量不足时必须**主动说明**，而不是让读者把数字当结论。

    实测：同一出口 5 次 `0/5` 全 SSLError，几分钟后 15/15 全好。
    """
    small = _pf([4, 4], repeat=3).describe()
    assert "样本量不足" in small
    big = _pf([4, 4], repeat=pf.RELIABLE_SAMPLE).describe()
    assert "样本量不足" not in big


def test_describe_marks_the_elevated_slots():
    txt = _pf([4, 0]).describe()
    assert "过半失败" in txt


# ── 注入点 ────────────────────────────────────────────────────────
def test_run_uses_the_injected_probe():
    """`run()` 的 probe 是注入点 ⇒ 判据逻辑可以完全离线测。"""
    calls = []

    def fake_probe(slot, repeat):
        calls.append((slot, repeat))
        return _h(repeat, repeat, slot=slot)

    p = pf.run(["a", "b", "c"], repeat=5, probe=fake_probe)
    assert sorted(calls) == [("a", 5), ("b", 5), ("c", 5)]
    assert p.total_ok == 15
    assert p.verdict() == ""


def test_run_with_no_slots_probes_the_direct_route():
    """没配槽位池时探**直连**（`None`）—— 而不是什么都不查。"""
    seen = []

    def fake_probe(slot, repeat):
        seen.append(slot)
        return _h(repeat, repeat, slot=slot)

    pf.run([], repeat=2, probe=fake_probe)
    assert seen == [None]


# ── 标签 ──────────────────────────────────────────────────────────
def test_label_distinguishes_slots_without_leaking_credentials():
    """🔴 标签要能区分槽位，又不能泄账密。

    本项目 `.slots.txt` 的槽位是"同 host:port、不同 sid"，只用 `redact_url`
    会让它们**全都长成一样** ⇒ 表格里看不出是哪个出口在失败。
    """
    a = "us.example.net:2000:acct-region-SG-sid-aaaaaaaa-t-5:SECRETPW"
    b = "us.example.net:2000:acct-region-SG-sid-bbbbbbbb-t-5:SECRETPW"
    assert pf.label(a) != pf.label(b), "同 host:port 的不同槽位必须可区分"
    for lab in (pf.label(a), pf.label(b)):
        assert "SECRETPW" not in lab
        assert "acct" not in lab
        assert "us.example.net" in lab  # host:port 保留（排查要对得上）


def test_label_of_direct_is_readable():
    assert pf.label(None) == "(直连)"
