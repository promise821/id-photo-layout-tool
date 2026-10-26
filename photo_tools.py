# -*- coding: utf-8 -*-
"""photo_tools.py —— 单张照片处理：换背景色、限制尺寸与文件大小。

对外接口：
    hex_to_rgb(value)                 -> (r, g, b)
    rgb_to_hex(rgb)                   -> '#RRGGBB'
    make_background(size, color, mode)-> 背景图
    replace_background(image, ...)    -> 换好背景的新图
    fit_jpeg(image, ...)              -> (字节内容, 实际质量, 实际缩放比例)
    export_single(image, path, ...)   -> 结果信息字典

设计取舍：
    * 抠图链路"失败即回退"：GrabCut 不可用或失败就退回漫水填充，
      漫水填充再失败就原样返回，任何一步都不把异常抛给调用方；
    * 漫水填充是纯 numpy 的四邻域扩散，不引入 scipy 依赖；
    * 颜色、背景生成、抠图这些"被 UI 反复调用"的接口一律不抛异常，
      非法输入给出安全默认值；只有导出写文件（export_single）允许报错，
      由调用方捕获后提示用户。

依赖：numpy（必需）、opencv-python（可选，缺失时自动走漫水填充）、Pillow。
"""

from __future__ import annotations

import io
import math
import os
import sys
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image, ImageOps

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 背景渲染模式
BG_KEEP = "keep"                    # 保留原背景
BG_SOLID = "solid"                  # 纯色
BG_GRADIENT_V = "gradient_v"        # 上下渐变（上深下浅）
BG_GRADIENT_CENTER = "gradient_center"   # 中心渐变（中心为纯色，四周加深）
BG_MODES = (BG_KEEP, BG_SOLID, BG_GRADIENT_V, BG_GRADIENT_CENTER)

# 抠图方法
METHOD_AUTO = "auto"      # 自动（神经网络 → GrabCut → 漫水）
METHOD_NEURAL = "neural"  # MODNet 神经网络抠图（首选，失败回退 GrabCut）
METHOD_GRABCUT = "grabcut"
METHOD_FLOOD = "flood"
METHODS = (METHOD_AUTO, METHOD_NEURAL, METHOD_GRABCUT, METHOD_FLOOD)

# 神经网络抠图（MODNet ONNX）：输入边长、模型文件搜索顺序、会话缓存
NEURAL_INPUT_SIZE = 512
NEURAL_MODEL_NAMES = ("hivision_modnet.onnx", "modnet.onnx")
_neural_session_cache: dict = {}

# 渐变参数：上深下浅时顶部亮度系数；中心渐变时边缘最深到原来的 65%
GRADIENT_V_TOP_RATIO = 0.82
GRADIENT_CENTER_EDGE_RATIO = 0.35

# 漫水填充判定阈值：像素与种子颜色的欧氏距离小于阈值才算背景。
# strength 越大阈值越宽容，同时仍限制在 28~36 的经验区间内。
FLOOD_BASE_THRESHOLD = 32.0
FLOOD_MIN_THRESHOLD = 28.0
FLOOD_MAX_THRESHOLD = 36.0

# 渐变过渡带：背景外侧多少层像素参与"按颜色距离算 alpha"的过渡
FLOOD_REF_LAYERS = 32

# 多轮放宽阈值继续扩散的步长：影棚背景常带渐变/阴影，
# 一刀切会在人脸周围留下一圈原色（换底后"红边"的元凶）。
FLOOD_PASS_STEPS = (10.0, 20.0, 32.0)

# 种子筛选的宽松倍数：边界像素与四角背景参考色的距离小于
# 阈值 × 该倍数，才允许作为漫水起点（挡住贴着下边缘的衣服）
FLOOD_SEED_FACTOR = 3.0

# 局部渐变容忍：与相邻像素颜色差异小于 阈值 × 该倍数时也允许继续扩散，
# 这样"越靠近人脸越暗"的阴影渐变能被跨过去；全局漂移仍由阈值 ×
# FLOOD_SEED_FACTOR 兜住，不会一路飘进人物身上。
FLOOD_LOCAL_FACTOR = 0.6

# GrabCut 工作分辨率：长边不超过该像素数时抠图最稳且够快
GRABCUT_MAX_SIDE = 900
# GrabCut 迭代次数
GRABCUT_ITERATIONS = 3

# JPEG 体积压缩用的质量阶梯（从高到低）
QUALITY_LADDER = (88, 80, 72, 64, 55, 45, 35, 25)
# 最低质量仍超标时，最多等比缩放几次
MAX_SCALE_STEPS = 5
# 缩放时留的安全余量
SCALE_SAFETY = 0.95


# ---------------------------------------------------------------------------
# 1. 颜色工具
# ---------------------------------------------------------------------------
def hex_to_rgb(value: str) -> Tuple[int, int, int]:
    """#RRGGBB / RRGGBB / #RGB → (r, g, b)。非法输入返回白色。"""
    try:
        text = str(value or "").strip().lstrip("#").strip()
        if len(text) == 3:          # #RGB 三位展开成六位
            text = "".join(ch * 2 for ch in text)
        if len(text) != 6:
            return (255, 255, 255)
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except Exception:
        # 颜色输入常来自用户手输，任何异常都退化成白色而不是打断界面
        return (255, 255, 255)


