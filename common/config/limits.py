"""注册尝试限流 + 本地配额 + **启动校验**（`validate()` / `lint()`）。

（从 `common/config.py` 拆出；对外仍由 `common.config` 包统一转出。）
"""

import importlib.util
import os

# 🔴 跨模块引用**走模块对象**（`_mail._mail.MAIL_SOURCES`）而不是 `from .mail import ...`：
#    后者会把值复制一份，于是测试 `monkeypatch.setattr(mail, "_mail.MAIL_SOURCES", ...)`
#    改不到 `validate()` 里读的那一份 —— 一个"改了没生效"的静默陷阱。
from . import captcha as _captcha
from . import mail as _mail
from . import proxy as _proxy
from . import site as _site
from ._env import _env_float, _env_int

# ── 注册尝试限流（🔴 实测：**两道**按不同维度的窗口）────────────────
#
# 实测证据（2026-10-13 首测 / 2026-10-18 复测）：
#
#   ① **IP 维度**：`400 Too many registration attempts from this IP. ...`
#      2026-10-18 实测 **阈值 ≈4**（同一出口 4 枪全过、第 5 枪 IP 限流）。
#      ⚠ 旧注释里的 "一次 ~16 连发即触发" **不可信** —— 那次突发固定用
#        一个域名，先撞的是域名维度。
#      ⚠ 窗口长度**仍未测准**（受控实测 >16 分钟）。唯一真源是
#        `docs/protocol.md` §3；本注释只引用它、不写数字
#      —— 它曾漂移成三处互不相同的值（`>4 分钟` / `>45 分钟` / 默认 `600s`），
#      而那正是本文件现在不写数字的原因。有守卫用例钉住这点：
#      `tests/test_doc_contract.py`。
#   ② **域名维度**：`400 Too many registration attempts from this email domain. ...`
#      用**全新的出口 IP** 打已被用过的域名仍然命中它
#      ⇒ 它**不是**按 IP 计的（至少不是仅按 IP）。换出口解决不了。
#      2026-10-18 实测：**阈值 ≈10 次/窗口**，且 10 个不同出口一起计数
#      ⇒ 计数是全局的（但有 0.5s 级反证 ⇒ 最终一致）。详见 §3。
#
# 两者**共用** `/api/verification` 与 `/api/user/register` 两个端点。
# `login` 不受影响（连打 6 次错误密码 → 全 401）。
# 请求体**格式**错误（密码 < 8 / 两次不一致 / code < 4 位）会先返回校验错误，
# **不**触发限流 ⇒ 限流检查在 schema 校验之后。
#
# ⚠ **域名阈值 ≈10、IP 阈值 ≈4** 已实测；**窗口长度只有下界（>16 分钟）**。
#   所以本地闸门取**保守值**（见下面的默认值），且**分别按维度分桶**：
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
# 🔴 2026-10-18 实测（两次独立复测）：
#    · **域名阈值 ≈10 次/窗口**（9 枪全过、第 10 枪域名限流；10 个不同出口
#      一起计数 ⇒ 全局）。本地取 **8**（4 个账号）留 2 格余量。
#    · **IP 阈值 ≈4 次/窗口**（同一出口 4 枪全过、第 5 枪 `from this IP`）。
#      本地取 **4**（2 个账号）—— 这是**贴着实测阈值**的激进值；如果频繁撞
#      `from this IP`，调回 **2**（1 个账号）。旧 "~16" 是**域名维度污染**
#      出来的假数，不要拿它当 IP 阈值。
#    ⚠ 窗口长度**仍未有上界**：受控实测 **>45.37 分钟**（16.14 / 45.37 分钟
#      两次都仍限流；旧 ">45 分钟" 被证实为下界）。所以 `*_WINDOW_S` 保持
#      3600s（只比下界多 ~15 分钟余量；要绝对安全调到 5400s+）——
#      唯一真源见 `docs/protocol.md` §3。
REGISTER_MAX_PER_WINDOW = max(1, _env_int("AR_REGISTER_MAX_PER_WINDOW", 4))
REGISTER_WINDOW_S = max(1.0, _env_float("AR_REGISTER_WINDOW_S", 3600.0))
# 域名维度的窗口。与 IP 维度**分开配** —— 它们的真实长度未必相同，
# 合成一个数会让"调 IP 的"顺手把域名的也改了。
# 🔴 与 IP 维度不同的是：域名维度在注册时**换不动**（邮箱里已经写死了），
#    所以 `provision` 必须**一次占够 REQUESTS_PER_ACCOUNT 格**才能发码。
DOMAIN_MAX_PER_WINDOW = max(1, _env_int("AR_DOMAIN_MAX_PER_WINDOW", 8))
DOMAIN_WINDOW_S = max(1.0, _env_float("AR_DOMAIN_WINDOW_S", 3600.0))
# 本地闸门"等不起就换维度"的阈值（秒）。
# 🔴 为什么需要它：两道窗口都可能是**小时级**的，抱着闸门 `sleep` 会把批量
#    拖死。超过这个时长就不等了 —— 换出口 / 换域名（换不动才封顶退避）。
LIMITER_MAX_WAIT_S = _env_float("AR_LIMITER_MAX_WAIT_S", 20.0)
# 撞到服务端注册限流后最多再试几次（每次先换出口、换不动才退避一个窗口）。
# ⚠ 上限存在是因为"限流会自己好"只对**窗口**成立；若平台改了策略，
#    无限重试就是无限白打枪。
REGISTER_RETRIES = max(0, _env_int("AR_REGISTER_RETRIES", 3))


