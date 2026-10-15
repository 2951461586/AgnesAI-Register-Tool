"""`smailr` 客户端的离线用例（零网络）。

为什么值得测
============
它是**第三个**邮箱源，而且有两个前两个源都没有的行为：

  · **等级 403 时复用已有邮箱** —— 写错方向就会"整批失败"（而本来能跑）；
  · **成功落库后删邮箱** —— 写错方向会**删错邮箱**（不可逆）。

另外它与另两个源有一个**根本差别**：地址由**服务端**定（我们只给
`local_part`）⇒ 返回的地址必须以响应为准，自己拼会拼出一个不存在的地址。

⚠ 真实 API 的关键路径（建邮箱 / 收码）在**账号等级修好之前无法端到端验**，
  所以这里用假 session 把判据与形状钉住；真实可达的部分（`list_mailboxes` /
  `quota`）另有一次性人工验证。
"""

import json

import pytest

from src import mailbox
from src.smailr import (
    SmailrClient,
    SmailrError,
    _is_level_restricted,
    _mailbox_email,
    _mailbox_id,
    _parse_ms,
)


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = json.dumps(body, ensure_ascii=False) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """按 `(method, url 子串)` 匹配响应；记录全部调用。

    ⚠ 路由是 **3 元组** `(method, url 片段, 响应)` —— 写成 2 元组
      `((method, 片段), 响应)` 会在这里 `too many values to unpack`（第一版就这么挂的）。
    """

    def __init__(self, routes):
        self.routes = routes  # [(method, url 片段, FakeResponse|Exception|callable)]
        self.calls = []
        self.headers = {}

    def request(self, method, url, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json})
        path = url.split("?", 1)[0]
        for method_want, frag, resp in self.routes:
            # ⚠ 用**路径后缀**匹配，不是子串：`/api/v1/mailboxes` 是
            #    `/api/v1/mailboxes/mb1/mails` 的**前缀** ⇒ 子串匹配会让列表路由
            #    抢走收信路由，而症状是"收信一直不报错、一直轮询到超时"
            #    （第一版就这么挂的，很难查）。
            if method_want == method and path.endswith(frag):
                if isinstance(resp, Exception):
                    raise resp
                if callable(resp):
                    return resp(json or {})
                return resp
        raise AssertionError(f"没有为 {method} {url} 预置响应")


def client_with(routes, **kw):
    c = SmailrClient(api_key="nm_test", **kw)
    fake = FakeSession(routes)
    c.session = fake  # type: ignore[assignment]  # 测试替身
    return c, fake


@pytest.fixture(autouse=True)
def _pin_domain_ids(monkeypatch):
    """把域名配置钉在**测试本地**，不读宿主的 `.env`。

    🔴 与 `conftest.py` 里那次修复同一类缺陷：用例读真实配置 ⇒ 它在 CI 上
      的行为取决于环境，而且**改一次 `.env` 就会让一批用例变红/变绿**。
      实测：2026-10-14 把真 `domain_id` 写进 `.env` 之后，
      `test_create_mailbox_omits_domain_id_for_the_default_domain` 就挂了。

    ⚠ 默认给一个**空的** id 表（= 只有 `SMAILR_DEFAULT_DOMAIN` 那条兜底
      路径），需要 id 的用例自己 `monkeypatch.setattr` 填。
    """
    from common import config

    monkeypatch.setattr(config, "SMAILR_DOMAIN_IDS", {})
    monkeypatch.setattr(config, "SMAILR_DEFAULT_DOMAIN", "smailr.com")


def _mb(mid="mb1", email="agnesabc@smailr.com", **extra):
    return {"id": mid, "email": email, "mail_count": 0, **extra}


