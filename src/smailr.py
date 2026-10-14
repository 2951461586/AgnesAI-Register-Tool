"""smailr.com 邮箱源 —— 本项目的**第三个**邮箱源。

为什么值得接（实测依据）
======================
它提供 **4 个**域名，而实测（2026-10-14）**全部过 Agnes 白名单**：

    smailr.com / loc.cc / mail.nodeloc.cc / nodeloc.cc      ✅✅✅✅

⇒ 域名池 7 → **11** ⇒ 11 个账号/小时。**三个源里最值的一个**
（catchmail 只给 3 个）。域名数是吞吐的硬上限（域名维度 2 格 = 1 个账号），
所以"多一个源"是唯一能加域名窗口的手段。

🔴 账号等级是硬门（实测撞到过）
============================
不传 `domain_id` 时用账号的**默认域名**，而默认域名可能要求更高等级：

    403 {"error":"该域名要求用户等级 4 及以上，您当前等级为 2"}

而传一个不知道的 `domain_id` 会得到：

    400 {"error":"no active domain configured"}

⇒ 账号后台要把「邮箱域名」设成上面 4 个之一（它们是**等级 1** 就能用的）。
⚠ API **不可枚举**域名（`/domains` 与 `/settings/domains` 都是
  `403 api key access not allowed for this endpoint`），所以非默认域名的
  `domain_id` 只能人工拿（`AR_SMAILR_DOMAIN_IDS`）。

撞到等级 403 时**复用已有邮箱**
==============================
这是从参考实现（`GPT-Register-Tool` 的 `mailbox_smailr.py`）学来的一招：
等级不够建不了新邮箱时，从 `GET /mailboxes` 里**领一个现成的**
（同域名、未归档、接收开启、邮件最少）—— 这样账号还能跑下去，而不是整批失败。

⚠ 我**没有**照搬它的"查台账看这个邮箱用没用过"（那要依赖它的账号库）：
  这里改用 **`mail_count == 0` 优先**。理由一样而依赖更少 —— 一个被用过的
  邮箱一定收过 Agnes 的验证码，所以 `mail_count >= 1`；而 `== 0` 基本就是
  没用过的。**这是启发式，不是保证**（有人手工往那个邮箱发过信就会误判）。

🔴 成功落库后**删掉邮箱**（本项目特有）
=====================================
smailr 账号有**邮箱总数上限**（实测 `mailbox_limit=100`），而每个账号占一个
邮箱 ⇒ 不删会跑几十个就卡死。所以 `delete_mailbox()` 是本源的**契约扩展**，
由 `mailbox.cleanup_successful()` 在**台账写盘之后**调用。
⚠ 删除不可逆，但那时账号已进台账；台账里存的是 Agnes 的凭据（密码/JWT/
  `sk-` key），与收信邮箱无关 ⇒ 删掉不影响账号可用性。

API（实测 2026-10-14，OpenAPI 见 `/app/openapi.json`）
==================================================
    GET    /api/v1/mailboxes                    → 邮箱列表
    POST   /api/v1/mailboxes {local_part, domain_id?}
    GET    /api/v1/mailboxes/{id}/mails?folder=INBOX&page=1&per_page=N
    GET    /api/v1/mails/{id}                   → 邮件正文
    DELETE /api/v1/mailboxes/{id}               → 删邮箱
    GET    /api/v1/quota                        → 配额（`mailbox_limit` 等）

鉴权：`Authorization: Bearer nm_...` **与** `X-API-Key: nm_...`（两个都带上最稳）。
"""

import json
import random
import string
import time

import requests

from common import config

from .tempmail import Mail, extract_code, extract_links, now_ms

DEFAULT_BASE = "https://smailr.com"
LOCAL_ALPHABET = string.ascii_lowercase + string.digits
LOCAL_PREFIX = "agnes"


