"""批量收尾报告（**纯展示**）。

为什么单独一个模块
==================
两个理由，第二个才是关键：

1. 这 ~200 行是纯展示逻辑，混在 CLI 入口里会把真正的流程控制淹没；
2. **测试链不该 import `run.py`** —— 那会把整套 pipeline + requests 拉进
   `sys.path`。而这块 stdout 是**逐字节契约**（人靠它判断"这批跑得怎么样"），
   必须有契约测试钉住，所以它得待在能被测试 import 的层。

所以本模块**只依赖 stdlib**：`quota` / `config` / `error_kind_of` 全部由调用方
**注入**。这样报告层的改动不可能顺手把 `requests` 带进测试。

报告要回答的四个问题
--------------------
1. 这批**成了几个**（以及没成的为什么没成）—— 按 `error_kind` 分类，不是按文案；
2. **每个账号的凭据与额度**（台账的副本视图，方便当场核对）；
3. 时间花在哪 —— 阶段分解，且打印**最慢的那个账号**（批量总时长由它决定，
   不是均值）；
4. 台账落在哪 —— 路径要相对仓库根（不带盘符 / 用户名）。
"""

from statistics import median


def _field(rec, name: str, default=None):
    """同时吃 dict 与对象 —— 台账记录是 dict，本批记录是 `AccountRecord`。"""
    if isinstance(rec, dict):
        return rec.get(name, default)
    return getattr(rec, name, default)


def _timings(rec) -> dict:
    """取 `timings`。**保证返回 dict**。

    ⚠ 台账是**人工可读**的 JSON，可能被人手改过（塞个注释、把 `timings`
      改成字符串）。报告层崩掉的代价特别高：那意味着**这批跑了什么完全看不到了**
      （而台账文件本身可能还在写）。所以这里一律降级成空 dict。
    """
    t = _field(rec, "timings")
    return t if isinstance(t, dict) else {}


def _as_int(raw, default=None):
    """宽容取整；取不到返回 `default`。"""
    if isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _fmt_ms(v) -> str:
    v = _as_int(v)
    if v is None:
        return "-"
    return f"{v}ms" if v < 1000 else f"{v / 1000:.1f}s"


def count_by_status(records) -> dict:
    out = {"success": 0, "failed": 0, "skipped": 0}
    for r in records:
        st = str(_field(r, "status") or "failed")
        out[st] = out.get(st, 0) + 1
    return out


def error_histogram(records, error_kind_of) -> dict:
    """`error_kind` → 条数（只统计非空）。

    🔴 读点必须走**注入的** `error_kind_of`，不要在报告里再判一次文本 ——
       Intern-Register-Tool 的教训是：靠"在 error 文本里搜关键词"分类，
       会把**我们自己拼的**文案（引用了服务端的词）也算成服务端的证据。
    """
    hist: dict = {}
    for r in records:
        kind = error_kind_of(r)
        if kind:
            hist[kind] = hist.get(kind, 0) + 1
    return hist


def stage_stats(records, stages) -> list:
    """每个阶段的 `(阶段名, 中位, 最大)` 耗时（毫秒）；没有样本的阶段给 `None`。

    ⚠ 返回的**阶段名不带 `_ms` 后缀**（就是 `STAGES` 里的名字），因为 `timings`
      的键是 `<阶段>_ms` 而阶段名是 `<阶段>` —— 两种叫法混在一起很容易让人
      在调用处写错。

    ⚠ 打印**最大**值而不只是中位：批量总时长由最慢那个账号决定，
      只看中位会让人以为"这批很快"，而实际墙钟被某个离群账号拖住。
    """
    out = []
    for name in stages:
        vals = []
        for r in records:
            v = _as_int(_timings(r).get(f"{name}_ms"))
            if v is not None:
                vals.append(v)
        out.append((name, int(median(vals)) if vals else None, max(vals) if vals else None))
    return out


def slowest(records):
    """本批**总耗时最长**的那条记录（没有 timings 时返回 None）。"""
    best, best_total = None, -1
    for r in records:
        total = 0
        for v in _timings(r).values():
            total += _as_int(v, 0) or 0
        if total > best_total:
            best, best_total = r, total
    return best


def render_account_line(rec, error_kind_of, redact_email=None) -> str:
    """一行一个账号：状态 + 邮箱 + 关键字段。"""
    status = str(_field(rec, "status") or "failed")
    mark = {"success": "✓", "skipped": "∅"}.get(status, "✗")
    email = str(_field(rec, "email") or "(无邮箱)")
    if redact_email:
        email = redact_email(email)
    if status != "success":
        kind = error_kind_of(rec) or "?"
        return f"  {mark} {email:<44} [{kind}] {str(_field(rec, 'error') or '')[:100]}"
    bits = [f"plan={_field(rec, 'plan') or 'free'}"]
    key = str(_field(rec, "api_key") or "")
    if key:
        # 🔴 **只打前后缀**（`sk-1a2b…c3d4`）。整把 key 已经落进台账了，
        #    终端这一行会被贴进 issue / 截图 —— 那是它最容易泄漏的地方。
        bits.append(_mask_key(key))
    verify = str((_field(rec, "stages") or {}).get("verify") or "")
    if verify:
        bits.append(f"verify={verify}")
    return f"  {mark} {email:<44} {'｜'.join(bits)}"


