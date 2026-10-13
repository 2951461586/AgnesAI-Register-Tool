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
from src.agnes import AgnesClient, AgnesError, AgnesQuota  # noqa: E402
from src.tempmail import TempMailClient  # noqa: E402


def _probe_once(client, mail, tag):
    """建邮箱 → 发验证码。返回 `(ok, detail)`。"""
    try:
        emails = mail.create_mailbox(config.WORKER_DOMAIN, 1)
    except Exception as ex:  # noqa: BLE001
        return None, f"建邮箱失败：{type(ex).__name__}: {ex}"
    if not emails:
        return None, "建邮箱返回空"
    addr = emails[0]
    try:
        client.send_code(addr)
        return True, f"{addr} 发码成功"
    except AgnesQuota as ex:
        return False, f"{addr} 被限流：{ex}"
    except AgnesError as ex:
        return False, f"{addr} 其它错误：{ex}"


def mode_burst(args):
    mail = TempMailClient()
    client = AgnesClient()
    print(f"⚠ burst 模式：连打 {args.n} 次注册尝试（会烧掉本 IP 的窗口）")
    first_limited = None
    for i in range(1, args.n + 1):
        ok, detail = _probe_once(client, mail, i)
        mark = "OK " if ok else "LIM"
        print(f"  #{i:>2} [{mark}] {detail}")
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
    print(f"⚠ window 模式：静默 {args.wait}s 后探一次，共 {args.rounds} 轮")
    print("   探测期间**不要**发任何其它注册类请求。")
    for r in range(1, args.rounds + 1):
        print(f"  第 {r} 轮：静默 {args.wait}s ...", flush=True)
        time.sleep(args.wait)
        ok, detail = _probe_once(client, mail, r)
        if ok:
            print(f"  第 {r} 轮 [OK ] {detail}")
            print(f"\n结论：静默 {args.wait}s 后恢复 ⇒ 窗口 ≤ {args.wait}s")
            return
        print(f"  第 {r} 轮 [LIM] {detail}")
    print(f"\n结论：静默 {args.rounds} × {args.wait}s 后仍被拦 ⇒ 窗口 > {args.rounds * args.wait}s")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Agnes 注册尝试限流边界探测")
    ap.add_argument("--mode", choices=("burst", "window"), default="burst")
    ap.add_argument("--n", type=int, default=12, help="burst 模式的连打次数")
    ap.add_argument("--interval", type=float, default=0.0, help="burst 模式每次之间的间隔（秒）")
    ap.add_argument("--wait", type=float, default=600.0, help="window 模式每轮静默多久（秒）")
    ap.add_argument("--rounds", type=int, default=4, help="window 模式的轮数")
    args = ap.parse_args(argv)

    missing = config.validate()
    if missing:
        print(f"✗ 缺少必需配置：{'、'.join(missing)}", file=sys.stderr)
        return 1
    if args.mode == "burst":
        mode_burst(args)
    else:
        mode_window(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
