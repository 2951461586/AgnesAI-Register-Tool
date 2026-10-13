#!/usr/bin/env python
"""收信域名探测：哪些域名真能收到 Agnes 的验证码邮件。

⚠ **每次探测都会消耗一次注册尝试窗口**（发验证码是注册类请求）。
   所以默认只探一个域名，多个域名请显式 `--domains a,b` 并接受烧窗口的代价。

用法：
    python tools/probes/probe_domains.py                     # 只探 AR_WORKER_DOMAIN
    python tools/probes/probe_domains.py --domains a.com,b.com --delay 5

退出码：0 = 至少一个域名收到了码；1 = 一个都没收到 / 配置缺失。
"""

import argparse
import sys
import time

from _path import ROOT  # noqa: F401,E402

from common import config  # noqa: E402
from src.agnes import AgnesClient, AgnesQuota  # noqa: E402
from src.tempmail import TempMailClient, now_ms  # noqa: E402


def probe_one(domain: str, timeout: int) -> bool:
    mail = TempMailClient()
    client = AgnesClient()
    try:
        emails = mail.create_mailbox(domain, 1)
    except Exception as ex:  # noqa: BLE001
        print(f"  ✗ 建邮箱失败：{type(ex).__name__}: {ex}")
        return False
    if not emails:
        print("  ✗ Worker 没返回地址")
        return False
    addr = emails[0]
    print(f"  邮箱 {addr}")
    sent_at = now_ms()
    try:
        client.send_code(addr)
    except AgnesQuota as ex:
        print(f"  ⚠ 发码被限流（不是域名的问题）：{ex}")
        return False
    except Exception as ex:  # noqa: BLE001
        print(f"  ✗ 发码失败：{type(ex).__name__}: {ex}")
        return False
    print(f"  已发码，等 {timeout}s ...")
    code = mail.wait_for_code(addr, since_ts=sent_at, timeout=timeout)
    if code:
        print(f"  ✅ 收到验证码 {code}（轮询 {mail.last_polls} 次）")
        return True
    if mail.last_error:
        print(f"  ✗ 邮箱读不出来：{mail.last_error}")
    else:
        print(f"  ✗ {timeout}s 内没收到（轮询 {mail.last_polls} 次）—— 该域名可能收不到 Agnes 的信")
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="收信域名探测（⚠ 消耗注册窗口）")
    ap.add_argument("--domains", default=None, help="逗号分隔的域名（默认 AR_WORKER_DOMAIN）")
    ap.add_argument("--delay", type=float, default=3.0, help="域名之间的间隔（秒）")
    ap.add_argument("--timeout", type=int, default=90, help="每个域名等信超时（秒）")
    args = ap.parse_args(argv)

    missing = config.validate()
    if missing:
        print(f"✗ 缺少必需配置：{'、'.join(missing)}", file=sys.stderr)
        return 1

    domains = [d.strip() for d in (args.domains or config.WORKER_DOMAIN).split(",") if d.strip()]
    print(f"⚠ 探测 {len(domains)} 个域名 —— 每个都消耗一次注册尝试窗口。")
    ok = 0
    for i, d in enumerate(domains):
        print(f"\n[{i + 1}/{len(domains)}] @{d}")
        if probe_one(d, args.timeout):
            ok += 1
        if i + 1 < len(domains):
            time.sleep(args.delay)
    print(f"\n结果：{ok}/{len(domains)} 个域名收到了验证码。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
