#!/usr/bin/env python
"""出口池体检：每个槽位的**真实出口 IP**，以及 Agnes 是否可达 / 失败率。

为什么不能只做 TCP 连通检查
--------------------------
Intern-Register-Tool 实测过：本机开着 Clash TUN 时，连一个**不可达**的端口
只要 0.02s（中国到美国不可能是 20ms）—— 那是连到了本地虚拟网卡。
"TCP 能连" ≠ "代理可用"。所以这里**真的发一个请求拿出口 IP**。

用法：
    python tools/probes/probe_slots.py                 # 出口 IP + 可达性
    python tools/probes/probe_slots.py --repeat 15     # 再加"按出口的失败率"
    python tools/probes/probe_slots.py --urls http://127.0.0.1:7901,...

🔴 **失败率必须看，而且要够多样本**（`--repeat`）：

  本项目差点因为**样本量不足**把一个**好**槽位从 `.slots.txt` 里删掉 ——
  某出口 5 次 `0/5` 全 `SSLError`，看上去"持续坏掉"，而几分钟后同一个出口
  `15/15` 全好。失败是**时间聚集**的（几秒~几分钟的坏时段影响当时**所有**
  出口），**不是按槽位稳定分布的**。

  ⇒ 判"坏槽位"要：① 看**失败率**而不是失败数；② 样本量够；
    ③ **在多个时间点重复测**。单一时刻的 `0/5` 什么都证明不了。

⚠ 探测用的是**只读**请求（`/api/user/self` 不带鉴权 ⇒ 401），
  **不消耗**注册窗口。
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

import requests
from _path import ROOT  # noqa: F401,E402

from common import config  # noqa: E402
from src.proxypool import SlotStats  # noqa: E402
from src.redact import redact_url  # noqa: E402

IP_ECHO = "https://api.ipify.org?format=json"
# 只读 + 无鉴权 ⇒ 401。**不是**注册类端点，所以不消耗注册窗口。
AGNES_PROBE = f"{config.PLATFORM_BASE}/api/user/self"


def _session():
    """干净会话：**必须** `trust_env = False`，否则环境代理会静默盖掉槽位。"""
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": config.USER_AGENT})
    return s


def egress_ip(slot: str | None) -> str:
    """经这个出口看到的公网 IP。失败返回 `EXC ...`。"""
    px = config.proxies(slot) if slot else None
    try:
        r = _session().get(IP_ECHO, proxies=px, timeout=20)
        return r.json().get("ip", "?")
    except Exception as ex:  # noqa: BLE001
        return f"EXC {type(ex).__name__}: {ex}"


def agnes_reachable(slot: str | None) -> str:
    """Agnes 控制台是否可达（只看**有没有 HTTP 响应**，不看业务码）。"""
    px = config.proxies(slot) if slot else None
    try:
        r = _session().get(AGNES_PROBE, proxies=px, timeout=20)
        return f"HTTP {r.status_code}"
    except Exception as ex:  # noqa: BLE001
        return f"EXC {type(ex).__name__}: {ex}"


def agnes_rate(slot: str | None, n: int) -> tuple:
    """连打 `n` 次 Agnes，返回 `(成功数, {异常类型: 次数})`。

    🔴 与 `agnes_reachable` 的差别：那个是**一次**取样，只能回答"通不通"；
      这个回答"**多常**不通" —— 而判"坏槽位"靠的正是后者（见模块 docstring）。
    """
    px = config.proxies(slot) if slot else None
    ok, errs = 0, {}
    for _ in range(n):
        try:
            _session().get(AGNES_PROBE, proxies=px, timeout=25)
            ok += 1
        except Exception as ex:  # noqa: BLE001
            name = type(ex).__name__
            errs[name] = errs.get(name, 0) + 1
    return ok, errs


def _label(slot: str | None) -> str:
    """报告用的短标签 —— 🔴 **不能泄凭据，也不能让槽位长得一样**。

    ⚠ 只用 `redact_url(slot)` 是**不够**的：本项目 `.slots.txt` 的 10 个槽位是
      "同 host:port、不同 sid"，脱敏后**全部长成一样** ⇒ 表格里根本看不出是
      哪个出口在失败 —— 而那正是本工具要回答的问题（实测踩过：`--repeat`
      表格里 0/3 全坏的那一行跟其它 9 行长得一模一样，无法定位）。

    所以复用 `SlotStats.label`（它已由用例钉住"不泄代理密码"），
    形态是 `host:port#<短哈希>`。
    """
    if not slot:
        return redact_url(config.AR_PROXY) or "(直连)"
    return SlotStats(url=slot).label


def main(argv=None):
    ap = argparse.ArgumentParser(description="槽位出口体检")
    ap.add_argument("--urls", default=None, help="逗号分隔的槽位 URL（默认读配置）")
    ap.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="每个出口连打几次（>1 时额外输出**失败率**；判坏槽位建议 ≥15）",
    )
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
        print(f"{_label(slot):<34} {ip:<18} {reach}")

    real = [ip for ip in ips if not ip.startswith("EXC")]
    dupes = {ip for ip in real if real.count(ip) > 1}
    print()
    if dupes:
        print(f"⚠ 出口 IP 有重复：{sorted(dupes)} —— 重复的槽位共用同一张限流窗口，")
        print("   把它们都算成'一个出口'（吞吐乘数小于槽位数）。")
    elif len(real) > 1:
        print(f"✅ {len(real)} 个出口 IP 两两不同 ⇒ 注册窗口有 {len(real)} 张。")

    if args.repeat > 1:
        _rate_section(targets, args.repeat)
    return 0


def _rate_section(targets, n: int) -> None:
    """按出口的失败率。见模块 docstring：判"坏槽位"看这个，不看失败数。"""
    print()
    print("─" * 78)
    print(f"失败率（每出口 {n} 次 GET /api/user/self，无鉴权 ⇒ **不消耗注册窗口**）")
    print(f"{'槽位':<34} {'成功':<10} {'失败率':<8} 失败形态")
    print("-" * 78)

    # 跨槽位并行、槽位内串行（保持每个出口的请求是有先后顺序的真实形态）
    with ThreadPoolExecutor(max_workers=min(len(targets), 11)) as ex:
        measured = list(ex.map(lambda s: agnes_rate(s, n), targets))

    failed = []
    for slot, (ok, errs) in zip(targets, measured, strict=True):
        rate = 1 - ok / n
        if rate:
            failed.append((_label(slot), rate))
        print(f"{_label(slot):<34} {ok}/{n:<8} {rate:>6.0%}   {errs or ''}")
    print("-" * 78)

    # 🔴 样本量警告 —— 这条是**实测教训**，不是防御性提醒。
    if n < 10:
        print(f"⚠ 每出口只测了 {n} 次 ⇒ **样本量不足**，不能据此判定'某个出口坏了'。")
        print("   实测过：同一出口 5 次 `0/5` 全 SSLError，几分钟后 15/15 全好。")
        print("   失败是**时间聚集**的 ⇒ 判坏槽位要在**多个时间点**重复测。")
    if failed:
        print(f"⚠ 有失败率的出口：{[(x, f'{r:.0%}') for x, r in failed]}")
        print("   撞到 network 的正确反应是**换出口重试**，而不是把出口拉黑。")
    else:
        print(f"✅ 全部 {n}/{n} —— 当前没有持续坏掉的出口。")


if __name__ == "__main__":
    sys.exit(main())
