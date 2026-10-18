#!/usr/bin/env python
"""captcha 出口质量体检：**直连 vs 槽位代理**，以及 `uip` 不一致会不会被拒。

为什么需要它
------------
captcha provider 必须在**同一个出口**上完成两件事：
  ① 浏览器解出 `ticket`（TCaptcha 会话带 `uip`）；
  ② 主流水线用 `send_code` 把 `ticket` 交给 Agnes。

如果这两步出口不同，服务端**可能**因为 `uip` 不一致拒掉 ticket。这是个
**保守假设**（写在 `src/captcha.py` 的模块 docstring 里），但一直**没测过**。
同时，live 跑批里「等 captcha 图片超时」占了失败的大头 —— 而它是**出口质量**
问题，不是 solver 问题。所以需要两个数：

  A. 直连 vs 代理的**求解成功率与耗时**（`--mode`）
  B. 直连解出的 ticket，**换一个出口发码**是否被拒（`--uip-test`）

⚠ **A 段不消耗注册窗口**：`TCaptchaProvider.solve()` 会
  `page.route("**/api/verification*", abort)` 把前端发码请求拦掉，所以它只打
  TCaptcha 端点。可以在额度紧张时放心跑。

🔴 **B 段会消耗注册窗口**（真的发一枪 `/api/verification`）。所以它必须显式
   `--uip-test` 才跑。**单样本不够**：B 段只有一次成功也只能说"这一次没被拒"。
   用 `--uip-runs N` 重复 N 次（自动轮换出口与域名，互不污染窗口），
   判据才是"N/N 接受"。

用法
----
    python tools/probes/probe_captcha_egress.py --mode both --runs 3
    python tools/probes/probe_captcha_egress.py --uip-test --uip-runs 4
    python tools/probes/probe_captcha_egress.py --uip-test --domain catchmail.io

判读
----
- 直连成功率显著高 ⇒ 「captcha 直连 + 发码走代理」值得试（先跑 B 段看会不会被拒）。
- 两者差不多 ⇒ 出口不是瓶颈，别为了 captcha 改架构。
- 代理**超时**多而直连正常 ⇒ 那些槽位对浏览器流量不友好（TLS/长连接），
  应该按 `probe_slots.py` 的方法再筛一遍。
- B 段：**N/N 接受**（N ≥ 3）才把 "uip 不绑出口" 当结论；出现一次
  `Captcha verification is required` 就是**绑**，不要拿成功率解释。
"""

import argparse
import secrets
import string
import sys
import time

import _path  # noqa: F401  # 副作用导入：插 sys.path + 修控制台编码

from common import config
from src import redact, windowstore
from src.agnes import AgnesCaptcha, AgnesClient, AgnesQuota, AgnesRejected
from src.captcha import TCaptchaProvider
from src.pipeline import _slot_key, register_limiter

# B 段的结果码（不要用 True/False："被限流"与"被 captcha 拒"含义完全不同）。
UIP_OK = "accepted"
UIP_REJECTED = "uip_rejected"
UIP_QUOTA = "quota"
UIP_OTHER = "other"
UIP_SOLVE = "solve_failed"


def _rnd_email(domain: str) -> str:
    alphabet = string.ascii_lowercase + string.digits
    s = "".join(secrets.choice(alphabet) for _ in range(14))
    return f"probe{s}@{domain}"


def _try_solve(provider, email, proxy, label):
    t = time.monotonic()
    try:
        provider.solve(email, proxy=proxy)
        dt = time.monotonic() - t
        print(f"    {label:16} ✓ {dt:5.1f}s")
        return True, dt, ""
    except AgnesCaptcha as ex:
        dt = time.monotonic() - t
        msg = str(ex)[:110]
        print(f"    {label:16} ✗ {dt:5.1f}s  {msg}")
        return False, dt, msg
    except Exception as ex:  # noqa: BLE001
        dt = time.monotonic() - t
        msg = f"{type(ex).__name__}: {ex}"[:110]
        print(f"    {label:16} ✗ {dt:5.1f}s  {msg}")
        return False, dt, msg


def _summarize(rows, label):
    if not rows:
        print(f"  {label}: 无样本")
        return
    ok = sum(1 for r in rows if r[0])
    dts = [r[1] for r in rows]
    print(
        f"  {label}: {ok}/{len(rows)} 成功"
        f"｜中位 {sorted(dts)[len(dts) // 2]:.1f}s"
        f"｜最长 {max(dts):.1f}s"
    )
    kinds = {}
    for r in rows:
        if r[0]:
            continue
        key = "timeout" if "超时" in r[2] else ("verify拒绝" if "errorCode" in r[2] else "其他")
        kinds[key] = kinds.get(key, 0) + 1
    if kinds:
        print(f"       失败构成：{kinds}")


