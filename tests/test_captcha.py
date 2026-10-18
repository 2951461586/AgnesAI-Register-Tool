"""captcha provider 的离线契约（零网络、零浏览器）。

钉住四件事：

1. `send_code` 带 `ticket`/`randstr`；不带时保持旧 query；
2. `AgnesCaptcha` 归到 `captcha`（**不是** `rejected`）；
3. `AR_CAPTCHA_ENABLED=0` 时 `default_provider()` 返回 `None`；
4. 流水线**先解 captcha、再发码**，并把 ticket/randstr 交给 client。

⚠ 本文件不 import Playwright / numpy / scipy / Pillow：`src/captcha.py` 只在
   `solve()` 里懒加载它们，`default_provider()` 只读配置。
"""

from pathlib import Path

from config_source import config_source_text

from common import config
from src import pipeline as P
from src.agnes import AgnesCaptcha, AgnesClient

ROOT = Path(__file__).resolve().parents[1]


class _Resp:
    """`_json_or_none` 只用到 `.json()`。"""

    def json(self):
        return {"code": 200}


def _default_provider():
    """动态 import：避免 LSP 对**新建模块**的陈旧索引误报。"""
    import importlib

    return importlib.import_module("src.captcha").default_provider


def test_send_code_includes_ticket_and_randstr(monkeypatch):
    client = AgnesClient()
    captured = {}

    def fake_request(method, path, *, params=None, payload=None, headers=None):
        captured.update(method=method, path=path, params=params)
        return _Resp()

    monkeypatch.setattr(client, "_request", fake_request)
    client.send_code("a@b.c", ticket="T-1", randstr="@R")
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/verification"
    assert captured["params"] == {
        "email": "a@b.c",
        "purpose": "register",
        "ticket": "T-1",
        "randstr": "@R",
    }


def test_send_code_without_captcha_keeps_legacy_query(monkeypatch):
    client = AgnesClient()
    captured = {}

    def fake_request(method, path, *, params=None, payload=None, headers=None):
        captured.update(params=params)
        return _Resp()

    monkeypatch.setattr(client, "_request", fake_request)
    client.send_code("a@b.c")
    assert captured["params"] == {"email": "a@b.c", "purpose": "register"}


def test_captcha_error_is_its_own_kind():
    assert P.classify(AgnesCaptcha("boom")) == "captcha"
    assert "ERR_CAPTCHA" in P.__all__
    assert "AgnesCaptcha" in P.__all__


def test_default_provider_is_none_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", False)
    assert _default_provider()() is None


def test_captcha_defaults_are_on_and_headless():
    """契约：默认开、默认 headless（用户明确要求）。"""
    src = config_source_text()
    assert '_env_bool("AR_CAPTCHA_ENABLED", True)' in src
    assert '_env_bool("AR_CAPTCHA_HEADLESS", True)' in src


def test_pipeline_solves_captcha_before_send_code(monkeypatch):
    """端到端（离线）：provider 先被调用，client 收到 ticket/randstr。"""
    from test_pipeline import FakeClient, FakeMail, _patch_verify

    calls = []

    class FakeCaptcha:
        def solve(self, email, *, proxy=None):
            calls.append(("solve", email, proxy))
            return ("T-1", "@R")

    monkeypatch.setattr(P.config, "REGISTER_MAX_PER_WINDOW", 100)
    monkeypatch.setattr(P.config, "REGISTER_WINDOW_S", 1.0)
    monkeypatch.setattr(P.config, "REGISTER_RETRIES", 0)
    _patch_verify(monkeypatch)
    client = FakeClient()
    results = P.run_batch(
        count=1,
        producers=1,
        workers=1,
        verbose=False,
        mail_factory=lambda domain=None: FakeMail(),
        client_factory=lambda proxy=None: client,
        slot_pool=P.SlotPool([]),
        captcha_provider=FakeCaptcha(),
    )
    assert len(results) == 1
    assert results[0].status == "success"
    assert calls and calls[0][1] == "oai-test@own.example"
    assert client.calls[0] == "send_code"
    assert client.last_captcha == ("T-1", "@R")
    assert results[0].timings.get("captcha_ms") is not None


def test_captcha_failure_marks_account_failed_with_captcha_kind(monkeypatch):
    """provider 抛 AgnesCaptcha ⇒ 账号 failed/captcha，且**没发码**。"""
    from test_pipeline import FakeClient, FakeMail, _patch_verify

    class BadCaptcha:
        def solve(self, email, *, proxy=None):
            raise AgnesCaptcha("浏览器超时")

    monkeypatch.setattr(P.config, "REGISTER_MAX_PER_WINDOW", 100)
    monkeypatch.setattr(P.config, "REGISTER_WINDOW_S", 1.0)
    monkeypatch.setattr(P.config, "REGISTER_RETRIES", 0)
    _patch_verify(monkeypatch)
    client = FakeClient()
    results = P.run_batch(
        count=1,
        producers=1,
        workers=1,
        verbose=False,
        mail_factory=lambda domain=None: FakeMail(),
        client_factory=lambda proxy=None: client,
        slot_pool=P.SlotPool([]),
        captcha_provider=BadCaptcha(),
    )
    assert len(results) == 1
    assert results[0].status == "failed"
    assert results[0].error_kind == "captcha"
    assert client.calls == []  # 🔴 一个发码请求都没发


