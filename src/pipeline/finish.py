"""消费者段：注册 → 密码登录 → 建 Key → 只读校验（+ 末尾并行校验 Key）。

为什么"密码登录"这一步不能省
----------------------------
注册响应里**已经带 `access_token`**（实测），但那条 token 可能是"刚建号时
发的"，与密码无关。所以本段会**再走一次 `/api/user/login`** —— 那一步才是
"这个账号真能用密码登录"的证明。

⚠ `verify_keys` 是**非致命**的：网关侧失败只写进 `stages["verify"]`，
  不改账号状态。目的是避免"看着成功、其实不能用"的假绿账号。
"""

import time
from concurrent.futures import ThreadPoolExecutor

from common import config

from .. import apikey as _apikey
from ..agnes import (
    ERR_QUOTA,
    ERR_QUOTA_GUARD,
    ERR_REJECTED,
    AgnesClient,
    AgnesError,
    AgnesQuota,
    AgnesRejected,
    access_token_of,
)
from .limiter import (
    WindowBusy,
    _is_domain_limit,
    _register_backoff_s,
    _rotate_slot,
    domain_limiter,
    register_limiter,
)
from .record import AccountRecord, Ticket, _as_float, _as_int, _print, classify


# ── 消费者：注册 → 登录 → 建 Key → 只读校验 ───────────────────────
def finish(ticket: Ticket, *, client_factory=None, slot_pool=None, log=_print) -> AccountRecord:
    """跑完消费者那一段。**就地改 `rec` 并返回它**（失败也返回，不抛）。"""
    rec = ticket.rec
    client_factory = client_factory or (lambda proxy=None: AgnesClient(proxy=proxy))

    # ① 注册。用生产者拿到的验证码。
    t0 = time.time()
    rec.enter("register")
    waited = 0.0
    rotated_from: list[str] = []
    body = None
    for attempt in range(config.REGISTER_RETRIES + 1):
        # 注册同样消耗**两个维度**的窗口 —— 与发码同一套闸门。
        ip_wait = register_limiter(ticket.proxy).wait_time()
        # 🔴 域名维度：第一枪用的是 `provision` **已经替我们占好**的那一格预留
        #    —— 所以这时"窗口满"是正常的（满的就是我们自己的预留），不能拿它
        #    当"等不起"。只有**重试**才需要新占格。
        #    这条区分正是修"码烧了、注册进不去"的关键：不认预留，就会把已经
        #    属于自己的名额当成别人的，白白把账号判死。
        reserved = ticket.reservation.held > 0
        dom_wait = 0.0 if reserved else domain_limiter(ticket.domain).wait_time()
        if ip_wait > config.LIMITER_MAX_WAIT_S:
            old = ticket.proxy
            if _rotate_slot(slot_pool, ticket, error="local ip window full"):
                rotated_from.append(old or "(直连)")
                ticket.client = client_factory(ticket.proxy)
                continue
        # 🔴 域名维度等不起时**只能退避** —— 邮箱已经定了，换不了域名
        #    （换出口对域名窗口没用）。退避封顶，避免把批量拖死。
        need = max(ip_wait, dom_wait)
        if need > 0:
            time.sleep(min(need, _register_backoff_s()))
        # 与发码同一套封顶（理由见 provision）：窗口可能被并发打满，
        # 不能抱着 3600s 的域名窗口睡死。
        try:
            waited += register_limiter(ticket.proxy).acquire(config.LIMITER_MAX_WAIT_S)
            if reserved:
                # 用掉 provision 替我们占的那一格（预留变成"已用"）。
                ticket.reservation.consume()
            else:
                domain_limiter(ticket.domain).acquire(config.LIMITER_MAX_WAIT_S)
        except WindowBusy as ex:
            # 邮箱和验证码**已经发出去了**（不是"一个请求都没发"）⇒ 这是
            # failed 而不是 skipped。重试未用完就退避再来一次；用完就诚实
            # 标 `quota_guard` 失败 —— 试过了，本地闸门等不起。
            if attempt >= config.REGISTER_RETRIES:
                rec.fail(
                    ERR_QUOTA_GUARD,
                    f"注册闸门等不起（已试 {attempt + 1} 次）：{ex}",
                    stage="register",
                )
                rec.timings["register_ms"] = int((time.time() - t0) * 1000)
                return rec
            time.sleep(_register_backoff_s())
            continue
        try:
            body = ticket.client.register(rec.email, rec.password, ticket.code)
            break
        except AgnesQuota as ex:
            # 与发验证码同一条判据：限流自愈，先换**对的那个维度**再退避。
            if attempt >= config.REGISTER_RETRIES:
                rec.fail(
                    ERR_QUOTA,
                    f"注册持续被限流（已试 {attempt + 1} 次，换出口 {len(rotated_from)} 次）：{ex}",
                    stage="register",
                )
                rec.timings["register_ms"] = int((time.time() - t0) * 1000)
                return rec
            if _is_domain_limit(ex):
                # 域名维度：邮箱已定，换出口没用 → 退避重试
                time.sleep(_register_backoff_s())
                continue
            old = ticket.proxy
            if _rotate_slot(slot_pool, ticket, error=str(ex)):
                rotated_from.append(old or "(直连)")
                ticket.client = client_factory(ticket.proxy)
                continue
            time.sleep(_register_backoff_s())
        except AgnesRejected as ex:
            rec.fail(ERR_REJECTED, f"注册被拒：{ex}", stage="register")
            rec.timings["register_ms"] = int((time.time() - t0) * 1000)
            return rec
        except AgnesError as ex:
            rec.fail(classify(ex), f"注册失败：{ex}", stage="register")
            rec.timings["register_ms"] = int((time.time() - t0) * 1000)
            return rec
    else:
        # 🔴 循环**没有 `break`** 就结束了 ⇒ **注册请求一次都没发出去**：
        #    每一次尝试都被上面的「IP 窗口等不起 → 换出口」给 `continue` 掉了。
        #    那条路径**没有** `attempt >= REGISTER_RETRIES` 的兜底，所以它能
        #    安静地把所有尝试吃完 —— 与 `provision` 里那个是同一类缺陷。
        #
        #    🔴 这里**绝不能**往下走：下面会拿 `body=None` 算出空的 `user`，
        #    然后 `rec.mark_ok("register", "uid=")` —— 把**从没注册过**的账号
        #    标成"注册成功"，再拿它去登录，最后以
        #    `登录失败：HTTP 401 Invalid username or password` 收场。
        #    实测踩到 2 次：`stages['register'] == 'ok(uid=)'`（uid 是空的），
        #    而报告把原因指向**密码**，与真相（注册根本没发出去）恰好相反。
        rec.fail(
            ERR_QUOTA_GUARD,
            f"注册请求一次都没发出去（{config.REGISTER_RETRIES + 1} 次尝试"
            f"全被「IP 窗口等不起 → 换出口」消耗）",
            stage="register",
        )
        rec.timings["register_ms"] = int((time.time() - t0) * 1000)
        return rec
    rec.timings["register_ms"] = int((time.time() - t0) * 1000)
    if waited:
        rec.timings["register_wait_ms"] = int(waited * 1000)
    if rotated_from:
        rec.proxy_rotated = " → ".join([*rotated_from, ticket.proxy or "(直连)"])
    rec.proxy = ticket.proxy or rec.proxy
    user = (
        (body or {}).get("data", {}).get("user")
        if isinstance((body or {}).get("data"), dict)
        else None
    )
    # 🔴 注册响应里必须有 `data.user.id` —— **HTTP 200 也可能是空壳**。
    #    缺了就说明账号没真建出来；继续标 ok 会把"没注册"伪装成"密码错"。
    if not isinstance(user, dict) or not user.get("id"):
        rec.fail(
            ERR_REJECTED,
            f"注册响应缺少 data.user.id（body={str(body)[:120]}）",
            stage="register",
        )
        return rec
    rec.user_id = str(user.get("id") or "")
    rec.user_uuid = str(user.get("uuid") or "")
    rec.display_id = str(user.get("display_id") or "")
    rec.mark_ok("register", f"uid={rec.user_id}")

    # ② 密码登录 —— **换一条全新的 JWT**。这一步才是"账号真能用"的证明：
    #    注册返回的那条 token 是"刚建号时发的"，与密码无关。
    t1 = time.time()
    rec.enter("login")
    try:
        login = ticket.client.login(rec.email, rec.password)
        jwt = access_token_of(login)
        if not jwt:
            raise AgnesRejected("登录响应里没有 access_token", code="NO_TOKEN")
        rec.jwt = jwt
        luser = login.get("data", {}).get("user") if isinstance(login.get("data"), dict) else None
        if isinstance(luser, dict):
            rec.user_id = str(luser.get("id") or rec.user_id)
            rec.user_uuid = str(luser.get("uuid") or rec.user_uuid)
            rec.display_id = str(luser.get("display_id") or rec.display_id)
        rec.mark_ok("login", f"jwt {len(jwt)}B")
    except AgnesError as ex:
        rec.fail(classify(ex), f"登录失败：{ex}", stage="login")
        rec.timings["login_ms"] = int((time.time() - t1) * 1000)
        return rec
    rec.timings["login_ms"] = int((time.time() - t1) * 1000)

    # ③ 建 API Key。
    t2 = time.time()
    rec.enter("key")
    try:
        data = ticket.client.create_key(config.KEY_NAME)
        rec.api_key = str(data.get("key") or "")
        rec.key_id = str(data.get("id") or "")
        rec.key_preview = str(data.get("key_preview") or "")
        if not rec.api_key:
            raise AgnesRejected("建 key 响应里没有 key 字段", code="NO_KEY")
        rec.mark_ok("key", rec.key_preview or f"id={rec.key_id}")
    except AgnesError as ex:
        rec.fail(classify(ex), f"建 key 失败：{ex}", stage="key")
        rec.timings["key_ms"] = int((time.time() - t2) * 1000)
        return rec
    rec.timings["key_ms"] = int((time.time() - t2) * 1000)

    # ④ 只读校验：账号 / 余额 / 订阅 / key 配额。
    if not verify_app(rec, ticket.client):
        return rec

    rec.done()
    return rec