# ── 时间戳（`since_ts` 的防"读到旧码"全靠它）─────────────────────
def test_parse_ms_accepts_seconds_ms_and_iso():
    """🔴 秒 / 毫秒 / ISO 三种形态都要归一化到**毫秒**。

    写错的后果：`since_ts` 过滤失效 ⇒ 拿到**上一轮的旧验证码** ⇒ 服务端报
    "验证码错误"，而真相是我们在用旧信（修法完全不同）。
    """
    assert _parse_ms(1_700_000_000) == 1_700_000_000_000  # 秒
    assert _parse_ms(1_700_000_000_000) == 1_700_000_000_000  # 毫秒
    assert _parse_ms("1700000000") == 1_700_000_000_000  # 数字串
    assert _parse_ms("2026-10-14T08:00:00+00:00") == _parse_ms("2026-10-14T16:00:00+08:00")
    assert _parse_ms("2026-10-14T08:00:00Z") > 0


def test_parse_ms_tolerates_garbage():
    for bad in ("", None, "yesterday", [], True, "2026-13-45T99:99:99"):
        assert _parse_ms(bad) == 0


# ── 等级 403 vs 权限 403（判错方向会把排查引到相反方向）──────────
def test_level_403_detected():
    ex = SmailrError("x", status=403, body={"error": "该域名要求用户等级 4 及以上，您当前等级为 2"})
    assert _is_level_restricted(ex) is True


def test_scope_403_is_not_level_403():
    """API key 无权（换 key）与等级不够（改域名设置）是**两件事**。

    🔴 混为一类的后果：key 权限问题会被当成等级问题 ⇒ 去改域名设置，白费功夫。
    """
    for body in (
        {"error": "api key access not allowed for this endpoint"},
        {"error": "missing scope"},
    ):
        assert _is_level_restricted(SmailrError("x", status=403, body=body)) is False


def test_other_status_is_not_level_403():
    assert _is_level_restricted(SmailrError("x", status=400, body={"error": "等级"})) is False


# ── 字段兜底（服务端 JSON 不归我们所有）───────────────────────────
def test_mailbox_email_handles_several_shapes():
    assert _mailbox_email({"email": "a@b.com"}) == "a@b.com"
    assert _mailbox_email({"address": "A@B.com"}) == "a@b.com"
    assert _mailbox_email({"local_part": "x", "domain": "smailr.com"}) == "x@smailr.com"
    assert _mailbox_email({"local_part": "x", "domain_name": "@smailr.com"}) == "x@smailr.com"
    assert _mailbox_email({}) == ""


def test_mailbox_id_handles_several_shapes():
    assert _mailbox_id({"id": "1"}) == "1"
    assert _mailbox_id({"mailbox_id": "2"}) == "2"
    assert _mailbox_id({"uuid": "3"}) == "3"
    assert _mailbox_id({}) == ""


# ── 建址：地址以**响应**为准（服务端分配）─────────────────────────
def test_create_mailbox_takes_the_address_from_the_response():
    """🔴 与另两个源的**根本差别**：我们只给 `local_part`，地址由服务端定。

    自己拼地址会拼出一个**不存在**的地址（而收信是按地址查的）。
    """
    c, fake = client_with(
        [
            (
                "POST",
                "/api/v1/mailboxes",
                FakeResponse(200, {"data": {"id": "mb9", "email": "serverpicked@smailr.com"}}),
            )
        ]
    )
    out = c.create_mailbox("smailr.com", 1)
    assert out == ["serverpicked@smailr.com"], "必须用响应里的地址，不是自己拼的"
    assert fake.calls[0]["json"]["local_part"].startswith("agnes")