def rgb_to_hex(rgb: Tuple[int, int, int]) -> str:
    """(r, g, b) → '#RRGGBB'。"""
    try:
        parts = list(rgb)[:3]
        while len(parts) < 3:
            parts.append(255)
        out = []
        for value in parts:
            num = int(round(float(value)))
            num = max(0, min(255, num))
            out.append("%02X" % num)
        return "#" + "".join(out)
    except Exception:
        return "#FFFFFF"


# ---------------------------------------------------------------------------
# 2. 背景生成
# ---------------------------------------------------------------------------
def _clamp_color(color: Tuple[int, int, int]) -> Tuple[int, int, int]:
    """把颜色分量夹到 0~255，非法输入退化成白色。"""
    try:
        parts = list(color)[:3]
        while len(parts) < 3:
            parts.append(255)
        return tuple(max(0, min(255, int(round(float(v))))) for v in parts)  # type: ignore[return-value]
    except Exception:
        return (255, 255, 255)


def make_background(size: Tuple[int, int], color: Tuple[int, int, int],
                    mode: str = BG_SOLID) -> Image.Image:
    """按模式生成背景图（size = (宽, 高)）。"""
    try:
        width = max(1, int(size[0]))
        height = max(1, int(size[1]))
    except Exception:
        width, height = 1, 1

    base = _clamp_color(color)
    kind = (mode or BG_SOLID).strip().lower()

    if kind == BG_KEEP:
        return Image.new("RGB", (width, height), base)

    if kind == BG_GRADIENT_V:
        # 上深下浅：顶部取 0.82 倍亮度，底部为纯色，逐行线性插值
        top = np.array([v * GRADIENT_V_TOP_RATIO for v in base], dtype=np.float32)
        bottom = np.array([float(v) for v in base], dtype=np.float32)
        step = np.linspace(0.0, 1.0, height, dtype=np.float32).reshape(-1, 1)
        rows = top.reshape(1, 3) * (1.0 - step) + bottom.reshape(1, 3) * step
        arr = np.repeat(rows.reshape(height, 1, 3), width, axis=1)
        return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")

    if kind == BG_GRADIENT_CENTER:
        # 中心渐变：到中心的距离按"半对角线"归一化，越靠边越暗
        ys = np.arange(height, dtype=np.float32).reshape(-1, 1) - (height - 1) / 2.0
        xs = np.arange(width, dtype=np.float32).reshape(1, -1) - (width - 1) / 2.0
        half_diag = math.sqrt((width / 2.0) ** 2 + (height / 2.0) ** 2) or 1.0
        dist = np.sqrt(xs * xs + ys * ys) / half_diag
        # 偶数尺寸时几何中心落在四个像素之间，减去最小距离才能保证
        # "正中心像素就是纯色"（而不是略暗一点）
        dist = np.clip(dist - dist.min(), 0.0, 1.0)
        factor = (1.0 - GRADIENT_CENTER_EDGE_RATIO * dist).reshape(height, width, 1)
        arr = np.array(base, dtype=np.float32).reshape(1, 1, 3) * factor
        return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")

    # 默认纯色（未知模式也走这里，避免界面因脏数据崩掉）
    return Image.new("RGB", (width, height), base)


# ---------------------------------------------------------------------------
# 3. 抠图内部实现
# ---------------------------------------------------------------------------
def _region_frontier(region: np.ndarray):
    """取区域内部与外部相邻的那一圈像素（作为继续扩散的前沿）。"""
    neighbour = np.zeros_like(region)
    neighbour[1:, :] |= region[:-1, :]        # 上邻居
    neighbour[:-1, :] |= region[1:, :]        # 下邻居
    neighbour[:, 1:] |= region[:, :-1]        # 左邻居
    neighbour[:, :-1] |= region[:, 1:]        # 右邻居
    ys, xs = np.nonzero(region & ~neighbour)
    return ys.astype(np.intp), xs.astype(np.intp)


def _corner_reference(arr: np.ndarray) -> Optional[np.ndarray]:
    """取四个角块的像素中位数，作为背景参考色。

    证件照的背景几乎总是出现在四角；用中位数还能抵消角落里的杂物。
    """
    try:
        height, width = arr.shape[:2]
        bh = max(1, int(height * 0.06))
        bw = max(1, int(width * 0.06))
        corners = (arr[:bh, :bw], arr[:bh, width - bw:],
                   arr[height - bh:, :bw], arr[height - bh:, width - bw:])
        samples = np.concatenate([c.reshape(-1, 3) for c in corners], axis=0)
        if samples.size == 0:
            return None
        return np.median(samples, axis=0)
    except Exception:
        return None


