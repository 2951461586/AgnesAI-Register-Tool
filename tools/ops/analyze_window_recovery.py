#!/usr/bin/env python
"""从**台账**反推注册窗口的恢复时间（**零窗口代价**，不发任何请求）。

为什么需要它
============
`docs/protocol.md` §3 里"窗口长度"一直只有一句 **>45 分钟**，而那句话是
**早期探针连续打点**得出的 —— 探针每打一枪，如果被拒的那一枪**也计数**，
窗口就被**自己续命**了。所以 ">45 分钟" 很可能不是窗口长度，而是"探针一直
在给它加事件"。

台账里其实已经有一批**受控样本**：`run.py` 的失败记录带着 `created_at` 与
逐阶段 `timings`，成功记录里有 `code_send_ms`（= 发码请求真的发出去了）。
于是可以反推：

    「某维度在 T 时刻被限流」 → 「同一维度下一次发码成功在 T+Δ」
    ⇒ **恢复间隔 = Δ**

🔴 **Δ 不是窗口长度 W。** 它只说明"到那时计数已低于阈值"；而计数多久降到
阈值以下，取决于阈值、失败那一枪是否计数、以及是不是多级限流。2026-10-18
的受控复测在 **16.14 分钟仍被域名限流** ⇒ 台账里 14.88 分钟的恢复间隔**不是**
W 的上界。所以本工具现在只报"恢复间隔"，**不**再把它写成 W ≤ Δ。

它能做什么：
  · 给出"某维度最早多久恢复"的**观测**（本项目 2026-10-13~17 的台账里是
    14.88 分钟）；
  · 发现**反证**（跨出口 0.5s 成功 ⇒ 不是严格全局计数）；
  · 给出子域/父域共窗的历史证据。
它**不能**回答"窗口长度是多少" —— 那要用 `probe_window_limits.py` 的单次触碰
设计（打满 → 静默 → 只探一枪）。

本工具**只读** `ledger/`，不碰网络、不消耗窗口。唯一的代价是读台账 ——
台账里有明文凭据，所以本工具**只打印域名与脱敏后的出口哈希**，绝不打印
邮箱 / 密码 / JWT / `sk-` key / 代理账密。

⚠ 它回答不了的三件事（别把它当万能）：
  1. **精确窗口长度**：只有上界。要测准得用 `probe_window_limits.py`
     （受控、单次触碰）。
  2. **IP 维度的归属**：失败记录如果中途换过出口，`proxy` 是**最后**那个，
     而 `proxy_rotated` 只在成功路径写。所以本工具**跳过**带
     `proxy_rotated` 的记录；剩下的仍可能低估失败时刻（重试/退避的耗时
     不在 `code_wait_send_ms` 里）⇒ 上界只会**更紧**，不会更松。
  3. **域名维度是不是全局的**：本工具会专门报一类**反证** ——
     "A 出口刚被域名限流，B 出口 0.5s 后在同一域名上发码成功"。
     如果域名窗口是**全局计数**，这不可能发生；出现它说明服务端要么按
     (IP, 域名) 分桶，要么是**最终一致**的（边缘节点各算各的）。

用法
====
    python tools/ops/analyze_window_recovery.py
    python tools/ops/analyze_window_recovery.py --json      # 机器可读
    python tools/ops/analyze_window_recovery.py --top 20    # 多列样本

退出码：0 = 分析完成；2 = 台账里没有可分析的记录。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

# ⚠ 刻意**不** import `tools/_bootstrap`：本模块要能被用例直接 import（纯逻辑、
#   零副作用）。仓库根用 `__file__` 推，不依赖 cwd（与 `src/ledger.py` 同理由）。
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / "ledger"

# 恢复时间的分桶（分钟）。用来一眼看出"样本集中在哪个量级"。
BUCKETS = (0.0, 1.0, 5.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0)


# ── 读台账 ────────────────────────────────────────────────────────
def load_records(ledger_dir: Path = DEFAULT_LEDGER) -> list[dict]:
    """读 `ledger/runs/*/*.json` + `ledger/latest.json`，**按 (email, created_at) 去重**。

    🔴 必须去重：`ledger/runs/` 是**快照**，每次跑批都会把**当时已知的全部记录**
      写进去（实测 74 个文件 = 10376 条，去重后只有 227 条）。不去重的话，
      同一对"失败 → 成功"会被数几十遍，样本量看起来很大，其实只有一对。

    ⚠ 读不出来的文件**跳过并计数**，不抛：台账是数据，但本工具是分析器，
      一个坏文件不该让整份报告没有。
    """
    files = sorted((ledger_dir / "runs").glob("*/*.json"))
    latest = ledger_dir / "latest.json"
    if latest.is_file():
        files.append(latest)
    seen: dict[tuple, dict] = {}
    bad = 0
    for p in files:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bad += 1
            continue
        recs = raw if isinstance(raw, list) else [raw]
        for r in recs:
            if not isinstance(r, dict):
                continue
            key = (r.get("email"), r.get("created_at"))
            if not key[1]:
                continue
            seen.setdefault(key, r)
    out = list(seen.values())
    out.sort(key=_created)
    if bad:
        print(f"⚠ 跳过 {bad} 个读不出来的台账文件", file=sys.stderr)
    return out


# ── 时间线 ────────────────────────────────────────────────────────
def _created(r: dict) -> dt.datetime:
    return dt.datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")


def _at(r: dict, *keys: str) -> dt.datetime:
    """`created_at` + 若干 `timings` 的**毫秒**偏移。缺字段按 0 算。

    ⚠ `timings` 是**服务端请求的耗时**，不是绝对时刻。所以这里的"请求开始
      时刻"是 `created_at + 之前的阶段耗时` —— 精度到秒级，够用来判
      "15 分钟 vs 60 分钟"，不够用来判 "14 分 59 秒 vs 15 分整"。
    """
    t = _created(r)
    timings = r.get("timings") or {}
    for k in keys:
        try:
            t += dt.timedelta(milliseconds=int(timings.get(k) or 0))
        except (TypeError, ValueError):
            # 坏值按 0 算：时间戳只用来判量级（15min vs 60min），
            # 一个脏字段不该让整份分析没有。
            continue
    return t


def _sent_ok(r: dict) -> bool:
    """这次**发码请求真的发出去了**（服务端没限流、没网络失败）。

    🔴 判据是 `code_send_ms` **存在**，不是 `stages.code == "ok"`：
      `provision` 在 `send_code` 返回后才写 `code_send_ms`，而"发出去了但
      没等到信"会把 `stages.code` 标成 `failed`（`MAIL_TIMEOUT`）。
      用 `stages.code` 判会**漏掉**这些已经消耗窗口的样本。
    """
    return "code_send_ms" in (r.get("timings") or {})


def _send_time(r: dict) -> dt.datetime:
    """发码请求的**开始**时刻（本地闸门的等待也算进去）。"""
    return _at(r, "mailbox_ms", "code_wait_send_ms")


def _fail_time(r: dict) -> dt.datetime:
    """这条**限流失败**的请求开始时刻（近似）。

    - 发码阶段失败：`created + mailbox + code_wait_send`
    - 注册阶段失败：再往后加 `code_send + code_wait + register_wait`

    ⚠ 重试 / 换出口的耗时**没有**逐次记录 ⇒ 这里的失败时刻可能**早于**真实值。
      那会让 Δ 偏大（上界变松），但结论方向不变：真实窗口只会更短。
    """
    stages = r.get("stages") or {}
    if stages.get("code") == "failed" or not _sent_ok(r):
        return _at(r, "mailbox_ms", "code_wait_send_ms")
    return _at(
        r,
        "mailbox_ms",
        "code_wait_send_ms",
        "code_send_ms",
        "code_wait_ms",
        "register_wait_ms",
    )


def _is_quota(r: dict) -> bool:
    """服务端那条 `Too many registration attempts` 的 400。"""
    if r.get("error_kind") == "quota":
        return True
    return "too many registration attempts" in (r.get("error") or "").lower()


def _quota_dim(r: dict) -> str:
    """限流消息属于哪个维度：`ip` / `domain` / `""`（分不出）。

    🔴 两条消息**只差中间一个词**，所以必须看消息，不能看 `error_kind`
      （两者都是 `quota`）。见 `docs/protocol.md` §3。
    """
    err = (r.get("error") or "").lower()
    if "email domain" in err:
        return "domain"
    if "from this ip" in err:
        return "ip"
    return ""


def _ip_key(r: dict) -> str | None:
    """出口的**脱敏**键；换过出口的记录返回 `None`（归属不可信）。

    🔴 用哈希而不是原串：`proxy` 是 `scheme://user:pass@host:port`，原样打印
      就是把代理账密写进终端 / 报告（`src/pipeline._slot_key` 已踩过这个坑）。
    """
    if r.get("proxy_rotated"):
        return None
    raw = r.get("proxy") or "direct"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _domain_key(r: dict) -> str | None:
    """收信域名；换过域名的记录返回 `None`（失败那一枪不一定在这个域名上）。"""
    if r.get("mail_domain_rotated"):
        return None
    return (r.get("mail_domain") or "").lower() or None


# ── 样本 ──────────────────────────────────────────────────────────
@dataclass
class Sample:
    """一次"被限流 → 后来发码成功"的恢复观测。"""

    dim: str
    key: str
    fail_at: dt.datetime
    ok_at: dt.datetime
    fail_domain: str
    fail_ip: str | None
    ok_domain: str
    ok_ip: str | None
    #: 失败与恢复是否在**同一个出口**上（IP 维度恒为 True；域名维度用于识别反证）。
    same_ip: bool | None = None

    @property
    def delta_s(self) -> float:
        return (self.ok_at - self.fail_at).total_seconds()

    @property
    def delta_min(self) -> float:
        return self.delta_s / 60.0


def recovery_samples(records: list[dict], dim: str) -> list[Sample]:
    """每个限流失败 → 同一维度上**下一次**发码成功的间隔。

    `dim="ip"` 按出口分桶，`dim="domain"` 按**精确 hostname** 分桶。
    同一维度内按失败时刻排序，取"严格晚于失败时刻"的最近一次成功。
    """
    keyf = _ip_key if dim == "ip" else _domain_key
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        k = keyf(r)
        if k:
            groups[k].append(r)

    out: list[Sample] = []
    for key, ev in groups.items():
        ev.sort(key=_created)
        for i, r in enumerate(ev):
            if not (_is_quota(r) and _quota_dim(r) == dim):
                continue
            tf = _fail_time(r)
            later = [x for x in ev[i + 1 :] if _sent_ok(x) and _send_time(x) > tf]
            if not later:
                continue
            x = min(later, key=_send_time)
            same_ip: bool | None = None
            if dim == "domain":
                a, b = _ip_key(r), _ip_key(x)
                same_ip = (a == b) if (a and b) else None
            else:
                # IP 维度本来就是按出口分桶的：失败与恢复必然在同一个出口。
                same_ip = True
            out.append(
                Sample(
                    dim=dim,
                    key=key,
                    fail_at=tf,
                    ok_at=_send_time(x),
                    fail_domain=_domain_key(r) or "",
                    fail_ip=_ip_key(r),
                    ok_domain=_domain_key(x) or "",
                    ok_ip=_ip_key(x),
                    same_ip=same_ip,
                )
            )
    out.sort(key=lambda s: s.delta_s)
    return out


def family_pairs(domains: list[str]) -> list[tuple[str, str]]:
    """找出域名池里的 **(子域, 父域)** 对（按后缀，不查公共后缀表）。

    本项目只用简单后缀（`sub.own.example` ⊂ `own.example`），所以"一个域名是
    另一个的后缀"足够；换成 `.co.uk` 这类需要 PSL，本工具不假装支持。
    """
    ds = sorted({d.lower() for d in domains if d})
    return [(a, b) for a in ds for b in ds if a != b and a.endswith("." + b)]


def family_evidence(records: list[dict], pairs: list[tuple[str, str]]) -> list[dict]:
    """父域 / 子域**共窗**的证据（历史台账，零代价）。

    对每个 (子域, 父域) 对，统计三类事件：
      · 同一对**先后**被域名限流（时间差 < 5 分钟）—— 支持"共窗"；
      · 一边被限流、另一边**之后**发码成功 —— 反证"共窗"；
      · 另一边在失败**之前**发码成功 —— 中性（那是更早的历史，不构成反证）。

    🔴 "失败前的成功"必须与"失败后的成功"分开：第一版把它们算在一起，
      于是 `-7.5s`（失败前 7.5 秒）也被当成了反证 —— 而它什么都证明不了。
    """
    sends = [r for r in records if _sent_ok(r)]
    fails = [r for r in records if _is_quota(r) and _quota_dim(r) == "domain"]
    out: list[dict] = []
    for child, parent in pairs:
        pair = {child, parent}
        pair_fails = [r for r in fails if _domain_key(r) in pair]
        pair_sends = [r for r in sends if _domain_key(r) in pair]
        for r in pair_fails:
            tf = _fail_time(r)
            d = _domain_key(r)
            other = parent if d == child else child
            same_side_fail = [
                x
                for x in pair_fails
                if x is not r
                and _domain_key(x) == other
                and abs((_fail_time(x) - tf).total_seconds()) <= 300
            ]
            other_sends = [x for x in pair_sends if _domain_key(x) == other]
            other_ok_after = [
                x for x in other_sends if 0 <= (_send_time(x) - tf).total_seconds() <= 300
            ]
            other_ok_before = [
                x for x in other_sends if -60 <= (_send_time(x) - tf).total_seconds() < 0
            ]
            out.append(
                {
                    "child": child,
                    "parent": parent,
                    "failed": d,
                    "other": other,
                    "at": tf.isoformat(sep=" ", timespec="seconds"),
                    "other_failed_within_5min": len(same_side_fail),
                    "other_sent_ok_after_5min": len(other_ok_after),
                    "other_sent_ok_before_60s": len(other_ok_before),
                    "other_send_deltas_s": [
                        round((_send_time(x) - tf).total_seconds(), 1) for x in other_ok_after[:5]
                    ],
                }
            )
    out.sort(key=lambda e: e["at"])
    return out


def bucket_counts(samples: list[Sample]) -> dict[str, int]:
    """按分钟分桶（`[0,1) [1,5) …`），最后一个是 `60+`。"""
    labels = [f"{BUCKETS[i]:g}~{BUCKETS[i + 1]:g}min" for i in range(len(BUCKETS) - 1)]
    labels.append(f"{BUCKETS[-1]:g}min+")
    counts = dict.fromkeys(labels, 0)
    for s in samples:
        m = s.delta_min
        for i in range(len(BUCKETS) - 1):
            if BUCKETS[i] <= m < BUCKETS[i + 1]:
                counts[labels[i]] += 1
                break
        else:
            counts[labels[-1]] += 1
    return counts


def global_counter_contradictions(samples: list[Sample], *, within_s: float = 60.0) -> list[Sample]:
    """域名维度上"A 出口刚被限流、B 出口立刻成功"的样本。

    如果域名窗口是**全局计数**，这在逻辑上不可能（计数只随时间下降）。
    出现它 ⇒ 服务端要么按 `(IP, 域名)` 分桶，要么计数是**最终一致**的
    （CF 边缘各算各的）。**这会直接影响本地闸门该按什么维度分桶。**
    """
    return [
        s for s in samples if s.dim == "domain" and s.same_ip is False and s.delta_s <= within_s
    ]


def summarize(records: list[dict]) -> dict:
    """一次算完所有结论（给 CLI 与用例共用）。

    🔴 入口先排序：`date_range` 直接取首尾，不排序的话调用方传进来的顺序
      会决定报告里的日期区间（`load_records` 会排，但用例/外部调用不一定）。
    """
    records = sorted(records, key=_created)
    ip = recovery_samples(records, "ip")
    dom = recovery_samples(records, "domain")
    # ⚠ 显式建成 `list[str]`：`_domain_key` 的返回类型是 `str | None`，
    #    直接 `sorted({...})` 会让类型检查器把 `None` 也带进来。
    domains: list[str] = []
    for r in records:
        d = _domain_key(r)
        if d:
            domains.append(d)
    pairs = family_pairs(domains)

    def _bound(samples: list[Sample], *, same_ip: bool | None) -> float | None:
        picked = [s for s in samples if same_ip is None or s.same_ip is same_ip]
        return round(picked[0].delta_min, 2) if picked else None

    return {
        "records": len(records),
        "date_range": [
            _created(records[0]).isoformat(sep=" ", timespec="seconds"),
            _created(records[-1]).isoformat(sep=" ", timespec="seconds"),
        ]
        if records
        else [],
        "quota_failures": sum(1 for r in records if _is_quota(r)),
        "quota_by_dim": dict(Counter(_quota_dim(r) for r in records if _is_quota(r))),
        "ip_samples": ip,
        "domain_samples": dom,
        "ip_min_recovery_min": _bound(ip, same_ip=None),
        # 🔴 域名维度必须**分两个上界**：跨出口的“刚限流就成功”是反证，
        #    不能拿它当窗口上界（那会得出 W ≤ 0 这种荒谬值）。
        "domain_min_recovery_min": _bound(dom, same_ip=True),
        "domain_min_recovery_cross_ip_min": _bound(dom, same_ip=False),
        "ip_buckets": bucket_counts(ip),
        "domain_buckets": bucket_counts(dom),
        "family_pairs": pairs,
        "family_evidence": family_evidence(records, pairs),
        "domain_contradictions": global_counter_contradictions(dom),
    }


# ── 输出 ──────────────────────────────────────────────────────────
def _print_samples(samples: list[Sample], top: int) -> None:
    for s in samples[:top]:
        same = "-" if s.same_ip is None else ("same" if s.same_ip else "OTHER-IP")
        print(
            f"  Δ={s.delta_min:7.2f}min  {s.key:<22} "
            f"{s.fail_at.strftime('%m-%d %H:%M:%S')} → "
            f"{s.ok_at.strftime('%H:%M:%S')}  ip:{same:<8} "
            f"dom={s.fail_domain}→{s.ok_domain}"
        )
    if len(samples) > top:
        print(f"  … 还有 {len(samples) - top} 条")


def _print_buckets(title: str, counts: dict[str, int]) -> None:
    print(f"  {title}：")
    for label, n in counts.items():
        if n:
            print(f"    {label:<12} {n:>3} 条")


def print_report(res: dict, top: int) -> None:
    print("═" * 72)
    print("注册窗口恢复时间 —— 从台账反推（零窗口代价）")
    print("═" * 72)
    print(
        f"台账记录：{res['records']} 条（去重后）  "
        f"区间：{res['date_range'][0]} → {res['date_range'][1]}"
    )
    print(f"服务端限流失败：{res['quota_failures']} 条，维度分布：{res['quota_by_dim']}")

    for dim, samples, buckets in (
        ("IP 维度（按出口）", res["ip_samples"], res["ip_buckets"]),
        ("域名维度（按精确 hostname）", res["domain_samples"], res["domain_buckets"]),
    ):
        print(f"\n── {dim} ──")
        if not samples:
            print("  没有可用样本（限流记录后面没有同维度的成功发码）。")
            continue
        _print_samples(samples, top)
        _print_buckets("恢复时间分布", buckets)
        if dim.startswith("IP"):
            print(
                f"  ⇒ **最短恢复间隔：{res['ip_min_recovery_min']} 分钟**（样本 {len(samples)} 条；**不是**窗口长度，见 docs/protocol.md §3）"
            )
        else:
            print(
                f"  ⇒ **同出口最短恢复间隔：{res['domain_min_recovery_min']} 分钟**"
                f"（**不是**窗口长度；受控复测在 16 分钟仍限流）"
            )
            if res["domain_bound_cross_ip_min"] is not None:
                print(
                    f"  ⚠ 跨出口样本里最短只有 {res['domain_min_recovery_cross_ip_min']} 分钟 "
                    f"⇒ 域名计数不是“全局严格计数”（见下一节）。"
                )

    contra = res["domain_contradictions"]
    print("\n── 域名窗口是不是全局的？ ──")
    if contra:
        print(
            f"  🔴 发现 {len(contra)} 条**反证**（A 出口被限流后 ≤60s，B 出口在同域名发码成功）："
        )
        for s in contra[:10]:
            print(
                f"    {s.fail_at.strftime('%m-%d %H:%M:%S')} "
                f"{s.fail_domain}  ip:{s.fail_ip} 失败 → ip:{s.ok_ip} 成功 "
                f"(Δ={s.delta_s:.1f}s)"
            )
        print("  ⇒ 与「全局计数」矛盾：要么按 (IP, 域名) 分桶，要么计数**最终一致**。")
        print("     ⇒ 本地闸门按**全局域名**分桶是**保守**的，但会少算吞吐。")
    else:
        print("  没有发现反证（所有恢复都晚于 60s 或来自同一出口）。")
        print("  ⚠ 没有反证**不等于**证明了全局计数 —— 需要受控实验。")

    print("\n── 子域 / 父域是否共窗？ ──")
    if not res["family_pairs"]:
        print("  域名池里没有 (子域, 父域) 对。")
    else:
        for e in res["family_evidence"]:
            print(
                f"  {e['at']}  被限流={e['failed']:<22} 另一边={e['other']:<22} "
                f"另一边也限流(±5min)={e['other_failed_within_5min']} "
                f"另一边之后成功={e['other_sent_ok_after_5min']} "
                f"{e['other_send_deltas_s']}"
            )
        print("  判读：")
        print("    · 另一边**也限流** ⇒ 支持共窗；")
        print("    · 另一边在失败**之后**发码成功 ⇒ 反证共窗（各自独立窗口）。")

    print("\n── 结论与局限 ──")
    print("  · 恢复间隔来自「失败后第一次成功」，只能证明到那时已恢复；它不是窗口长度。")
    print("  · IP 维度跳过换过出口的记录；失败时刻用 timings 近似（可能偏早）。")
    print("  · 要测准窗口长度：用 tools/probes/probe_window_limits.py（受控、单次触碰）。")
    print("  · 台账里有明文凭据 —— 本工具只输出域名与出口哈希。")


def to_json(res: dict) -> dict:
    """样本对象 → 可 `json.dumps` 的 dict（**不含**任何凭据）。"""

    def s2d(s: Sample) -> dict:
        return {
            "dim": s.dim,
            "key": s.key,
            "fail_at": s.fail_at.isoformat(sep=" ", timespec="seconds"),
            "ok_at": s.ok_at.isoformat(sep=" ", timespec="seconds"),
            "delta_s": round(s.delta_s, 1),
            "fail_domain": s.fail_domain,
            "ok_domain": s.ok_domain,
            "same_ip": s.same_ip,
        }

    out = dict(res)
    out["ip_samples"] = [s2d(s) for s in res["ip_samples"]]
    out["domain_samples"] = [s2d(s) for s in res["domain_samples"]]
    out["domain_contradictions"] = [s2d(s) for s in res["domain_contradictions"]]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从台账反推注册窗口恢复时间（零窗口代价）")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="台账目录（默认 ledger/）")
    ap.add_argument("--top", type=int, default=8, help="每类样本最多列几条")
    ap.add_argument("--json", action="store_true", help="输出 JSON（不含凭据）")
    a = ap.parse_args(argv)

    records = load_records(Path(a.ledger))
    if not records:
        print("✗ 台账里没有可用记录（先跑几次 run.py）", file=sys.stderr)
        return 2
    res = summarize(records)
    if a.json:
        print(json.dumps(to_json(res), ensure_ascii=False, indent=2))
    else:
        print_report(res, a.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
