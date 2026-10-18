"""结构性守卫：**静默降级（silent degradation）**。

本项目最怕的失败形态不是崩溃，而是**"结论全绿、原因错误"** —— 崩溃会有人修，
静默降级只会被当成事实写进文档和注释里。这个文件把已经踩过的几种形态
固化成**可执行**的规则。

本文件覆盖的形态
================

① **循环耗尽静默降级**（真实出过 **2 次**）
   `provision`（发码）—— 尝试被"本地闸门等不起 → 换维度"吃光，循环耗尽后
   **照样**去 `wait_for_code`，抱着 120s 等一封**从没被请求过**的信，报
   `等码超时 / rejected`。真相是"一个注册请求都没发"。
   `finish`（注册）—— 尝试被"IP 窗口等不起 → 换出口"吃光，循环耗尽后
   `body` 仍是 `None`，**照样** `mark_ok("register", "uid=")`，把**从没注册过**
   的账号标成成功，再拿它去登录 → `401 Invalid username or password`。
   真相是"注册根本没发出去"，而报告把原因指向**密码**。

② **`except` 吞异常**（体里只有 `pass` / `continue`）
   把异常吃掉而不留痕 ⇒ 上层只能重新分类，或干脆把"失败"当成"没有数据"。

③ **拿宽容读取器当合并基准**（真实出过 **1 次**）
   `load_existing` 在读源损坏时静默返回 `[]`，而 `run.py` 的流程是
   「读读源 → 合并 → 写回」⇒ 写出一份**只剩本批**的快照 ⇒ 历史账号永久丢失，
   而退出码是 0、报告全绿、连「结果合并：原有 N 条」那行都不打（`kept=0` 不打）。
   实测：50 条的读源被截断 ⇒ 写回 1 条，全程无告警。

④ **条件赋值 + 空值 coerce**（`finish` 那个 bug 的签名）—— ⚠ **未自动化**，
   见文末「已知未覆盖」一节。

约定
====
⚠ 白名单是**显式**的、带理由的。要加就写清为什么这里的降级是**正确**结果。
⚠ 每个白名单都配一条**反向用例**：白名单**不能腐烂** —— 它恰恰是守卫唯一的
   漏洞来源。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP = (ast.For, ast.While, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _files():
    for pkg in ("src", "common", "tools"):
        yield from sorted((ROOT / pkg).rglob("*.py"))


def _parse(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parents[c] = n
    return tree, parents


def _fn_name(node, parents):
    p = parents.get(node)
    while p is not None:
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return p.name
        p = parents.get(p)
    return "<module>"


# ══════════════════════════════════════════════════════════════════
# ① 循环耗尽静默降级
# ══════════════════════════════════════════════════════════════════

# (相对路径, 函数名) —— 这些循环的"耗尽"本身就是设计好的正常出口。
LOOP_ALLOWED = {
    ("src/captcha_solver.py", "_select_peaks"): (
        "两个循环都是**top-K 选取扫描**（尺度分桶优先 + 按分数补位）："
        "`continue` 是“这个桶已经有一个峰了 / 离已选峰太近”，耗尽 = "
        "找不到 K 个互不重叠的峰 —— 返回已选中的那些（可能是 `[]`）就是正常出口。"
        "这不是重试循环，没有“成功/失败”语义，也不存在耗尽后的静默降级。"
        "⚠ 原来这条挂在 `_ncc_peaks` 上；P1–P3 把扫描与选峰拆开后，扫描循环"
        "搬进了 `_iter_ncc`（只 `yield`、不 `continue` 消耗尝试），选峰逻辑留在"
        "这里 ⇒ 白名单跟着搬。"
    ),
    ("src/captcha.py", "_find_captcha_frame"): (
        "遍历 `page.frames` 找 captcha iframe。`continue` 是**跳过 detach 的 frame**，"
        "耗尽 = 没找到目标 frame，`None` 就是它的正常出口之一；调用方拿到 `None` 会"
        "抛 `AgnesCaptcha`（不是静默降级）。"
    ),
    ("src/tempmail.py", "wait_for_mail"): (
        "轮询循环：`while time.time() < deadline` 耗尽 = **等超时**，这正是设计"
        "结果（返回值 `None` 就是它的正常出口之一）。循环后那段不是降级，而是"
        "**分类**：`errs` / `last_err` 非空 ⇒ `last_error` 置位 ⇒ 调用方报"
        "「邮箱读不出来」，否则报「邮件没到」—— 两者的修法完全不同。"
        "内层 `for m in mails` 同理：没匹配到 = 本轮没有 ⇒ sleep 后再轮。"
    ),
    ("src/catchmail.py", "wait_for_mail"): (
        "与 `src/tempmail.py::wait_for_mail` **完全同构**（那是本模块的模板）："
        "`while time.time() < deadline` 耗尽 = **等超时**，返回值 `None` 就是它的"
        "正常出口之一。循环后那段是**分类**而不是降级：`errs` / `last_err` 非空"
        "⇒ `last_error` 置位 ⇒ 调用方报「邮箱读不出来」，否则报「邮件没到」"
        "—— 两者修法完全不同。\n"
        "    ⚠ 两个源共用同一套语义是**有意的**：`provision` 靠 `last_error` 是否"
        "为空来分流，两个源给出不同的语义会让那个分流变成碰运气。"
    ),
    ("src/smailr.py", "wait_for_mail"): (
        "与 `src/tempmail.py` / `src/catchmail.py` 的 `wait_for_mail` **完全同构**"
        "—— **三个源共用同一套语义是有意的**：`provision` 靠 `last_error` 是否为空"
        "来分流（空 = 邮件没到 → `MAIL_TIMEOUT`；非空 = 读不出来 → `MAIL_READ`），"
        "三个源给出不同语义会让那个分流变成碰运气。\n"
        "     `while time.time() < deadline` 耗尽 = **等超时**，`None` 就是它的正常"
        "出口之一；循环后那段是**分类**而不是降级。"
    ),
    ("tools/probes/probe_window_limits.py", "pick_slot"): (
        "两轮都是**偏好排序**，不是重试：第一轮只挑本地窗口看起来空的出口，"
        "耗尽 ⇒ 落到第二轮（任意未用过的出口）；第二轮耗尽 ⇒ 返回 `None`，"
        "调用方会返回 `no-slot` 并明确报出来。"
        "`continue` 是“跳过不满足条件的候选”，不是“消耗一次尝试后等下一次” ——"
        "与 `provision` / `finish` 那两个静默降级形态不同。"
    ),
}


def _stmts_after(node, parents):
    """循环之后**实际会执行到**的语句数（循环是块内最后一句时向上穿透）。"""
    cur = node
    while True:
        p = parents.get(cur)
        if p is None:
            return 0
        body = getattr(p, "body", None)
        if not isinstance(body, list) or cur not in body:
            for attr in ("orelse", "finalbody"):
                b = getattr(p, attr, None)
                if isinstance(b, list) and cur in b:
                    body = b
                    break
            else:
                return 0
        i = body.index(cur)
        if i < len(body) - 1:
            return len(body) - i - 1
        cur = p


def _own_flags(loop):
    """本层的 `continue` / 成功出口（**不进嵌套循环** —— 它们的属于自己）。

    🔴 必须判**语句自身**的类型，不能只看子节点：`return m` / `break` 常常是
       直接写在循环体里的语句，只遍历子节点会**整类漏掉**。
       （写这个扫描器时就是这么漏掉过一次，得到过"0 个风险"的假结论。）
    """
    flags = {"continue": False, "exit": False}
    stack = list(loop.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Continue):
            flags["continue"] = True
        elif isinstance(node, (ast.Break, ast.Return)):
            flags["exit"] = True
        for c in ast.iter_child_nodes(node):
            if isinstance(c, SKIP):
                continue
            stack.append(c)
    return flags


def _risky_loops():
    """所有「有 continue + 成功出口 + 无 else + 循环后有语句」的循环。"""
    out = []
    for path in _files():
        rel = path.relative_to(ROOT).as_posix()
        tree, parents = _parse(path)
        for n in ast.walk(tree):
            if not isinstance(n, (ast.For, ast.While)):
                continue
            if n.orelse or _stmts_after(n, parents) == 0:
                continue
            f = _own_flags(n)
            if f["continue"] and f["exit"]:
                out.append((rel, _fn_name(n, parents), n.lineno))
    return out


def test_retry_loops_must_have_else_clause():
    offenders = [
        f"{rel}:{line} in {fn}()"
        for rel, fn, line in _risky_loops()
        if (rel, fn) not in LOOP_ALLOWED
    ]
    assert not offenders, (
        "这些循环用 `continue` 消耗尝试、用 break/return 表示成功，但**耗尽后会落到"
        "后面的代码**（没有 `else:`）—— 那正是本项目出过两次「静默降级 / 假成功」"
        "的形态：\n  " + "\n  ".join(offenders) + "\n"
        "修法：加 `else:` 分支明确处理「一次都没成功」（抛错 / 判失败），"
        "或在 `LOOP_ALLOWED` 里写清为什么「耗尽」本身就是正确结果。"
    )


def test_loop_allowlist_entries_are_not_stale():
    live = {(rel, fn) for rel, fn, _ in _risky_loops()}
    stale = sorted(set(LOOP_ALLOWED) - live)
    assert not stale, f"LOOP_ALLOWED 里这些条目已经不需要了，请删掉：{stale}"


# ══════════════════════════════════════════════════════════════════
# ② except 吞异常（体里只有 pass / continue）
# ══════════════════════════════════════════════════════════════════

# (相对路径, 函数名, 异常类型名) —— 这里的"吞掉"是**有意**的。
EXCEPT_ALLOWED = {
    ("src/captcha.py", "_find_captcha_frame", "Exception"): (
        "遍历 `page.frames` 找 captcha iframe；某个 frame 在遍历途中被 detach 会让 "
        "`locator().count()` 抛 —— 跳过它继续找下一个是安全的（我们只是在找目标 "
        "frame，不是吞业务错误）。找不到会由调用方抛 `AgnesCaptcha`。"
    ),
    ("src/captcha.py", "_solve_once", "Exception"): (
        "注册 tab 的点击是**重试循环**：页面还在加载时 `get_by_text().click()` 会抛 "
        "timeout / detached，吞掉后由下一轮重试；4 轮都失败会走 `else` 抛 "
        "`AgnesCaptcha`。这里吞的是**定位/时序**错误，不是业务拒绝。"
    ),
    ("tools/ops/analyze_window_recovery.py", "_at", "(TypeError, ValueError)"): (
        '台账里的 `timings` 是**别人写的 JSON**：某个字段变成 `"12s"` / `null` 时，'
        "`int()` 会抛。这里只做**时间线近似**（判 15min 还是 60min 的量级），"
        "坏字段按 0 算 —— 让一个脏字段炸掉整份分析，比少算一个偏移更糟。"
        "⚠ 这是**离线分析器**，不是流水线：它不产生 `error_kind`，也没有 `rec` 可写。"
    ),
    ("src/jsonlstore.py", "read", "ValueError"): (
        "半截 JSONL 行（写盘时被杀 / 被人手改）只丢**这一行**，不丢整个文件。"
        "**状态文件是加速器不是数据**：丢一行的后果是闸门**偏松**（下一轮可能"
        "撞服务端墙），而让它抛的后果是**整批任务起不来** —— 两害相权取轻。"
        "⚠ 与之相对，**台账**那一侧的读源必须抛（见 `ledger.load_read_source`）——"
        "混用这两者会出真实事故（拿宽容读取器当合并基准 ⇒ 台账被静默抹光）。"
        "\n    📌 这里原本有**两条**白名单（`src/quota.py::_read_entries` 与"
        "`src/windowstore.py::_read_entries`）—— 因为那是同一套存储代码抄了两遍。"
        "抽成 `jsonlstore` 之后只剩这一条，而这正是白名单腐烂测试（下面那条）"
        "逼出来的：它先报新文件未登记，再报旧条目已失效。"
    ),
}


def _swallowing_handlers():
    """`except` 体里只有 `pass` / `continue` 的处理器。"""
    out = []
    for path in _files():
        rel = path.relative_to(ROOT).as_posix()
        tree, parents = _parse(path)
        for n in ast.walk(tree):
            if not isinstance(n, ast.ExceptHandler):
                continue
            if not n.body or not all(isinstance(s, (ast.Pass, ast.Continue)) for s in n.body):
                continue
            out.append((rel, _fn_name(n, parents), ast.unparse(n.type) if n.type else "(bare)"))
    return out


def test_swallowing_except_handlers_are_allowlisted():
    offenders = [
        f"{rel}::{fn} except {typ}"
        for rel, fn, typ in _swallowing_handlers()
        if (rel, fn, typ) not in EXCEPT_ALLOWED
    ]
    assert not offenders, (
        "这些 `except` 把异常整个吃掉（体里只有 pass / continue），既不留痕也不上报：\n  "
        + "\n  ".join(offenders)
        + "\n修法：要么让它抛（异常类型带着 `error_kind` 分类信息，吞掉就得在这里"
        "重新分类 = 第二份分类实现），要么记进 `rec` / 日志，"
        "或在 `EXCEPT_ALLOWED` 里写清为什么这里丢掉是安全的。"
    )


def test_except_allowlist_entries_are_not_stale():
    live = set(_swallowing_handlers())
    stale = sorted(set(EXCEPT_ALLOWED) - live)
    assert not stale, f"EXCEPT_ALLOWED 里这些条目已经不需要了，请删掉：{stale}"


# ══════════════════════════════════════════════════════════════════
# ③ 合并基准必须**严格读**（宽容读取器会把台账静默抹光）
# ══════════════════════════════════════════════════════════════════


def _run_ast():
    return ast.parse((ROOT / "run.py").read_text(encoding="utf-8"))


def test_merge_basis_is_read_strictly():
    """🔴 `run.py` 的**合并基准**必须来自 `load_read_source`（损坏就抛）。

    用宽容的 `load_existing` 会静默返回 `[]` ⇒ `merged = 本批` ⇒ 写回一份
    只剩本批的快照 ⇒ **历史账号永久丢失**，而退出码是 0、报告全绿。
    实测：50 条的读源被截断 ⇒ 写回 1 条，全程无告警。
    `save()` 的防缩水护栏拦不住这条（它比的是合并**前**的条数，而调用方传的是
    合并**后**的全量）。
    """
    tree = _run_ast()
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "merge_records"
    ]
    assert calls, "run.py 里找不到 merge_records 调用 —— 本用例的前提变了，请重看"
    for call in calls:
        assert call.args, "merge_records 没有位置实参？"
        basis = call.args[0]
        assert isinstance(basis, ast.Name), f"合并基准不是变量：{ast.unparse(basis)}"
        assigns = [
            a
            for a in ast.walk(tree)
            if isinstance(a, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == basis.id for t in a.targets)
        ]
        assert assigns, f"找不到 `{basis.id}` 的赋值"
        readers = [
            a.value.func.attr
            for a in assigns
            if isinstance(a.value, ast.Call) and isinstance(a.value.func, ast.Attribute)
        ]
        assert "load_read_source" in readers, (
            f"🔴 合并基准 `{basis.id}` 必须由 `load_read_source` 读出（损坏就抛）—— "
            f"实际的读取器：{readers or '(没有函数调用)'}。"
        )
        assert "load_existing" not in readers, (
            f"🔴 合并基准 `{basis.id}` 用了宽容读取器 `load_existing` —— "
            f"读源损坏时它静默返回 `[]`，会把历史账号抹光"
            f"（实测 50 条 → 1 条，而退出码还是 0）。"
        )


def test_lenient_reader_is_not_used_on_the_write_path():
    """`load_existing`（宽容）**不得**出现在写入路径（`run.py`）上。

    它服务的是**只读**用途（导出 key / 复核账号）—— 那些场景下"读不出来"与
    "没有数据"后果一样，宽容是对的。一旦上了写入路径，宽容就变成"静默抹账"。
    """
    tree = _run_ast()
    bad = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "load_existing"
    ]
    assert not bad, (
        f"run.py 在 {bad} 处调用了 `load_existing`（宽容读取器）。"
        f"写入路径必须用 `load_read_source` —— 详见它的 docstring。"
    )


# ══════════════════════════════════════════════════════════════════
# 已知未覆盖（**有意不自动化**，写在这里当路标）
# ══════════════════════════════════════════════════════════════════
#
# ④ **条件赋值 + 空值 coerce**
#    `x = None`（或 `{}` / `[]` / `""`）→ 在某个循环 / try 里赋值（该构造有
#    `continue` / `except` 可以跳过赋值）→ 循环后写成 `(x or {}).get(...)`，
#    于是"一次都没成功"被 coerce 成"空结果"。`finish` 那个假成功正是这个签名。
#
#    ⚠ **不做成自动规则**：它的"安全"与"危险"取决于**派生值有没有被校验** ——
#      `finish` 现在的 `(body or {}).get("data")` 是安全的，因为紧接着就有
#        if not isinstance(user, dict) or not user.get("id"): fail(...)
#      精确判断需要数据流分析；粗糙版本（只看 coerce）会给 `finish` 开一条
#      **函数级**白名单，而那会把它将来新增的、真正没校验的 coerce 一起豁免掉 ——
#      比不做更坏。
#
#    ✅ 替代覆盖：行为用例。这个签名的真实实例由
#      `tests/test_pipeline.py::test_register_response_without_user_is_rejected`
#      钉住（"HTTP 200 的空壳必须判失败"）。
#    🔎 要人工复查时，按这个签名搜：`x = None` + 循环/`try` 内赋值 +
#      循环后 `x or {}` / `x.get(...)`，且**派生值没有被校验**。
