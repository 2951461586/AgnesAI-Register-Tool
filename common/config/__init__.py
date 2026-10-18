"""全局配置。

所有密钥类信息集中在此，可通过环境变量或项目根的 `.env` 文件覆盖。

🔴 凭据**不写死在代码里** —— 本项目托管在公开仓库，写死等于直接泄漏。
   首次使用：`cp .env.example .env`，填入真实值。`.env` 已在 .gitignore 中。

与 Intern-Register-Tool / Hark-Register-Tool 的差异（都是刻意的）
==============================================================
1. **前缀换成 `AR_`**。三台工具可能共用同一台 CF Worker（同一个邮箱域名），
   但配置各归各的：共用前缀会让"我只改了 agnes 的配额上限"变成"顺手把
   intern 的也改了"。
2. **邮箱源是可插拔的**（`worker` / `catchmail` / `smailr`，见 `MAIL_SOURCE_KINDS`
   与 `src/mailbox.py`）。⚠ 本条曾写“只有 Worker 一个邮箱源”与“Agnes 没有域名门”
   —— **两句都已过时**：平台侧**有一道域名白名单**（可用 / 被拒清单见
   `docs/protocol.md` §2），而“多一个域名 = 多一张域名窗口”是吞吐的硬杠杆，
   所以后来又接了 `catchmail` / `smailr` 两个源。
3. **captcha 是可选 provider，默认开、headless** —— 2026-10-15 16:24 起
   ②发验证码 要求 **captcha**（`400 Captcha verification is required`）；
   2026-10-17 起主流水线接入 `src/captcha.py`（Playwright headless + 点选
   solver），把 `ticket`/`randstr` 交给 `send_code`。**默认开**，所以运行
   依赖变成 `requests` + **可选** `[captcha]` extra（Playwright / numpy /
   scipy / Pillow）；不装就设 `AR_CAPTCHA_ENABLED=0`（但那样发码会被
   服务端拒绝）。实测与未测项见 `docs/protocol.md` §2②。
4. **限流是"按 IP 的注册尝试窗口"**（实测，见 `REGISTER_MAX_PER_WINDOW`）。
   这是本项目加代理槽位池的**唯一理由**：窗口按出口各有一张，
   所以"有多少个不同出口"才是吞吐的乘数。
"""

# 🔴 本文件只做**转出**：`from common import config` 的既有写法保持不变。
#    实现按职责拆在下面的子模块里（每个只讲一件事）：
#
#      _env.py     .env 加载 + 环境变量解析 + 仓库根
#      site.py     站点 / 运行时参数（base、超时、UA、本地配额）
#      mail.py     收信侧（Worker / 域名池 / 三个邮箱源）
#      captcha.py  captcha provider 配置
#      proxy.py    出口代理与槽位池
#      limits.py   注册尝试限流 + 启动校验（validate/lint）
#
# ⚠ `_env` 必须排第一：它在 import 时加载 `.env`，后面的子模块才能读到
#   真实环境变量。其余子模块也会 import `_env`，所以顺序不会破坏这一点。

from ._env import (
    ROOT,
    _env_bool,
    _env_float,
    _env_int,
    _load_dotenv,
)
from .captcha import (
    CAPTCHA_DEBUG_DIR,
    CAPTCHA_ENABLED,
    CAPTCHA_ENTRY_URL,
    CAPTCHA_HEADLESS,
    CAPTCHA_RESOLVE_ATTEMPTS,
    CAPTCHA_RETRIES,
    CAPTCHA_TARGETS,
    CAPTCHA_TIMEOUT_S,
    CAPTCHA_USE_PROXY,
)
from .limits import (
    DOMAIN_MAX_PER_WINDOW,
    DOMAIN_WINDOW_S,
    LIMITER_MAX_WAIT_S,
    REGISTER_MAX_PER_WINDOW,
    REGISTER_RETRIES,
    REGISTER_WINDOW_S,
    REQUESTS_PER_ACCOUNT,
    lint,
    validate,
)
from .mail import (
    CATCHMAIL_DOMAINS,
    MAIL_DOMAINS,
    MAIL_SOURCE_KINDS,
    MAIL_SOURCES,
    SMAILR_API_KEY,
    SMAILR_BASE,
    SMAILR_DEFAULT_DOMAIN,
    SMAILR_DELETE_AFTER_SUCCESS,
    SMAILR_DOMAIN_IDS,
    SMAILR_DOMAINS,
    WORKER_ADMIN_TOKEN,
    WORKER_BASE,
    WORKER_DOMAIN,
    mail_domains,
    source_of,
)
from .proxy import (
    AR_PROXY,
    AR_PROXY_SLOTS,
    AR_PROXY_SLOTS_FILE,
    PROXY_MAIL,
    PROXY_SLOT_TIMEOUT,
    apply_proxy,
    proxies,
    proxy_slots,
)
from .site import (
    APIHUB_BASE,
    CONSOLE_BASE,
    KEY_NAME,
    MAIL_POLL_INTERVAL,
    MAIL_POLL_TIMEOUT,
    MAIL_RETRIES,
    PASSWORD_LEN,
    PLATFORM_BASE,
    REG_QUOTA_MAX,
    REG_QUOTA_WINDOW_H,
    REQUEST_TIMEOUT,
    USER_AGENT,
    VERIFY_CHAT,
    VERIFY_MODEL,
    VERIFY_TIMEOUT,
)