def verify_app(rec: AccountRecord, client) -> bool:
    """只读校验：`/api/user/self` / `balance` / `subscription` / `token` 列表。

    返回 `True` = 全部通过。

    🔴 抽成独立函数是为了**只有一处实现**：`finish()`（注册链的收尾）与
      `tools/ops/verify_accounts.py`（对已有账号做只读复核）必须检查同样的东西
      —— 否则"复核通过"与"注册成功"会变成两个不同的标准，而台账里那两行
      看不出区别。

    ⚠ **不调用 `rec.done()`**：那一步是调用方的。失败时已把 `rec` 打上标
      （`error_kind` + `stages.app = failed`）。
    """
    t0 = time.time()
    rec.enter("app")
    try:
        me = client.user_self()
        if not me.get("email"):
            rec.fail(ERR_REJECTED, "user/self 没有返回 email（会话没建立）", stage="app")
            return False
        rec.user_id = str(me.get("id") or rec.user_id)
        rec.user_uuid = str(me.get("uuid") or rec.user_uuid)
        rec.display_id = str(me.get("display_id") or rec.display_id)
        if not me.get("is_active", True):
            # 注册成功但**账号未激活**是"看着成功、其实不能用"，必须显式标出来。
            rec.fail(ERR_REJECTED, "user/self 显示 is_active=false（账号未激活）", stage="app")
            return False
        bal = client.balance()
        rec.balance_minor = _as_float(bal.get("available_total_minor"))
        rec.balance_currency = str(bal.get("currency") or "")
        sub = client.subscription()
        rec.plan = str((sub or {}).get("plan") or (sub or {}).get("name") or "free")
        keys = client.list_keys()
        quota = keys.get("default_quota") if isinstance(keys.get("default_quota"), dict) else {}
        rec.key_max = _as_int(quota.get("max_keys"))
    except AgnesError as ex:
        rec.fail(classify(ex), f"校验失败：{ex}", stage="app")
        return False
    finally:
        rec.timings["app_ms"] = int((time.time() - t0) * 1000)
    rec.mark_ok(
        "app", f"plan={rec.plan}, keys<={rec.key_max}" if rec.key_max else f"plan={rec.plan}"
    )
    return True


