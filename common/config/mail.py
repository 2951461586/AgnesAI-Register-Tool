"""收信侧配置：CF Worker 凭据、域名池、三个邮箱源、`mail_domains()` / `source_of()`。

（从 `common/config.py` 拆出；对外仍由 `common.config` 包统一转出。）
"""

import os

from ._env import _env_bool

# ── CF Worker 临时邮箱（必填）─────────────────────────────────────
# 与 Intern-Register-Tool 共用同一台 Worker（实测可收 Agnes 的验证码邮件：
# 发件人 `noreply@agnes-ai.com`，主题 `Your Agnes Platform Verification Code`）。
#
# 🔴 为什么这三项没有默认值：本仓库是公开的，写死等于把邮箱服务的
#    管理凭据 + 自有域名一起交出去。缺了会在**启动阶段**报错（`validate()`），
#    而不是带着空 base 去发一堆注定失败的请求。
WORKER_BASE = os.getenv("AR_WORKER_BASE", "").strip()
WORKER_ADMIN_TOKEN = os.getenv("AR_WORKER_ADMIN_TOKEN", "").strip()
WORKER_DOMAIN = os.getenv("AR_WORKER_DOMAIN", "").strip()

# ── 收信域名池（域名轮换）─────────────────────────────────────────
# 🔴 **Agnes 有一道域名维度的限流**（实测）：
#     `400 Too many registration attempts from this email domain. Please try again later`
#    它**按域名**计 —— 用**全新的出口 IP** 打已用过的域名仍然命中。
#    所以"多出口"只解决 IP 维度那一半，另一半要靠**换收信域名**。
#    🔴 2026-10-18 实测：**阈值 ≈10 次/窗口**（不是旧写的 2~3；旧值其实是
#       `DOMAIN_MAX_PER_WINDOW` 的本地默认值，被误当成了服务端阈值）。
#       跨出口一起计数 ⇒ 计数是**全局**的；但存在 0.5s 级的跨出口反证，
#       说明它是**最终一致**的。长度/阈值的唯一真源见 `docs/protocol.md` §3。
#
# 🔴 Agnes 还有一道**域名白名单**（实测 2026-10-13 首测 / 2026-10-14 复测）：
#     允许（4 个）：own.example / sub.own.example / own2.example / own3.example
#     明确拒（3 个）：own4.example / own5.example / own6.example
#              → `400 This email domain looks invalid or abusive and is not
#                 allowed for registration`
#
#     🔴 **域名数是吞吐的硬上限**：`DOMAIN_MAX_PER_WINDOW` 格 = MAX/2 个账号
#       （一个账号要两枪，见 `REQUESTS_PER_ACCOUNT`），所以可用域名数直接
#       决定每小时能跑几个。
#       把被拒的域名加进 `AR_MAIL_DOMAINS` 只会拿到确定性的 `rejected`，
#       还会白烧一格 IP 窗口 —— **没有收益，只有损失**。
#
#     ⚠ 白名单是**平台侧**的、随时可能变。别把"允许"当承诺；换域名池之前
#       先跑 `tools/probes/probe_domains.py`（⚠ 消耗注册窗口）。
#
#     ⚠ `own3.example` 首测（10-13）记为"未测清" —— 当时撞到的是 **IP 限流**，
#       不是域名拒绝。10-14 复测确认**允许**。这正是"未测清 ≠ 被拒"的例子：
#       把限流当成域名被拒，就会白白丢掉一个可用域名。
#
# 取值来源：`AR_MAIL_DOMAINS`（逗号分隔）> `AR_WORKER_DOMAIN`（单个）。
# 换域名池之前先跑 `tools/probes/probe_domains.py`（⚠ 消耗注册窗口）。
MAIL_DOMAINS = [d.strip() for d in os.getenv("AR_MAIL_DOMAINS", "").split(",") if d.strip()]

# ── 邮箱源（**多源**）────────────────────────────────────────────
# 🔴 为什么要"源"这一层：服务端的注册限流有一道**按收信域名**的窗口，
#    所以"有多少个可用域名"直接决定每小时能跑几个账号。而**单个源能提供的
#    域名数有限**（自有域名只有 4 个可用）⇒ 第二个源是**唯一能加域名窗口**的
#    手段（实测：catchmail 一家就给 3 个）。
#
# 取值：`worker`（自有 CF Worker 域名）/ `catchmail`（公共临时邮箱，免鉴权）。
# ⚠ **默认只有 `worker`** —— 换源是**显式动作**（与 Intern 那边的约定一致：
#   别名/隐式回退会让"我只改了 X"变成"顺手把 Y 也改了"）。
MAIL_SOURCES = [
    s.strip().lower() for s in os.getenv("AR_MAIL_SOURCES", "worker").split(",") if s.strip()
]
# 唯一真源：`validate()` / `lint()` 与 `mailbox.py` 的工厂都从这里取 ——
# 避免"校验认得的取值"和"工厂认得的取值"各写一份然后漂移。
MAIL_SOURCE_KINDS = ("worker", "catchmail", "smailr")

