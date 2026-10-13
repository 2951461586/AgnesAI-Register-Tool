"""CF Worker 邮箱客户端的离线用例（零网络）。

钉住验证码抽取的两条路（结构化优先 + 正文兜底）与 `since_ts` 过滤 ——
后者是"补跑时拿到上一轮旧验证码"那个坑的唯一防线。
"""

from src import tempmail as T


def test_codes_prefers_worker_structured_result():
    m = T.Mail(
        id="1",
        to_address="a@b.c",
        from_address="noreply@agnes-ai.com",
        subject="Your Agnes Platform Verification Code",
        body="your code is 999999",
        extracted_json='[{"value":"123456","type":"code"}]',
        received_at=0,
    )
    assert m.codes[0] == "123456"
    assert m.find_code() == "123456"


def test_codes_fall_back_to_body_when_extraction_empty():
    """历史行（规则落库之前）里 `extracted_json` 是 `[]`，必须能兜底。"""
    m = T.Mail(
        id="2",
        to_address="a@b.c",
        from_address="noreply@agnes-ai.com",
        subject="Your Agnes Platform Verification Code",
        body="please enter the verification code to confirm your email address:\n\n763388\n",
        extracted_json="[]",
        received_at=0,
    )
    assert m.find_code() == "763388"


def test_extract_code_skips_css_gray_colors():
    """`#ffffff` 这类色值不能被当成验证码。"""
    assert T.extract_code("color:#ffffff; code: 424242") == "424242"
    assert T.extract_code("color:#ffffff;") is None


def test_extract_code_ignores_longer_digit_runs():
    assert T.extract_code("boundary1234567890 end") is None
    assert T.extract_code("id 1234567") is None


def test_codes_dedupes_across_paths():
    m = T.Mail(
        id="3",
        to_address="a@b.c",
        from_address="noreply@agnes-ai.com",
        subject="code",
        body="763388",
        extracted_json='[{"value":"763388","type":"code"}]',
        received_at=0,
    )
    assert m.codes == ["763388"]


def test_wait_for_code_respects_since_ts():
    """早于 `since_ts` 的旧邮件必须被跳过（否则补跑会拿到上一轮的码）。"""

    class FakeMail(T.TempMailClient):
        def __init__(self, mails):
            super().__init__(base="https://x.invalid", token="t")
            self._mails = mails

        def list_mails(self, limit=None, email=None):
            return list(self._mails)

    old = T.Mail(
        id="old",
        to_address="a@b.c",
        from_address="noreply@agnes-ai.com",
        subject="Your Agnes Platform Verification Code",
        body="314159",
        extracted_json="[]",
        received_at=1000,
    )
    new = T.Mail(
        id="new",
        to_address="a@b.c",
        from_address="noreply@agnes-ai.com",
        subject="Your Agnes Platform Verification Code",
        body="271828",
        extracted_json="[]",
        received_at=5000,
    )
    c = FakeMail([old, new])
    assert c.wait_for_code("a@b.c", since_ts=4000, timeout=2, interval=0.01) == "271828"
    # 只看旧邮件时，`since_ts` 之后没有信 → 超时返回 None
    c2 = FakeMail([old])
    assert c2.wait_for_code("a@b.c", since_ts=4000, timeout=1, interval=0.01) is None


def test_wait_for_code_skips_mailless_then_takes_next():
    class FakeMail(T.TempMailClient):
        def __init__(self, mails):
            super().__init__(base="https://x.invalid", token="t")
            self._mails = mails

        def list_mails(self, limit=None, email=None):
            return list(self._mails)

    junk = T.Mail(
        id="junk",
        to_address="a@b.c",
        from_address="news@agnes-ai.com",
        subject="Agnes newsletter",
        body="no digits here",
        extracted_json="[]",
        received_at=2000,
    )
    good = T.Mail(
        id="good",
        to_address="a@b.c",
        from_address="noreply@agnes-ai.com",
        subject="Your Agnes Platform Verification Code",
        body="654321",
        extracted_json="[]",
        received_at=3000,
    )
    c = FakeMail([junk, good])
    assert c.wait_for_code("a@b.c", timeout=2, interval=0.01) == "654321"


def test_find_link_still_works():
    m = T.Mail(
        id="4",
        to_address="a@b.c",
        from_address="noreply@agnes-ai.com",
        subject="verify",
        body="click https://platform.agnes-ai.com/verify?token=abc.",
        extracted_json="[]",
        received_at=0,
        body_links=T.extract_links("click https://platform.agnes-ai.com/verify?token=abc."),
    )
    assert m.find_link("verify") == "https://platform.agnes-ai.com/verify?token=abc"
