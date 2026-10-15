"""台账记录与错误分类：`AccountRecord` / `Ticket` / `classify`。

为什么单独一个模块
------------------
这三样是"一条账号在流水线里的形状"：字段、阶段、错误类别。它们被
`provision` / `finish` / `batch` 三方共用，而**自己不含任何网络或编排逻辑** ——
所以放在最底层，谁都能 import 它，它不 import 谁（只依赖 `limiter` 与 `agnes`）。

🔴 `STAGES` 是**单一真源**：`AccountRecord.stages` 的键、报告的列、测试的断言
   都从这里取。各写一份必然漂移，而漂移的表现是"报告里少一列"（不会报错）。
"""

import json
import secrets
import threading
from dataclasses import dataclass, field

from common import config

from .. import quota as _quota
from ..agnes import (
    ERR_NETWORK,
    ERR_QUOTA,
    ERR_QUOTA_GUARD,
    AgnesClient,
    AgnesError,
)
from .limiter import REQUESTS_PER_ACCOUNT, DomainReservation, WindowBusy

# 阶段名与顺序。**单一真源**：`AccountRecord.stages` 的键、报告的列、
# 测试的断言都从这里取 —— 各写一份必然漂移（漂移的表现是"报告里少一列"，
# 而那不会报错）。
STAGES = ("mailbox", "code", "register", "login", "key", "app", "verify")


# 密码字符集。**刻意不用 `string.punctuation`**：里面含 `"` / `\` / `'` / `` ` ``，
# 而密码会被写进 JSON 台账、有时还要手打进浏览器 —— 那些字符在每一条路径上
# 都要被转义一次，是纯粹的麻烦来源。
_PW_LOWER = "abcdefghijkmnopqrstuvwxyz"  # 去掉 l
_PW_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # 去掉 I O
_PW_DIGIT = "23456789"  # 去掉 0 1
_PW_SYMBOL = "!#$%&*+-=?@^_"


def make_password(length: int | None = None) -> str:
    """生成一个必然满足服务端策略的密码。

    🔴 服务端实测只卡**长度**（`Password must be at least 8 characters`），
      没观察到大小写 / 数字 / 符号的强制要求。但**本地仍按四类字符各取一个**：
      平台随时可能加策略，而"密码策略变更导致整批注册失败"是最冤的一种失败。
      代价为零，所以不省。
    """
    n = max(8, int(length or config.PASSWORD_LEN))
    pool = _PW_LOWER + _PW_UPPER + _PW_DIGIT + _PW_SYMBOL
    chars = [
        secrets.choice(_PW_LOWER),
        secrets.choice(_PW_UPPER),
        secrets.choice(_PW_DIGIT),
        secrets.choice(_PW_SYMBOL),
    ]
    chars += [secrets.choice(pool) for _ in range(n - len(chars))]
    # Fisher–Yates 洗牌。**必须洗**：不洗的话前四位永远是"小写+大写+数字+符号"，
    # 那是一个可被规则识别的形状。
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def _as_int(raw, default: int = 0) -> int:
    """宽容地取整数 —— 用于**服务端给的**字段（额度上限等）。

    ⚠ 这些值来自 Agnes 的 JSON，而我们不拥有那个 API：某天它把
      `max_keys` 从数字改成 `"20"` 或 `null`，`int()` 会抛 `ValueError`，
      而那个异常会被 `classify()` 归到 `network`（可重试）——
      于是一次**形态变更**被伪装成网络抖动，重试多少次都一样。
      退回 0 的后果只是台账里那一列不好看，是安全方向的降级。
    """
    if isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _as_float(raw, default: float = 0.0) -> float:
    if isinstance(raw, bool):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# 🔴 打印锁：生产者与消费者线程都会往 stdout 打进度，而 `print()` 对
#    多段写入**不是原子的** —— 多线程并发时行会互相撕裂。实测症状：
#    `▸ <email> 邮箱+验证码就绪` 的邮箱位被别的线程覆盖成空白，看起来像
#    "邮箱是空的"，把排查引到完全错误的方向。
#    ⚠ 它**必须**与 `run_batch` 里那把 `lock` 分开：那把锁保护共享状态，
#      而 `record_out` 会持有它；打印再抢同一把锁就是死锁风险。
_PRINT_LOCK = threading.Lock()


def _print(msg) -> None:
    """默认日志出口 —— **必须 flush**，且**必须串行化**。

    🔴 为什么必须 flush：批量运行几乎总是把 stdout 重定向到文件 / 管道
       （`python run.py | tee log`），而 Python 对**非 tty** 的 stdout 用块缓冲：
       不 flush 的话，进程被 Ctrl-C / 超时杀掉时，缓冲里的进度**全部丢失** ——
       看起来像"卡住了一句话都没打"，而实际跑了大半。这个坑实测踩到过
       （一次 420s 的跑批，输出只剩预检那四行）。

    🔴 为什么必须持锁：见 `_PRINT_LOCK` 上方。少了它，多线程日志会撕裂成
       看似合理但**是错的信息** —— 比丢日志更坏。
    """
    with _PRINT_LOCK:
        print(msg, flush=True)


