"""台账合并语义（防静默缩水）。

这些用例钉的是 Intern-Register-Tool 栽过两次的那个坑：
**小规模运行覆盖掉整个台账**。判据是"字段只增不减"与"不让失败盖掉成功"。
"""

import pytest

from src import ledger as L


def _rec(email, status="success", **kw):
    d = {"email": email, "status": status}
    d.update(kw)
    return d


def test_merge_adds_new_and_keeps_old():
    existing = [_rec("a@x.com"), _rec("b@x.com")]
    new = [_rec("c@x.com")]
    merged, kept, added, upgraded = L.merge_records(existing, new)
    assert kept == 2
    assert added == 1
    assert upgraded == 0
    assert [r["email"] for r in merged] == ["a@x.com", "b@x.com", "c@x.com"]


def test_failure_does_not_overwrite_success():
    existing = [_rec("a@x.com", "success", api_key="sk-1")]
    new = [_rec("a@x.com", "failed", error="boom")]
    merged, _kept, _added, upgraded = L.merge_records(existing, new)
    assert merged[0]["status"] == "success"
    assert merged[0]["api_key"] == "sk-1"
    assert upgraded == 0


def test_success_upgrades_failure():
    existing = [_rec("a@x.com", "failed", error="boom")]
    new = [_rec("a@x.com", "success", api_key="sk-1")]
    merged, _kept, _added, upgraded = L.merge_records(existing, new)
    assert merged[0]["status"] == "success"
    assert merged[0]["api_key"] == "sk-1"
    assert upgraded == 1


def test_same_rank_union_keeps_old_fields():
    """同级合并取**并集** —— 新记录缺的字段不能被抹掉。"""
    existing = [_rec("a@x.com", "success", api_key="sk-1", plan="free")]
    new = [_rec("a@x.com", "success", models_count=3)]
    merged, _kept, _added, upgraded = L.merge_records(existing, new)
    assert merged[0]["api_key"] == "sk-1"
    assert merged[0]["plan"] == "free"
    assert merged[0]["models_count"] == 3
    assert upgraded == 1


def test_shrink_guard_refuses(tmp_path):
    p = tmp_path / "accounts.json"
    with pytest.raises(ValueError):
        L.save(p, [{"email": "a@x.com"}], existing=[{"email": "a@x.com"}, {"email": "b@x.com"}])
    # 显式覆盖（existing=[]）才放行
    L.save(p, [{"email": "a@x.com"}], existing=[])
    assert L.load_existing(p) == [{"email": "a@x.com"}]


def test_snapshot_naming_and_latest_are_distinct(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "LEDGER_DIR", tmp_path / "ledger")
    snap = L.snapshot_path(when=1_700_000_000)
    assert snap.name.startswith("accounts-")
    assert snap.parent.parent.name == "runs"
    # latest.json 与快照**不是**同一个文件（后者不能当读源）
    assert L.last_run_path().name == "latest.json"
    assert L.last_run_path() != snap


def test_ledger_path_returns_newest_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "LEDGER_DIR", tmp_path / "ledger")
    runs = L.LEDGER_DIR / "runs" / "2026-10-13"
    runs.mkdir(parents=True)
    (runs / "accounts-20261013-100000.json").write_text("[]", encoding="utf-8")
    (runs / "accounts-20261013-110000.json").write_text("[]", encoding="utf-8")
    # 一个**命名不合规**的文件不能被当成读源
    (runs / "notes.json").write_text("[]", encoding="utf-8")
    assert L.ledger_path().name == "accounts-20261013-110000.json"


def test_load_existing_tolerates_garbage(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{not json", encoding="utf-8")
    assert L.load_existing(p) == []
    p.write_text('{"a":1}', encoding="utf-8")
    assert L.load_existing(p) == []
    assert L.load_existing(tmp_path / "missing.json") == []


# ── 读源：**损坏就抛**，绝不静默降级成"空台账" ──────────────────
#
# 🔴 这是本会话普查出的最严重一条。`load_existing` 的"宽容"对只读用途是对的，
#    但拿它当**合并的基准**会出事：静默返回 `[]` ⇒ `merged = 本批` ⇒ 写回一份
#    只剩本批的快照 ⇒ 历史账号永久丢失，而退出码是 0、报告全绿、
#    连「结果合并：原有 N 条」那行都不打（`kept=0` 时它不打）。
#    实测：50 条的读源被截断 ⇒ 写回 1 条，全程无告警。


def test_read_source_refuses_corrupt_instead_of_silently_empty(tmp_path):
    p = tmp_path / "accounts-x.json"
    p.write_text('[{"email": "a@x.com"}, {"email"', encoding="utf-8")  # 截断
    with pytest.raises(L.LedgerCorrupt):
        L.load_read_source(p)
    # "宽容"的那个仍然宽容 —— 它服务的是**只读**用途（导出 key / 复核账号）
    assert L.load_existing(p) == []


def test_read_source_rejects_non_list(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    with pytest.raises(L.LedgerCorrupt):
        L.load_read_source(p)
    assert L.load_existing(p) == []


def test_read_source_treats_missing_file_as_empty_ledger(tmp_path):
    """⚠ 与"损坏"严格区分：文件不存在 = 全新台账，空是**正确**答案。

    `ledger_path()` 在没有任何快照时返回一个永不存在的占位路径 —— 若这里
    也抛错，全新仓库第一次跑就起不来了。
    """
    assert L.load_read_source(tmp_path / "missing.json") == []


def test_read_source_returns_records_for_valid_file(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('[{"email": "a@x.com"}, 42]', encoding="utf-8")
    # 非 dict 的元素被丢弃（与 load_existing 同一条过滤规则）
    assert L.load_read_source(p) == [{"email": "a@x.com"}]