def test_create_mailbox_prefers_an_explicit_domain_id(monkeypatch):
    """🔴 **显式配了 `domain_id` 就优先用它**，哪怕它是"默认域名"。

    这条语义是被**浏览器 HAR 推翻后**改过来的（2026-10-14）：
    `POST /api/v1/mailboxes` 的请求体里，4 个域名**全都带了** `domain_id`
    （包括那个默认域名），而且全部 201。反过来说：账号的默认域名是个
    **要等级 4** 的域名 ⇒ 省略 `domain_id` 会撞 403 —— 带上它的真 id
    反而能建成。
    ⇒ "能省略"只是**没配 id 时**的兜底，不该盖过显式配置。
    """
    from common import config

    monkeypatch.setattr(config, "SMAILR_DEFAULT_DOMAIN", "smailr.com")
    monkeypatch.setattr(config, "SMAILR_DOMAIN_IDS", {"smailr.com": "dom-default-uuid"})
    c, fake = client_with(
        [("POST", "/api/v1/mailboxes", FakeResponse(200, {"id": "1", "email": "a@smailr.com"}))]
    )
    c.create_mailbox("smailr.com", 1)
    assert fake.calls[0]["json"]["domain_id"] == "dom-default-uuid"


def test_create_mailbox_omits_domain_id_when_default_and_unconfigured(monkeypatch):
    """没配 id 且它就是默认域名 ⇒ **省略**（API 不可枚举域名，只能靠账号设置）。"""
    from common import config

    monkeypatch.setattr(config, "SMAILR_DEFAULT_DOMAIN", "smailr.com")
    monkeypatch.setattr(config, "SMAILR_DOMAIN_IDS", {})
    c, fake = client_with(
        [("POST", "/api/v1/mailboxes", FakeResponse(200, {"id": "1", "email": "a@smailr.com"}))]
    )
    c.create_mailbox("smailr.com", 1)
    assert "domain_id" not in fake.calls[0]["json"]


def test_create_mailbox_requires_domain_id_for_other_domains(monkeypatch):
    """非默认域名必须配 `domain_id`；没配就报**可操作**的错（而不是发个空 id 出去）。"""
    from common import config

    monkeypatch.setattr(config, "SMAILR_DEFAULT_DOMAIN", "smailr.com")
    monkeypatch.setattr(config, "SMAILR_DOMAIN_IDS", {})
    c, _ = client_with([])
    assert c.create_mailbox("loc.cc", 1) == []
    assert "AR_SMAILR_DOMAIN_IDS" in c.last_error


def test_create_mailbox_sends_the_configured_domain_id(monkeypatch):
    from common import config

    monkeypatch.setattr(config, "SMAILR_DEFAULT_DOMAIN", "smailr.com")
    monkeypatch.setattr(config, "SMAILR_DOMAIN_IDS", {"loc.cc": "dom-uuid-1"})
    c, fake = client_with(
        [("POST", "/api/v1/mailboxes", FakeResponse(200, {"id": "1", "email": "a@loc.cc"}))]
    )
    assert c.create_mailbox("loc.cc", 1) == ["a@loc.cc"]
    assert fake.calls[0]["json"]["domain_id"] == "dom-uuid-1"


# ── 等级 403 → 复用已有邮箱（前两个源没有的行为）──────────────────
def test_level_403_reuses_an_existing_mailbox():
    """🔴 等级不够时**领一个现成的**，而不是整条链失败。

    参考实现（`GPT-Register-Tool`）就是这么做的 —— 否则等级不够时整批都跑不动。
    """
    level_403 = FakeResponse(403, {"error": "该域名要求用户等级 4 及以上，您当前等级为 2"})
    c, _ = client_with(
        [
            ("POST", "/api/v1/mailboxes", level_403),
            (
                "GET",
                "/api/v1/mailboxes",
                FakeResponse(200, [_mb("mb7", "old@smailr.com", mail_count=3)]),
            ),
        ]
    )
    assert c.create_mailbox("smailr.com", 1) == ["old@smailr.com"]
    assert c.reused is True


