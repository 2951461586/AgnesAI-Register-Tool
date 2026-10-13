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

import time

import pytest

from src import pipeline as P
from src.proxypool import SlotPool


class FakeMail:
    def __init__(self, code="123456", create=None):
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
            "data": {"access_token": "reg-jwt", "user": {"id": 11, "uuid": "u-1", "display_id": "d-1"}},
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

    client = FakeClient(send_error=AgnesQuota("Too many registration attempts", code="RATE_LIMITED"))
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
