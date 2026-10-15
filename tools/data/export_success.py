#!/usr/bin/env python
"""把台账里**成功**的账号汇总到一个目录（给下游 / 存档用）。

用法：
    python tools/data/export_success.py                 # 写 ledger/success/
    python tools/data/export_success.py --out some/dir  # 换目录

产出（默认 `ledger/success/`）：

    accounts.json   全部成功记录，**原样保序** —— 这是权威副本（读它，别读 csv）
    accounts.csv    扁平表（表格 / 下游用）
    keys.txt        每行一把 `sk-` key（只含有的）
    SUMMARY.md      汇总（条数 / 成功率 / 按日期 / 按收信域名 / 按 plan）

🔴 **输出全是明文凭据**（密码 / JWT / `sk-` key）。默认落点 `ledger/success/`
   在 `ledger/` 之下 ⇒ 已被 `.gitignore` **整目录**排除（见 `.gitignore`
   §凭据 —— 那里是唯一真源，别在这里另写判断：这条注释曾经写着"在"而实际
   不在，于是"文档承诺了安全、实现没有"）。换 `--out` 时请自己确认目标
   目录也被忽略，**不要**拷进仓库或贴到别处。

为什么单独一个工具（而不是在报告里顺手写一份）
--------------------------------------------
`ledger/runs/` 里是**每次跑批一份全量快照**，读源是"最新的那一份"。要一个
"只要成功的、扁平、能直接喂给下游"的视图，就得有一个**可重复**的入口 ——
手写一次性的脚本，下次跑批后又得重来，而重来时的口径未必和上次一样。

⚠ 读的是 `ledger_path()`（= 最新全量快照），用 `load_existing`（**宽容**：
   读源损坏时返回 `[]` 而不是抛）。宽容在这里是对的：本工具是**只读导出**，
   损坏时"导不出"比"起不来"更安全（对比 `run.py` 的合并流程必须用
   `load_read_source` —— 那里损坏必须抛，否则会写出只剩本批的快照）。
"""

import argparse
import csv
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from _path import ROOT  # noqa: F401,E402

from src import fsutil  # noqa: E402
from src import ledger as _ledger  # noqa: E402

# 默认落点。放在 `ledger/` 之下 ⇒ 跟着台账一起被 gitignore（整目录）。
DEFAULT_OUT = _ledger.LEDGER_DIR / "success"

# CSV 的列。**显式写死**（不用 `rows[0].keys()`）：顺序稳定，下游才能按位置读；
# 而且字段增删会在 diff 里看得见，不会因为某条记录恰好多一个键就变列。
CSV_FIELDS = (
    "email",
    "password",
    "user_id",
    "user_uuid",
    "api_key",
    "key_id",
    "plan",
    "balance_minor",
    "balance_currency",
    "key_max",
    "models_count",
    "created_at",
    "mail_domain",
    "mailboxes_created",
    "proxy",
)


def select_success(records) -> list[dict]:
    """只留 `status == "success"` 且有 `email` 的记录（**保序 + 按 email 去重**）。

    ⚠ 去重是必须的：`merge_records` 按 email 合并，正常不会重复；但台账是
      人工可写的目录，一条重复的 email 会让下游**重复注册 / 重复计费**，
      而那种错误在下游很难查回来。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for r in records:
        if r.get("status") != "success":
            continue
        email = r.get("email") or ""
        if not email or email in seen:
            continue
        seen.add(email)
        out.append(r)
    return out


def summarize(rows: list[dict], *, total: int, when: str) -> str:
    """`SUMMARY.md` 的正文。`total` = 台账总条数（用来算成功率）。"""
    keys = [r for r in rows if r.get("api_key")]
    dates = Counter((r.get("created_at") or "")[:10] for r in rows)
    domains = Counter(r.get("mail_domain") or "(未知)" for r in rows)
    plans = Counter(r.get("plan") or "(未知)" for r in rows)
    rate = f"{len(rows) / total:.0%}" if total else "—"

    def table(counter: Counter, head: str) -> str:
        lines = [f"| {head} | 账号数 |", "| --- | --- |"]
        lines += [f"| `{k}` | {v} |" for k, v in sorted(counter.items())]
        return "\n".join(lines)

    span = "—"
    real = sorted(d for d in dates if d)
    if real:
        span = real[0] if len(real) == 1 else f"{real[0]} ~ {real[-1]}"

    return (
        "# 成功账号汇总\n\n"
        f"- 生成时间：{when}\n"
        f"- **成功账号：{len(rows)} 个**（台账总 {total} 条，成功率 {rate}）\n"
        f"- 带 `sk-` key 的：{len(keys)} 个\n"
        f"- 时间跨度：{span}\n\n"
        "## 按日期\n\n"
        f"{table(dates, '日期')}\n\n"
        "## 按收信域名\n\n"
        f"{table(domains, '域名')}\n\n"
        "## 按 plan\n\n"
        f"{table(plans, 'plan')}\n\n"
        "---\n\n"
        "⚠ 同目录下的 `accounts.json` / `accounts.csv` / `keys.txt` 是**明文凭据**"
        "（密码 / JWT / `sk-` key）。本目录在 `.gitignore` 覆盖范围内，"
        "但**别把这里的文件拷进仓库或贴到别处**。\n"
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="汇总台账里成功的账号")
    ap.add_argument("--out", default=None, help=f"输出目录（默认 {DEFAULT_OUT}）")
    args = ap.parse_args(argv)

    records = _ledger.load_existing(_ledger.ledger_path())
    if not records:
        print("✗ 台账里没有记录", file=sys.stderr)
        return 1
    rows = select_success(records)
    if not rows:
        print("✗ 台账里没有成功的账号", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)

    # ① 权威副本：原样保序（下游该读这个，不是 csv）
    fsutil.atomic_write_json(out / "accounts.json", rows)

    # ② 扁平表。⚠ 用 `tmp_path_for` + `os.replace` 而不是 `atomic_write_text`：
    #    csv 需要 `newline=""`（否则 Windows 上每行多一个 \r）。
    csv_path = out / "accounts.csv"
    tmp = fsutil.tmp_path_for(csv_path)
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(CSV_FIELDS), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, csv_path)

    # ③ 只要 key 的下游（每行一把）
    keys = [r["api_key"] for r in rows if r.get("api_key")]
    fsutil.atomic_write_text(out / "keys.txt", "\n".join(keys) + ("\n" if keys else ""))

    # ④ 汇总
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fsutil.atomic_write_text(out / "SUMMARY.md", summarize(rows, total=len(records), when=when))

    print(
        f"汇总 {len(rows)} 个成功账号（台账 {len(records)} 条）→ {_ledger.repo_rel(out)}/\n"
        f"  accounts.json（权威副本，{len(rows)} 条）\n"
        f"  accounts.csv （扁平表）\n"
        f"  keys.txt     （{len(keys)} 把 sk- key）\n"
        f"  SUMMARY.md   （汇总）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
