"""全局配置。

所有密钥类信息集中在此，可通过环境变量或项目根的 `.env` 文件覆盖。

🔴 凭据**不写死在代码里** —— 本项目托管在公开仓库，写死等于直接泄漏。
   首次使用：`cp .env.example .env`，填入真实值。`.env` 已在 .gitignore 中。

与 Intern-Register-Tool / Hark-Register-Tool 的差异（都是刻意的）
==============================================================
1. **前缀换成 `AR_`**。三台工具可能共用同一台 CF Worker（同一个邮箱域名），
   但配置各归各的：共用前缀会让"我只改了 agnes 的配额上限"变成"顺手把
   intern 的也改了"。
2. **只有 Worker 一个邮箱源**，没有 `IR_MAILBOX_KIND` 那套可插拔抽象。
   那套存在的唯一理由是 OpenXLab 有**域名门**（`A0232`）；Agnes 实测
   **没有域名门**（`@own.example` 直接过），所以不引入用不上的抽象。
3. **没有浏览器阶段**。Agnes 的注册/登录全是 JSON API，无验证码、无 WAF 解盾
   （实测 register/login/token 三个写接口都是裸 JSON 200），所以整条链路
   **零浏览器**，运行依赖只有 `requests`。
4. **限流是"按 IP 的注册尝试窗口"**（实测，见 `REGISTER_MAX_PER_WINDOW`）。
   这是本项目加代理槽位池的**唯一理由**：窗口按出口各有一张，
   所以"有多少个不同出口"才是吞吐的乘数。
"""

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """极简 `.env` 解析（stdlib 实现，不引 python-dotenv 依赖）。

    只填充「尚未存在于 os.environ」的键 —— 真实环境变量优先级更高，
    这样临时覆盖（`AR_REG_QUOTA_MAX=1 python run.py`）依然生效。
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


ROOT = Path(__file__).resolve().parents[1]
_load_dotenv(ROOT / ".env")


def _env_int(name: str, default: int) -> int:
    """读整数环境变量。**坏值退回默认**，不抛。

    ⚠ 刻意不抛：这些是"行为参数"，一个手滑写进去的 `AR_PASSWORD_LEN=abc`
    不该让整个工具起不来 —— 它会在启动校验之后、第一个请求之前以
    `ValueError: invalid literal for int()` 的形态炸在很深的地方。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


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
#
# 🔴 Agnes 还有一道**域名白名单**（实测 2026-10-13 首测 / 2026-10-14 复测）：
#     允许（4 个）：own.example / sub.own.example / own2.example / own3.example
#     明确拒（3 个）：own4.example / own5.example / own6.example
#              → `400 This email domain looks invalid or abusive and is not
#                 allowed for registration`
#
#     🔴 **域名数是吞吐的硬上限**：域名维度 2 格 = 1 个账号（见
#       `DOMAIN_MAX_PER_WINDOW`），所以可用域名数直接决定每小时能跑几个。
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

# ── Agnes 站点 ────────────────────────────────────────────────────
# 🔴 **两个域名**：业务 API 在 `platform-backend.agnes-ai.com`，
#    而浏览器里的页面在 `platform.agnes-ai.com`（Origin / Referer 要指它）。
#    实测后端**不校验** Origin（裸 requests 也 200），但带上更接近真实流量。
PLATFORM_BASE = os.getenv("AR_PLATFORM_BASE", "").strip() or "https://platform-backend.agnes-ai.com"
CONSOLE_BASE = os.getenv("AR_CONSOLE_BASE", "").strip() or "https://platform.agnes-ai.com"

# ── API Key 专用推理网关 ──────────────────────────────────────────
# 官方文档给的国际站 Base URL（OpenAI 兼容）。API Key 只在这里有效 ——
# 拿它打 `platform-backend` 会 401（那是控制台 JWT 的地盘）。
APIHUB_BASE = os.getenv("AR_APIHUB_BASE", "").strip() or "https://apihub.agnes-ai.com/v1"
# 校验 key 用的模型与提示词。选 flash 是因为它便宜、且实测可用
# （`agnes-2.5-flash` 在 `GET /v1/models` 与 `POST /v1/chat/completions` 上都过）。
VERIFY_MODEL = os.getenv("AR_VERIFY_MODEL", "").strip() or "agnes-2.5-flash"
# 是否在 `verify` 阶段真的打一次 chat/completions。
# 🔴 默认开：只 `GET /v1/models` 不足以证明 key 能**计费调用**（有些平台对
#    无效 key 也在 models 上放行）。一次 1~8 token 的调用成本可以忽略。
VERIFY_CHAT = _env_bool("AR_VERIFY_CHAT", True)

