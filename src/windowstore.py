"""按维度分桶的滑动窗口事件存储（**跨进程持久化**）。

为什么需要它
============
`pipeline.SlidingWindow` 原本把窗口事件放在**进程内存**里，于是：

    python run.py --count 1     # 进程 A：本地窗口是空的 → 放行
    python run.py --count 1     # 进程 B：窗口**又是空的** → 又放行

而台账显示的实际用法正是一天内多次短跑（`ledger/runs/2026-10-14/` 有 24 份
快照 = 24 次 `run.py`）。⇒ 本地闸门在**跨进程**场景下形同不存在。

唯一跨进程持久的是 `state/register_quota.jsonl`，而它只计**成功**
（`used` = `ok=True`），失败一条都不进闸门 —— 所以它拦不住"每个进程都从零
开始打服务端窗口"。实测后果：17 条 `quota` 失败（撞服务端墙）——
本地以为窗口是空的，服务端不这么认为。

与 `quota.py` 的分工
--------------------
  * `quota.py` —— **爆炸半径上限**（30 / 24h，按成功计数）。防"手滑跑 500 个"。
  * 本模块   —— **与服务端窗口同构的节流**（IP 维度 + 域名维度各一张）。

两者都要，且**不能互相替代**：服务端有两道窗口，而一个"只计成功"的累计计数
既拦不住失败重试（81 条失败全不计），也拦不住"每次进程都从零"。

🔴 存储形态刻意与 `quota.py` **同构**（JSONL 追加 + 机会式压缩），理由也一样：
   追加一行是原子的，不需要"读整个文件 → 改 → 原子替换"。后者在两个进程同时
   跑时必然丢事件，而丢事件的方向是**更松** —— 正是最不该出错的方向。

🔴 写失败只告警、不抛（与 `quota.py` 同策略，与 `ledger.py` **相反**）：
   窗口状态是**加速器**，丢了只让下一轮更松，不该让整批任务失败。
   台账是**数据**，丢了就是永久丢账号，所以那边让它抛。

🔴 键里**绝不能出现代理账密**：槽位串是 `scheme://user:pass@host:port`，
   直接当键就是把凭据持久化到磁盘。键由 `pipeline._slot_key()` 构造
   （脱敏后的 host:port + 短哈希），本模块对键的内容不作任何假设。
"""

import json
import os
import time
from pathlib import Path

from common import config

from . import fsutil

# 环境变量覆盖（与 `AR_QUOTA_STATE` 同构）—— 测试靠它做隔离。
# 🔴 不隔离的话，跑一次测试就会往**真实**窗口文件里写事件，从而影响下一次
#    真实跑批的闸门（`quota.py` 那边实测踩过同样的坑）。
STATE_ENV = "AR_WINDOW_STATE"

# 压缩阈值（字节）。低于它**不做任何解析**就返回 —— 一次 `stat()` 的代价
# 换掉"每次记账都全量 parse 一遍文件"，而这个函数是每条账号链的热路径。
# 64KB ≈ 1000+ 行，而正常稳态只有几十行（窗口内的活跃事件）。
COMPACT_MIN_BYTES = 64_000

# 仓库根。⚠ 用 `__file__` 而不是 `Path.cwd()`：cwd 是调用者的，从别的目录跑
# `python tools/xxx.py` 会把窗口状态写到别处去（同 `ledger.ROOT`）。
# 单独导出是为了让"测试绝不能写进真实 `state/`"这条约定**可被断言**
# （见 `tests/test_pipeline.py::test_window_state_is_isolated_from_real_state_dir`）。
ROOT = Path(__file__).resolve().parents[1]


