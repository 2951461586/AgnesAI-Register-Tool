"""限速器工厂 + 按维度挑桶 + 换出口：**策略**层（不碰账号形状）。

（从 `src/pipeline/limiter.py` 拆出；对外仍由 `limiter` / `pipeline` 转出。）
"""

import hashlib
import threading

from common import config

from ..proxypool import SlotPoolEmpty
from ..redact import redact_url
from .window import SlidingWindow

# 限速器。**按维度分桶**。
#
# 🔴 服务端有**两道**注册尝试窗口（IP 维度 + 域名维度，见 config），所以本地
#    也要两道，**两者都要过**：
#      · `register_limiter(slot)` —— IP 维度，按出口分桶（多出口 = 多张窗口）
#      · `domain_limiter(domain)` —— 域名维度，**全局**分桶（多域名 = 多张窗口）
#    只配一个就会从另一个维度撞服务端。
#
# 🔴 两张窗口的事件都**落盘**（`windowstore`），因为进程内的窗口每次启动即清零

_REGISTER_LIMITERS: dict = {}
_DOMAIN_LIMITERS: dict = {}
_DOMAIN_USED: dict = {}
_LIMITER_LOCK = threading.Lock()


def _slot_key(slot: str | None) -> str:
    """槽位 → 窗口存储键。**绝不能把代理串原样写进 `state/`**。

    🔴 槽位串是 `scheme://user:pass@host:port`（或 `host:port:user:pass`），
      直接当键就是把**代理账密**持久化到磁盘。`state/` 虽在 `.gitignore` 中，
      但那是"不被提交"，不是"不该落盘" —— 与 `SlotStats.label` 是同一类事故
      （那边实测把密码打进了收尾报告）。

    形态 `ip:<脱敏 host:port>#<短哈希>`，两个部分都必需：
      · 保留 `host:port` —— 排查时能对上报告里的槽位标签；
      · 末尾哈希区分**同 host:port 的不同槽位** —— 本项目 `.slots.txt` 的 10 个
        槽位恰好是"同 host:port、不同 sid"，只留脱敏串会让它们**塌成一个键**，
        于是 10 张窗口变成 1 张（吞吐掉 10 倍，而且**不报错**）。
    """
    if not slot:
        return "ip:direct"
    shown = redact_url(slot)
    # `host:port:user:pass` → `host:port:***:***`；`http://u:p@h:port` → `http://***@h:port`
    hostpart = shown.split("://", 1)[-1].split("@")[-1]
    hostport = ":".join(hostpart.split(":")[:2]) or hostpart
    digest = hashlib.sha256(slot.encode("utf-8")).hexdigest()[:8]
    return f"ip:{hostport}#{digest}"


def register_limiter(slot: str | None = None) -> SlidingWindow:
    """取**某个出口**的注册尝试（验证码 + 注册）限速器（懒建 —— 测试改了 config 也能生效）。"""
    key = slot or ""
    with _LIMITER_LOCK:
        lim = _REGISTER_LIMITERS.get(key)
        if lim is None:
            lim = SlidingWindow(
                config.REGISTER_MAX_PER_WINDOW,
                config.REGISTER_WINDOW_S,
                key=_slot_key(slot),
            )
            _REGISTER_LIMITERS[key] = lim
        return lim


def domain_limiter(domain: str) -> SlidingWindow:
    """取**某个收信域名**的注册尝试限速器（**全局**，跨出口共享）。

    🔴 它与 `register_limiter` 是**两道独立的闸门**：服务端的域名维度窗口
      不是按出口计的（实测：用全新出口打已用过的域名仍然命中），
      所以本地也必须全局分桶 —— 按出口分桶等于放行 N 倍。
    """
    key = (domain or "").lower()
    with _LIMITER_LOCK:
        lim = _DOMAIN_LIMITERS.get(key)
        if lim is None:
            # ⚠ 域名进键是安全的（它不是凭据，台账里的 `mail_domain` 本就是明文）；
            #   出口才需要 `_slot_key()` 脱敏。
            lim = SlidingWindow(
                config.DOMAIN_MAX_PER_WINDOW,
                config.DOMAIN_WINDOW_S,
                key=f"domain:{key}",
            )
            _DOMAIN_LIMITERS[key] = lim
        return lim


def pick_domain(domains, *, exclude: str | None = None) -> str:
    """在域名池里挑一个（**最少使用优先**），尽量避开 `exclude`。

    与槽位池同一个理由：轮换取用会让某个域名先把窗口用满，总吞吐反而更低。
    """
    cands = [d for d in (domains or []) if d != exclude] or list(domains or [])
    if not cands:
        return ""
    with _LIMITER_LOCK:
        d = min(cands, key=lambda x: (_DOMAIN_USED.get(x, 0), x))
        _DOMAIN_USED[d] = _DOMAIN_USED.get(d, 0) + 1
        return d


