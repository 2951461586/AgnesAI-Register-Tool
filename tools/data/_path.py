"""`tools/` 子目录的引导垫片：把 `tools/` 插进 `sys.path` 后转交 `_bootstrap`。

为什么需要这一层：`python tools/data/xxx.py` 时 `sys.path` 上只有
**脚本所在目录**（`tools/data/`），所以 `tools/` 自己不在路径上 ——
直接 `import _bootstrap` 会 `ModuleNotFoundError`。先把 `tools/` 插进去，
再由 `_bootstrap` 插仓库根、重配控制台编码。

⚠ 它是**副作用导入**：必须在任何 `from src ...` **之前** import，
   所以 tools/ 下的脚本有结构性的 E402（见 pyproject 的 per-file-ignores）。
"""

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from _bootstrap import ROOT  # noqa: E402,F401
