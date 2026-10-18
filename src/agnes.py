"""Agnes AI 协议客户端 —— 整条注册链路的全部控制台 HTTP 交互。

协议来源
--------
**全部来自实测**（2026-10-13，逐条线上验证 + 前端 bundle 的路径枚举），
不是从混淆 JS 里猜的。完整证据链见 `docs/protocol.md`。

链路（6 步；② 发码由 headless captcha provider 前置）
----------------------------------------------------
🔴 **2026-10-15 16:24 起 ② 这一枪要求 captcha**（`400 Captcha verification
   is required`）⇒ 没带 `ticket`/`randstr` 时 `send_code()` 会抛
   `AgnesRejected`。主流水线默认用 `src/captcha.py` 的 **headless** provider
   先解出这两个值（`AR_CAPTCHA_ENABLED` 默认开）；本模块只负责把它们拼进
   query。实测与未测项见 `docs/protocol.md` §2②。

    ① 建邮箱（CF Worker，见 `tempmail.py`）
    ② GET  {platform}/api/verification?email=&purpose=register
         → {"code":200,"message":"ok","data":{"email":...}}   ← 发验证码
    ③ 收信（CF Worker）→ Worker 已按规则抽出 6 位码（`extracted_type="code"`）
    ④ POST {platform}/api/user/register
         {"email","password","password_confirm","code"}
         → {"code":200,"data":{"access_token":...,"token_type":"bearer","user":{...}}}
    ⑤ POST {platform}/api/user/login  {"username": email, "password": ...}
         → 同上（**换一条全新的 JWT**，是"密码真能用"的证明）
    ⑥ POST {platform}/api/token  {"name": ...}   （Authorization: Bearer <JWT>）
         → {"code":200,"data":{"key":"sk-...","id":...,"key_preview":"sk-xxxx…yyyy"}}

之后的只读校验：`/api/user/self` / `/api/user/balance` / `/api/user/subscription`
/ `/api/token`（key 列表），以及推理网关侧的 `/v1/models`（见 `apikey.py`）。

三条容易踩的坑
--------------
1. **注册尝试限流是"按 IP"的，而且覆盖两个端点**：`/api/verification` 与
   `/api/user/register` 共用同一张计数，消息是
   `Too many registration attempts from this IP. Please try again later`
   （HTTP **400**，不是 429）。它必须在**抛出点**就被打上 `quota` 标，
   否则会被当成普通 4xx 拒绝 —— 而"拒绝"的修法是改密码，"限流"的修法是
   换出口 / 等窗口，两者完全相反。
2. **请求体校验先于限流检查**：密码 < 8、两次密码不一致、code < 4 位会返回
   各自的校验错误，**不**触发限流。所以"没被限流"不等于"这个请求有效"。
3. **JWT 与 API Key 是两套东西**：JWT 打 `platform-backend.agnes-ai.com`
   （控制台），`sk-` Key 打 `apihub.agnes-ai.com/v1`（推理网关）。拿错会 401。

线程模型
--------
`AgnesClient` **不是线程安全的**（底层是 `requests.Session`）。本项目的用法是
"生产者线程建它 → 通过队列交给消费者线程 → 之后只有消费者碰它"，
即**顺序交接**，不存在并发访问。要改成并发共享就得换成每线程一个实例。
"""

import json
import time

import requests

from common import config

# ── 错误分类 ──────────────────────────────────────────────────────
# 🔴 分类而不是靠文本匹配，理由与 Intern / Hark 一致：我们自己拼的错误文案里
#    也可能出现服务端的词（例如把响应体原文附进 `error`），文本匹配分不清
#    "服务端说的"和"我们引用的"。**字段是权威，文本只兜底**。
ERR_QUOTA = "quota"
ERR_QUOTA_GUARD = "quota_guard"
ERR_REJECTED = "rejected"
ERR_NETWORK = "network"
ERR_MAIL = "mail"
ERR_VERIFY = "verify"
# captcha 求解失败（本地）。与 `rejected` 分开：`rejected` 是**服务端明确拒绝**，
# 而这一类是“我们没拿到 ticket/randstr”（依赖缺失 / 浏览器超时 / 答案被拒）。
ERR_CAPTCHA = "captcha"

