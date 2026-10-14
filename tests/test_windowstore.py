"""`windowstore` 的离线用例（零网络）。

钉住三件事：
  1. `events()` 返回**升序** —— `SlidingWindow` 用 `events[0]` 算"还要等多久"，
     顺序错了会把等待算成负数（表现为"明明满了却立刻放行"）；
  2. **坏文件不抛**（半截 JSONL 行只丢那一行、读不出来退化成空窗口）——
     与 `quota.py` 同一套降级策略，与 `ledger.py` 相反；
  3. 窗口**按 key 分桶**且**按窗口剪枝** —— 串桶会让两个出口共用一张窗口。

⚠ 隔离靠 `conftest.py` 的 autouse fixture（把 `AR_WINDOW_STATE` 钉在 tmp 目录）。
"""

from src import windowstore as W


def test_events_are_ascending():
    """🔴 契约：升序。乱序会让"还要等多久"算成负数。"""
    for ts in (300.0, 100.0, 200.0):
        W.record("ip:a", ts)
    assert W.events("ip:a", window_s=1000.0, now=400.0) == [100.0, 200.0, 300.0]


def test_events_prune_outside_window():
    W.record("ip:a", 100.0)
    W.record("ip:a", 950.0)
    assert W.events("ip:a", window_s=100.0, now=1000.0) == [950.0]


def test_events_are_bucketed_by_key():
    """串桶 = 两个出口共用一张窗口（吞吐掉一半且不报错）。"""
    W.record("ip:a", 100.0)
    W.record("domain:x.com", 200.0)
    assert W.events("ip:a", 1000.0, now=300.0) == [100.0]
    assert W.events("domain:x.com", 1000.0, now=300.0) == [200.0]
    assert W.events("ip:b", 1000.0, now=300.0) == []


def test_missing_file_is_an_empty_window():
    """全新状态（文件还没建）不是错误 —— 与 `ledger` 对"没有快照"的处置同构。"""
    assert not W.state_path().is_file()
    assert W.events("ip:a", 1000.0) == []


def test_corrupt_line_is_dropped_not_raised():
    """🔴 半截行（写盘时被杀）只丢**那一行**，不丢整个文件、更不抛。

    窗口状态是**加速器不是数据**：丢了只让下一轮更松。让它抛的后果是
    **整批任务起不来**。这条降级已登记在 `test_silent_degradation.EXCEPT_ALLOWED`。
    """
    W.record("ip:a", 100.0)
    with W.state_path().open("a", encoding="utf-8") as f:
        f.write('{"ts": 200.0, "key": "ip:a"')  # 半截行，无换行
    assert W.events("ip:a", 1000.0, now=300.0) == [100.0]  # 好的那行还在


def test_garbage_ts_falls_back_to_zero_and_is_pruned():
    """坏时间戳退回 0（= 远在窗口外，被剪掉），而不是让整批炸在 `float()` 上。

    ⚠ 用**真实量级的 epoch** 做断言：退回的 0.0 只在 `now` 是真实时钟时才
      算"窗口外"（`cutoff = now - window` 在真实时钟下远大于 0）。
    """
    W.record("ip:a", 1_000_000.0)
    with W.state_path().open("a", encoding="utf-8") as f:
        f.write('{"ts": "yesterday", "key": "ip:a"}\n')
    assert W.events("ip:a", 1000.0, now=1_000_500.0) == [1_000_000.0]


def test_entries_without_key_are_ignored():
    """没有 `key` 的行不参与任何桶（否则会污染所有桶）。"""
    with W.state_path().open("a", encoding="utf-8") as f:
        f.write('{"ts": 100.0}\n')
    assert W.events("ip:a", 1000.0, now=300.0) == []


def test_compaction_keeps_in_window_entries(monkeypatch):
    """压缩只丢窗口外的条目，且**按最长的那道窗口**保留。

    ⚠ 本文件是 IP 维度与域名维度**共用**的，按短的窗口压会把长的那个维度的
      事件也删掉 —— 闸门变松，而且不报错。
    ⚠ 时钟**必须先 patch**：`record()` 自己会调 `_maybe_compact()`，用真实时钟
      时所有测试时间戳（1.0 / 9500.0）都在窗口外 ⇒ 第一次记账就把文件压空了
      （这个坑是本用例第一版真实踩到的）。
    """
    monkeypatch.setattr(W, "COMPACT_MIN_BYTES", 0)  # 强制走压缩分支
    monkeypatch.setattr(W.config, "REGISTER_WINDOW_S", 100.0)
    monkeypatch.setattr(W.config, "DOMAIN_WINDOW_S", 1000.0)
    monkeypatch.setattr(W.time, "time", lambda: 10_000.0)
    W.record("ip:a", 1.0)  # 很久以前 —— 该被压掉（任何窗口都装不下）
    W.record("domain:x.com", 9500.0)  # 只在 1000s 那道窗口内 —— 必须留下
    W._maybe_compact()
    assert W.events("domain:x.com", 1000.0, now=10_000.0) == [9500.0]
    assert W.events("ip:a", 1000.0, now=10_000.0) == []
