"""两段式流水线：注册（生产者） ‖ 收尾（消费者）。

为什么分两段
------------
各阶段的时间形状完全不同：

| 阶段 | 动作 | 典型耗时 | 瓶颈在哪 |
|------|------|----------|----------|
| 生产者 | 建邮箱 → 发验证码 → **等码** | 2~5s | **SMTP 投递 + 收信轮询** |
| 消费者 | 注册 → 密码登录 → 建 Key → 只读校验 | 1~3s | 纯 HTTP，很快 |
| 末尾 | 校验 Key（打一次推理网关） | ~1~2s | 网关侧，**并行**做 |

把"等码"放在生产者池里并发跑，消费者池只做快活 —— 这样**邮件的等待时间被
并行化**，而不是被串行地加在关键路径上。

⚠ 与 Intern-Register-Tool 的**关键差异**：那边分两段的根本理由是"浏览器很贵"。
**本项目零浏览器**（整条链路纯 HTTP，无验证码 / 无 WAF 解盾），所以这里的拆分
理由是 **I/O 等待的形状不同**（等外部 SMTP vs. 打站点 API），不是资源稀缺。

为什么"注册尝试"要按出口限速
----------------------------
Agnes 实测有一层**按 IP 的注册尝试限流**，覆盖 `/api/verification` 与
`/api/user/register`（见 `common/config.REGISTER_MAX_PER_WINDOW` 的实测记录）。
所以本地闸门是**按出口分桶的滑动窗口**，而不是"每进程全局最小间隔" ——
后者两头都不对：既挡不住单出口超限，又白白把多出口串行化。

🔴 撞到服务端限流时的第一步是**换出口**，不是原地等。换不动（没配池子 /
只有 1 个出口 / 别的都占着）才退避重试。原地等一个长度未知的窗口，
在批量场景下会把整批拖死。

错误分类（`error_kind`）
-----------------------
| 值 | 含义 | 判据来源 |
|---|---|---|
| `""` | 没有错误 | — |
| `"quota"` | 服务端明确限流（那条 `Too many registration attempts` 的 400） | 服务端响应 |
| `"quota_guard"` | 本地守卫主动中止：爆炸半径上限（`QuotaExceeded`）或滑动窗口等不起（`WindowBusy`，封顶内等不到名额） | 本地。**`skipped` = 发码/注册前拦下，一个请求都没发；`failed` = 码已发出去、注册闸门才等不起** |
| `"rejected"` | 服务端明确拒绝（非配额）：邮箱已注册 / 密码策略 / 凭据错 | 服务端响应 |
| `"network"` | 网络 / 超时 / 5xx | 异常 |
| `"mail"` | 邮箱侧（建地址失败 / 读信读不出来） | 异常 |
| `"verify"` | 推理网关侧校验失败（非致命，账号本身仍算成功） | 网关响应 |

🔴 **读点只认字段**（`error_kind_of`）：字段非空即权威，只有字段为空时才退化成
文本匹配（给手工构造的老记录兜底）。Intern-Register-Tool 的教训是：靠自己拼的
错误文案里也可能出现服务端的词，文本匹配分不清"服务端说的"和"我们引用的"。
"""

import json
import queue
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from common import config

from . import apikey as _apikey
from . import quota as _quota
from .agnes import (
    ERR_MAIL,
    ERR_NETWORK,
    ERR_QUOTA,
    ERR_QUOTA_GUARD,
    ERR_REJECTED,
    ERR_VERIFY,
    AgnesClient,
    AgnesError,
    AgnesQuota,
    AgnesRejected,
    access_token_of,
)
from .proxypool import SlotPool, SlotPoolEmpty
from .tempmail import TempMailClient, now_ms

__all__ = [
    "STAGES",
    "ERR_QUOTA",
    "ERR_QUOTA_GUARD",
    "ERR_REJECTED",
    "ERR_NETWORK",
    "ERR_MAIL",
    "ERR_VERIFY",
    "WindowBusy",
    "AccountRecord",
    "error_kind_of",
    "classify",
    "make_password",
    "run_batch",
]

# 阶段名与顺序。**单一真源**：`AccountRecord.stages` 的键、报告的列、
# 测试的断言都从这里取 —— 各写一份必然漂移（漂移的表现是"报告里少一列"，
# 而那不会报错）。
STAGES = ("mailbox", "code", "register", "login", "key", "app", "verify")

# 连续多少次"服务端注册限流"就停整批。
# 🔴 取 2 而不是 1：单次限流完全可能是抖动。但连续两次就值得停 ——
#    撞上限流墙时继续打只是白烧请求，而且**加深**服务端对这张 IP 的印象。
QUOTA_STREAK_STOP = 2

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