def run_egress(provider, runs, slots, domain, *, include_direct=True):
    """A 段：直连 vs 代理。**不消耗注册窗口**。"""
    print("\n══════ A. 出口质量（直连 vs 槽位）══════")
    print("⚠ 本段只打 TCaptcha 端点（前端的 /api/verification 被拦），不耗注册窗口")

    direct, proxied = [], []
    for i in range(runs):
        print(f"  第 {i + 1}/{runs} 轮")
        if include_direct:
            direct.append(_try_solve(provider, _rnd_email(domain), None, "direct"))
        for k, slot in enumerate(slots):
            tag = f"slot[{k}] {redact.redact_url(slot)[:34]}"
            proxied.append(_try_solve(provider, _rnd_email(domain), slot, tag))

    print()
    if include_direct:
        _summarize(direct, "直连")
    if slots:
        _summarize(proxied, "代理")

    if direct and proxied:
        d_rate = sum(1 for r in direct if r[0]) / len(direct)
        p_rate = sum(1 for r in proxied if r[0]) / len(proxied)
        print(
            f"\n  差值：直连 {d_rate:.0%} vs 代理 {p_rate:.0%}（{(d_rate - p_rate) * 100:+.0f}pp）"
        )
        if d_rate - p_rate >= 0.3:
            print("  ⇒ 直连显著更稳。值得测 B 段（uip 不一致是否被拒）。")
        else:
            print("  ⇒ 差异不显著。别为了 captcha 改出口架构。")
    return direct, proxied


def run_uip_test(provider, slot, domain):
    """B 段：直连解出 ticket，再用**另一个出口**发码。⚠ 消耗 1 格注册窗口。

    返回 `(status, detail)`，见 `UIP_*` 常量。**不在这里下结论** —— 单样本
    的结论由 `--uip-runs` 汇总（"这一次没被拒"不是"uip 不绑出口"）。
    """
    email = _rnd_email(domain)
    print(f"  邮箱：{redact.redact_email(email)}")
    try:
        ticket, randstr = provider.solve(email, proxy=None)
    except AgnesCaptcha as ex:
        return UIP_SOLVE, str(ex)[:160]
    print(f"  直连解出 ticket={ticket[:24]}… randstr={randstr}")
    print(f"  发码出口：{redact.redact_url(slot)}（与浏览器**不同**）")

    # 🔴 先记账再发：被限流 / 网络失败的那一枪也可能已经落地（与主流水线
    #    `DomainReservation.consume` 同一条不变量）。不记账的话探针跑完
    #    本地比服务端松，下一次真实批量会撞墙。
    windowstore.record(_slot_key(slot))
    windowstore.record(f"domain:{domain.lower()}")

    client = AgnesClient(proxy=slot)
    try:
        body = client.send_code(email, ticket=ticket, randstr=randstr)
        return UIP_OK, str(body)[:180]
    except AgnesQuota as ex:
        # 限流：**不能**判成"uip 被拒"，也不能判成"接受" —— 本次实验不成立。
        return UIP_QUOTA, str(ex)[:180]
    except AgnesRejected as ex:
        msg = str(ex)
        if "captcha" in msg.lower():
            return UIP_REJECTED, msg[:180]
        # 域名白名单 / 邮箱已注册…：与 uip 无关，本次实验不成立。
        return UIP_OTHER, msg[:180]
    except Exception as ex:  # noqa: BLE001
        return UIP_OTHER, f"{type(ex).__name__}: {ex}"[:180]