# ── 注册资料 ──────────────────────────────────────────────────────
# 密码长度。服务端硬要求 >= 8（实测 `Ab1` → `Password must be at least 8 characters`）。
PASSWORD_LEN = max(8, _env_int("AR_PASSWORD_LEN", 16))
# 创建的 API Key 名称。
KEY_NAME = os.getenv("AR_KEY_NAME", "").strip() or "auto"

# ── 行为参数 ──────────────────────────────────────────────────────
# 验证码邮件实测在发信后 **1~3s** 到达（Worker 侧 `received_at` 与请求时刻
# 相差约 1.5s），轮询间隔 0.8s 可在 1~2 次内命中，而 `/api/inbox` 单次往返
# 约 0.3~0.6s，再密就只是在打 Cloudflare。
MAIL_POLL_INTERVAL = _env_float("AR_MAIL_POLL_INTERVAL", 0.8)
MAIL_POLL_TIMEOUT = _env_int("AR_MAIL_POLL_TIMEOUT", 120)
# 建地址（`create_mailbox`）失败后的重试次数 —— **只对连接类错误与 5xx**。
#
# 🔴 为什么需要它：实测 5 个生产者并发打 Worker 时，Cloudflare 会**掉连接**
#    （`ConnectionResetError 10054` / `RemoteDisconnected` / 非 JSON 错误页），
#    而 `create_mailbox` 原先**一次不成就判死** —— 整条账号链跟着失败。
#    10 批实测里有 8 次失败是这个原因，且它发生在**收信侧**（直连，不走代理），
#    所以换出口没用。重试的代价是一次往返，不重试的代价是一个账号。
# 4xx（凭据错 / 域名不存在）**不重试**：那是我们的问题，重试只会把
# "配置错了"伪装成"偶发失败"。
MAIL_RETRIES = max(0, _env_int("AR_MAIL_RETRIES", 2))
REQUEST_TIMEOUT = _env_float("AR_REQUEST_TIMEOUT", 30)
# 推理网关比控制台慢（首 token 可能要几秒），单独给一个更宽的预算。
VERIFY_TIMEOUT = _env_float("AR_VERIFY_TIMEOUT", 60)

