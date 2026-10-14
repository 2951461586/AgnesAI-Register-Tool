"""CLI 入口。

用法：
  python run.py                          # 跑 1 个账号
  python run.py --count 6                # 跑 6 个（默认 4 路生产者 × 4 路消费者）
  python run.py --count 6 --workers 1 --producers 1    # 强制顺序执行（最保守）
  python run.py --count 6 --workers 8    # 收尾并发 8（纯 HTTP，很快）
  python run.py --out accounts.json      # 显式导出到指定文件（不落台账快照）
  python run.py --ignore-quota           # 跳过本地配额闸门

关于 --out（台账落盘）：
  **默认不填 = 写进台账目录** `ledger/`：
      ledger/runs/<日期>/accounts-<时间戳>.json   ← 合并后的**全量**快照（= 读源）
      ledger/latest.json                          ← **本批结果**（含失败 / 跳过）
  显式 `--out X` 则是"导出到别处"：只写 X、**不落快照**。

  ⚠ 台账的读源是**最新的那份全量快照**，**不是** `latest.json`。后者只含本批
    那几十条，拿它当读源 ⇒ 合并基准只剩上一批 ⇒ 台账停止累积、每次跑批覆盖
    上一次（Intern-Register-Tool 栽过两次）。详见 `src/ledger.py`。

  ⚠ 台账在本项目是账号的**唯一副本**：邮箱是临时邮箱（收不到找回信），
    密码与 `sk-` key 只存在于台账里。所以它整个目录都在 `.gitignore` 中，
    且**不要**把它当临时文件清掉 —— 定期把 `ledger/` 另存一份到别处。

关于 --workers / --producers：
  **本项目零浏览器**（整条链路纯 HTTP，无验证码 / 无 WAF 解盾），
  所以并发只受两件事约束：邮箱 Worker（建地址 + 收信轮询）和
  **Agnes 按 IP 的注册尝试限流**（`/api/verification` + `/api/user/register`）。

      --producers  同时在"建邮箱 → 发验证码 → 等码"的账号数。
                   **等码是整链最大单项**（1~3s），所以这个数直接决定吞吐。
      --workers    同时在"注册 → 登录 → 建 key → 校验"的账号数。每账号 ~1~3s。

  🔴 真正的闸门是**按出口的滑动窗口**（`AR_REGISTER_MAX_PER_WINDOW` /
    `AR_REGISTER_WINDOW_S`）。**只调大并发不会提高吞吐** —— 同一张窗口里的
    名额是固定的；提高吞吐的唯一办法是配 `AR_PROXY_SLOTS`（多出口，
    每个出口一张窗口）。见 `docs/protocol.md`「限流与出口」。

退出码：
  0 = 至少有一个账号成功（或没有要跑的）
  1 = 本批**全部失败**（配置 / 网络 / 服务端拒绝）
  2 = 本地配额闸门拦下（**一个请求都没发**）
  3 = 台账护栏触发（读源损坏 / 防静默缩水，拒绝写盘，避免丢账号）
  4 = 配置有问题（缺凭据 / 值不合法，如窗口上限装不下一个账号）
  5 = 内部错误（记录序列化失败）
  6 = 链路预检不通过（`--preflight`；**未发出任何注册请求**）
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config  # noqa: E402
from src import cli as _cli  # noqa: E402
from src import ledger as _ledger  # noqa: E402
from src import preflight as _preflight  # noqa: E402
from src import quota as _quota  # noqa: E402
from src import redact  # noqa: E402
from src import report as _report  # noqa: E402
from src.pipeline import STAGES, error_kind_of, run_batch  # noqa: E402
from src.proxypool import SlotPool  # noqa: E402

# 🔴 必须在**任何 print 之前**：Windows 控制台默认 GBK，而下面到处是 emoji，
#    不重配会在启动提示行就 UnicodeEncodeError —— 一个请求都发不出去，
#    而且 exit 码还停在 0，看起来像“配置有问题”（实测踩过）。
_cli.setup_console()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Agnes AI 端到端注册（CF Worker 域名邮箱 + 验证码 + 建 API Key）"
    )
    ap.add_argument("--count", type=int, default=1, help="注册账号数量")
    ap.add_argument(
        "--producers",
        type=int,
        default=4,
        help="生产者并发：同时在「建邮箱→发验证码→等码」的账号数（默认 4）",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=4,
        help="消费者并发：同时在「注册→登录→建 key→校验」的账号数（默认 4）",
    )
    ap.add_argument("--mail-domain", default=None, help="邮箱域名（默认取 AR_WORKER_DOMAIN）")
    ap.add_argument("--key-name", default=None, help=f"API Key 名称（默认 {config.KEY_NAME}）")
    ap.add_argument(
        "--out",
        default=None,
        help="结果输出文件。**不填（默认）= 写进台账目录**："
        "ledger/runs/<日期>/accounts-<时间戳>.json（合并后的全量快照，也是读源）"
        "+ ledger/latest.json（本批结果）。显式给路径则只写那一个文件、不落快照。",
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="只写本次结果、不合并历史（默认按 email 合并，防止一次小规模探测覆盖掉整个账号台账）",
    )
    ap.add_argument("--quiet", action="store_true", help="只输出汇总（不逐账号打进度）")
    ap.add_argument(
        "--ignore-quota",
        action="store_true",
        help="跳过本地配额保护（仅当确信服务端没在拦时用）",
    )
    ap.add_argument(
        "--preflight",
        action="store_true",
        help="跑批前先体检出口链路（只读请求，不消耗注册窗口）。"
        "失败率过高就**中止**（退出码 6）—— 避免把注册窗口白烧在链路坏时段上。",
    )
    args = ap.parse_args(argv)

    if args.key_name:
        config.KEY_NAME = args.key_name

    # ── 启动校验：缺凭据就立刻失败，别等跑了一半才发现全是 401 ──────
    missing = config.validate()
    if missing:
        print(f"✗ 配置有问题：{'、'.join(missing)}", file=sys.stderr)
        # ⚠ 这条修法**只对"凭据缺失"成立**。值不对的情况（如窗口上限比一个账号
        #   需要的格数还小）后面自带括号说明，再塞一句"cp .env.example"会误导
        #   —— 用户会去改 .env 的凭据，而真正要改的是那个数值。
        #   判据：条目里**没有括号说明**的，就是"缺了某个凭据变量"。
        if any("（" not in m for m in missing):
            print(
                "  修法：cp .env.example .env 并填入真实值（.env 已在 .gitignore 中，不会进仓库）",
                file=sys.stderr,
            )
        return 4

    # 🔴 非致命警告（`config.lint()`）—— **不退出**。
    #    这里放的是"能跑，但你在浪费容量 / 大概率误解了单位"：窗口上限按
    #    `REQUESTS_PER_ACCOUNT` 量化，奇数会**静默**丢掉最后一格
    #    （实测：`MAX=3` 看着像 3 个账号，实际只有 1 个）。
    for warning in config.lint():
        print(f"  ⚠ 配置提醒：{warning}", flush=True)

    # ── 台账读源：**在任何请求之前**读好 ──────────────────────
    # 🔴 为什么放在这里（跑批之前），而不是留到落盘阶段：读源损坏时落盘会失败，
    #    而那时**整批请求已经花掉了** —— 服务端的注册窗口是本工具最贵的资源。
    #    `load_read_source` 损坏就抛，所以这个失败在启动阶段就能拦住。
    # ⚠ 代价：读源比"跑完再读"略旧一点。单进程单批的用法下无所谓；
    #    真要同时跑两批，本来就该用不同的 `--out`（两批写同一份台账必然互相覆盖）。
    out = Path(args.out) if args.out else None
    existing: list[dict] = []
    if not args.overwrite:
        try:
            existing = _ledger.load_read_source(out or _ledger.ledger_path())
        except _ledger.LedgerCorrupt as ex:
            print(f"✗ {ex}", file=sys.stderr)
            return 3

    # ── 本地配额闸门（**在发任何请求之前**）────────────────────────
    st = _quota.status()
    print(f"本地配额：{st.describe()}  [state: {_quota.state_path()}]", flush=True)
    hint = _quota.shortfall_hint(st.left, args.count, args.ignore_quota)
    if hint:
        print(hint, flush=True)
    if st.exhausted and not args.ignore_quota:
        print(
            "  ⚠ 窗口内计数已达上限（这是**保守估计**，不是服务端拒绝）。\n"
            "    若确信服务端没在拦，可加 --ignore-quota 或调大 AR_REG_QUOTA_MAX。",
            flush=True,
        )
        print(
            "✗ 本地配额闸门拦下了这一批：**未发出任何请求**。"
            "（这是本地保守保护，不是服务端拒绝 —— 详见 src/quota.py）",
            file=sys.stderr,
        )
        return 2

    # ── 代理槽位池 ──────────────────────────────────────────
    # 先把它建出来，好让"池子里有什么"在**跑第一个请求之前**就可见。
    # 🔴 这里**不做 TCP 预检**：Intern-Register-Tool 实测过，本机开着 Clash TUN 时
    #    "连一个不可达端口只要 0.02s"—— 那是连到了本地虚拟网卡，
    #    TCP 能连并不等于代理可用。真要看出口，用 tools/probes/probe_slots.py。
    slots = config.proxy_slots()
    slot_pool = SlotPool(slots, timeout=config.PROXY_SLOT_TIMEOUT)
    if slots:
        print(
            f"🔀 槽位池：{len(slots)} 个出口（{config.AR_PROXY_SLOTS_FILE or 'AR_PROXY_SLOTS'}）",
            flush=True,
        )
        for i, url in enumerate(slots, 1):
            # ⚠ 槽位串可能是 `http://user:pass@host:port`，原样打印会把账密写进
            #   终端 / 重定向的日志。过一遍脱敏。
            print(f"     slot{i} {redact.redact_url(url)}", flush=True)
        print(
            f"   注册尝试限流实测是**按出口**的 ⇒ 理论上限 ≈ "
            f"{config.REGISTER_MAX_PER_WINDOW * len(slots)} 次 / "
            f"{config.REGISTER_WINDOW_S:g}s（本地保守值，非服务端阈值）",
            flush=True,
        )
    else:
        print(
            "出口：未配置槽位池"
            + (f"，单代理 {redact.redact_url(config.AR_PROXY)}" if config.AR_PROXY else "（直连）")
            + "\n     ⚠ 没有池子时，注册尝试限流会**按你这一个 IP** 生效；"
            "批量会被窗口卡住。",
            flush=True,
        )

    # ── 链路健康预检（`--preflight`）──────────────────────────────
    # 🔴 为什么值得单独一步：注册窗口是本工具**最贵且不可再生**的资源
    #    （撞上就是 1 小时）。本会话**手工**体检过 3 次，其中一次正好避开
    #    全池 58% 失败的坏时段 —— 那一次省下 4 个域名窗口。
    # ⚠ 预检本身只打**只读**请求（无鉴权 ⇒ 401），**不消耗注册窗口**。
    # ⚠ 判据不是"失败率高低"，而是"**多数出口一起坏**吗" —— 个别坏槽位不该
    #    拦住整批（撞到它换出口就行了）。见 `src/preflight.py` 的 docstring。
    if args.preflight:
        pf = _preflight.run(slots or _preflight.enabled_slots())
        print(pf.describe(), flush=True)
        why = pf.verdict()
        if why:
            print(
                f"✗ 链路预检不通过：{why}\n"
                f"  已中止，**未发出任何注册请求**。\n"
                f"  选项：① 换时间点再跑；② 去掉 --preflight 强行跑（会白烧窗口）。",
                file=sys.stderr,
            )
            return 6

    t0 = time.time()
    results = run_batch(
        count=args.count,
        workers=args.workers,
        producers=args.producers,
        mail_domain=args.mail_domain,
        verbose=not args.quiet,
        ignore_quota=args.ignore_quota,
        slot_pool=slot_pool,
    )
    wall = time.time() - t0

    # ── 落盘 ──────────────────────────────────────────────────────
    # ⚠ 这里的 `json.loads` 只可能在**我们自己的** `AccountRecord.to_json()`
    #    出问题时抛 —— 那是内部不变量被破坏。**绝不静默丢记录**（少数据但指标
    #    全"正常"是最坏的失败），但要把肇事者说清楚。
    try:
        new_records = [json.loads(r.to_json()) for r in results]
    except json.JSONDecodeError as ex:
        print(f"✗ 内部错误：AccountRecord.to_json() 产出了非法 JSON：{ex}", file=sys.stderr)
        return 5

    if args.overwrite:
        merged = new_records
        print(f"\n（--overwrite：只写本次 {len(merged)} 条，不合并历史）")
    else:
        # 🔴 读源已在**启动阶段**读好（见 main 开头）：那里读是为了"别白跑一批"，
        #    这里只负责合并。用的是 `load_read_source`（**损坏就抛**）而不是
        #    `load_existing`（损坏时静默返回 `[]`）—— 后者会写出"只剩本批"的
        #    快照，把历史账号静默丢光（实测 50 条 → 1 条，而退出码还是 0）。
        merged, kept, added, upgraded = _ledger.merge_records(existing, new_records)
        if kept:
            print(_cli.merge_summary_line(kept, added, upgraded, len(merged)))

    try:
        # 🔴 判据是 `out is None`（= 用户没给 `--out`），**不是**
        #    `ledger.is_ledger_path(out)`。两者在"显式 `--out` 指向 ledger/ 内部"
        #    这一种输入下**行为不同**：本入口走纯导出（不落快照，见 --out 的
        #    help 承诺）。这是**有意的**，别"顺手统一"。
        if out is None:
            # `new_records` 是**本批原始**结果（含失败 / 跳过）→ latest.json；
            # `merged` 是合并后的**全量** → 快照（也是新的读源）。
            snap, _last = _ledger.save_snapshot(
                merged, new_records, existing=[] if args.overwrite else None
            )
            written = snap
            print(
                f"\n台账已落盘：\n"
                f"  快照（读源） {_ledger.repo_rel(snap)}\n"
                f"  本批结果     {_ledger.repo_rel(_ledger.last_run_path())}"
                f"（{len(new_records)} 条）"
            )
        else:
            written = _ledger.save(out, merged, existing=[] if args.overwrite else None)
    except ValueError as ex:
        # 防静默缩水护栏（src/ledger）。少数据但指标全"正常"是最坏的失败，
        # 宁可报错退出也不要静默丢掉账号。
        print(f"✗ {ex}", file=sys.stderr)
        return 3

    # 收尾报告整块在 `src/report.py`（纯展示 + 依赖注入），这里只接线。
    qline = _quota.status().describe()
    _report.render_batch_report(
        results,
        written,
        wall,
        stages=STAGES,
        error_kind_of=error_kind_of,
        quota_line=qline,
        workers=args.workers,
        repo_rel=_ledger.repo_rel,
        redact_email=redact.redact_email,
        slot_pool=slot_pool,
        redact_url=redact.redact_url,
    )

    ok = sum(1 for r in results if r.status == "success")
    if results and not ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