# ── 按出口的滑动窗口限速器 ────────────────────────────────────────
class SlidingWindow:
    """滑动窗口限速器（**线程安全**）。

    为什么需要它（实测依据，不是防御性编程）
    ------------------------------------
    Agnes 的注册尝试限流是**按 IP** 的，而 `/api/verification` 与
    `/api/user/register` **共用同一张计数**。所以本地闸门必须与平台的判据同构：

    🔴 用**滑动窗口**而不是"最小间隔"：平台限的是"窗口内最多 N 次"，
      不是"每 X 秒一次"。用固定间隔时，只要有一次请求比预期快，
      就可能把第 N+1 次挤进窗口而撞限流。滑动窗口不会出现这种偏差。
    """

    def __init__(self, max_events: int, window_s: float):
        self.max_events = max(1, int(max_events))
        self.window_s = float(window_s)
        self._times: list[float] = []
        self._lock = threading.Lock()

    def acquire(self, max_wait: float | None = None) -> float:
        """等到可以发一个请求为止，返回**实际等了多久**（秒）。

        ⚠ 持锁期间 `sleep`：这是有意的 —— 放锁会让两个线程同时看到
          "还有名额"然后一起发出去，限速就失效了。代价是并发度被这个
          上限压成 N/window，而那正是平台的真实上限。

        `max_wait` 给定时是**封顶**：窗口打满且还要等更久，就**不睡、
        不占名额**，直接抛 `WindowBusy`。存在的理由见那边 —— 预检与
        `acquire()` 之间别的线程可能刚好把窗口打满，没有封顶的话这一次
        就会抱着**长度未知**的窗口睡死在锁里。
        """
        with self._lock:
            now = time.time()
            self._times = [t for t in self._times if now - t < self.window_s]
            wait = 0.0
            if len(self._times) >= self.max_events:
                wait = (self._times[0] + self.window_s) - now
                if wait > 0:
                    if max_wait is not None and wait > max_wait:
                        raise WindowBusy(
                            f"本地滑动窗口已满，还要等 {wait:.0f}s（封顶 {max_wait:g}s，等不起）",
                            wait_s=wait,
                        )
                    time.sleep(wait)
                now = time.time()
                self._times = [t for t in self._times if now - t < self.window_s]
            self._times.append(now)
            return max(0.0, wait)

    def wait_time(self) -> float:
        """还要等多久才能发一个（0 = 现在就能发）。**不消费名额**。

        🔴 存在的理由：窗口可能是**小时级**的（IP 维度实测 > 45 分钟），
          而 `acquire()` 会一直 `sleep` 到窗口滑出 —— 批量场景下那是灾难。
          调用方先用 `wait_time()` 看一眼：等得起就 `acquire()`，
          等不起就换维度（换出口 / 换域名）。
        """
        with self._lock:
            now = time.time()
            self._times = [t for t in self._times if now - t < self.window_s]
            if len(self._times) < self.max_events:
                return 0.0
            return max(0.0, (self._times[0] + self.window_s) - now)


class WindowBusy(Exception):
    """本地滑动窗口已满、且等不起（`acquire(max_wait=...)` 撞到封顶）。

    🔴 与 `_quota.QuotaExceeded`（爆炸半径上限）同族：都是**本地守卫**主动
      中止。抛它而不是抱着窗口睡死 —— 窗口长度未知（域名维度默认 3600s），
      睡满它等于把这条账号链挂住，批量场景下就是整批假死。

    ⚠ `wait_s` 是"窗口还要等多久"，给报告/日志用（好让人知道是该等、
      该换维度，还是该收工）。
    """

    def __init__(self, message: str, *, wait_s: float = 0.0):
        super().__init__(message)
        self.wait_s = wait_s


# 限速器。**按维度分桶**。
#
# 🔴 服务端有**两道**注册尝试窗口（IP 维度 + 域名维度，见 config），所以本地
#    也要两道，**两者都要过**：
#      · `register_limiter(slot)` —— IP 维度，按出口分桶（多出口 = 多张窗口）
#      · `domain_limiter(domain)` —— 域名维度，**全局**分桶（多域名 = 多张窗口）
#    只配一个就会从另一个维度撞服务端。
_REGISTER_LIMITERS: dict = {}
_DOMAIN_LIMITERS: dict = {}
_DOMAIN_USED: dict = {}
_LIMITER_LOCK = threading.Lock()


def register_limiter(slot: str | None = None) -> SlidingWindow:
    """取**某个出口**的注册尝试（验证码 + 注册）限速器（懒建 —— 测试改了 config 也能生效）。"""
    key = slot or ""
    with _LIMITER_LOCK:
        lim = _REGISTER_LIMITERS.get(key)
        if lim is None:
            lim = SlidingWindow(config.REGISTER_MAX_PER_WINDOW, config.REGISTER_WINDOW_S)
            _REGISTER_LIMITERS[key] = lim
        return lim


def domain_limiter(domain: str) -> SlidingWindow:
    """取**某个收信域名**的注册尝试限速器（**全局**，跨出口共享）。

    🔴 它与 `register_limiter` 是**两道独立的闸门**：服务端的域名维度窗口
      不是按出口计的（实测：用全新出口打已用过的域名仍然命中），
      所以本地也必须全局分桶 —— 按出口分桶等于放行 N 倍。
    """
    key = (domain or "").lower()
    with _LIMITER_LOCK:
        lim = _DOMAIN_LIMITERS.get(key)
        if lim is None:
            lim = SlidingWindow(config.DOMAIN_MAX_PER_WINDOW, config.DOMAIN_WINDOW_S)
            _DOMAIN_LIMITERS[key] = lim
        return lim


def pick_domain(domains, *, exclude: str | None = None) -> str:
    """在域名池里挑一个（**最少使用优先**），尽量避开 `exclude`。

    与槽位池同一个理由：轮换取用会让某个域名先把窗口用满，总吞吐反而更低。
    """
    cands = [d for d in (domains or []) if d != exclude] or list(domains or [])
    if not cands:
        return ""
    with _LIMITER_LOCK:
        d = min(cands, key=lambda x: (_DOMAIN_USED.get(x, 0), x))
        _DOMAIN_USED[d] = _DOMAIN_USED.get(d, 0) + 1
        return d


def _is_domain_limit(ex) -> bool:
    """这次限流是不是**域名维度**的（换出口没用，得换域名）。"""
    return "email domain" in str(ex).lower()


def _is_ip_limit(ex) -> bool:
    """这次限流是不是**IP 维度**的（换域名没用，得换出口）。"""
    return "from this ip" in str(ex).lower()


