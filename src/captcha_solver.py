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
from typing import Any, cast

from .agnes import AgnesCaptcha

try:
    import numpy as np  # pyright: ignore[reportMissingImports]
    from PIL import Image  # pyright: ignore[reportMissingImports]
    from scipy import ndimage as ndi  # pyright: ignore[reportMissingImports]
    from scipy.fft import irfft2, next_fast_len, rfft2  # pyright: ignore[reportMissingImports]
    from scipy.optimize import linear_sum_assignment  # pyright: ignore[reportMissingImports]
except ImportError as ex:  # pragma: no cover - 只在没装 optional extra 时走到
    raise AgnesCaptcha(
        f"captcha solver 需要可选依赖（{ex.name}）；"
        f"装 `pip install -e .[captcha]` 或设 AR_CAPTCHA_ENABLED=0"
    ) from ex


# 粗搜 / 细搜网格。🔴 live 实测：背景图标可能比提示图**小到 0.4×**，也可能被
# **旋转 180°** —— 两个范围都必须覆盖。
COARSE_SCALES = [round(x, 2) for x in np.arange(0.3, 2.3, 0.2)]
# 🔴 P5 实测结论：把粗搜角度从 20° 改成 30°（18→12 个）在 16 张合成语料上
#    命中率 **10/16 → 9/16**，24° 更差（8/16）⇒ **保持 20°**。
#    收益换不来命中率，就不该拿。
COARSE_ANGLES = list(range(-180, 180, 20))
FINE_SCALE_RADIUS = 0.15
FINE_SCALE_STEP = 0.05
FINE_ANGLE_RADIUS = 10
FINE_ANGLE_STEP = 5
TOP_K = 4
# P4：>= 这个尺度的图标在 **0.5× 背景**上粗搜（FFT 像素少 4×）。
# 🔴 阈值是**实测**调的：0.6 起步会把命中率从 10/16 拉到 7/16，1.2 会丢一例，
#    1.6 才做到“命中率不降 + 约 1.4× 提速”。半分辨率对中小图标太伤笔画。
PYRAMID_MIN_SCALE = 1.6
# P1：NCC 的数值范围很小（分子分母都是像素计数的组合），float32 足够。
#     🔴 这是实测选择：float32 + 积分图 + 按形状缓存把 NCC 段提速 3.4~4.8×；
#     float64 更“逐位一致”，但会吃掉其中约 1.6×。边界上一两像素的翻峰
#     由语料对拍（1e-4 分差）兜住。
NCC_DTYPE = np.float32
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


def _integral(a: Any) -> Any:
    """二维前缀和（padding 1）。"""
    return np.pad(a, ((1, 0), (1, 0))).cumsum(0).cumsum(1)


def _box_sum(ii: Any, h: int, w: int) -> Any:
    """积分图上的 box sum（valid 区域）。"""
    H = ii.shape[0] - 1
    W = ii.shape[1] - 1
    oh, ow = H - h + 1, W - w + 1
    return ii[h : h + oh, w : w + ow] - ii[0:oh, w : w + ow] - ii[h : h + oh, 0:ow] + ii[0:oh, 0:ow]


class _NccEngine:
    """一个候选 crop 上的 NCC 计算器 —— 与模板形状无关的量只算一次。

    🔴 原实现把 `cf*cf` / `ones` / `fftconvolve(cf, ones)` 放在 angle 循环里，
      每个 (scale, angle) 都重算一遍：候选 crop 的 FFT 被重复了 ~180 次。
      这里改成：
        · `cf` / `cf²` / 积分图：构造时一次；
        · `sc` / `scc`（只依赖旋转后模板的**形状**）：积分图 box sum，按形状缓存；
        · 候选频谱 `rfft2(cf)`：按 FFT 尺寸缓存，等价 `fftconvolve(..., 'valid')`。
    """

    def __init__(self, cand: Any) -> None:
        self.cf = cand.astype(NCC_DTYPE)
        self.cf2 = self.cf * self.cf
        self.H: int = int(self.cf.shape[0])
        self.W: int = int(self.cf.shape[1])
        self.ii = _integral(self.cf)
        self.ii2 = _integral(self.cf2)
        self._boxes: dict[tuple[int, int], tuple[Any, Any]] = {}
        self._hats: dict[tuple[int, int], Any] = {}

    def boxes(self, h: int, w: int) -> tuple[Any, Any]:
        got = self._boxes.get((h, w))
        if got is None:
            got = (_box_sum(self.ii, h, w), _box_sum(self.ii2, h, w))
            self._boxes[(h, w)] = got
        return got

    def corr_valid(self, tf: Any) -> Any:
        """`fftconvolve(cf, tf[::-1,::-1], mode='valid')` 的等价实现。"""
        th: int = int(tf.shape[0])
        tw: int = int(tf.shape[1])
        shape = (
            cast(int, next_fast_len(self.H + th - 1)),
            cast(int, next_fast_len(self.W + tw - 1)),
        )
        hat = cast(Any, self._hats.get(shape))
        if hat is None:
            hat = cast(Any, rfft2(self.cf, s=shape))
            self._hats[shape] = hat
        pad = np.zeros(shape, dtype=NCC_DTYPE)
        pad[:th, :tw] = tf[::-1, ::-1]
        full = cast(Any, irfft2(cast(Any, hat * cast(Any, rfft2(pad))), s=shape))
        return full[th - 1 : self.H, tw - 1 : self.W]


