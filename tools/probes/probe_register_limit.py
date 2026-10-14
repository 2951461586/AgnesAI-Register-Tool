#!/usr/bin/env python
"""注册尝试限流的**边界探测**（⚠ 会消耗服务端窗口，别乱跑）。

背景
----
Agnes 对注册尝试（`/api/verification` + `/api/user/register`）有**按 IP** 的
限流，超了返回 `400 Too many registration attempts from this IP`。
窗口长度**尚未测准**（见 docs/protocol.md）。本脚本就是用来测它的。

两种模式
--------
  burst   连打 N 次，记录**第几次**开始被拦。用来估阈值 N。
          python tools/probes/probe_register_limit.py --mode burst --n 12

  window  静默 `--wait` 秒后探**一次**，记录成功/被拦；可重复 `--rounds` 轮。
          用来估窗口长度（每轮之间**不发任何其它注册类请求**）。
          python tools/probes/probe_register_limit.py --mode window --wait 600 --rounds 4

🔴 **两条硬规矩**（不遵守结论就是错的）
   1. 探测期间**不要**跑 `run.py` / 别的探针 —— 它们会消耗同一张窗口。
   2. `burst` 会**烧掉**这张 IP 的注册窗口。测完请等一个窗口再跑真实批次。

退出码：0 = 探测完成（不代表没被限流）；1 = 配置缺失。
"""

import argparse
import sys
import time

from _path import ROOT  # noqa: F401,E402

from common import config  # noqa: E402
from src import windowstore  # noqa: E402
from src.agnes import AgnesClient, AgnesError, AgnesQuota  # noqa: E402
from src.pipeline import _slot_key  # noqa: E402
from src.tempmail import TempMailClient  # noqa: E402


def _record_locally(domain: str, proxy: str | None = None) -> None:
    """把这一发**记进本地窗口**，但**不做闸门检查**。

    🔴 探针必须**绕过**本地闸门 —— 否则它永远超不过本地上限，就测不到服务端
      的阈值。但"绕过检查"不等于"不记账"：不记账的话，探针跑完之后
      **本地比服务端松**，而那个形态正是历史上 17 条 `quota` 失败的成因
      （本地以为窗口是空的，服务端不这么认为）。

    ⇒ 所以这里直接 `windowstore.record()`：只写不查。
    """
    now = time.time()
    windowstore.record(_slot_key(proxy), now)
    windowstore.record(f"domain:{domain.lower()}", now)


def _probe_once(client, mail, tag, domain):
    """建邮箱 → 发验证码。返回 `(ok, detail)`。

    🔴 `domain` 必须由调用方**轮换**传入：服务端有两道窗口（IP 维度 + 域名维度），
      而**域名阈值只有 2~3**。固定用一个域名的话，几发就会撞上域名维度 ——
      那时你测到的是**域名**的阈值，却会以为那是 **IP** 的。
    """
    try:
        emails = mail.create_mailbox(domain, 1)
    except Exception as ex:  # noqa: BLE001
        return None, f"建邮箱失败：{type(ex).__name__}: {ex}"
    if not emails:
        return None, "建邮箱返回空"
    addr = emails[0]
    try:
        client.send_code(addr)
        _record_locally(domain)
        return True, f"{addr} 发码成功"
    except AgnesQuota as ex:
        # 服务端也记了这一发（它回了限流，不是没收到）
        _record_locally(domain)
        return False, f"{addr} 被限流：{ex}"
    except AgnesError as ex:
        _record_locally(domain)
        return False, f"{addr} 其它错误：{ex}"


