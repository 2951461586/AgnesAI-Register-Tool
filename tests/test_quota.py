"""本地配额闸门的用例。

🔴 状态文件路径由 `conftest._isolate_state` 钉在 tmp 里 —— 不隔离的话，
   测试会往真实计数文件里写，从而影响**下一次真实跑批**。
"""

import time

import pytest

from src import quota as Q


def test_empty_state_allows():
    st = Q.status()
    assert st.used == 0 and not st.exhausted
    assert st.minutes_until_free() == 0


def test_records_count_only_success(monkeypatch):
    monkeypatch.setattr(Q.config, "REG_QUOTA_MAX", 2)
    monkeypatch.setattr(Q.config, "REG_QUOTA_WINDOW_H", 24.0)
    Q.record("a@x.com", ok=True)
    Q.record("b@x.com", ok=False, note="rejected")
    st = Q.status()
    assert st.used == 1
    assert st.attempts == 2
    assert not st.exhausted


def test_gate_raises_when_exhausted(monkeypatch):
    monkeypatch.setattr(Q.config, "REG_QUOTA_MAX", 1)
    monkeypatch.setattr(Q.config, "REG_QUOTA_WINDOW_H", 24.0)
    Q.record("a@x.com", ok=True)
    with pytest.raises(Q.QuotaExceeded):
        Q.check()
    # ignore 是显式的逃生门
    assert Q.check(ignore=True).used == 1


def test_window_excludes_old_entries(monkeypatch):
    monkeypatch.setattr(Q.config, "REG_QUOTA_MAX", 1)
    monkeypatch.setattr(Q.config, "REG_QUOTA_WINDOW_H", 1.0)
    Q.record("a@x.com", ok=True, ts=time.time() - 7200)  # 2 小时前
    st = Q.status()
    assert st.used == 0


def test_minutes_until_free_rounds_up(monkeypatch):
    monkeypatch.setattr(Q.config, "REG_QUOTA_MAX", 1)
    monkeypatch.setattr(Q.config, "REG_QUOTA_WINDOW_H", 1.0)
    now = time.time()
    # ts 取略早于 now：`record` 会把 ts 四舍五入到毫秒，若恰好取 now
    # 会得到一个比 now 大 0.0003s 的值，remain 就变成 3600.0003s → ceil 成 61。
    Q.record("a@x.com", ok=True, ts=now - 0.5)
    st = Q.status(now=now)
    # 剩余略少于 60 分钟 → 报 60，不是 61（`int(x//60)+1` 那个坑）
    assert st.minutes_until_free(now=now) == 60


def test_shortfall_hint_is_honest_when_ignoring():
    assert Q.shortfall_hint(0, 5, ignore=False) != ""
    assert Q.shortfall_hint(0, 5, ignore=True) == ""
    assert Q.shortfall_hint(10, 5, ignore=False) == ""
