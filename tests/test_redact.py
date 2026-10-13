"""脱敏助手的契约用例。

🔴 这些用例钉的是**输出边界的安全性质**，不是实现细节：
   * 代理串里的 `user:pass` 不能出现在输出里；
   * 凭据串默认**连前缀都不给**（前缀也足以在别处做关联）；
   * 邮箱只保留域名（域名是"用了哪个邮箱源"这个事实，本地部分才是账号标识）。

它们是"公开仓库 + 终端截图"这条边界上的最后一道闸 —— 改 `src/redact.py`
之前先看这里，改完之后要能解释每一条为什么仍然成立。
"""

from src import redact as R

# ── redact_url：抹掉 userinfo，保留 host ───────────────────────────


def test_redact_url_strips_userinfo():
    assert R.redact_url("http://user:pass@203.0.113.30:8080") == "http://***@203.0.113.30:8080"


def test_redact_url_keeps_url_without_userinfo():
    # 没有账密的 URL（槽位代理常见形态）原样返回，不画蛇添足
    assert R.redact_url("http://127.0.0.1:7901") == "http://127.0.0.1:7901"


def test_redact_url_keeps_scheme_and_port():
    # socks5h 也要认；只换 userinfo，别把 scheme/host/port 一起吞掉
    assert R.redact_url("socks5h://u:p@proxy.example:1080") == "socks5h://***@proxy.example:1080"


def test_redact_url_handles_empty_and_non_url():
    assert R.redact_url("") == ""
    # 不是 URL（没有 `://`）时不猜，原样返回
    assert R.redact_url("203.0.113.30:8080") == "203.0.113.30:8080"


# ── redact：默认把整个串藏起来 ────────────────────────────────────


def test_redact_hides_everything_by_default():
    out = R.redact("sk-abcdef1234567890")
    assert out == "<19 chars>"
    assert "sk-" not in out  # 默认 keep=0：连前缀都不打


def test_redact_keep_prefix_is_opt_in_and_bounded():
    assert R.redact("sk-abcdef1234567890", keep=6) == "sk-abc…<19 chars>"
    # keep 大于串长时不越界，仍返回全量掩码
    assert R.redact("abc", keep=6) == "<3 chars>"


def test_redact_empty():
    assert R.redact("") == ""


# ── redact_email：保留域名，打码本地部分 ──────────────────────────


def test_redact_email_keeps_domain_only():
    assert R.redact_email("oai-1234567890abcdef@own.example") == "oai…@own.example"


def test_redact_email_short_local_part():
    # 本地部分不足 3 位时按实际长度取，不补位
    assert R.redact_email("ab@x.com") == "ab…@x.com"


def test_redact_email_handles_input_without_at():
    assert R.redact_email("not-an-email") == "not-an-email"
    assert R.redact_email("") == ""
