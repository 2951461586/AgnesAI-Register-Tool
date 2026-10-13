"""把仓库根加进 `sys.path`（副作用导入）。

为什么每个子目录各有一份：`python tools/probes/xxx.py` 时，Python 只把
**脚本所在目录**（`tools/probes/`）加进 `sys.path`，所以 `tools/` 本身
不在路径上 —— `from _bootstrap import ROOT` 会 `ModuleNotFoundError`。
`tools/` 与 `src/` 是平级兄弟目录，把仓库根插进去之后
`from src import ...` 才成立。

⚠ 它是**副作用导入**：必须在任何 `from src ...` **之前** import，
   所以 tools/ 下的脚本有结构性的 E402（见 pyproject 的 per-file-ignores）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
