"""TCaptcha 点选求解器：提示图（N 个图标）→ 背景图里的点击坐标。

纯 numpy + scipy + Pillow（**可选依赖**，见 `pyproject.toml` 的 `captcha` extra），
不依赖 cv2 / 深度学习。

算法与 `docs/protocol.md` §2② 的 live 实测一致：

1. 纯黑掩码（`max(RGB) < thr`）分离线稿图标 —— 照片的暗部不是纯黑；
2. 清理提示图噪点，按**竖向空隙**切 N 个图标；
3. 背景膨胀 → 连通域 → 候选（含大复合图标，如眼镜 + 脸 + "2"）；
4. 每个（图标, 候选）做**多尺度 + 旋转 NCC**（scale 0.3~2.2、angle ±180°）；
5. 匈牙利分配（每个图标 → 不同候选）；
6. 点击点 = 最佳匹配的模板中心。

🔴 依赖缺失时抛 `AgnesCaptcha`（而不是 `ModuleNotFoundError`），让调用方能给出
   "装 `pip install -e .[captcha]` 或设 `AR_CAPTCHA_ENABLED=0`" 这种可执行的提示。
"""

from __future__ import annotations

import io
import math
from typing import Any

from .agnes import AgnesCaptcha

try:
    import numpy as np  # pyright: ignore[reportMissingImports]
    from PIL import Image  # pyright: ignore[reportMissingImports]
    from scipy import ndimage as ndi  # pyright: ignore[reportMissingImports]
    from scipy.optimize import linear_sum_assignment  # pyright: ignore[reportMissingImports]
    from scipy.signal import fftconvolve  # pyright: ignore[reportMissingImports]
except ImportError as ex:  # pragma: no cover - 只在没装 optional extra 时走到
    raise AgnesCaptcha(
        f"captcha solver 需要可选依赖（{ex.name}）；"
        f"装 `pip install -e .[captcha]` 或设 AR_CAPTCHA_ENABLED=0"
    ) from ex


# 粗搜 / 细搜网格。🔴 live 实测：背景图标可能比提示图**小到 0.4×**，也可能被
# **旋转 180°** —— 两个范围都必须覆盖。
COARSE_SCALES = [round(x, 2) for x in np.arange(0.3, 2.3, 0.2)]
COARSE_ANGLES = list(range(-180, 180, 20))
FINE_SCALE_RADIUS = 0.15
FINE_SCALE_STEP = 0.05
FINE_ANGLE_RADIUS = 10
FINE_ANGLE_STEP = 5
TOP_K = 4
# 尺度先验的权重：|log(模板尺寸 / 候选尺寸)| 的惩罚系数。
# 🔴 0.25 是 3 张样本（HAR + 2 张 live 失败样本）上的最优值：
#    0.15/0.2 不够（17:00 样本的 0.9× 假匹配会赢），0.3 会把正确的大图标
#    （17:00 的城堡）也压死。再大就过度拟合了。
SIZE_PRIOR_WEIGHT = 0.25
# 形状先验的权重（Hu 矩 + 端点/拐点 + 洞 + 长宽比 + 填充率）。
# 🔴 **默认关闭（0.0）** —— 2026-10-17 在 4 张真实样本（HAR + 3 张 live 失败样本）
#    上实测：`0.0` → 10/12、`0.2` → 10/12（平手）、`0.35` → 8/12（**变差**）。
#    原因：正确匹配的窗口里夹着照片纹理/遮挡物，形状指纹也会“脏”，
#    而错配的小尺度窗口反而可能很干净 ⇒ 这个信号在这些样本上不可靠。
#    ⚠ 代码留着（`_shape_features` / `_shape_distance`），因为它的**前提**没错；
#      要重新评估需要更多样本（至少几十张），而不是 4 张。
#    想试就把这里改成 0.2 并跑一批统计命中率 —— **别只看单张**。
SHAPE_PRIOR_WEIGHT = 0.0


def load_rgb_bytes(data: bytes) -> Any:
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).astype(np.int16)