# ── 末尾统一校验 Key（非致命）────────────────────────────────────
def verify_keys(records: list, *, verbose: bool = True, log=_print) -> None:
    """统一校验已建出的 API Key 能否调用推理网关。

    为什么单独放最后，而不是塞在 `finish` 的关键路径里：
      · 批量场景下把这一步并行化，消费者池只做"注册 → 登录 → 建 key"这些
        必须串行在**同一出口**上的事；
      · 校验是**非致命**的 —— 失败只写进 `stages["verify"]`，不影响账号状态。

    ⚠ Agnes 侧实测新建 key **立刻**可用（传播延迟约 0），所以这里几乎瞬时。
      若哪天平台加了传播延迟，`apikey.verify` 自带 `attempts` / `delay` 重试。
    """
    targets = [r for r in records if getattr(r, "api_key", "") and r.status == "success"]
    if not targets:
        return

    def one(rec: AccountRecord) -> tuple:
        t0 = time.time()
        try:
            res = _apikey.verify(rec.api_key, attempts=3, delay=2.0)
            if res["ok"]:
                rec.models_count = len(res["models"])
                return rec, str(res["note"])
            return rec, str(res["note"])
        except Exception as ex:  # noqa: BLE001
            return rec, f"failed: {str(ex)[:100]}"
        finally:
            # 🔴 计时必须在**这里**（每个账号自己那一段），不能在 `run_batch`
            #    里围着整个 `verify_keys` 取一次 `t0` 再发给所有记录 —— 那样
            #    每条拿到的是**同一个值** = 整个校验阶段的时长，而它是并行跑的。
            #    实测后果：5 个账号的 `verify_ms` 全是 `70414`，报告把 70s
            #    记到**每个**账号头上，把"单 key 全链路速率"整体带偏。
            rec.timings["verify_ms"] = int((time.time() - t0) * 1000)

    with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as ex:
        for f in [ex.submit(one, r) for r in targets]:
            rec, verdict = f.result()
            rec.stages["verify"] = verdict
            if verbose:
                log(f"key verify {rec.email}: {verdict}")
