#!/usr/bin/env python
"""在**合成语料**上量 captcha solver 的命中率与单题耗时（离线、零网络）。

为什么需要它
============
`src/captcha_solver.py` 是纯计算，但它的成本是 **NCC 次数 × FFT 尺寸**：
3 图标 × 候选数（最多 30）× 10 scale × 18 angle × 3 次 `fftconvolve`。
改一行网格/缓存就可能把单题从 15s 变成 3s —— 也可能把本来能解的题弄丢。
所以任何 solver 改动都要跑这个基准：**命中率不能降，耗时应该降**。

⚠ 语料是**合成**的（`tests/_captcha_synth.py`），不是真实题目截图：
  真实图带站点内容与账户上下文，不适合进公开仓库。合成图能钉住
  “算法有没有退化”，但**绝对耗时**会比真实图低（真实图候选更多）。

用法
====
    python tools/ops/bench_captcha_solver.py            # 16 张
    python tools/ops/bench_captcha_solver.py --seeds 40  # 更稳的命中率估计
    python tools/ops/bench_captcha_solver.py --seeds 16 --verbose  # 逐张明细

对比两次改动：在同一个 Python 环境里分别跑，记下 `solved` 与 `total`。
"""

from __future__ import annotations

import argparse
import math
import sys
import time

import _path  # noqa: F401  # 副作用：仓库根 + UTF-8 控制台
from _bootstrap import ROOT  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from _captcha_synth import make  # noqa: E402  # pyright: ignore[reportMissingImports]

from src.captcha_solver import solve_arrays  # noqa: E402

# 命中判定：三个点都落在 truth 的 25px 内（与 tests/test_captcha_solver.py 一致）
HIT_PX = 25.0


def main() -> int:
    ap = argparse.ArgumentParser(description="captcha solver 合成语料基准")
    ap.add_argument("--seeds", type=int, default=16, help="语料张数（默认 16）")
    ap.add_argument("--verbose", action="store_true", help="逐张打印")
    args = ap.parse_args()

    hits = 0
    total = 0.0
    for seed in range(args.seeds):
        bg, sprite, gt = make(seed)
        t0 = time.perf_counter()
        try:
            points, _meta = solve_arrays(bg, sprite, n=3)
            err = max(math.dist(p, g) for p, g in zip(points, gt, strict=True))
            hit = err < HIT_PX
        except Exception as ex:  # noqa: BLE001
            points, err, hit = None, float("inf"), False
            if args.verbose:
                print(f"  seed={seed:02d} ✗ {type(ex).__name__}: {ex}")
        dt = time.perf_counter() - t0
        total += dt
        hits += hit
        if args.verbose:
            state = "✓" if hit else "✗"
            print(f"  seed={seed:02d} {state} {dt:5.2f}s err={err:6.1f}px points={points}")

    n = args.seeds
    print(f"\nsolved {hits}/{n}（{hits / n:.0%}）  total {total:.1f}s  avg {total / n:.2f}s/张")
    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
