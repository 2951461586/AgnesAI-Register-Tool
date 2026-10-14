"""流水线编排的离线用例（零网络）。

用注入的假邮箱 / 假客户端跑完整的 `run_batch`，钉住：
  · 成功链路的阶段顺序与字段落位；
  · **限流 → `quota`、明确拒绝 → `rejected`** 的分类；
  · 本地闸门拦下时是 `skipped` 而不是 `failed`（"没发请求"与"试过了"不同）；
  · `verify` 是**非致命**的（网关校验失败不影响账号 success）。

⚠ 域名维度窗口（`DOMAIN_*`）由 `conftest.py` 的 autouse fixture 统一隔离
  （清 limiter 字典 + patch 配置）—— 它是模块级全局，不隔离的话跑满两个
  用例后 `acquire()` 会抱着默认的 1 小时窗口睡死，本文件整跑必挂。
"""

import contextlib
import threading
import time
from typing import cast

import pytest

from src import pipeline as P
from src.proxypool import SlotPool


class FakeMail:
    # ⚠ `code` 必须允许 `None` —— 那对应真实契约里 `wait_for_code()` 返回
    #   `str | None`（"等不到码"是一个真实状态，不是测试造出来的边界）。
    def __init__(self, code: str | None = "123456", create=None):
        self.code = code
        self._create = create
        self.last_error = ""
        self.last_polls = 1
        self.last_http_errors = 0

    def create_mailbox(self, domain=None, count=1):
        if self._create is not None:
            return self._create
        return ["oai-test@own.example"]

    def wait_for_code(self, address, length=6, **kw):
        return self.code


class FakeClient:
    def __init__(self, *, send_error=None, register_error=None, key_error=None):
        self.send_error = send_error
        self.register_error = register_error
        self.key_error = key_error
        self.jwt = "jwt-1"
        self.calls = []

    def send_code(self, email, purpose="register"):
        self.calls.append("send_code")
        if self.send_error:
            raise self.send_error
        return {"code": 200}

    def register(self, email, password, code):
        self.calls.append("register")
        if self.register_error:
            raise self.register_error
        return {
            "code": 200,
            "data": {
                "access_token": "reg-jwt",
                "user": {"id": 11, "uuid": "u-1", "display_id": "d-1"},
            },
        }

    def login(self, email, password):
        self.calls.append("login")
        return {"code": 200, "data": {"access_token": "jwt-2", "user": {"id": 11}}}

    def create_key(self, name):
        self.calls.append("create_key")
        if self.key_error:
            raise self.key_error
        return {"key": "sk-test-abcdefghijklmnop", "id": 99, "key_preview": "sk-test…mnop"}

    def user_self(self):
        return {"email": "oai-test@own.example", "id": 11, "uuid": "u-1", "is_active": True}

    def balance(self):
        return {"available_total_minor": 0.0, "currency": "usd"}

    def subscription(self):
        return None

    def list_keys(self):
        return {"default_quota": {"max_keys": 20, "current": 1}}


def _patch_verify(monkeypatch, ok=True, note="ok(11 models)"):
    def fake(key, **kw):
        return {"ok": ok, "note": note, "models": ["m1", "m2"] if ok else [], "usage": {}}

    monkeypatch.setattr(P._apikey, "verify", fake)


def _run(monkeypatch, **kw):
    monkeypatch.setattr(P.config, "REGISTER_MAX_PER_WINDOW", 100)
    monkeypatch.setattr(P.config, "REGISTER_WINDOW_S", 1.0)
    monkeypatch.setattr(P.config, "REGISTER_RETRIES", 0)
    _patch_verify(monkeypatch)
    mail = kw.pop("mail", FakeMail())
    client = kw.pop("client", FakeClient())
    return P.run_batch(
        count=kw.pop("count", 1),
        producers=1,
        workers=1,
        verbose=False,
        mail_factory=lambda: mail,
        client_factory=lambda proxy=None: client,
        slot_pool=SlotPool([]),
        **kw,
    )


