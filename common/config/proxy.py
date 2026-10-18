"""出口代理配置：单条 `AR_PROXY` + 槽位池（多出口 = 多张 IP 窗口）。

（从 `common/config.py` 拆出；对外仍由 `common.config` 包统一转出。）
"""

import os
from pathlib import Path

from ._env import _env_bool, _env_float

# ── 出口代理（可选）───────────────────────────────────────────────
# 格式（与常见代理商控制台一致）：
#     AR_PROXY=host:port:user:pass          # 单条
#     AR_PROXY=http://user:pass@host:port   # 也接受完整 URL
#     AR_PROXY=socks5h://user:pass@host:port # SOCKS5（见下面的 🔴）
#
# 🔴 **SOCKS5 必须写 `socks5h://`，不能写 `socks5://`** —— 否则会**静默失败**：
#
#     `socks5://`   → DNS **在本地**解析，把解析结果发给代理
#     `socks5h://`  → DNS **在代理侧**解析（h = hostname）
#
#     本机实测（2026-10-14）：开着 Clash TUN 时，代理域名 `<proxy-gateway>`
#     被本地 DNS 解析成 **`198.18.1.166`**（Clash 的 fake-IP 段），于是
#     `socks5://` 把那个**假 IP** 发给代理 ⇒ `0x04 Host unreachable`；
#     换 `socks5h://` 立刻通。
#     ⚠ 同一现象还有第二个面具：裸 TCP 连代理端口只要 **0.03s** —— 那是连到了
#       Clash 的本地虚拟网卡。**"TCP 能连" ≠ "代理可用"**（与 `probe_slots`
#       的 docstring 里那条是同一个坑）。
#
# 🔴 SOCKS 需要 `requests[socks]`（PySocks），见 requirements.txt。
AR_PROXY = os.getenv("AR_PROXY", "").strip()
# 收信轮询是否也走代理（默认否）。轮询是整链瓶颈，绕代理只会更慢；
# 而 Cloudflare Worker 不关心我们的出口 IP，走代理没有任何收益。
PROXY_MAIL = _env_bool("AR_PROXY_MAIL", False)

# ── 槽位代理池（多出口并发）────────────────────────────────
# 🔴 它是**两个杠杆之一**，理由是一条**实测事实**：注册尝试限流有
#    **IP 维度**那一半（见上），所以每个出口各自有一张窗口 ⇒
#    "有多少个不同出口"是吞吐的乘数之一，只调大 `--producers` 是没用的
#    （那是同一张窗口里的名额）。
#    ⚠ 另一半是**域名维度**（`AR_MAIL_DOMAINS`）—— 两者必须一起用：
#      只加出口不加域名会撞域名窗口，只加域名不加出口会撞 IP 窗口。
#
# 来源优先级：`AR_PROXY_SLOTS_FILE` > `AR_PROXY_SLOTS`。槽位多时用文件。
AR_PROXY_SLOTS = os.getenv("AR_PROXY_SLOTS", "").strip()
AR_PROXY_SLOTS_FILE = os.getenv("AR_PROXY_SLOTS_FILE", "").strip()
# 等一个空闲槽位最多等多久（秒）。全部槽位都被占着时 `acquire()` 会阻塞。
# ⚠ 别设成 0/无穷：0 会让"全忙"瞬间变成一片假失败；无穷会让批量任务永远挂在那里。
PROXY_SLOT_TIMEOUT = _env_float("AR_PROXY_SLOT_TIMEOUT", 240)


def proxy_slots() -> list[str]:
    """读槽位清单。**没配置就返回空列表**（调用方据此退回单代理行为）。

    文件里允许一行一个，也允许逗号分隔；`#` 开头视为注释。

    🔴 注释必须**按行**判定，不能先把全文按逗号切分再逐项看 `#` ——
    后者会让注释行里的 ASCII 逗号切出**不带 `#` 的碎片**，碎片被当成槽位 URL
    （Intern-Register-Tool 实测：一行「# 端口是 7131,7119,7149」凭空产出 3 个
    假槽位，报错还是「端口 7119 的出口 IP 未知」，指向注释里的数字，极难回溯）。
    所以顺序是：先按行 → 丢掉空行/注释行 → 再在行内按逗号切。
    """
    raw = ""
    if AR_PROXY_SLOTS_FILE:
        p = Path(AR_PROXY_SLOTS_FILE)
        if not p.is_file():
            raise ValueError(f"AR_PROXY_SLOTS_FILE 指向的文件不存在：{p}")
        raw = p.read_text(encoding="utf-8")
    elif AR_PROXY_SLOTS:
        raw = AR_PROXY_SLOTS
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for item in line.split(","):
            item = item.strip()
            if item and not item.startswith("#"):
                out.append(item)
    return out


def proxies(raw: str | None = None) -> dict | None:
    """把代理串解析成 requests 的 `proxies` 字典；空则返回 `None`。

    支持两种写法：
        host:port:user:pass          → http://user:pass@host:port
        scheme://user:pass@host:port → 原样使用（可指定 socks5）

    `raw` 传 None 时用全局 `AR_PROXY`。
    """
    raw = (AR_PROXY if raw is None else raw).strip()
    if not raw:
        return None
    if "://" not in raw:
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            raw = f"http://{user}:{pwd}@{host}:{port}"
        elif len(parts) == 2:
            raw = f"http://{raw}"
        else:
            raise ValueError(
                f"代理串格式无法识别：{raw!r}（期望 host:port:user:pass "
                f"或 scheme://user:pass@host:port）"
            )
    return {"http": raw, "https": raw}


def apply_proxy(session, proxy: str | None = None) -> None:
    """把代理挂到 `requests.Session` 上（未配置则什么都不做）。

    🔴 **必须同时关掉 `trust_env`** —— 否则环境变量会**静默盖掉**
    `session.proxies`。机制在 `requests/sessions.py` 的
    `merge_environment_settings`：环境变量先 `setdefault` 进字典，
    而 `merge_setting(proxies, self.proxies)` **不覆盖已存在的键** ——
    于是 `session.proxies` 的优先级**低于**环境变量。

    现象特别隐蔽：请求**成功了**（200），只是**没走你指定的代理** ——
    既不报错也不告警，很容易误判成"代理已生效"。

    ⚠ 未配置代理时**不动 `trust_env`**：那样环境代理（本机 Clash）仍是
      默认出口，与加这个功能之前的行为一致。
    """
    px = proxies(proxy)
    if not px:
        return
    session.proxies = px
    session.trust_env = False


__all__ = [
    "AR_PROXY",
    "PROXY_MAIL",
    "AR_PROXY_SLOTS",
    "AR_PROXY_SLOTS_FILE",
    "PROXY_SLOT_TIMEOUT",
    "proxy_slots",
    "proxies",
    "apply_proxy",
]