def mode_burst(args):
    mail = TempMailClient()
    client = AgnesClient()
    domains = [args.domain] if args.domain else config.mail_domains()
    if not domains:
        print("✗ 没有可用收信域名（AR_MAIL_DOMAINS / AR_WORKER_DOMAIN）", file=sys.stderr)
        return
    print(f"⚠ burst 模式：连打 {args.n} 次注册尝试（会烧掉本出口的窗口）")
    if len(domains) == 1:
        print(f"  🔴 **只用一个域名（{domains[0]}）** ⇒ 这测的是**域名维度**的阈值，")
        print("     **不是 IP 维度**。域名阈值实测只有 2~3 ⇒ 几发就会撞上，")
        print("     而你看到的消息两条一模一样（都是 `Too many registration attempts`），")
        print("     只看消息**分不出**是哪一维。要测 IP 维度：多配几个可用域名，")
        print("     或去掉 `--domain` 让域名轮换起来。")
    else:
        print(f"  域名轮换（{len(domains)} 个：{'、'.join(domains[:4])}…）")
        print(f"  ⇒ 前 {len(domains) * 2} 发之内不会撞域名维度，")
        print("     所以更早被拦就是 **IP 维度**（这才是本模式想测的东西）。")
    first_limited = None
    for i in range(1, args.n + 1):
        domain = domains[(i - 1) % len(domains)]
        ok, detail = _probe_once(client, mail, i, domain)
        mark = "OK " if ok else "LIM"
        print(f"  #{i:>2} [{mark}] @{domain} {detail}")
        if ok is False and first_limited is None:
            first_limited = i
        if args.interval:
            time.sleep(args.interval)
    if first_limited:
        print(f"\n结论：第 {first_limited} 次起被限流（阈值 ≈ {first_limited - 1} 次/窗口）")
    else:
        print(f"\n结论：{args.n} 次全部成功 —— 阈值 > {args.n}，加大 --n 再试")
    print("⚠ 现在这张窗口是热的：跑真实批次前请等一个窗口（或换出口）。")


def mode_window(args):
    mail = TempMailClient()
    client = AgnesClient()
    domains = [args.domain] if args.domain else config.mail_domains()
    if not domains:
        print("✗ 没有可用收信域名", file=sys.stderr)
        return
    print(f"⚠ window 模式：静默 {args.wait}s 后探一次，共 {args.rounds} 轮")
    print("   探测期间**不要**发任何其它注册类请求。")
    if len(domains) == 1:
        print(f"  🔴 只用一个域名（{domains[0]}）⇒ 探到的是**域名**窗口，不是 IP 窗口。")
    for r in range(1, args.rounds + 1):
        print(f"  第 {r} 轮：静默 {args.wait}s ...", flush=True)
        time.sleep(args.wait)
        # ⚠ 每轮换一个域名：固定一个的话，第一轮就把**域名**窗口打满，
        #   后面每轮被拦的都是域名维度 —— 测不到 IP 窗口。
        domain = domains[(r - 1) % len(domains)]
        ok, detail = _probe_once(client, mail, r, domain)
        if ok:
            print(f"  第 {r} 轮 [OK ] @{domain} {detail}")
            print(f"\n结论：静默 {args.wait}s 后恢复 ⇒ 窗口 ≤ {args.wait}s")
            return
        print(f"  第 {r} 轮 [LIM] @{domain} {detail}")
    print(f"\n结论：静默 {args.rounds} × {args.wait}s 后仍被拦 ⇒ 窗口 > {args.rounds * args.wait}s")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Agnes 注册尝试限流边界探测")
    ap.add_argument("--mode", choices=("burst", "window"), default="burst")
    ap.add_argument("--n", type=int, default=12, help="burst 模式的连打次数")
    ap.add_argument(
        "--domain",
        default=None,
        help="burst 模式固定用这一个域名（⚠ 那就变成测**域名**阈值，不是 IP 阈值）；"
        "不传则用 `AR_MAIL_DOMAINS` 轮换",
    )
    ap.add_argument("--interval", type=float, default=0.0, help="burst 模式每次之间的间隔（秒）")
    ap.add_argument("--wait", type=float, default=600.0, help="window 模式每轮静默多久（秒）")
    ap.add_argument("--rounds", type=int, default=4, help="window 模式的轮数")
    args = ap.parse_args(argv)

    missing = config.validate()
    if missing:
        print(f"✗ 配置有问题：{'、'.join(missing)}", file=sys.stderr)
        return 1
    if args.mode == "burst":
        mode_burst(args)
    else:
        mode_window(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