def test_debug_images_saved_on_failure(tmp_path, monkeypatch):
    """`AR_CAPTCHA_DEBUG_DIR` 设了才留图；默认关（不产生任何文件）。"""
    import json

    from src.captcha import _save_debug_images

    # 默认空 = 不留
    monkeypatch.setattr(config, "CAPTCHA_DEBUG_DIR", "")
    _save_debug_images(b"bg", b"sprite", [], None, AgnesCaptcha("x"))
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setattr(config, "CAPTCHA_DEBUG_DIR", str(tmp_path))
    _save_debug_images(
        b"bg-bytes",
        b"sprite-bytes",
        [(1.0, 2.0)],
        {"details": [{"score": 0.5}]},
        AgnesCaptcha("boom"),
    )
    files = sorted(p.name for p in tmp_path.iterdir())
    assert any(n.endswith("_img0_sprite.png") for n in files), files
    assert any(n.endswith("_img1_bg.png") for n in files), files
    meta = next(p for p in tmp_path.iterdir() if p.name.endswith("_meta.json"))
    data = json.loads(meta.read_text(encoding="utf-8"))
    assert data["error"] == "boom"
    assert data["points"] == [[1.0, 2.0]]


def test_captcha_timeout_defaults_are_45s_and_no_inner_retry():
    """契约：一次 45s 超时（不是 90s × 2 次）—— 降级交给 `provision` 换出口。"""
    src = config_source_text()
    assert '_env_int("AR_CAPTCHA_TIMEOUT_S", 45)' in src
    assert '_env_int("AR_CAPTCHA_RETRIES", 0)' in src


def test_captcha_failure_rotates_exit_once_then_gives_up(monkeypatch):
    """captcha 失败 ⇒ **换出口重试一次**；第二个出口也失败 ⇒ 直接 failed/captcha。"""
    from test_pipeline import FakeClient, FakeMail, _patch_verify

    seen_proxies = []

    class AlwaysBadCaptcha:
        def solve(self, email, *, proxy=None):
            seen_proxies.append(proxy)
            raise AgnesCaptcha("等 captcha 图片超时（45s）")

    monkeypatch.setattr(P.config, "REGISTER_MAX_PER_WINDOW", 100)
    monkeypatch.setattr(P.config, "REGISTER_WINDOW_S", 1.0)
    monkeypatch.setattr(P.config, "REGISTER_RETRIES", 3)
    _patch_verify(monkeypatch)
    client = FakeClient()
    results = P.run_batch(
        count=1,
        producers=1,
        workers=1,
        verbose=False,
        mail_factory=lambda domain=None: FakeMail(),
        client_factory=lambda proxy=None: client,
        slot_pool=P.SlotPool(["http://slot-a", "http://slot-b", "http://slot-c"]),
        captcha_provider=AlwaysBadCaptcha(),
    )
    assert len(results) == 1
    assert results[0].status == "failed"
    assert results[0].error_kind == "captcha"
    # 🔴 恰好换了 **1** 次出口 ⇒ provider 被调用 2 次（原出口 + 新出口）
    # 同出口重解 × 2 个出口（换出口本身仍被限成 1 次）
    assert len(seen_proxies) == 2 * P.config.CAPTCHA_RESOLVE_ATTEMPTS, seen_proxies
    # 🔴 调用次数 = 同出口重解 × 2 个出口 = `CAPTCHA_RESOLVE_ATTEMPTS`(2) × 2 = **4**。
    #    关键的是**换出口被限成 1 次**（`REGISTER_RETRIES=3` 而不限的话会更多）。
    # ⚠ 默认 `AR_CAPTCHA_USE_PROXY=0` ⇒ 传给 provider 的 proxy 都是 `None`（直连），
    #    所以这里不能靠“proxy 不同”来判换出口。
    assert len(seen_proxies) == 2 * P.config.CAPTCHA_RESOLVE_ATTEMPTS, seen_proxies
    assert client.calls == []  # 一个发码请求都没发


def test_captcha_retry_succeeds_on_second_exit(monkeypatch):
    """第一个出口超时 → 换出口后解出 ⇒ 账号照常成功。"""
    from test_pipeline import FakeClient, FakeMail, _patch_verify

    class FlakyCaptcha:
        def __init__(self):
            self.n = 0

        def solve(self, email, *, proxy=None):
            self.n += 1
            if self.n == 1:
                raise AgnesCaptcha("等 captcha 图片超时（45s）")
            return ("T-ok", "@ok")

    monkeypatch.setattr(P.config, "REGISTER_MAX_PER_WINDOW", 100)
    monkeypatch.setattr(P.config, "REGISTER_WINDOW_S", 1.0)
    monkeypatch.setattr(P.config, "REGISTER_RETRIES", 3)
    _patch_verify(monkeypatch)
    client = FakeClient()
    results = P.run_batch(
        count=1,
        producers=1,
        workers=1,
        verbose=False,
        mail_factory=lambda domain=None: FakeMail(),
        client_factory=lambda proxy=None: client,
        slot_pool=P.SlotPool(["http://slot-a", "http://slot-b"]),
        captcha_provider=FlakyCaptcha(),
    )
    assert results[0].status == "success"
    assert client.last_captcha == ("T-ok", "@ok")