def state_path() -> Path:
    """窗口状态文件。`AR_WINDOW_STATE` 可覆盖（测试靠它隔离）。

    ⚠ 在**调用时**读环境变量，不受导入顺序影响 —— 与 `config` 的常量不同，
      这样测试可以在 import 之后改环境变量而不必重载模块（同 `quota.state_path`）。
    """
    override = (os.getenv(STATE_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return ROOT / "state" / "register_windows.jsonl"


def _as_ts(raw) -> float:
    """宽容地取时间戳。**坏值退回 0**，不抛（理由同 `quota._as_ts`）。

    ⚠ 这个文件是**本地**的、可能被人手改过。一个 `"ts": "yesterday"` 不该让
      整批任务炸在 `float()` 上 —— 退回 0 的后果只是这条记录被当成窗口外，
      是安全方向的降级。
    """
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _read_entries() -> list[dict]:
    """读状态文件 → 条目列表。**读不到就返回空**，不抛。

    🔴 静默降级是有意的，代价必须说清楚：文件读不出来 ⇒ 窗口看起来是空的
      ⇒ 闸门放行。这就是"状态文件是加速器、不是数据"那条约定（见 `fsutil`）。
      真正的高危失败在**台账**那一侧，那边是让它抛的。
    """
    p = state_path()
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue  # 半截行（写盘时被杀）只丢这一行，不丢整个文件
        if isinstance(d, dict) and d.get("key"):
            out.append(d)
    return out


def events(key: str, window_s: float, now: float | None = None) -> list[float]:
    """`key` 在窗口内的发生时刻，**升序**。

    🔴 升序是**契约**：`SlidingWindow` 用 `events[0]` 算"还要等多久"，
      顺序错了会把等待时间算成负数 —— 表现为"明明满了却立刻放行"，
      而那正是本地闸门唯一要防的事。
    """
    now = time.time() if now is None else now
    cutoff = now - float(window_s)
    out = [
        _as_ts(e.get("ts"))
        for e in _read_entries()
        if e.get("key") == key and _as_ts(e.get("ts")) > cutoff
    ]
    out.sort()
    return out


def record(key: str, ts: float | None = None) -> None:
    """追加一条事件。**写失败只告警，不抛**（见模块 docstring）。"""
    p = state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": round(time.time() if ts is None else ts, 3), "key": key},
            ensure_ascii=False,
        )
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as ex:
        print(
            f"  ⚠ 窗口状态写不进去（{type(ex).__name__}: {ex}）"
            f"—— 本次不记账，闸门会偏松（下一轮可能撞服务端墙）"
        )
        return
    _maybe_compact()


def drop(key: str, n: int = 1) -> None:
    """丢掉 `key` 上**最新**的 `n` 条事件（预留没用上时归还）。

    🔴 为什么需要它：域名维度的预留是按"**一个账号要打两枪**"占的
      （见 `pipeline.provision` —— 发码 + 注册共用同一张域名计数，而注册时
      域名已定死、换不了）。账号中途失败时那两格里可能有一格没用上，
      不还回去就会一直占着 —— 而一个域名一小时只有 2 格，泄漏几次就把整个
      域名锁死，现象是"什么都没干却被限流"。

    ⚠ 与 `record` 不同，这是**读-改-写**（要删行，追加做不到），所以在并发下
      是**尽力而为**：两个进程同时 drop 可能多丢一条。方向是**更松**，可接受；
      且它只在"账号失败 / 换域名"这条非热路径上被调用。

    ⚠ 时间戳可能相同（同一瞬间连占两格）⇒ 排序键带上原文件下标，
      保证"后写进去的"被当成更新的。
    """
    n = int(n)
    if n <= 0:
        return
    entries = _read_entries()
    mine = [i for i, e in enumerate(entries) if e.get("key") == key]
    if not mine:
        return
    newest = sorted(mine, key=lambda i: (_as_ts(entries[i].get("ts")), i))[-n:]
    drop_idx = set(newest)
    keep = [e for i, e in enumerate(entries) if i not in drop_idx]
    text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep)
    try:
        fsutil.atomic_write_text(state_path(), text)
    except OSError as ex:
        # 不抛：归还失败只是"那一格多占一会儿"，而它自愈（窗口滑出）。
        print(f"  ⚠ 窗口预留归还失败（{type(ex).__name__}: {ex}）—— 那一格会一直占着")


def _maybe_compact() -> None:
    """行数远多于窗口所需时，重写文件只留窗口内的条目。

    🔴 只在**文件明显变大**时才解析 + 重写：
      · 先 `stat()` 挡一道（见 `COMPACT_MIN_BYTES`）—— 避免每次记账都全量
        parse 一遍，那是热路径；
      · 再看"过期条目是否过半"—— 大部分还在窗口内时压缩没有收益，
        白重写一遍只是放大"写到一半被杀"的窗口（同 `quota._maybe_compact`）。
    """
    p = state_path()
    try:
        if p.stat().st_size < COMPACT_MIN_BYTES:
            return
    except OSError:
        return
    entries = _read_entries()
    # 保留"最长那道窗口"内的条目 —— 短的窗口下次 `events()` 会自己过滤掉。
    # ⚠ 取 max 而不是各自的窗口：本文件是**两个维度共用**的，按短的压会把
    #   长的那个维度的事件也删掉（闸门变松，且不报错）。
    keep_s = max(float(config.REGISTER_WINDOW_S), float(config.DOMAIN_WINDOW_S))
    cutoff = time.time() - keep_s
    keep = [e for e in entries if _as_ts(e.get("ts")) > cutoff]
    if len(keep) * 2 > len(entries):
        return  # 过期的不够多，压缩收益抵不上重写的代价
    text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep)
    try:
        fsutil.atomic_write_text(state_path(), text)
    except OSError as ex:
        print(f"  ⚠ 窗口状态压缩失败（{type(ex).__name__}: {ex}）—— 保持原文件")
