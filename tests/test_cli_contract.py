"""`run.py` 与 `src/report.py` 之间的**关键字契约**。

为什么需要它：`render_batch_report` 的参数是纯关键字、且全部**注入**
（报告层不 import 项目内模块）。这意味着参数名写错**不会**在 import 期被发现 ——
它只会在运行到最后一行报告时抛 `TypeError`，而那时整批账号已经跑完了。

本用例用 AST 取出 `run.py` 里那次调用的关键字，逐个对**本项目的**签名核一遍。
"""

import ast
import inspect
from pathlib import Path

import pytest

from src import report as R

ROOT = Path(__file__).resolve().parents[1]


def _report_call_kwargs():
    tree = ast.parse((ROOT / "run.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "render_batch_report":
                found.append(node)
    assert found, "run.py 里找不到 render_batch_report 调用"
    return {kw.arg for kw in found[0].keywords if kw.arg}


def test_run_py_kwargs_match_report_signature():
    sig = inspect.signature(R.render_batch_report)
    params = set(sig.parameters)
    kw = _report_call_kwargs()
    unknown = kw - params
    assert not unknown, f"run.py 传了 report 不认得的参数：{sorted(unknown)}"


def test_report_has_no_project_imports():
    """报告层只依赖 stdlib —— 这是它能被测试链安全 import 的前提。"""
    src = (ROOT / "src" / "report.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] in {"statistics", "json", "math"}, a.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in {"statistics", "json", "math"}, node.module


def test_report_renders_with_injected_stubs():
    """注入最小依赖，报告能跑完（不炸在字段缺失上）。"""
    lines = []
    rec = {
        "email": "oai-abc@own.example",
        "status": "success",
        "plan": "free",
        "api_key": "sk-1234567890abcdefghij",
        "stages": {"verify": "ok(11 models)"},
        "timings": {"mailbox_ms": 100, "code_ms": 2000, "verify_ms": 50},
    }
    R.render_batch_report(
        [rec],
        written_path="/tmp/accounts.json",
        wall_seconds=3.0,
        stages=("mailbox", "code", "verify"),
        error_kind_of=lambda r: "",
        quota_line="0/30 个",
        workers=2,
        repo_rel=lambda p: "ledger/runs/x/accounts.json",
        redact_email=lambda e: e,
        slot_pool=None,
        redact_url=lambda u: u,
        log=lines.append,
    )
    text = "\n".join(lines)
    assert "成功 1 / 1" in text
    assert "sk-1234567890abcdefghij" not in text  # key 必须打码
    assert "sk-123456…ghij" in text


def test_corrupt_read_source_fails_before_any_request(tmp_path, monkeypatch):
    """🔴 回归：读源损坏必须在**启动阶段**就退出（码 3），不能等跑完一批才发现。

    这个工具最贵的资源是**服务端的注册窗口** —— 跑完 5 次尝试再告诉你
    "台账读不出来"，等于白烧一批。所以读源在 `main()` 开头就读好
    （`load_read_source`，损坏就抛）。

    ⚠ 与"静默抹账"是同一件事的两面：损坏时若不报错，`merged` 会退化成
      "只剩本批"，历史账号被静默丢光（见 `ledger.load_read_source`）。
    """
    import run
    from common import config
    from src import ledger as L

    monkeypatch.setattr(L, "LEDGER_DIR", tmp_path / "ledger")
    runs = L.LEDGER_DIR / "runs" / "2026-10-14"
    runs.mkdir(parents=True)
    # 半截 JSON —— 模拟写盘时被杀 / 外部损坏
    (runs / "accounts-20261014-000000.json").write_text(
        '[{"email":"a@x.com"},', encoding="utf-8"
    )
    monkeypatch.setattr(config, "validate", lambda: [])
    # 只要它走到 run_batch 就会发**真请求** —— 用哨兵把那条路炸出来
    monkeypatch.setattr(run, "run_batch", lambda **kw: pytest.fail("读源损坏时不该跑批！"))

    assert run.main(["--count", "1"]) == 3