def _pick_domain_with_room(domains, *, exclude: str, need: int) -> str:
    """挑一个**装得下 `need` 格**的域名（最少使用优先）。没有就返回 `""`。

    🔴 与 `pick_domain` 的差别：那个只按"最少使用"挑，会挑到一个**同样满**的
      域名 ⇒ 白白多转一圈，而每转一圈都消耗一次尝试（最后以 `quota_guard`
      收场，日志里看上去像"域名换了个遍都不行"）。这个先筛"装得下"。
    """
    cands = [d for d in (domains or []) if d != exclude and domain_limiter(d).room() >= need]
    if not cands:
        return ""
    with _LIMITER_LOCK:
        d = min(cands, key=lambda x: (_DOMAIN_USED.get(x, 0), x))
        _DOMAIN_USED[d] = _DOMAIN_USED.get(d, 0) + 1
        return d


def _is_domain_limit(ex) -> bool:
    """这次限流是不是**域名维度**的（换出口没用，得换域名）。"""
    return "email domain" in str(ex).lower()


def _is_ip_limit(ex) -> bool:
    """这次限流是不是**IP 维度**的（换域名没用，得换出口）。"""
    return "from this ip" in str(ex).lower()


def _register_backoff_s() -> float:
    """换不动维度时退避多久（秒）。**封顶 45s**。

    🔴 不取"等满一个窗口"：窗口长度尚未测准（见 `docs/protocol.md` §3），
      按账号等满一个窗口会把批量拖死。封顶后重试仍失败，就把这条记录标成
      `quota` 失败 —— 那是**诚实**的失败（试过了，被限流），比挂着不动好。
    """
    per_ip = config.REGISTER_WINDOW_S / max(1, config.REGISTER_MAX_PER_WINDOW)
    per_dom = config.DOMAIN_WINDOW_S / max(1, config.DOMAIN_MAX_PER_WINDOW)
    return max(5.0, min(45.0, max(per_ip, per_dom)))


def _slot_has_room(slot: str | None) -> bool:
    """这个出口的 IP 窗口**现在**还有名额吗（只查询，不消费）。

    🔴 存在的理由与 `_pick_domain_with_room` **完全同形**，只是换了维度：
      `SlotPool` 的"最少使用"是**进程内**计数，新进程启动时全为 0 ⇒ 排序
      退化成列表顺序 ⇒ 先借到上一批刚打满的出口，白转一圈才以 `quota_guard`
      收场。实测（2026-10-14）：一批 7 个跑完后立刻再跑，4 次尝试全撞在
      满窗口上，而池里其实还有 3 个出口是空的。

    ⚠ 用 `LIMITER_MAX_WAIT_S` 作为判据（而不是 `room() > 0`），因为
      "还剩 1 格但要等 59 分钟"在本项目的语义里就是**不可用** ——
      `provision` 的重试循环用的正是同一个阈值。
    """
    return register_limiter(slot).wait_time() <= config.LIMITER_MAX_WAIT_S


def _rotate_slot(slot_pool, holder, *, error: str) -> bool:
    """把这条链换到**另一个出口**。换成了返回 `True`。

    🔴 为什么是换出口，而不是原地重试：注册限流是**按 IP** 的。原地重试撞的
      还是同一张窗口；换一个出口就是一张**全新**的窗口。

    ⚠ 旧出口要**归还**（并按失败记账）—— 否则池子会逐渐少一个，而现象是
      "批量慢慢变慢/超时"，不是"池子漏了"。
    ⚠ `holder.proxy` 必须一起改：消费者最后归还的是 `holder.proxy` 指向的那个，
      不改就会**双重归还旧出口、而新出口永远漏在外面**。
    """
    if not slot_pool or len(slot_pool) < 2:
        return False
    old = holder.proxy
    try:
        # 🔴 带 `prefer=_slot_has_room`：换出口的目的是换一张**新的窗口**，
        #    而换到一个同样满的出口等于没换（还白烧一次尝试）。
        new = slot_pool.acquire(timeout=5, prefer=_slot_has_room)
    except SlotPoolEmpty:
        return False
    if not new or new == old:
        # 只借到同一个（或没借到）⇒ 换不动，让调用方退避重试。
        if new:
            slot_pool.release(new)
        return False
    slot_pool.release(old, ok=False, error=f"quota {error[:120]}")
    holder.proxy = new
    return True


__all__ = [
    "_REGISTER_LIMITERS",
    "_DOMAIN_LIMITERS",
    "_DOMAIN_USED",
    "_LIMITER_LOCK",
    "_slot_key",
    "register_limiter",
    "domain_limiter",
    "pick_domain",
    "_pick_domain_with_room",
    "_is_domain_limit",
    "_is_ip_limit",
    "_register_backoff_s",
    "_slot_has_room",
    "_rotate_slot",
]