def _flood_region(arr: np.ndarray, threshold: float,
                   start_region: Optional[np.ndarray] = None,
                   start_seed: Optional[np.ndarray] = None,
                   seed_mask: Optional[np.ndarray] = None):
    """四邻域漫水：从四条边界像素向内扩散。

    返回 (region, seed)：
        region 布尔数组，True 表示判定为背景；
        seed 是每个背景像素的"种子颜色"，后续按它算颜色距离。
    用前沿（frontier）增量扩散而不是整幅布尔迭代，图片越大越省时间。

    传入 start_region/start_seed 可以**接着上次的结果继续扩散**——
    多轮放宽阈值时用它把阴影边缘也吃掉。
    seed_mask 限定"哪些边界像素可以当种子"：人物的肩膀/衣服常常贴着
    图片下边缘，不加限制就会把它们当成背景种子，衣服被整片吃掉。
    """
    height, width = arr.shape[:2]
    if start_region is not None and start_seed is not None:
        region = start_region.copy()
        seed = start_seed.copy()
        front_y, front_x = _region_frontier(region)
    else:
        region = np.zeros((height, width), dtype=bool)
        seed = np.zeros((height, width, 3), dtype=np.float32)

        border_y = np.concatenate([
            np.zeros(width, dtype=np.intp), np.full(width, height - 1, dtype=np.intp),
            np.arange(height, dtype=np.intp), np.arange(height, dtype=np.intp)])
        border_x = np.concatenate([
            np.arange(width, dtype=np.intp), np.arange(width, dtype=np.intp),
            np.zeros(height, dtype=np.intp), np.full(height, width - 1, dtype=np.intp)])
        if seed_mask is not None:
            keep = seed_mask[border_y, border_x]
            border_y, border_x = border_y[keep], border_x[keep]
        region[border_y, border_x] = True
        seed[border_y, border_x] = arr[border_y, border_x]

        front_y, front_x = border_y, border_x
    directions = ((-1, 0), (1, 0), (0, -1), (0, 1))

    while front_y.size:
        next_y, next_x = [], []
        for dy, dx in directions:
            cand_y = front_y + dy
            cand_x = front_x + dx
            inside = ((cand_y >= 0) & (cand_y < height) &
                      (cand_x >= 0) & (cand_x < width))
            if not inside.any():
                continue
            cand_y, cand_x = cand_y[inside], cand_x[inside]
            src_y, src_x = front_y[inside], front_x[inside]

            free = ~region[cand_y, cand_x]
            if not free.any():
                continue
            cand_y, cand_x = cand_y[free], cand_x[free]
            src_y, src_x = src_y[free], src_x[free]

            # 与相邻已判定背景像素的种子颜色比对：差异小才算同一片背景。
            # 只跟种子色比，跨不过"越靠近人脸越暗"的阴影渐变，所以再加一条：
            # 与**相邻像素**颜色接近、且离种子色还不算太远，也算同一片背景
            # （局部渐变容忍 + 全局漂移上限，避免一路飘进人物身上）。
            dist = np.sqrt(((arr[cand_y, cand_x] - seed[src_y, src_x]) ** 2).sum(axis=1))
            local = np.sqrt(((arr[cand_y, cand_x] - arr[src_y, src_x]) ** 2).sum(axis=1))
            ok = ((dist < threshold)
                  | ((local < threshold * FLOOD_LOCAL_FACTOR)
                     & (dist < threshold * FLOOD_SEED_FACTOR)))
            if not ok.any():
                continue
            cand_y, cand_x = cand_y[ok], cand_x[ok]
            src_y, src_x = src_y[ok], src_x[ok]

            # 同一个候选可能被多个前沿像素触及，去重保留第一个
            flat = cand_y * width + cand_x
            _, first = np.unique(flat, return_index=True)
            first = np.sort(first)
            cand_y, cand_x = cand_y[first], cand_x[first]
            src_y, src_x = src_y[first], src_x[first]

            region[cand_y, cand_x] = True
            seed[cand_y, cand_x] = seed[src_y, src_x]
            next_y.append(cand_y)
            next_x.append(cand_x)

        if next_y:
            front_y = np.concatenate(next_y)
            front_x = np.concatenate(next_x)
        else:
            front_y = np.array([], dtype=np.intp)
            front_x = np.array([], dtype=np.intp)

    return region, seed


def _reference_colors(region: np.ndarray, seed: np.ndarray, layers: int):
    """把背景区域的种子颜色向外扩散若干层。

    作用是给背景外侧一圈像素一个"最近的背景色"参考，用来算过渡 alpha，
    否则背景与前景之间会出现硬边。
    """
    height, width = region.shape
    ref = seed.copy()
    have = region.copy()
    # 只从"背景区与外界的交界环"向外扩散：区域内部的像素本来就带着自己的
    # 种子色，参与扩散只会白白增加计算量
    eroded = region.copy()
    eroded[1:, :] &= region[:-1, :]
    eroded[:-1, :] &= region[1:, :]
    eroded[:, 1:] &= region[:, :-1]
    eroded[:, :-1] &= region[:, 1:]
    ys, xs = np.nonzero(region & ~eroded)
    directions = ((-1, 0), (1, 0), (0, -1), (0, 1))

    for _ in range(max(1, int(layers))):
        next_y, next_x = [], []
        for dy, dx in directions:
            cand_y = ys + dy
            cand_x = xs + dx
            inside = ((cand_y >= 0) & (cand_y < height) &
                      (cand_x >= 0) & (cand_x < width))
            if not inside.any():
                continue
            cand_y, cand_x = cand_y[inside], cand_x[inside]
            src_y, src_x = ys[inside], xs[inside]

            free = ~have[cand_y, cand_x]
            if not free.any():
                continue
            cand_y, cand_x = cand_y[free], cand_x[free]
            src_y, src_x = src_y[free], src_x[free]

            ref[cand_y, cand_x] = ref[src_y, src_x]
            have[cand_y, cand_x] = True
            next_y.append(cand_y)
            next_x.append(cand_x)

        if not next_y:
            break
        ys = np.concatenate(next_y)
        xs = np.concatenate(next_x)

    return ref, have