def _mask_key(key: str) -> str:
    """`sk-abcdefgh…wxyz` —— 中间打码，保留可人眼核对的头尾。"""
    k = (key or "").strip()
    if len(k) <= 14:
        return k
    return f"{k[:9]}…{k[-4:]}"


def render_slot_section(slot_pool, redact_url=None, log=print) -> None:
    """每个出口的用量与成败。

    为什么值得占几行：注册限流是**按出口**的，所以"哪个出口用得多/失败得多"
    直接决定了下一批该不该换掉某个出口。这与 Intern-Register-Tool 把
    `proxy_slot` 写进台账是同一个理由（那边写的是"被封的到底是哪个出口"）。

    ⚠ `failed` 只统计 `network` 类失败 —— 把"邮箱已注册"记到出口头上，
      会让每个出口的失败数变成噪音，这一栏就没人看了。
    """
    if not slot_pool or not len(slot_pool):
        return
    log("─" * 62)
    log("出口（槽位池）")
    for st in slot_pool.stats():
        shown = redact_url(st.url) if redact_url else st.url
        line = f"  {st.label:<6} 用 {st.used:<3} 成功 {st.ok:<3} 失败 {st.failed:<3} {shown}"
        if st.failed and st.last_error:
            line += f"\n         最近错误：{st.last_error[:100]}"
        log(line)


def render_batch_report(
    records,
    written_path,
    wall_seconds: float,
    *,
    stages,
    error_kind_of,
    quota_line: str = "",
    workers: int = 1,
    repo_rel=None,
    redact_email=None,
    slot_pool=None,
    redact_url=None,
    log=print,
) -> None:
    """打印收尾报告。**依赖全部注入**，本模块不 import 任何项目内模块。

    🔴 这个关键字契约由 `tests/test_cli_contract.py` 钉住：它用 AST 取出
      `run.py` 调用点上的关键字，逐个对**本模块**的签名核一遍。
    """
    n = len(records)
    by = count_by_status(records)
    per = wall_seconds / n if n else 0

    log("\n" + "═" * 62)
    log(
        f"结果：成功 {by.get('success', 0)} / {n}"
        f"（失败 {by.get('failed', 0)}，跳过 {by.get('skipped', 0)}）"
        f"｜墙钟 {wall_seconds:.1f}s｜{workers} 路并发 → {per:.2f}s/账号"
    )
    log("─" * 62)
    for r in records:
        log(render_account_line(r, error_kind_of, redact_email))

    if by.get("success"):
        log("─" * 62)
        log("阶段耗时（中位 / 最大）")
        for name, med, mx in stage_stats(records, stages):
            if med is None:
                continue
            note = ""
            if name == "code":
                waits = [
                    w for w in (_as_int(_timings(r).get("code_wait_ms")) for r in records) if w
                ]
                if waits:
                    # 等验证码是整链最大单项 —— 单独拆出来，否则会被读成"发信慢"。
                    note = f"   ← 其中等码 中位 {_fmt_ms(int(median(waits)))}"
            log(f"  {name:<10} {_fmt_ms(med):>8} / {_fmt_ms(mx):>8}{note}")

        slow = slowest(records)
        if slow is not None:
            parts = " ".join(f"{k[:-3]}={_fmt_ms(v)}" for k, v in _timings(slow).items() if v)
            # 🔴 这里的邮箱**也要打码** —— 逐行渲染那条路打了码，这一条漏了的话，
            #    明文邮箱就从"最慢账号"这一行漏出去。
            slow_email = str(_field(slow, "email") or "")
            if redact_email:
                slow_email = redact_email(slow_email)
            log(f"最慢账号（决定本批总时长）：{slow_email}\n  {parts}")

    hist = error_histogram(records, error_kind_of)
    if hist:
        log("─" * 62)
        log("错误分类（error_kind）")
        for kind, cnt in sorted(hist.items(), key=lambda kv: -kv[1]):
            log(f"  {kind:<12} {cnt}")

    render_slot_section(slot_pool, redact_url, log=log)

    if quota_line:
        log("─" * 62)
        log(f"本地配额：{quota_line}")
    if written_path:
        shown = repo_rel(written_path) if repo_rel else str(written_path)
        log(f"台账：{shown}")
    log("═" * 62)
