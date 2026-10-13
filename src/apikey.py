"""API Key 可用性校验（推理网关侧）。

为什么单独一个模块
==================
"key 建出来了"与"key 能调用模型"是**两件事**。控制台的 `POST /api/token`
只要返回了 `sk-...` 就算成功，但那把 key 在网关侧可能：
  · 还没生效（传播延迟）；
  · 因为账号被风控 / 未激活而被拒；
  · 被配额策略限制。

而这三件事在台账里长得**一模一样**（都有 `api_key`、`status=success`）。
不单独校验，就会得到一批"看着成功、其实不能用"的假绿账号 ——
这是本项目最该防的一类失败。所以校验**独立成阶段**（`STAGES` 里的 `verify`），
结果写进 `stages["verify"]`，失败**不影响**账号本身的状态（非致命）。

两个校验层级
------------
  ① `GET /v1/models` —— 最便宜，能证明 key 通过了网关鉴权；
  ② `POST /v1/chat/completions` —— 真正计费的一次调用，能证明 key 能出结果。

🔴 默认**两级都做**（`config.VERIFY_CHAT`）：实测里模型列表对某些无效 key
   也可能放行，而一次 1~8 token 的调用成本可以忽略。
"""

import time

import requests

from common import config

# 校验失败的原因分类（写进 `stages["verify"]` 的前缀，供报告与复盘看）。
V_OK = "ok"
V_NOT_ACTIVE = "not_active"
V_AUTH = "auth_failed"
V_NETWORK = "network"


class KeyVerifyError(RuntimeError):
    """key 校验失败。`kind` 是上面那组 `V_*` 常量。"""

    def __init__(self, message: str, *, kind: str = V_NETWORK, status: int = 0):
        super().__init__(message)
        self.kind = kind
        self.status = status


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def list_models(key: str, *, timeout: float | None = None) -> list[str]:
    """`GET /v1/models` → 模型 id 列表。鉴权失败抛 `KeyVerifyError(kind=auth)`。"""
    url = f"{config.APIHUB_BASE.rstrip('/')}/models"
    try:
        r = requests.get(
            url, headers=_headers(key), timeout=float(timeout or config.VERIFY_TIMEOUT)
        )
    except requests.RequestException as ex:
        raise KeyVerifyError(f"{type(ex).__name__}: {ex}", kind=V_NETWORK) from ex
    if r.status_code in (401, 403):
        raise KeyVerifyError(
            f"HTTP {r.status_code}：网关拒绝这把 key（{_short(r)}）", kind=V_AUTH, status=r.status_code
        )
    if r.status_code >= 500:
        raise KeyVerifyError(f"HTTP {r.status_code}：{_short(r)}", kind=V_NETWORK, status=r.status_code)
    if r.status_code != 200:
        raise KeyVerifyError(f"HTTP {r.status_code}：{_short(r)}", kind=V_NETWORK, status=r.status_code)
    try:
        data = r.json().get("data") or []
    except ValueError:
        raise KeyVerifyError(f"模型列表不是 JSON：{_short(r)}", kind=V_NETWORK) from None
    out = []
    for m in data:
        if isinstance(m, dict) and m.get("id"):
            out.append(str(m["id"]))
    return out


def chat_once(
    key: str,
    *,
    model: str | None = None,
    prompt: str = "ping",
    max_tokens: int = 8,
    timeout: float | None = None,
) -> dict:
    """`POST /v1/chat/completions` 打一次最小调用。返回原始 JSON。

    🔴 `max_tokens` 刻意小：这只是"能不能用"的探针，不是真的要用模型。
      实测 Agnes 的模型会先输出 `reasoning_content`，所以即使 `content` 为空，
      只要 HTTP 200 + 有 `usage` 就算通过（见 `verify`）。
    """
    url = f"{config.APIHUB_BASE.rstrip('/')}/chat/completions"
    payload = {
        "model": model or config.VERIFY_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(max_tokens),
    }
    try:
        r = requests.post(
            url,
            headers={**_headers(key), "Content-Type": "application/json"},
            json=payload,
            timeout=float(timeout or config.VERIFY_TIMEOUT),
        )
    except requests.RequestException as ex:
        raise KeyVerifyError(f"{type(ex).__name__}: {ex}", kind=V_NETWORK) from ex
    if r.status_code in (401, 403):
        raise KeyVerifyError(
            f"HTTP {r.status_code}：网关拒绝这把 key（{_short(r)}）", kind=V_AUTH, status=r.status_code
        )
    if r.status_code != 200:
        raise KeyVerifyError(
            f"HTTP {r.status_code}：{_short(r)}", kind=V_NETWORK, status=r.status_code
        )
    try:
        return r.json()
    except ValueError:
        raise KeyVerifyError(f"chat 响应不是 JSON：{_short(r)}", kind=V_NETWORK) from None


def verify(
    key: str,
    *,
    model: str | None = None,
    do_chat: bool | None = None,
    attempts: int = 3,
    delay: float = 3.0,
) -> dict:
    """校验一把 key。返回 `{"ok":bool,"note":str,"models":[...],"usage":{...}}`。

    `attempts` / `delay` 是为**传播延迟**准备的：新建的 key 在网关侧可能要
    几秒才生效（Intern-Register-Tool 实测约 10s）。批量场景下由调用方
    把这一步推迟到流水线末尾，那时早已生效；单账号场景就地重试。

    ⚠ 只对 `not_active` / `network` 重试，`auth` **不重试** ——
      鉴权被拒是确定性的，重试只是白打枪。
    """
    do_chat = config.VERIFY_CHAT if do_chat is None else do_chat
    last: KeyVerifyError | None = None
    for attempt in range(max(1, attempts)):
        try:
            models = list_models(key)
            usage = {}
            if do_chat:
                body = chat_once(key, model=model)
                usage = body.get("usage") or {}
                if not body.get("choices") and not usage:
                    raise KeyVerifyError("chat 返回里既没有 choices 也没有 usage", kind=V_NETWORK)
            return {
                "ok": True,
                "note": f"ok({len(models)} models" + (f", {usage.get('total_tokens')} tok)" if usage else ")"),
                "models": models,
                "usage": usage,
            }
        except KeyVerifyError as ex:
            last = ex
            if ex.kind == V_AUTH:
                break
            if attempt + 1 < max(1, attempts):
                time.sleep(float(delay))
    assert last is not None
    note = (
        f"{V_AUTH}: {last}"
        if last.kind == V_AUTH
        else f"{V_NOT_ACTIVE}: {last}（试了 {max(1, attempts)} 次）"
    )
    return {"ok": False, "note": note, "models": [], "usage": {}}


def _short(r) -> str:
    """错误正文短摘要（与 `agnes._short` 同构；这里是叶子模块，不反向 import）。"""
    text = (r.text or "").strip()
    if not text:
        ctype = (r.headers or {}).get("Content-Type", "")
        return f"(空响应体{'，' + ctype if ctype else ''})"
    try:
        d = r.json()
    except ValueError:
        return text[:200].replace("\n", " ")
    if isinstance(d, dict):
        msg = d.get("error") or d.get("message") or ""
        if isinstance(msg, dict):
            msg = msg.get("message") or ""
        if msg:
            return str(msg)[:200]
        return str(d)[:200]
    return text[:200]
