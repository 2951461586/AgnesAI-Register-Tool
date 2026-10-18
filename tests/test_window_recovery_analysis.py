"""`tools/ops/analyze_window_recovery.py` 的**离线**契约用例。

为什么值得单独测
================
这个工具的输出会被写进 `docs/protocol.md` 当"实测事实"用，而它读的是**台账**
（含明文凭据）。两类错误最贵：
  1. **算错**：把跨出口的 0.5s 成功当成"窗口上界 0 分钟"，于是得出荒谬结论；
  2. **泄漏**：把台账里的邮箱 / 密码 / JWT / 代理账密打印出来。
两者都不会报错，只会让人基于错结论做决定 —— 所以钉成用例。

⚠ 用例**不读真实台账**：全部用合成记录（`ledger/` 是数据，不该被用例依赖）。
   模块用 `importlib` 从路径加载，避免 `tools/` 的包结构假设。
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / "tools" / "ops" / "analyze_window_recovery.py"


def _load():
    spec = importlib.util.spec_from_file_location("analyze_window_recovery", MOD_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # 🔴 必须先登记进 `sys.modules` 再 exec：模块里有 `@dataclass`，而
    #    dataclasses 在解析注解时会 `sys.modules[cls.__module__]` —— 没登记
    #    就是 `None.__dict__`，报一个和本用例毫无关系的 AttributeError。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


AWR = _load()


def _rec(
    created: str,
    *,
    email: str = "a@d.example",
    domain: str = "d.example",
    proxy: str = "direct",
    proxy_rotated: str = "",
    domain_rotated: str = "",
    error_kind: str = "",
    error: str = "",
    stages: dict | None = None,
    timings: dict | None = None,
    status: str = "failed",
) -> dict:
    return {
        "email": email,
        "created_at": created,
        "mail_domain": domain,
        "proxy": proxy,
        "proxy_rotated": proxy_rotated,
        "mail_domain_rotated": domain_rotated,
        "error_kind": error_kind,
        "error": error,
        "stages": stages or {"code": "failed", "register": ""},
        "timings": timings or {},
        "status": status,
    }


DOMAIN_LIMIT = (
    "HTTP 400 400：Too many registration attempts from this email domain. Please try again later"
)
IP_LIMIT = "HTTP 400 400：Too many registration attempts from this IP. Please try again later"


# ── 读台账 ────────────────────────────────────────────────────────
def test_load_records_dedupes_snapshots_and_skips_bad_files(tmp_path):
    """`ledger/runs/` 是**快照**：同一条记录会在几十个文件里重复。"""
    runs = tmp_path / "runs" / "2026-10-14"
    runs.mkdir(parents=True)
    a = _rec("2026-10-14 10:00:00", email="a@d.example")
    b = _rec("2026-10-14 10:05:00", email="b@d.example")
    (runs / "a.json").write_text(json.dumps([a, b]), encoding="utf-8")
    (runs / "b.json").write_text(json.dumps([a]), encoding="utf-8")
    (tmp_path / "latest.json").write_text(json.dumps([b]), encoding="utf-8")
    (runs / "bad.json").write_text("{not json", encoding="utf-8")

    got = AWR.load_records(tmp_path)
    assert len(got) == 2, "去重失败：快照里的重复记录被算成了多个样本"
    assert [r["email"] for r in got] == ["a@d.example", "b@d.example"]


# ── 恢复样本 ──────────────────────────────────────────────────────
def test_domain_bound_uses_same_ip_not_cross_ip_anomaly():
    """🔴 跨出口的 0.5s 成功是**反证**，不能当窗口上界。

    本用例钉的正是第一版报告的错误形态：把 `Δ=0` 的跨出口样本当成
    "W ≤ 0 分钟"。正确做法是：同出口样本给上界，跨出口样本单独报。
    """
    recs = [
        # 同一出口：10:00 被域名限流 → 10:15 发码成功 ⇒ W ≤ 15min
        _rec("2026-10-14 10:00:00", proxy="http://p1", error_kind="quota", error=DOMAIN_LIMIT),
        _rec(
            "2026-10-14 10:15:00",
            proxy="http://p1",
            status="success",
            stages={"code": "ok", "register": "ok"},
            timings={"code_send_ms": 120},
        ),
        # 跨出口：10:20 在 p1 上被限流，0.5s 后 p2 上发码成功 ⇒ 反证
        _rec("2026-10-14 10:20:00", proxy="http://p1", error_kind="quota", error=DOMAIN_LIMIT),
        _rec(
            "2026-10-14 10:20:00",
            proxy="http://p2",
            status="success",
            stages={"code": "ok", "register": "ok"},
            timings={"code_send_ms": 90, "mailbox_ms": 500},
        ),
    ]
    res = AWR.summarize(recs)
    assert res["domain_min_recovery_min"] == 15.0, "同出口最短恢复间隔应是 15 分钟"
    assert res["domain_min_recovery_cross_ip_min"] is not None
    assert res["domain_min_recovery_cross_ip_min"] < 0.1, "跨出口反证应接近 0"
    assert len(res["domain_contradictions"]) == 1, "跨出口 0s 成功应被单独列为反证"
    assert res["domain_contradictions"][0].same_ip is False


def test_ip_samples_are_marked_same_ip():
    """IP 维度按出口分桶 ⇒ 失败与恢复必然同出口（`same_ip=True`）。"""
    recs = [
        _rec("2026-10-14 10:00:00", proxy="http://p1", error_kind="quota", error=IP_LIMIT),
        _rec(
            "2026-10-14 10:12:00",
            proxy="http://p1",
            status="success",
            stages={"code": "ok"},
            timings={"code_send_ms": 50},
        ),
    ]
    samples = AWR.recovery_samples(recs, "ip")
    assert len(samples) == 1
    assert samples[0].same_ip is True
    assert samples[0].delta_min == 12.0


def test_send_success_is_code_send_ms_not_stage_ok():
    """`stages.code == "failed"` 也可能是**发码成功但没等到信**。

    判据必须是 `code_send_ms` **存在** —— 用 `stages.code` 会漏掉这些已经
    消耗了窗口的样本（`provision` 在 send_code 返回后才写它）。
    """
    recs = [
        _rec("2026-10-14 10:00:00", error_kind="quota", error=DOMAIN_LIMIT),
        _rec(
            "2026-10-14 10:20:00",
            stages={"code": "failed", "register": ""},  # 等码超时
            timings={"code_send_ms": 300, "code_wait_ms": 120000},
        ),
    ]
    samples = AWR.recovery_samples(recs, "domain")
    assert len(samples) == 1, "发码成功（code_send_ms 存在）应算恢复样本"
    assert samples[0].delta_min == 20.0


def test_rotated_records_are_excluded_from_keyed_samples():
    """换过出口 / 域名的记录，归属不可信 ⇒ 不参与对应维度的分桶。"""
    recs = [
        _rec(
            "2026-10-14 10:00:00",
            proxy="http://p1",
            proxy_rotated="http://p0 → http://p1",
            error_kind="quota",
            error=IP_LIMIT,
        ),
        _rec(
            "2026-10-14 10:00:00",
            domain="d.example",
            domain_rotated="x.example → d.example",
            error_kind="quota",
            error=DOMAIN_LIMIT,
        ),
        _rec(
            "2026-10-14 11:00:00",
            proxy="http://p1",
            status="success",
            stages={"code": "ok"},
            timings={"code_send_ms": 10},
        ),
    ]
    assert AWR.recovery_samples(recs, "ip") == []
    assert AWR.recovery_samples(recs, "domain") == []


# ── 子域 / 父域 ───────────────────────────────────────────────────
def test_family_pairs_only_suffix_matches():
    pairs = AWR.family_pairs(["sub.a.example", "a.example", "b.example", "SUB.a.example"])
    assert pairs == [("sub.a.example", "a.example")]


def test_family_evidence_separates_after_success_from_before_success():
    """🔴 失败**之前**的成功不是反证 —— 第一版把它算进去，得出过错结论。"""
    recs = [
        # 父域在子域失败前 7.5s 成功（中性）
        _rec(
            "2026-10-14 09:59:52",
            domain="a.example",
            status="success",
            stages={"code": "ok"},
            timings={"code_send_ms": 10},
        ),
        # 子域被限流
        _rec("2026-10-14 10:00:00", domain="sub.a.example", error_kind="quota", error=DOMAIN_LIMIT),
        # 父域在失败后 30s 成功（反证）
        _rec(
            "2026-10-14 10:00:30",
            domain="a.example",
            status="success",
            stages={"code": "ok"},
            timings={"code_send_ms": 10},
        ),
        # 父域也限流（支持共窗）
        _rec("2026-10-14 10:01:00", domain="a.example", error_kind="quota", error=DOMAIN_LIMIT),
    ]
    ev = AWR.family_evidence(recs, [("sub.a.example", "a.example")])
    child_fail = [e for e in ev if e["failed"] == "sub.a.example"][0]
    assert child_fail["other_sent_ok_before_60s"] == 1
    assert child_fail["other_sent_ok_after_5min"] == 1
    assert child_fail["other_failed_within_5min"] >= 1


# ── 分桶 / 输出 ───────────────────────────────────────────────────
def test_bucket_counts_cover_all_samples():
    samples = [
        AWR.Sample("domain", "d", _dt(0), _dt(0), "d", None, "d", None),
        AWR.Sample("domain", "d", _dt(0), _dt(0.5), "d", None, "d", None),
        AWR.Sample("domain", "d", _dt(0), _dt(15), "d", None, "d", None),
        AWR.Sample("domain", "d", _dt(0), _dt(120), "d", None, "d", None),
    ]
    counts = AWR.bucket_counts(samples)
    assert sum(counts.values()) == 4
    assert counts["0~1min"] == 2
    assert counts["15~20min"] == 1
    assert counts["60min+"] == 1


def test_json_output_has_no_credentials():
    """🔴 台账里有明文凭据 —— 分析输出只许出现域名与出口哈希。"""
    recs = [
        _rec(
            "2026-10-14 10:00:00",
            email="secret@d.example",
            proxy="http://user:pass@host.example:8080",
            error_kind="quota",
            error=DOMAIN_LIMIT,
        ),
        _rec(
            "2026-10-14 10:15:00",
            email="secret@d.example",
            proxy="http://user:pass@host.example:8080",
            status="success",
            stages={"code": "ok"},
            timings={"code_send_ms": 10},
        ),
    ]
    recs[0]["password"] = "hunter2"
    recs[0]["jwt"] = "eyJhbGciOiJIUzI1NiJ9.payload.sig"
    text = json.dumps(AWR.to_json(AWR.summarize(recs)), ensure_ascii=False)
    for needle in ("secret@", "hunter2", "eyJhbGci", "user:pass"):
        assert needle not in text, f"分析输出泄漏了凭据形状：{needle}"
    assert "d.example" in text, "域名应该保留（排查要用）"


def _dt(minutes: float):
    import datetime as dt

    return dt.datetime(2026, 10, 14, 10, 0) + dt.timedelta(minutes=minutes)