def _iter_ncc(engine: _NccEngine, tpl: Any, scales, angles, *, min_score: float = 0.0):
    """按 scale/angle 网格产出 `(ncc, scale, angle, tpl_mask)`。

    `min_score` 只用于**可证明无效**的剪枝：NCC 对给定 `(st, sc)` 有一个
    解析上界 `u(st, sc)`，若 `u < min_score` 则这一枪不可能产生合格峰。
    ⚠ 上界不是“模板比窗口密就为负”（那个推导漏了 `n`）：
      `num <= n*min(st,sc) - st*sc`，在 `st > sc` 时是 `sc(n-st)`，
      只要模板没有填满整个窗口（`st < n`）就**仍然为正**。
    """
    H, W = engine.H, engine.W
    for s in scales:
        tw = max(1, int(round(tpl.shape[1] * s)))
        th = max(1, int(round(tpl.shape[0] * s)))
        # 🔴 防止**极小模板**刷分：6×10 的模板能匹配任何暗块，NCC 还是高的。
        if tw < 12 or th < 12 or tw * th < 200:
            continue
        if tw > W or th > H:
            continue
        timg = Image.fromarray((tpl * 255).astype(np.uint8)).resize(
            (tw, th), Image.Resampling.NEAREST
        )
        for a in angles:
            timg2 = timg.rotate(a, expand=True, resample=Image.Resampling.NEAREST) if a else timg
            tt = np.asarray(timg2) > 127
            if tt.shape[0] > H or tt.shape[1] > W:
                continue
            st = int(tt.sum())
            if st < 10:
                continue
            sc, scc = engine.boxes(*tt.shape)
            n = tt.size
            if min_score > 0.0:
                # 解析上界：对固定 st、sc，`stc <= min(st, sc)`；上界在 sc 离 st
                # 最远时最大，所以只需看 sc 的两端。
                sc_lo = float(sc.min())
                sc_hi = float(sc.max())
                if sc_hi > 0 and (st <= sc_lo or st >= sc_hi):
                    sc_ref = sc_lo if st <= sc_lo else sc_hi
                    if 0 < st < n and 0 < sc_ref < n:
                        num = st * (n - sc_ref) if st <= sc_ref else sc_ref * (n - st)
                        den = sc_ref * (n - st) if st <= sc_ref else st * (n - sc_ref)
                        if num / den < min_score * min_score:
                            continue
            tf = tt.astype(NCC_DTYPE)
            stt = float(st)
            stc = engine.corr_valid(tf)
            # 🔴 FFT 用 float32，但**归一化公式用 float64**：
            #    `n*scc - sc*sc` 在“整个窗口几乎全黑”时会接近 0，
            #    float32 的舍入会把 den 抹成 0 ⇒ 整张图冒出 -2.0 的洞。
            #    这只是小幅数组运算，代价远小于 FFT。
            stc64 = stc.astype(np.float64)
            sc64 = sc.astype(np.float64)
            scc64 = scc.astype(np.float64)
            num = n * stc64 - st * sc64
            den = np.sqrt(np.maximum((n * stt - st * st) * (n * scc64 - sc64 * sc64), 0.0))
            with np.errstate(invalid="ignore", divide="ignore"):
                ncc = np.where(den > 1e-9, num / den, -2.0).astype(NCC_DTYPE)
            yield ncc, s, a, tt


def _select_peaks(peaks, k: int, nms_px: int):
    """按分数取 top-K，并保证尺度多样 + NMS 去重。"""
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


def _ncc_best(tpl: Any, cand: Any, scales, angles):
    """在 cand 上滑窗匹配 tpl，返回 (score, scale, angle, (dy,dx), tpl_shape)。"""
    best = (-2.0, None, None, None, None)
    engine = _NccEngine(cand)
    for ncc, s, a, tt in _iter_ncc(engine, tpl, scales, angles):
        idx = np.unravel_index(int(np.argmax(ncc)), ncc.shape)
        score = float(ncc[idx])
        if score > best[0]:
            best = (score, s, a, idx, tt.shape)
    return best


def _ncc_peaks_engine(
    engine: _NccEngine,
    tpl: Any,
    scales,
    angles,
    *,
    k: int = 3,
    min_score: float = 0.25,
    nms_px: int = 20,
):
    """在**已有的**候选引擎上取 top-K 峰。

    单独抽出来是为了让同一候选的 3 个图标共用候选频谱缓存。
    """
    peaks = []
    for ncc, s, a, tt in _iter_ncc(engine, tpl, scales, angles, min_score=min_score):
        mx = ndi.maximum_filter(ncc, size=15)
        ys, xs = np.nonzero((ncc == mx) & (ncc > min_score))
        for y, x in zip(ys, xs, strict=True):
            peaks.append((float(ncc[y, x]), s, a, (int(x), int(y)), tt.shape))
    return _select_peaks(peaks, k, nms_px)


