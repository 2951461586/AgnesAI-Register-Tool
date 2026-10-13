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
# 🔴 Agnes 还有一道**域名白名单**（实测，同一台 Worker 的 7 个域名里）：
#     允许   ：own.example / sub.own.example / own2.example
#     明确拒 ：own4.example / own5.example / own6.example
#              → `400 This email domain looks invalid or abusive and is not
#                 allowed for registration`
#     未测清 ：own3.example（测到的是 IP 限流，不是域名拒绝）
#     ⚠ 白名单是**平台侧**的、随时可能变。别把"允许"当承诺。
#
# 取值来源：`AR_MAIL_DOMAINS`（逗号分隔）> `AR_WORKER_DOMAIN`（单个）。
# 换域名池之前先跑 `tools/probes/probe_domains.py`（⚠ 消耗注册窗口）。
MAIL_DOMAINS = [d.strip() for d in os.getenv("AR_MAIL_DOMAINS", "").split(",") if d.strip()]

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
REQUEST_TIMEOUT = _env_float("AR_REQUEST_TIMEOUT", 30)
# 推理网关比控制台慢（首 token 可能要几秒），单独给一个更宽的预算。
VERIFY_TIMEOUT = _env_float("AR_VERIFY_TIMEOUT", 60)

# ── 注册尝试限流（🔴 实测：**两道**按不同维度的窗口）────────────────
#
# 实测证据（2026-10-13，本机 + 两个槽位出口）：
#
#   ① **IP 维度**：`400 Too many registration attempts from this IP. ...`
#      本机单出口实测：一次 ~16 连发触发，且 **t+45 分钟仍被拦** ⇒ 窗口 > 45 分钟。
#      ⚠ 窗口长度这条事实的**唯一真源是 `docs/protocol.md` §3**，本注释只引用它；
#        别在这里另写数字（曾漂移成「一处 >4 分钟 / 一处 >45 分钟 / 一处默认 600s」）。
#   ② **域名维度**：`400 Too many registration attempts from this email domain. ...`
#      用**全新的出口 IP** 打已被用过的域名（`own.example`）仍然命中它
#      ⇒ 它**不是**按 IP 计的（至少不是仅按 IP）。换出口解决不了。
#
# 两者**共用** `/api/verification` 与 `/api/user/register` 两个端点。
# `login` 不受影响（连打 6 次错误密码 → 全 401）。
# 请求体**格式**错误（密码 < 8 / 两次不一致 / code < 4 位）会先返回校验错误，
# **不**触发限流 ⇒ 限流检查在 schema 校验之后。
#
# ⚠ 两道窗口的**长度与阈值都还没测准**（见 docs/protocol.md）。已知 IP 窗口
#   > 45 分钟；域名的未知。所以本地闸门取保守值，且**分别按维度分桶**：
#     · `register_limiter(slot)`   —— IP 维度，按出口分桶
#     · `domain_limiter(domain)`   —— 域名维度，**全局**分桶（保守：宁可少打）
#   撞到服务端消息时：IP 维度 → 换出口；域名维度 → 换域名；换不动才退避。
REGISTER_MAX_PER_WINDOW = max(1, _env_int("AR_REGISTER_MAX_PER_WINDOW", 2))
REGISTER_WINDOW_S = max(1.0, _env_float("AR_REGISTER_WINDOW_S", 3600.0))
# 域名维度的窗口。与 IP 维度**分开配** —— 它们的真实长度未必相同，
# 合成一个数会让"调 IP 的"顺手把域名的也改了。
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
    if not WORKER_ADMIN_TOKEN:
        missing.append("AR_WORKER_ADMIN_TOKEN")
    if not WORKER_BASE:
        missing.append("AR_WORKER_BASE")
    if not WORKER_DOMAIN:
        missing.append("AR_WORKER_DOMAIN")
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
    return missing


def mail_domains() -> list[str]:
    """可用的收信域名列表（**唯一真源**）。

    优先级：`AR_MAIL_DOMAINS` > `AR_WORKER_DOMAIN`。
    两者都空时返回空列表 —— 由 `validate()` 在入口报错，而不是在这里抛。
    """
    if MAIL_DOMAINS:
        return list(MAIL_DOMAINS)
    return [WORKER_DOMAIN] if WORKER_DOMAIN else []