def _register_backoff_s() -> float:
    """换不动维度时退避多久（秒）。**封顶 45s**。

    🔴 不取"等满一个窗口"：窗口长度尚未测准（IP 维度实测 > 45 分钟），
      按账号等满一个窗口会把批量拖死。封顶后重试仍失败，就把这条记录标成
      `quota` 失败 —— 那是**诚实**的失败（试过了，被限流），比挂着不动好。
    """
    per_ip = config.REGISTER_WINDOW_S / max(1, config.REGISTER_MAX_PER_WINDOW)
    per_dom = config.DOMAIN_WINDOW_S / max(1, config.DOMAIN_MAX_PER_WINDOW)
    return max(5.0, min(45.0, max(per_ip, per_dom)))


def _rotate_slot(slot_pool, holder, *, error: str) -> bool:
    """把这条链换到**另一个出口**。换成了返回 `True`。

    🔴 为什么是换出口，而不是原地重试：注册限流是**按 IP** 的。原地重试撞的
      还是同一张窗口；换一个出口就是一张**全新**的窗口。

    ⚠ 旧出口要**归还**（并按失败记账）—— 否则池子会逐渐少一个，而现象是
      "批量慢慢变慢/超时"，不是"池子漏了"。
    ⚠ `holder.proxy` 必须一起改：消费者最后归还的是 `holder.proxy` 指向的那个，
      不改就会**双重归还旧出口、而新出口永远漏在外面**。
    """
    if not slot_pool or len(slot_pool) < 2:
        return False
    old = holder.proxy
    try:
        new = slot_pool.acquire(timeout=5)
    except SlotPoolEmpty:
        return False
    if not new or new == old:
        # 只借到同一个（或没借到）⇒ 换不动，让调用方退避重试。
        if new:
            slot_pool.release(new)
        return False
    slot_pool.release(old, ok=False, error=f"quota {error[:120]}")
    holder.proxy = new
    return True