def test_success_path(monkeypatch):
    results = _run(monkeypatch)
    assert len(results) == 1
    rec = results[0]
    assert rec.status == "success"
    assert rec.email == "oai-test@own.example"
    assert rec.password
    assert rec.jwt == "jwt-2"  # 取自**登录**，不是注册
    assert rec.api_key.startswith("sk-")
    assert rec.user_id == "11"
    assert rec.plan == "free"
    assert rec.key_max == 20
    assert rec.models_count == 2
    for stage in P.STAGES:
        assert rec.stages[stage].startswith("ok"), (stage, rec.stages[stage])
    assert rec.error == "" and rec.error_kind == ""


def test_quota_at_send_code_is_quota_kind(monkeypatch):
    from src.agnes import AgnesQuota

    client = FakeClient(
        send_error=AgnesQuota("Too many registration attempts", code="RATE_LIMITED")
    )
    results = _run(monkeypatch, client=client)
    rec = results[0]
    assert rec.status == "failed"
    assert P.error_kind_of(rec) == P.ERR_QUOTA
    assert rec.stages["code"] == "failed"


def test_rejected_at_register_is_rejected_kind(monkeypatch):
    from src.agnes import AgnesRejected

    client = FakeClient(register_error=AgnesRejected("This email is already registered"))
    results = _run(monkeypatch, client=client)
    rec = results[0]
    assert rec.status == "failed"
    assert P.error_kind_of(rec) == P.ERR_REJECTED
    assert rec.stages["register"] == "failed"
    # 后面的阶段**保持空**（= 没跑到），不能被标成 failed。
    assert rec.stages["login"] == ""


def test_local_guard_skips_without_requests(monkeypatch):
    monkeypatch.setattr(P.config, "REG_QUOTA_MAX", 0)
    results = _run(monkeypatch)
    rec = results[0]
    assert rec.status == "skipped"
    assert P.error_kind_of(rec) == P.ERR_QUOTA_GUARD


def test_no_mailbox_is_rejected(monkeypatch):
    mail = FakeMail(create=[])
    results = _run(monkeypatch, mail=mail)
    rec = results[0]
    assert rec.status == "failed"
    assert P.error_kind_of(rec) == P.ERR_REJECTED
    assert rec.stages["mailbox"] == "failed"


def test_empty_mailbox_address_is_rejected(monkeypatch):
    """🔴 回归：**列表非空但元素是空串**（`[""]`）必须立刻失败。

    Worker 在异常路径下会这么返回。只查 `if not emails` 的版本会拿空地址
    一路走下去 —— 而收信是按**收件人**过滤的（`GET /api/inbox?email=`），
    空收件人等于**不设限**，可能抽到别人邮件的验证码。
    实测症状：日志里 `▸ <空> 邮箱+验证码就绪`。
    """
    for bad in ([""], ["   "], [None]):
        results = _run(monkeypatch, mail=FakeMail(create=bad))
        rec = results[0]
        assert rec.status == "failed", bad
        assert P.error_kind_of(rec) == P.ERR_REJECTED, bad
        assert rec.stages["mailbox"] == "failed", bad
        assert rec.email == "", bad


def test_all_attempts_eaten_by_local_gate_is_skipped_not_mail_timeout(monkeypatch):
    """🔴 回归：所有尝试都被“换维度”吃掉、**一次码都没发出去**时，
    绝不能落到 `wait_for_code`。

    实测踩到：10 批里 5 次**假 `rejected`** —— 日志说“等 120s 没等到验证码
    邮件（打了 113 次收信接口）”，把排查引向“Worker 没收到信 / 域名白名单”，
    而真相是“本地闸门把每一次尝试都换掉了，一个注册请求都没发”。
    按项目自己的定义，这必须是 `skipped`（没试过）而不是 `failed`（试过了不行）。
    """
    monkeypatch.setattr(P.config, "mail_domains", lambda: ["a.com", "b.com", "c.com"])
    monkeypatch.setattr(P.config, "DOMAIN_MAX_PER_WINDOW", 1)
    monkeypatch.setattr(P.config, "DOMAIN_WINDOW_S", 3600.0)
    # 把三个域名的本地窗口都打满 ⇒ 之后每次尝试只能“换域名”，换不动。
    for d in ("a.com", "b.com", "c.com"):
        P.domain_limiter(d).acquire(0)

    client = FakeClient()
    rec = _run(monkeypatch, client=client)[0]

    assert rec.status == "skipped"  # 一个注册请求都没发
    assert P.error_kind_of(rec) == P.ERR_QUOTA_GUARD
    assert client.calls == []  # 确认 send_code 根本没被调用
    assert rec.stages["code"] != "failed"  # 不是那个假的“等码超时”