# catchmail 的域名。实测（2026-10-14）：3 个**全部过 Agnes 白名单**，
# 且端到端可用（建址→发码→**2s 收到验证码**）。可用 `AR_CATCHMAIL_DOMAINS` 覆盖。
#
# 🔴 与自有域名的**关键差别**：这些是**公共**域名 —— 任何人都能在上面建地址
#    ⇒ 那个域名的服务端窗口**与陌生人共享**，可能被别人的量打满且**不可控**。
#    （同类项目记过 `Mail.tm` 的公共域名 `uberip.com` "周期性被封"。）
#    ⇒ 定位是**补充窗口**，不是替代自有域名。
CATCHMAIL_DOMAINS = [
    d.strip() for d in os.getenv("AR_CATCHMAIL_DOMAINS", "").split(",") if d.strip()
] or ["catchmail.io", "mailistry.com", "zeppost.com"]

# ── smailr 邮箱源 ────────────────────────────────────────────────
# 🔴 它给 **4 个**域名，实测（2026-10-14）**全部过 Agnes 白名单** ⇒ 域名池
#    7 → 11。所以它是三个源里**最值**的一个（catchmail 只给 3 个）。
#
# ⚠ **账号等级是硬门**：不传 `domain_id` 时用账号的**默认域名**，而默认域名可能
#    要求更高等级（实测撞到 `403 该域名要求用户等级 4 及以上，您当前等级为 2`）。
#    ⇒ 账号后台要把「邮箱域名」设成下面这 4 个之一（它们是**等级 1** 就能用的）。
SMAILR_API_KEY = os.getenv("AR_SMAILR_API_KEY", "").strip()
SMAILR_BASE = os.getenv("AR_SMAILR_BASE", "").strip() or "https://smailr.com"
# 等级 1 就能用的 4 个域名（与参考实现 `SMAILR_LV1_DOMAINS` 一致）。
SMAILR_DOMAINS = [
    d.strip().lower() for d in os.getenv("AR_SMAILR_DOMAINS", "").split(",") if d.strip()
] or ["smailr.com", "loc.cc", "mail.nodeloc.cc", "nodeloc.cc"]
# 账号后台里设的那个默认域名 —— 它是**唯一可以省略 `domain_id`** 的域名。
SMAILR_DEFAULT_DOMAIN = os.getenv("AR_SMAILR_DEFAULT_DOMAIN", "").strip().lower() or "smailr.com"
# 非默认域名需要显式给 `domain_id`（API **不可枚举**域名，只能人工拿）。
# 写法：`域名=id,域名2=id2`。
SMAILR_DOMAIN_IDS = {
    k.strip().lower(): v.strip()
    for k, _, v in (
        item.partition("=") for item in os.getenv("AR_SMAILR_DOMAIN_IDS", "").split(",")
    )
    if k.strip() and v.strip()
}
# 🔴 **成功落库后删除该邮箱**（默认开）。理由：smailr 账号有**邮箱总数上限**
#    （实测 `mailbox_limit=100`），而每个账号占一个邮箱 ⇒ 不删会跑几十个就卡死。
#    ⚠ 删除**不可逆**，但它发生在账号已经进台账**之后** —— 台账里存的是 Agnes
#      的凭据（密码/JWT/sk- key），与收信邮箱无关，删掉不影响账号可用性。
SMAILR_DELETE_AFTER_SUCCESS = _env_bool("AR_SMAILR_DELETE_AFTER_SUCCESS", True)


def mail_domains() -> list[str]:
    """**所有启用源**的可用收信域名（**唯一真源**）。

    优先级（`worker` 源内部）：`AR_MAIL_DOMAINS` > `AR_WORKER_DOMAIN`。
    `catchmail` 源额外贡献 `AR_CATCHMAIL_DOMAINS`（默认 3 个）。

    ⚠ 两者都空时返回空列表 —— 由 `validate()` 在入口报错，而不是在这里抛。
    """
    out: list[str] = []
    if "worker" in MAIL_SOURCES:
        out += MAIL_DOMAINS or ([WORKER_DOMAIN] if WORKER_DOMAIN else [])
    if "catchmail" in MAIL_SOURCES:
        out += CATCHMAIL_DOMAINS
    if "smailr" in MAIL_SOURCES:
        out += SMAILR_DOMAINS
    # 去重但保序（同一个域名可能被两个源都列到）
    seen: set[str] = set()
    return [d for d in out if not (d.lower() in seen or seen.add(d.lower()))]


def source_of(domain: str) -> str:
    """域名 → 邮箱源名（`worker` / `catchmail`）。

    🔴 这是"**域名决定源**"这条规则的**唯一实现**：`provision` 拿到域名后靠它
      决定用哪个客户端。写两处必然漂移，而漂移的形态是"拿 Worker 的客户端去
      catchmail 的域名上建地址" —— 报错会指向一个不存在的问题。
    """
    d = (domain or "").strip().lower()
    if d and d in {x.lower() for x in CATCHMAIL_DOMAINS}:
        return "catchmail"
    if d and d in {x.lower() for x in SMAILR_DOMAINS}:
        return "smailr"
    return "worker"


__all__ = [
    "WORKER_BASE",
    "WORKER_ADMIN_TOKEN",
    "WORKER_DOMAIN",
    "MAIL_DOMAINS",
    "MAIL_SOURCES",
    "MAIL_SOURCE_KINDS",
    "CATCHMAIL_DOMAINS",
    "SMAILR_API_KEY",
    "SMAILR_BASE",
    "SMAILR_DOMAINS",
    "SMAILR_DEFAULT_DOMAIN",
    "SMAILR_DOMAIN_IDS",
    "SMAILR_DELETE_AFTER_SUCCESS",
    "mail_domains",
    "source_of",
]
