"""catchmail.io 邮箱源（**免鉴权** REST）—— 本项目的**第二个**邮箱源。

为什么值得引入
==============
服务端的注册限流有一道**按收信域名**的窗口，所以"有多少个可用域名"直接决定
每小时能跑几个账号。而自有域名只有 4 个（Worker 上 7 个里 3 个被 Agnes 拒）。

catchmail 提供 **3 个**实测能过 Agnes 白名单的域名
（`catchmail.io` / `mailistry.com` / `zeppost.com`）⇒ 域名池 4 → 7。

🔴 与自有域名的**关键差别**（决定了它的定位）
------------------------------------------
这些是**公共**域名 —— 任何人都能在上面建地址 ⇒ 那个域名的服务端窗口
**与陌生人共享**，可能被别人的量打满，且**不可控**。
（参照：同类项目记过 `Mail.tm` 的公共域名 `uberip.com` "周期性被封"。）

⇒ 定位是**补充窗口**，不是替代自有域名。自有域名（独占、可预测）当主力，
  catchmail 当溢出容量 —— 反正撞限流时代码会自动换域名。

API（实测 2026-10-14）
=====================
    GET /api/v1/mailbox?address=<addr>       → {"address":…,"messages":[{id,from,subject,date,size}],…}
    GET /api/v1/message/<id>?mailbox=<addr>  → {…,"body":{"text":…,"html":…},…}

🔴 与 CF Worker 源的**形状差异**（这是它 `create_mailbox` 能直接接上的原因）：
   地址是**我们自己选**的（本地部分随机 + 指定域名），不是服务端分配的。

⚠ 需要 `Referer` / `Origin` 头（`catchmail.io`）—— 裸请求会被拒。
"""

import random
import re
import string
import time
from datetime import datetime

import requests

from common import config

from .tempmail import Mail, extract_code, extract_links

API_BASE = "https://api.catchmail.io/api/v1"
SITE = "https://catchmail.io"

# catchmail 的 3 个域名。实测（2026-10-14）**全部过 Agnes 白名单**，
# 且端到端可用（2s 收到验证码）。可用 `AR_CATCHMAIL_DOMAINS` 覆盖。
DEFAULT_DOMAINS = ("catchmail.io", "mailistry.com", "zeppost.com")

# 本地部分前缀。用固定前缀 + 随机尾巴，方便在收件箱里一眼认出是我们的。
_LOCAL_PREFIX = "sdk"
_LOCAL_ALPHABET = string.ascii_lowercase + string.digits


def _parse_ms(iso: object) -> int:
    """ISO 8601（带时区，如 `2026-10-14T08:37:21.491000+00:00`）→ **毫秒** epoch。

    🔴 必须转成毫秒，才能与 `now_ms()` / `since_ts` 对齐 —— 而 `since_ts` 是
      "只看这之后到的信"的判据：不设下限就会拿到**上一轮的旧验证码**，
      然后服务端报"验证码错误"，而真正的原因是我们在用旧信。

    ⚠ 坏值退回 0（= 不过滤）。这是安全方向的降级：宁可多读一封，
      也不要因为一个时间戳格式变化把整条收信链炸掉。
    """
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