@dataclass
class AccountRecord:
    """一个账号的台账记录。`as_dict()` 的字段顺序就是这里写的顺序。"""

    email: str = ""
    password: str = ""
    # 这个账号用的是哪个收信域名（域名维度限流的记账维度，见 config）。
    mail_domain: str = ""
    # 🔴 这个账号的收信邮箱是**复用**来的（而不是新建的）—— smailr 账号等级不够
    #    时会走这条路（见 `src/smailr.py` 的复用逻辑）。
    #    它直接影响清理：**复用的不能删** —— 那是用户预先建好的池子，删了下一个
    #    账号就没得用（而删新建的是对的：smailr 有 100 个邮箱上限）。
    #    所以这个事实必须**进台账**，而不只活在内存里。
    mail_reused: bool = False
    # 🔴 **本账号新建过的所有收信地址**（换域名会**重建邮箱** ⇒ 可能好几个）。
    #
    #    为什么必须记全：清理 smailr 邮箱时只删最后一个 `email`，会让**中间**那些
    #    静默占着上限（实测 `mailbox_limit=100`）—— 不可见、不报错。
    #
    #    ⚠ **复用的地址不在里面**：那是用户预先建好的池子（等级不够时只能领现成的），
    #      删了下一个账号就没得用。所以这个列表同时就是"**该删的集合**"
    #      （`mailbox.cleanup_settled` 的输入）。
    mailboxes_created: list[str] = field(default_factory=list)
    # 控制台账号标识（来自 `/api/user/self` 与登录响应里的 `user`）。
    user_id: str = ""
    user_uuid: str = ""
    display_id: str = ""
    # 登录换到的 JWT。**取自密码登录那一步**，不是注册返回的那一条 ——
    # 后者可能是"刚建号时发的"，与密码无关。
    jwt: str = ""
    # 推理网关用的 `sk-` key。
    api_key: str = ""
    key_id: str = ""
    key_preview: str = ""
    # 订阅 / 额度快照。免费账号的 `/api/user/subscription` 实测是 `null`。
    plan: str = ""
    balance_minor: float = 0.0
    balance_currency: str = ""
    key_max: int = 0
    models_count: int = 0
    status: str = "failed"
    error: str = ""
    error_kind: str = ""
    stages: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    created_at: str = ""
    proxy: str = ""
    # 撞限流换过出口时的**轨迹**（`a → b`）。平时是空串。
    #
    # 🔴 为什么要单独一个字段：`proxy` 记的是**最终**出口（会话是在那里建的），
    #    而"曾经在 a 上被限流"是有价值的排查信息。
    proxy_rotated: str = ""
    # 撞**域名维度**限流时换过域名的轨迹（`a → b`）。平时是空串。
    # 与 `proxy_rotated` 同构：两个维度是独立的，台账里也要能分开看。
    mail_domain_rotated: str = ""
    # **当前正在跑的阶段**。不是台账字段（`as_dict` 里没有它）。
    #
    # 🔴 为什么需要它：`stages` 里的值会被**提前**写进去，所以"第一个还是空的
    #    阶段"**不能**代表失败发生在哪一步。显式记一个游标，
    #    `_failed_stage()` 就不再是启发式。
    stage_now: str = ""

    def __post_init__(self):
        # 所有阶段先落成 ""（= 没跑到）。**不能**只在跑过时写键：
        # 报告要按 `STAGES` 逐列渲染，缺键会让"没跑到"和"不存在这个阶段"
        # 变成两件事，而它们对读者是同一件事。
        for name in STAGES:
            self.stages.setdefault(name, "")

    # ── 打标 ──────────────────────────────────────────────────
    def enter(self, name: str) -> None:
        """进入一个阶段。**每个阶段开始时调一次** —— 失败时才知道自己死在哪。"""
        self.stage_now = name

    def stage(self, name: str, value: str) -> None:
        """记一个阶段的结果。`value` 形如 `ok` / `ok(19 keys left)` / `failed: …`。"""
        self.stages[name] = value

    def mark_ok(self, name: str, note: str = "") -> None:
        self.stages[name] = f"ok({note})" if note else "ok"

    def fail(self, kind: str, message: str, stage: str | None = None) -> None:
        """打上失败标记。**所有失败路径都必须走这里**（否则读点会静默退化）。

        🔴 `stage` 给定时，只把**那个**阶段标成 failed —— 后面的阶段保持 ""
          （= 没跑到）。把整条链都标成 failed 会让人以为"后面几步也试过了"。
        """
        self.status = "failed"
        self.error_kind = kind
        self.error = message[:500]
        if stage:
            self.stages[stage] = "failed"

    def skip(self, kind: str, message: str) -> None:
        """没跑（本地守卫拦下 / 本批已停）。与 `failed` 的区别是**没发请求**。"""
        self.status = "skipped"
        self.error_kind = kind
        self.error = message[:500]
        for name in STAGES:
            if not self.stages.get(name):
                self.stages[name] = "skipped"

    def done(self) -> None:
        self.status = "success"
        self.error = ""
        self.error_kind = ""

    # ── 序列化 ────────────────────────────────────────────────
    def as_dict(self) -> dict:
        return {
            "email": self.email,
            "password": self.password,
            "mail_domain": self.mail_domain,
            "mail_reused": self.mail_reused,
            "mailboxes_created": list(self.mailboxes_created),
            "user_id": self.user_id,
            "user_uuid": self.user_uuid,
            "display_id": self.display_id,
            "jwt": self.jwt,
            "api_key": self.api_key,
            "key_id": self.key_id,
            "key_preview": self.key_preview,
            "plan": self.plan,
            "balance_minor": self.balance_minor,
            "balance_currency": self.balance_currency,
            "key_max": self.key_max,
            "models_count": self.models_count,
            "status": self.status,
            "error": self.error,
            "error_kind": self.error_kind,
            "stages": dict(self.stages),
            "timings": dict(self.timings),
            "created_at": self.created_at,
            "proxy": self.proxy,
            "proxy_rotated": self.proxy_rotated,
            "mail_domain_rotated": self.mail_domain_rotated,
        }

    def to_json(self) -> str:
        # `ensure_ascii=False`：台账是给人看的，`\u4e2d` 那种形态没法读。
        return json.dumps(self.as_dict(), ensure_ascii=False)