def dark_mask(rgb: Any, thr: int) -> Any:
    """纯黑线稿：三个通道都暗。`max(RGB)` 比灰度更抗照片暗部。"""
    return rgb.max(axis=2) < thr


def clean_mask(mask: Any, min_area: int = 60) -> Any:
    """去掉小噪点，只留面积 >= min_area 的连通域（8 邻接）。"""
    lab, n = ndi.label(mask, structure=np.ones((3, 3), dtype=int))  # pyright: ignore[reportGeneralTypeIssues]
    out = np.zeros_like(mask, dtype=bool)
    for i in range(1, n + 1):
        comp = lab == i
        if comp.sum() >= min_area:
            out |= comp
    return out


def _zero_runs(cols: Any):
    runs, start = [], None
    for i, c in enumerate(cols):
        if c == 0 and start is None:
            start = i
        elif c != 0 and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(cols) - 1))
    return runs


def _dominant_bbox(seg: Any):
    """切片内**主导连通域**的 bbox（相对切片坐标）。

    🔴 不能直接用 `np.nonzero(seg)` 的极值：提示图上散落的孤立暗点会把
       bbox 拉得很宽，模板里于是半张是空白 —— NCC 分数就永远上不去
       （实测 17:03 样本的“日”就是这样丢的：32×45 的模板里只有 ~12px 是笔画）。
    """
    lab, cnt = ndi.label(seg, structure=np.ones((3, 3), dtype=int))  # pyright: ignore[reportGeneralTypeIssues]
    if cnt == 0:
        return None
    areas = ndi.sum(seg, lab, range(1, cnt + 1))
    largest = float(areas.max()) if len(areas) else 0.0
    keep = np.zeros_like(seg, dtype=bool)
    for idx, area in enumerate(areas, 1):
        if area >= max(30.0, 0.15 * largest):
            keep |= lab == idx
    ys, xs = np.nonzero(keep)
    if len(xs) == 0:
        return None
    return xs.min(), xs.max(), ys.min(), ys.max()