# ── 启动校验 ──────────────────────────────────────────────────────
def validate() -> list[str]:
    """返回缺失 / 有问题的必需配置项（空列表 = 就绪）。

    刻意**不在 import 时抛错** —— 那样连 `--help` 和离线分析都跑不起来。
    由入口显式调用，报错时直接给出修法。
    """
    missing = []
    # ⚠ 凭据检查**按启用的源**来：只开 `catchmail` 时不需要 Worker 的三项
    #   （它免鉴权）—— 无条件要求它们会让人以为"必须填"，而那是假话。
    if "worker" in _mail.MAIL_SOURCES:
        if not _mail.WORKER_ADMIN_TOKEN:
            missing.append("AR_WORKER_ADMIN_TOKEN")
        if not _mail.WORKER_BASE:
            missing.append("AR_WORKER_BASE")
        if not _mail.WORKER_DOMAIN:
            missing.append("AR_WORKER_DOMAIN")
    if "smailr" in _mail.MAIL_SOURCES and not _mail.SMAILR_API_KEY:
        missing.append("AR_SMAILR_API_KEY（启用 smailr 源时必需）")
    # 源名写错是配置错误，且**必须**报出来：`source_of()` 对未知域名一律
    # 回落到 `worker`，所以一个拼错的源名会静默变成"用 Worker"。
    unknown = [s for s in _mail.MAIL_SOURCES if s not in _mail.MAIL_SOURCE_KINDS]
    if unknown:
        missing.append(
            f"AR_MAIL_SOURCES（未知取值 {unknown}，只认 {' / '.join(_mail.MAIL_SOURCE_KINDS)}）"
        )
    # 一个域名都没有 ⇒ 一个账号都跑不出来（`provision` 会抛 NO_DOMAIN）。
    if not _mail.mail_domains():
        missing.append("AR_MAIL_SOURCES / AR_MAIL_DOMAINS（没有任何可用收信域名）")
    # 槽位文件读不到是配置错误，但用"缺项"的形式报出来比抛 traceback 可读得多。
    try:
        _proxy.proxy_slots()
    except (ValueError, OSError) as ex:
        missing.append(f"AR_PROXY_SLOTS_FILE（读不到：{ex}）")
    # 密码长度是服务端硬要求（>= 8）。配置写小了会在**每个账号**上撞一次
    # `Password must be at least 8 characters` —— 提前到启动阶段报出来。
    if _site.PASSWORD_LEN < 8:
        missing.append(f"AR_PASSWORD_LEN（{_site.PASSWORD_LEN} < 8，服务端会拒绝）")
    if not _site.KEY_NAME:
        missing.append("AR_KEY_NAME（API Key 名不能为空）")
    # 域名池：显式配了 `AR_MAIL_DOMAINS` 却解析出空列表（比如只写了逗号），
    # 那是配置错误 —— 会在第一个账号上以"create_mailbox failed"的形式炸出来。
    if os.getenv("AR_MAIL_DOMAINS", "").strip() and not _mail.MAIL_DOMAINS:
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
    # captcha：默认开。缺可选依赖时**启动就报**，而不是等第一个账号在浏览器里
    # 炸成 `ModuleNotFoundError`。
    if _captcha.CAPTCHA_ENABLED:
        missing_deps = [
            name
            for name in ("playwright", "numpy", "scipy", "PIL")
            if importlib.util.find_spec(name) is None
        ]
        if missing_deps:
            missing.append(
                f"captcha 已启用（AR_CAPTCHA_ENABLED 默认开）但缺少可选依赖："
                f"{', '.join(missing_deps)}"
                "（装 `pip install -e .[captcha]`，或设 AR_CAPTCHA_ENABLED=0）"
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


__all__ = [
    "REQUESTS_PER_ACCOUNT",
    "REGISTER_MAX_PER_WINDOW",
    "REGISTER_WINDOW_S",
    "DOMAIN_MAX_PER_WINDOW",
    "DOMAIN_WINDOW_S",
    "LIMITER_MAX_WAIT_S",
    "REGISTER_RETRIES",
    "validate",
    "lint",
]
