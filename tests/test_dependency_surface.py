"""依赖面检查：`src/` 与 `common/` 只允许标准库 + `requests`。

为什么要有它：本项目声称"零浏览器、运行依赖只有 requests"。一旦有人在
`src/` 里 import 了别的第三方包，那个声明就变成假话 —— 而**部署时才炸**
（ModuleNotFoundError），且炸在别人机器上。

⚠ 白名单是**显式**的。要加新依赖，先在这里加，再在 `pyproject.toml` 里加 ——
   两处都改才说明是"有意的依赖"，而不是顺手 import 进来的。
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_THIRD_PARTY = {"requests"}
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
        for root in _module_roots(path):
            if root in stdlib or root in ALLOWED_THIRD_PARTY or root in ALLOWED_LOCAL:
                continue
            offenders.setdefault(path.name, set()).add(root)
    assert not offenders, f"出现了未登记的依赖：{offenders}"