# ── 生产者：建邮箱 → 发验证码 → 等码 ──────────────────────────────
def provision(
    ticket: Ticket,
    *,
    mail,
    mail_domain: str | None = None,
    slot_pool=None,
    client_factory=None,
    log=_print,
) -> Ticket:
    """跑完生产者那一段。成功返回 `ticket`；失败**抛异常**（由调用方打标）。

    ⚠ 这里**不捕获异常**，是有意的：异常类型带着 `error_kind` 的分类信息，
      在这一层吞掉就得在这一层重新分类（= 第二份分类实现）。让调用方统一
      用 `classify()` 处理。
    """
    rec = ticket.rec
    client_factory = client_factory or (lambda proxy=None: AgnesClient(proxy=proxy))
    t0 = time.time()

    # 密码在**生产者**这一侧就生成好，而不是等消费者那一步再生成 ——
    # `rec.password` 是台账里的一等字段，而"邮箱建出来了但密码是空的"
    # 是最坏的一种中间态（账号已存在、却无法用密码登录）。
    rec.password = make_password()

    # 🔴 域名池：服务端有一道**域名维度**的限流，所以这里要能换域名。
    #    `--mail-domain` 给定时**锁定**那一个域名（不再轮换）——
    #    那是有意的：它用于"我就想测这个域名"的排查场景。
    domains = [mail_domain] if mail_domain else config.mail_domains()
    if not domains:
        raise AgnesRejected(
            "没有可用的收信域名（AR_MAIL_DOMAINS / AR_WORKER_DOMAIN 都为空）", code="NO_DOMAIN"
        )
    domain = pick_domain(domains)
    rotated_domains: list[str] = []
    rotated_slots: list[str] = []
    waited = 0.0
    sent_at = 0
    t1 = time.time()
    for attempt in range(config.REGISTER_RETRIES + 1):
        # ── 两个维度的本地闸门 ────────────────────────────────────
        # 🔴 **先看要等多久再决定**：窗口可能是小时级的，抱着闸门 sleep 会把
        #    批量拖死。等不起就换维度（换出口 / 换域名）。
        ip_wait = register_limiter(ticket.proxy).wait_time()
        if ip_wait > config.LIMITER_MAX_WAIT_S:
            old = ticket.proxy
            if _rotate_slot(slot_pool, ticket, error="local ip window full"):
                rotated_slots.append(old or "(直连)")
                ticket.client = client_factory(ticket.proxy)
                log(f"本地 IP 窗口要等 {ip_wait:.0f}s → 换出口")
                continue
        dom_wait = domain_limiter(domain).wait_time()
        if dom_wait > config.LIMITER_MAX_WAIT_S:
            nxt = pick_domain(domains, exclude=domain)
            if nxt and nxt != domain:
                rotated_domains.append(domain)
                domain = nxt
                log(f"本地域名窗口要等 {dom_wait:.0f}s → 换域名 {domain}")
                continue
        need = max(ip_wait, dom_wait)
        if need > 0:
            time.sleep(min(need, _register_backoff_s()))
        # 🔴 封顶传 `LIMITER_MAX_WAIT_S`：上面的预检与这里的 acquire 之间，
        #    别的线程可能刚好把窗口打满。没有封顶的话这一次 acquire 会抱着
        #    长度未知的窗口（域名维度默认 3600s）睡死在锁里 —— 整条账号链
        #    挂住。撞封顶抛 `WindowBusy`，由本循环 / 调用方按"等不起"处置。
        waited += register_limiter(ticket.proxy).acquire(config.LIMITER_MAX_WAIT_S)
        domain_limiter(domain).acquire(config.LIMITER_MAX_WAIT_S)

        # ── 建邮箱（每次重试都新建：换域名就得换地址）──────────────
        rec.enter("mailbox")
        emails = mail.create_mailbox(domain, 1)
        # 🔴 必须校验**元素**，不能只查 `if not emails`。
        #    Worker 在异常路径下会返回**列表非空但元素是空串**（`[""]`）——
        #    只查列表的话空地址会一路走下去，后果比"报错点偏远"严重得多：
        #      · 收信是按**收件人**过滤的（`GET /api/inbox?email=`），空收件人
        #        等于**不设限** ⇒ 可能抽到**别人邮件的验证码**，把别人的地址
        #        当成自己的账号注册掉；
        #      · 日志表现为"邮箱+验证码就绪"但邮箱位是空的（实测见过）。
        #    ⚠ 不能写成 `str(emails[0]).strip()`：`None` 会变成字符串 `"None"`
        #      —— 一个**看着合法**的假地址，比空串更难查。
        first = emails[0] if emails else None
        address = first.strip() if isinstance(first, str) else ""
        if not address:
            raise AgnesRejected(
                f"邮箱 Worker 没返回有效地址（create_mailbox 返回 {emails!r}）",
                code="NO_MAILBOX",
            )
        rec.email = address
        rec.mail_domain = domain
        if not rec.timings.get("mailbox_ms"):
            rec.timings["mailbox_ms"] = int((time.time() - t0) * 1000)
        rec.mark_ok("mailbox")

        # ── 发验证码（写请求，两个维度的窗口都消耗）───────────────
        rec.enter("code")
        sent_at = now_ms()
        try:
            ticket.client.send_code(rec.email)
            break
        except AgnesQuota as ex:
            if attempt >= config.REGISTER_RETRIES:
                raise
            # 🔴 **先看是哪个维度**：域名维度换出口没用，IP 维度换域名没用。
            #    换错维度 = 白烧一次尝试（而尝试本身也消耗窗口）。
            if _is_domain_limit(ex):
                nxt = pick_domain(domains, exclude=domain)
                if nxt and nxt != domain:
                    rotated_domains.append(domain)
                    domain = nxt
                    log(f"发码被**域名**限流 → 换域名 {domain}（{ex}）")
                    continue
            else:
                old = ticket.proxy
                if _rotate_slot(slot_pool, ticket, error=str(ex)):
                    rotated_slots.append(old or "(直连)")
                    ticket.client = client_factory(ticket.proxy)
                    log(f"发码被限流 → 换出口（{ex}）")
                    continue
            back = _register_backoff_s()
            log(f"发码被限流，退避 {back:.0f}s 后重试（{ex}）")
            time.sleep(back)
    else:
        # 🔴 循环**没有 `break`** 就结束了 ⇒ **一次验证码都没发出去**：
        #    每一次尝试都被上面的"本地闸门等不起 → 换维度"给 `continue` 掉了
        #    （IP 窗口与域名窗口都满，换不动）。
        #
        #    🔴 这里**绝不能**落到下面的 `wait_for_code`：那会抱着
        #    `MAIL_POLL_TIMEOUT`（默认 120s）等一封**从来没被请求过**的信，
        #    最后报 `等码超时`（`rejected`）—— 而真相是"一个注册请求都没发"。
        #    实测踩到：10 批里 5 次这样的**假 `rejected`**，把排查引向
        #    "Worker 没收到信 / 域名白名单"，与真实原因（本地闸门）恰好相反。
        #    项目对这两种状态的区分是硬要求：`skipped` = 没试过，
        #    `failed` = 试过了不行。
        #
        #    抛 `WindowBusy` ⇒ `classify` → `quota_guard` ⇒ 生产者按
        #    **`skipped`** 记账（且**不**计入配额尝试）—— 与语义一致。
        raise WindowBusy(
            f"本地闸门把 {config.REGISTER_RETRIES + 1} 次尝试全部换掉了，"
            f"一个验证码请求都没发出去（IP / 域名窗口都满）"
        )
    rec.timings["code_send_ms"] = int((time.time() - t1) * 1000)
    if waited:
        rec.timings["code_wait_send_ms"] = int(waited * 1000)

    # 等信。**等码是整链最大的单项耗时**（SMTP + 轮询）。
    t2 = time.time()
    code = mail.wait_for_code(rec.email, since_ts=sent_at)
    rec.timings["code_wait_ms"] = int((time.time() - t2) * 1000)
    rec.timings["code_polls"] = int(getattr(mail, "last_polls", 0))
    rec.timings["code_5xx"] = int(getattr(mail, "last_http_errors", 0))
    if not code:
        # 🔴 两种失败必须分开报：`last_error` 非空 = **读不出来**（修法完全不同）。
        if getattr(mail, "last_error", ""):
            raise AgnesError(f"邮箱读不出来：{mail.last_error}", code="MAIL_READ")
        raise AgnesRejected(
            f"等 {config.MAIL_POLL_TIMEOUT}s 没等到验证码邮件"
            f"（打了 {getattr(mail, 'last_polls', 0)} 次收信接口）",
            code="MAIL_TIMEOUT",
        )
    ticket.code = code
    ticket.domain = domain
    rec.proxy = ticket.proxy or rec.proxy
    rec.timings["code_ms"] = int((time.time() - t1) * 1000)
    rec.mark_ok("code", f"{getattr(mail, 'last_polls', 0)} polls")
    if rotated_slots:
        rec.proxy_rotated = " → ".join([*rotated_slots, ticket.proxy or "(直连)"])
    if rotated_domains:
        rec.mail_domain_rotated = " → ".join([*rotated_domains, domain])
    return ticket