def split_sprite(mask: Any, n: int = 3):
    """把提示图按竖向空隙切成 n 个图标，返回 [(x, y, icon_mask), ...]。"""
    cols = mask.sum(axis=0)
    gaps = _zero_runs(cols)
    interior = [g for g in gaps if g[0] > 0 and g[1] < len(cols) - 1]
    interior.sort(key=lambda g: -(g[1] - g[0]))
    cuts = sorted((g[0] + g[1]) // 2 for g in interior[: n - 1])
    if len(cuts) < n - 1:  # 兜底：没有足够空隙就等分
        cuts = [mask.shape[1] * i // n for i in range(1, n)]
    bounds = [0] + cuts + [mask.shape[1]]
    icons = []
    for i in range(n):
        seg = mask[:, bounds[i] : bounds[i + 1]]
        box = _dominant_bbox(seg)
        if box is None:
            continue
        x0, x1, y0, y1 = box
        icons.append((bounds[i] + x0, y0, seg[y0 : y1 + 1, x0 : x1 + 1]))
    return icons


def find_candidates(
    mask: Any,
    *,
    min_area: int = 400,
    min_wh: int = 30,
    max_wh: int = 340,
    max_candidates: int = 30,
):
    """膨胀后找连通域，返回背景图里的候选图标（含大复合图标）。"""
    dil = ndi.binary_dilation(mask, iterations=2)
    lab, _ = ndi.label(dil)  # pyright: ignore[reportGeneralTypeIssues]
    cands = []
    for i, sl in enumerate(ndi.find_objects(lab), 1):
        if sl is None:
            continue
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        area = int((lab[sl] == i).sum())
        if area < min_area or not (min_wh <= w <= max_wh and min_wh <= h <= max_wh):
            continue
        x0, y0 = sl[1].start, sl[0].start
        sub = mask[sl]
        ys, xs = np.nonzero(sub)
        if len(xs) == 0:
            continue
        cands.append(
            {
                "x": x0,
                "y": y0,
                "w": w,
                "h": h,
                "area": area,
                "cx": float(x0 + xs.mean()),
                "cy": float(y0 + ys.mean()),
            }
        )
    cands.sort(key=lambda c: -c["area"])
    return cands[:max_candidates]


def _ncc_best(tpl: Any, cand: Any, scales, angles):
    """在 cand 上滑窗匹配 tpl，返回 (score, scale, angle, (dy,dx), tpl_shape)。"""
    best = (-2.0, None, None, None, None)
    cf = cand.astype(np.float64)
    for s in scales:
        tw = max(1, int(round(tpl.shape[1] * s)))
        th = max(1, int(round(tpl.shape[0] * s)))
        # 🔴 防止**极小模板**刷分：6×10 的模板能匹配任何暗块，NCC 还是高的。
        if tw < 12 or th < 12 or tw * th < 200:
            continue
        if tw > cand.shape[1] or th > cand.shape[0]:
            continue
        timg = Image.fromarray((tpl * 255).astype(np.uint8)).resize(
            (tw, th), Image.Resampling.NEAREST
        )
        for a in angles:
            timg2 = timg.rotate(a, expand=True, resample=Image.Resampling.NEAREST) if a else timg
            tt = np.asarray(timg2) > 127
            if tt.sum() < 10 or tt.shape[0] > cand.shape[0] or tt.shape[1] > cand.shape[1]:
                continue
            tf = tt.astype(np.float64)
            n = tf.size
            st = tf.sum()
            stt = (tf * tf).sum()
            ones = np.ones_like(tf)
            sc = fftconvolve(cf, ones, mode="valid")
            scc = fftconvolve(cf * cf, ones, mode="valid")
            stc = fftconvolve(cf, tf[::-1, ::-1], mode="valid")
            num = n * stc - st * sc
            den = np.sqrt(np.maximum((n * stt - st * st) * (n * scc - sc * sc), 0.0))
            with np.errstate(invalid="ignore", divide="ignore"):
                ncc = np.where(den > 1e-9, num / den, -2.0)
            idx = np.unravel_index(int(np.argmax(ncc)), ncc.shape)
            score = float(ncc[idx])
            if score > best[0]:
                best = (score, s, a, idx, tt.shape)
    return best


def _ncc_peaks(
    tpl: Any, cand: Any, scales, angles, *, k: int = 3, min_score: float = 0.25, nms_px: int = 20
):
    """返回 cand 上 tpl 的 top-K 局部峰值（跨 scale/angle）。

    🔴 不能只取全局最优：正确匹配可能是**第二个峰** —— live 实测里 0.3× 的
       小块假匹配分数更高，只保留全局最优会让细搜永远看不到正确位置。
    """
    cf = cand.astype(np.float64)
    peaks = []
    for s in scales:
        tw = max(1, int(round(tpl.shape[1] * s)))
        th = max(1, int(round(tpl.shape[0] * s)))
        if tw < 12 or th < 12 or tw * th < 200:
            continue
        if tw > cand.shape[1] or th > cand.shape[0]:
            continue
        timg = Image.fromarray((tpl * 255).astype(np.uint8)).resize(
            (tw, th), Image.Resampling.NEAREST
        )
        for a in angles:
            timg2 = timg.rotate(a, expand=True, resample=Image.Resampling.NEAREST) if a else timg
            tt = np.asarray(timg2) > 127
            if tt.sum() < 10 or tt.shape[0] > cand.shape[0] or tt.shape[1] > cand.shape[1]:
                continue
            tf = tt.astype(np.float64)
            n = tf.size
            st = tf.sum()
            stt = (tf * tf).sum()
            ones = np.ones_like(tf)
            sc = fftconvolve(cf, ones, mode="valid")
            scc = fftconvolve(cf * cf, ones, mode="valid")
            stc = fftconvolve(cf, tf[::-1, ::-1], mode="valid")
            num = n * stc - st * sc
            den = np.sqrt(np.maximum((n * stt - st * st) * (n * scc - sc * sc), 0.0))
            with np.errstate(invalid="ignore", divide="ignore"):
                ncc = np.where(den > 1e-9, num / den, -2.0)
            mx = ndi.maximum_filter(ncc, size=15)
            ys, xs = np.nonzero((ncc == mx) & (ncc > min_score))
            for y, x in zip(ys, xs, strict=True):
                peaks.append((float(ncc[y, x]), s, a, (int(x), int(y)), tt.shape))
    peaks.sort(key=lambda p: -p[0])

    def _dist2(p, o) -> float:
        return (p[3][0] - o[3][0]) ** 2 + (p[3][1] - o[3][1]) ** 2

    def _bucket(s: float) -> int:
        # 尺度分桶：0.3~0.6 / 0.6~1.0 / 1.0~1.5 / 1.5~2.2
        if s < 0.6:
            return 0
        if s < 1.0:
            return 1
        if s < 1.5:
            return 2
        return 3

    # 🔴 **尺度多样**：先每个桶取一个最高分。只按分数取 top-K 时，小尺度
    #    假匹配（0.3~0.5×）会把所有名额占满，正确的大尺度峰根本进不了细搜
    #    （实测 17:03 样本的 "日" 就是这样被 0.5× 假匹配抢走的）。
    out = []
    seen_buckets = set()
    for p in peaks:
        b = _bucket(p[1])
        if b in seen_buckets:
            continue
        if all(_dist2(p, o) > nms_px**2 for o in out):
            out.append(p)
            seen_buckets.add(b)
        if len(out) >= k:
            break
    # 还有名额就按分数补（NMS 去重）
    for p in peaks:
        if len(out) >= k:
            break
        if p in out:
            continue
        if all(_dist2(p, o) > nms_px**2 for o in out):
            out.append(p)
    out.sort(key=lambda p: -p[0])
    return out


def _crop(bg_mask: Any, cand: dict, pad: int):
    x0 = max(0, cand["x"] - pad)
    y0 = max(0, cand["y"] - pad)
    x1 = min(bg_mask.shape[1], cand["x"] + cand["w"] + pad)
    y1 = min(bg_mask.shape[0], cand["y"] + cand["h"] + pad)
    return bg_mask[y0:y1, x0:x1], x0, y0


def _fine_grid(s_coarse: float, a_coarse: float):
    """细搜网格：scale 在原尺度附近，angle 要**绕回** ±180。"""
    scales = [
        round(s_coarse + d, 2)
        for d in np.arange(-FINE_SCALE_RADIUS, FINE_SCALE_RADIUS + 1e-9, FINE_SCALE_STEP)
    ]
    angles = [
        ((int(a_coarse) + d + 180) % 360) - 180
        for d in range(-FINE_ANGLE_RADIUS, FINE_ANGLE_RADIUS + 1, FINE_ANGLE_STEP)
    ]
    return [s for s in scales if 0.2 <= s <= 4.0], angles


def _thin(mask: Any, max_iter: int = 30) -> Any:
    """Zhang-Suen 细化（numpy 向量化）。返回骨架。

    自己写而不是引 `skimage.morphology.skeletonize`：本项目不想为一个函数
    多一个重依赖（`skimage` 比 `scipy` 还大，且只在这里用）。
    模板很小（≤90×90），30 轮足够。
    """
    img = mask.astype(np.uint8).copy()
    for _ in range(max_iter):
        changed = False
        for step in (0, 1):
            p = np.pad(img, 1)
            p2, p3 = p[:-2, 1:-1], p[:-2, 2:]
            p4, p5 = p[1:-1, 2:], p[2:, 2:]
            p6, p7 = p[2:, 1:-1], p[2:, :-2]
            p8, p9 = p[1:-1, :-2], p[:-2, :-2]
            nb = p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9
            seq = [p2, p3, p4, p5, p6, p7, p8, p9, p2]
            trans = sum(((seq[k] == 0) & (seq[k + 1] == 1)).astype(np.uint8) for k in range(8))
            if step == 0:
                cond = (
                    (p2 * p4 * p6 == 0)
                    & (p4 * p6 * p8 == 0)
                    & (img > 0)
                    & (nb >= 2)
                    & (nb <= 6)
                    & (trans == 1)
                )
            else:
                cond = (
                    (p2 * p4 * p8 == 0)
                    & (p2 * p6 * p8 == 0)
                    & (img > 0)
                    & (nb >= 2)
                    & (nb <= 6)
                    & (trans == 1)
                )
            if cond.any():
                img[cond] = 0
                changed = True
        if not changed:
            break
    return img > 0


def _shape_features(mask: Any) -> dict:
    """形状指纹：Hu 矩 + 端点/拐点数 + 洞数 + 长宽比 + 填充率。

    为什么要它（实测）：NCC 只比较**重合度**，而错配的典型形态是
    “细长字形 ↔ 密集复合图标”（「1」配到「图表+笑脸」），两者轮廓交叠度也能
    到 0.7。真正把它们分开的是**拓扑与统计形状**：
      · 「1」：2 个端点、0 个洞、长宽比大、填充率高
      · 「日」：0 个端点、2 个洞
      · 复合图标：端点/拐点很多、洞数偏多
    而 **Hu 矩对缩放与旋转不变** —— 正好对应我们正在搜的 scale/angle 自由度。
    """
    m = np.asarray(mask, dtype=bool)
    h, w = m.shape
    area = float(m.sum())
    out = {
        "aspect": float(w) / float(max(h, 1)),
        "fill": area / float(max(w * h, 1)),
        "holes": 0,
        "endpoints": 0,
        "junctions": 0,
        "hu": [0.0] * 7,
    }
    if area < 20:
        return out
    # 洞：背景连通域中不接触边界的那些
    bg_lab, n = ndi.label(~m, structure=np.ones((3, 3), dtype=int))  # pyright: ignore[reportGeneralTypeIssues]
    border = set(bg_lab[0, :].tolist()) | set(bg_lab[-1, :].tolist())
    border |= set(bg_lab[:, 0].tolist()) | set(bg_lab[:, -1].tolist())
    out["holes"] = sum(1 for i in range(1, n + 1) if i not in border)
    # 端点 / 拐点（骨架邻居数）
    sk = _thin(m)
    k = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], dtype=np.uint8)
    nb = ndi.convolve(sk.astype(np.uint8), k, mode="constant")
    out["endpoints"] = int(((nb == 1) & sk).sum())
    out["junctions"] = int(((nb >= 3) & sk).sum())
    # Hu 矩：标准归一化中心矩 eta = mu_pq / mu00^(1+(p+q)/2)，再算 7 个不变量。
    # 🔴 漏掉那个除数会让 Hu 随**面积**变化（窗口大小不同 → 分值不可比），
    #    实测把正确匹配也一起罚下去（10/12 → 6/12）。
    ys, xs = np.nonzero(m)
    yy = ys.astype(np.float64) - ys.mean()
    xx = xs.astype(np.float64) - xs.mean()
    mu = {}
    for pp in range(4):
        for qq in range(4 - pp):
            mu[(pp, qq)] = float((xx**pp * yy**qq).sum())
    mu00 = max(mu[(0, 0)], 1.0)

    def eta(pp: int, qq: int) -> float:
        return mu[(pp, qq)] / mu00 ** (1.0 + (pp + qq) / 2.0)

    e20, e02, e11 = eta(2, 0), eta(0, 2), eta(1, 1)
    e30, e12, e21, e03 = eta(3, 0), eta(1, 2), eta(2, 1), eta(0, 3)
    h1 = e20 + e02
    h2 = (e20 - e02) ** 2 + 4 * e11**2
    h3 = (e30 - 3 * e12) ** 2 + (3 * e21 - e03) ** 2
    h4 = (e30 + e12) ** 2 + (e21 + e03) ** 2
    h5 = (e30 - 3 * e12) * (e30 + e12) * ((e30 + e12) ** 2 - 3 * (e21 + e03) ** 2) + (
        3 * e21 - e03
    ) * (e21 + e03) * (3 * (e30 + e12) ** 2 - (e21 + e03) ** 2)
    h6 = (e20 - e02) * ((e30 + e12) ** 2 - (e21 + e03) ** 2) + 4 * e11 * (e30 + e12) * (e21 + e03)
    h7 = (3 * e21 - e03) * (e30 + e12) * ((e30 + e12) ** 2 - 3 * (e21 + e03) ** 2) - (
        e30 - 3 * e12
    ) * (e21 + e03) * (3 * (e30 + e12) ** 2 - (e21 + e03) ** 2)
    out["hu"] = [math.log10(abs(v) + 1e-12) for v in (h1, h2, h3, h4, h5, h6, h7)]
    return out