class SmailrError(RuntimeError):
    """smailr 请求失败。带上 `status` 与 `body` —— 等级 403 的判据要用它们。"""

    def __init__(self, message: str, *, status: int = 0, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def _is_level_restricted(ex: "SmailrError") -> bool:
    """这次 403 是不是**等级**问题（而不是 API key 权限问题）。

    🔴 判据与参考实现一致：403 **且** body 里没有"key 无权/缺 scope"那类字样。
      区分这两者很重要 —— key 权限问题要换 key（改配置），等级问题要改域名设置
      （或者走复用路径）；混在一起会把排查引向相反方向。
    """
    if ex.status != 403:
        return False
    text = json.dumps(ex.body, ensure_ascii=False, default=str).lower()
    return "api key access not allowed" not in text and "missing scope" not in text


def _unwrap(value):
    """拆掉常见响应外壳（`{"data": ...}`）。**不假设只有一种形状。**"""
    if isinstance(value, dict):
        nested = value.get("data")
        if isinstance(nested, (dict, list)):
            return nested
    return value


def _as_list(value, *keys) -> list:
    """把响应统一成 list：本身就是 list，或包在 `data`/`mailboxes`/`items` 里。"""
    v = _unwrap(value)
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        for k in (*keys, "data", "items", "value"):
            sub = v.get(k)
            if isinstance(sub, list):
                return sub
    return []


def _mailbox_email(mb) -> str:
    """邮箱对象 → 地址。**字段名有好几种可能**（参考实现也是这么兜的）。"""
    if not isinstance(mb, dict):
        return ""
    for key in ("email", "address", "local_part"):
        v = str(mb.get(key) or "").strip()
        if "@" in v:
            return v.lower()
    local = str(mb.get("local_part") or "").strip().lower()
    domain = str(mb.get("domain") or mb.get("domain_name") or "").strip().lower().lstrip("@")
    return f"{local}@{domain}" if local and domain else ""


def _mailbox_id(mb) -> str:
    if not isinstance(mb, dict):
        return ""
    return str(mb.get("id") or mb.get("mailbox_id") or mb.get("uuid") or "")


def _parse_ms(value) -> int:
    """时间戳 → **毫秒** epoch。数字（秒或毫秒）与 ISO 串都要认。

    🔴 必须统一到毫秒才能与 `now_ms()` / `since_ts` 对齐 —— 而 `since_ts` 是
      "只看这之后到的信"的判据：不设下限就会拿到**上一轮的旧验证码**。
    ⚠ 坏值退回 0（= 不过滤），这是安全方向的降级。
    """
    if isinstance(value, bool) or value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        ts = float(value)
        return int(ts if ts > 10_000_000_000 else ts * 1000)
    text = str(value).strip()
    if text.isdigit():
        return _parse_ms(float(text))
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


class SmailrClient:
    """smailr 客户端。**线程不安全**（每个任务一个实例）。

    与 `TempMailClient` / `CatchMailClient` **同一套鸭子类型契约**，所以
    `provision` 不需要知道自己在用哪个源。
    """

    def __init__(self, api_key=None, base=None, domains=None, timeout: float | None = None):
        self.api_key = (api_key or config.SMAILR_API_KEY or "").strip()
        self.base = (base or config.SMAILR_BASE or DEFAULT_BASE).rstrip("/")
        self.domains = list(domains or config.SMAILR_DOMAINS)
        self.timeout = float(timeout or config.REQUEST_TIMEOUT)
        self.session = requests.Session()
        self.session.headers.update(
            {
                # 🔴 两种鉴权头都带（参考实现也这么做）—— 只带一个可能被拒。
                "Authorization": f"Bearer {self.api_key}",
                "X-API-Key": self.api_key,
                "Accept": "application/json",
                "User-Agent": config.USER_AGENT,
            }
        )
        # 与另两个源同名的三个状态位（`provision` 会读它们）。
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        # `地址 → 邮箱 id`。建完就缓存，免得每次收信都重新列一遍。
        self._ids: dict[str, str] = {}
        # 这个实例用到的邮箱是不是**复用**来的（写进日志/排查用）。
        self.reused = False

    # ── 底层 ──────────────────────────────────────────────────
    def _request(self, method: str, path: str, json_body=None):
        try:
            r = self.session.request(
                method, f"{self.base}{path}", json=json_body, timeout=self.timeout
            )
        except requests.RequestException as ex:
            raise SmailrError(f"{type(ex).__name__}: {ex}") from ex
        body = None
        if r.content:
            try:
                body = r.json()
            except ValueError:
                body = r.text[:300]
        if not 200 <= r.status_code < 300:
            raise SmailrError(
                f"smailr {method} {path} → HTTP {r.status_code}：{str(body)[:200]}",
                status=r.status_code,
                body=body,
            )
        return body

    # ── 邮箱 CRUD ─────────────────────────────────────────────
    def list_mailboxes(self) -> list:
        return _as_list(self._request("GET", "/api/v1/mailboxes"), "mailboxes")

    def _domain_id(self, domain: str) -> str:
        """域名 → `domain_id`。**默认域名可省略**，其余必须配。

        ⚠ 与 `config.source_of()` 不同：这里判的是"能不能省略 domain_id"，
          而不是"属于哪个源"。两件事，别合并。
        """
        d = (domain or "").lower()
        # 🔴 **显式配了 id 就优先用它** —— 哪怕它是"默认域名"。
        #    HAR 实测（2026-10-14）：账号的默认域名是个**要等级 4** 的域名 ⇒
        #    省略 `domain_id` 会撞 403；而带上该域名的真 `domain_id` 就能建成
        #    （`loc.cc` / `mail.nodeloc.cc` / `nodeloc.cc` 三个域名都是 201）。
        #    ⇒ "能省略"只是**没配 id 时**的兜底，不该盖过显式配置。
        did = config.SMAILR_DOMAIN_IDS.get(d)
        if did:
            return did
        if d == (config.SMAILR_DEFAULT_DOMAIN or "").lower():
            return ""
        if not did:
            raise SmailrError(
                f"smailr 域名 @{d} 不是默认域名，必须在 AR_SMAILR_DOMAIN_IDS 里给它 domain_id"
                f"（API 不可枚举域名，只能从后台人工拿）"
            )
        return did

    def _take_reusable(self, domain: str, taken: set) -> dict:
        """等级不够建新邮箱时，从已有邮箱里**领一个**。

        优先级：同域名 → 未归档 → 接收开启 → **`mail_count` 最少**。
        ⚠ `mail_count == 0` 是"没用过"的**启发式**（见模块 docstring）。
        """
        cands = []
        for mb in self.list_mailboxes():
            if not isinstance(mb, dict):
                continue
            email = _mailbox_email(mb)
            if not _mailbox_id(mb) or not email or email in taken:
                continue
            if email.rsplit("@", 1)[-1].lower() != domain.lower():
                continue
            if mb.get("is_archived") or mb.get("receiveEnabled") is False:
                continue
            try:
                count = int(mb.get("mail_count") or 0)
            except (TypeError, ValueError):
                count = 0
            cands.append((count, mb))
        if not cands:
            raise SmailrError(
                f"smailr @{domain} 需要更高账号等级，且**没有可复用的邮箱**"
                f"（后台把「邮箱域名」改成 {config.SMAILR_DEFAULT_DOMAIN} 等 LV1 域名，"
                f"或用 LV1 域名先建一个邮箱）"
            )
        cands.sort(key=lambda kv: kv[0])
        self.reused = True
        return cands[0][1]

    def create_mailbox(self, domain: str | None = None, count: int = 1) -> list[str]:
        """建（或领）`count` 个地址。

        🔴 与另两个源的**关键差别**：这里的地址由**服务端**定（我们只给
          `local_part`）⇒ 返回的地址必须以响应为准，不能自己拼。

        ⚠ 返回列表长度可能**小于** `count`；调用方必须自己检查。
        """
        if not self.api_key:
            self.last_error = "smailr 缺 AR_SMAILR_API_KEY"
            return []
        d = (domain or (self.domains[0] if self.domains else "")).lower()
        if not d:
            return []
        out: list[str] = []
        for _ in range(max(1, count)):
            local = LOCAL_PREFIX + "".join(random.choices(LOCAL_ALPHABET, k=10))
            # 这一轮拿到的邮箱对象（**建出来的**或**复用的**）—— 两者形状相同，
            # 所以后面统一用它取地址与 id。
            # ⚠ 必须在这里初始化：复用那条路径不经过 `resp = ...`，
            #   直接在下面用它会是 unbound（第一版就被类型检查抓到了）。
            mb: dict = {}
            email = ""
            try:
                did = self._domain_id(d)
                resp = _unwrap(
                    self._request(
                        "POST",
                        "/api/v1/mailboxes",
                        {"local_part": local, **({"domain_id": did} if did else {})},
                    )
                )
                if isinstance(resp, dict):
                    mb = resp
                email = _mailbox_email(mb)
                if not email:
                    # 建成功了但响应里没给地址 ⇒ 回列表里找（参考实现也这么兜）
                    email = self._find_by_local(local, d)
                if not email:
                    self.last_error = f"smailr 建址成功但拿不到地址（响应 {str(resp)[:120]}）"
                    break
            except SmailrError as ex:
                if _is_level_restricted(ex):
                    # 🔴 等级不够 ⇒ 领一个现成的（而不是整条链失败）
                    try:
                        mb = self._take_reusable(d, set(out))
                    except SmailrError as ex2:
                        self.last_error = str(ex2)
                        break
                    email = _mailbox_email(mb)
                else:
                    self.last_error = str(ex)
                    break
            # 🔴 响应里通常**直接带 id** ⇒ 就用它，别再多打一次列表接口
            #    （每次建址多一次往返，而建址是每个账号的第一枪）。
            self._ids[email] = _mailbox_id(mb) or self._id_of(email)
            out.append(email)
        return out

    def _find_by_local(self, local: str, domain: str) -> str:
        for mb in self.list_mailboxes():
            email = _mailbox_email(mb)
            if email == f"{local}@{domain}".lower():
                return email
        return ""

    def _id_of(self, address: str) -> str:
        """地址 → 邮箱 id（缓存优先）。"""
        if address in self._ids:
            return self._ids[address]
        for mb in self.list_mailboxes():
            if _mailbox_email(mb) == address.lower():
                self._ids[address] = _mailbox_id(mb)
                return self._ids[address]
        return ""

    def delete_mailbox(self, address: str) -> bool:
        """删掉一个邮箱（**成功落库后**由 `mailbox.cleanup_successful()` 调用）。

        🔴 为什么必须删：smailr 账号有**邮箱总数上限**（实测 `mailbox_limit=100`），
          而每个账号占一个 ⇒ 不删跑几十个就卡死。
        ⚠ **幂等**：地址查不到（已经删过 / 不是我们的）也返回 True —— 清理不该因为
          "它已经不在了"而报错。
        """
        mid = self._id_of(address)
        if not mid:
            return True
        try:
            self._request("DELETE", f"/api/v1/mailboxes/{mid}")
        except SmailrError as ex:
            # 404 视作已删（幂等）；其余记进 last_error 并返回 False
            if ex.status == 404:
                self._ids.pop(address, None)
                return True
            self.last_error = str(ex)
            return False
        self._ids.pop(address, None)
        return True

    def quota(self) -> dict:
        """配额（`mailbox_limit` / `mailbox_count` 等）。只读，供排查用。"""
        data = _unwrap(self._request("GET", "/api/v1/quota"))
        return data if isinstance(data, dict) else {}

    # ── 收信 ──────────────────────────────────────────────────
    def _list_raw(self, address: str) -> list:
        mid = self._id_of(address)
        if not mid:
            raise SmailrError(f"smailr 找不到邮箱 {address}（可能已被删）", status=404)
        return _as_list(
            self._request("GET", f"/api/v1/mailboxes/{mid}/mails?folder=INBOX&page=1&per_page=25"),
            "mails",
        )

    def _detail(self, mail_id: str) -> dict:
        data = _unwrap(self._request("GET", f"/api/v1/mails/{mail_id}"))
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _to_mail(raw: dict, detail: dict | None = None) -> Mail:
        """列表项（+ 可选详情）→ `Mail`。

        ⚠ 字段名刻意列了**多种候选**：这是服务端 JSON，而我们不拥有它
          （参考实现也是这么兜的）。正文可能在 `body`/`body_text`/`content` 里，
          而 `body` 自己还可能是 `{content, text}` 这种嵌套。
        """
        raw = raw if isinstance(raw, dict) else {}
        detail = detail if isinstance(detail, dict) else {}
        merged = {**raw, **detail}

        def first(*keys):
            for k in keys:
                v = merged.get(k)
                if v not in (None, ""):
                    return v
            return ""

        body = first("body", "body_html", "body_text", "content", "html", "text")
        if isinstance(body, dict):
            body = body.get("content") or body.get("text") or ""
        subject = str(first("subject", "title") or "")
        sender = first("from", "sender", "from_email", "from_address", "from_addr")
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or ""
        return Mail(
            id=str(first("id", "mail_id", "message_id") or ""),
            to_address=str(first("to_address", "mailbox") or ""),
            from_address=str(sender or ""),
            subject=subject,
            body=str(body or ""),
            extracted_json="[]",  # smailr 不给结构化抽取，正文兜底
            received_at=_parse_ms(
                first(
                    "received_at",
                    "receivedAt",
                    "received_datetime",
                    "created_at",
                    "createdAt",
                    "date",
                    "timestamp",
                    "sent_at",
                )
            ),
            raw_html=str(first("body_html", "html") or ""),
            body_links=extract_links(f"{body}\n{merged.get('body_html') or ''}"),
        )

    def list_mails(self, limit: int | None = None, email: str | None = None) -> list[Mail]:
        """列信（**元数据**，正文要另取详情）。`email` 必给。"""
        if not email:
            return []
        out = [self._to_mail(m) for m in self._list_raw(email)]
        return out[:limit] if limit else out

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

        失败语义与另两个源同一套：`last_error` 为空 = **邮件没到**（该继续等）；
        非空 = **读不出来**（该修）。
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
            except SmailrError as ex:
                # 4xx 是**我们**的问题（地址不对 / 邮箱被删）⇒ 立刻失败，
                # 别把它伪装成"邮件一直没到"。
                if ex.status and 400 <= ex.status < 500:
                    raise
                errs += 1
                self.last_http_errors = errs
                last_err = str(ex)[:120]
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
                try:
                    return self._to_mail(m, self._detail(mail.id))
                except SmailrError as ex:
                    self.last_error = f"smailr 取正文失败（{ex}）"
                    return None
            time.sleep(interval)
        if errs:
            self.last_error = f"smailr 持续读不出来（{errs} 次，最近 {last_err}）"
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
            if mail.received_at:
                kw["since_ts"] = mail.received_at + 1
        return None


__all__ = ["SmailrClient", "SmailrError", "extract_code", "now_ms"]
