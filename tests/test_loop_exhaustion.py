"""结构性守卫：**循环耗尽不能静默降级**。

为什么要有它
============
这个形态在本项目里**真实发生过两次**，而且两次都产出了"看起来合理但是错"
的结论 —— 比崩溃难查得多：

  ① `provision`（发码）—— 尝试被"本地闸门等不起 → 换维度"吃光，循环耗尽后
     **照样**去 `wait_for_code`，抱着 120s 等一封**从没被请求过**的信，报
     `等码超时 / rejected`。真相是"一个注册请求都没发"。
  ② `finish`（注册）—— 尝试被"IP 窗口等不起 → 换出口"吃光，循环耗尽后
     `body` 仍是 `None`，**照样** `mark_ok("register", "uid=")`，把**从没注册过**
     的账号标成成功，再拿它去登录 → `401 Invalid username or password`。
     真相是"注册根本没发出去"，而报告把原因指向**密码**。

两次的形态**完全一样**：

    用 `continue` 消耗尝试  +  用 `break`/`return` 表示成功  +  无 `else`
    ⇒ 耗尽后落到后面的代码，而那一段假设"循环成功了"

所以这里把它钉死：**这种循环必须有 `else:` 分支**，显式处理"一次都没成功"。

⚠ 白名单是**显式**的，且带理由 —— 要加就写清为什么"耗尽"本身就是正确结果。
⚠ 还有一条反向用例：白名单**不能腐烂**（循环被改/删之后条目要跟着删）。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (相对路径, 函数名) —— 这些循环的"耗尽"本身就是设计好的正常出口。
ALLOWED = {
    ("src/tempmail.py", "wait_for_mail"): (
        "轮询循环：`while time.time() < deadline` 耗尽 = **等超时**，这正是设计"
        "结果（返回值 `None` 就是它的正常出口之一）。循环后那段不是降级，而是"
        "**分类**：`errs` / `last_err` 非空 ⇒ `last_error` 置位 ⇒ 调用方报"
        "「邮箱读不出来」，否则报「邮件没到」—— 两者的修法完全不同。"
        "内层 `for m in mails` 同理：没匹配到 = 本轮没有 ⇒ sleep 后再轮。"
    ),
}

SKIP = (ast.For, ast.While, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _parents(tree):
    parents = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parents[c] = n
    return parents


def _enclosing_fn(node, parents):
    p = parents.get(node)
    while p is not None:
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return p.name
        p = parents.get(p)
    return "<module>"


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
    for pkg in ("src", "common", "tools"):
        for path in sorted((ROOT / pkg).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents = _parents(tree)
            for n in ast.walk(tree):
                if not isinstance(n, (ast.For, ast.While)):
                    continue
                if n.orelse or _stmts_after(n, parents) == 0:
                    continue
                f = _own_flags(n)
                if f["continue"] and f["exit"]:
                    out.append((rel, _enclosing_fn(n, parents), n.lineno))
    return out


def test_retry_loops_must_have_else_clause():
    offenders = [f"{rel}:{line} in {fn}()" for rel, fn, line in _risky_loops() if (rel, fn) not in ALLOWED]
    assert not offenders, (
        "这些循环用 `continue` 消耗尝试、用 break/return 表示成功，但**耗尽后会落到"
        "后面的代码**（没有 `else:`）—— 那正是本项目出过两次「静默降级 / 假成功」"
        "的形态：\n  " + "\n  ".join(offenders) + "\n"
        "修法：加 `else:` 分支明确处理「一次都没成功」（抛错 / 判失败），"
        "或在 `ALLOWED` 里写清为什么「耗尽」本身就是正确结果。"
    )


def test_allowlist_entries_are_not_stale():
    """白名单不能变成"永久豁免"：条目失效（循环被删或改了形态）就该删掉。

    没有这条的话，白名单会慢慢变成一份"谁也不敢动"的名单 —— 而它恰恰是
    这个守卫唯一的漏洞来源。
    """
    live = {(rel, fn) for rel, fn, _ in _risky_loops()}
    stale = sorted(set(ALLOWED) - live)
    assert not stale, f"ALLOWED 里这些条目已经不需要了，请删掉：{stale}"
