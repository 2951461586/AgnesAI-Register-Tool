"""站点 / 运行时参数：Agnes 两个 base、推理网关、超时、UA、本地配额上限。

（从 `common/config.py` 拆出；对外仍由 `common.config` 包统一转出。）
"""

import os

from ._env import _env_bool, _env_float, _env_int

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


__all__ = [
    "PLATFORM_BASE",
    "CONSOLE_BASE",
    "APIHUB_BASE",
    "VERIFY_MODEL",
    "VERIFY_CHAT",
    "PASSWORD_LEN",
    "KEY_NAME",
    "MAIL_POLL_INTERVAL",
    "MAIL_POLL_TIMEOUT",
    "MAIL_RETRIES",
    "REQUEST_TIMEOUT",
    "VERIFY_TIMEOUT",
    "USER_AGENT",
    "REG_QUOTA_MAX",
    "REG_QUOTA_WINDOW_H",
]