def run_uip_suite(provider, slots, domains, n):
    """重复 B 段 N 次，**每次换出口与域名**，最后给一个多样本判据。

    🔴 为什么必须换域名：域名维度阈值只有 2~3（`docs/protocol.md` §3），
      在同一个域名上连测 3 次，第 3 次会被**域名限流**打断 —— 那不是 uip 的
      结论。换域名后每枪都是一张新窗口。
    🔴 为什么必须换出口：IP 维度同理（虽然阈值更高）。
    """
    print(f"\n══════ B. uip 一致性（直连解 ticket → 代理发码）× {n} ══════")
    print("🔴 每枪**真的发一次** /api/verification ⇒ 每枪消耗 1 格注册窗口")
    if n > len(domains):
        print(
            f"  ⚠ 只准备了 {len(domains)} 个域名，{n} 枪会**重复用域名** ⇒ "
            f"后半段可能撞域名限流（不是 uip 结论）。建议 --uip-runs ≤ {len(domains)}"
        )
    rows = []
    for i in range(n):
        slot = slots[i % len(slots)]
        domain = domains[i % len(domains)]
        print(f"\n  ── 样本 {i + 1}/{n}：@{domain} → {redact.redact_url(slot)} ──")
        status, detail = run_uip_test(provider, slot, domain)
        rows.append((status, detail))
        mark = {
            UIP_OK: "✓ 接受",
            UIP_REJECTED: "✗ captcha 被拒",
            UIP_QUOTA: "⚠ 被限流（不成立）",
            UIP_SOLVE: "⚠ captcha 没解出来（不成立）",
        }.get(status, "⚠ 其它错误（不成立）")
        print(f"  {mark}：{detail[:160]}")

    ok = sum(1 for s, _ in rows if s == UIP_OK)
    rejected = sum(1 for s, _ in rows if s == UIP_REJECTED)
    valid = ok + rejected
    print(f"\n  ── B 段汇总：{ok}/{n} 接受，{rejected} 次 captcha 拒绝，有效样本 {valid}/{n} ──")
    if rejected:
        print("  ⇒ 出现 `Captcha verification is required` ⇒ **uip 绑定成立**")
        print("     ⇒ 浏览器必须与发码走**同一个出口**（AR_CAPTCHA_USE_PROXY=1）。")
    elif valid == 0:
        print("  ⇒ 没有一个有效样本（全被限流/求解失败打断）—— 换个时间再测。")
    elif valid < 3:
        print(f"  ⇒ 目前 {ok}/{valid} 接受，但有效样本 < 3 ⇒ **仍不足以下结论**。")
        print("     再跑几次（换域名/出口），或把 `--uip-runs` 调到 3~4。")
    else:
        print(f"  ⇒ **{ok}/{valid} 接受**（≥3 个有效样本）⇒ 支持「uip 不一致不被拒」。")
        print("     但这是**统计**结论：若服务端某天改策略，第一症状就是发码报")
        print("     `Captcha verification is required`；那时把 AR_CAPTCHA_USE_PROXY 打开。")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("direct", "proxy", "both"), default="both")
    ap.add_argument("--runs", type=int, default=2, help="每种模式跑几轮")
    ap.add_argument("--slot", action="append", default=None, help="指定槽位（可多次）")
    ap.add_argument("--domain", default=None, help="用哪个域名拼探针邮箱（默认第一个）")
    ap.add_argument(
        "--uip-domains",
        default=None,
        help="B 段用哪些域名（逗号分隔，默认 AR_MAIL_DOMAINS 轮换）；每个域名一枪，避免撞域名限流",
    )
    ap.add_argument("--timeout", type=float, default=None, help="覆盖 AR_CAPTCHA_TIMEOUT_S")
    ap.add_argument(
        "--captcha-retries",
        type=int,
        default=1,
        help="provider 内部重试次数（默认 1；只花时间，不耗注册窗口）",
    )
    ap.add_argument(
        "--uip-test",
        action="store_true",
        help="⚠ 额外跑 B 段：直连解 ticket + 代理发码（每枪消耗 1 格注册窗口）",
    )
    ap.add_argument(
        "--uip-runs",
        type=int,
        default=1,
        help="B 段跑几次（自动换出口与域名；≥3 才能下结论）",
    )
    a = ap.parse_args()

    if not config.CAPTCHA_ENABLED:
        print("✗ AR_CAPTCHA_ENABLED=0 —— 本探针就是测 captcha 的，请先打开")
        return 2

    slots = a.slot or config.proxy_slots()[:1]
    domain = a.domain or (config.mail_domains() or ["example.com"])[0]
    timeout = a.timeout if a.timeout is not None else float(config.CAPTCHA_TIMEOUT_S)
    provider = TCaptchaProvider(
        headless=config.CAPTCHA_HEADLESS,
        timeout_s=timeout,
        targets=config.CAPTCHA_TARGETS,
        retries=max(0, a.captcha_retries),
        entry_url=config.CAPTCHA_ENTRY_URL,
    )
    print(
        f"配置：headless={provider.headless} timeout={timeout:.0f}s "
        f"targets={provider.targets} 域名={domain}"
    )
    print(f"槽位：{[redact.redact_url(s) for s in slots]}")

    if a.mode == "direct":
        run_egress(provider, a.runs, [], domain, include_direct=True)
    elif a.mode == "proxy":
        run_egress(provider, a.runs, slots, domain, include_direct=False)
    else:
        run_egress(provider, a.runs, slots, domain, include_direct=True)

    if a.uip_test or a.uip_runs > 1:
        all_slots = a.slot or config.proxy_slots()
        if a.uip_domains:
            all_domains = [d.strip() for d in a.uip_domains.split(",") if d.strip()]
        elif a.domain:
            all_domains = [a.domain]
        else:
            all_domains = config.mail_domains() or ["example.com"]
        if not all_slots:
            print("\n⚠ --uip-test 需要至少一个槽位（AR_PROXY_SLOTS / .slots.txt）")
            return 2
        if len(all_domains) == 1 and max(1, a.uip_runs) > 1:
            print(
                "\n⚠ B 段只有 1 个域名 ⇒ 多枪会重复用同一个域名，"
                "后半段可能撞域名限流（不是 uip 结论）。"
            )
        # 🔴 优先用**本地窗口看起来空的**出口：`.slots.txt` 里的出口可能刚被
        #    上一轮探针 / 真实批量打过。不排一下，B 段可能撞 IP 维度而不是
        #    uip 结论（本地窗口是保守值，所以这里只排序、不排除）。
        all_slots = sorted(all_slots, key=lambda s: register_limiter(s).wait_time())
        run_uip_suite(provider, all_slots, all_domains, max(1, a.uip_runs))

    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