def _shape_distance(tpl_feat: dict, win: Any) -> float:
    """形状差异（0 = 完全一致）。用于给 NCC 分数加惩罚。"""
    win_feat = _shape_features(win)
    if win_feat["fill"] < 0.02:
        return 3.0  # 窗口几乎是空的 ⇒ 不可能是同一个图标
    # Hu 矩：log 尺度上的平均绝对差（对缩放/旋转不敏感）
    hu = sum(abs(a - b) for a, b in zip(tpl_feat["hu"], win_feat["hu"], strict=True)) / 7.0
    d_aspect = abs(math.log(max(win_feat["aspect"], 1e-3) / max(tpl_feat["aspect"], 1e-3)))
    d_fill = abs(win_feat["fill"] - tpl_feat["fill"])
    # 端点/拐点：用相对差（复合图标的拐点通常 ≥ 字形的一倍以上）
    d_ep = abs(win_feat["endpoints"] - tpl_feat["endpoints"]) / max(
        tpl_feat["endpoints"] + win_feat["endpoints"], 2
    )
    d_jn = abs(win_feat["junctions"] - tpl_feat["junctions"]) / max(
        tpl_feat["junctions"] + win_feat["junctions"], 2
    )
    d_holes = abs(win_feat["holes"] - tpl_feat["holes"]) / 3.0
    return 0.35 * hu + 0.2 * d_aspect + 0.15 * d_fill + 0.1 * d_ep + 0.15 * d_jn + 0.05 * d_holes