def _flood_alpha(arr: np.ndarray, strength: float) -> Optional[np.ndarray]:
    """纯 numpy 漫水抠图，返回 alpha（1 = 保留原图，0 = 换成背景）。"""
    try:
        height, width = arr.shape[:2]
        if height < 3 or width < 3:
            return None

        thr = float(np.clip(FLOOD_BASE_THRESHOLD + (float(strength) - 0.5) * 8.0,
                            FLOOD_MIN_THRESHOLD, FLOOD_MAX_THRESHOLD))

        # 种子筛选：只让"颜色接近四角背景参考色"的边界像素当种子。
        # 否则贴着图片下边缘的衣服/肩膀会被当成背景，整片衣服被吃掉。
        seed_mask = None
        ref_color = _corner_reference(arr)
        if ref_color is not None:
            dist = np.sqrt(((arr - ref_color[None, None, :]) ** 2).sum(axis=2))
            seed_mask = dist < thr * FLOOD_SEED_FACTOR

        # 多轮放宽阈值继续扩散：影棚背景常有渐变/阴影，一刀切会把
        # 人脸周围的阴影圈留在原色（就是"换底后一圈红边"的元凶）。
        region, seed = _flood_region(arr, thr, seed_mask=seed_mask)
        for extra in FLOOD_PASS_STEPS:
            region, seed = _flood_region(arr, thr + extra, region, seed)
        ref, have = _reference_colors(region, seed, FLOOD_REF_LAYERS)

        # 按颜色距离线性映射到 0~1 alpha：越接近背景色越该被替换
        dist = np.sqrt(((arr - ref) ** 2).sum(axis=2))
        band = max(8.0, thr * 0.5)
        alpha = np.clip((dist - thr) / band, 0.0, 1.0).astype(np.float32)
        alpha[region] = 0.0
        alpha[~have] = 1.0        # 离背景太远、没有参考色的像素一律保留

        keep = float(np.clip(strength, 0.0, 1.0))
        return np.clip(alpha * keep, 0.0, 1.0)
    except Exception:
        return None


def _subject_blob(region: np.ndarray, face_box: Optional[Tuple[int, int, int, int]]):
    """在"非背景"区域里找出**人像主体**那一块（供 GrabCut 当可能前景）。

    有人脸框时从人脸中心扩散，取到的那一整块就是主体（脸 + 头发 + 身体）；
    没有时取最大的一块非背景连通域。
    """
    fg = ~region
    height, width = fg.shape
    if not fg.any():
        return None

    starts: List[Tuple[int, int]] = []
    if face_box is not None:
        left, top, fw, fh = (float(v) for v in face_box[:4])
        cx = int(round(left + fw / 2.0))
        cy = int(round(top + fh / 2.0))
        if 0 <= cx < width and 0 <= cy < height:
            starts.append((cy, cx))
    if not starts:
        # 退而求其次：取最大的非背景连通域
        ys, xs = np.nonzero(fg)
        if ys.size == 0:
            return None
        starts.append((int(ys[0]), int(xs[0])))

    seen = np.zeros_like(fg)
    blob = np.zeros_like(fg)
    stack = [starts[0]]
    seen[starts[0]] = True
    while stack:
        y, x = stack.pop()
        blob[y, x] = True
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < height and 0 <= nx < width and fg[ny, nx] and not seen[ny, nx]:
                seen[ny, nx] = True
                stack.append((ny, nx))
    return blob


