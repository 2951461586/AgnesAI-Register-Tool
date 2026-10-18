"""配置的**最底层**：`.env` 加载 + 环境变量解析 + 仓库根。

（从 `common/config.py` 拆出；对外仍由 `common.config` 包统一转出。）
"""

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """极简 `.env` 解析（stdlib 实现，不引 python-dotenv 依赖）。

    只填充「尚未存在于 os.environ」的键 —— 真实环境变量优先级更高，
    这样临时覆盖（`AR_REG_QUOTA_MAX=1 python run.py`）依然生效。
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


ROOT = Path(__file__).resolve().parents[2]
_load_dotenv(ROOT / ".env")


def _env_int(name: str, default: int) -> int:
    """读整数环境变量。**坏值退回默认**，不抛。

    ⚠ 刻意不抛：这些是"行为参数"，一个手滑写进去的 `AR_PASSWORD_LEN=abc`
    不该让整个工具起不来 —— 它会在启动校验之后、第一个请求之前以
    `ValueError: invalid literal for int()` 的形态炸在很深的地方。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


__all__ = [
    "ROOT",
    "_load_dotenv",
    "_env_int",
    "_env_float",
    "_env_bool",
]
