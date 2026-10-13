"""CLI 入口共用的骨架：控制台编码 + 台账合并摘要。

为什么单独一个模块
==================
`run.py`（注册主链）与 `tools/` 下的脚本都会（a）把结果合并回台账、并打一行
摘要，（b）在 Windows 的 GBK 控制台上打 emoji。两件事都曾经是**逐字重复**的。
重复的代价不是"多打几行字"，而是**漂移**：改一边忘一边，两个入口对同一件事
给出不同说法 —— 而这类漂移**不会报错**。

⚠ 本模块**只放各入口经核对真正等价的部分**。落盘分支的判据就**不**抽：
`run.py` 判 `out is None`（显式给 `--out` ⇒ 纯导出、不落快照），
而复核工具判 `ledger.is_ledger_path(dest)`。"显式 `--out` 指向 `ledger/` 内部"
这一种输入下两者行为不同，且各自的 `--help` 都把自己那套写成了承诺 ⇒
**"重复"不等于"等价"，先证明再合并**。
"""

import sys


def setup_console() -> None:
    """把 stdout / stderr 钉在 UTF-8。已经是 UTF-8 时**什么都不做**。

    🔴 为什么必需：Windows 控制台默认编码是 **GBK(cp936)**，而本项目的输出
       里到处是 emoji（`⚠` / `✅` / `🔴` …）。GBK 编不出 `U+26A0`，于是
       `print()` 直接抛 `UnicodeEncodeError`。

    实测过的崩法：`run.py` 在**启动阶段的配额闸门提示行**（`⚠ …`）上崩掉，
    exit 码还停在 0 —— 一个请求都没发出去，看起来却像"配置有问题"。
    比"跑一半崩"更难查。

    🔴 用 `errors="replace"` 而不是默认的 `strict`：宁可把打不出的字符降级成
       `?`，也不能让**告警行本身**把进程打死 —— 告警存在的意义就是让人看见。

    ⚠ 只改**编码与错误策略**，不动缓冲策略（`line_buffering` 等）：那是各入口
       自己的事，混进来会让"为什么这行没及时打出来"变成第二个谜。

    对"不是 `TextIOWrapper`"的流（被重定向成自定义对象、pytest 的捕获对象）
    安静跳过 —— 它们没有 `reconfigure`，硬调会 `AttributeError`。
    """
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower()
        if enc.replace("-", "") == "utf8":
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        reconfigure(encoding="utf-8", errors="replace")


def merge_summary_line(kept: int, added: int, upgraded: int, total: int) -> str:
    """「结果合并：原有 N 条 + 本次新增 M 条（K 条已更新：升级或补全字段）= T 条」。

    🔴 返回的字符串**带前导 `\\n`** —— 它就是 `print()` 出去的那一行。
       保持逐字节相同是抽取的前提，所以换行跟着文本一起走。

    ⚠ 只返回**文本**，不打印、不带 `if kept:` 判断 —— 调用方在那里可能不同。
    """
    extra = f"（{upgraded} 条已更新：升级或补全字段）" if upgraded else ""
    return f"\n结果合并：原有 {kept} 条 + 本次新增 {added} 条{extra} = {total} 条"
