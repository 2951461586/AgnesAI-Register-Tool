"""`catchmail` 客户端的离线用例（零网络）。

为什么值得测
============
它是本项目的**第二个邮箱源**，而 `provision` 对邮箱源只有一套鸭子类型契约
（`create_mailbox` / `wait_for_code` / `last_error` / `last_polls` /
`last_http_errors`）。所以这里钉住的是**它与 CF Worker 源语义一致**的那部分 ——
不一致的形态是"用 catchmail 时 `provision` 的分流判据失效"，而那不会报错。

🔴 最要紧的一条：`last_error` 的**两种含义**
  · `""`        = **邮件没到**（该继续等）
  · 非空        = **读不出来**（该修）
`provision` 靠它分流（`mail.last_error` → `MAIL_READ` vs `MAIL_TIMEOUT`），
两个源给出不同语义会让那个分流变成碰运气。
"""

from datetime import datetime
from typing import cast

import pytest
import requests

from src.catchmail import CatchMailClient, _parse_ms


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            # ⚠ 必须把 `response` 挂上：`wait_for_mail` 靠 `ex.response.status_code`
            #   分流 4xx / 5xx。而 `FakeResponse` 不是真的 `Response` ⇒ 用 `cast`
            #   明确表示"这是测试替身"（同 `tests/test_agnes_protocol.py` 的做法）。
            raise requests.HTTPError(
                f"HTTP {self.status_code}", response=cast("requests.Response", self)
            )


class FakeSession:
    """按 URL 片段匹配响应；记录每次调用，便于断言"只取了该取的那封"。"""

    def __init__(self, routes):
        self.routes = routes  # [(url 子串, FakeResponse 或 异常 或 callable), ...]
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}})
        for frag, resp in self.routes:
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                if callable(resp):
                    return resp(params or {})
                return resp
        raise AssertionError(f"没有为 {url} 预置响应")


def client_with(routes):
    """造一个装了假 session 的客户端，返回 `(client, fake_session)`。

    ⚠ 返回的是**假 session 本身**（而不是 `c.session`）—— 后者的静态类型是
      `requests.Session`，于是 `fake.calls` 会被判为未知属性（第一版就这么挂的）。
    """
    c = CatchMailClient(domains=["catchmail.io"], timeout=5)
    fake = FakeSession(routes)
    c.session = fake  # type: ignore[assignment]  # 测试替身，只用到 get()
    return c, fake


def _msg(
    mid="m1",
    sender="Agnes AI <noreply@agnes-ai.com>",
    subject="Your Code",
    date="2026-10-14T08:00:00+00:00",
):
    return {
        "id": mid,
        "mailbox": "x@catchmail.io",
        "from": sender,
        "subject": subject,
        "date": date,
        "size": 1,
    }


# ── 时间戳（`since_ts` 的防"读到旧码"全靠它）─────────────────────
def test_parse_ms_is_timezone_aware():
    """🔴 同一个时刻的不同时区写法必须得到**同一个**毫秒值。

    写错方向的后果：`since_ts` 过滤失效 ⇒ 拿到**上一轮的旧验证码** ⇒
    服务端报"验证码错误"，而真相是我们在用旧信（修法完全不同）。
    """
    utc = _parse_ms("2026-10-14T08:00:00+00:00")
    plus8 = _parse_ms("2026-10-14T16:00:00+08:00")
    z = _parse_ms("2026-10-14T08:00:00Z")
    assert utc == plus8 == z > 0


def test_parse_ms_matches_stdlib():
    iso = "2026-10-14T08:37:21.491000+00:00"
    assert _parse_ms(iso) == int(datetime.fromisoformat(iso).timestamp() * 1000)


def test_parse_ms_tolerates_garbage():
    """坏值退回 0（= 不过滤）—— 安全方向的降级：宁可多读一封。"""
    for bad in ("", None, "yesterday", 123, [], "2026-13-45T99:99:99"):
        assert _parse_ms(bad) == 0


# ── 建地址 ────────────────────────────────────────────────────────
def test_create_mailbox_uses_the_requested_domain():
    """🔴 地址是**我们自己选**的（与 CF Worker 源相反）—— 本地部分随机 + 指定域名。"""
    c, fake = client_with([("/mailbox", FakeResponse(200, {"messages": []}))])
    out = c.create_mailbox("mailistry.com", 2)
    assert len(out) == 2
    assert all(a.endswith("@mailistry.com") for a in out)
    assert len({a.split("@")[0] for a in out}) == 2, "两个地址的本地部分必须不同"
    assert len(fake.calls) == 2
    assert all(call["params"]["address"].endswith("@mailistry.com") for call in fake.calls)


def test_create_mailbox_returns_empty_and_records_error_on_failure():
    """建址失败**不抛**（与 `TempMailClient` 一致），但要把原因记进 `last_error`。

    ⚠ 记原因是为了让 `provision` 报出真实原因，而不是笼统的"没返回有效地址"。
    """
    c, _ = client_with([("/mailbox", requests.ConnectionError("boom"))])
    assert c.create_mailbox("catchmail.io", 1) == []
    assert "catchmail 建址失败" in c.last_error


