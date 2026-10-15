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
**本项目零浏览器**（整条链路纯 HTTP），所以这里的拆分理由是 **I/O 等待的形状
不同**（等外部 SMTP vs. 打站点 API），不是资源稀缺 —— 这个结论**不因 captcha
而改变**。
🔴 ⚠ 但 **2026-10-15 16:24 起 ②发验证码 要求 captcha**（`400 Captcha
   verification is required`）⇒ "零浏览器"这条前提当前**不成立**：生产者段会在
   `send_code` 上以 `rejected` 失败。见 `docs/protocol.md` §2②。

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

# 🔴 本文件**只做转出**：`from src.pipeline import X` 的既有写法全部保持不变。
#    实现按职责分在下面几个子模块里（每个只讲一件事）：
#
#      limiter.py    本地闸门：滑动窗口 / 域名预留 / 限流器工厂 / 换出口
#      record.py     记录与分类：AccountRecord / Ticket / classify / STAGES
#      provision.py  生产者段：建邮箱 → 发码 → 等码
#      finish.py     消费者段：注册 → 登录 → 建 Key → 只读校验
#      batch.py      编排：生产者池 ‖ 消费者池 + 配额闸门 + 收尾
#
# 🔴 **为什么要拆**：拆之前是单文件 1500+ 行，改一处要在 4 个函数之间追
#    `Ticket` 上的状态（本会话因此出过 2 个 bug）。拆完每个文件只讲一件事。
#
# ⚠ `time` / `_apikey` / `_quota` / `_windows` 这些**模块对象**也在这里转出：
#    用例会通过它们 patch 时钟与推理网关（`monkeypatch.setattr(P.time, ...)`）。
#    转出的是**同一个模块对象**，所以 patch 仍然打到真对象上。

import time

from common import config

from .. import apikey as _apikey
from .. import quota as _quota
from .. import windowstore as _windows
from ..agnes import (
    ERR_MAIL,
    ERR_NETWORK,
    ERR_QUOTA,
    ERR_QUOTA_GUARD,
    ERR_REJECTED,
    ERR_VERIFY,
    AgnesClient,
)
from ..proxypool import SlotPool, SlotPoolEmpty
from ..redact import redact_url
from ..tempmail import TempMailClient, now_ms
from .batch import QUOTA_STREAK_STOP, _failed_stage, _fmt_ms, run_batch
from .finish import finish, verify_app, verify_keys
from .limiter import (
    _DOMAIN_LIMITERS,
    _DOMAIN_USED,
    _LIMITER_LOCK,
    _REGISTER_LIMITERS,
    REQUESTS_PER_ACCOUNT,
    DomainReservation,
    SlidingWindow,
    WindowBusy,
    _is_domain_limit,
    _is_ip_limit,
    _pick_domain_with_room,
    _register_backoff_s,
    _rotate_slot,
    _slot_key,
    domain_limiter,
    pick_domain,
    register_limiter,
)
from .provision import provision
from .record import (
    STAGES,
    AccountRecord,
    Ticket,
    _as_float,
    _as_int,
    _print,
    classify,
    error_kind_of,
    make_password,
)

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
