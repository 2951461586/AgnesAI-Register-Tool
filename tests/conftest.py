"""共享夹具。

🔴 **隔离本地状态**：`quota.state_path()` 与 `windowstore.state_path()` 都在
   **调用时**读各自的环境变量，所以这里 setenv 就能把两个状态文件钉在 tmp 目录里
   —— 不这么做的话，跑一次测试就会往真实 `state/` 里写记录，而那两个文件会影响
   **下一次真实跑批**的闸门（实测踩过：测试把额度吃掉）。
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AR_QUOTA_STATE", str(tmp_path / "register_quota.jsonl"))
    # 🔴 滑动窗口的事件现在**落盘**了（`src/windowstore.py`），所以它也必须隔离：
    #    不隔离的话，一个用例占掉的名额会顺着真实 `state/register_windows.jsonl`
    #    漏给下一个用例（甚至漏给真实的 `run.py`），而症状是"某个用例莫名被限流"。
    monkeypatch.setenv("AR_WINDOW_STATE", str(tmp_path / "register_windows.jsonl"))
    from src import pipeline

    # 🔴 域名维度的窗口也要隔离，理由与下面清字典一样：两个 limiter 字典都是
    #    **模块级全局**、跨用例共享，且 limiter 是**懒建**的（建的那一刻才读
    #    config）。只 patch IP 维度不 patch 域名维度的话，域名 limiter 会用真实
    #    默认值（2 次 / 3600s）建出来，两个用例就打满了 —— 之后每个用例都会在
    #    `acquire()` 里抱着 1 小时的窗口睡死（曾让 tests/test_pipeline.py
    #    整文件必挂，2026-10-13）。清字典保证下个用例重建时读到隔离值。
    monkeypatch.setattr(pipeline.config, "DOMAIN_MAX_PER_WINDOW", 100)
    monkeypatch.setattr(pipeline.config, "DOMAIN_WINDOW_S", 1.0)
    pipeline._REGISTER_LIMITERS.clear()
    pipeline._DOMAIN_LIMITERS.clear()
    pipeline._DOMAIN_USED.clear()
    yield
    pipeline._REGISTER_LIMITERS.clear()
    pipeline._DOMAIN_LIMITERS.clear()
    pipeline._DOMAIN_USED.clear()
