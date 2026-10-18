"""TCaptcha 点选 provider（headless Playwright）。

契约
====
    class CaptchaProvider(Protocol):
        def solve(self, email: str, *, proxy: str | None = None) -> tuple[str, str]: ...

`TCaptchaProvider` 用真浏览器跑 TCaptcha widget，解出点选答案后自动点击、确认，
返回服务端接受的 `(ticket, randstr)`。

🔴 **不自己发 `/api/verification`**：那一枪由主流水线用 `requests` + 槽位代理
   发出（这样 IP 维度的窗口记账才对）。所以 provider 会
   `page.route("**/api/verification*", abort)` 把前端的发码请求拦掉。

🔴 **浏览器必须走同一个槽位代理**：`ticket` 绑定了当次 captcha 会话的 `uip`
   （`prehandle` 里能看到）。如果浏览器直连、而 `send_code` 走代理，服务端
   可能因为 IP 不一致拒绝 ticket。所以 `solve(email, proxy=slot)` 把槽位串传进来。

依赖
====
Playwright / numpy / scipy / Pillow 是**可选依赖**（`pyproject.toml` 的
`captcha` extra）。缺依赖时抛 `AgnesCaptcha`，由 `config.validate()` 在启动
阶段先拦一次，给出可执行的修法。
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from common import config

from .agnes import AgnesCaptcha

_OPTIONAL_DEPS = ("playwright", "numpy", "scipy", "PIL")

# 与探针实测一致的 UA（headless 下没有触发无头检测）。
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

_JSONP = re.compile(r"^\s*[\w.$]+\s*\((.*)\)\s*;?\s*$", re.S)


class CaptchaProvider(Protocol):
    """流水线只依赖这个契约 —— 测试可以注入假 provider。"""

    def solve(self, email: str, *, proxy: str | None = None) -> tuple[str, str]: ...


def available() -> bool:
    """可选依赖是否齐全（`config.validate()` 用它提前报错）。"""
    return all(importlib.util.find_spec(name) is not None for name in _OPTIONAL_DEPS)


def default_provider() -> TCaptchaProvider | None:
    """按配置造默认 provider；`AR_CAPTCHA_ENABLED=0` 时返回 `None`。"""
    if not config.CAPTCHA_ENABLED:
        return None
    return TCaptchaProvider(
        headless=config.CAPTCHA_HEADLESS,
        timeout_s=float(config.CAPTCHA_TIMEOUT_S),
        targets=config.CAPTCHA_TARGETS,
        retries=config.CAPTCHA_RETRIES,
        entry_url=config.CAPTCHA_ENTRY_URL,
    )


def _parse_jsonp(text: str) -> dict:
    m = _JSONP.match(text)
    try:
        return json.loads(m.group(1) if m else text)
    except json.JSONDecodeError as ex:
        raise AgnesCaptcha(f"captcha prehandle 不是合法 JSON：{ex}") from ex


def _debug_dir() -> Path | None:
    raw = config.CAPTCHA_DEBUG_DIR
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = config.ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _save_debug_images(bg_bytes: bytes, sprite_bytes: bytes, points, meta, error) -> None:
    """失败留图（默认关）。`AR_CAPTCHA_DEBUG_DIR` 为空时什么都不做。"""
    d = _debug_dir()
    if d is None:
        return
    try:
        ts = time.strftime("%Y%m%d-%H%M%S")
        tag = f"{ts}_{int(time.time() * 1000) % 1000:03d}"
        (d / f"{tag}_img0_sprite.png").write_bytes(sprite_bytes)
        (d / f"{tag}_img1_bg.png").write_bytes(bg_bytes)
        info = {
            "error": str(error),
            "points": points or [],
            "details": (meta or {}).get("details", []),
        }
        (d / f"{tag}_meta.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as ex:  # noqa: BLE001
        # 留图是**尽力而为**：磁盘满 / 权限错不该把 captcha 的原始错误盖掉。
        print(f"  ⚠ captcha 留图失败：{type(ex).__name__}: {ex}", file=sys.stderr)


def _playwright_proxy(proxy: str | None) -> Any:
    """把项目里的代理串（URL / host:port:user:pass）转成 Playwright 的 proxy dict。"""
    px = config.proxies(proxy)
    if not px:
        return None
    url = px.get("http") or px.get("https") or ""
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.port:
        raise AgnesCaptcha(f"代理串无法解析成 Playwright 格式：{url!r}")
    out: dict[str, Any] = {
        "server": f"{parsed.scheme.replace('socks5h', 'socks5')}://{parsed.hostname}:{parsed.port}"
    }
    if parsed.username:
        out["username"] = unquote(parsed.username)
    if parsed.password:
        out["password"] = unquote(parsed.password)
    return out


def _size_2d(prehandle: Any) -> list:
    default = [672, 480]
    if prehandle is None:
        return default
    try:
        body = _parse_jsonp(prehandle.text())
        data = body.get("data") or {}
        dyn = data.get("dyn_show_info") or {}
        return (dyn.get("bg_elem_cfg") or {}).get("size_2d") or default
    except Exception:  # noqa: BLE001
        return default


def _find_captcha_frame(page: Any):
    for fr in page.frames:
        try:
            if fr.locator(".tc-imgarea").count() > 0:
                return fr
        except Exception:  # noqa: BLE001
            continue
    return None


def _check_points_sane(points, *, min_gap: float = 25.0) -> None:
    """提交前自检：两个点击点不能挤在一起。

    🔴 两个图标被点到同一个 blob 上时，答案**必然**错（服务端 errorCode=50）。
       与其白烧一次 verify，不如在这里判矛盾 ⇒ 抛 `AgnesCaptcha` ⇒ 由
       `provision` 的重解循环（`AR_CAPTCHA_RESOLVE_ATTEMPTS`）拿一张新题。
    """
    for i in range(len(points)):
        for k in range(i + 1, len(points)):
            dx = points[i][0] - points[k][0]
            dy = points[i][1] - points[k][1]
            if (dx * dx + dy * dy) ** 0.5 < min_gap:
                raise AgnesCaptcha(
                    f"[contradiction] 点 {i + 1} 与点 {k + 1} 距离 <{min_gap:.0f}px"
                    f"（{points[i]} vs {points[k]}）—— 大概率选到同一个图标"
                )


def _click_points(page: Any, frame: Any, points, size_2d) -> None:
    box = frame.locator(".tc-imgarea").first.bounding_box()
    if not box:
        raise AgnesCaptcha("拿不到 .tc-imgarea 的 bounding_box")
    sx = box["width"] / size_2d[0]
    sy = box["height"] / size_2d[1]
    for x, y in points:
        px = box["x"] + x * sx
        py = box["y"] + y * sy
        # 先移动再点：让 tdc/collect 记录到鼠标轨迹，而不是一个孤立 click。
        page.mouse.move(px - 24, py - 18)
        page.mouse.move(px, py, steps=6)
        page.mouse.click(px, py)
        page.wait_for_timeout(300)
    # 🔴 live 实测：`.verify-btn` 会先命中 **embed 模式**的隐藏按钮；
    #    popup 模式的确认按钮是 `.tc-action.verify-btn`（带 show 类）。
    btn = frame.locator(".tc-action.verify-btn").first
    try:
        btn.wait_for(state="visible", timeout=6000)
    except Exception:  # noqa: BLE001
        btn = frame.locator(".verify-btn:visible").first
        btn.wait_for(state="visible", timeout=3000)
    btn.click()


_TLS = threading.local()


class _BrowserSession:
    """一个线程独占的 Playwright 实例 + 浏览器。"""

    __slots__ = ("pw", "browser")

    def __init__(self, pw: Any, browser: Any) -> None:
        self.pw = pw
        self.browser = browser


def _browser_session(headless: bool) -> _BrowserSession:
    """取**当前线程**的浏览器会话（没有就起一个）。

    🔴 Playwright 的 sync API **不是线程安全的**，而且 `sync_playwright()`
      必须与使用它的线程同生共死 —— 会话只能 thread-local。
      复用的是“每次 solve 都重开浏览器”的那 1~2s（实测启动+关闭成本）。
    """
    sess = getattr(_TLS, "session", None)
    if sess is not None:
        return sess
    try:
        from playwright.sync_api import sync_playwright  # pyright: ignore[reportMissingImports]
    except ImportError as ex:  # pragma: no cover - 依赖缺失路径
        raise AgnesCaptcha(
            "captcha provider 需要 Playwright；装 `pip install -e .[captcha]` 或设 AR_CAPTCHA_ENABLED=0"
        ) from ex
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
    except Exception:
        pw.stop()
        raise
    sess = _BrowserSession(pw, browser)
    _TLS.session = sess
    return sess


def _close_thread_session() -> None:
    """关掉**当前线程**的浏览器会话（幂等）。"""
    sess = getattr(_TLS, "session", None)
    if sess is None:
        return
    _TLS.session = None
    try:
        sess.browser.close()
    finally:
        sess.pw.stop()


@contextlib.contextmanager
def _browser(headless: bool):
    """借出 thread-local 浏览器；只在它已经崩了时丢弃会话。"""
    sess = _browser_session(headless)
    try:
        yield sess.browser
    finally:
        try:
            alive = bool(sess.browser.is_connected())
        except Exception:  # noqa: BLE001
            alive = False
        if not alive:
            _close_thread_session()


@dataclass
class TCaptchaProvider:
    """headless 浏览器 + 点选 solver。

    ⚠ 每次 `solve()` 都**新开一个浏览器**：Playwright 的 sync API 不是线程安全的，
      而生产者池是多线程的。多开一个浏览器的代价（~2s）远小于给锁串行化。
    """

    headless: bool = True
    timeout_s: float = 90.0
    targets: int = 3
    retries: int = 1
    entry_url: str = field(default_factory=lambda: f"{config.CONSOLE_BASE}/login")

    def solve(self, email: str, *, proxy: str | None = None) -> tuple[str, str]:
        # 预先给一个“未知原因”的实例：循环一定至少跑一轮（`retries >= 0`），
        # 但用普通赋值而不是 `assert` —— 生产代码里的 `assert` 在 `-O` 下会被剥离。
        last = AgnesCaptcha("captcha 求解失败（未知原因）")
        for attempt in range(self.retries + 1):
            try:
                return self._solve_once(email, proxy=proxy)
            except AgnesCaptcha as ex:
                last = ex
            except Exception as ex:  # noqa: BLE001
                # 🔴 Playwright / 浏览器启动失败也是**本地 captcha 能力**的问题。
                #    归到 `network` 会让报告把它和出口故障混在一起（实测踩过：
                #    浏览器二进制缺失 ⇒ 5 批全报 `[network]`，看起来像代理坏了）。
                last = AgnesCaptcha(f"{type(ex).__name__}: {ex}")
            if attempt < self.retries:
                time.sleep(1.5)
        raise last

    def _solve_once(self, email: str, *, proxy: str | None = None) -> tuple[str, str]:
        # 用 importlib 而不是 `from .captcha_solver import ...`：这个模块的
        # 顶层 import 会拉 numpy/scipy/Pillow，必须在**真正要解图时**才加载。
        # （顺带避开 LSP 对「函数内相对 import」的陈旧索引误报。）
        solve_bytes = importlib.import_module(".captcha_solver", __package__).solve_bytes

        pw_proxy = _playwright_proxy(proxy)
        with _browser(self.headless) as browser:
            ctx = None
            try:
                ctx = browser.new_context(
                    user_agent=_USER_AGENT,
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 900},
                    proxy=pw_proxy,
                )
                page = ctx.new_page()
                state: dict[str, Any] = {"imgs": {}, "prehandle": None, "verify": None}

                def on_response(resp: Any) -> None:
                    u = resp.url
                    if "cap_union_new_getcapbysig" in u:
                        m = re.search(r"img_index=(\d+)", u)
                        if m:
                            # 保留**最新**一张（captcha 可能 reload）
                            state["imgs"][int(m.group(1))] = resp
                    elif "cap_union_prehandle" in u:
                        state["prehandle"] = resp
                    elif "cap_union_new_verify" in u:
                        state["verify"] = resp

                page.on("response", on_response)
                # 🔴 拦掉前端的发码请求：真正那一枪由主流水线用 requests 发。
                page.route("**/api/verification*", lambda route: route.abort())

                page.goto(self.entry_url, wait_until="domcontentloaded", timeout=45000)
                # 原来的固定 3000ms：改成等登录表单出现就往下走（通常 <1s）。
                page.wait_for_selector("#email", timeout=15000)

                # 切到注册 tab。🔴 必须等到**注册专属**字段出现 —— `#email`
                #    登录表单也有，所以"fill(#email) 没报错"不能证明切过去了。
                for _ in range(4):
                    try:
                        page.get_by_text("注册", exact=True).first.click(timeout=6000)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        page.wait_for_selector("#confirmPassword", timeout=8000)
                        break
                    except Exception:  # noqa: BLE001
                        page.wait_for_timeout(300)
                else:
                    raise AgnesCaptcha("进不去注册表单（#confirmPassword 未出现）")

                page.fill("#email", email)
                page.wait_for_timeout(200)
                page.wait_for_selector('img[alt="can send"]', timeout=10000)
                page.click('img[alt="can send"]', force=True, timeout=10000)

                # 等两张 captcha 图
                deadline = time.monotonic() + self.timeout_s
                while time.monotonic() < deadline and not (
                    0 in state["imgs"] and 1 in state["imgs"]
                ):
                    page.wait_for_timeout(300)
                if not (0 in state["imgs"] and 1 in state["imgs"]):
                    raise AgnesCaptcha(f"[timeout] 等 captcha 图片超时（{self.timeout_s:.0f}s）")

                bg_bytes = state["imgs"][1].body()
                sprite_bytes = state["imgs"][0].body()
                size_2d = _size_2d(state["prehandle"])
                points = None
                meta = None
                try:
                    points, meta = solve_bytes(bg_bytes, sprite_bytes, n=self.targets)
                    if not points:
                        raise AgnesCaptcha("[solver] 点选 solver 没有返回坐标")

                    frame = _find_captcha_frame(page)
                    if frame is None:
                        raise AgnesCaptcha("找不到 captcha frame（.tc-imgarea）")
                    _click_points(page, frame, points, size_2d)

                    # 等 verify
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline and state["verify"] is None:
                        page.wait_for_timeout(300)
                    if state["verify"] is None:
                        raise AgnesCaptcha("等 cap_union_new_verify 超时（确认没生效 / 答案被拒）")
                    body = json.loads(state["verify"].text())
                    if str(body.get("errorCode")) != "0":
                        raise AgnesCaptcha(
                            f"[rejected] captcha verify 拒绝：errorCode={body.get('errorCode')} "
                            f"errMessage={body.get('errMessage')!r}"
                        )
                    ticket = body.get("ticket") or ""
                    randstr = body.get("randstr") or ""
                    if not ticket or not randstr:
                        raise AgnesCaptcha("[no-ticket] captcha verify 返回了空 ticket/randstr")
                    return ticket, randstr
                except AgnesCaptcha as ex:
                    _save_debug_images(bg_bytes, sprite_bytes, points, meta, ex)
                    raise
            finally:
                if ctx is not None:
                    ctx.close()

    def close_thread(self) -> None:
        """释放**当前线程**的浏览器（生产者线程结束时调用）。

        ⚠ Playwright 的 sync 对象只能在创建它的线程里关闭 —— 所以不能从
          主线程统一收尾。`batch` 在每个生产者线程的 finally 里调这个。
        """
        _close_thread_session()
