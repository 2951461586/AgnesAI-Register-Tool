"""`mailbox` 注册表的离线用例（零网络）。

为什么值得测
============
"**域名决定源**"是本项目新增的一条规则，而它有两个容易写错的方向：

  · 派发错了 ⇒ 拿 Worker 的客户端去 catchmail 的域名上建地址。报错会指向
    "建址失败 / 没返回地址"，**与真实原因（用错客户端）无关**；
  · 两处各判一次 ⇒ 一处改了另一处没改，而漂移的形态正是上面那种错。

所以这里钉住：① 派发正确；② 两个源都满足 `MailboxSource` 契约
（新增源的"检查清单"）；③ 未登记的域名有**确定的**回退行为，而不是抛。
"""

from common import config
from src import mailbox
from src.catchmail import CatchMailClient
from src.mailbox import MailboxSource
from src.tempmail import TempMailClient

# ⚠ `conftest.py` 的 autouse fixture 会把 `config.mail_domains` 换成一个**固定值**
#   （P4 的宿主隔离：用例不能依赖本机的 `.env`）。而本文件里有一条用例要验的
#   **正是**那个函数的逻辑 ⇒ 在**模块加载时**抓住真实现（那时 fixture 还没跑）。
_real_mail_domains = config.mail_domains


def test_dispatch_by_domain():
    """catchmail 的域名 → CatchMailClient；其余 → TempMailClient。"""
    for d in ("catchmail.io", "mailistry.com", "zeppost.com"):
        assert isinstance(mailbox.for_domain(d), CatchMailClient), d
    for d in ("own.example", "sub.own.example", "own3.example"):
        assert isinstance(mailbox.for_domain(d), TempMailClient), d


def test_dispatch_is_case_insensitive():
    """域名大小写不该改变派发（`source_of` 做了 lower）。"""
    assert isinstance(mailbox.for_domain("CatchMail.IO"), CatchMailClient)


def test_unregistered_domain_falls_back_to_worker():
    """🔴 未登记的域名**回落到 worker**，而不是抛。

    这样 `--mail-domain <随便一个域名>` 这条排查路径仍然能走：Worker 会以
    明确的 4xx 拒掉，比在这里提前拦下**信息量更大**。
    """
    assert isinstance(mailbox.for_domain("something.example"), TempMailClient)
    assert isinstance(mailbox.for_domain(None), TempMailClient)
    assert isinstance(mailbox.for_domain(""), TempMailClient)


def test_both_sources_satisfy_the_contract():
    """🔴 新增邮箱源的**检查清单**：满足 `MailboxSource` 契约。

    ⚠ 只能用 `isinstance`：本 Protocol 含数据成员（`last_error` 等），
      `issubclass()` 会直接抛 `TypeError: Protocols with non-method members ...`
      （Intern 那边实测过）。这条限制本身也值得钉住 —— 否则下一个人会踩。
    """
    for src in (TempMailClient(), CatchMailClient()):
        assert isinstance(src, MailboxSource), type(src).__name__
        # 数据成员：`provision` 会读它们（`last_error` 的空/非空是分流判据）
        assert src.last_error == ""
        assert src.last_polls == 0
        assert src.last_http_errors == 0
        for name in ("create_mailbox", "wait_for_code"):
            assert callable(getattr(src, name)), name


def test_issubclass_raises_for_data_member_protocol():
    """把上面那条限制写成**可执行的**事实，免得后人以为可以用 `issubclass`。

    ⚠ 这里**必须** `# type: ignore`：pyright 会在**类型检查阶段**就拒绝
      `issubclass()` 于含数据成员的 Protocol（正是本用例要断言的运行时行为）。
      换句话说，这个 ignore 不是绕过检查 —— **那个拒绝本身就是被断言的事实**。
    """
    import pytest

    with pytest.raises(TypeError):
        issubclass(TempMailClient, MailboxSource)  # type: ignore


def test_dispatch_uses_config_as_the_single_source_of_truth():
    """🔴 `mailbox` **不自己判**"是不是 catchmail 的域名"，而是问 `config.source_of`。

    两处各判一次必然漂移 —— 所以这里把"它们指向同一个集合"钉住：
    改了 `AR_CATCHMAIL_DOMAINS`（覆盖），派发必须跟着变。
    """
    assert mailbox.for_domain is not None
    for d in config.CATCHMAIL_DOMAINS:
        assert config.source_of(d) == "catchmail"
        assert isinstance(mailbox.for_domain(d), CatchMailClient), d
    # 反向：非 catchmail 域名一律 worker
    assert config.source_of("own.example") == "worker"


def test_enabled_sources_gate_the_domain_pool(monkeypatch):
    """🔴 默认只有 `worker` ⇒ 域名池里**不含** catchmail 的域名。

    这条是"换源必须是显式动作"的可执行版本：不开 `AR_MAIL_SOURCES=catchmail`，
    域名池就不该多出 3 个域名（否则"我只改了 X"会顺手把 Y 也改了）。

    ⚠ 用 `_real_mail_domains`（模块加载时抓的真实现），因为 fixture 会把
      `config.mail_domains` 换成固定值来隔离宿主 `.env`。
    """
    monkeypatch.setattr(config, "MAIL_SOURCES", ["worker"])
    assert all(d not in config.CATCHMAIL_DOMAINS for d in _real_mail_domains())
    monkeypatch.setattr(config, "MAIL_SOURCES", ["worker", "catchmail"])
    for d in config.CATCHMAIL_DOMAINS:
        assert d in _real_mail_domains()


def test_catchmail_source_needs_no_credentials(monkeypatch):
    """只开 catchmail 时**不该**要求 Worker 的三项凭据（它免鉴权）。

    ⚠ 无条件要求它们会让人以为"必须填"，而那是假话 —— 而假话会让用户
      去配一堆用不上的东西。
    """
    monkeypatch.setattr(config, "MAIL_SOURCES", ["catchmail"])
    bad = [x for x in config.validate() if x.startswith("AR_WORKER_")]
    assert bad == [], bad