def test_reuse_prefers_the_emptiest_mailbox():
    """🔴 挑 `mail_count` **最少**的 —— 那是"没用过"的启发式。

    （用过的邮箱一定收过 Agnes 的验证码 ⇒ `mail_count >= 1`。）
    """
    level_403 = FakeResponse(403, {"error": "等级不足"})
    c, _ = client_with(
        [
            ("POST", "/api/v1/mailboxes", level_403),
            (
                "GET",
                "/api/v1/mailboxes",
                FakeResponse(
                    200,
                    [
                        _mb("a", "used@smailr.com", mail_count=5),
                        _mb("b", "fresh@smailr.com", mail_count=0),
                        _mb("c", "half@smailr.com", mail_count=2),
                    ],
                ),
            ),
        ]
    )
    assert c.create_mailbox("smailr.com", 1) == ["fresh@smailr.com"]


def test_reuse_skips_other_domains_archived_and_disabled():
    level_403 = FakeResponse(403, {"error": "等级不足"})
    c, _ = client_with(
        [
            ("POST", "/api/v1/mailboxes", level_403),
            (
                "GET",
                "/api/v1/mailboxes",
                FakeResponse(
                    200,
                    [
                        _mb("a", "other@loc.cc"),  # 别的域名
                        _mb("b", "arch@smailr.com", is_archived=True),
                        _mb("c", "off@smailr.com", receiveEnabled=False),
                        _mb("d", "ok@smailr.com"),
                    ],
                ),
            ),
        ]
    )
    assert c.create_mailbox("smailr.com", 1) == ["ok@smailr.com"]


def test_reuse_failure_says_what_to_do():
    """没有可复用的邮箱时，报错必须**指向可操作的动作**（去改后台域名设置）。"""
    c, _ = client_with(
        [
            ("POST", "/api/v1/mailboxes", FakeResponse(403, {"error": "等级不足"})),
            ("GET", "/api/v1/mailboxes", FakeResponse(200, [])),
        ]
    )
    assert c.create_mailbox("smailr.com", 1) == []
    assert "没有可复用的邮箱" in c.last_error and "邮箱域名" in c.last_error


def test_scope_403_does_not_trigger_reuse():
    """key 权限 403 **不**走复用 —— 那是配置问题，换 key 才能修。"""
    c, _ = client_with(
        [("POST", "/api/v1/mailboxes", FakeResponse(403, {"error": "api key access not allowed"}))]
    )
    assert c.create_mailbox("smailr.com", 1) == []
    assert "api key access not allowed" in c.last_error


# ── 删邮箱（成功落库后调用；**不可逆**）──────────────────────────
def test_delete_mailbox_resolves_id_then_deletes():
    c, fake = client_with(
        [
            ("GET", "/api/v1/mailboxes", FakeResponse(200, [_mb("mb42", "x@smailr.com")])),
            ("DELETE", "/api/v1/mailboxes/mb42", FakeResponse(204, None)),
        ]
    )
    assert c.delete_mailbox("x@smailr.com") is True
    assert any(call["method"] == "DELETE" for call in fake.calls)


def test_delete_mailbox_is_idempotent():
    """🔴 地址查不到（已经删过 / 不是我们的）也返回 True —— 清理不该因此报错。"""
    c, fake = client_with([("GET", "/api/v1/mailboxes", FakeResponse(200, []))])
    assert c.delete_mailbox("gone@smailr.com") is True
    assert not any(call["method"] == "DELETE" for call in fake.calls)


def test_delete_mailbox_treats_404_as_done():
    c, _ = client_with(
        [
            ("GET", "/api/v1/mailboxes", FakeResponse(200, [_mb("mb1", "x@smailr.com")])),
            ("DELETE", "/api/v1/mailboxes/mb1", FakeResponse(404, {"error": "not found"})),
        ]
    )
    assert c.delete_mailbox("x@smailr.com") is True


def test_delete_mailbox_reports_real_failures():
    c, _ = client_with(
        [
            ("GET", "/api/v1/mailboxes", FakeResponse(200, [_mb("mb1", "x@smailr.com")])),
            ("DELETE", "/api/v1/mailboxes/mb1", FakeResponse(500, {"error": "boom"})),
        ]
    )
    assert c.delete_mailbox("x@smailr.com") is False
    assert c.last_error


