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

  🔴 **最强的判据是横向对照**：如果**多数出口**一起失败，那就是链路坏时段，
  **任何删槽位的结论都无效**。实测过：一轮里 10 个老槽位 17~50%，而**刚加的
  新代理也 42%** —— 一个新出口不可能"是坏槽位"，所以那一轮的高低全是链路侧的
  （那个新代理在坏时段之前是 **0%**）。工具会主动提示这一点。

⚠ 探测用的是**只读**请求（`/api/user/self` 不带鉴权 ⇒ 401），
  **不消耗**注册窗口。

⚠ SOCKS5 代理必须写 `socks5h://`（**远端**解析 DNS）。写 `socks5://` 时本地
  DNS 被 Clash 劫持会得到 fake-IP（`198.18.x.x`）并报 `0x04 Host unreachable`
  —— 详见 `common/config/proxy.py` 的出口代理注释。
"""

import argparse
import sys

import requests
from _path import ROOT  # noqa: F401,E402

from common import config  # noqa: E402
from src import preflight as _preflight  # noqa: E402

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
        print(f"{_preflight.label(slot):<34} {ip:<18} {reach}")

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
    """按出口的失败率。

    🔴 **实现搬去了 `src/preflight.py`** —— 那里是**唯一实现**，
      `run.py --preflight` 用的是同一份。两处各写一遍必然漂移，而漂移的
      形态是"探针说可以跑、跑批却拦下来"（判据不一致）。
    """
    print()
    print("─" * 78)
    print(_preflight.run(targets, repeat=n).describe())


if __name__ == "__main__":
    sys.exit(main())
