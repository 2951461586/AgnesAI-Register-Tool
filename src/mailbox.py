"""邮箱源注册表：把"域名 → 用哪个邮箱客户端"这条映射收在**一处**。

为什么需要这一层
================
`provision` 需要的是"给我**这个域名**的邮箱客户端"。而域名现在来自**多个源**
（自有 CF Worker / catchmail），两者的协议完全不同（一个要 `X-Admin-Token`、
一个要 `Referer`/`Origin`）⇒ 必须有个地方决定用哪个。

🔴 **`MailboxSource` 是结构性契约（`typing.Protocol`），不是基类。**
   `provision` 本来就是鸭子类型（用例传的是 `FakeMail()`），这里只是把既有事实
   写成**可检查**的接口，不引入继承关系 —— 引入基类会让现有替身全部要改。
   ⚠ 同理 **`issubclass()` 不能用**：本 Protocol 含数据成员（`last_error` 等），
   `issubclass` 会直接抛 `TypeError: Protocols with non-method members ...`。
   要检查只能用 `isinstance`（需 `@runtime_checkable`）。

🔴 **"域名决定源"只有一份实现**（`config.source_of`）。这里只做派发，
   不自己再判一次"这个域名是不是 catchmail 的" —— 两处判必然漂移，而漂移的
   形态是"拿 Worker 的客户端去 catchmail 的域名上建地址"，报错会指向一个
   不存在的问题。

⚠ 未登记的域名**回落到 `worker`**（而不是抛错）：这样 `--mail-domain` 传一个
  临时域名仍然能试（Worker 会以 4xx 明确拒绝，比这里提前拦下更有信息量）。
"""

from typing import Protocol, runtime_checkable

from common import config

from .catchmail import CatchMailClient
from .tempmail import TempMailClient


@runtime_checkable
class MailboxSource(Protocol):
    """邮箱源的结构性契约 —— `provision` 对邮箱的全部要求。

    ⚠ 这些方法/属性**不是**我发明的接口，而是 `provision` **本来就在调**的：
      把既有事实写下来，才能让"新增一个源"变成一件有判据的事
      （而不是"跑一遍看炸不炸"）。
    """

    #: "" = 邮件没到（该继续等）；非空 = **读不出来**（该修）。两者修法完全不同。
    last_error: str
    #: 打了多少次收信接口（含 5xx 重试）—— 判断"我们是不是把收信侧打满的元凶"。
    last_polls: int
    #: 读失败了几次。
    last_http_errors: int

    def create_mailbox(self, domain: str | None = None, count: int = 1) -> list[str]:
        """建 `count` 个地址。**返回列表可能短于 `count`**（调用方必须自己查）。"""
        ...

    def wait_for_code(self, address: str, length: int = 6, **kw) -> str | None:
        """等一封含验证码的信。**必须支持 `since_ts`**（只看这之后到的信）。"""
        ...


def for_domain(domain: str | None) -> MailboxSource:
    """按域名给出对应的邮箱客户端。

    🔴 每次调用都**新建**一个实例：底层 `requests.Session` **不是线程安全的**，
      而生产者是并发的。所以"一个账号一条链一个客户端"这条约定必须在这里守住
      （与 `Ticket` 里 `client` 的顺序交接同理）。
    """
    if config.source_of(domain or "") == "catchmail":
        return CatchMailClient()
    return TempMailClient()