def test_captcha_uses_proxy_only_when_configured(monkeypatch):
    """`AR_CAPTCHA_USE_PROXY=1` 才把槽位传给 provider；默认直连。"""
    from test_pipeline import FakeClient, FakeMail, _patch_verify

    seen = []

    class SpyCaptcha:
        def solve(self, email, *, proxy=None):
            seen.append(proxy)
            return ("T", "@R")

    monkeypatch.setattr(P.config, "REGISTER_MAX_PER_WINDOW", 100)
    monkeypatch.setattr(P.config, "REGISTER_WINDOW_S", 1.0)
    monkeypatch.setattr(P.config, "REGISTER_RETRIES", 0)
    _patch_verify(monkeypatch)

    def run_once():
        P.run_batch(
            count=1,
            producers=1,
            workers=1,
            verbose=False,
            mail_factory=lambda domain=None: FakeMail(),
            client_factory=lambda proxy=None: FakeClient(),
            slot_pool=P.SlotPool(["http://slot-a"]),
            captcha_provider=SpyCaptcha(),
        )

    monkeypatch.setattr(P.config, "CAPTCHA_USE_PROXY", False)
    run_once()
    assert seen == [None], seen

    seen.clear()
    monkeypatch.setattr(P.config, "CAPTCHA_USE_PROXY", True)
    run_once()
    assert seen == ["http://slot-a"], seen


def test_captcha_use_proxy_defaults_to_direct():
    src = config_source_text()
    assert '_env_bool("AR_CAPTCHA_USE_PROXY", False)' in src


def test_contradictory_points_trigger_resolve(monkeypatch):
    """A3：两个点挤在一起 ⇒ 提交前判矛盾（不白烧一次 verify）。"""
    from src.captcha import _check_points_sane

    _check_points_sane([(10, 10), (200, 200), (400, 100)])  # 正常
    try:
        _check_points_sane([(10, 10), (20, 18), (400, 100)])
    except AgnesCaptcha as ex:
        assert "[contradiction]" in str(ex)
    else:
        raise AssertionError("两个点距离 12px 却没有判矛盾")


def test_captcha_failure_messages_carry_slugs():
    """B3：失败文案带 slug，报告里能区分「超时」与「答案被拒」。"""
    src = (ROOT / "src" / "captcha.py").read_text(encoding="utf-8")
    for slug in ("[timeout]", "[rejected]", "[contradiction]", "[solver]"):
        assert slug in src, slug


def test_captcha_retry_does_not_recreate_mailbox(monkeypatch):
    """B1：captcha 失败重解时**复用**同一邮箱，不重复 create_mailbox。"""
    from test_pipeline import FakeClient, FakeMail, _patch_verify

    created = []

    class CountingMail(FakeMail):
        def create_mailbox(self, domain=None, count=1):
            created.append(domain)
            return ["oai-test@own.example"]

    class FlakyCaptcha:
        def __init__(self):
            self.n = 0

        def solve(self, email, *, proxy=None):
            self.n += 1
            if self.n == 1:
                raise AgnesCaptcha("[timeout] 等 captcha 图片超时（45s）")
            return ("T", "@R")

    monkeypatch.setattr(P.config, "REGISTER_MAX_PER_WINDOW", 100)
    monkeypatch.setattr(P.config, "REGISTER_WINDOW_S", 1.0)
    monkeypatch.setattr(P.config, "REGISTER_RETRIES", 3)
    monkeypatch.setattr(P.config, "CAPTCHA_RESOLVE_ATTEMPTS", 2)
    _patch_verify(monkeypatch)
    results = P.run_batch(
        count=1,
        producers=1,
        workers=1,
        verbose=False,
        mail_factory=lambda domain=None: CountingMail(),
        client_factory=lambda proxy=None: FakeClient(),
        slot_pool=P.SlotPool(["http://slot-a"]),
        captcha_provider=FlakyCaptcha(),
    )
    assert results[0].status == "success"
    # 🔴 只建了 **1** 个邮箱（重解没有重建）
    assert len(created) == 1, created
    assert len(results[0].mailboxes_created) == 1


def test_preflight_checks_captcha_when_enabled():
    """B2：`--preflight` 必须**顺带**体检 captcha 能力（不耗注册窗口）。"""
    src = (ROOT / "run.py").read_text(encoding="utf-8")
    assert "captcha" in src.lower()
    # 预检段里必须真的调用一次 solve()
    head = src.index("if args.preflight")
    tail = src.index("t0 = time.time()", head)
    block = src[head:tail]
    assert ".solve(" in block, "预检没有真正跑一次 captcha solve"
