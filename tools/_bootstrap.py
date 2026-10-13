"""`tools/` 下脚本的公共引导（**副作用导入**）。

做两件事，顺序不能换：

1. 把**仓库根**加进 `sys.path` —— `python tools/probes/xxx.py` 时 Python 只把
   **脚本所在目录**（`tools/probes/`）加进去，`from src import ...` 会
   `ModuleNotFoundError`。`tools/` 与 `src/` 是平级兄弟目录。
2. 把 stdout / stderr 重配成 UTF-8 —— Windows 控制台默认 GBK，而本项目的输出
   里有 emoji，不重配会在**第一行提示**就 `UnicodeEncodeError` 崩掉
   （实测：`probe_slots.py` 的收尾 `✅` 行）。

⚠ 必须在任何 `from src ...` **之前** import，所以 `tools/` 下的脚本有
   **结构性**的 E402（见 pyproject 的 per-file-ignores）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cli import setup_console  # noqa: E402

setup_console()
