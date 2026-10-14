"""JSONL 状态存储 —— 追加、宽容读取、原子重写（**唯一实现**）。

为什么单独一个模块
==================
`quota.py`（爆炸半径计数）与 `windowstore.py`（滑动窗口事件）用的是**同一个
存储形态**：一行一个 JSON 对象、追加写入、坏行只丢那一行、行数过多时机会式
重写。两处各写一遍的代价不是行数，而是**修复只会改一处** ——
本项目的 `windowstore.py` 就是照着 `quota.py` 抄出来的，而抄的时候并没有
意识到自己在抄（那是"意外重复"，与 `report.py` 那种"故意不带项目内依赖"
的重复完全不同）。

🔴 宽容读取 vs 严格读取 —— **本项目最值钱的一条区分**
-----------------------------------------------------
| | 读不出来时 | 为什么 |
| --- | --- | --- |
| 状态文件（**本模块**） | 返回空 ⇒ 闸门**偏松** | 它是**加速器不是数据**：丢了只影响下一轮的保守程度 |
| 台账（`ledger.load_read_source`） | **抛异常** | 它是**数据**：邮箱是临时的、密码与 `sk-` key 只存在于那里，丢了就是永久丢账号 |

⚠ 把这两者混用会出真实事故：拿宽容读取器当**合并基准** ⇒ 读源一坏就写成
"只剩本批"的快照 ⇒ 历史账号被静默抹光，而退出码还是 0。实测出过
（`load_existing` 当基准，50 条 → 1 条）。所以本模块的宽容是**有意的**，
但**只对状态文件成立**。

⚠ 本模块只管**存储**。"窗口内 / 窗口外"、"什么时候压缩"这类**策略**留在各自
的模块里 —— 两者的保留期与阈值本来就不同（配额按行数、窗口按字节）。
"""

import json
import os
import time
from pathlib import Path

from . import fsutil


def as_ts(raw) -> float:
    """宽容地取时间戳。**坏值退回 0**，不抛。

    ⚠ 状态文件是**本地**的、可能被人手改过（比如手工塞一行进去看闸门行为）。
      一个 `"ts": "yesterday"` 不该让整批任务炸在 `float()` 上 ——
      退回 0 的后果只是这条记录被当成窗口外，是安全方向的降级。
    """
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


class JsonlStore:
    """一个 JSONL 状态文件。**所有方法都不抛 OSError**（状态是加速器）。"""

    def __init__(self, env_var: str, default_path, *, label: str):
        self._env_var = env_var
        self._default = Path(default_path)
        # 告警文案里的名字（如"配额计数"）—— 让用户一眼知道**哪个文件**出问题。
        self._label = label

    def path(self) -> Path:
        """状态文件路径。`env_var` 可覆盖（测试靠它隔离）。

        🔴 在**调用时**读环境变量，不受导入顺序影响 —— 与 `config` 的常量不同，
          这样测试可以在 import 之后改环境变量而不必重载模块。
        🔴 不隔离的后果很具体：跑一次测试就往**真实** `state/` 里写记录，而那个
          文件会影响**下一次真实跑批**的闸门（实测踩过：测试把额度吃掉）。
        """
        override = (os.getenv(self._env_var) or "").strip()
        if override:
            return Path(override).expanduser()
        return self._default

    def read(self) -> list[dict]:
        """读全部条目。**文件不在 / 读不出来 / 坏行都只是少几条**，不抛。

        ⚠ 半截行（写盘时被杀）只丢**那一行**，不丢整个文件 —— 这个降级已登记在
          `tests/test_silent_degradation.py` 的 `EXCEPT_ALLOWED` 里（带理由）。
        """
        p = self.path()
        if not p.is_file():
            return []
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            return []
        out: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
        return out

    def append(self, entry: dict) -> bool:
        """追加一行。**写失败只告警不抛**，返回是否写成功。

        🔴 为什么是**追加**而不是"读整个文件 → 改 → 原子替换"：追加一行是原子的，
          两个进程同时跑不会互相覆盖；而读-改-写必然丢事件，**丢事件的方向是
          更松** —— 正是最不该出错的方向。
        """
        p = self.path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry, ensure_ascii=False)
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as ex:
            print(
                f"  ⚠ {self._label}写不进去（{type(ex).__name__}: {ex}）—— 本次不记账，闸门会偏松"
            )
            return False
        return True

    def rewrite(self, entries: list[dict]) -> None:
        """原子重写整个文件（压缩用）。**失败只告警不抛**。

        ⚠ 与 `append` 不同，这是**读-改-写**，所以在并发下是尽力而为：两个进程
          同时重写可能丢条目。方向是**更松**，可接受 —— 且它只在"文件明显变大"
          这条非热路径上被调用。
        """
        text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
        try:
            fsutil.atomic_write_text(self.path(), text)
        except OSError as ex:
            print(f"  ⚠ {self._label}压缩失败（{type(ex).__name__}: {ex}）—— 保持原文件")

    def now(self) -> float:
        """当前时间。抽成方法是为了让测试能 patch `jsonlstore.time.time`。"""
        return time.time()