def error_kind_of(rec) -> str:
    """读点的**唯一**实现：字段非空即权威，只有字段为空时才退化成文本匹配。

    ⚠ 文本兜底只服务"手工构造的老记录"。新增失败路径时**必须在抛出点打标**，
      否则读点会静默退化成文本匹配 —— 而那种退化不报错，只是让分类悄悄变糊。
    """
    if isinstance(rec, dict):
        kind, err = rec.get("error_kind"), rec.get("error") or ""
    else:
        kind, err = rec.error_kind, rec.error or ""
    kind = (kind or "").strip()
    if kind:
        return kind
    text = str(err).lower()
    if "quota_guard" in text or "本地配额" in text:
        return ERR_QUOTA_GUARD
    if "too many registration attempts" in text or "429" in text or "quota" in text:
        return ERR_QUOTA
    return ""


def classify(ex: BaseException) -> str:
    """异常 → `error_kind`。**唯一映射点**（别在调用处各写一份 if/elif）。"""
    if isinstance(ex, AgnesError):
        return ex.kind
    # 两个本地守卫同族：爆炸半径上限（`QuotaExceeded`）与滑动窗口等不起
    # （`WindowBusy`）都是"本地主动中止"，归 `quota_guard`。
    if isinstance(ex, (WindowBusy, _quota.QuotaExceeded)):
        return ERR_QUOTA_GUARD
    return ERR_NETWORK


@dataclass
class Ticket:
    """生产者交给消费者的东西。

    🔴 **`client` 是"顺序交接"的**：生产者线程建它、用它发验证码，
      然后通过队列交给消费者线程，之后只有消费者碰它。
      `requests.Session` 不是线程安全的 —— 但这里不存在**并发**访问，
      只有顺序访问，所以安全。要改成共享就得上每线程一个实例。

    🔴 **`proxy` 跟着账号走完两段**：注册限流是按出口的，所以消费者必须知道
      这个账号在哪个出口上 —— 否则它会在出口 A 的窗口里给出口 B 的账号排队。
      一个账号一条链一个出口，这也让台账里的 `proxy` 字段真的能回答
      "这个账号当时走的哪个出口"。
    """

    rec: AccountRecord
    client: AgnesClient
    code: str = ""
    mail_polls: int = 0
    proxy: str | None = None
    # 这个账号的收信域名。消费者注册时也要过**域名维度**的闸门，
    # 所以它必须跟着 ticket 走完两段。
    domain: str = ""
    # ── 域名维度的**预留**（见 `DomainReservation` 的 docstring）────────
    # 🔴 为什么预留要跟着 ticket 走：注册在**另一段**（消费者）发生，而那时
    #    域名已经定死。不把"我已经替你占好了"这个事实传过去，消费者就只能
    #    重新去抢 —— 而抢不到就变成了"码烧了、账号拿不到"。
    # ⚠ 必须 `default_factory`：`DomainReservation` 是**可变**的，共享一份默认值
    #    会让所有 Ticket 串账（这是个静默的串桶 bug）。
    reservation: DomainReservation = field(
        default_factory=lambda: DomainReservation(REQUESTS_PER_ACCOUNT)
    )