# ── 消费者：注册 → 登录 → 建 Key → 只读校验 ───────────────────────
def finish(ticket: Ticket, *, client_factory=None, slot_pool=None, log=_print) -> AccountRecord:
    """跑完消费者那一段。**就地改 `rec` 并返回它**（失败也返回，不抛）。"""
    rec = ticket.rec
    client_factory = client_factory or (lambda proxy=None: AgnesClient(proxy=proxy))

    # ① 注册。用生产者拿到的验证码。
    t0 = time.time()
    rec.enter("register")
    waited = 0.0
    rotated_from: list[str] = []
    body = None
    for attempt in range(config.REGISTER_RETRIES + 1):
        # 注册同样消耗**两个维度**的窗口 —— 与发码同一套闸门。
        ip_wait = register_limiter(ticket.proxy).wait_time()
        dom_wait = domain_limiter(ticket.domain).wait_time()
        if ip_wait > config.LIMITER_MAX_WAIT_S:
            old = ticket.proxy
            if _rotate_slot(slot_pool, ticket, error="local ip window full"):
                rotated_from.append(old or "(直连)")
                ticket.client = client_factory(ticket.proxy)
                continue
        # 🔴 域名维度等不起时**只能退避** —— 邮箱已经定了，换不了域名
        #    （换出口对域名窗口没用）。退避封顶，避免把批量拖死。
        need = max(ip_wait, dom_wait)
        if need > 0:
            time.sleep(min(need, _register_backoff_s()))
        # 与发码同一套封顶（理由见 provision）：窗口可能被并发打满，
        # 不能抱着 3600s 的域名窗口睡死。
        try:
            waited += register_limiter(ticket.proxy).acquire(config.LIMITER_MAX_WAIT_S)
            domain_limiter(ticket.domain).acquire(config.LIMITER_MAX_WAIT_S)
        except WindowBusy as ex:
            # 邮箱和验证码**已经发出去了**（不是"一个请求都没发"）⇒ 这是
            # failed 而不是 skipped。重试未用完就退避再来一次；用完就诚实
            # 标 `quota_guard` 失败 —— 试过了，本地闸门等不起。
            if attempt >= config.REGISTER_RETRIES:
                rec.fail(
                    ERR_QUOTA_GUARD,
                    f"注册闸门等不起（已试 {attempt + 1} 次）：{ex}",
                    stage="register",
                )
                rec.timings["register_ms"] = int((time.time() - t0) * 1000)
                return rec
            time.sleep(_register_backoff_s())
            continue
        try:
            body = ticket.client.register(rec.email, rec.password, ticket.code)
            break
        except AgnesQuota as ex:
            # 与发验证码同一条判据：限流自愈，先换**对的那个维度**再退避。
            if attempt >= config.REGISTER_RETRIES:
                rec.fail(
                    ERR_QUOTA,
                    f"注册持续被限流（已试 {attempt + 1} 次，换出口 {len(rotated_from)} 次）：{ex}",
                    stage="register",
                )
                rec.timings["register_ms"] = int((time.time() - t0) * 1000)
                return rec
            if _is_domain_limit(ex):
                # 域名维度：邮箱已定，换出口没用 → 退避重试
                time.sleep(_register_backoff_s())
                continue
            old = ticket.proxy
            if _rotate_slot(slot_pool, ticket, error=str(ex)):
                rotated_from.append(old or "(直连)")
                ticket.client = client_factory(ticket.proxy)
                continue
            time.sleep(_register_backoff_s())
        except AgnesRejected as ex:
            rec.fail(ERR_REJECTED, f"注册被拒：{ex}", stage="register")
            rec.timings["register_ms"] = int((time.time() - t0) * 1000)
            return rec
        except AgnesError as ex:
            rec.fail(classify(ex), f"注册失败：{ex}", stage="register")
            rec.timings["register_ms"] = int((time.time() - t0) * 1000)
            return rec
    else:
        # 🔴 循环**没有 `break`** 就结束了 ⇒ **注册请求一次都没发出去**：
        #    每一次尝试都被上面的「IP 窗口等不起 → 换出口」给 `continue` 掉了。
        #    那条路径**没有** `attempt >= REGISTER_RETRIES` 的兜底，所以它能
        #    安静地把所有尝试吃完 —— 与 `provision` 里那个是同一类缺陷。
        #
        #    🔴 这里**绝不能**往下走：下面会拿 `body=None` 算出空的 `user`，
        #    然后 `rec.mark_ok("register", "uid=")` —— 把**从没注册过**的账号
        #    标成"注册成功"，再拿它去登录，最后以
        #    `登录失败：HTTP 401 Invalid username or password` 收场。
        #    实测踩到 2 次：`stages['register'] == 'ok(uid=)'`（uid 是空的），
        #    而报告把原因指向**密码**，与真相（注册根本没发出去）恰好相反。
        rec.fail(
            ERR_QUOTA_GUARD,
            f"注册请求一次都没发出去（{config.REGISTER_RETRIES + 1} 次尝试"
            f"全被「IP 窗口等不起 → 换出口」消耗）",
            stage="register",
        )
        rec.timings["register_ms"] = int((time.time() - t0) * 1000)
        return rec
    rec.timings["register_ms"] = int((time.time() - t0) * 1000)
    if waited:
        rec.timings["register_wait_ms"] = int(waited * 1000)
    if rotated_from:
        rec.proxy_rotated = " → ".join([*rotated_from, ticket.proxy or "(直连)"])
    rec.proxy = ticket.proxy or rec.proxy
    user = (
        (body or {}).get("data", {}).get("user")
        if isinstance((body or {}).get("data"), dict)
        else None
    )
    # 🔴 注册响应里必须有 `data.user.id` —— **HTTP 200 也可能是空壳**。
    #    缺了就说明账号没真建出来；继续标 ok 会把"没注册"伪装成"密码错"。
    if not isinstance(user, dict) or not user.get("id"):
        rec.fail(
            ERR_REJECTED,
            f"注册响应缺少 data.user.id（body={str(body)[:120]}）",
            stage="register",
        )
        return rec
    rec.user_id = str(user.get("id") or "")
    rec.user_uuid = str(user.get("uuid") or "")
    rec.display_id = str(user.get("display_id") or "")
    rec.mark_ok("register", f"uid={rec.user_id}")

    # ② 密码登录 —— **换一条全新的 JWT**。这一步才是"账号真能用"的证明：
    #    注册返回的那条 token 是"刚建号时发的"，与密码无关。
    t1 = time.time()
    rec.enter("login")
    try:
        login = ticket.client.login(rec.email, rec.password)
        jwt = access_token_of(login)
        if not jwt:
            raise AgnesRejected("登录响应里没有 access_token", code="NO_TOKEN")
        rec.jwt = jwt
        luser = login.get("data", {}).get("user") if isinstance(login.get("data"), dict) else None
        if isinstance(luser, dict):
            rec.user_id = str(luser.get("id") or rec.user_id)
            rec.user_uuid = str(luser.get("uuid") or rec.user_uuid)
            rec.display_id = str(luser.get("display_id") or rec.display_id)
        rec.mark_ok("login", f"jwt {len(jwt)}B")
    except AgnesError as ex:
        rec.fail(classify(ex), f"登录失败：{ex}", stage="login")
        rec.timings["login_ms"] = int((time.time() - t1) * 1000)
        return rec
    rec.timings["login_ms"] = int((time.time() - t1) * 1000)

    # ③ 建 API Key。
    t2 = time.time()
    rec.enter("key")
    try:
        data = ticket.client.create_key(config.KEY_NAME)
        rec.api_key = str(data.get("key") or "")
        rec.key_id = str(data.get("id") or "")
        rec.key_preview = str(data.get("key_preview") or "")
        if not rec.api_key:
            raise AgnesRejected("建 key 响应里没有 key 字段", code="NO_KEY")
        rec.mark_ok("key", rec.key_preview or f"id={rec.key_id}")
    except AgnesError as ex:
        rec.fail(classify(ex), f"建 key 失败：{ex}", stage="key")
        rec.timings["key_ms"] = int((time.time() - t2) * 1000)
        return rec
    rec.timings["key_ms"] = int((time.time() - t2) * 1000)

    # ④ 只读校验：账号 / 余额 / 订阅 / key 配额。
    if not verify_app(rec, ticket.client):
        return rec

    rec.done()
    return rec