class CatchMailClient:
    """catchmail.io 客户端。**线程不安全**（每个任务一个实例）。

    与 `TempMailClient` **同一套鸭子类型契约**（`create_mailbox` /
    `wait_for_code` / `wait_for_mail` / `last_error` / `last_polls` /
    `last_http_errors`），所以 `provision` 不需要知道自己在用哪个源。
    """

    def __init__(self, domains=None, timeout: float | None = None):
        self.domains = list(domains or config.CATCHMAIL_DOMAINS or DEFAULT_DOMAINS)
        self.timeout = float(timeout or config.REQUEST_TIMEOUT)
        self.session = requests.Session()
        # 🔴 `Referer` / `Origin` 是必需的：裸请求会被 catchmail 拒。
        #    （与 CF Worker 源不同 —— 那边靠 `X-Admin-Token`。）
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Referer": f"{SITE}/",
                "Origin": SITE,
                "User-Agent": config.USER_AGENT,
            }
        )
        # 与 `TempMailClient` 同名的三个状态位（`provision` 会读它们）。
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0

    # ── 建地址 ────────────────────────────────────────────────
    def create_mailbox(self, domain: str | None = None, count: int = 1) -> list[str]:
        """建 `count` 个地址。

        🔴 **地址是我们自己选的**：`<随机本地部分>@<域名>`。所以这里没有
          "服务端分配"这一步 —— `GET /mailbox?address=` 就是"建 + 列信"。
          这也意味着**同一个地址可以重复调用**（幂等），不会产生新邮箱。

        ⚠ 返回列表长度可能**小于** `count`（某个地址建失败就跳过），
          调用方必须自己检查 —— 与 `TempMailClient` 的约定一致。
        """
        d = domain or (self.domains[0] if self.domains else "")
        if not d:
            return []
        out: list[str] = []
        for _ in range(max(1, count)):
            local = _LOCAL_PREFIX + "".join(random.choices(_LOCAL_ALPHABET, k=14))
            addr = f"{local}@{d}"
            try:
                r = self.session.get(
                    f"{API_BASE}/mailbox", params={"address": addr}, timeout=self.timeout
                )
                r.raise_for_status()
                r.json()
                out.append(addr)
            except (requests.RequestException, ValueError) as ex:
                # ⚠ 建址失败**不抛**：与 `TempMailClient` 一致（那边也是把失败
                #    留给调用方按"返回空列表"处理）。区别是这里把原因记进
                #    `last_error`，好让 `provision` 报出真实原因而不是"没返回地址"。
                self.last_error = f"catchmail 建址失败（{type(ex).__name__}: {ex}）"
                break
        return out

    # ── 读信 ──────────────────────────────────────────────────
    def _list_raw(self, address: str) -> list[dict]:
        """列某个地址的信（元数据）。**抛异常**，由调用方按 4xx/5xx 分类。"""
        r = self.session.get(
            f"{API_BASE}/mailbox", params={"address": address}, timeout=self.timeout
        )
        r.raise_for_status()
        return [m for m in (r.json().get("messages") or []) if isinstance(m, dict)]

    def _detail(self, mid: str, address: str) -> dict:
        r = self.session.get(
            f"{API_BASE}/message/{mid}",
            params={"mailbox": address},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json() or {}

    @staticmethod
    def _to_mail(raw: dict, detail: dict | None = None) -> Mail:
        """列表项（+ 可选的详情）→ `Mail`。

        🔴 列表里**没有正文** ⇒ 需要正文时必须单独取详情（`/message/<id>`）。
          所以 `list_mails()` 返回的是**元数据**（`body` 为空），而
          `wait_for_mail()` / `wait_for_code()` 会在**选中之后**补齐正文 ——
          这样每次轮询只多一次请求，而不是对每封信都取详情。
        """
        detail = detail or {}
        body = detail.get("body") or {}
        text = str(body.get("text") or "")
        html = str(body.get("html") or "")
        return Mail(
            id=str(raw.get("id", "")),
            to_address=str(raw.get("mailbox", "")),
            from_address=str(raw.get("from", "")),
            subject=str(raw.get("subject", "")),
            body=text,
            extracted_json="[]",  # catchmail 不给结构化抽取，正文兜底
            received_at=_parse_ms(raw.get("date")),
            raw_html=html,
            body_links=extract_links(f"{text}\n{html}"),
        )

    def list_mails(self, limit: int | None = None, email: str | None = None) -> list[Mail]:
        """列信（**元数据**，不含正文）。`email` 必给 —— 本源的 API 就是按地址查。"""
        if not email:
            return []
        raw = self._list_raw(email)
        out = [self._to_mail(m) for m in raw]
        if limit:
            out = out[:limit]
        return out

    def wait_for_mail(
        self,
        address: str,
        sender_contains: str = "agnes",
        timeout: int | None = None,
        interval: float | None = None,
        since_ts: int = 0,
        limit: int | None = None,
    ) -> Mail | None:
        """轮询等一封匹配的信，**返回时已补齐正文**。

        失败语义与 `TempMailClient` 同一套：`last_error` 为空 = **邮件没到**
        （该继续等）；非空 = **读不出来**（该修）。
        """
        timeout = timeout or config.MAIL_POLL_TIMEOUT
        interval = interval or config.MAIL_POLL_INTERVAL
        deadline = time.time() + timeout
        want = (sender_contains or "").lower()
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        errs = 0
        last_err = ""

        while time.time() < deadline:
            self.last_polls += 1
            try:
                raw = self._list_raw(address)
            except requests.HTTPError as ex:
                code = ex.response.status_code if ex.response is not None else 0
                # 4xx 是**我们**的问题（地址不对 / 被拒）⇒ 立刻失败，别把它
                # 伪装成"邮件一直没到"。
                if code and code < 500:
                    raise
                errs += 1
                self.last_http_errors = errs
                last_err = f"HTTP {code}"
                time.sleep(min(interval * (1 + errs // 5), 5.0))
                continue
            except Exception as ex:  # noqa: BLE001
                errs += 1
                self.last_http_errors = errs
                last_err = f"{type(ex).__name__}: {ex}"
                time.sleep(min(interval * (1 + errs // 5), 5.0))
                continue
            for m in raw:
                mail = self._to_mail(m)
                if since_ts and mail.received_at and mail.received_at < since_ts:
                    continue
                if (
                    want
                    and want not in mail.from_address.lower()
                    and want not in mail.subject.lower()
                ):
                    continue
                # 选中了 ⇒ 才去取正文（每次轮询至多多一次请求）
                try:
                    return self._to_mail(m, self._detail(str(m.get("id", "")), address))
                except Exception as ex:  # noqa: BLE001
                    self.last_error = f"catchmail 取正文失败（{type(ex).__name__}: {ex}）"
                    return None
            time.sleep(interval)
        if errs:
            self.last_error = (
                f"catchmail 持续读不出来（{errs} 次，最近 {last_err}）—— 不是邮件没到，是读不出来"
            )
        return None

    def wait_for_code(self, address: str, length: int = 6, **kw) -> str | None:
        """等一封**含验证码**的信，返回那个码。

        ⚠ 与 `wait_for_mail` 的差别不只是"多抽一次码"：这里**继续等下一封**，
          而不是拿到第一封就返回（收件箱里可能先到一封无关的信）。
        """
        timeout = kw.pop("timeout", None) or config.MAIL_POLL_TIMEOUT
        interval = kw.pop("interval", None) or config.MAIL_POLL_INTERVAL
        deadline = time.time() + timeout
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        while time.time() < deadline:
            mail = self.wait_for_mail(
                address, timeout=max(1, int(deadline - time.time())), interval=interval, **kw
            )
            if mail is None:
                return None
            code = mail.find_code(length)
            if code:
                return code
            # 这封没有码 ⇒ 抬高 `since_ts` 再看下一封
            if mail.received_at:
                kw["since_ts"] = mail.received_at + 1
        return None


def is_catchmail_domain(domain: str) -> bool:
    """这个域名是不是 catchmail 的（用于域名 → 源的派发）。

    ⚠ 与 `config.CATCHMAIL_DOMAINS` 对齐 —— 那是**唯一真源**，用户可以用
      `AR_CATCHMAIL_DOMAINS` 覆盖它。
    """
    return (domain or "").lower() in {
        d.lower() for d in (config.CATCHMAIL_DOMAINS or DEFAULT_DOMAINS)
    }


def extract_code_from(text: str) -> str | None:
    """本模块曾经自己写一份抽码 —— 已改为复用 `tempmail.extract_code`。

    保留这个薄封装只是为了让"抽码只有一个实现"这件事**可被搜索到**
    （历史上 Intern 那边就出现过两处正则漂移）。
    """
    return extract_code(text)


# `re` 只在类型注解/未来扩展里可能用到；当前没有直接使用 —— 删掉以免 lint 报 F401。
del re