def _grabcut_alpha(image: Image.Image,
                   face_box: Optional[Tuple[int, int, int, int]],
                   strength: float,
                   bg_region: Optional[np.ndarray] = None,
                   fg_region: Optional[np.ndarray] = None,
                   bg_extra: Optional[np.ndarray] = None) -> Optional[np.ndarray]:
    """用 OpenCV GrabCut 抠出人像，返回 alpha；不可用或失败返回 None。

    bg_region / fg_region / bg_extra 是可选的先验遮罩（通常来自漫水计算）：
        · bg_region（确定背景）——GrabCut 的背景颜色模型据此建立，
          人脸周围残留的原背景色会被正确归到背景，不会留下光晕；
        · fg_region（人像主体）——当可能前景，头发和身体不会被误吃掉；
        · bg_extra（补充背景）——颜色仍接近背景参考色、但又不在人像主体里的
          像素，用来对付边界生硬的阴影区。
    """
    try:
        import cv2
    except Exception:
        return None

    try:
        width0, height0 = image.size
        if width0 < 4 or height0 < 4:
            return None

        # 工作分辨率：长边不超过 900px，算完再把掩码放大回原图
        scale = min(1.0, GRABCUT_MAX_SIDE / float(max(width0, height0)))
        small_w = max(1, int(round(width0 * scale)))
        small_h = max(1, int(round(height0 * scale)))
        if (small_w, small_h) == (width0, height0):
            small = image
        else:
            small = image.resize((small_w, small_h), Image.Resampling.LANCZOS)

        bgr = cv2.cvtColor(np.asarray(small, dtype=np.uint8), cv2.COLOR_RGB2BGR)
        # 默认：先全部当可能背景，有人脸/主体先验再改写
        mask = np.full((small_h, small_w), cv2.GC_PR_BGD, dtype=np.uint8)

        def _resize_mask(src_mask: np.ndarray) -> np.ndarray:
            if src_mask.shape[:2] == (small_h, small_w):
                return src_mask
            return cv2.resize(src_mask.astype(np.uint8), (small_w, small_h),
                              interpolation=cv2.INTER_NEAREST).astype(bool)

        # ---- 确定背景：漫水算出来的可靠背景区 ----
        if bg_region is not None:
            mask[_resize_mask(bg_region)] = cv2.GC_BGD

        # ---- 可能前景：人像主体（脸 + 头发 + 身体）----
        if fg_region is not None:
            mask[_resize_mask(fg_region)] = cv2.GC_PR_FGD

        # ---- 补充背景：颜色仍接近背景参考色的像素（排除人像主体）----
        if bg_extra is not None:
            extra = _resize_mask(bg_extra)
            if fg_region is not None:
                extra = extra & ~_resize_mask(fg_region)
            mask[extra] = cv2.GC_BGD

        if face_box is not None:
            left, top, fw, fh = (float(v) for v in face_box[:4])
            cx = (left + fw / 2.0) * scale
            cy = (top + fh / 2.0) * scale
            rx = max(2.0, fw * scale / 2.0)
            ry = max(2.0, fh * scale / 2.0)
            # 外扩 2 倍的椭圆 = 可能前景；椭圆之内 = 确定前景
            cv2.ellipse(mask, (int(round(cx)), int(round(cy))),
                        (int(round(rx * 2.0)), int(round(ry * 2.0))),
                        0, 0, 360, cv2.GC_PR_FGD, -1)
            cv2.ellipse(mask, (int(round(cx)), int(round(cy))),
                        (int(round(rx)), int(round(ry))),
                        0, 0, 360, cv2.GC_FGD, -1)
        else:
            # 没人脸框时：四周 6% 边框当确定背景，其余当可能前景
            band_w = max(1, int(round(small_w * 0.06)))
            band_h = max(1, int(round(small_h * 0.06)))
            mask[:, :] = cv2.GC_PR_FGD
            mask[:band_h, :] = cv2.GC_BGD
            mask[small_h - band_h:, :] = cv2.GC_BGD
            mask[:, :band_w] = cv2.GC_BGD
            mask[:, small_w - band_w:] = cv2.GC_BGD

        # GrabCut 要求掩码里必须同时有前景与背景标签
        if not ((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD)).any():
            mask[small_h // 2, small_w // 2] = cv2.GC_PR_FGD
        if not ((mask == cv2.GC_BGD) | (mask == cv2.GC_PR_BGD)).any():
            mask[0, 0] = cv2.GC_PR_BGD

        bgd_model = np.zeros((1, 65), np.float64)
        fgd_model = np.zeros((1, 65), np.float64)
        cv2.grabCut(bgr, mask, None, bgd_model, fgd_model,
                    GRABCUT_ITERATIONS, cv2.GC_INIT_WITH_MASK)

        foreground = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD),
                              1.0, 0.0).astype(np.float32)
        # 羽化：高斯过渡去掉锯齿边
        sigma = max(1.0, min(small_w, small_h) / 200.0)
        alpha = cv2.GaussianBlur(foreground, (0, 0), sigmaX=sigma, sigmaY=sigma)
        alpha = np.clip(alpha, 0.0, 1.0)

        if (small_w, small_h) != (width0, height0):
            alpha = cv2.resize(alpha, (width0, height0), interpolation=cv2.INTER_LINEAR)
        alpha = np.clip(alpha, 0.0, 1.0)

        keep = float(np.clip(strength, 0.0, 1.0))
        return np.clip(alpha * keep, 0.0, 1.0)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 3.5 神经网络抠图（MODNet / ONNX Runtime）
# ---------------------------------------------------------------------------
def _neural_model_paths() -> List[str]:
    """按优先级列出可能存放 MODNet 模型的目录。"""
    roots: List[str] = []
    env = str(os.environ.get("IDPHOTO_MODEL_DIR", "")).strip()
    if env:
        roots.append(env)
    roots.append(os.path.dirname(os.path.abspath(__file__)))
    try:                                     # PyInstaller 打包后的临时目录
        bundle = getattr(sys, "_MEIPASS", "")
        if bundle:
            roots.append(bundle)
    except Exception:
        pass
    roots.append(os.getcwd())

    paths: List[str] = []
    for root in roots:
        for name in NEURAL_MODEL_NAMES:
            full = os.path.join(root, "models", name)
            if full not in paths:
                paths.append(full)
    return paths