# ── 注册尝试限流（🔴 实测：**两道**按不同维度的窗口）────────────────
#
# 实测证据（2026-10-13，本机 + 两个槽位出口）：
#
#   ① **IP 维度**：`400 Too many registration attempts from this IP. ...`
#      本机单出口实测：一次 ~16 连发即触发，且**很久之后仍被拦**。
#      ⚠ 窗口长度这条事实的**唯一真源是 `docs/protocol.md` §3**，本注释只引用它、
#        **不写数字** —— 它曾漂移成三处互不相同的值（`>4 分钟` / `>45 分钟` /
#        默认 `600s`），而那正是本文件现在不写数字的原因。有守卫用例钉住这点：
#        `tests/test_doc_contract.py`。
#   ② **域名维度**：`400 Too many registration attempts from this email domain. ...`
#      用**全新的出口 IP** 打已被用过的域名（`own.example`）仍然命中它
#      ⇒ 它**不是**按 IP 计的（至少不是仅按 IP）。换出口解决不了。
#
# 两者**共用** `/api/verification` 与 `/api/user/register` 两个端点。
# `login` 不受影响（连打 6 次错误密码 → 全 401）。
# 请求体**格式**错误（密码 < 8 / 两次不一致 / code < 4 位）会先返回校验错误，
# **不**触发限流 ⇒ 限流检查在 schema 校验之后。
#
# ⚠ 两道窗口的**长度与阈值都还没测准**（具体下界见 `docs/protocol.md` §3）。
#   所以本地闸门取保守值，且**分别按维度分桶**：
#     · `register_limiter(slot)`   —— IP 维度，按出口分桶
#     · `domain_limiter(domain)`   —— 域名维度，**全局**分桶（保守：宁可少打）
#   撞到服务端消息时：IP 维度 → 换出口；域名维度 → 换域名；换不动才退避。
#
# 🔴 单位是「**请求**」，不是一个账号一枪：`/api/verification` 与
#    `/api/user/register` 共用同一张计数，所以 **2 次 = 1 个账号**。
#    想跑 N 个账号就把上限调到 ≥ `REQUESTS_PER_ACCOUNT * N`。
#    这个单位被误读过（"2 次" → "2 个账号"），而误读的后果不是报错，
#    是"码烧了、注册进不去"。
#
# 🔴🔴 **一个账号要 `REQUESTS_PER_ACCOUNT` 格，所以上限按这个数**量化**：
#
#      MAX=1  → **0 个账号**（连一个账号都装不下）
#      MAX=2  → 1 个账号
#      MAX=3  → **1 个账号**（第 3 格永远用不到 —— 静默浪费）
#      MAX=4  → 2 个账号
#
#    奇数配置不会报错，只会**静默**少一半容量。所以 `validate()` 拦 < 2，
#    `lint()` 对奇数发警告。改这里请连带改 `REQUESTS_PER_ACCOUNT` 的语义说明。
REQUESTS_PER_ACCOUNT = 2
REGISTER_MAX_PER_WINDOW = max(1, _env_int("AR_REGISTER_MAX_PER_WINDOW", 2))
REGISTER_WINDOW_S = max(1.0, _env_float("AR_REGISTER_WINDOW_S", 3600.0))
# 域名维度的窗口。与 IP 维度**分开配** —— 它们的真实长度未必相同，
# 合成一个数会让"调 IP 的"顺手把域名的也改了。
# 🔴 与 IP 维度不同的是：域名维度在注册时**换不动**（邮箱里已经写死了），
#    所以 `provision` 必须**一次占够 REQUESTS_PER_ACCOUNT 格**才能发码。
DOMAIN_MAX_PER_WINDOW = max(1, _env_int("AR_DOMAIN_MAX_PER_WINDOW", 2))
DOMAIN_WINDOW_S = max(1.0, _env_float("AR_DOMAIN_WINDOW_S", 3600.0))
# 本地闸门"等不起就换维度"的阈值（秒）。
# 🔴 为什么需要它：两道窗口都可能是**小时级**的，抱着闸门 `sleep` 会把批量
#    拖死。超过这个时长就不等了 —— 换出口 / 换域名（换不动才封顶退避）。
LIMITER_MAX_WAIT_S = _env_float("AR_LIMITER_MAX_WAIT_S", 20.0)
# 撞到服务端注册限流后最多再试几次（每次先换出口、换不动才退避一个窗口）。
# ⚠ 上限存在是因为"限流会自己好"只对**窗口**成立；若平台改了策略，
#    无限重试就是无限白打枪。
REGISTER_RETRIES = max(0, _env_int("AR_REGISTER_RETRIES", 3))

# 站点的 Cloudflare 边缘会按 UA 判人机（Intern / Hark 两侧都实测过：
# 不带浏览器 UA 的裸请求会被边缘 403）。所以这里给一个真实的浏览器 UA。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

# ── 注册配额保护（本地累计计数）───────────────────────────────────
# 🔴 先弄清它的**角色**：真正的限流是**服务端的两道窗口**（IP 维度 +
#    域名维度，见 `REGISTER_MAX_PER_WINDOW`）。本项管的是
#    "**一批最多开多少个**" —— 它是**爆炸半径上限**，防的是
#    "手滑跑了 500 个"那种事，**不是**服务端配额。
#
#    默认 30 ≈ 一次合理跑批的上限。超过它就该是有意的动作
#    （显式加 --ignore-quota 或调大本值），而不是"忘了配"。
#
# ⚠ 仍然是**本地**的：换机器 / 删 `state/` 都会重置。
REG_QUOTA_MAX = _env_int("AR_REG_QUOTA_MAX", 30)
REG_QUOTA_WINDOW_H = _env_float("AR_REG_QUOTA_WINDOW_H", 24)