def _adjust(score: float, shape, cand: dict) -> float:
    """尺度先验：模板尺寸与候选 blob 尺寸越接近越好。

    🔴 小尺度假匹配（0.3× 的窗口/小块）常常原始分更高；用
       |log(模板尺寸 / 候选尺寸)| 惩罚“小模板套大 blob”。
    """
    ratio = 0.5 * (shape[1] / max(cand["w"], 1) + shape[0] / max(cand["h"], 1))
    return score - SIZE_PRIOR_WEIGHT * abs(math.log(max(ratio, 1e-3)))


def _snap_to_blob(cx: float, cy: float, cand: dict, bg_mask: Any):
    """把点击点约束到**候选 blob 内**。返回 ((x, y), 说明)。

    🔴 为什么必需：NCC 的最佳窗口中心可能落在候选 bbox 内但**没压到笔画**上
       —— 典型是“细长字形”配到“密集复合图标”上（实测：“1” 被配到图表图标，
       窗口中心偏 centroid 26px，服务端直接 `errorCode=50`）。
       这时改用该候选的 centroid（它定义上就是 blob 的重心）比把错位坐标
       发出去更可能被接受。

    判据分两层（任一不满足就回退）：
      1. 点必须落在候选自己的 bbox 内（不能漂到邻居身上）；
      2. 点必须压在暗像素上（`bg_mask[y, x]`）。
    """

    def _in_bbox(x: float, y: float) -> bool:
        return cand["x"] <= x <= cand["x"] + cand["w"] and cand["y"] <= y <= cand["y"] + cand["h"]

    def _is_dark(x: float, y: float) -> bool:
        xi, yi = int(round(x)), int(round(y))
        if not (0 <= yi < bg_mask.shape[0] and 0 <= xi < bg_mask.shape[1]):
            return False
        return bool(bg_mask[yi, xi])

    if _in_bbox(cx, cy) and _is_dark(cx, cy):
        return (round(float(cx), 1), round(float(cy), 1)), ""

    fx, fy = cand["cx"], cand["cy"]
    if not _in_bbox(fx, fy):
        fx = min(max(fx, cand["x"]), cand["x"] + cand["w"])
        fy = min(max(fy, cand["y"]), cand["y"] + cand["h"])
    if not _in_bbox(cx, cy):
        reason = "centroid(outside-bbox)"
    else:
        reason = "centroid(off-stroke)"
    return (round(float(fx), 1), round(float(fy), 1)), reason


