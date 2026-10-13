"""原子写盘的契约用例。

🔴 这个模块只有两条性质不能错，其余都是实现细节：

   1. **临时名是追加 `.tmp`**，不是 `path.with_suffix()` 的替换。
      替换会抹掉"它是什么文件"，而且同目录同时存在 `a.json` / `a.jsonl`
      时，两个临时名会**撞车**。
   2. **替换是原子的** —— 崩溃只会留下"旧内容 + 一个半截 tmp"，
      读者永远看不到半截的目标文件。台账（账号的唯一副本）靠这条活着。

⚠ 这里**不**断言具体用哪个 syscall，只断言可观察结果（旧内容完好 / tmp
   不残留 / 目标文件是新内容），这样换实现不会误红。
"""

import json

import pytest

from src import fsutil as F

# ── tmp_path_for：追加而不是替换 ──────────────────────────────────


def test_tmp_path_appends_not_replaces():
    assert F.tmp_path_for("register_quota.jsonl").name == "register_quota.jsonl.tmp"


def test_tmp_path_does_not_collide_across_suffixes(tmp_path):
    # with_suffix 的坑：a.json / a.jsonl 都会塌缩成 a.tmp ⇒ 撞车
    assert F.tmp_path_for(tmp_path / "a.json") != F.tmp_path_for(tmp_path / "a.jsonl")


# ── atomic_write_text ─────────────────────────────────────────────


def test_atomic_write_text_creates_parents_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "deep" / "nested" / "out.txt"
    got = F.atomic_write_text(target, "hi")
    assert got == target
    assert target.read_text(encoding="utf-8") == "hi"
    assert not F.tmp_path_for(target).exists()  # tmp 必须被 replace 吃掉


def test_atomic_write_text_overwrites(tmp_path):
    target = tmp_path / "x.txt"
    F.atomic_write_text(target, "old")
    F.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.glob("*.tmp")) == []


def test_crash_before_replace_keeps_old_content(tmp_path, monkeypatch):
    """模拟"写到一半崩了"：目标文件必须还是**旧内容**，半截只落在 tmp。"""
    target = tmp_path / "x.txt"
    F.atomic_write_text(target, "old")

    def boom(_src, _dst):
        raise OSError("simulated crash")

    monkeypatch.setattr(F.os, "replace", boom)
    with pytest.raises(OSError):
        F.atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert F.tmp_path_for(target).exists()


def test_atomic_write_uses_replace(tmp_path, monkeypatch):
    """原子性的唯一来源就是 `os.replace` —— 别退化成 `write_text` 直写。"""
    seen = []
    real = F.os.replace

    def spy(src, dst):
        seen.append((str(src), str(dst)))
        return real(src, dst)

    monkeypatch.setattr(F.os, "replace", spy)
    target = tmp_path / "x.txt"
    F.atomic_write_text(target, "v")
    assert seen == [(str(F.tmp_path_for(target)), str(target))]


# ── atomic_write_json ─────────────────────────────────────────────


def test_atomic_write_json_is_utf8_and_not_escaped(tmp_path):
    target = tmp_path / "a.json"
    F.atomic_write_json(target, {"中文": "值", "n": 1})
    raw = target.read_text(encoding="utf-8")
    assert "中文" in raw
    assert "\\u4e2d" not in raw  # ensure_ascii=False：给人看的
    assert json.loads(raw) == {"中文": "值", "n": 1}