def _neural_session():
    """懒加载 ONNX Runtime 会话；不可用返回 None（结果缓存，含失败结果）。"""
    cached = _neural_session_cache.get("session", "missing")
    if cached != "missing":
        return cached

    session = None
    try:
        import onnxruntime as ort                      # 可选依赖
        path = next((p for p in _neural_model_paths() if os.path.isfile(p)), None)
        if path:
            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            opts.intra_op_num_threads = max(1, (os.cpu_count() or 4) // 2)
            session = ort.InferenceSession(path, sess_options=opts,
                                           providers=["CPUExecutionProvider"])
    except Exception:
        session = None

    _neural_session_cache["session"] = session
    return session


def neural_alpha(image: Image.Image,
                 face_box: Optional[Tuple[int, int, int, int]] = None,
                 strength: float = 1.0) -> Optional[np.ndarray]:
    """MODNet 神经网络抠图，返回 alpha（1 = 人物，0 = 背景）；不可用返回 None。

    预处理与 HivisionIDPhotos 一致：RGB → 缩到 512×512 → /255 →
    (x-0.5)/0.5 → NCHW → 推理 → alpha 放大回原图。
    face_box 传入时，人脸椭圆内的像素强制视为前景，防止模型把脸判成背景。
    """
    session = _neural_session()
    if session is None:
        return None
    try:
        import cv2
    except Exception:
        return None

    try:
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        src = np.asarray(rgb, dtype=np.uint8)
        height, width = src.shape[:2]
        if height < 4 or width < 4:
            return None

        size = NEURAL_INPUT_SIZE
        small = cv2.resize(src, (size, size), interpolation=cv2.INTER_LINEAR)
        x = small.astype(np.float32) / 255.0
        x = (x - 0.5) / 0.5
        x = np.ascontiguousarray(x.transpose(2, 0, 1)[None])

        input_name = session.get_inputs()[0].name
        outputs = session.run(None, {input_name: x})
        alpha = np.asarray(outputs[0], dtype=np.float32).squeeze()
        if alpha.shape != (size, size):
            alpha = cv2.resize(alpha, (size, size), interpolation=cv2.INTER_LINEAR)
        alpha = np.clip(alpha, 0.0, 1.0)
        alpha = cv2.resize(alpha, (width, height), interpolation=cv2.INTER_LINEAR)
        alpha = np.clip(alpha, 0.0, 1.0).astype(np.float32)

        # 极低 alpha 的散点是模型对背景的"犹豫"，直接归零，免得换底后
        # 留下一层半透明的原色薄雾（残留背景的主要来源）
        alpha[alpha < 0.08] = 0.0

        # 人脸椭圆内强制前景：模型偶尔会把深色头发/眼镜附近判成背景
        if face_box is not None:
            ellipse = _face_ellipse(height, width, face_box, scale=1.15)
            if ellipse is not None:
                alpha = np.maximum(alpha, ellipse)

        keep = float(np.clip(strength, 0.0, 1.0))
        return np.clip(alpha * keep, 0.0, 1.0)
    except Exception:
        return None


def _face_ellipse(height: int, width: int,
                  face_box: Tuple[int, int, int, int],
                  scale: float = 1.0) -> Optional[np.ndarray]:
    """返回人脸椭圆掩码（float32 0/1），越界或失败返回 None。"""
    try:
        import cv2
    except Exception:
        return None
    try:
        left, top, fw, fh = (float(v) for v in face_box[:4])
        cx = left + fw / 2.0
        cy = top + fh / 2.0
        rx = max(2.0, fw / 2.0 * scale)
        ry = max(2.0, fh / 2.0 * scale)
        mask = np.zeros((int(height), int(width)), np.uint8)
        cv2.ellipse(mask, (int(round(cx)), int(round(cy))),
                    (int(round(rx)), int(round(ry))), 0, 0, 360, 1, -1)
        return mask.astype(np.float32)
    except Exception:
        return None


def _protect_mask(alpha: Optional[np.ndarray],
                  face_box: Optional[Tuple[int, int, int, int]],
                  blob_max_ratio: float = 0.6) -> Optional[np.ndarray]:
    """人物**内芯**保护掩码（float32 0/1）：这些像素绝不允许被换成背景色。

    组成：人脸椭圆（略缩） ∪ 抠图结果里"确定是人物"的最大连通域向内收缩。
    只保护"内芯"而不保护边缘——头发丝、肩膀轮廓仍交给抠图算法决定，
    否则换底后会留一圈原色光晕。

    alpha 为 None 时只用脸椭圆；抠图结果覆盖超过 blob_max_ratio 说明
    算法根本没找到背景（等于没抠），此时不采信连通域，避免"整图被保护"。
    """
    try:
        import cv2
    except Exception:
        return None

    try:
        height, width = alpha.shape[:2] if alpha is not None else (0, 0)
        if not height or not width:
            return None
        mask = np.zeros((height, width), np.uint8)

        if face_box is not None:
            ellipse = _face_ellipse(height, width, face_box, scale=0.9)
            if ellipse is not None:
                mask |= ellipse.astype(np.uint8)

        if alpha is not None:
            core = (alpha > 0.85).astype(np.uint8)
            num, labels, stats, _ = cv2.connectedComponentsWithStats(core, 8)
            best = 0
            best_area = 0
            for idx in range(1, num):
                area = int(stats[idx, cv2.CC_STAT_AREA])
                if area > best_area:
                    best_area = area
                    best = idx
            if best and best_area <= core.size * blob_max_ratio:
                mask |= (labels == best).astype(np.uint8)

        if not mask.any():
            return None

        # 向内收缩：只留内芯，边缘像素不强制，避免留下光晕
        radius = max(2, int(round(min(height, width) * 0.012)))
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
        inner = cv2.erode(mask, kernel)
        if not inner.any():
            inner = mask                     # 收缩后空了（小图）→ 用原掩码
        return inner.astype(np.float32)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 4. 对外接口：换背景
# ---------------------------------------------------------------------------
def replace_background(image: Image.Image,
                       color: Tuple[int, int, int] = (255, 255, 255),
                       mode: str = BG_SOLID,
                       face_box: Optional[Tuple[int, int, int, int]] = None,
                       method: str = METHOD_AUTO,
                       strength: float = 1.0) -> Image.Image:
    """把背景替换成指定颜色/渐变，返回新图（原图不修改）。"""
    try:
        kind = (mode or BG_SOLID).strip().lower()
        if kind == BG_KEEP:
            return image.copy()

        rgb = image if image.mode == "RGB" else image.convert("RGB")
        width, height = rgb.size
        if width < 1 or height < 1:
            return image.copy()

        base = _clamp_color(color)
        background = make_background((width, height), base, kind)
        src = np.asarray(rgb, dtype=np.float32)
        bg = np.asarray(background, dtype=np.float32)

        algo = (method or METHOD_AUTO).strip().lower()
        if algo not in METHODS:
            algo = METHOD_AUTO

        # 先算漫水背景遮罩：它既是独立方案，也是 GrabCut 的**确定背景种子**。
        # 没有这一步，GrabCut 只拿四条边界那一圈当背景，人脸周围残留的
        # 原背景色（阴影/渐变）会被判成前景，换底后就留下一圈色边。
        flood_alpha = _flood_alpha(src, float(strength))
        bg_region = None
        fg_region = None
        bg_extra = None
        if flood_alpha is not None:
            bg_region = flood_alpha < 0.5
            fg_region = _subject_blob(bg_region, face_box)
            # 补充背景：颜色仍接近四角背景参考色的像素（漫水跨不过去的
            # 生硬阴影边界），但**排除人像主体**，免得把同色衣服吃掉
            ref_color = _corner_reference(src)
            if ref_color is not None:
                dist = np.sqrt(((src - ref_color[None, None, :]) ** 2).sum(axis=2))
                bg_extra = dist < FLOOD_BASE_THRESHOLD * FLOOD_SEED_FACTOR
                bg_extra &= ~bg_region
                if fg_region is not None:
                    bg_extra &= ~fg_region

        # 人物内芯保护：基于最终抠图结果算出"确定是人物"的区域，
        # 任何算法都不许把脸/身体主体换成背景色
        protect = None
        alpha = None
        if algo == METHOD_FLOOD:
            alpha = flood_alpha
        else:
            if algo in (METHOD_AUTO, METHOD_NEURAL):
                alpha = neural_alpha(rgb, face_box=face_box, strength=float(strength))
            if alpha is None and algo == METHOD_NEURAL:
                # 明确指定神经网络但模型缺失 → 仍要给出结果，退回 GrabCut
                algo = METHOD_GRABCUT
            if alpha is None and algo in (METHOD_AUTO, METHOD_GRABCUT):
                alpha = _grabcut_alpha(rgb, face_box, float(strength),
                                       bg_region=bg_region, fg_region=fg_region,
                                       bg_extra=bg_extra)
                if alpha is None:            # GrabCut 不可用或失败 → 回退漫水
                    alpha = flood_alpha
                elif flood_alpha is not None:
                    protect = _protect_mask(alpha, face_box)
                    # 漫水**确定**是背景的地方保持背景（只在人物内芯之外），
                    # 其余交给 GrabCut 决定；此前直接相乘会把漫水误判成背景的
                    # 人物像素一起乘没 —— 正是"人物被抠掉"的元凶。
                    bg_forced = flood_alpha < 0.1
                    if protect is not None:
                        bg_forced &= protect < 0.5
                    alpha = np.where(bg_forced, np.minimum(alpha, flood_alpha * 2.0), alpha)
        if alpha is None:                   # 兜底：原样返回，不给调用方添乱
            return rgb.copy()

        alpha = np.clip(alpha, 0.0, 1.0)
        if protect is None:
            protect = _protect_mask(alpha, face_box)
        if protect is not None:             # 人物内芯强制保留原图
            alpha = np.maximum(alpha, protect)
        alpha = alpha.reshape(height, width, 1)
        merged = src * alpha + bg * (1.0 - alpha)
        return Image.fromarray(np.clip(merged, 0, 255).astype(np.uint8), "RGB")
    except Exception:
        try:
            return image.copy()
        except Exception:
            return image


# ---------------------------------------------------------------------------
# 5. 对外接口：JPEG 体积控制
# ---------------------------------------------------------------------------
def _encode_jpeg(img: Image.Image, quality: int, subsampling: int,
                 dpi: Optional[Tuple[int, int]]) -> bytes:
    """按给定质量编码 JPEG；subsampling 0 = 4:4:4，2 = 4:2:0。"""
    buffer = io.BytesIO()
    options = {"format": "JPEG",
               "quality": int(max(1, min(100, quality))),
               "subsampling": int(subsampling)}
    if dpi:
        options["dpi"] = (int(dpi[0]), int(dpi[1]))
    img.save(buffer, **options)
    return buffer.getvalue()


def _encode_within_budget(img: Image.Image, target: int, quality: int,
                          subsampling: int,
                          dpi: Optional[Tuple[int, int]]):
    """在体积上限内找最高可用质量。

    返回 (数据, 实际质量, 是否满足上限)；不满足时返回体积最小的结果。
    """
    data = _encode_jpeg(img, quality, subsampling, dpi)
    if len(data) <= target:
        return data, int(quality), True

    smallest, smallest_q = data, int(quality)
    for step in QUALITY_LADDER:
        if step == int(quality):
            continue                      # 质量序列去重
        candidate = _encode_jpeg(img, step, subsampling, dpi)
        if len(candidate) <= target:
            return candidate, int(step), True
        if len(candidate) < len(smallest):
            smallest, smallest_q = candidate, int(step)
    return smallest, smallest_q, False


def _fit_jpeg_core(image: Image.Image, max_kb: int, quality: int,
                   subsampling: int,
                   dpi: Optional[Tuple[int, int]] = None):
    """fit_jpeg 的内部实现，额外返回实际输出图，便于导出时写入 DPI。"""
    img = image.convert("RGB")
    try:
        limit_kb = int(max_kb)
    except Exception:
        limit_kb = 0
    target = limit_kb * 1024 if limit_kb > 0 else 0
    try:
        start_quality = int(quality)
    except Exception:
        start_quality = 95
    start_quality = max(1, min(100, start_quality))

    if target <= 0:
        data = _encode_jpeg(img, start_quality, subsampling, dpi)
        return data, start_quality, 1.0, img

    data, used_quality, ok = _encode_within_budget(img, target, start_quality,
                                                   subsampling, dpi)
    if ok:
        return data, used_quality, 1.0, img

    # 最低质量仍超标：等比缩小后再试一轮质量阶梯
    current = img
    for _ in range(MAX_SCALE_STEPS):
        ratio = math.sqrt(target / float(max(1, len(data)))) * SCALE_SAFETY
        ratio = max(0.05, min(0.95, ratio))
        new_w = max(1, int(round(current.width * ratio)))
        new_h = max(1, int(round(current.height * ratio)))
        if new_w == current.width and new_h == current.height:
            break
        current = current.resize((new_w, new_h), Image.Resampling.LANCZOS)
        data, used_quality, ok = _encode_within_budget(current, target,
                                                       start_quality, subsampling, dpi)
        if ok:
            return data, used_quality, current.width / float(img.width), current

    return data, used_quality, current.width / float(img.width), current


def fit_jpeg(image: Image.Image, max_kb: int = 0, quality: int = 95,
             subsampling: int = 0) -> Tuple[bytes, int, int]:
    """编码 JPEG。返回 (字节内容, 实际质量, 实际边长缩放比例)。"""
    data, used_quality, scale, _ = _fit_jpeg_core(image, max_kb, quality, subsampling)
    return data, int(used_quality), float(scale)


# ---------------------------------------------------------------------------
# 6. 对外接口：导出单张
# ---------------------------------------------------------------------------
def export_single(image: Image.Image, path: str,
                  size_px: Optional[Tuple[int, int]] = None,
                  max_kb: int = 0, quality: int = 95,
                  dpi: Tuple[int, int] = (300, 300),
                  subsampling: int = 0) -> dict:
    """按目标尺寸与最大体积导出单张证件照，返回结果信息字典。"""
    img = image.convert("RGB")

    if size_px:
        try:
            target_w = int(size_px[0])
            target_h = int(size_px[1])
        except Exception:
            target_w = target_h = 0
        if target_w > 0 and target_h > 0:
            # Cover 裁剪：等比铺满后居中裁掉溢出部分，不拉伸变形
            img = ImageOps.fit(img, (target_w, target_h),
                               method=Image.Resampling.LANCZOS,
                               centering=(0.5, 0.5))

    try:
        dpi_pair = (int(dpi[0]), int(dpi[1]))
    except Exception:
        dpi_pair = (300, 300)
    if dpi_pair[0] <= 0 or dpi_pair[1] <= 0:
        dpi_pair = (300, 300)

    data, used_quality, scale, _ = _fit_jpeg_core(img, max_kb, quality,
                                                  subsampling, dpi=dpi_pair)

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)

    # 回读校验：文件大小与实际像素尺寸都以磁盘上的文件为准
    file_bytes = os.path.getsize(path)
    with Image.open(path) as check:
        real_w, real_h = check.size

    return {
        "path": str(path),
        "width": int(real_w),
        "height": int(real_h),
        "bytes": int(file_bytes),
        "kb": round(file_bytes / 1024.0, 2),
        "quality": int(used_quality),
        "scale": float(scale),
        "dpi": dpi_pair,
        "max_kb": int(max_kb) if max_kb else 0,
        "fit_size": (int(img.size[0]), int(img.size[1])),
    }