# ── 出口代理（可选）───────────────────────────────────────────────
# 格式（与常见代理商控制台一致）：
#     AR_PROXY=host:port:user:pass          # 单条
#     AR_PROXY=http://user:pass@host:port   # 也接受完整 URL
#     AR_PROXY=socks5h://user:pass@host:port # SOCKS5（见下面的 🔴）
#
# 🔴 **SOCKS5 必须写 `socks5h://`，不能写 `socks5://`** —— 否则会**静默失败**：
#
#     `socks5://`   → DNS **在本地**解析，把解析结果发给代理
#     `socks5h://`  → DNS **在代理侧**解析（h = hostname）
#
#     本机实测（2026-10-14）：开着 Clash TUN 时，代理域名 `<proxy-gateway>`
#     被本地 DNS 解析成 **`198.18.1.166`**（Clash 的 fake-IP 段），于是
#     `socks5://` 把那个**假 IP** 发给代理 ⇒ `0x04 Host unreachable`；
#     换 `socks5h://` 立刻通。
#     ⚠ 同一现象还有第二个面具：裸 TCP 连代理端口只要 **0.03s** —— 那是连到了
#       Clash 的本地虚拟网卡。**"TCP 能连" ≠ "代理可用"**（与 `probe_slots`
#       的 docstring 里那条是同一个坑）。
#
# 🔴 SOCKS 需要 `requests[socks]`（PySocks），见 requirements.txt。
AR_PROXY = os.getenv("AR_PROXY", "").strip()
# 收信轮询是否也走代理（默认否）。轮询是整链瓶颈，绕代理只会更慢；
# 而 Cloudflare Worker 不关心我们的出口 IP，走代理没有任何收益。
PROXY_MAIL = _env_bool("AR_PROXY_MAIL", False)

# ── 槽位代理池（多出口并发）────────────────────────────────
# 🔴 它是**两个杠杆之一**，理由是一条**实测事实**：注册尝试限流有
#    **IP 维度**那一半（见上），所以每个出口各自有一张窗口 ⇒
#    "有多少个不同出口"是吞吐的乘数之一，只调大 `--producers` 是没用的
#    （那是同一张窗口里的名额）。
#    ⚠ 另一半是**域名维度**（`AR_MAIL_DOMAINS`）—— 两者必须一起用：
#      只加出口不加域名会撞域名窗口，只加域名不加出口会撞 IP 窗口。
#
# 来源优先级：`AR_PROXY_SLOTS_FILE` > `AR_PROXY_SLOTS`。槽位多时用文件。
AR_PROXY_SLOTS = os.getenv("AR_PROXY_SLOTS", "").strip()
AR_PROXY_SLOTS_FILE = os.getenv("AR_PROXY_SLOTS_FILE", "").strip()
# 等一个空闲槽位最多等多久（秒）。全部槽位都被占着时 `acquire()` 会阻塞。
# ⚠ 别设成 0/无穷：0 会让"全忙"瞬间变成一片假失败；无穷会让批量任务永远挂在那里。
PROXY_SLOT_TIMEOUT = _env_float("AR_PROXY_SLOT_TIMEOUT", 240)


def proxy_slots() -> list[str]:
    """读槽位清单。**没配置就返回空列表**（调用方据此退回单代理行为）。

    文件里允许一行一个，也允许逗号分隔；`#` 开头视为注释。

    🔴 注释必须**按行**判定，不能先把全文按逗号切分再逐项看 `#` ——
    后者会让注释行里的 ASCII 逗号切出**不带 `#` 的碎片**，碎片被当成槽位 URL
    （Intern-Register-Tool 实测：一行「# 端口是 7131,7119,7149」凭空产出 3 个
    假槽位，报错还是「端口 7119 的出口 IP 未知」，指向注释里的数字，极难回溯）。
    所以顺序是：先按行 → 丢掉空行/注释行 → 再在行内按逗号切。
    """
    raw = ""
    if AR_PROXY_SLOTS_FILE:
        p = Path(AR_PROXY_SLOTS_FILE)
        if not p.is_file():
            raise ValueError(f"AR_PROXY_SLOTS_FILE 指向的文件不存在：{p}")
        raw = p.read_text(encoding="utf-8")
    elif AR_PROXY_SLOTS:
        raw = AR_PROXY_SLOTS
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for item in line.split(","):
            item = item.strip()
            if item and not item.startswith("#"):
                out.append(item)
    return out