# 实测的服务端业务码 / 文案。
# 🔴 限流是 **HTTP 400 + 这句话**，不是 429 —— 这是本项目最容易漏的一处。
MSG_IP_RATE_LIMITED = "too many registration attempts"
MSG_ALREADY_REGISTERED = "already registered"
MSG_WEAK_PWD = "password must be at least 8"
MSG_PWD_MISMATCH = "passwords do not match"
MSG_INVALID_CREDENTIALS = "invalid username or password"

# 配额类业务码（HTTP 429 或响应体里出现这些 code 时按配额处理）。
_QUOTA_CODES = frozenset({"TOO_MANY_REQUESTS", "RATE_LIMITED", "QUOTA_EXCEEDED", "429"})


class AgnesError(Exception):
    """链路错误的基类。`kind` 是结构化类别（见上面的 ERR_* 常量）。"""

    kind = ERR_NETWORK

    def __init__(self, message: str, *, code: str = "", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


class AgnesRejected(AgnesError):
    """服务端**明确拒绝**（4xx，非配额）：邮箱已注册、密码策略、凭据错……"""

    kind = ERR_REJECTED


class AgnesQuota(AgnesError):
    """限流 / 配额：HTTP 429，或那条 `Too many registration attempts` 的 400。

    🔴 它必须与 `AgnesRejected` 分开：限流是**自愈**的（换出口 / 等窗口），
      而拒绝是**确定性**的（改输入才有用）。混为一类会让调用方
      对着一个"密码太短"无限换出口。
    """

    kind = ERR_QUOTA


class AgnesNetwork(AgnesError):
    """网络 / 超时 / 5xx —— **可重试**的一类。"""

    kind = ERR_NETWORK


class AgnesCaptcha(AgnesError):
    """captcha 求解失败（本地）：依赖缺失 / 浏览器超时 / 答案被拒。

    🔴 它**不是** `rejected`：`rejected` 是服务端对着已经带 ticket 的请求
       明确拒绝；而这一类是**根本没拿到** ticket/randstr。两者的修法不同：
       前者改输入，后者查 captcha provider / 依赖 / 窗口。
    """

    kind = ERR_CAPTCHA


def _json_or_none(r):
    """响应 → JSON dict；不是 JSON 就返回 None（**不抛**）。

    单独抽出来是为了把「是不是 JSON」与「状态码多少」解耦：
    CF 边缘拦截时返回的是 HTML 错误页，而应用层错误是 JSON ——
    两者必须分开判，否则会得到 `JSONDecodeError` 这种指向错误位置的异常。
    """
    try:
        d = r.json()
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _short(body, r) -> str:
    """错误正文的**短**摘要：优先 JSON 里的 message，其次截断的原始文本。

    ⚠ 截断是必须的：CF 边缘的错误页有整页 HTML，原样塞进 `error` 字段会让
      台账里出现几 KB 的 HTML。

    🔴 **空响应体必须说出来**：不处理的话错误文案会退化成 `HTTP 500：`
      （冒号后面什么都没有），而人看到这个会以为是**我们**没把信息填进去。
    """
    if isinstance(body, dict):
        msg = body.get("message") or body.get("detail") or body.get("error") or ""
        if msg:
            return str(msg)[:200]
        return json.dumps(body, ensure_ascii=False)[:200]
    text = (r.text or "").strip()
    if not text:
        ctype = (r.headers or {}).get("Content-Type", "")
        return f"(空响应体{'，' + ctype if ctype else ''})"
    return text[:200].replace("\n", " ")


def _is_ip_limited(body, status: int) -> bool:
    """这次响应是不是那条**按 IP 的注册尝试限流**。

    🔴 判据必须同时看状态码与文案：实测它是 `400`（不是 429），
       所以"只看状态码"必然漏。而只看文案又太宽（别的接口也可能说
       "try again later"），所以要求 `too many registration attempts`
       这个**具体**短语。
    """
    if status == 429:
        return True
    msg = ""
    if isinstance(body, dict):
        msg = str(body.get("message") or body.get("detail") or "").lower()
    return MSG_IP_RATE_LIMITED in msg


def access_token_of(body) -> str:
    """从登录 / 注册响应里取 JWT。**只有一个实现**（别在调用处各写一份）。"""
    if not isinstance(body, dict):
        return ""
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("access_token", "token"):
            if data.get(key):
                return str(data[key])
    for key in ("access_token", "token"):
        if body.get(key):
            return str(body[key])
    return ""


class AgnesClient:
    """一个账号一条链路一个实例。"""

    def __init__(
        self,
        *,
        base: str | None = None,
        console: str | None = None,
        timeout: float | None = None,
        user_agent: str | None = None,
        proxy: str | None = None,
    ):
        self.base = (base or config.PLATFORM_BASE).rstrip("/")
        self.console = (console or config.CONSOLE_BASE).rstrip("/")
        self.timeout = float(timeout or config.REQUEST_TIMEOUT)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent or config.USER_AGENT,
                "Accept": "*/*",
                "Content-Type": "application/json",
                "Origin": self.console,
                "Referer": f"{self.console}/",
                "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8",
            }
        )
        # 代理只挂 Agnes 这一侧（邮箱 Worker 不挂，见 tempmail 的说明）。
        config.apply_proxy(self.session, proxy)
        # 最近一次响应的状态码 / 业务码，供报告与错误打标使用。
        self.last_status = 0
        self.last_code = ""
        self.jwt = ""

    # ── 底层 ──────────────────────────────────────────────────
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        payload: dict | None = None,
        headers: dict | None = None,
        retries: int = 0,
    ) -> requests.Response:
        """发一次请求，并把"错误"翻译成 `AgnesError` 的某个子类。

        🔴 **判据顺序**：先看"是不是那条 IP 限流"，再看状态码，最后看响应体。
        反过来的话，限流那条 `400` 会被归到"服务端拒绝"（`AgnesRejected`），
        于是调用方会去改密码、而不是换出口。

        🔴 `retries` 只给**幂等且无副作用**的调用用（`user_self` / `balance` /
        `list_keys` 这类 GET）。`send_code` / `register` / `login` / `create_key`
        **一律不自动重试**：它们要么消耗服务端窗口（发信、注册），
        要么"第一次到底成没成"在 5xx 时是**未知**的。

        🔴 **`send_code` 的网络失败也不重试** —— 这是**查证过的决定**，不是漏做：

        2026-10-14 实测了台账里 34 条 `network@code` 的**异常原文**：

        | 形态 | 条数 | 请求落地了吗 |
        | --- | --- | --- |
        | `RemoteDisconnected('Remote end closed connection without response')` | 25 | **可能已落地**（请求已写出） |
        | `SSLEOFError('[SSL: UNEXPECTED_EOF_WHILE_READING]')` | 9 | **可能已落地**（握手**之后**读到 EOF） |

        ⇒ **34 条里 0 条属于"确定没落地"**，所以"只对没落地的重试"这条窄路
        **救不回任何一条**。而重试一条可能已落地的 `send_code` 会变成
        **第二封验证码 + 第二格服务端窗口** —— 代价确定，收益不确定。

        ⚠ 差点归错：`SSLError` 望文生义会当成"TLS 握手失败（请求没出去）"，
        而实测原文是 `SSLEOFError`。**看异常原文，别看类名。**

        🔴 不重试的**实际代价比预想小**：2026-10-14 11:36 跑批里 3 条
        `send_code` 撞网络错误，因为请求**没完成**，所以**零验证码被烧**、
        零服务端窗口被白占 —— 损失只是一个账号，不是"码烧了账号还没了"。
        （那种更贵的失败形态是 P6 修的另一个问题，与本条无关。）
        """
        last_exc: AgnesNetwork | None = None
        for attempt in range(retries + 1):
            try:
                return self._once(method, path, params=params, payload=payload, headers=headers)
            except AgnesNetwork as ex:
                last_exc = ex
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        if last_exc is None:  # pragma: no cover —— 循环至少跑一次，到不了这里
            raise AgnesNetwork("请求未执行（内部错误）")
        raise last_exc

    def _once(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        payload: dict | None = None,
        headers: dict | None = None,
    ) -> requests.Response:
        """单次请求（不重试）。见 `_request` 的说明。"""
        try:
            r = self.session.request(
                method,
                self.base + path,
                params=params,
                json=payload,
                headers=headers or None,
                timeout=self.timeout,
            )
        except requests.RequestException as ex:
            raise AgnesNetwork(f"{type(ex).__name__}: {ex}") from ex

        self.last_status = r.status_code
        body = _json_or_none(r)
        self.last_code = str((body or {}).get("code") or "")

        if _is_ip_limited(body, r.status_code) or self.last_code in _QUOTA_CODES:
            raise AgnesQuota(
                f"HTTP {r.status_code} {self.last_code or 'RATE_LIMITED'}：{_short(body, r)}",
                code=self.last_code or "RATE_LIMITED",
                status=r.status_code,
            )
        if r.status_code >= 500:
            raise AgnesNetwork(f"HTTP {r.status_code}：{_short(body, r)}", status=r.status_code)
        if r.status_code >= 400:
            raise AgnesRejected(
                f"HTTP {r.status_code} {self.last_code or '?'}：{_short(body, r)}",
                code=self.last_code,
                status=r.status_code,
            )
        return r

    # ── ② 发验证码 ────────────────────────────────────────────
    def send_code(
        self,
        email: str,
        purpose: str = "register",
        *,
        ticket: str | None = None,
        randstr: str | None = None,
    ) -> dict:
        """请求服务端发验证码。

        ⚠ 这是**写**请求：它消耗服务端那张按 IP 的注册尝试窗口
          （见 `MSG_IP_RATE_LIMITED`），所以调用前必须过本地闸门
          （`pipeline.register_limiter(slot)`）。

        🔴 2026-10-15 16:24 起服务端要求 captcha：没带 `ticket`/`randstr` 会返回
          `400 {"code":400,"message":"Captcha verification is required"}`。
          这两个值由 `src/captcha.py` 的 provider 在**真浏览器**里解出来；
          本方法只负责把它们拼进 query（其余端点不带）。
        """
        params = {"email": email, "purpose": purpose}
        if ticket:
            params["ticket"] = ticket
        if randstr:
            params["randstr"] = randstr
        r = self._request(
            "GET",
            "/api/verification",
            params=params,
            headers={"x-user-language": "zh-CN"},
        )
        return _json_or_none(r) or {}

    # ── ④ 注册 ────────────────────────────────────────────────
    def register(self, email: str, password: str, code: str) -> dict:
        """用验证码注册。

        🔴 成功响应里**已经带 `access_token`**（实测），但本链路仍然会在
          下一步再走一次 `/api/user/login` —— 那一步才是"密码真能用"的证明
          （注册返回的 token 可能是"刚建号时发的那一条"，与密码无关）。
        """
        r = self._request(
            "POST",
            "/api/user/register",
            payload={
                "email": email,
                "password": password,
                "password_confirm": password,
                "code": code,
            },
            headers={"x-user-language": "zh"},
        )
        return _json_or_none(r) or {}

    # ── ⑤ 密码登录 ────────────────────────────────────────────
    def login(self, email: str, password: str) -> dict:
        """用密码登录 —— 这是"账号真能用"的**最强证明**。"""
        r = self._request(
            "POST",
            "/api/user/login",
            payload={"username": email, "password": password},
            headers={"x-user-language": "zh"},
        )
        body = _json_or_none(r) or {}
        self.jwt = access_token_of(body)
        return body

    # ── ⑥ 建 API Key ──────────────────────────────────────────
    def create_key(self, name: str) -> dict:
        """用 JWT 建一把 API Key。返回 `data`（含 `key` / `id` / `key_preview`）。

        ⚠ 需要 `Authorization: Bearer <JWT>` —— JWT 来自上一步的密码登录。
        """
        r = self._request(
            "POST",
            "/api/token",
            payload={"name": name},
            headers={
                "x-user-language": "zh-CN",
                "Authorization": f"Bearer {self.jwt}",
            },
        )
        body = _json_or_none(r) or {}
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    # ── 只读校验 ──────────────────────────────────────────────
    def _auth_headers(self) -> dict:
        return {"x-user-language": "zh-CN", "Authorization": f"Bearer {self.jwt}"}

    def user_self(self) -> dict:
        r = self._request("GET", "/api/user/self", headers=self._auth_headers(), retries=1)
        body = _json_or_none(r) or {}
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def balance(self) -> dict:
        r = self._request("GET", "/api/user/balance", headers=self._auth_headers(), retries=1)
        body = _json_or_none(r) or {}
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def subscription(self) -> dict | None:
        """当前订阅（免费账号实测是 `null`）。返回 `None` 表示免费档。"""
        r = self._request("GET", "/api/user/subscription", headers=self._auth_headers(), retries=1)
        body = _json_or_none(r) or {}
        data = body.get("data")
        return data if isinstance(data, dict) else None

    def list_keys(self) -> dict:
        """key 列表 + 配额（`default_quota.max_keys` 等）。"""
        r = self._request("GET", "/api/token", headers=self._auth_headers(), retries=1)
        body = _json_or_none(r) or {}
        data = body.get("data")
        return data if isinstance(data, dict) else {}
