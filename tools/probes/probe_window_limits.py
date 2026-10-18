#!/usr/bin/env python
"""受控测量：**窗口恢复时间** 与 **子域/父域是否共窗**（⚠ 会消耗注册窗口）。

为什么需要它（而不是继续用老探针）
==================================
旧探针的 `--mode window` 设计有一个**致命缺陷**：
它每隔 `--wait` 秒对**同一个域名**打一枪。如果被拒的那一枪**也计数**
（服务端回了限流，说明它收到了），那每次探测都在给窗口**续命** ——
于是"一直 400"只能证明"你一直在打它"，**不能**反推窗口长度。
docs/protocol.md 里那条 ">45 分钟" 很可能就是这么来的。

本探针改成**单次触碰**：

    T 时刻把窗口打满（fill） → 静默 wait 秒（**一枪都不打**） → 只在 T+wait 探一枪

于是：
  · T+wait 成功 ⇒ 窗口长度 W ≤ wait（**上界**，干净）；
  · T+wait 仍被限流 ⇒ W > wait（**下界**）。

一次运行只回答一个点（"≤ wait 还是 > wait"）。要收窄就换几个 wait 各跑一次，
**每次用不同的域名 / 出口**，互不污染。

子域问题同理：把**父域**打满，然后立刻探一次**子域**：
  · 子域成功 ⇒ 父/子各自独立窗口（按**精确 hostname** 计）；
  · 子域也被域名限流 ⇒ 共窗（按**可注册域**计）。
⚠ 前提是父域**确实被打满**了（fill 里至少出现一次 `email domain` 限流）；
  否则结论是"没测出来"，不是"独立"。

🔴 成本（每个 send 都要解一次 captcha，实测单次成功率 ~50%、20~45s）：
  · `recovery`：fill(默认 12) + 1 次探测，再等 `--wait`（默认 2700s = 45 分钟）
  · `subdomain`：fill(3) + 1 次探测
  · 每个 send 都可能因为 captcha 失败重解 ⇒ 实际耗时按 ×2 估

默认 `--mode plan` **不发任何请求**，只打印计划与成本。真正测量要显式选模式。

用法
====
    # 只看计划（零成本）
    python tools/probes/probe_window_limits.py

    # 窗口恢复：打满一个域名 → 静默 15 分钟 → 探一枪
    python tools/probes/probe_window_limits.py --mode recovery --wait 2700

    # 子域/父域：打满父域 → 立刻探子域
    python tools/probes/probe_window_limits.py --mode subdomain

    # 反向再验一次（打满子域 → 探父域）
    python tools/probes/probe_window_limits.py --mode subdomain --reverse

⚠ 探测期间**不要**跑 `run.py` / 别的探针：它们会打同一批窗口，结论就废了。
   探针会把每一枪记进本地窗口（`windowstore`），所以跑完本地闸门是"热"的 ——
   真实批量前请等一个窗口或换域名/出口。

退出码：0 = 测到了结论；1 = 配置缺失；3 = 没测出来（fill 没打满 / captcha 失败）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import _path  # noqa: F401  # 副作用：插 sys.path + 修控制台编码

from common import config
from src import redact, windowstore
from src.agnes import AgnesCaptcha, AgnesClient, AgnesError, AgnesQuota, AgnesRejected
from src.captcha import TCaptchaProvider
from src.mailbox import for_domain
from src.pipeline import _slot_key, register_limiter

# 一次探测的判定结果。用字符串而不是 Enum：输出里要直接打给人看。
OK = "ok"  # 发码成功
LIM_IP = "ip"  # 被 IP 维度限流
LIM_DOMAIN = "domain"  # 被域名维度限流
REJECTED = "rejected"  # 服务端明确拒绝（域名白名单 / 邮箱已注册…）
NETWORK = "network"  # 网络 / 5xx（状态未知）
CAPTCHA = "captcha"  # 本地没解出 ticket（**不消耗注册窗口**）
MAILBOX = "mailbox"  # 建邮箱失败


def _now() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def _iso() -> str:
    return dt.datetime.now().isoformat(sep=" ", timespec="seconds")


def _quota_dim(ex: Exception) -> str:
    """把 `Too many registration attempts ...` 分成 IP / 域名两维。

    🔴 两条消息**只差中间一个词**，所以必须看消息；`AgnesQuota` 本身不带维度。
    """
    err = str(ex).lower()
    if "email domain" in err:
        return LIM_DOMAIN
    if "from this ip" in err:
        return LIM_IP
    return "quota"


@dataclass
class SendResult:
    """一枪的完整证据。**不含明文邮箱**（只留脱敏串）。"""

    at: str
    domain: str
    email: str  # 已脱敏
    slot: str  # 已脱敏
    status: str
    detail: str = ""
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "at": self.at,
            "domain": self.domain,
            "email": self.email,
            "slot": self.slot,
            "status": self.status,
            "detail": self.detail,
            "elapsed_s": round(self.elapsed_s, 1),
        }


class Prober:
    """一个账号一枪的探针：建邮箱 → 直连解 captcha → 指定出口发码。

    🔴 **绕过本地闸门**：探针的目的就是超过本地保守值去测服务端，所以
      `send()` 不调 `acquire()`。但"绕过检查"不等于"不记账" —— 每一枪都
      `windowstore.record()`，否则探针跑完本地比服务端松，真实批量会撞墙。
    """

    def __init__(
        self,
        slots: list[str],
        provider: TCaptchaProvider,
        *,
        captcha_proxy: str | None = None,
        send_retries: int = 2,
        out: Path | None = None,
    ):
        self.slots = slots
        self.provider = provider
        self.captcha_proxy = captcha_proxy
        self.send_retries = max(1, send_retries)
        self.out = out
        self._slot_i = 0
        self.used_slots: list[str] = []
        self.results: list[SendResult] = []

    # ── 出口选择 ──────────────────────────────────────────────
    def pick_slot(self, *, avoid: set[str] | None = None) -> str | None:
        """挑一个**本探针还没用过**的出口（优先本地窗口看起来空的）。

        为什么优先"本地空"而不是直接轮换：`.slots.txt` 里的出口可能刚被真实
        批量打过，直接拿来测会把**上一批的窗口**算进本次结论。
        但本地窗口是保守值（3600s），不能让它挡住探针 —— 所以两轮：
        先挑 `wait_time()==0` 的，挑不到再挑任意未用过的。
        """
        avoid = avoid or set()
        n = len(self.slots)
        if n == 0:
            return None
        order = [self.slots[(self._slot_i + i) % n] for i in range(n)]
        for slot in order:
            if slot in avoid or slot in self.used_slots:
                continue
            if register_limiter(slot).wait_time() <= 0:
                self._slot_i = (order.index(slot) + 1) % n
                return slot
        for slot in order:
            if slot in avoid or slot in self.used_slots:
                continue
            self._slot_i = (order.index(slot) + 1) % n
            return slot
        return None

    def _record(self, domain: str, slot: str | None, ts: float | None = None) -> None:
        """把这一枪记进本地窗口（两个维度都记）。"""
        now = time.time() if ts is None else ts
        windowstore.record(_slot_key(slot), now)
        windowstore.record(f"domain:{domain.lower()}", now)

    def _emit(self, r: SendResult) -> None:
        self.results.append(r)
        print(f"  [{_now()}] {r.status:<8} @{r.domain:<22} {r.slot:<28} {r.detail[:90]}")
        if self.out:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            with self.out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")

    # ── 一枪 ──────────────────────────────────────────────────
    def send(
        self, domain: str, *, avoid: set[str] | None = None, force_slot: str | None = None
    ) -> SendResult:
        """建邮箱 + 解 captcha + 发码。**captcha 失败会重解**（不耗注册窗口）。

        `force_slot` 给定时**复用同一个出口** —— 用来区分两个假设：
          · 域名窗口是**全局**的 ⇒ 换不换出口都一样；
          · 域名窗口是**按 (IP, 域名)** 的 ⇒ 只有同一出口连打才会满。

        ⚠ 建邮箱 / captcha 失败时**没有发码** ⇒ 不记本地窗口（服务端没收到）。
          发码这一枪（无论成功 / 限流 / 拒绝 / 网络失败）**都要记** ——
          网络失败的那一枪可能已经落地（见 `agnes._request` 的实测）。
        """
        t0 = time.monotonic()
        slot = force_slot or self.pick_slot(avoid=avoid)
        if slot is None:
            return SendResult(_iso(), domain, "-", "-", "no-slot", "没有可用出口")
        if not force_slot:
            # 强制复用的出口**不**进 used_slots：它本来就是要被再次使用的。
            self.used_slots.append(slot)

        mail = for_domain(domain)
        try:
            emails = mail.create_mailbox(domain, 1)
        except Exception as ex:  # noqa: BLE001
            return SendResult(
                _iso(),
                domain,
                "-",
                redact.redact_url(slot),
                MAILBOX,
                f"{type(ex).__name__}: {ex}",
            )
        if not emails:
            return SendResult(
                _iso(),
                domain,
                "-",
                redact.redact_url(slot),
                MAILBOX,
                getattr(mail, "last_error", "") or "建邮箱返回空",
            )
        addr = emails[0]
        shown = redact.redact_email(addr)

        # captcha：失败就重解。它**不消耗**注册窗口，所以重试是免费的（只花时间）。
        ticket = randstr = ""
        last = ""
        for attempt in range(self.send_retries):
            try:
                ticket, randstr = self.provider.solve(addr, proxy=self.captcha_proxy)
                break
            except AgnesCaptcha as ex:
                last = str(ex)[:140]
                if attempt + 1 < self.send_retries:
                    print(
                        f"  [{_now()}] captcha 失败，重解（{attempt + 1}/{self.send_retries}）：{last}"
                    )
                    time.sleep(1.0)
        if not ticket:
            return SendResult(
                _iso(),
                domain,
                shown,
                redact.redact_url(slot),
                CAPTCHA,
                last,
                time.monotonic() - t0,
            )

        client = AgnesClient(proxy=slot)
        at = _iso()
        # 🔴 先记账再发（与 `DomainReservation.consume` 同一条不变量）：
        #    网络失败的那一枪可能已经落地，服务端可能已经记了。
        self._record(domain, slot)
        try:
            body = client.send_code(addr, ticket=ticket, randstr=randstr)
            r = SendResult(
                at,
                domain,
                shown,
                redact.redact_url(slot),
                OK,
                f"code={body.get('code')} message={body.get('message')}",
                time.monotonic() - t0,
            )
        except AgnesQuota as ex:
            r = SendResult(
                at,
                domain,
                shown,
                redact.redact_url(slot),
                _quota_dim(ex),
                str(ex)[:200],
                time.monotonic() - t0,
            )
        except AgnesRejected as ex:
            r = SendResult(
                at,
                domain,
                shown,
                redact.redact_url(slot),
                REJECTED,
                str(ex)[:200],
                time.monotonic() - t0,
            )
        except AgnesError as ex:
            r = SendResult(
                at,
                domain,
                shown,
                redact.redact_url(slot),
                NETWORK,
                str(ex)[:200],
                time.monotonic() - t0,
            )
        self._emit(r)
        return r


# ── 计划 / 成本 ────────────────────────────────────────────────────
def find_family(domains: list[str]) -> list[tuple[str, str]]:
    """域名池里的 (子域, 父域) 对。"""
    ds = sorted({d.lower() for d in domains if d})
    return [(a, b) for a in ds for b in ds if a != b and a.endswith("." + b)]


def print_plan(args, domains: list[str], slots: list[str]) -> None:
    pairs = find_family(domains)
    print("═" * 72)
    print("受控窗口测量 —— 计划（**零请求**）")
    print("═" * 72)
    print(f"收信域名：{len(domains)} 个；出口：{len(slots)} 个")
    print(
        f"captcha：headless={config.CAPTCHA_HEADLESS} "
        f"timeout={config.CAPTCHA_TIMEOUT_S}s targets={config.CAPTCHA_TARGETS}"
    )
    print(
        f"本地默认窗口：IP {config.REGISTER_WINDOW_S:.0f}s / "
        f"域名 {config.DOMAIN_WINDOW_S:.0f}s（探针会绕过它们，但会记账）"
    )

    print("\n── burst 模式（只测**阈值**，不测窗口长度）──")
    print(f"  在域名 {args.domain or domains[0] if domains else '<无>'} 上连打 fill={args.fill} 枪")
    print("  默认每枪换出口（测全局阈值）；--same-slot 则同一出口连打（测 (IP,域名) 阈值）")
    print("  判读：第 k 枪起被域名限流 ⇒ 阈值 ≤ k；全过 ⇒ 阈值 > fill")
    print(f"  成本：最多 {args.fill} 次 captcha 解")

    print("\n── recovery 模式 ──")
    print(f"  1. 在域名 {args.domain or domains[0] if domains else '<无>'} 上 fill={args.fill} 枪")
    print("     （默认每枪换一个出口；--same-slot 则同一个出口连打）")
    print("     fill 里要出现一次 `email domain` 限流才算打满")
    print(f"  2. 静默 {args.wait:.0f}s（**一枪都不打**）")
    print("  3. 在**同一个域名**上探 1 枪（默认换出口；--same-slot 则用同一个）")
    print("  判读：成功 ⇒ W ≤ wait；仍被域名限流 ⇒ W > wait")
    print(f"  成本：{args.fill + 1} 次 captcha 解 + {args.wait / 60:.1f} 分钟等待")

    print("\n── subdomain 模式 ──")
    if not pairs:
        print("  ✗ 域名池里没有 (子域, 父域) 对 —— 先加一个子域到 AR_MAIL_DOMAINS")
    else:
        for child, parent in pairs[:3]:
            print(f"  候选：父 {parent} / 子 {child}")
        print(f"  1. 打满**父域**（fill={args.fill} 枪；默认换出口，--same-slot 则同一个）")
        print("  2. 立刻探一次**子域**（默认换出口；--same-slot 则用同一个）")
        print("  判读：子域成功 ⇒ 独立窗口；子域也被域名限流 ⇒ 共窗")
        print(f"  成本：{args.fill + 1} 次 captcha 解；--reverse 再翻倍")
    print("\n⚠ 2026-10-18 实测：同一域名 3 枪（**不同出口**）全过 ⇒ 域名阈值不是")
    print("   文档旧写的 2~3。先用 --same-slot 区分「全局计数」与「按 (IP, 域名) 计数」。")
    print("⚠ 真正测量请显式 --mode recovery / --mode subdomain。")
    print("⚠ 探测期间不要跑 run.py / 别的探针。")


# ── burst ────────────────────────────────────────────────────────
def run_burst(prober: Prober, domain: str, args) -> int:
    """只测**阈值**：在同一个域名上连打 `fill` 枪，看第几枪开始被域名限流。

    ⚠ 它**不测窗口长度**：每枪都在给窗口加事件（如果被拒的也计数），
      所以"一直限流"不能反推 W。要测长度用 `recovery`（单次触碰）。
    """
    print(f"\n══ burst：@{domain} 连打 {args.fill} 枪 ══")
    slot = prober.pick_slot() if args.same_slot else None
    if slot:
        print(f"  🔴 --same-slot：全部用 {redact.redact_url(slot)}")
    for i in range(args.fill):
        r = prober.send(domain, force_slot=slot)
        if r.status == LIM_DOMAIN:
            print(f"  ⇒ 第 {i + 1} 枪起**域名限流** ⇒ 阈值 ≤ {i + 1}（本窗口内）")
            return 0
        if r.status == LIM_IP:
            print(f"  ⇒ 第 {i + 1} 枪撞**IP 维度** ⇒ IP 阈值 ≤ {i + 1}（本窗口内）")
            return 0
        if r.status in (CAPTCHA, MAILBOX, NETWORK, REJECTED):
            print(f"  ✗ 被 {r.status} 打断 —— 本次没测出来")
            return 3
    print(f"  ⇒ {args.fill} 枪全过 ⇒ 阈值 > {args.fill}，或窗口比这几枪的跨度短")
    print("     （加大 --fill 再试；注意每枪都在消耗窗口）")
    return 0


# ── recovery ──────────────────────────────────────────────────────
def run_recovery(prober: Prober, domain: str, args) -> int:
    print(f"\n══ recovery：@{domain} fill={args.fill} → 静默 {args.wait:.0f}s → 探 1 枪 ══")
    fill_slot = prober.pick_slot() if args.same_slot else None
    if fill_slot:
        print(f"  🔴 --same-slot：fill 与探测都用 {redact.redact_url(fill_slot)}")
        print("     这测的是「按 (IP, 域名) 计数」的窗口；换出口能绕过它。")
    fill: list[SendResult] = []
    for i in range(args.fill):
        r = prober.send(domain, force_slot=fill_slot)
        fill.append(r)
        if r.status == LIM_DOMAIN:
            print(f"  ✓ 第 {i + 1} 枪确认域名维度已满（后面的 fill 可以停了）")
            break
        if r.status == LIM_IP:
            print(
                f"  ✗ 第 {i + 1} 枪撞到 **IP 维度**（这个出口的 IP 窗口满了）—— "
                f"fill 无法继续；换更大的出口池或减少 --fill。"
            )
            return 3
        if r.status in (CAPTCHA, MAILBOX, NETWORK, REJECTED):
            print(f"  ✗ fill 被 {r.status} 打断 —— 本次没测出来（换时间再试）")
            return 3
    if not any(r.status == LIM_DOMAIN for r in fill):
        print(
            f"  ✗ fill {len(fill)} 枪都没撞到 `email domain` 限流 ⇒ **没打满**，"
            f"结论不成立。加大 --fill，或换 --same-slot（若域名窗口是按 IP 计的，"
            f"换出口永远打不满）。"
        )
        return 3

    t_full = time.time()
    print(
        f"  ⏳ 窗口在 {dt.datetime.fromtimestamp(t_full).strftime('%H:%M:%S')} 打满；"
        f"静默 {args.wait:.0f}s ...（Ctrl-C 中止）"
    )
    deadline = t_full + args.wait
    while time.time() < deadline:
        left = int(deadline - time.time())
        time.sleep(min(60, max(1, left)))
        if time.time() < deadline:
            print(f"    … 还剩 {int(deadline - time.time())}s", flush=True)

    # 探测必须用**同一个出口**（--same-slot）才能测「按 (IP, 域名)」的窗口；
    # 用别的出口测的是「全局域名窗口」。
    r = prober.send(domain, force_slot=fill_slot)
    delta = time.time() - t_full
    print(f"\n  探测结果：{r.status}（打满后 {delta / 60:.2f} 分钟）")
    if r.status == OK:
        print(f"  ⇒ **W ≤ {delta / 60:.2f} 分钟**（成功；窗口已恢复）")
        return 0
    if r.status == LIM_DOMAIN:
        print(f"  ⇒ **W > {delta / 60:.2f} 分钟**（仍是域名维度限流）")
        print("     ⚠ 这是下界：要收窄就换一个域名、用更大的 --wait 再跑一次。")
        return 0
    if r.status == LIM_IP:
        print("  ✗ 撞到的是 **IP 维度**（--same-slot 下 fill 把 IP 窗口也打满了）")
        print("     ⇒ 换一个更大的出口池 / 减少 --fill，或不用 --same-slot。")
        return 3
    print(f"  ✗ 探测被 {r.status} 打断 —— 本次无结论")
    return 3


# ── subdomain ─────────────────────────────────────────────────────
def _fill_domain(
    prober: Prober, domain: str, n: int, *, force_slot: str | None = None
) -> tuple[bool, list[SendResult]]:
    """把 `domain` 打满。返回 (是否确认打满, 结果列表)。"""
    out: list[SendResult] = []
    for i in range(n):
        r = prober.send(domain, force_slot=force_slot)
        out.append(r)
        if r.status == LIM_DOMAIN:
            print(f"  ✓ 第 {i + 1} 枪确认 @{domain} 的域名窗口已满")
            return True, out
        if r.status == LIM_IP:
            print(f"  ✗ 第 {i + 1} 枪撞到 **IP 维度**（这个出口的 IP 窗口满了）—— fill 无法继续")
            return False, out
        if r.status in (CAPTCHA, MAILBOX, NETWORK, REJECTED):
            print(f"  ✗ fill 被 {r.status} 打断 —— 本次没测出来")
            return False, out
    print(f"  ✗ {n} 枪都没撞到 @{domain} 的域名限流 ⇒ 没打满，结论不成立")
    return False, out


def run_subdomain(prober: Prober, child: str, parent: str, args) -> int:
    print(f"\n══ subdomain：父 {parent} / 子 {child} ══")
    fill_slot = prober.pick_slot() if args.same_slot else None
    if fill_slot:
        print(f"  🔴 --same-slot：父域 fill 与子域探测都用 {redact.redact_url(fill_slot)}")
        print("     这测的是「同一出口下父/子是否共窗」；换出口能绕过 ⇒ 说明窗口按 IP 分桶。")
    ok, _ = _fill_domain(prober, parent, args.fill, force_slot=fill_slot)
    if not ok:
        print(
            "  ⚠ 没打满父域。若 fill 枪全过，先怀疑「域名窗口按 (IP, 域名) 计」"
            "或「阈值比 --fill 大」—— 加大 --fill，或加 --same-slot 再试。"
        )
        return 3

    # 子域探测：--same-slot 用**同一个出口**（测 per-IP 共窗）；否则换新出口
    # （测全局共窗）。
    r = prober.send(child, force_slot=fill_slot)
    print(f"\n  子域探测结果：{r.status}")
    if r.status == OK:
        print("  ⇒ **父/子各自独立窗口**（按精确 hostname 计）——")
        print("     前提是父域刚被确认打满；这次子域在同一时刻还能发码。")
        if not fill_slot:
            print("     （这次子域用了**不同出口** ⇒ 只能证明「不共全局窗口」。")
            print("       要证明同一出口下也不共窗，加 --same-slot 再跑一次。）")
        return 0
    if r.status == LIM_DOMAIN:
        print("  ⇒ **父/子共窗**（服务端按可注册域计，子域不额外给名额）")
        if fill_slot:
            print("     （同一出口下共窗 ⇒ 至少按 (IP, 可注册域) 计。）")
        return 0
    if r.status == LIM_IP:
        print("  ✗ 撞到 IP 维度 —— 换一个出口再探一次")
        r = prober.send(child, avoid=set(prober.used_slots))
        if r.status == OK:
            print("  ⇒ **父/子各自独立窗口**（换出口后成功）")
            return 0
        if r.status == LIM_DOMAIN:
            print("  ⇒ **父/子共窗**（换出口后仍是域名限流）")
            return 0
        print(f"  ✗ 第二次仍被 {r.status} 打断 —— 本次无结论")
        return 3
    print(f"  ✗ 被 {r.status} 打断 —— 本次无结论")
    return 3


# ── CLI ───────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="受控测量注册窗口恢复时间 / 子域是否共窗（⚠ 消耗注册窗口）"
    )
    ap.add_argument("--mode", choices=("plan", "burst", "recovery", "subdomain"), default="plan")
    ap.add_argument("--domain", default=None, help="recovery 用哪个域名（默认第一个）")
    ap.add_argument("--parent", default=None, help="subdomain 模式的父域（默认自动找）")
    ap.add_argument("--child", default=None, help="subdomain 模式的子域（默认自动找）")
    ap.add_argument("--reverse", action="store_true", help="subdomain 再反向测一次")
    ap.add_argument(
        "--fill",
        type=int,
        default=12,
        help="打满窗口用几枪（2026-10-18 实测：域名阈值 ≈10、IP 阈值 ≈4）",
    )
    ap.add_argument(
        "--same-slot",
        action="store_true",
        help="fill 与探测用**同一个出口**（测「按 (IP, 域名) 计数」的窗口）；"
        "默认每枪换出口（测全局窗口）",
    )
    ap.add_argument(
        "--wait",
        type=float,
        default=2700.0,
        help="recovery 静默多少秒（默认 2700 = 45 分钟；实测 45.37 分钟仍限流）",
    )
    ap.add_argument("--send-retries", type=int, default=2, help="captcha 失败后重解几次")
    ap.add_argument("--captcha-retries", type=int, default=1, help="provider 内部重试次数")
    ap.add_argument(
        "--captcha-proxy",
        action="store_true",
        help="captcha 也走槽位代理（默认直连，与生产默认一致）",
    )
    ap.add_argument("--out", default=None, help="证据 JSONL 输出路径（默认不写）")
    a = ap.parse_args(argv)

    missing = config.validate()
    if missing:
        print(f"✗ 配置有问题：{'、'.join(missing)}", file=sys.stderr)
        return 1

    domains = config.mail_domains()
    slots = config.proxy_slots()
    if not domains:
        print("✗ 没有可用收信域名（AR_MAIL_DOMAINS / AR_WORKER_DOMAIN）", file=sys.stderr)
        return 1
    if not slots:
        print("✗ 没有出口（AR_PROXY_SLOTS / .slots.txt）—— 单出口会被 IP 维度污染", file=sys.stderr)
        return 1
    if not config.CAPTCHA_ENABLED:
        print("✗ AR_CAPTCHA_ENABLED=0 —— 发码需要 captcha，探针跑不了", file=sys.stderr)
        return 1

    if a.mode == "plan":
        print_plan(a, domains, slots)
        return 0

    pairs = find_family(domains)
    # 🔴 先给 `child/parent` 一个确定的值：它们只在 subdomain 分支被赋值，
    #    而 `try` 里两种模式共用同一个出口 —— 不初始化会让类型检查器（和读者）
    #    以为可能 unbound。
    child = parent = ""
    if a.mode == "subdomain":
        child, parent = a.child or "", a.parent or ""
        if not (child and parent):
            if not pairs:
                print("✗ 域名池里没有 (子域, 父域) 对 —— 用 --child/--parent 指定", file=sys.stderr)
                return 1
            child, parent = pairs[0]
        if not child.endswith("." + parent):
            print(f"✗ {child} 不是 {parent} 的子域", file=sys.stderr)
            return 1

    print("⚠ 本探针会**真的发码**（消耗服务端注册窗口）。")
    print(
        f"   captcha：headless={config.CAPTCHA_HEADLESS} "
        f"timeout={config.CAPTCHA_TIMEOUT_S}s 直连解 ticket"
    )
    print(f"   出口：{len(slots)} 个（每枪换一个，避免 IP 维度污染）")

    provider = TCaptchaProvider(
        headless=config.CAPTCHA_HEADLESS,
        timeout_s=float(config.CAPTCHA_TIMEOUT_S),
        targets=config.CAPTCHA_TARGETS,
        retries=a.captcha_retries,
        entry_url=config.CAPTCHA_ENTRY_URL,
    )
    out = Path(a.out) if a.out else None
    prober = Prober(
        slots,
        provider,
        captcha_proxy=(slots[0] if a.captcha_proxy else None),
        send_retries=a.send_retries,
        out=out,
    )

    try:
        if a.mode == "recovery":
            domain = (a.domain or domains[0]).lower()
            if domain not in [d.lower() for d in domains]:
                print(f"⚠ {domain} 不在 AR_MAIL_DOMAINS 里 —— 仍按你的指定执行")
            return run_recovery(prober, domain, a)

        if a.mode == "burst":
            domain = (a.domain or domains[0]).lower()
            if domain not in [d.lower() for d in domains]:
                print(f"⚠ {domain} 不在 AR_MAIL_DOMAINS 里 —— 仍按你的指定执行")
            return run_burst(prober, domain, a)

        rc = run_subdomain(prober, child, parent, a)
        if a.reverse and rc == 0:
            print("\n── 反向复验（打满子域 → 探父域）──")
            rc2 = run_subdomain(prober, parent, child, a)
            if rc2 != 0:
                rc = rc2
        return rc
    except KeyboardInterrupt:
        print("\n✗ 被 Ctrl-C 中止 —— 已发出的枪都记进了本地窗口。")
        return 130
    finally:
        print(
            f"\n共发出 {len(prober.results)} 枪；本地窗口已记账（state/register_windows.jsonl）。"
        )
        if out:
            print(f"证据已追加到 {out}")


if __name__ == "__main__":
    sys.exit(main())