def _ncc_peaks(
    tpl: Any, cand: Any, scales, angles, *, k: int = 3, min_score: float = 0.25, nms_px: int = 20
):
    """返回 cand 上 tpl 的 top-K 局部峰值（跨 scale/angle）。

    🔴 不能只取全局最优：正确匹配可能是**第二个峰** —— live 实测里 0.3× 的
       小块假匹配分数更高，只保留全局最优会让细搜永远看不到正确位置。
    """
    return _ncc_peaks_engine(
        _NccEngine(cand), tpl, scales, angles, k=k, min_score=min_score, nms_px=nms_px
    )


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


def _downscale_mask(mask: Any, factor: int = 2) -> Any:
    """2×2 max-pool（`any`）降采样：细笔画只要有一格暗就保留。"""
    h = (mask.shape[0] // factor) * factor
    w = (mask.shape[1] // factor) * factor
    return mask[:h, :w].reshape(h // factor, factor, w // factor, factor).any(axis=(1, 3))


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
    # P4：大尺度在 0.5× 背景上搜（FFT 像素少 4×），小尺度留在全分辨率
    #     （半分辨率会把 12px 下限吃掉；0.3~0.5× 正是小图标假匹配的来源）。
    small_scales = [s for s in COARSE_SCALES if s < PYRAMID_MIN_SCALE]
    big_scales = [s for s in COARSE_SCALES if s >= PYRAMID_MIN_SCALE]
    half_mask = _downscale_mask(bg_mask) if big_scales else None
    # 🔴 每个候选只建一份裁剪/引擎：3 个图标共用候选频谱缓存
    #    （同尺寸的模板形状会命中同一份 `rfft2(cf)`）。
    crops = [_crop(bg_mask, c, pad=pad) for c in cands]
    engines = [_NccEngine(c[0]) for c in crops]
    hcrops: list[tuple[Any, int, int]] = []
    hengines: list[_NccEngine] = []
    if half_mask is not None:
        for cand in cands:
            hcand = {
                "x": cand["x"] // 2,
                "y": cand["y"] // 2,
                "w": max(1, cand["w"] // 2),
                "h": max(1, cand["h"] // 2),
                "cx": cand["cx"] / 2.0,
                "cy": cand["cy"] / 2.0,
            }
            hcrops.append(_crop(half_mask, hcand, pad=pad // 2))
        hengines = [_NccEngine(h[0]) for h in hcrops]
    coarse = np.full((len(icons), len(cands)), -2.0)
    coarse_peaks = {}
    for i, (_sx, _sy, tpl) in enumerate(icons):
        for j, _cand in enumerate(cands):
            crop, ox, oy = crops[j]
            peaks = _ncc_peaks_engine(engines[j], tpl, small_scales, COARSE_ANGLES, k=3)
            if half_mask is not None:
                hcrop, _hx, _hy = hcrops[j]
                # 半分辨率 → 全分辨率 crop 坐标（±1px 误差由细搜 ±50px 吸收）
                for p in _ncc_peaks_engine(
                    hengines[j], tpl, [s / 2 for s in big_scales], COARSE_ANGLES, k=3
                ):
                    peaks.append(
                        (
                            p[0],
                            p[1] * 2,
                            p[2],
                            (p[3][0] * 2, p[3][1] * 2),
                            (p[4][0] * 2, p[4][1] * 2),
                        )
                    )
                peaks = _select_peaks(peaks, 3, 20)
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
    # P2：形状指纹只在 `SHAPE_PRIOR_WEIGHT != 0` 时才算。默认为 0，
    #     而 `_shape_distance` 一次要 ~1.6ms（骨架细化 + 洞/矩），
    #     细搜里会跑上百次 —— 算了再乘 0 是纯浪费。
    feats = [_shape_features(icon[2]) if SHAPE_PRIOR_WEIGHT else None for icon in icons]
    for i in range(len(icons)):
        # 🔴 top-K 取**两个排名的并集**：只看 coarse_adj 会漏掉“原始分最高”
        #    的正确候选（17:00 样本的城堡就是这样丢的）；只看原始分则会被小尺度
        #    假峰占满名额（17:03 样本的“日”）。两者都要。
        order = sorted(
            set(np.argsort(-coarse[i])[:top_k].tolist())
            | set(np.argsort(-coarse_adj[i])[:top_k].tolist())
        )
        tpl = icons[i][2]
        tpl_feat = feats[i]
        for j in order:
            j = int(j)
            if coarse[i, j] <= -1.5:
                continue
            cand = cands[j]
            crop, ox, oy = crops[j]
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
                sd = (
                    _shape_distance(tpl_feat, win)
                    if (SHAPE_PRIOR_WEIGHT and tpl_feat is not None)
                    else 0.0
                )
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
        crop, _ox, _oy = crops[j]
        win = crop[idx[1] : idx[1] + shape[0], idx[0] : idx[0] + shape[1]]
        sd = (
            _shape_distance(feats[i], win)
            if (SHAPE_PRIOR_WEIGHT and feats[i] is not None and win.size)
            else 0.0
        )
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
