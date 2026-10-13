"""`create_mailbox` 的重试契约（离线，零网络）。

🔴 回归：10 批实测里 **8 次失败是 `ConnectionResetError 10054` /
   `RemoteDisconnected`** —— 5 个生产者并发打 Worker 时 Cloudflare 会掐连接。
   原先 `create_mailbox` **一次不成就判死**，整条账号链跟着失败。
   而收信是**直连**（不走代理），换出口没用，只能重试。

同时钉住**不该重试**的两种：4xx（凭据 / 域名是我们的问题）与
`ok:false`（Worker 明确拒绝）—— 重试它们只会把"配置错了"伪装成"偶发失败"。

⚠ 替身**继承** `requests.Response` / `requests.Session` 而不是鸭子类型：
   `requests.HTTPError(response=...)` 要求真的是 `Response`，类型检查也才成立。
⚠ `_client()` 把替身**单独返回**，而不是让测试从 `c.session` 上读 `calls`：
   `c.session` 的声明类型是 `requests.Session`，读不到子类属性。
"""

import pytest
import requests

from src import tempmail as T


class _Resp(requests.Response):
    """最小响应替身：只实现被测代码真正碰的三个成员。"""

    def __init__(self, status=200, payload=None):
        super().__init__()
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self, **kw):
        if self._payload is None:
            # Cloudflare `Error 1101` 回的是 HTML 页 —— `json()` 在这里炸
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _Session(requests.Session):
    """按脚本依次返回响应 / 抛异常，并记录调用次数。"""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.calls = 0

    def post(self, *a, **kw):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(monkeypatch, script, retries=2):
    """返回 `(客户端, 替身 session)` —— 断言 `calls` 请用第二个。"""
    monkeypatch.setattr(T.config, "MAIL_RETRIES", retries)
    monkeypatch.setattr(T.config, "MAIL_POLL_INTERVAL", 0.0)  # 退避不真的睡
    c = T.TempMailClient.__new__(T.TempMailClient)  # 绕开 __init__（不碰 .env）
    c.base = "https://worker.test"
    c.token = "t"
    c.timeout = 5.0
    sess = _Session(script)
    c.session = sess
    return c, sess


def test_retries_connection_reset_then_succeeds(monkeypatch):
    c, sess = _client(
        monkeypatch,
        [
            requests.ConnectionError("Connection aborted: 10054"),
            requests.ConnectionError("RemoteDisconnected"),
            _Resp(200, {"ok": True, "emails": ["a@x.com"]}),
        ],
    )
    assert c.create_mailbox("x.com", 1) == ["a@x.com"]
    assert sess.calls == 3


def test_retries_5xx_then_succeeds(monkeypatch):
    c, sess = _client(monkeypatch, [_Resp(502), _Resp(200, {"ok": True, "emails": ["b@x.com"]})])
    assert c.create_mailbox() == ["b@x.com"]
    assert sess.calls == 2


def test_retries_non_json_error_page(monkeypatch):
    c, sess = _client(
        monkeypatch, [_Resp(200, None), _Resp(200, {"ok": True, "emails": ["c@x.com"]})]
    )
    assert c.create_mailbox() == ["c@x.com"]
    assert sess.calls == 2


def test_gives_up_after_retries(monkeypatch):
    err = requests.ConnectionError("10054")
    c, sess = _client(monkeypatch, [err, err, err], retries=2)
    with pytest.raises(RuntimeError, match="连续失败 3 次"):
        c.create_mailbox()
    assert sess.calls == 3


def test_4xx_is_not_retried(monkeypatch):
    c, sess = _client(monkeypatch, [_Resp(401)])
    with pytest.raises(requests.HTTPError):
        c.create_mailbox()
    assert sess.calls == 1


def test_ok_false_is_not_retried(monkeypatch):
    c, sess = _client(monkeypatch, [_Resp(200, {"ok": False, "error": "bad domain"})])
    with pytest.raises(RuntimeError, match="create_mailbox failed"):
        c.create_mailbox()
    assert sess.calls == 1