def test_register_loop_exhausted_is_not_marked_ok(monkeypatch):
    """🔴 回归：注册循环的尝试被「换出口」吃光时，**绝不能**标成 ok。

    实测踩到 2 次：`stages['register'] == 'ok(uid=)'`（uid 是空的），然后拿一个
    **从没注册过**的账号去登录，最后以
    `登录失败：HTTP 401 Invalid username or password` 收场 —— 报告把原因
    指向**密码**，与真相（注册请求根本没发出去）恰好相反。
    """
    slots = ["s1", "s2"]
    monkeypatch.setattr(P.config, "REGISTER_MAX_PER_WINDOW", 1)
    monkeypatch.setattr(P.config, "REGISTER_WINDOW_S", 3600.0)
    monkeypatch.setattr(P.config, "REGISTER_RETRIES", 1)
    monkeypatch.setattr(P.config, "DOMAIN_MAX_PER_WINDOW", 100)
    monkeypatch.setattr(P.config, "DOMAIN_WINDOW_S", 1.0)

    pool = SlotPool(list(slots))
    held = pool.acquire()  # 模拟生产者已占住一个出口
    # 两个出口的 IP 窗口都打满 ⇒ 每次尝试只能“换出口”，换到循环耗尽
    for s in slots:
        P.register_limiter(s).acquire(0)

    client = FakeClient()
    ticket = P.Ticket(
        rec=P.AccountRecord(email="a@x.com", password="pw", mail_domain="d.com"),
        # ⚠ `Ticket.client` 的声明类型是具体的 `AgnesClient`（生产代码的契约），
        #   而这里注入的是替身 —— `cast` 把"这是测试替身"这个意图写明。
        client=cast("P.AgnesClient", client),
        code="123456",
        proxy=held,
        domain="d.com",
    )
    rec = P.finish(ticket, client_factory=lambda proxy=None: client, slot_pool=pool)

    assert "register" not in client.calls  # 注册请求一次都没发
    assert rec.stages["register"] == "failed"  # 🔴 不是 "ok(uid=)"
    assert P.error_kind_of(rec) == P.ERR_QUOTA_GUARD
    assert rec.status == "failed"  # 码已发出 ⇒ failed，不是 skipped
    assert rec.stages["login"] == ""  # 没有往下跑


def test_register_response_without_user_is_rejected(monkeypatch):
    """🔴 回归：注册响应缺 `data.user.id`（HTTP 200 的空壳）必须判失败。

    否则会把“没注册”标成“注册成功”，再拿它去登录 —— 那会把原因伪装成
    `登录失败：401 Invalid username or password`。
    """

    class EmptyRegisterClient(FakeClient):
        def register(self, email, password, code):
            self.calls.append("register")
            return {"code": 200, "data": {}}

    rec = _run(monkeypatch, client=EmptyRegisterClient())[0]
    assert rec.status == "failed"
    assert rec.stages["register"] == "failed"
    assert P.error_kind_of(rec) == P.ERR_REJECTED
    assert "data.user" in rec.error
    assert rec.stages["login"] == ""  # 没往下跑


def test_verify_failure_is_non_fatal(monkeypatch):
    _patch_verify(monkeypatch, ok=False, note="auth_failed: HTTP 401")
    monkeypatch.setattr(P.config, "REGISTER_MAX_PER_WINDOW", 100)
    monkeypatch.setattr(P.config, "REGISTER_WINDOW_S", 1.0)
    monkeypatch.setattr(P.config, "REGISTER_RETRIES", 0)
    results = P.run_batch(
        count=1,
        producers=1,
        workers=1,
        verbose=False,
        mail_factory=lambda: FakeMail(),
        client_factory=lambda proxy=None: FakeClient(),
        slot_pool=SlotPool([]),
    )
    rec = results[0]
    assert rec.status == "success"  # key 建出来了，账号本身是成功的
    assert rec.stages["verify"].startswith("auth_failed")