# ── 读信 ──────────────────────────────────────────────────────────
def test_list_mails_maps_metadata_without_body():
    """列表**只有元数据**（正文要另取详情）—— 这是"每轮只多一次请求"的前提。"""
    c, _ = client_with([("/mailbox", FakeResponse(200, {"messages": [_msg()]}))])
    mails = c.list_mails(email="x@catchmail.io")
    assert len(mails) == 1
    m = mails[0]
    assert m.id == "m1"
    assert m.from_address.startswith("Agnes AI")
    assert m.subject == "Your Code"
    assert m.received_at > 0
    assert m.body == "", "列表阶段不该有正文（正文是详情接口给的）"


def test_wait_for_code_fetches_detail_only_for_the_selected_message():
    """🔴 只对**选中**的那封取详情 —— 否则每轮轮询会对每封信各打一次请求。"""
    detail = {"id": "m1", "subject": "Your Code", "body": {"text": "your code is 123456"}}
    c, fake = client_with(
        [
            ("/message/m1", FakeResponse(200, detail)),
            ("/mailbox", FakeResponse(200, {"messages": [_msg()]})),
        ]
    )
    assert c.wait_for_code("x@catchmail.io", timeout=5, interval=0.01) == "123456"
    kinds = [c["url"].rsplit("/api/v1/", 1)[-1].split("/")[0] for c in fake.calls]
    assert kinds.count("message") == 1
    assert kinds.count("mailbox") == 1


def test_wait_for_code_respects_since_ts():
    """🔴 `since_ts` 之后的信才要 —— 否则会拿到**上一轮的旧验证码**。"""
    old = _msg(mid="old", date="2026-10-14T07:00:00+00:00")
    new = _msg(mid="new", date="2026-10-14T09:00:00+00:00")
    c, fake = client_with(
        [
            ("/message/new", FakeResponse(200, {"body": {"text": "code 654321"}})),
            ("/mailbox", FakeResponse(200, {"messages": [old, new]})),
        ]
    )
    code = c.wait_for_code(
        "x@catchmail.io", timeout=5, interval=0.01, since_ts=_parse_ms("2026-10-14T08:00:00+00:00")
    )
    assert code == "654321"
    # 只对**新的**那封取过详情
    assert all("old" not in call["url"] for call in fake.calls)


def test_wait_for_code_skips_mail_from_other_senders():
    """收件箱里可能先到一封无关的信 ⇒ 不能拿到第一封就返回。"""
    spam = _msg(
        mid="spam", sender="promo@elsewhere.com", subject="Sale", date="2026-10-14T09:00:00+00:00"
    )
    good = _msg(mid="good", date="2026-10-14T09:00:01+00:00")
    c, fake = client_with(
        [
            ("/message/good", FakeResponse(200, {"body": {"text": "code 111222"}})),
            ("/mailbox", FakeResponse(200, {"messages": [spam, good]})),
        ]
    )
    assert c.wait_for_code("x@catchmail.io", timeout=5, interval=0.01) == "111222"
    assert all("spam" not in call["url"] for call in fake.calls)


# ── 失败语义（与 `provision` 的分流判据绑死）──────────────────────
def test_no_mail_leaves_last_error_empty():
    """🔴 **邮件没到** ⇒ `last_error` 必须为 `""`（`provision` 据此报 `MAIL_TIMEOUT`）。"""
    c, _ = client_with([("/mailbox", FakeResponse(200, {"messages": []}))])
    assert c.wait_for_mail("x@catchmail.io", timeout=1, interval=0.01) is None
    assert c.last_error == ""


def test_read_failure_sets_last_error():
    """🔴 **读不出来** ⇒ `last_error` 非空（`provision` 据此报 `MAIL_READ`）。"""
    c, _ = client_with([("/mailbox", requests.ConnectionError("boom"))])
    assert c.wait_for_mail("x@catchmail.io", timeout=1, interval=0.01) is None
    assert "读不出来" in c.last_error


def test_4xx_is_not_swallowed_as_no_mail():
    """4xx 是**我们**的问题（地址不对 / 被拒）⇒ 必须抛，不能伪装成"邮件没到"。"""
    c, _ = client_with([("/mailbox", FakeResponse(400, {}))])
    with pytest.raises(requests.HTTPError):
        c.wait_for_mail("x@catchmail.io", timeout=5, interval=0.01)


def test_poll_counters_are_tracked():
    """`last_polls` / `last_http_errors` 是 `provision` 写进台账的字段。"""
    c, _ = client_with([("/mailbox", requests.ConnectionError("boom"))])
    c.wait_for_mail("x@catchmail.io", timeout=1, interval=0.01)
    assert c.last_polls >= 1
    assert c.last_http_errors >= 1