# ── 收码 ──────────────────────────────────────────────────────────
def _mail(mid="m1", sender="noreply@agnes-ai.com", **extra):
    """造一封邮件。

    ⚠ 发件人用 `sender=` 传，**不能**用 `from_=` —— `from` 是 Python 关键字，
      所以 `_mail(..., from_="x")` 会**新增一个 `from_` 键**，而默认的 `from`
      仍然是 Agnes ⇒ 发件人过滤不会生效（第一版就这么挂的）。
    """
    return {
        "id": mid,
        "subject": "Your Agnes Platform Verification Code",
        "from": sender,
        **extra,
    }


def test_wait_for_code_lists_then_fetches_detail():
    c, fake = client_with(
        [
            ("GET", "/mails/m1", FakeResponse(200, {"body": {"content": "your code is 445566"}})),
            ("GET", "/mailboxes", FakeResponse(200, [_mb("mb1", "x@smailr.com")])),
            (
                "GET",
                "/mailboxes/mb1/mails",
                FakeResponse(200, [_mail("m1", received_at=1_700_000_000_000)]),
            ),
        ]
    )
    assert c.wait_for_code("x@smailr.com", timeout=5, interval=0.01) == "445566"
    kinds = [call["url"].rsplit("/api/v1/", 1)[-1] for call in fake.calls]
    assert sum(1 for k in kinds if k.startswith("mails/")) == 1, "只该对选中的那封取详情"


def test_wait_for_code_respects_since_ts():
    """🔴 `since_ts` 之后的信才要 —— 否则会拿到上一轮的旧码。"""
    c, fake = client_with(
        [
            ("GET", "/mails/new", FakeResponse(200, {"body": "code 998877"})),
            ("GET", "/api/v1/mailboxes", FakeResponse(200, [_mb("mb1", "x@smailr.com")])),
            (
                "GET",
                "/mailboxes/mb1/mails",
                FakeResponse(
                    200,
                    [
                        _mail("old", received_at=1_700_000_000_000),
                        _mail("new", received_at=1_700_000_500_000),
                    ],
                ),
            ),
        ]
    )
    code = c.wait_for_code("x@smailr.com", timeout=5, interval=0.01, since_ts=1_700_000_100_000)
    assert code == "998877"
    assert all("mails/old" not in call["url"] for call in fake.calls)


def test_wait_for_code_skips_other_senders():
    c, fake = client_with(
        [
            ("GET", "/mails/good", FakeResponse(200, {"body": "code 112233"})),
            ("GET", "/api/v1/mailboxes", FakeResponse(200, [_mb("mb1", "x@smailr.com")])),
            (
                "GET",
                "/mailboxes/mb1/mails",
                FakeResponse(
                    200,
                    [
                        _mail("spam", sender="promo@elsewhere.com", subject="Sale", received_at=1),
                        _mail("good", received_at=2),
                    ],
                ),
            ),
        ]
    )
    assert c.wait_for_code("x@smailr.com", timeout=5, interval=0.01) == "112233"


def test_no_mail_leaves_last_error_empty():
    """🔴 **邮件没到** ⇒ `last_error` 必须为 `""`（`provision` 据此报 `MAIL_TIMEOUT`）。"""
    c, _ = client_with(
        [
            ("GET", "/api/v1/mailboxes", FakeResponse(200, [_mb("mb1", "x@smailr.com")])),
            ("GET", "/mailboxes/mb1/mails", FakeResponse(200, [])),
        ]
    )
    assert c.wait_for_mail("x@smailr.com", timeout=1, interval=0.01) is None
    assert c.last_error == ""