def verify_app(rec: AccountRecord, client) -> bool:
    """只读校验：`/api/user/self` / `balance` / `subscription` / `token` 列表。

    返回 `True` = 全部通过。

    🔴 抽成独立函数是为了**只有一处实现**：`finish()`（注册链的收尾）与
      `tools/ops/verify_accounts.py`（对已有账号做只读复核）必须检查同样的东西
      —— 否则"复核通过"与"注册成功"会变成两个不同的标准，而台账里那两行
      看不出区别。

    ⚠ **不调用 `rec.done()`**：那一步是调用方的。失败时已把 `rec` 打上标
      （`error_kind` + `stages.app = failed`）。
    """
    t0 = time.time()
    rec.enter("app")
    try:
        me = client.user_self()
        if not me.get("email"):
            rec.fail(ERR_REJECTED, "user/self 没有返回 email（会话没建立）", stage="app")
            return False
        rec.user_id = str(me.get("id") or rec.user_id)
        rec.user_uuid = str(me.get("uuid") or rec.user_uuid)
        rec.display_id = str(me.get("display_id") or rec.display_id)
        if not me.get("is_active", True):
            # 注册成功但**账号未激活**是"看着成功、其实不能用"，必须显式标出来。
            rec.fail(ERR_REJECTED, "user/self 显示 is_active=false（账号未激活）", stage="app")
            return False
        bal = client.balance()
        rec.balance_minor = _as_float(bal.get("available_total_minor"))
        rec.balance_currency = str(bal.get("currency") or "")
        sub = client.subscription()
        rec.plan = str((sub or {}).get("plan") or (sub or {}).get("name") or "free")
        keys = client.list_keys()
        quota = keys.get("default_quota") if isinstance(keys.get("default_quota"), dict) else {}
        rec.key_max = _as_int(quota.get("max_keys"))
    except AgnesError as ex:
        rec.fail(classify(ex), f"校验失败：{ex}", stage="app")
        return False
    finally:
        rec.timings["app_ms"] = int((time.time() - t0) * 1000)
    rec.mark_ok(
        "app", f"plan={rec.plan}, keys<={rec.key_max}" if rec.key_max else f"plan={rec.plan}"
    )
    return True