def solve_arrays(
    bg_rgb: Any,
    sprite_rgb: Any,
    *,
    n: int = 3,
    sprite_thr: int = 60,
    bg_thr: int = 30,
    pad: int = 25,
    top_k: int = TOP_K,
):
    """返回 (points, meta)。points 按提示图顺序，坐标在背景图坐标系。"""
    sprite_mask = clean_mask(dark_mask(sprite_rgb, sprite_thr), min_area=60)
    icons = split_sprite(sprite_mask, n=n)
    if len(icons) < n:
        raise AgnesCaptcha(f"提示图只切出 {len(icons)} 个图标，期望 {n} 个")

    bg_mask = dark_mask(bg_rgb, bg_thr)
    cands = find_candidates(bg_mask)
    if len(cands) < n:
        raise AgnesCaptcha(f"背景图只找到 {len(cands)} 个候选，少于 {n} 个目标")

    # ── 粗搜：每个（图标, 候选）保留 top-K 峰 ──────────────────
    coarse = np.full((len(icons), len(cands)), -2.0)
    coarse_peaks = {}
    for i, (_sx, _sy, tpl) in enumerate(icons):
        for j, cand in enumerate(cands):
            crop, ox, oy = _crop(bg_mask, cand, pad=pad)
            peaks = _ncc_peaks(tpl, crop, COARSE_SCALES, COARSE_ANGLES, k=3)
            coarse_peaks[(i, j)] = [(p, ox, oy) for p in peaks]
            coarse[i, j] = peaks[0][0] if peaks else -2.0

    # ── 每个候选的“调整后”粗分：用于挑 top-K 细搜 ──────────────
    # 🔴 不能按**原始**粗分挑 top-K：小尺度假峰会占满名额，正确的大尺度
    #    候选根本进不了细搜（17:03 样本的“日”就是这样丢的）。
    coarse_adj = np.full((len(icons), len(cands)), -2.0)
    for (i, j), peaks in coarse_peaks.items():
        if not peaks:
            continue
        cand = cands[j]
        coarse_adj[i, j] = max(_adjust(p[0], p[4], cand) for p, _ox, _oy in peaks)

    # ── 细搜 + 逐峰尺度先验 ────────────────────────────────────
    # 🔴 候选的分数要取**调整后**的最高分，而不是先取原始最高分再调整：
    #    17:03 样本的“日”就是反例 —— 0.5× 假峰原始分 0.540、大尺度正确峰
    #    0.428；先取原始最高会把正确峰丢掉，逐峰调整后正确峰反而更高。
    fine_raw = np.full((len(icons), len(cands)), -2.0)
    fine_adj = np.full((len(icons), len(cands)), -2.0)
    fine_info = {}
    for i in range(len(icons)):
        # 🔴 top-K 取**两个排名的并集**：只看 coarse_adj 会漏掉“原始分最高”
        #    的正确候选（17:00 样本的城堡就是这样丢的）；只看原始分则会被小尺度
        #    假峰占满名额（17:03 样本的“日”）。两者都要。
        order = sorted(
            set(np.argsort(-coarse[i])[:top_k].tolist())
            | set(np.argsort(-coarse_adj[i])[:top_k].tolist())
        )
        tpl = icons[i][2]
        tpl_feat = _shape_features(tpl)
        for j in order:
            j = int(j)
            if coarse[i, j] <= -1.5:
                continue
            cand = cands[j]
            crop, ox, oy = _crop(bg_mask, cand, pad=pad)
            for p, _ox, _oy in coarse_peaks[(i, j)]:
                _cscore, s_c, a_c, idx_c, shape_c = p
                cx = idx_c[0] + shape_c[1] / 2.0
                cy = idx_c[1] + shape_c[0] / 2.0
                margin = 50
                fx0 = max(0, int(cx - margin))
                fy0 = max(0, int(cy - margin))
                fx1 = min(crop.shape[1], int(cx + margin))
                fy1 = min(crop.shape[0], int(cy + margin))
                sub = crop[fy0:fy1, fx0:fx1]
                if sub.size == 0:
                    continue
                scales, angles = _fine_grid(s_c, a_c)
                score, s, a, idx, shape = _ncc_best(tpl, sub, scales, angles)
                if idx is None or shape is None:
                    continue
                fine_raw[i, j] = max(fine_raw[i, j], score)
                win = sub[idx[1] : idx[1] + shape[0], idx[0] : idx[0] + shape[1]]
                sd = _shape_distance(tpl_feat, win)
                adj = _adjust(score, shape, cand) - SHAPE_PRIOR_WEIGHT * sd
                if adj > fine_adj[i, j]:
                    fine_adj[i, j] = adj
                    fine_info[(i, j)] = (
                        ox + fx0 + idx[0] + shape[1] / 2.0,
                        oy + fy0 + idx[1] + shape[0] / 2.0,
                        s,
                        a,
                        shape,
                        round(sd, 3),
                    )

    # 没进细搜的 pair 用粗搜 top-1 兜底
    for (i, j), peaks in coarse_peaks.items():
        if (i, j) in fine_info or not peaks:
            continue
        (score, s, a, idx, shape), ox, oy = peaks[0]
        cand = cands[j]
        tpl_feat = _shape_features(icons[i][2])
        crop, _ox, _oy = _crop(bg_mask, cand, pad=pad)
        win = crop[idx[1] : idx[1] + shape[0], idx[0] : idx[0] + shape[1]]
        sd = _shape_distance(tpl_feat, win) if win.size else 3.0
        fine_raw[i, j] = max(fine_raw[i, j], score)
        adj = _adjust(score, shape, cand) - SHAPE_PRIOR_WEIGHT * sd
        if adj > fine_adj[i, j]:
            fine_adj[i, j] = adj
            fine_info[(i, j)] = (
                ox + idx[0] + shape[1] / 2.0,
                oy + idx[1] + shape[0] / 2.0,
                s,
                a,
                shape,
                round(sd, 3),
            )

    # ── 匈牙利分配（每个图标一个不同候选）──────────────────────
    rows, cols = linear_sum_assignment(-fine_adj)
    points, details = [], []
    for i, j in zip(rows, cols, strict=True):
        cand = cands[int(j)]
        cx, cy, s, a, _shape, sd = fine_info[(i, int(j))]
        click, snapped = _snap_to_blob(cx, cy, cand, bg_mask)
        points.append(click)
        details.append(
            {
                "icon": int(i),
                "candidate": int(j),
                "score": round(float(fine_raw[i, j]), 3),
                "adjusted": round(float(fine_adj[i, j]), 3),
                "scale": float(s),
                "angle": int(a),
                "click": click,
                "match_center": (round(float(cx), 1), round(float(cy), 1)),
                "shape_dist": sd,
                "snapped": snapped,
                "candidate_centroid": (
                    round(cand["cx"], 1),
                    round(cand["cy"], 1),
                ),
            }
        )
    return points, {"details": details, "candidates": cands}


def solve_bytes(bg_bytes: bytes, sprite_bytes: bytes, *, n: int = 3):
    """从两张图片的原始 bytes 求解。返回 (points, meta)。"""
    return solve_arrays(load_rgb_bytes(bg_bytes), load_rgb_bytes(sprite_bytes), n=n)
