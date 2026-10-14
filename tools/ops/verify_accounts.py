#!/usr/bin/env python
"""台账账号存活复核（**只读**，不建新 key、不发验证码）。

回答一个问题：台账里那些"注册成功"的账号，**现在**还能不能用？
判据两条（与注册链的 `verify` 一致）：
  ① 控制台：用台账里的密码重新登录 → 拿新 JWT → `user/self` + `balance`；
  ② 网关：用台账里的 `sk-` key 打 `/v1/models`（+ 可选一次 chat）。

为什么值得单独一个工具：注册成功与"现在能用"是两件事 —— 平台可能改策略、
封号、或 key 被删。台账里那行 `status=success` 是**当时**的结论。

用法：
    python tools/ops/verify_accounts.py                 # 复核全部，结果合并回台账
    python tools/ops/verify_accounts.py --limit 5
    python tools/ops/verify_accounts.py --out check.json  # 只写文件，不动台账

⚠ 登录**不**消耗注册窗口（实测），所以这个工具可以随时跑。
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

from _path import ROOT  # noqa: F401,E402

from src import apikey as _apikey  # noqa: E402
from src import ledger as _ledger  # noqa: E402
from src.agnes import AgnesClient, AgnesError  # noqa: E402


def check_one(rec: dict) -> dict:
    out = dict(rec)
    email = str(rec.get("email") or "")
    pwd = str(rec.get("password") or "")
    key = str(rec.get("api_key") or "")
    note = []
    if pwd:
        c = AgnesClient()
        try:
            c.login(email, pwd)
            if c.jwt:
                note.append("login=ok")
                try:
                    me = c.user_self()
                    note.append(f"active={bool(me.get('is_active', True))}")
                    bal = c.balance()
                    out["balance_minor"] = bal.get(
                        "available_total_minor", out.get("balance_minor")
                    )
                except AgnesError as ex:
                    note.append(f"self=fail({ex})")
            else:
                note.append("login=no_token")
        except AgnesError as ex:
            note.append(f"login=fail({ex})")
    else:
        note.append("login=skip(no password)")
    if key:
        try:
            res = _apikey.verify(key, attempts=1)
            note.append(f"key={'ok' if res['ok'] else res['note'][:60]}")
        except Exception as ex:  # noqa: BLE001
            note.append(f"key=exc({type(ex).__name__})")
    else:
        note.append("key=skip")
    stages = dict(rec.get("stages") or {})
    stages["recheck"] = "；".join(note)
    out["stages"] = stages
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="台账账号存活复核")
    ap.add_argument("--limit", type=int, default=0, help="只复核前 N 条（0 = 全部）")
    ap.add_argument("--out", default=None, help="只写这个文件，不合并回台账")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    src = _ledger.ledger_path()
    records = _ledger.load_existing(src)
    if not records:
        print(f"✗ 台账里没有记录（读源 {_ledger.repo_rel(src)}）", file=sys.stderr)
        return 1
    if args.limit:
        records = records[: args.limit]
    print(f"复核 {len(records)} 条（读源 {_ledger.repo_rel(src)}）...")

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        checked = list(ex.map(check_one, records))
    for r in checked:
        print(f"  {r.get('email'):<44} {r['stages'].get('recheck', '')}")

    if args.out:
        _ledger.save(args.out, checked)
        print(f"\n已写出：{_ledger.repo_rel(args.out)}")
        return 0
    merged, kept, added, upgraded = _ledger.merge_records(records, checked)
    snap, last = _ledger.save_snapshot(merged, checked)
    print(
        f"\n台账已更新：原有 {kept} + 新增 {added}（更新 {upgraded}）"
        f"\n  快照 {_ledger.repo_rel(snap)}\n  本批 {_ledger.repo_rel(last)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