def proxies(raw: str | None = None) -> dict | None:
    """把代理串解析成 requests 的 `proxies` 字典；空则返回 `None`。

    支持两种写法：
        host:port:user:pass          → http://user:pass@host:port
        scheme://user:pass@host:port → 原样使用（可指定 socks5）

    `raw` 传 None 时用全局 `AR_PROXY`。
    """
    raw = (AR_PROXY if raw is None else raw).strip()
    if not raw:
        return None
    if "://" not in raw:
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            raw = f"http://{user}:{pwd}@{host}:{port}"
        elif len(parts) == 2:
            raw = f"http://{raw}"
        else:
            raise ValueError(
                f"代理串格式无法识别：{raw!r}（期望 host:port:user:pass "
                f"或 scheme://user:pass@host:port）"
            )
    return {"http": raw, "https": raw}


def apply_proxy(session, proxy: str | None = None) -> None:
    """把代理挂到 `requests.Session` 上（未配置则什么都不做）。

    🔴 **必须同时关掉 `trust_env`** —— 否则环境变量会**静默盖掉**
    `session.proxies`。机制在 `requests/sessions.py` 的
    `merge_environment_settings`：环境变量先 `setdefault` 进字典，
    而 `merge_setting(proxies, self.proxies)` **不覆盖已存在的键** ——
    于是 `session.proxies` 的优先级**低于**环境变量。

    现象特别隐蔽：请求**成功了**（200），只是**没走你指定的代理** ——
    既不报错也不告警，很容易误判成"代理已生效"。

    ⚠ 未配置代理时**不动 `trust_env`**：那样环境代理（本机 Clash）仍是
      默认出口，与加这个功能之前的行为一致。
    """
    px = proxies(proxy)
    if not px:
        return
    session.proxies = px
    session.trust_env = False


