"""生产者段：建邮箱 → 发验证码 → 等码。

⚠ 与 `finish` 的**关键差异**：这一段要**为整条账号预留两枪的域名额度**
  （发码 + 注册）。理由是服务端的域名维度在注册时**换不动**（域名已写死在
  邮箱地址里），所以"只留 1 格就发码"= 码烧了、注册进不去。

🔴 这里**不捕获异常**，是有意的：异常类型带着 `error_kind` 的分类信息，
  在这一层吞掉就得在这一层重新分类（= 第二份分类实现）。让调用方统一用
  `classify()` 处理。
"""

import time

from common import config

from ..agnes import AgnesClient, AgnesError, AgnesQuota, AgnesRejected
from ..tempmail import now_ms
from .limiter import (
    WindowBusy,
    _is_domain_limit,
    _pick_domain_with_room,
    _register_backoff_s,
    _rotate_slot,
    domain_limiter,
    pick_domain,
    register_limiter,
)
from .record import Ticket, _print, make_password


# ── 生产者：建邮箱 → 发验证码 → 等码 ──────────────────────────────
def provision(
    ticket: Ticket,
    *,
    mail,
    mail_domain: str | None = None,
    slot_pool=None,
    client_factory=None,
    log=_print,
) -> Ticket:
    """跑完生产者那一段。成功返回 `ticket`；失败**抛异常**（由调用方打标）。

    ⚠ 这里**不捕获异常**，是有意的：异常类型带着 `error_kind` 的分类信息，
      在这一层吞掉就得在这一层重新分类（= 第二份分类实现）。让调用方统一
      用 `classify()` 处理。
    """
    rec = ticket.rec
    client_factory = client_factory or (lambda proxy=None: AgnesClient(proxy=proxy))
    t0 = time.time()

    # 密码在**生产者**这一侧就生成好，而不是等消费者那一步再生成 ——
    # `rec.password` 是台账里的一等字段，而"邮箱建出来了但密码是空的"
    # 是最坏的一种中间态（账号已存在、却无法用密码登录）。
    rec.password = make_password()

    # 🔴 域名池：服务端有一道**域名维度**的限流，所以这里要能换域名。
    #    `--mail-domain` 给定时**锁定**那一个域名（不再轮换）——
    #    那是有意的：它用于"我就想测这个域名"的排查场景。
    domains = [mail_domain] if mail_domain else config.mail_domains()
    if not domains:
        raise AgnesRejected(
            "没有可用的收信域名（AR_MAIL_DOMAINS / AR_WORKER_DOMAIN 都为空）", code="NO_DOMAIN"
        )
    domain = pick_domain(domains)
    # 预留的**唯一所有者**。`provision` / `finish` / `run_batch` 都只通过它
    # 操作格子，不再各自改 `Ticket` 上的裸字段（那套写法出过 2 个 bug）。
    reservation = ticket.reservation
    rotated_domains: list[str] = []
    rotated_slots: list[str] = []
    waited = 0.0
    sent_at = 0
    t1 = time.time()
    for attempt in range(config.REGISTER_RETRIES + 1):
        # ── IP 维度（按出口）：只要 1 格 ───────────────────────────
        # 🔴 这里**不需要**一次占两格：IP 维度在注册时**换得动**（10 个出口），
        #    所以 `finish` 撞满时可以换出口。换不动的是**域名**维度 —— 见下。
        ip_wait = register_limiter(ticket.proxy).wait_time()
        if ip_wait > config.LIMITER_MAX_WAIT_S:
            old = ticket.proxy
            if _rotate_slot(slot_pool, ticket, error="local ip window full"):
                rotated_slots.append(old or "(直连)")
                ticket.client = client_factory(ticket.proxy)
                log(f"本地 IP 窗口要等 {ip_wait:.0f}s → 换出口")
                continue

        # ── 域名维度：**必须给这条账号占够 REQUESTS_PER_ACCOUNT 格** ─
        # 🔴 一个账号要打两枪注册类请求，而两枪**共用**同一张域名计数
        #    （docs/protocol.md §3）；注册那一枪的域名已经定死在邮箱里，
        #    **换不了**。所以"只留 1 格就发码"= 码烧了、注册进不去。
        dom = domain_limiter(domain)
        # 当前域名上还差几格：本次发码 1 格 + 将来注册 1 格。
        # 🔴 用"**补足**到 REQUESTS_PER_ACCOUNT"而不是"每次重试都再占 2 格"：
        #    发码被服务端限流后重试会把预留吃掉一格，这时只补 1 格就够 ——
        #    而无脑再占 2 格会把注册那一格也挤掉，等于白重试（码烧了还是进不去）。
        #    （这条不变量现在由 `DomainReservation.reserve()` 自己保证。）
        gap = reservation.missing_for(domain)
        if gap > 0:
            if dom.room() < gap:
                # 装不下 ⇒ **一枪都不发**。先试换一个有位置的域名；
                # 换不动（都满 / 没别的域名）就交给调用方，**不烧码**。
                nxt = _pick_domain_with_room(domains, exclude=domain, need=gap)
                if nxt:
                    rotated_domains.append(domain)
                    domain = nxt
                    log(f"域名窗口装不下 {gap} 枪 → 换域名 {domain}")
                    continue
                raise WindowBusy(
                    f"域名窗口装不下 {gap} 枪，也没有别的域名可换（{domain}）"
                    f"—— 不发验证码，避免烧码",
                    wait_s=dom.wait_time(),
                )
            gap_wait = max(ip_wait, dom.wait_time())
            if gap_wait > 0:
                time.sleep(min(gap_wait, _register_backoff_s()))
            try:
                # ⚠ 换域名时 `reserve()` 会**自己**把旧域名上没用上的还回去 ——
                #   这里不需要（也不应该）再手写一次归还：那正是以前出 bug 的地方。
                reservation.reserve(domain, dom, config.LIMITER_MAX_WAIT_S)
            except WindowBusy:
                # 预检与 acquire 之间被别的线程/进程抢光了 —— 重试，**不烧码**。
                if attempt >= config.REGISTER_RETRIES:
                    raise
                time.sleep(_register_backoff_s())
                continue

        # 🔴 封顶传 `LIMITER_MAX_WAIT_S`：上面的预检与这里的 acquire 之间，
        #    别的线程可能刚好把窗口打满。没有封顶的话这一次 acquire 会抱着
        #    长度未知的窗口（域名维度默认 3600s）睡死在锁里 —— 整条账号链
        #    挂住。撞封顶抛 `WindowBusy`，由本循环 / 调用方按"等不起"处置。
        try:
            waited += register_limiter(ticket.proxy).acquire(config.LIMITER_MAX_WAIT_S)
        except WindowBusy:
            # 域名已经预留好了，但这一枪发不出去 ⇒ 把预留还回去再抛，
            # 否则那 2 格会被一条根本没跑的账号白占一小时。
            reservation.release()
            raise

        # ── 建邮箱（每次重试都新建：换域名就得换地址）──────────────
        rec.enter("mailbox")
        emails = mail.create_mailbox(domain, 1)
        # 🔴 必须校验**元素**，不能只查 `if not emails`。
        #    Worker 在异常路径下会返回**列表非空但元素是空串**（`[""]`）——
        #    只查列表的话空地址会一路走下去，后果比"报错点偏远"严重得多：
        #      · 收信是按**收件人**过滤的（`GET /api/inbox?email=`），空收件人
        #        等于**不设限** ⇒ 可能抽到**别人邮件的验证码**，把别人的地址
        #        当成自己的账号注册掉；
        #      · 日志表现为"邮箱+验证码就绪"但邮箱位是空的（实测见过）。
        #    ⚠ 不能写成 `str(emails[0]).strip()`：`None` 会变成字符串 `"None"`
        #      —— 一个**看着合法**的假地址，比空串更难查。
        first = emails[0] if emails else None
        address = first.strip() if isinstance(first, str) else ""
        if not address:
            raise AgnesRejected(
                f"邮箱 Worker 没返回有效地址（create_mailbox 返回 {emails!r}）",
                code="NO_MAILBOX",
            )
        rec.email = address
        rec.mail_domain = domain
        if not rec.timings.get("mailbox_ms"):
            rec.timings["mailbox_ms"] = int((time.time() - t0) * 1000)
        rec.mark_ok("mailbox")

        # ── 发验证码（写请求，两个维度的窗口都消耗）───────────────
        rec.enter("code")
        sent_at = now_ms()
        # 🔴 这一枪**即将发出** ⇒ 先把预留扣掉，而且必须扣在**调用之前**。
        #
        #    "成功后扣"是不够的：网络层失败（`RemoteDisconnected` / `SSLEOFError`）
        #    的请求**可能已经落地**，服务端可能已经记了这一枪。那种失败下不扣，
        #    就等于把这一格也还回去 ⇒ 本地闸门比服务端**松** —— 而"宁可少打"
        #    是本项目对本地闸门的一贯要求（见 config 里域名维度的注释）。
        #
        #    ⚠ 实测踩到：一次跑批 3 条 `send_code` 撞网络错误，域名窗口**各扣 0 格**
        #      （预留 2 格被全数还回）。
        reservation.consume()
        try:
            ticket.client.send_code(rec.email)
        except AgnesQuota as ex:
            if attempt >= config.REGISTER_RETRIES:
                raise
            # 🔴 **先看是哪个维度**：域名维度换出口没用，IP 维度换域名没用。
            #    换错维度 = 白烧一次尝试（而尝试本身也消耗窗口）。
            if _is_domain_limit(ex):
                nxt = pick_domain(domains, exclude=domain)
                if nxt and nxt != domain:
                    # 换域名 ⇒ 旧域名上剩下的预留由**下一次 `reserve()`** 自动归还
                    # （它在域名变化时会先 `release()`）。这里不用手写归还。
                    rotated_domains.append(domain)
                    domain = nxt
                    log(f"发码被**域名**限流 → 换域名 {domain}（{ex}）")
                    continue
            else:
                old = ticket.proxy
                if _rotate_slot(slot_pool, ticket, error=str(ex)):
                    rotated_slots.append(old or "(直连)")
                    ticket.client = client_factory(ticket.proxy)
                    log(f"发码被限流 → 换出口（{ex}）")
                    continue
            back = _register_backoff_s()
            log(f"发码被限流，退避 {back:.0f}s 后重试（{ex}）")
            time.sleep(back)
            continue
        # 发码成功：那一格已在**调用前**扣掉，剩下的留给 `finish` 的首次注册。
        break
    else:
        # 🔴 循环**没有 `break`** 就结束了 ⇒ **一次验证码都没发出去**：
        #    每一次尝试都被上面的"本地闸门等不起 → 换维度"给 `continue` 掉了
        #    （IP 窗口与域名窗口都满，换不动）。
        #
        #    🔴 这里**绝不能**落到下面的 `wait_for_code`：那会抱着
        #    `MAIL_POLL_TIMEOUT`（默认 120s）等一封**从来没被请求过**的信，
        #    最后报 `等码超时`（`rejected`）—— 而真相是"一个注册请求都没发"。
        #    实测踩到：10 批里 5 次这样的**假 `rejected`**，把排查引向
        #    "Worker 没收到信 / 域名白名单"，与真实原因（本地闸门）恰好相反。
        #    项目对这两种状态的区分是硬要求：`skipped` = 没试过，
        #    `failed` = 试过了不行。
        #
        #    抛 `WindowBusy` ⇒ `classify` → `quota_guard` ⇒ 生产者按
        #    **`skipped`** 记账（且**不**计入配额尝试）—— 与语义一致。
        reservation.release()
        raise WindowBusy(
            f"本地闸门把 {config.REGISTER_RETRIES + 1} 次尝试全部换掉了，"
            f"一个验证码请求都没发出去（IP / 域名窗口都满）"
        )
    rec.timings["code_send_ms"] = int((time.time() - t1) * 1000)
    if waited:
        rec.timings["code_wait_send_ms"] = int(waited * 1000)

    # 等信。**等码是整链最大的单项耗时**（SMTP + 轮询）。
    t2 = time.time()
    code = mail.wait_for_code(rec.email, since_ts=sent_at)
    rec.timings["code_wait_ms"] = int((time.time() - t2) * 1000)
    rec.timings["code_polls"] = int(getattr(mail, "last_polls", 0))
    rec.timings["code_5xx"] = int(getattr(mail, "last_http_errors", 0))
    if not code:
        # 🔴 两种失败必须分开报：`last_error` 非空 = **读不出来**（修法完全不同）。
        if getattr(mail, "last_error", ""):
            raise AgnesError(f"邮箱读不出来：{mail.last_error}", code="MAIL_READ")
        raise AgnesRejected(
            f"等 {config.MAIL_POLL_TIMEOUT}s 没等到验证码邮件"
            f"（打了 {getattr(mail, 'last_polls', 0)} 次收信接口）",
            code="MAIL_TIMEOUT",
        )
    ticket.code = code
    ticket.domain = domain
    rec.proxy = ticket.proxy or rec.proxy
    rec.timings["code_ms"] = int((time.time() - t1) * 1000)
    rec.mark_ok("code", f"{getattr(mail, 'last_polls', 0)} polls")
    if rotated_slots:
        rec.proxy_rotated = " → ".join([*rotated_slots, ticket.proxy or "(直连)"])
    if rotated_domains:
        rec.mail_domain_rotated = " → ".join([*rotated_domains, domain])
    return ticket
