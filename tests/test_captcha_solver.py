"""点选 solver 的离线回归：合成语料上的命中率 + 数值等价。

⚠ 这些用例依赖 **可选依赖**（numpy / scipy / Pillow）。CI 只装运行依赖
  （见 `.github/workflows/ci.yml`），所以它们在 CI 里会 **skip** ——
  本地的 `python -m pytest` 才会真正跑。这是项目「CI 零额外依赖」的代价，
  不是遗漏：装了 `.[captcha]` 的人应该跑得到。

语料是**合成**的（`_captcha_synth.py`）：真实题目截图不适合进公开仓库。
同一 seed 像素级确定，所以「优化前后逐题对拍」可复现。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("PIL")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _captcha_synth import make  # noqa: E402

from src.captcha_solver import _NccEngine, solve_arrays  # noqa: E402

# 这几张在 2026-10-19 的语料上是**稳定命中**的（误差 < 25px）。
# 它们钉的是“P1–P6 的改动没有把本来能解的题弄丢”，不是“solver 万能”。
SEEDS = (2, 5, 12)


@pytest.mark.parametrize("seed", SEEDS)
def test_finds_planted_icons(seed):
    bg, sprite, gt = make(seed)
    points, meta = solve_arrays(bg, sprite, n=3)

    assert len(points) == 3
    # 匈牙利分配的契约：每个图标落到**不同**候选
    assert len({d["candidate"] for d in meta["details"]}) == 3
    for p, g in zip(points, gt, strict=True):
        assert math.dist(p, g) < 25, f"seed={seed} point={p} gt={g}"


def test_is_deterministic():
    """同一输入两次求解必须给出同一坐标（缓存/并行不能引入抖动）。"""
    bg, sprite, _gt = make(2)
    a, _ = solve_arrays(bg, sprite, n=3)
    b, _ = solve_arrays(bg, sprite, n=3)
    assert a == b


def test_corr_valid_matches_fftconvolve():
    """P3 的自写相关必须与 `fftconvolve(..., 'valid')` 数值等价。

    对齐错一个像素就会让所有峰值漂移；这里用两个奇偶不同的模板形状覆盖。
    """
    from scipy.signal import fftconvolve

    rng = np.random.default_rng(0)
    cand = rng.random((90, 110)) > 0.9
    engine = _NccEngine(cand)
    for shape in ((20, 30), (37, 25)):
        tf = (rng.random(shape) > 0.7).astype(np.float32)
        got = engine.corr_valid(tf)
        ref = fftconvolve(cand.astype(np.float64), tf.astype(np.float64)[::-1, ::-1], mode="valid")
        assert got.shape == ref.shape
        # float32 FFT 的绝对误差在 1e-4 量级；1e-2 是宽松但有效的护栏。
        assert float(np.abs(got - ref).max()) < 1e-2
