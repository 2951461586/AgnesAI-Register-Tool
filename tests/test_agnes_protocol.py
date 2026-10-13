"""Agnes 协议客户端的离线用例（零网络）。

钉住三件事：
  1. **那条按 IP 的注册限流必须被归成 `quota`**（它是 HTTP 400 + 特定文案，
     不是 429）—— 归错会让调用方去改密码，而不是换出口；
  2. 请求体 / 请求头形状（字段名写错会在**线上**才炸，而那时已经烧了窗口）；
  3. `access_token_of` 的取值口径（登录与注册的响应形状实测相同）。
"""

import json

import pytest

from src import agnes as A


class FakeResponse:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body or {})
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """记录每次请求，按队列返回预置响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.proxies = {}
        self.trust_env = True

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        if not self.responses:
            raise AssertionError("没有预置响应了")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client_with(responses):
    """造一个装了假 session 的客户端，返回 `(client, fake_session)`。"""
    c = A.AgnesClient(base="https://example.invalid", console="https://console.invalid")
    fake = FakeSession(responses)
    c.session = fake  # type: ignore[assignment]  # 测试替身，只用到 request()
    return c, fake


def test_ip_rate_limit_400_is_quota():
    """🔴 实测：限流是 **400 + 文案**，不是 429。"""
    c, _ = client_with(
        [
            FakeResponse(
                400,
                {
                    "code": 400,
                    "message": "Too many registration attempts from this IP. Please try again later",
                },
            )
        ]
    )
    with pytest.raises(A.AgnesQuota) as ei:
        c.send_code("a@b.c")
    assert ei.value.kind == A.ERR_QUOTA
    assert ei.value.status == 400


def test_http_429_is_quota():
    c, _ = client_with([FakeResponse(429, {"code": 429, "message": "slow down"})])
    with pytest.raises(A.AgnesQuota):
        c.send_code("a@b.c")


def test_already_registered_is_rejected():
    c, _ = client_with(
        [FakeResponse(400, {"code": 400, "message": "This email is already registered"})]
    )
    with pytest.raises(A.AgnesRejected) as ei:
        c.register("a@b.c", "Lz#abcdefgh", "123456")
    assert ei.value.kind == A.ERR_REJECTED


def test_weak_password_is_rejected():
    c, _ = client_with(
        [FakeResponse(400, {"code": 400, "message": "Password must be at least 8 characters"})]
    )
    with pytest.raises(A.AgnesRejected):
        c.register("a@b.c", "Ab1", "123456")


def test_5xx_is_network():
    c, _ = client_with([FakeResponse(500, {"code": 500, "message": "boom"})])
    with pytest.raises(A.AgnesNetwork):
        c.send_code("a@b.c")


def test_send_code_request_shape():
    c, fake = client_with([FakeResponse(200, {"code": 200, "message": "ok"})])
    c.send_code("a@b.c")
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/api/verification")
    assert call["params"] == {"email": "a@b.c", "purpose": "register"}
    assert call["headers"]["x-user-language"] == "zh-CN"


def test_register_request_shape():
    c, fake = client_with([FakeResponse(200, {"code": 200, "data": {"access_token": "t"}})])
    c.register("a@b.c", "Lz#abcdefgh", "123456")
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/api/user/register")
    assert call["json"] == {
        "email": "a@b.c",
        "password": "Lz#abcdefgh",
        "password_confirm": "Lz#abcdefgh",
        "code": "123456",
    }


def test_login_request_shape_and_token():
    c, fake = client_with([FakeResponse(200, {"code": 200, "data": {"access_token": "jwt-1"}})])
    body = c.login("a@b.c", "Lz#abcdefgh")
    assert A.access_token_of(body) == "jwt-1"
    call = fake.calls[0]
    assert call["json"] == {"username": "a@b.c", "password": "Lz#abcdefgh"}
    assert c.jwt == "jwt-1"


def test_create_key_uses_bearer_and_reads_data_key():
    c, fake = client_with(
        [
            FakeResponse(
                200, {"code": 200, "data": {"key": "sk-abc", "id": 7, "key_preview": "sk-a…c"}}
            )
        ]
    )
    c.jwt = "jwt-1"
    data = c.create_key("auto")
    assert data["key"] == "sk-abc"
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer jwt-1"
    assert fake.calls[0]["json"] == {"name": "auto"}


def test_access_token_of_tolerates_shapes():
    assert A.access_token_of({"data": {"access_token": "x"}}) == "x"
    assert A.access_token_of({"data": {"token": "y"}}) == "y"
    assert A.access_token_of({"access_token": "z"}) == "z"
    assert A.access_token_of({"data": {}}) == ""
    assert A.access_token_of(None) == ""


def test_short_reports_empty_body():
    r = FakeResponse(500, None, text="")
    assert "空响应体" in A._short(None, r)


def test_write_endpoints_do_not_auto_retry():
    """🔴 写请求**不自动重试** —— 重试会多消耗一张服务端窗口。

    这里预置一个响应，若实现里偷偷重试，第二次响应会缺货并抛断言。
    """
    c, fake = client_with([FakeResponse(500, {"code": 500})])
    with pytest.raises(A.AgnesNetwork):
        c.send_code("a@b.c")
    assert len(fake.calls) == 1
