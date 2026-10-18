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

from ..agnes import AgnesCaptcha, AgnesClient, AgnesError, AgnesQuota, AgnesRejected
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


def _create_mailbox(mail, domain: str, rec, t0: float) -> str:
    """建一个收信地址并做**元素级**校验。返回地址（非法则空串）。

    🔴 必须校验**元素**，不能只查 `if not emails`：Worker 在异常路径下会返回
      **列表非空但元素是空串**（`[""]`）。空收件人等于**不设限**
      （`GET /api/inbox?email=`），可能抽到**别人邮件的验证码**。
    ⚠ 不能写成 `str(emails[0]).strip()`：`None` 会变成字符串 `"None"`
      —— 一个**看着合法**的假地址，比空串更难查。
    """
    emails = mail.create_mailbox(domain, 1)
    first = emails[0] if emails else None
    address = first.strip() if isinstance(first, str) else ""
    if address and not rec.timings.get("mailbox_ms"):
        rec.timings["mailbox_ms"] = int((time.time() - t0) * 1000)
    return address


# ── 生产者：建邮箱 → 发验证码 → 等码 ──────────────────────────────
def provision(
    ticket: Ticket,
    *,
    mail_for,
    mail_domain: str | None = None,
    slot_pool=None,
    client_factory=None,
    captcha=None,
    log=_print,
) -> Ticket:
    """跑完生产者那一段。成功返回 `ticket`；失败**抛异常**（由调用方打标）。

    ⚠ 这里**不捕获异常**，是有意的：异常类型带着 `error_kind` 的分类信息，
      在这一层吞掉就得在这一层重新分类（= 第二份分类实现）。让调用方统一
      用 `classify()` 处理。

    🔴 `mail_for(domain)` 是**按域名取邮箱客户端**的可调用对象，而不是一个
      现成的客户端：域名来自**多个源**（自有 CF Worker / catchmail），而两者
      的协议完全不同（一个要 `X-Admin-Token`、一个要 `Referer`/`Origin`）。
      所以客户端必须**跟着域名走** —— 换域名时重新取一个，否则会拿 Worker
      的客户端去 catchmail 的域名上建地址。
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
    # 真正**发出去了几枪**验证码请求。`for…else` 的收尾靠它区分
    # "没试过"（`skipped`）与"试过了不行"（`failed`）—— 见 else 分支。
    sent_attempts = 0
    # captcha 阶段已换过几次出口。
    # 🔴 单独计数并**只允许换一次**（不是“用光 REGISTER_RETRIES”）：
    #    换出口能治的是“链路坏 / 图没到”，治不了“字形认错”（那种换几个出口
    #    都一样错）。每个出口 45s，最多 2 个出口就止损。
    cap_rotations = 0
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
        # 🔴 客户端**跟着域名取**：域名可能刚从另一个源换过来。
        mail = mail_for(domain)
        rec.enter("mailbox")
        # 🔴 **重试时复用已有邮箱**（B1，实测浪费）：`continue` 会回到循环头，
        #    而邮箱地址在**同一个域名**下是仍然有效的 —— 但老代码每次都
        #    `create_mailbox`，于是 captcha 失败重解 / 限流重试都会**多建一个地址**：
        #      · 浪费 smailr 的 100 个邮箱额度（它会真删，但建也占额）；
        #      · 往台账 `mailboxes_created` 里塞一堆没用过的地址。
        #    只有**换域名**才必须重建（域名是地址的一部分）。
        reuse = bool(rec.email) and rec.mail_domain == domain
        if reuse:
            address = rec.email
            rec.mark_ok("mailbox", "reused")
        else:
            address = _create_mailbox(mail, domain, rec, t0)
            if not address:
                raise AgnesRejected(
                    f"邮箱源没返回有效地址（{config.source_of(domain)} 的 create_mailbox "
                    f"返回了空地址）",
                    code="NO_MAILBOX",
                )
            rec.email = address
            rec.mail_domain = domain
            rec.mail_reused = bool(getattr(mail, "reused", False))
            if not rec.mail_reused and address not in rec.mailboxes_created:
                rec.mailboxes_created.append(address)
            rec.mark_ok("mailbox")

        # ── 发验证码（写请求，两个维度的窗口都消耗）───────────────
        rec.enter("code")
        sent_at = now_ms()
        # ── captcha：在**写请求之前**解出 ticket/randstr ──────────
        # 🔴 放在 `reservation.consume()` **之前**：captcha 失败时这一枪
        #    根本没发出去，那一格域名/IP 预留不该被扣掉（由调用方释放）。
        # 🔴 放在循环**里面**：ticket 是一次性的，`send_code` 被限流后重试
        #    必须重新解一张，不能复用上一张。
        cap_ticket = cap_randstr = None
        if captcha is not None:
            # 🔴 **同出口重解**（`AR_CAPTCHA_RESOLVE_ATTEMPTS`，默认 2 = 重解 1 次）：
            #    实测定单次成功率 ~50%，而失败与出口**无关**（直连/代理一样，
            #    失败都是 errorCode=50）⇒ 「再拿一张新题」比「换出口等 45s 超时」划算。
            last_cap_ex: AgnesCaptcha | None = None
            for cap_try in range(config.CAPTCHA_RESOLVE_ATTEMPTS):
                t_cap = time.time()
                try:
                    cap_ticket, cap_randstr = captcha.solve(
                        rec.email,
                        # 🔴 默认**直连**（实测：快一倍，成功率一样，uip 不一致不被拒）。
                        #    要强绑同一出口就设 AR_CAPTCHA_USE_PROXY=1。
                        proxy=ticket.proxy if config.CAPTCHA_USE_PROXY else None,
                    )
                except AgnesCaptcha as ex:
                    last_cap_ex = ex
                    if cap_try + 1 < config.CAPTCHA_RESOLVE_ATTEMPTS:
                        log(f"captcha 失败（第 {cap_try + 1} 次）→ 同出口重解：{ex}")
                finally:
                    # 🔴 累计计时（多次尝试的**总** captcha 成本），不要覆盖 ——
                    #    报告里那个数就该回答“这条账号在 captcha 上花了多久”。
                    rec.timings["captcha_ms"] = rec.timings.get("captcha_ms", 0) + int(
                        (time.time() - t_cap) * 1000
                    )
                if cap_ticket is not None:
                    break
            if cap_ticket is None:
                # 重解用完了还不成 ⇒ 换出口再赌一次（可能是链路坏，不是题难）。
                old = ticket.proxy
                if cap_rotations < 1 and _rotate_slot(
                    slot_pool, ticket, error=f"captcha: {last_cap_ex}"
                ):
                    cap_rotations += 1
                    rotated_slots.append(old or "(直连)")
                    ticket.client = client_factory(ticket.proxy)
                    log(f"captcha 重解耗尽 → 换出口重试（{last_cap_ex}）")
                    continue
                raise last_cap_ex or AgnesCaptcha("captcha 未知失败")
            log(f"  captcha 就绪（{rec.timings['captcha_ms']}ms）")

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
        # 🔴 在**调用之前**记这一枪：网络层失败（`RemoteDisconnected` /
        #    `SSLEOFError`）的请求**可能已经落地** —— 那也算"发出去了"。
        sent_attempts += 1
        try:
            ticket.client.send_code(rec.email, ticket=cap_ticket, randstr=cap_randstr)
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
        # 🔴 循环**没有 `break`** 就结束了。这里其实有**两种形态**，必须分开 ——
        #    老实现把两种都当成第一种，于是把"发过枪"的账号误标成 `skipped`。
        #
        #   A. `sent_attempts == 0` —— 一次验证码都没发出去：每一次尝试都被
        #      上面的"本地闸门等不起 → 换维度"给 `continue` 掉了
        #      （IP 窗口与域名窗口都满，换不动）。
        #      ⇒ 按项目定义是 `skipped`（没试过），抛 `WindowBusy`。
        #
        #   B. `sent_attempts > 0` —— **发过枪**，只是那一枪被服务端限流拒了
        #      （`except AgnesQuota` 那条路**也会** `continue`），之后剩下的
        #      尝试才被换维度吃掉。⇒ 这是"试过了，不行"，必须是
        #      `failed`/`quota`，抛 `AgnesQuota`。
        #
        # 🔴 实测踩到（2026-10-15）：台账里有 `skipped` 记录带着
        #    `stages.mailbox == 'ok'` —— 而 `mark_ok("mailbox")` 之后**唯一**
        #    的语句就是 `send_code`。即那些账号**确实发过请求**，却被标成
        #    `skipped`，消息还写着"一个验证码请求都没发出去"（**假话**）。
        #    项目对这两种状态的区分是硬要求：`skipped` = 没试过，
        #    `failed` = 试过了不行。
        #
        # 🔴 两种都不能落到下面的 `wait_for_code`：那会抱着
        #    `MAIL_POLL_TIMEOUT`（默认 120s）等一封**从来没被请求过**的信，
        #    最后报 `等码超时`（`rejected`）—— 与真相恰好相反。
        #    （实测踩到：10 批里 5 次这样的**假 `rejected`**。）
        reservation.release()
        # 🔴 消息里**带上两个维度的剩余等待时间**：让人能判断"该等 5 分钟还是
        #    该换池子"。⚠ 它们报的是**当前**（换过之后）的窗口 —— 所以 0s
        #    不代表"从来没满过"，别把 0s 读成"窗口是空的却跳过了"。
        ip_wait_now = register_limiter(ticket.proxy).wait_time()
        dom_wait_now = domain_limiter(domain).wait_time()
        if sent_attempts:
            raise AgnesQuota(
                f"已发出 {sent_attempts} 次验证码请求但都被服务端限流"
                f"（Too many registration attempts），"
                f"本地闸门轮换 {config.REGISTER_RETRIES + 1} 次后仍拿不到名额"
                f"（出口窗口还要等 {ip_wait_now:.0f}s，域名窗口还要等 {dom_wait_now:.0f}s）"
            )
        raise WindowBusy(
            f"本地闸门把 {config.REGISTER_RETRIES + 1} 次尝试全部换掉了，"
            f"一个验证码请求都没发出去"
            f"（出口窗口还要等 {ip_wait_now:.0f}s，域名窗口还要等 {dom_wait_now:.0f}s）"
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