def test_5xx_sets_last_error_but_4xx_raises():
    """5xx = 读不出来（重试到超时）；4xx = **我们**的问题（立刻抛）。"""
    c, _ = client_with(
        [
            ("GET", "/api/v1/mailboxes", FakeResponse(200, [_mb("mb1", "x@smailr.com")])),
            ("GET", "/mailboxes/mb1/mails", FakeResponse(500, {"error": "boom"})),
        ]
    )
    assert c.wait_for_mail("x@smailr.com", timeout=1, interval=0.01) is None
    assert "读不出来" in c.last_error

    c2, _ = client_with(
        [
            ("GET", "/api/v1/mailboxes", FakeResponse(200, [_mb("mb1", "x@smailr.com")])),
            ("GET", "/mailboxes/mb1/mails", FakeResponse(400, {"error": "bad"})),
        ]
    )
    with pytest.raises(SmailrError):
        c2.wait_for_mail("x@smailr.com", timeout=5, interval=0.01)


# ── 清理（落盘之后：success + skipped）──────────────────────────────────────────
def test_cleanup_settles_success_and_skipped_but_keeps_failed(monkeypatch):
    """🔴 只删**已了结**的、且**属于 smailr**的：`success` + `skipped`。

    ⚠ 失败的账号**不能**删它的邮箱：那个账号可能要重跑，而邮箱是它唯一的收信地址。
    ⚠ `skipped` 那一条是**防御性**的（当前所有 skip 路径都在 `create_mailbox`
      之前）—— 但契约要写死：只要一条 `skipped` 带了地址，它就该被清掉，
      而不是静默占着 smailr 的 100 邮箱额度。
    """
    deleted = []

    class FakeClient:
        last_error = ""

        def delete_mailbox(self, email):
            deleted.append(email)
            return True

    monkeypatch.setattr(mailbox, "SmailrClient", lambda: FakeClient())
    records = [
        {
            "status": "success",
            "email": "a@smailr.com",
            "mail_domain": "smailr.com",
            "mailboxes_created": ["a@smailr.com"],
        },
        # 失败：不删
        {
            "status": "failed",
            "email": "b@smailr.com",
            "mail_domain": "smailr.com",
            "mailboxes_created": ["b@smailr.com"],
        },
        # 🔴 跳过：删 —— 邮箱建出来了，但账号没建成
        {
            "status": "skipped",
            "email": "e@smailr.com",
            "mail_domain": "smailr.com",
            "mailboxes_created": ["e@smailr.com"],
        },
        # 跳过但**没建出邮箱**（如“域名窗口装不下两枪”）：没有地址可删
        {"status": "skipped", "email": "", "mail_domain": "", "mailboxes_created": []},
        # 别的源：不删
        {
            "status": "success",
            "email": "c@catchmail.io",
            "mail_domain": "catchmail.io",
            "mailboxes_created": ["c@catchmail.io"],
        },
        {
            "status": "success",
            "email": "d@own.example",
            "mail_domain": "own.example",
            "mailboxes_created": ["d@own.example"],
        },
    ]
    out = mailbox.cleanup_settled(records, log=lambda *_: None)
    assert deleted == ["a@smailr.com", "e@smailr.com"]
    assert out == {"deleted": 2, "failed": 0}


def test_cleanup_deletes_every_created_mailbox_not_just_the_last(monkeypatch):
    """🔴 回归：一条账号**换域名时建过多个邮箱** ⇒ 清理要逐个删。

    只删 `email`（最后一个）会让中间那些**静默**占着 smailr 的 100 邮箱上限 ——
    不可见、不报错，跑几十个就卡死。
    """
    deleted = []

    class FakeClient:
        last_error = ""

        def delete_mailbox(self, email):
            deleted.append(email)
            return True

    monkeypatch.setattr(mailbox, "SmailrClient", lambda: FakeClient())
    records = [
        {
            "status": "success",
            "email": "b@loc.cc",  # `email` 只是最后一个
            "mail_domain": "loc.cc",
            "mailboxes_created": ["a@smailr.com", "b@loc.cc"],
        }
    ]
    out = mailbox.cleanup_settled(records, log=lambda *_: None)
    assert deleted == ["a@smailr.com", "b@loc.cc"]
    assert out == {"deleted": 2, "failed": 0}