# ── 末尾统一校验 Key（非致命）────────────────────────────────────
def verify_keys(records: list, *, verbose: bool = True, log=_print) -> None:
    """统一校验已建出的 API Key 能否调用推理网关。

    为什么单独放最后，而不是塞在 `finish` 的关键路径里：
      · 批量场景下把这一步并行化，消费者池只做"注册 → 登录 → 建 key"这些
        必须串行在**同一出口**上的事；
      · 校验是**非致命**的 —— 失败只写进 `stages["verify"]`，不影响账号状态。

    ⚠ Agnes 侧实测新建 key **立刻**可用（传播延迟约 0），所以这里几乎瞬时。
      若哪天平台加了传播延迟，`apikey.verify` 自带 `attempts` / `delay` 重试。
    """
    targets = [r for r in records if getattr(r, "api_key", "") and r.status == "success"]
    if not targets:
        return

    def one(rec: AccountRecord) -> tuple:
        t0 = time.time()
        try:
            res = _apikey.verify(rec.api_key, attempts=3, delay=2.0)
            if res["ok"]:
                rec.models_count = len(res["models"])
                return rec, str(res["note"])
            return rec, str(res["note"])
        except Exception as ex:  # noqa: BLE001
            return rec, f"failed: {str(ex)[:100]}"
        finally:
            # 🔴 计时必须在**这里**（每个账号自己那一段），不能在 `run_batch`
            #    里围着整个 `verify_keys` 取一次 `t0` 再发给所有记录 —— 那样
            #    每条拿到的是**同一个值** = 整个校验阶段的时长，而它是并行跑的。
            #    实测后果：5 个账号的 `verify_ms` 全是 `70414`，报告把 70s
            #    记到**每个**账号头上，把"单 key 全链路速率"整体带偏。
            rec.timings["verify_ms"] = int((time.time() - t0) * 1000)

    with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as ex:
        for f in [ex.submit(one, r) for r in targets]:
            rec, verdict = f.result()
            rec.stages["verify"] = verdict
            if verbose:
                log(f"key verify {rec.email}: {verdict}")