# ── 启动校验 ──────────────────────────────────────────────────────
def validate() -> list[str]:
    """返回缺失 / 有问题的必需配置项（空列表 = 就绪）。

    刻意**不在 import 时抛错** —— 那样连 `--help` 和离线分析都跑不起来。
    由入口显式调用，报错时直接给出修法。
    """
    missing = []
    # ⚠ 凭据检查**按启用的源**来：只开 `catchmail` 时不需要 Worker 的三项
    #   （它免鉴权）—— 无条件要求它们会让人以为"必须填"，而那是假话。
    if "worker" in MAIL_SOURCES:
        if not WORKER_ADMIN_TOKEN:
            missing.append("AR_WORKER_ADMIN_TOKEN")
        if not WORKER_BASE:
            missing.append("AR_WORKER_BASE")
        if not WORKER_DOMAIN:
            missing.append("AR_WORKER_DOMAIN")
    if "smailr" in MAIL_SOURCES and not SMAILR_API_KEY:
        missing.append("AR_SMAILR_API_KEY（启用 smailr 源时必需）")
    # 源名写错是配置错误，且**必须**报出来：`source_of()` 对未知域名一律
    # 回落到 `worker`，所以一个拼错的源名会静默变成"用 Worker"。
    unknown = [s for s in MAIL_SOURCES if s not in MAIL_SOURCE_KINDS]
    if unknown:
        missing.append(
            f"AR_MAIL_SOURCES（未知取值 {unknown}，只认 {' / '.join(MAIL_SOURCE_KINDS)}）"
        )
    # 一个域名都没有 ⇒ 一个账号都跑不出来（`provision` 会抛 NO_DOMAIN）。
    if not mail_domains():
        missing.append("AR_MAIL_SOURCES / AR_MAIL_DOMAINS（没有任何可用收信域名）")
    # 槽位文件读不到是配置错误，但用"缺项"的形式报出来比抛 traceback 可读得多。
    try:
        proxy_slots()
    except (ValueError, OSError) as ex:
        missing.append(f"AR_PROXY_SLOTS_FILE（读不到：{ex}）")
    # 密码长度是服务端硬要求（>= 8）。配置写小了会在**每个账号**上撞一次
    # `Password must be at least 8 characters` —— 提前到启动阶段报出来。
    if PASSWORD_LEN < 8:
        missing.append(f"AR_PASSWORD_LEN（{PASSWORD_LEN} < 8，服务端会拒绝）")
    if not KEY_NAME:
        missing.append("AR_KEY_NAME（API Key 名不能为空）")
    # 域名池：显式配了 `AR_MAIL_DOMAINS` 却解析出空列表（比如只写了逗号），
    # 那是配置错误 —— 会在第一个账号上以"create_mailbox failed"的形式炸出来。
    if os.getenv("AR_MAIL_DOMAINS", "").strip() and not MAIL_DOMAINS:
        missing.append("AR_MAIL_DOMAINS（写了但解析出空列表）")
    # 🔴 窗口比"一个账号要的格数"还小 ⇒ **永远跑不出账号**（`floor(MAX/2) == 0`）。
    #    这是个**静默失效**：不报错、不警告，只是每个账号都被本地闸门 skip 掉，
    #    现象看着像"域名全被限流"。所以提前到启动阶段报出来。
    for name, val in (
        ("AR_REGISTER_MAX_PER_WINDOW", REGISTER_MAX_PER_WINDOW),
        ("AR_DOMAIN_MAX_PER_WINDOW", DOMAIN_MAX_PER_WINDOW),
    ):
        if val < REQUESTS_PER_ACCOUNT:
            missing.append(
                f"{name}（{val} < {REQUESTS_PER_ACCOUNT}，一个账号要 "
                f"{REQUESTS_PER_ACCOUNT} 次注册类请求 ⇒ 永远跑不出账号）"
            )
    return missing


def lint() -> list[str]:
    """返回**非致命**的配置警告（空列表 = 没问题）。

    与 `validate()` 的分工：`validate()` 是"不修就跑不了"（入口直接退出）；
    这里是"能跑，但你在浪费容量 / 大概率是误解了单位"，只打一行提醒。

    🔴 为什么单独一个函数而不是塞进 `validate()`：`validate()` 的每一项都会
      让入口 `return 4`。把"奇数窗口"这种**能跑但浪费**的事做成硬失败，
      会让用户以为工具坏了 —— 而那正是它要防的那种沟通失败。
    """
    warn = []
    # 奇数上限：`floor(MAX / REQUESTS_PER_ACCOUNT)` 会静默丢掉余数。
    # 实测（2026-10-14）本项目的配置注释曾把 "2 次" 读成 "2 个账号"，
    # 而 `MAX=3` 看起来像"3 个账号"，实际只有 1 个。
    for name, val in (
        ("AR_REGISTER_MAX_PER_WINDOW", REGISTER_MAX_PER_WINDOW),
        ("AR_DOMAIN_MAX_PER_WINDOW", DOMAIN_MAX_PER_WINDOW),
    ):
        # ⚠ `val < REQUESTS_PER_ACCOUNT` 已经被 `validate()` 硬拦了，
        #   这里不再重复报 —— 否则会给出"建议改成 0"这种**没意义**的建议
        #   （0 格等于关掉闸门），而一条荒谬的建议会让人不再信任整个 lint。
        if val < REQUESTS_PER_ACCOUNT:
            continue
        if val % REQUESTS_PER_ACCOUNT:
            usable = val - val % REQUESTS_PER_ACCOUNT
            warn.append(
                f"{name}={val} 是奇数 ⇒ 只有前 {usable} 格能用，"
                f"第 {val} 格永远用不到（= {usable // REQUESTS_PER_ACCOUNT} 个账号）。"
                f"建议改成 {usable} 或 {usable + REQUESTS_PER_ACCOUNT}。"
            )
    return warn


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