def test_verify_ms_is_per_record_not_whole_phase(monkeypatch):
    """🔴 回归：`verify_ms` 必须是**每个账号自己那一段**。

    实测踩过：`run_batch` 围着整个 `verify_keys` 取一次 `t0` 再发给所有记录
    ⇒ 5 个账号的 `verify_ms` 全是同一个 `70414`，报告把"校验阶段总时长"
    记到**每个**账号头上，把"单 key 全链路速率"整体带偏。

    这里用可控假时钟（不 sleep）：一个账号的校验耗时必须等于它自己那一段，
    而不是阶段总时长。
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr(P.time, "time", lambda: clock["t"])

    def fake_verify(key, **kw):
        clock["t"] += 3.0
        return {"ok": True, "note": "ok(1 model)", "models": ["m"], "usage": {}}

    monkeypatch.setattr(P._apikey, "verify", fake_verify)
    rec = P.AccountRecord(email="a@x.com", status="success", api_key="sk-A")
    P.verify_keys([rec], verbose=False, log=lambda *_: None)
    assert rec.timings["verify_ms"] == 3000
    assert rec.models_count == 1


def test_make_password_policy():
    for _ in range(20):
        pw = P.make_password(12)
        assert len(pw) == 12
        assert any(c.islower() for c in pw)
        assert any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert any(not c.isalnum() for c in pw)


def test_classify_maps_errors():
    from src.agnes import AgnesNetwork, AgnesQuota, AgnesRejected
    from src.quota import QuotaExceeded

    assert P.classify(AgnesQuota("x")) == P.ERR_QUOTA
    assert P.classify(AgnesRejected("x")) == P.ERR_REJECTED
    assert P.classify(AgnesNetwork("x")) == P.ERR_NETWORK
    assert P.classify(QuotaExceeded("x")) == P.ERR_QUOTA_GUARD
    assert P.classify(ValueError("x")) == P.ERR_NETWORK


def test_window_busy_instead_of_sleeping_forever():
    """🔴 守卫（回归）：窗口打满且等不起时抛 `WindowBusy`，而不是抱着长度
    未知的窗口睡死在锁里。

    这个场景真实发生过：域名维度默认窗口 3600s，两个测试用例打满之后，
    第三个用例在 `acquire()` 里睡 1 小时 —— `tests/test_pipeline.py` 整文件
    必挂。封顶等待**不占名额**（否则外面的人拿到的"立即能发"是假的）。
    """
    win = P.SlidingWindow(max_events=1, window_s=3600.0)
    assert win.acquire() == 0.0  # 第一个请求不需要等

    t0 = time.time()
    with pytest.raises(P.WindowBusy) as ei:
        win.acquire(max_wait=0.5)
    elapsed = time.time() - t0

    assert elapsed < 5  # 没有真的去睡 3600s
    assert ei.value.wait_s > 3000  # 但异常带上了"还要等多久"
    assert win.wait_time() > 3000  # 没占到名额：窗口对别人依然关着
    assert P.classify(P.WindowBusy("x")) == P.ERR_QUOTA_GUARD


def test_window_busy_acquire_without_cap_still_sleeps():
    """`max_wait=None`（默认）保持旧语义：等满窗口而不是抛。

    调用方全都显式传了封顶，这是给"就想等"的场景留的门 —— 行为不变，
    不是回归。
    """
    win = P.SlidingWindow(max_events=1, window_s=0.3)
    assert win.acquire() == 0.0
    t0 = time.time()
    waited = win.acquire()  # 不封顶 ⇒ 睡到窗口滑出
    assert waited >= 0.1
    assert time.time() - t0 >= 0.1
    # 睡完之后窗口应已滑出（那次 acquire 自己占的位也在窗口外），不用再等
    time.sleep(0.35)
    assert win.acquire() == 0.0


def test_window_events_persist_across_processes(monkeypatch):
    """🔴 守卫（P5）：窗口事件必须**跨进程**生效。

    进程内窗口的实际后果：`python run.py --count 1` 连跑两次，第二个进程仍然
    认为窗口是空的 ⇒ 本地闸门形同不存在 ⇒ 撞服务端墙（台账实测 17 条 `quota`
    失败，而实际用法正是一天内 24 次短跑）。

    这里用"清空 limiter 字典"模拟一次新的 `run.py` 进程 —— 新进程里那个字典
    本来就是空的，除此之外没有任何跨进程状态。
    """
    monkeypatch.setattr(P.config, "REGISTER_MAX_PER_WINDOW", 1)
    monkeypatch.setattr(P.config, "REGISTER_WINDOW_S", 3600.0)

    assert P.register_limiter("s1").acquire(0) == 0.0  # 占掉唯一的名额
    P._REGISTER_LIMITERS.clear()  # ⇐ 模拟进程重启
    win = P.register_limiter("s1")

    assert win.wait_time() > 3000, "窗口没跨进程生效 —— 新进程又以为窗口是空的"
    with pytest.raises(P.WindowBusy):
        win.acquire(max_wait=0.5)  # 没真的睡 1 小时
    # 另一个出口必须是**独立**的桶（多出口 = 多张窗口，那是吞吐的乘数）
    assert P.register_limiter("s2").wait_time() == 0.0


def test_domain_window_also_persists_across_processes(monkeypatch):
    """域名维度同样落盘 —— 它是两道闸门里更紧的那一道（实测 13/15 条限流来自它）。"""
    monkeypatch.setattr(P.config, "DOMAIN_MAX_PER_WINDOW", 1)
    monkeypatch.setattr(P.config, "DOMAIN_WINDOW_S", 3600.0)

    assert P.domain_limiter("a.com").acquire(0) == 0.0
    P._DOMAIN_LIMITERS.clear()  # ⇐ 模拟进程重启

    assert P.domain_limiter("a.com").wait_time() > 3000
    assert P.domain_limiter("b.com").wait_time() == 0.0


def test_acquire_many_is_atomic_and_refuses_partial():
    """🔴 守卫：`acquire_many` 要么拿到 n 格、要么一格不占（**原子**）。

    占一半是最坏的：调用方会以为占成功了，而实际只够发一枪。
    """
    win = P.SlidingWindow(max_events=2, window_s=3600.0)
    win.acquire_many(2)
    assert win.room() == 0
    with pytest.raises(P.WindowBusy):
        win.acquire_many(2, max_wait=0.0)
    assert win.room() == 0  # 没被占掉半格

    win.release(1)
    assert win.room() == 1
    win.release(99)  # 多还要安全（不抛、不倒扣）
    assert win.room() == 2


def test_acquire_many_rejects_impossible_n():
    """上限本身就装不下 n 格 = 配置矛盾，等多久都没用 ⇒ 立刻抛，别睡。"""
    win = P.SlidingWindow(max_events=1, window_s=60.0)
    with pytest.raises(P.WindowBusy) as ei:
        win.acquire_many(2, max_wait=0.0)
    assert "配置矛盾" in str(ei.value)
    assert win.room() == 1  # 一格没动


def test_domain_window_reserves_both_shots_so_codes_are_not_burned(monkeypatch):
    """契约：域名窗口装不下「发码 + 注册」两枪时，**一枪都不发**。

    实测（2026-10-14，5 账号跑批）：3/5 是 `code='ok(5 polls)'` +
    `register='failed' [quota_guard]` —— 码烧了、账号拿不到。根因是 `provision`
    只预留 1 格就发码，而一个账号要打**两枪**（发码 + 注册共用同一张域名计数，
    见 docs/protocol.md §3），且注册时域名已定死在邮箱里、**换不了**。

    ⚠ 本用例是**顺序**跑的（`_run` 固定 `producers=1`），而真正的缺陷是
      **竞态** ⇒ 它在修前也会过。真正的回归用例是下面那条
      `test_concurrent_producers_cannot_burn_codes`。这条锁的是**契约**：
      "预留不够就一枪不发"，以及"一个成功账号恰好占 2 格"（不漏不多）。
    """
    monkeypatch.setattr(P.config, "mail_domains", lambda: ["only.com"])
    monkeypatch.setattr(P.config, "DOMAIN_MAX_PER_WINDOW", 2)  # = 1 个账号
    monkeypatch.setattr(P.config, "DOMAIN_WINDOW_S", 3600.0)

    # 第 1 个账号：预留 2 格 → 发码 → 注册 → 成功（域名正好占满）
    first = FakeClient()
    r1 = _run(monkeypatch, client=first)[0]
    assert r1.status == "success", r1.error
    assert first.calls == ["send_code", "register", "login", "create_key"]
    assert P.domain_limiter("only.com").room() == 0  # 恰好 2 枪，不多不少

    # 第 2 个账号：一格都没有，也没别的域名可换
    #   ⇒ 必须 `skipped`（**一个请求都没发**），绝不能是"发了码才失败"
    second = FakeClient()
    r2 = _run(monkeypatch, client=second)[0]
    assert r2.status == "skipped", (r2.status, r2.error)
    assert P.error_kind_of(r2) == P.ERR_QUOTA_GUARD
    assert second.calls == []  # 🔴 send_code 一次都没调 = 没烧码
    assert r2.stages["mailbox"] == "skipped"
    assert "避免烧码" in r2.error


def test_domain_reservation_is_released_when_account_dies(monkeypatch):
    """🔴 守卫：账号在发码之后死掉时，**没用上的那一格必须还回去**。

    不还的后果：一个域名一小时只有 2 格，每失败一个账号就白占一格
    ⇒ 跑几个失败账号就把域名锁死，现象是"什么都没干却被限流"。
    """
    monkeypatch.setattr(P.config, "mail_domains", lambda: ["only.com"])
    monkeypatch.setattr(P.config, "DOMAIN_MAX_PER_WINDOW", 4)
    monkeypatch.setattr(P.config, "DOMAIN_WINDOW_S", 3600.0)

    # 发码成功、但等不到码 ⇒ 死在 rejected（注册那一枪永远不会发生）
    r1 = _run(monkeypatch, mail=FakeMail(code=None))[0]
    assert r1.status == "failed"
    assert P.error_kind_of(r1) == P.ERR_REJECTED

    # 预留 2 格 → 发码用掉 1 格 → 死掉时归还剩下的 1 格。
    # ⚠ 剩下的**那一格发码是真的发出去了**（服务端记了账），所以不该还 ——
    #   4 - 1 = 3 才是诚实值，还成 4 就比服务端松了。
    assert P.domain_limiter("only.com").room() == 3


def test_send_code_network_failure_charges_exactly_one_grid(monkeypatch):
    """🔴 守卫（回归）：`send_code` 撞**网络错误**时，域名窗口必须扣 **1** 格。

    网络层失败（`RemoteDisconnected` / `SSLEOFError`）的请求**可能已经落地** ——
    服务端可能已经记了这一枪。所以本地不能把它当成"没发生"：那样本地闸门
    会比服务端**松**，而"宁可少打"是本地闸门的一贯要求。

    实测踩到（2026-10-14 11:36 跑批）：3 条 `send_code` 撞网络错误，
    域名窗口**各扣 0 格**（预留的 2 格被全数还回）。
    根因是预留只在**成功 / 限流**两条路上扣，网络失败那条没扣。
    ⇒ 现在这条不变量由 `DomainReservation.consume()`（在调用**之前**扣）保证。
    """
    monkeypatch.setattr(P.config, "mail_domains", lambda: ["only.com"])
    monkeypatch.setattr(P.config, "DOMAIN_MAX_PER_WINDOW", 2)
    monkeypatch.setattr(P.config, "DOMAIN_WINDOW_S", 3600.0)

    from src.agnes import AgnesNetwork

    client = FakeClient(send_error=AgnesNetwork("RemoteDisconnected"))
    r = _run(monkeypatch, client=client)[0]

    assert r.status == "failed"
    assert P.error_kind_of(r) == P.ERR_NETWORK
    assert r.stages["mailbox"] == "ok"  # 邮箱建出来了
    assert r.stages["code"] == "failed"  # 死在发码
    # 🔴 恰好扣 1 格：0 = 太松（比服务端松），2 = 太紧（把注册那格也白占）
    room = P.domain_limiter("only.com").room()
    assert room == 1, f"应扣 1 格 ⇒ room=1；实际 room={room}"


def test_batch_does_not_burn_codes_when_domains_run_out(monkeypatch):
    """契约：3 个域名 × 2 格（= 各 1 个账号），跑 4 个 ⇒ 3 成功 + 1 跳过。

    修前：4 个生产者各自发码 ⇒ 域名窗口全被打满 ⇒ **所有** register 进不去
          ⇒ 码全烧了、账号一个都拿不到（实测 3/5）。
    修后：3 个账号各占满一个域名（两枪都装得下）⇒ 成功；
          第 4 个**一枪不发** ⇒ `skipped`（而不是"烧了码才失败"）。

    ⚠ 同样地，`producers=1` 是顺序跑，所以这条在修前也会过 —— 它锁的是
      "**不变量**：没有任何账号是'发了码却没成功'"以及"每域名恰好 2 枪"。
    """
    monkeypatch.setattr(P.config, "mail_domains", lambda: ["a.com", "b.com", "c.com"])
    monkeypatch.setattr(P.config, "DOMAIN_MAX_PER_WINDOW", 2)
    monkeypatch.setattr(P.config, "DOMAIN_WINDOW_S", 3600.0)

    results = _run(monkeypatch, count=4)  # `_run` 固定 producers=1 ⇒ 顺序可预期
    by = {}
    for r in results:
        by[r.status] = by.get(r.status, 0) + 1

    assert by.get("success") == 3, [(r.status, r.error[:60]) for r in results]
    assert by.get("skipped") == 1, [(r.status, r.error[:60]) for r in results]
    # 🔴 关键：没有任何一条是"码发了才失败"
    for r in results:
        assert not (r.stages["code"].startswith("ok") and r.status != "success"), (
            r.status,
            r.stages["code"],
            r.error[:80],
        )
    # 三个域名各恰好 2 枪（不多不少 ⇒ 预留没漏也没多占）
    for d in ("a.com", "b.com", "c.com"):
        assert P.domain_limiter(d).room() == 0, d


def test_concurrent_producers_cannot_burn_codes(monkeypatch):
    """🔴 守卫（**真正的回归**）：并发生产者不能"都发了码、却都注册不进去"。

    实测（2026-10-14，5 账号跑批）：3/5 是 `code='ok(...)'` +
    `register='failed' [quota_guard]`。根因是 `provision` 的"看窗口还有没有位置"
    与"占位置"**不是原子的**：两路生产者可以**同时**看到还有位置，然后一起
    发码 —— 而域名窗口只有 2 格，注册那一枪就没地方了。

    这里用一个 barrier 把两路生产者**钉在**"预留完成、发码之前"那一点，
    把竞态变成确定性的（不靠碰运气复现）：

        修前：两条都是 `failed`（码烧了）—— 上面那条不变量被破坏
        修后：一条成功、另一条 `skipped`（**一枪都没发**）
    """
    monkeypatch.setattr(P.config, "mail_domains", lambda: ["only.com"])
    monkeypatch.setattr(P.config, "DOMAIN_MAX_PER_WINDOW", 2)  # 只够 1 个账号
    monkeypatch.setattr(P.config, "DOMAIN_WINDOW_S", 3600.0)
    monkeypatch.setattr(P.config, "REGISTER_MAX_PER_WINDOW", 100)  # 隔离出域名维度
    monkeypatch.setattr(P.config, "REGISTER_WINDOW_S", 1.0)
    monkeypatch.setattr(P.config, "REGISTER_RETRIES", 0)
    _patch_verify(monkeypatch)

    # 只够 1 个账号 ⇒ 两路都到齐是不可能的（另一路会被闸门拦下）。
    # ⚠ 超时短一点：修后本来就只有一路能走到这里。
    barrier = threading.Barrier(2, timeout=2)

    class BarrierMail(FakeMail):
        def create_mailbox(self, domain=None, count=1):
            # 两路都到齐才放行。另一路被闸门拦下时这里会超时 —— 那正是"修后"的
            # 正常情况，所以用 `suppress` 明确表达"这个异常是预期的"，
            # 而不是写一个空 `except`（那看起来像漏了处理）。
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait()
            return super().create_mailbox(domain, count)

    clients = [FakeClient(), FakeClient()]
    handed: list = []

    def factory(proxy=None):
        # 生产者与消费者都会调它；按调用顺序发，保证每个线程拿到稳定实例
        c = clients[len(handed) % 2]
        handed.append(c)
        return c

    results = P.run_batch(
        count=2,
        producers=2,
        workers=1,
        verbose=False,
        mail_factory=lambda: BarrierMail(),
        client_factory=factory,
        slot_pool=SlotPool([]),
    )

    by = {}
    for r in results:
        by[r.status] = by.get(r.status, 0) + 1
    assert by.get("success") == 1, [(r.status, r.error[:80]) for r in results]
    assert by.get("skipped") == 1, [(r.status, r.error[:80]) for r in results]
    # 🔴 不变量：没有任何账号是"发了码却没成功"（= 烧码）
    for r in results:
        assert not (r.stages["code"].startswith("ok") and r.status != "success"), (
            "有账号发了码却没注册成功（码被烧掉了）",
            r.status,
            r.stages["code"],
            r.error[:80],
        )


def test_slot_key_never_contains_credentials():
    """🔴 守卫：窗口存储键**绝不能**含代理账密。

    槽位串是 `scheme://user:pass@host:port`（或 `host:port:user:pass`），直接
    当键就是把凭据持久化到 `state/register_windows.jsonl` —— `state/` 是
    gitignore 的，但"不被提交"不等于"不该落盘"。与 `SlotStats.label` 是同一类
    事故（那边实测把代理密码打进了收尾报告）。
    """
    secret = "sup3rs3cret"
    for raw in (
        f"http://alice:{secret}@203.0.113.7:8080",
        f"203.0.113.7:8080:alice:{secret}",
    ):
        key = P._slot_key(raw)
        assert secret not in key, key
        assert "alice" not in key, key
        assert "203.0.113.7" in key, key  # host:port 保留（排查要对得上报告）

    # 🔴 同 host:port、不同槽位必须**不同键**：本项目 `.slots.txt` 的 10 个槽位
    #    恰好是"同 host:port、不同 sid"，只留脱敏串会让它们**塌成一个键**
    #    ⇒ 10 张窗口变 1 张（吞吐掉 10 倍，而且不报错）。
    assert P._slot_key("us.example.net:2000:u1:pw") != P._slot_key("us.example.net:2000:u2:pw")
    # 直连（无槽位）也要有稳定的键
    assert P._slot_key(None) == P._slot_key("") == "ip:direct"


def test_window_state_is_isolated_from_real_state_dir():
    """🔴 守卫：测试**绝不能**写进真实的 `state/`。

    窗口状态一旦被测试污染，会顺着 `state/register_windows.jsonl` 影响
    **下一次真实跑批**的闸门 —— 症状是"某个出口莫名被限流"，而原因在两小时前
    跑的一次测试里（`quota.py` 那边实测踩过同样的坑，见 `conftest.py`）。

    ⚠ 本用例断言的是 `conftest._isolate_state` **真的生效**，而不是断言代码
      逻辑 —— 它正是那个 fixture 的回归用例。
    """
    from src import windowstore

    p = windowstore.state_path().resolve()
    real = (windowstore.ROOT / "state").resolve()
    assert not p.is_relative_to(real), (
        f"窗口状态落到了真实 state/：{p}\n"
        f"  `conftest.py::_isolate_state` 应该把 AR_WINDOW_STATE 钉在 tmp 目录里。"
    )