# ── 编排 ──────────────────────────────────────────────────────────
def run_batch(
    *,
    count: int,
    workers: int = 4,
    producers: int = 4,
    mail_domain: str | None = None,
    verbose: bool = True,
    ignore_quota: bool = False,
    mail_factory=None,
    client_factory=None,
    slot_pool=None,
    log=_print,
) -> list[AccountRecord]:
    """跑 `count` 个账号。返回**本批**记录（含失败 / 跳过，顺序=完成顺序）。

    并发结构：

        ┌─ 生产者池（producers 路）──────────────┐   ┌─ 消费者池（workers 路）──┐
        │ 建邮箱 → 发验证码 → 等码                │──▶│ 注册 → 登录 → 建 Key → 校验 │
        └────────────────────────────────────────┘ 队列 └─────────────────────────┘
                                                   末尾：并行校验所有 Key

    `mail_factory` / `client_factory` 是**注入点**：默认造真的客户端，
    测试传假的 —— 这样编排、打标、配额逻辑都能**离线**测（零网络请求）。
    """
    mail_factory = mail_factory or (lambda: TempMailClient())
    client_factory = client_factory or (lambda proxy=None: AgnesClient(proxy=proxy))
    # 槽位池：**由调用方拥有**（`run.py` 建它、传进来、跑完读它的统计）。
    # 不在这里建的原因：返回值是 `list[AccountRecord]`，没法把统计挂在上面
    # （`list` 不能设属性），而用模块全局会在同进程跑多批时被后一批覆盖。
    if slot_pool is None:
        slot_pool = SlotPool(config.proxy_slots(), timeout=config.PROXY_SLOT_TIMEOUT)

    q: queue.Queue = queue.Queue(maxsize=max(2, producers))
    done: list[AccountRecord] = []
    lock = threading.Lock()
    stop = threading.Event()
    streak = {"n": 0}
    sentinel = object()

    def say(msg: str) -> None:
        if verbose:
            log(msg)

    def record_out(rec: AccountRecord) -> None:
        """一条记录完工 —— **所有出口都走这里**（成功 / 失败 / 跳过）。

        🔴 配额计数也在这里记，而不是在消费者里 —— 放在消费者里的话，
          **生产者阶段**就失败的账号（等不到验证码 / 服务端拒绝）根本不会落账，
          于是"打了几枪"这个数永远偏小。

        🔴 `skipped` **不记账**：那是本地闸门拦下的（一个请求都没发），
          把它算进"失败尝试"会把分母弄脏。
        """
        with lock:
            done.append(rec)
        if rec.status != "skipped":
            _quota.record(rec.email, ok=(rec.status == "success"), note=rec.error_kind)

    def producer(idx: int) -> None:
        rec = AccountRecord()
        rec.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # ① 本地闸门。**在发任何请求之前** —— 这就是 `quota_guard` 的判据。
        #
        # 🔴 这里先把"该不该跑"判成一个字符串，**出了锁**再记账：
        #    `record_out()` 自己也要拿 `lock`，而 `threading.Lock` **不可重入**
        #    ⇒ 在 `with lock:` 里调它就是**自锁**（整个批次永久挂死）。
        reason = None
        with lock:
            if stop.is_set():
                reason = "本批已因连续注册限流而停止（未发请求）"
            else:
                try:
                    _quota.check(ignore=ignore_quota)
                except _quota.QuotaExceeded as ex:
                    reason = str(ex)
        if reason is not None:
            rec.skip(ERR_QUOTA_GUARD, reason)
            record_out(rec)
            return
        # ② 借一个出口。**在闸门之后、第一个请求之前** —— 被本地闸门拦下的
        #    任务不该占着出口，也不该把出口的使用次数记上一笔。
        try:
            slot = slot_pool.acquire()
        except SlotPoolEmpty as ex:
            rec.fail(ERR_NETWORK, f"代理槽位池：{ex}", stage="mailbox")
            record_out(rec)
            return
        rec.proxy = slot or config.AR_PROXY
        mail = mail_factory()
        client = client_factory(slot)
        ticket = Ticket(rec=rec, client=client, proxy=slot)
        try:
            provision(
                ticket,
                mail=mail,
                mail_domain=mail_domain,
                slot_pool=slot_pool,
                client_factory=client_factory,
            )
        except (_quota.QuotaExceeded, WindowBusy) as ex:  # 注入的假客户端也可能抛
            # 本地守卫主动中止（爆炸半径上限 / 滑动窗口等不起）⇒ **一个请求都
            # 没发** ⇒ skipped，不是 failed。两者对读者的含义完全不同：
            # skipped = "本来就没试"，failed = "试过了，不行"。
            rec.skip(ERR_QUOTA_GUARD, str(ex))
            slot_pool.release(slot, ok=None)
            record_out(rec)
            return
        except Exception as ex:  # noqa: BLE001
            kind = classify(ex)
            stage = _failed_stage(rec)
            rec.fail(kind, f"{type(ex).__name__}: {ex}" if kind == ERR_NETWORK else str(ex), stage)
            # 只有 `network` 才可能真的是出口的问题 —— 把"邮箱已注册"记到
            # 出口头上，会让每个出口的失败数变成噪音，报告就没人看了。
            slot_pool.release(slot, ok=(False if kind == ERR_NETWORK else None), error=rec.error)
            if kind == ERR_QUOTA:
                with lock:
                    streak["n"] += 1
                    n = streak["n"]
                    tripped = n >= QUOTA_STREAK_STOP
                    if tripped:
                        stop.set()
                if tripped:
                    log(f"  ⛔ 连续 {n} 次服务端注册限流 —— 停止本批剩余任务（不发请求）")
            else:
                with lock:
                    streak["n"] = 0
            record_out(rec)
            return
        with lock:
            streak["n"] = 0
        say(
            f"  ▸ {rec.email} 邮箱+验证码就绪"
            f"（出口 {slot or '直连'}，等码 {rec.timings.get('code_wait_ms', 0)}ms，"
            f"轮询 {ticket.mail_polls or rec.timings.get('code_polls', 0)} 次）"
        )
        # 槽位**不在这里归还** —— 消费者还要用它做注册（限流是按出口的）。
        q.put(ticket)

    def consumer() -> None:
        while True:
            item = q.get()
            try:
                if item is sentinel:
                    return
                try:
                    rec = finish(item, client_factory=client_factory, slot_pool=slot_pool, log=log)
                except Exception as ex:  # noqa: BLE001
                    # 🔴 兜底：`finish()` 抛出未预期异常时**不能**让 consumer
                    #    线程死掉 —— 线程死 = 这条记录永远不 `record_out` =
                    #    台账静默少一条。防缩水护栏只会在写盘时发现"少了"，
                    #    答不上"为什么少"。打标后走统一的收尾路径。
                    rec = item.rec
                    kind = classify(ex)
                    rec.fail(kind, f"{type(ex).__name__}: {ex}", _failed_stage(rec))
                if rec.status == "success":
                    say(f"  ✓ {rec.email} 注册成功（{_fmt_ms(rec)}）")
                else:
                    say(f"  ✗ {rec.email} 失败 [{rec.error_kind}] {rec.error[:120]}")
                # 出口的成败由**整条链**的结果决定（而不是只看注册那一段）。
                slot_pool.release(
                    item.proxy,
                    ok=(
                        True
                        if rec.status == "success"
                        else (False if rec.error_kind == ERR_NETWORK else None)
                    ),
                    error=rec.error,
                )
                record_out(rec)
            finally:
                q.task_done()

    consumers = [threading.Thread(target=consumer, daemon=True) for _ in range(max(1, workers))]
    for th in consumers:
        th.start()
    # 🔴 生产者并发必须**真的**受 `--producers` 约束：手写 `count` 个线程再逐个
    #    `start()` 的话，那个参数只是个装饰 —— 而"参数没生效"不会报错，
    #    只会让限流变成一句空话。`ThreadPoolExecutor` 的 `max_workers` 才是闸门。
    with ThreadPoolExecutor(max_workers=max(1, producers)) as pool:
        list(pool.map(producer, range(count)))
    # 🔴 哨兵数量 = 消费者数量：少一个就有线程永远 `get()` 阻塞，
    #    `run_batch` 直接挂死（而且是在"全部账号都已跑完"之后才挂）。
    for _ in consumers:
        q.put(sentinel)
    for th in consumers:
        th.join()

    # 逐条计时由 `verify_keys.one()` 自己写进 `rec.timings["verify_ms"]` ——
    # 不能在这里取一次 `t0` 发给所有人（见 `one()` 里的注释）。
    verify_keys(done, verbose=verbose, log=log)
    return done


def _failed_stage(rec: AccountRecord) -> str | None:
    """失败发生在哪一步 —— **靠游标，不靠猜**。

    🔴 `AccountRecord.enter()` 在每个阶段开始时把 `stage_now` 设成那个阶段。
      不能用"第一个还是空的阶段"代替：`stages` 里的值会被提前写，
      而"第一个空阶段"会把一次**注册失败**报成**登录失败** —— 那是一个
      **错误结论**，比"不知道"坏得多。

    游标为空（比如注入的假客户端在第一个阶段之前就抛）时，退回"第一个空阶段"。
    """
    if rec.stage_now:
        return rec.stage_now
    for name in STAGES:
        if not rec.stages.get(name):
            return name
    return None


def _fmt_ms(rec: AccountRecord) -> str:
    total = sum(int(v) for v in rec.timings.values())
    parts = " ".join(f"{k[:-3]}={v}ms" for k, v in rec.timings.items() if v)
    return f"合计 {total}ms｜{parts}"
