#!/usr/bin/env python
"""从台账导出 `sk-` key 列表（给下游用）。

用法：
    python tools/data/export_keys.py                       # 写 exports/keys.txt + keys.csv
    python tools/data/export_keys.py --only-success        # 只导 status=success 的
    python tools/data/export_keys.py --out my.txt --format txt

🔴 `exports/` 不在 `.gitignore` 里吗？—— **在**（见 `.gitignore` §凭据）。
   但导出的文件本身是明文凭据，**不要**拷进仓库或贴到别处。
"""

import argparse
import csv
import os
import sys
from pathlib import Path

from _path import ROOT  # noqa: F401,E402

from src import fsutil  # noqa: E402
from src import ledger as _ledger


def main(argv=None):
    ap = argparse.ArgumentParser(description="从台账导出 sk- key")
    ap.add_argument("--out", default=None, help="输出文件（默认按 --format 定名）")
    ap.add_argument("--format", choices=("txt", "csv", "json"), default="txt")
    ap.add_argument("--only-success", action="store_true", help="只导 status=success 的账号")
    args = ap.parse_args(argv)

    records = _ledger.load_existing(_ledger.ledger_path())
    if not records:
        print("✗ 台账里没有记录", file=sys.stderr)
        return 1

    rows = []
    for r in records:
        if args.only_success and r.get("status") != "success":
            continue
        if not r.get("api_key"):
            continue
        rows.append(
            {
                "email": r.get("email", ""),
                "api_key": r.get("api_key", ""),
                "key_preview": r.get("key_preview", ""),
                "plan": r.get("plan", ""),
                "balance_minor": r.get("balance_minor", 0),
                "models_count": r.get("models_count", 0),
                "created_at": r.get("created_at", ""),
                "status": r.get("status", ""),
            }
        )

    out = Path(args.out) if args.out else (ROOT / "exports" / f"keys.{args.format}")
    if args.format == "txt":
        fsutil.atomic_write_text(out, "\n".join(r["api_key"] for r in rows) + ("\n" if rows else ""))
    elif args.format == "csv":
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = fsutil.tmp_path_for(out)
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["email", "api_key"])
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, out)
    else:
        fsutil.atomic_write_json(out, rows)

    print(f"导出 {len(rows)} 条 → {_ledger.repo_rel(out)}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
