#!/usr/bin/env python
"""出口池体检：每个槽位的**真实出口 IP**，以及 Agnes 是否可达。

为什么不能只做 TCP 连通检查
--------------------------
Intern-Register-Tool 实测过：本机开着 Clash TUN 时，连一个**不可达**的端口
只要 0.02s（中国到美国不可能是 20ms）—— 那是连到了本地虚拟网卡。
"TCP 能连" ≠ "代理可用"。所以这里**真的发一个请求拿出口 IP**。

用法：
    python tools/probes/probe_slots.py                 # 用 AR_PROXY_SLOTS(_FILE)
    python tools/probes/probe_slots.py --urls http://127.0.0.1:7901,...

⚠ 探测用的是一次**只读**请求（`/api/user/self` 不带鉴权），**不消耗**注册窗口。
"""

import argparse
import sys

import requests
from _path import ROOT  # noqa: F401,E402

from common import config  # noqa: E402
from src.redact import redact_url  # noqa: E402

IP_ECHO = "https://api.ipify.org?format=json"


def egress_ip(slot: str | None) -> str:
    """经这个出口看到的公网 IP。失败返回 `EXC ...`。"""
    px = config.proxies(slot) if slot else None
    s = requests.Session()
    s.trust_env = False
    try:
        r = s.get(IP_ECHO, proxies=px, timeout=20)
        return r.json().get("ip", "?")
    except Exception as ex:  # noqa: BLE001
        return f"EXC {type(ex).__name__}: {ex}"


def agnes_reachable(slot: str | None) -> str:
    """Agnes 控制台是否可达（只看**有没有 HTTP 响应**，不看业务码）。"""
    px = config.proxies(slot) if slot else None
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": config.USER_AGENT})
    try:
        r = s.get(f"{config.PLATFORM_BASE}/api/user/self", proxies=px, timeout=20)
        return f"HTTP {r.status_code}"
    except Exception as ex:  # noqa: BLE001
        return f"EXC {type(ex).__name__}: {ex}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="槽位出口体检")
    ap.add_argument("--urls", default=None, help="逗号分隔的槽位 URL（默认读配置）")
    args = ap.parse_args(argv)

    if args.urls:
        slots = [u.strip() for u in args.urls.split(",") if u.strip()]
    else:
        try:
            slots = config.proxy_slots()
        except (ValueError, OSError) as ex:
            print(f"✗ 读槽位配置失败：{ex}", file=sys.stderr)
            return 1

    targets = slots or [None]
    if not slots:
        print("ℹ 未配置槽位池 —— 体检**直连 / AR_PROXY** 这一个出口。")

    print(f"{'槽位':<34} {'出口 IP':<18} Agnes")
    ips = []
    for slot in targets:
        ip = egress_ip(slot)
        reach = agnes_reachable(slot)
        ips.append(ip)
        shown = redact_url(slot) if slot else (redact_url(config.AR_PROXY) or "(直连)")
        print(f"{shown:<34} {ip:<18} {reach}")

    real = [ip for ip in ips if not ip.startswith("EXC")]
    dupes = {ip for ip in real if real.count(ip) > 1}
    print()
    if dupes:
        print(f"⚠ 出口 IP 有重复：{sorted(dupes)} —— 重复的槽位共用同一张限流窗口，")
        print("   把它们都算成'一个出口'（吞吐乘数小于槽位数）。")
    elif len(real) > 1:
        print(f"✅ {len(real)} 个出口 IP 两两不同 ⇒ 注册窗口有 {len(real)} 张。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
