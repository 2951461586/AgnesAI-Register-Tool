"""测试辅助：读 `common/config/` 包的全部源码。

配置已从单文件 `common/config.py` 拆成包，所以"读配置源码"的契约用例
必须扫**整个包** —— 只读 `__init__.py` 会漏掉子模块里的旋钮，而漏掉
不会报错，只会让 `.env.example` / captcha 默认值的守卫**形同不存在**。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def config_source_text() -> str:
    """`common/config/*.py` 的拼接源码（含门面与所有子模块）。"""
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted((ROOT / "common" / "config").glob("*.py"))
    )
