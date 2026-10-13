"""`run.py` 与 `src/report.py` 之间的**关键字契约**。

为什么需要它：`render_batch_report` 的参数是纯关键字、且全部**注入**
（报告层不 import 项目内模块）。这意味着参数名写错**不会**在 import 期被发现 ——
它只会在运行到最后一行报告时抛 `TypeError`，而那时整批账号已经跑完了。

本用例用 AST 取出 `run.py` 里那次调用的关键字，逐个对**本项目的**签名核一遍。
"""

import ast
import inspect
from pathlib import Path

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
