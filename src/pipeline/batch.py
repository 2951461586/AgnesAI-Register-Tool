"""编排：生产者池 ‖ 消费者池 + 本地配额闸门 + 收尾统计。

    ┌─ 生产者池（producers 路）──────────────┐   ┌─ 消费者池（workers 路）──┐
    │ 建邮箱 → 发验证码 → 等码                │──▶│ 注册 → 登录 → 建 Key → 校验 │
    └────────────────────────────────────────┘队列 └─────────────────────────┘
                                               末尾：并行校验所有 Key

🔴 并发结构里最容易写错的两处（都实测踩过）：
  · **生产者并发必须真的受 `--producers` 约束** —— 手写 `count` 个线程再逐个
    `start()` 的话那个参数只是装饰，而"参数没生效"不会报错，只会让限流变成
    一句空话。所以用 `ThreadPoolExecutor(max_workers=...)`。
  · **哨兵数量必须等于消费者数量** —— 少一个就有线程永远 `get()` 阻塞，
    而现象是"所有账号都跑完了，然后整个进程挂住"。

🔴 `record_out()` 是**所有**出口的唯一落账点（成功/失败/跳过），配额计数也在
  那里 —— 放在消费者里的话，生产者阶段就失败的账号根本不会落账，
  "打了几枪"这个数会永远偏小。
"""

import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from common import config

from .. import mailbox as _mailbox
from .. import quota as _quota
from ..agnes import (
    ERR_NETWORK,
    ERR_QUOTA,
    ERR_QUOTA_GUARD,
    AgnesClient,
)
from ..proxypool import SlotPool, SlotPoolEmpty
from .finish import finish, verify_keys
from .limiter import WindowBusy, _slot_has_room
from .provision import provision
from .record import STAGES, AccountRecord, Ticket, _print, classify

# 连续多少次"服务端注册限流"就停整批。
# 🔴 取 2 而不是 1：单次限流完全可能是抖动。但连续两次就值得停 ——
#    撞上限流墙时继续打只是白烧请求，而且**加深**服务端对这张 IP 的印象。
QUOTA_STREAK_STOP = 2


