"""合成 captcha 语料（测试夹具，不是用例）。

为什么不用真实题目截图：真实图带站点内容与账户上下文，不适合进公开仓库。
这里用 **PIL 画线稿图标 + 合成照片感背景**，并保留 ground truth 坐标，
于是求解精度与速度都可以离线量化。

生成的多张图由 `seed` 完全确定（同一 seed 每次像素级一致），
所以「优化前后逐题对拍」是可复现的。
"""

from __future__ import annotations

import math
import random

import numpy as np
from PIL import Image, ImageDraw

W, H = 672, 480
SPRITE_W, SPRITE_H = 720, 240


def _icon(kind: int, size: int = 90) -> Image.Image:
    """画一个线稿图标（黑笔画、白底、单通道 L）。"""
    img = Image.new("L", (size, size), 255)
    d = ImageDraw.Draw(img)
    m = size * 0.14
    lw = max(2, size // 22)
    if kind == 0:  # 星
        pts = []
        for k in range(10):
            r = size / 2 - m if k % 2 == 0 else size / 4
            a = -math.pi / 2 + k * math.pi / 5
            pts.append((size / 2 + r * math.cos(a), size / 2 + r * math.sin(a)))
        d.line(pts + [pts[0]], fill=0, width=lw, joint="curve")
    elif kind == 1:  # 日
        d.rectangle([m, m, size - m, size - m], outline=0, width=lw)
        d.line([m, size / 2, size - m, size / 2], fill=0, width=lw)
    elif kind == 2:  # 三角 + 内点
        d.line(
            [(size / 2, m), (size - m, size - m), (m, size - m), (size / 2, m)], fill=0, width=lw
        )
        d.ellipse([size * 0.42, size * 0.62, size * 0.58, size * 0.78], outline=0, width=lw)
    elif kind == 3:  # 箭头
        d.line([(m, size / 2), (size - m, size / 2)], fill=0, width=lw)
        d.line([(size * 0.62, m), (size - m, size / 2), (size * 0.62, size - m)], fill=0, width=lw)
    elif kind == 4:  # 圆 + 十字（线与圆相连，保证单个连通域）
        d.ellipse([m, m, size - m, size - m], outline=0, width=lw)
        d.line([size / 2, m, size / 2, size - m], fill=0, width=lw)
        d.line([m, size / 2, size - m, size / 2], fill=0, width=lw)
    else:  # 波浪
        pts = [
            (m + i * (size - 2 * m) / 8, size / 2 + (size / 5) * math.sin(i * 1.2))
            for i in range(9)
        ]
        d.line(pts, fill=0, width=lw, joint="curve")
    return img


def _sprite(kinds: list[int]) -> Image.Image:
    img = Image.new("L", (SPRITE_W, SPRITE_H), 255)
    for i, k in enumerate(kinds):
        img.paste(_icon(k), (30 + i * 220, 75))
    return img


def _place(bg: Image.Image, kind: int, rng: random.Random, placed: list) -> tuple[float, float]:
    """把一个缩放/旋转后的图标画进背景，返回中心坐标。"""
    for _ in range(400):
        s = rng.uniform(0.75, 1.7)
        icon = _icon(kind).resize((int(90 * s), int(90 * s)), Image.Resampling.BILINEAR)
        # 🔴 `fillcolor=255` 不能省：rotate(expand=True) 默认用 0（黑）填角，
        #    会把每个图标变成黑方块 —— 背景候选全被污染，语料就废了。
        icon = icon.rotate(
            rng.uniform(-180, 180), expand=True, resample=Image.Resampling.BICUBIC, fillcolor=255
        )
        iw, ih = icon.size
        if iw + 20 >= W or ih + 20 >= H:
            continue
        cx = rng.uniform(iw / 2 + 10, W - iw / 2 - 10)
        cy = rng.uniform(ih / 2 + 10, H - ih / 2 - 10)
        # 动态间距：两个图标的半径之和 + 50，避免叠成一个候选
        if all(
            (cx - px) ** 2 + (cy - py) ** 2 > (max(iw, ih) / 2 + max(pw, ph) / 2 + 50) ** 2
            for px, py, pw, ph in placed
        ):
            placed.append((cx, cy, iw, ih))
            break
    else:
        raise RuntimeError("放不下")

    ox, oy = int(cx - icon.width / 2), int(cy - icon.height / 2)
    region = bg.crop((ox, oy, ox + icon.width, oy + icon.height)).convert("L")
    mask = np.asarray(icon) < 128
    ra = np.asarray(region).copy()
    ra[mask] = 0
    bg.paste(Image.fromarray(ra, "L").convert("RGB"), (ox, oy))
    return float(cx), float(cy)


def make(seed: int, *, kinds=None, distractors: int = 2):
    """返回 (bg_rgb, sprite_rgb, ground_truth_points)；同一 seed 完全确定。"""
    rng = random.Random(seed)
    nprng = np.random.default_rng(seed * 7919 + 13)

    pool = [0, 1, 2, 3, 4, 5]
    rng.shuffle(pool)
    if kinds is None:
        # 固定几组「形状彼此可区分」的目标，保证语料有可解样本
        kinds = [(0, 1, 5), (1, 2, 5), (0, 3, 5), (1, 3, 4)][seed % 4]
    dist_kinds = [k for k in pool if k not in kinds][:distractors]

    # 背景：浅色照片纹理 + 细小黑点
    base = 205 + (nprng.random((H, W)) * 45).astype(np.int16)
    bg = Image.fromarray(base.astype(np.uint8), "L").convert("RGB")
    d = ImageDraw.Draw(bg)
    for _ in range(250):  # 小噪点（面积远小于 400）
        x, y = rng.randrange(W), rng.randrange(H)
        r = rng.choice((1, 1, 2, 2, 3))
        v = rng.randrange(0, 60)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(v, v, v))

    placed: list[tuple[float, float, float, float]] = []
    gt: list[tuple[float, float]] = []
    for k in kinds:
        gt.append(_place(bg, k, rng, placed))
    for k in dist_kinds:
        _place(bg, k, rng, placed)

    sprite = _sprite(list(kinds)).convert("RGB")
    return np.asarray(bg), np.asarray(sprite), gt