def test_cleanup_judges_the_source_per_address_not_by_mail_domain(monkeypatch):
    """🔴 判据是**按地址**判源，不是按 `mail_domain`。

    一条账号可能**跨源轮换**（smailr → catchmail），而 `mail_domain` 只记最后一个
    ⇒ 按它判会把中间那个 smailr 邮箱整个漏掉（而它正是占额度的那一个）。
    """
    deleted = []

    class FakeClient:
        last_error = ""

        def delete_mailbox(self, email):
            deleted.append(email)
            return True

    monkeypatch.setattr(mailbox, "SmailrClient", lambda: FakeClient())
    records = [
        {
            "status": "success",
            "email": "z@mailistry.com",
            "mail_domain": "mailistry.com",  # 最后一个源是 catchmail
            "mailboxes_created": ["a@smailr.com", "z@mailistry.com"],
        }
    ]
    out = mailbox.cleanup_settled(records, log=lambda *_: None)
    assert deleted == ["a@smailr.com"]  # 只有 smailr 那个被删
    assert out == {"deleted": 1, "failed": 0}


def test_cleanup_never_deletes_a_reused_mailbox(monkeypatch):
    """🔴 守卫：**复用的邮箱不能删** —— 删了下一个账号就没得用。

    这条冲突是**真实的**（不是假想）：实测里 `create_mailbox` 已经会走复用路径
    （等级 403 → 领现成的）。若还删它，第一个账号成功之后池子就空了。

    ⚠ 机制上它天然安全：复用的地址**不会进 `mailboxes_created`**（`provision`
      就不往里写）⇒ 清理看不到它。所以这里**不能**再用 `mail_reused` 当判据 ——
      一条账号完全可以“先新建一个、后来复用了一个”，而那个新建的**该删**。
    """
    deleted = []

    class FakeClient:
        last_error = ""

        def delete_mailbox(self, email):
            deleted.append(email)
            return True

    monkeypatch.setattr(mailbox, "SmailrClient", lambda: FakeClient())
    records = [
        # 新建的 → 删（释放那 100 个邮箱额度）
        {
            "status": "success",
            "email": "new@smailr.com",
            "mail_domain": "smailr.com",
            "mail_reused": False,
            "mailboxes_created": ["new@smailr.com"],
        },
        # 复用的 → **不删**（那是用户预先建好的池子）；它不在 `mailboxes_created` 里
        {
            "status": "success",
            "email": "pool@smailr.com",
            "mail_domain": "smailr.com",
            "mail_reused": True,
            "mailboxes_created": [],
        },
    ]
    out = mailbox.cleanup_settled(records, log=lambda *_: None)
    assert deleted == ["new@smailr.com"]
    assert out == {"deleted": 1, "failed": 0}


def test_cleanup_reports_failures_without_raising(monkeypatch):
    class FakeClient:
        last_error = "boom"

        def delete_mailbox(self, email):
            return False

    monkeypatch.setattr(mailbox, "SmailrClient", lambda: FakeClient())
    out = mailbox.cleanup_settled(
        [
            {
                "status": "success",
                "email": "a@smailr.com",
                "mail_domain": "smailr.com",
                "mailboxes_created": ["a@smailr.com"],
            }
        ],
        log=lambda *_: None,
    )
    assert out == {"deleted": 0, "failed": 1}


def test_cleanup_is_a_noop_without_smailr_records(monkeypatch):
    def boom():
        raise AssertionError("不该建客户端")

    monkeypatch.setattr(mailbox, "SmailrClient", boom)
    assert mailbox.cleanup_settled([], log=lambda *_: None) == {"deleted": 0, "failed": 0}