# ── 编排 ──────────────────────────────────────────────────────────
def run_batch(
    *,
    count: int,
    workers: int = 4,
    producers: int = 4,
    mail_domain: str | None = None,
    verbose: bool = True,
    ignore_quota: bool = False,
    mail_factory=None,
    client_factory=None,
    slot_pool=None,
    log=_print,
) -> list[AccountRecord]:
    """跑 `count` 个账号。返回**本批**记录（含失败 / 跳过，顺序=完成顺序）。

    并发结构：

        ┌─ 生产者池（producers 路）──────────────┐   ┌─ 消费者池（workers 路）──┐
        │ 建邮箱 → 发验证码 → 等码                │──▶│ 注册 → 登录 → 建 Key → 校验 │
        └────────────────────────────────────────┘ 队列 └─────────────────────────┘
                                                   末尾：并行校验所有 Key

    `mail_factory` / `client_factory` 是**注入点**：默认造真的客户端，
    测试传假的 —— 这样编排、打标、配额逻辑都能**离线**测（零网络请求）。
    """
    mail_factory = mail_factory or (lambda domain=None: _mailbox.for_domain(domain))
    client_factory = client_factory or (lambda proxy=None: AgnesClient(proxy=proxy))
    # 槽位池：**由调用方拥有**（`run.py` 建它、传进来、跑完读它的统计）。
    # 不在这里建的原因：返回值是 `list[AccountRecord]`，没法把统计挂在上面
    # （`list` 不能设属性），而用模块全局会在同进程跑多批时被后一批覆盖。
    if slot_pool is None:
        slot_pool = SlotPool(config.proxy_slots(), timeout=config.PROXY_SLOT_TIMEOUT)

    q: queue.Queue = queue.Queue(maxsize=max(2, producers))
    done: list[AccountRecord] = []
    lock = threading.Lock()
    stop = threading.Event()
    streak = {"n": 0}
    sentinel = object()

    def say(msg: str) -> None:
        if verbose:
            log(msg)

    def record_out(rec: AccountRecord) -> None:
        """一条记录完工 —— **所有出口都走这里**（成功 / 失败 / 跳过）。

        🔴 配额计数也在这里记，而不是在消费者里 —— 放在消费者里的话，
          **生产者阶段**就失败的账号（等不到验证码 / 服务端拒绝）根本不会落账，
          于是"打了几枪"这个数永远偏小。

        🔴 `skipped` **不记账**：那是本地闸门拦下的（一个请求都没发），
          把它算进"失败尝试"会把分母弄脏。
        """
        with lock:
            done.append(rec)
        if rec.status != "skipped":
            _quota.record(rec.email, ok=(rec.status == "success"), note=rec.error_kind)

    def producer(idx: int) -> None:
        rec = AccountRecord()
        rec.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # ① 本地闸门。**在发任何请求之前** —— 这就是 `quota_guard` 的判据。
        #
        # 🔴 这里先把"该不该跑"判成一个字符串，**出了锁**再记账：
        #    `record_out()` 自己也要拿 `lock`，而 `threading.Lock` **不可重入**
        #    ⇒ 在 `with lock:` 里调它就是**自锁**（整个批次永久挂死）。
        reason = None
        with lock:
            if stop.is_set():
                reason = "本批已因连续注册限流而停止（未发请求）"
            else:
                try:
                    _quota.check(ignore=ignore_quota)
                except _quota.QuotaExceeded as ex:
                    reason = str(ex)
        if reason is not None:
            rec.skip(ERR_QUOTA_GUARD, reason)
            record_out(rec)
            return
        # ② 借一个出口。**在闸门之后、第一个请求之前** —— 被本地闸门拦下的
        #    任务不该占着出口，也不该把出口的使用次数记上一笔。
        try:
            # 🔴 带 `prefer=_slot_has_room`：池子的"最少使用"是**进程内**计数，
            #    新进程里全是 0 ⇒ 不传它就会按列表顺序借到上一批刚打满的出口。
            slot = slot_pool.acquire(prefer=_slot_has_room)
        except SlotPoolEmpty as ex:
            rec.fail(ERR_NETWORK, f"代理槽位池：{ex}", stage="mailbox")
            record_out(rec)
            return
        rec.proxy = slot or config.AR_PROXY
        client = client_factory(slot)
        ticket = Ticket(rec=rec, client=client, proxy=slot)
        try:
            provision(
                ticket,
                # 🔴 传的是**工厂**而不是现成客户端：域名可能换到**另一个源**
                #   （自有域名 ↔ catchmail），而两者的协议不同。
                mail_for=mail_factory,
                mail_domain=mail_domain,
                slot_pool=slot_pool,
                client_factory=client_factory,
            )
        except (_quota.QuotaExceeded, WindowBusy) as ex:  # 注入的假客户端也可能抛
            # 本地守卫主动中止（爆炸半径上限 / 滑动窗口等不起）⇒ **一个请求都
            # 没发** ⇒ skipped，不是 failed。两者对读者的含义完全不同：
            # skipped = "本来就没试"，failed = "试过了，不行"。
            # ⚠ 归还域名预留（幂等）—— 不还就把域名锁死了。
            ticket.reservation.release()
            rec.skip(ERR_QUOTA_GUARD, str(ex))
            slot_pool.release(slot, ok=None)
            record_out(rec)
            return
        except Exception as ex:  # noqa: BLE001
            kind = classify(ex)
            stage = _failed_stage(rec)
            rec.fail(kind, f"{type(ex).__name__}: {ex}" if kind == ERR_NETWORK else str(ex), stage)
            # ⚠ 这条链已经死了（网络 / 等不到码 / 建不出邮箱）⇒ 归还域名预留。
            #    这些是**常见**路径（实测 5 账号跑批里 2 条走这里），不还就等于
            #    每失败一个账号就永久占掉半个域名。
            ticket.reservation.release()
            # 只有 `network` 才可能真的是出口的问题 —— 把"邮箱已注册"记到
            # 出口头上，会让每个出口的失败数变成噪音，报告就没人看了。
            slot_pool.release(slot, ok=(False if kind == ERR_NETWORK else None), error=rec.error)
            if kind == ERR_QUOTA:
                with lock:
                    streak["n"] += 1
                    n = streak["n"]
                    tripped = n >= QUOTA_STREAK_STOP
                    if tripped:
                        stop.set()
                if tripped:
                    log(f"  ⛔ 连续 {n} 次服务端注册限流 —— 停止本批剩余任务（不发请求）")
            else:
                with lock:
                    streak["n"] = 0
            record_out(rec)
            return
        with lock:
            streak["n"] = 0
        say(
            f"  ▸ {rec.email} 邮箱+验证码就绪"
            f"（出口 {slot or '直连'}，等码 {rec.timings.get('code_wait_ms', 0)}ms，"
            f"轮询 {ticket.mail_polls or rec.timings.get('code_polls', 0)} 次）"
        )
        # 槽位**不在这里归还** —— 消费者还要用它做注册（限流是按出口的）。
        q.put(ticket)

    def consumer() -> None:
        while True:
            item = q.get()
            try:
                if item is sentinel:
                    return
                try:
                    rec = finish(item, client_factory=client_factory, slot_pool=slot_pool, log=log)
                except Exception as ex:  # noqa: BLE001
                    # 🔴 兜底：`finish()` 抛出未预期异常时**不能**让 consumer
                    #    线程死掉 —— 线程死 = 这条记录永远不 `record_out` =
                    #    台账静默少一条。防缩水护栏只会在写盘时发现"少了"，
                    #    答不上"为什么少"。打标后走统一的收尾路径。
                    rec = item.rec
                    kind = classify(ex)
                    rec.fail(kind, f"{type(ex).__name__}: {ex}", _failed_stage(rec))
                if rec.status == "success":
                    say(f"  ✓ {rec.email} 注册成功（{_fmt_ms(rec)}）")
                else:
                    say(f"  ✗ {rec.email} 失败 [{rec.error_kind}] {rec.error[:120]}")
                # ⚠ 这条链已经跑完 ⇒ 没用上的域名预留还回去（幂等）。
                #    典型场景：注册被拒 / 等不到码 —— 预留的"注册那一格"没花掉。
                item.reservation.release()
                # 出口的成败由**整条链**的结果决定（而不是只看注册那一段）。
                slot_pool.release(
                    item.proxy,
                    ok=(
                        True
                        if rec.status == "success"
                        else (False if rec.error_kind == ERR_NETWORK else None)
                    ),
                    error=rec.error,
                )
                record_out(rec)
            finally:
                q.task_done()

    consumers = [threading.Thread(target=consumer, daemon=True) for _ in range(max(1, workers))]
    for th in consumers:
        th.start()
    # 🔴 生产者并发必须**真的**受 `--producers` 约束：手写 `count` 个线程再逐个
    #    `start()` 的话，那个参数只是个装饰 —— 而"参数没生效"不会报错，
    #    只会让限流变成一句空话。`ThreadPoolExecutor` 的 `max_workers` 才是闸门。
    with ThreadPoolExecutor(max_workers=max(1, producers)) as pool:
        list(pool.map(producer, range(count)))
    # 🔴 哨兵数量 = 消费者数量：少一个就有线程永远 `get()` 阻塞，
    #    `run_batch` 直接挂死（而且是在"全部账号都已跑完"之后才挂）。
    for _ in consumers:
        q.put(sentinel)
    for th in consumers:
        th.join()

    # 逐条计时由 `verify_keys.one()` 自己写进 `rec.timings["verify_ms"]` ——
    # 不能在这里取一次 `t0` 发给所有人（见 `one()` 里的注释）。
    verify_keys(done, verbose=verbose, log=log)
    return done


def _failed_stage(rec: AccountRecord) -> str | None:
    """失败发生在哪一步 —— **靠游标，不靠猜**。

    🔴 `AccountRecord.enter()` 在每个阶段开始时把 `stage_now` 设成那个阶段。
      不能用"第一个还是空的阶段"代替：`stages` 里的值会被提前写，
      而"第一个空阶段"会把一次**注册失败**报成**登录失败** —— 那是一个
      **错误结论**，比"不知道"坏得多。

    游标为空（比如注入的假客户端在第一个阶段之前就抛）时，退回"第一个空阶段"。
    """
    if rec.stage_now:
        return rec.stage_now
    for name in STAGES:
        if not rec.stages.get(name):
            return name
    return None


def _fmt_ms(rec: AccountRecord) -> str:
    total = sum(int(v) for v in rec.timings.values())
    parts = " ".join(f"{k[:-3]}={v}ms" for k, v in rec.timings.items() if v)
    return f"合计 {total}ms｜{parts}"
