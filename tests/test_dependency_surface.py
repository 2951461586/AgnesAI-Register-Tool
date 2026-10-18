"""依赖面检查：`src/` 与 `common/` 只允许标准库 + `requests`。

为什么要有它：本项目声称“运行依赖只有 requests”。一旦有人在 `src/` 里
import 了别的第三方包，那个声明就变成假话 —— 而**部署时才炸**
（ModuleNotFoundError），且炸在别人机器上。

⚠ 白名单是**显式**的。要加新依赖，先在这里加，再在 `pyproject.toml` 里加 ——
   两处都改才说明是“有意的依赖”，而不是顺手 import 进来的。

⚠ **captcha 的可选依赖**（`playwright` / `numpy` / `scipy` / `PIL`）只允许出现在
   `src/captcha.py` / `src/captcha_solver.py` 里，且由 `pyproject.toml` 的
   `[captcha]` extra 提供。不装时 `config.validate()` 会在启动阶段报错，
   或 `AR_CAPTCHA_ENABLED=0` 关掉。
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_THIRD_PARTY = {"requests"}
# captcha 的**可选**依赖：只在 `src/captcha.py` / `src/captcha_solver.py` 里允许。
# 它们由 `pyproject.toml` 的 `[captcha]` extra 提供；不装时 `config.validate()`
# 会在启动阶段报错（或 `AR_CAPTCHA_ENABLED=0` 关掉）。
OPTIONAL_THIRD_PARTY = {"playwright", "numpy", "scipy", "PIL"}
OPTIONAL_FILES = {"captcha.py", "captcha_solver.py"}
ALLOWED_LOCAL = {"src", "common"}


def _module_roots(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                roots.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对 import（from . import x）
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _source_files():
    for pkg in ("src", "common"):
        yield from (ROOT / pkg).rglob("*.py")


def test_only_allowed_imports():
    stdlib = set(sys.stdlib_module_names)
    offenders = {}
    for path in _source_files():
        allowed = ALLOWED_THIRD_PARTY
        if path.name in OPTIONAL_FILES:
            allowed = ALLOWED_THIRD_PARTY | OPTIONAL_THIRD_PARTY
        for root in _module_roots(path):
            if root in stdlib or root in allowed or root in ALLOWED_LOCAL:
                continue
            offenders.setdefault(path.name, set()).add(root)
    assert not offenders, f"出现了未登记的依赖：{offenders}"
