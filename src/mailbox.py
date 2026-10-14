"""邮箱源注册表：把"域名 → 用哪个邮箱客户端"这条映射收在**一处**。

为什么需要这一层
================
`provision` 需要的是"给我**这个域名**的邮箱客户端"。而域名现在来自**多个源**
（自有 CF Worker / catchmail / smailr），三者的协议完全不同（一个要
`X-Admin-Token`、一个要 `Referer`/`Origin`、一个要 `Bearer nm_...`）
⇒ 必须有个地方决定用哪个。

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
from .smailr import SmailrClient
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
    kind = config.source_of(domain or "")
    if kind == "catchmail":
        return CatchMailClient()
    if kind == "smailr":
        return SmailrClient()
    return TempMailClient()


def cleanup_successful(records, *, log=print) -> dict:
    """**成功落库之后**删掉 smailr 的收信邮箱。返回 `{"deleted", "failed"}`。

    🔴 为什么必须在落库**之后**：删除**不可逆**，而"账号已进台账"是唯一的
      安全点 —— 万一落盘失败，邮箱还在，还能重跑那条链。所以调用点在
      `run.py` 的 `save_snapshot()` 之后，而不是 `finish()` 里。

    🔴 为什么必须删：smailr 账号有**邮箱总数上限**（实测 `mailbox_limit=100`），
      而每个账号占一个 ⇒ 不删跑几十个就卡死。

    🔴 **只删"我们新建的"，不删"复用的"**。复用的邮箱是用户**预先建好的池子**
      （账号等级不够时只能领现成的，见 `src/smailr.py`），删掉它下一个账号就没得
      用 —— 而那个冲突是**真实的**：实测里 `create_mailbox` 已经会走复用路径了。
      判据是台账字段 `mail_reused`（由 `provision` 写入）。

    ⚠ **非致命**：删不掉只告警 —— 账号已经保存好了，不该因为清理失败让整批失败。
    ⚠ 只处理 `status == "success"` 的：失败的账号可能要重跑，把它的邮箱删了
      反而会丢掉唯一的收信地址。
    """

    def _f(r, name, default=""):
        """同时吃 dict 与 `AccountRecord`（调用方两处都会传）。"""
        return (r.get(name) if isinstance(r, dict) else getattr(r, name, default)) or default

    targets = [
        r
        for r in records
        if _f(r, "status") == "success"
        and config.source_of(_f(r, "mail_domain")) == "smailr"
        # 🔴 复用的**不删**（见 docstring）
        and not (r.get("mail_reused") if isinstance(r, dict) else getattr(r, "mail_reused", False))
    ]
    if not targets:
        return {"deleted": 0, "failed": 0}

    client = SmailrClient()
    deleted = failed = 0
    for r in targets:
        email = _f(r, "email")
        if not email:
            continue
        if client.delete_mailbox(email):
            deleted += 1
            log(f"  🗑  smailr 邮箱已删：{email}")
        else:
            failed += 1
            log(f"  ⚠ smailr 邮箱删不掉（账号已落库，不影响可用性）：{email} — {client.last_error}")
    return {"deleted": deleted, "failed": failed}
