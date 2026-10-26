# -*- coding: utf-8 -*-
"""
layout_engine.py —— 证件照排版引擎（纯 Python，不依赖任何 UI 框架）。

职责：
    1. 读取图片（自动纠正手机照片的 EXIF 方向）；
    2. Cover 模式裁剪填充（ImageOps.fit 居中裁剪，绝不拉伸变形），支持用户自定义裁剪框；
    3. 顺时针旋转（单人照 90°）；
    4. 坐标计算：
       · "wedding" —— 结婚套装（左右两列，照片间留足间距）；
       · "grid"    —— 通用网格（一寸多人 / 二寸 / 小二寸 / 合并排版），带溢出检测；
    5. 合成画布、绘制裁切辅助线/出血线、导出带 300 DPI 元数据的高质量 JPG；
    6. 按图片长宽比自动归类（双人照 / 单人照）。

对外主要接口：
    load_image(path)                 -> PIL.Image
    classify_by_size(size)           -> "couple" | "single"
    prepare_image(img, spec, crop)   -> PIL.Image           （裁剪 + 旋转）
    LayoutEngine(template).build()   -> LayoutResult
    export_jpeg(canvas, path, ...)   -> str
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

from PIL import Image, ImageDraw, ImageOps

import auto_layout
from auto_layout import plan_auto_layout
from config import (
    BLEED,
    BLEED_MM,
    CAT_COUPLE,
    CAT_P1,
    CAT_P2,
    CAT_P3,
    CATEGORY_LABELS,
    COUPLE_RATIO,
    DEFAULT_PAPER_KEY,
    DEFAULT_PRINT_SCALE,
    DPI,
    GAP,
    MARGIN,
    MIX_KEYS,
    PAPER_SIZES,
    SINGLE_RATIO,
    SPECS,
    PhotoSpec,
    SlotSpec,
    Template,
    TEMPLATES,
    WEDDING_TEMPLATE,
    mm_to_px,
    mm_to_spec,
    px_to_cm,
)

# 归一化裁剪框：(left, top, right, bottom)，取值 0~1，相对原图
CropBox = Tuple[float, float, float, float]

PEOPLE_CATEGORIES = (CAT_P1, CAT_P2, CAT_P3)


def scale_spec(spec: PhotoSpec, scale: float) -> PhotoSpec:
    """按校准系数放大照片规格（等比，旋转标记不变）。"""
    if abs(scale - 1.0) < 1e-6:
        return spec
    return replace(
        spec,
        width_px=max(1, int(round(spec.width_px * scale))),
        height_px=max(1, int(round(spec.height_px * scale))),
    )


# ---------------------------------------------------------------------------
# 1. 基础工具：读取 / 裁剪 / 旋转 / 归类
# ---------------------------------------------------------------------------
def load_image(path: str) -> Image.Image:
    """读取图片为 RGB 的 PIL Image，并按 EXIF 自动纠正方向（手机拍摄必做）。"""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)      # 依据 EXIF Orientation 旋转
        im = im.convert("RGB")
        im.load()                             # 数据读入内存后再退出 with
    return im


def read_size(path: str) -> Tuple[int, int]:
    """只读图片尺寸（不解码像素），用于快速自动归类。"""
    with Image.open(path) as im:
        return im.size


def rotate_cw(image: Image.Image, degrees: int = 90) -> Image.Image:
    """顺时针旋转（无损 transpose，不做插值重采样）。

    PIL 的正角度是逆时针，因此顺时针 90° 对应 Transpose.ROTATE_270。
    """
    if degrees % 360 == 0:
        return image
    if degrees % 360 == 90:
        return image.transpose(Image.Transpose.ROTATE_270)
    if degrees % 360 == 180:
        return image.transpose(Image.Transpose.ROTATE_180)
    if degrees % 360 == 270:
        return image.transpose(Image.Transpose.ROTATE_90)
    return image.rotate(-degrees, expand=True)


def default_crop_box(size: Tuple[int, int], ratio: float) -> CropBox:
    """按目标宽高比算出居中的最大裁剪框（归一化）。"""
    w, h = size
    if w / h > ratio:          # 原图更宽 → 裁两侧
        cw = h * ratio
        ch = float(h)
    else:                      # 原图更高 → 裁上下
        cw = float(w)
        ch = w / ratio
    left = (w - cw) / 2.0 / w
    top = (h - ch) / 2.0 / h
    return (left, top, left + cw / w, top + ch / h)


def apply_crop(image: Image.Image, crop: Optional[CropBox]) -> Image.Image:
    """按归一化裁剪框裁剪原图。"""
    if not crop:
        return image
    w, h = image.size
    left, top, right, bottom = crop
    box = (
        max(0, min(w - 1, int(round(left * w)))),
        max(0, min(h - 1, int(round(top * h)))),
        max(1, min(w, int(round(right * w)))),
        max(1, min(h, int(round(bottom * h)))),
    )
    if box[2] - box[0] < 1 or box[3] - box[1] < 1:
        return image
    return image.crop(box)


def fit_cover(image: Image.Image, width: int, height: int,
              crop: Optional[CropBox] = None) -> Image.Image:
    """Cover 模式：居中裁剪填充到目标尺寸，保证铺满且不变形。

    crop 不为空时先按用户指定的裁剪框裁剪，再等比缩放。
    """
    if crop:
        image = apply_crop(image, crop)
    return ImageOps.fit(
        image, (width, height),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def prepare_image(image: Image.Image, spec: PhotoSpec,
                  crop: Optional[CropBox] = None) -> Image.Image:
    """按规格处理一张照片：先（按需）裁剪 → Cover 缩放 → 按需顺时针旋转。"""
    fitted = fit_cover(image, spec.width_px, spec.height_px, crop)
    if spec.rotate_cw:
        fitted = rotate_cw(fitted, spec.rotate_cw)
    return fitted


def classify_by_size(size: Tuple[int, int],
                     couple_ratio: float = COUPLE_RATIO,
                     single_ratio: float = SINGLE_RATIO) -> str:
    """按图片长宽比自动归类。

    横版（宽 > 高）→ 双人照；竖版（高 > 宽）→ 单人照；方图取更接近的一类。
    """
    w, h = size
    if w <= 0 or h <= 0:
        return CAT_P1
    r = w / h
    if r > 1.0:
        return CAT_COUPLE
    if r < 1.0:
        return CAT_P1
    return CAT_COUPLE if abs(r - couple_ratio) <= abs(r - single_ratio) else CAT_P1


# ---------------------------------------------------------------------------
# 2. 结果数据结构
# ---------------------------------------------------------------------------
@dataclass
class PhotoInput:
    """一张待排版的源照片。crop 为用户自定义的归一化裁剪框。"""

    image: Image.Image
    crop: Optional[CropBox] = None

    def prepared(self, spec: PhotoSpec) -> Image.Image:
        return prepare_image(self.image, spec, self.crop)


@dataclass(frozen=True)
class Cell:
    """网格类模板中的一个格子。"""

    category: str
    spec: PhotoSpec
    rotate_cw: int = 0

    @property
    def width(self) -> int:
        """落版宽（已含旋转）。"""
        return self.spec.height_px if self.rotate_cw in (90, 270) else self.spec.width_px

    @property
    def height(self) -> int:
        """落版高（已含旋转）。"""
        return self.spec.width_px if self.rotate_cw in (90, 270) else self.spec.height_px


@dataclass
class Placement:
    """一张照片在画布上的落位信息。"""

    slot_key: str
    source_index: int
    image: Image.Image
    x: int
    y: int
    width: int
    height: int

    @property
    def box(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)


@dataclass
class LayoutResult:
    """一次排版的完整结果。"""

    canvas: Image.Image
    placements: List[Placement] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    template_key: str = ""
    bleed: int = 0
    bleed_box: Tuple[int, int, int, int] = (0, 0, 0, 0)
    used_spacing: int = 0

    @property
    def size(self) -> Tuple[int, int]:
        return self.canvas.size

    @property
    def guide_boxes(self) -> List[Tuple[int, int, int, int]]:
        return [p.box for p in self.placements]


# ---------------------------------------------------------------------------
# 3. 排版引擎
# ---------------------------------------------------------------------------
class LayoutEngine:
    """按模板计算坐标并合成画布。"""

    def __init__(self, template: Template, gap: int = GAP, margin: int = MARGIN,
                 bleed: int = BLEED, print_scale: float = DEFAULT_PRINT_SCALE,
                 canvas_size: Optional[Tuple[int, int]] = None,
                 gap_h: Optional[int] = None) -> None:
        self.template = template
        self.print_scale = float(print_scale)
        self.base_gap = max(1, int(round(gap * self.print_scale)))
        self.margin = max(0, int(round(margin * self.print_scale)))
        self.bleed = bleed
        self.gap = self.base_gap
        # 左右间隔：结婚套装里两列之间的横向缝，与上下间隔（self.gap）互相独立。
        # 为 None 时退化为与上下间隔相同 —— 保证既有调用方行为完全不变。
        self.gap_h = self.base_gap if gap_h is None else max(0, int(gap_h))
        # 画布尺寸：默认沿用模板自带尺寸（既有 9 套模板行为完全不变），
        # 自动排版模板可以在运行时换成别的相纸。
        if canvas_size:
            self.canvas_width = int(canvas_size[0])
            self.canvas_height = int(canvas_size[1])
        else:
            self.canvas_width = int(template.canvas_width)
            self.canvas_height = int(template.canvas_height)
        self._auto_plan: Optional[auto_layout.GridPlan] = None
        self._specs: Dict[str, PhotoSpec] = {
            k: scale_spec(v, self.print_scale) for k, v in SPECS.items()
        }

    # -- 对外主入口 ---------------------------------------------------------
    def build(self, slot_images: Mapping[str, Sequence[Union[PhotoInput, Image.Image]]],
              params: Optional[Mapping[str, object]] = None) -> LayoutResult:
        """执行排版。

        slot_images : {类别 key: [PhotoInput | PIL.Image, ...]}
        params      : 模板动态参数（人数、每人张数等）
        """
        params = dict(params or {})
        warnings: List[str] = []
        sources: Dict[str, List[PhotoInput]] = {}
        for key, items in slot_images.items():
            sources[key] = [it if isinstance(it, PhotoInput) else PhotoInput(it)
                            for it in items]

        # 相纸（自动排版模板）→ 画布尺寸；其余模板沿用自带画布，行为不变
        self.canvas_width, self.canvas_height = self._resolve_canvas(params)

        if self.template.kind == "wedding":
            placements, geo = self._layout_wedding(sources, params)
        else:
            rows, pack_warnings = self._build_rows(self.template.builder, params)
            if self.template.builder == "auto":
                placements, geo = self._layout_auto(rows, self._auto_plan, sources)
            else:
                placements, geo = self._layout_grid(rows, sources)
            geo = list(pack_warnings) + list(geo)
        warnings.extend(geo)
        # 最终硬约束：不许越界、不许重叠（两种版式共用）
        placements = self._enforce_bounds(placements, warnings)

        canvas = Image.new("RGB", (self.canvas_width, self.canvas_height),
                           "white")
        for p in placements:
            canvas.paste(p.image, (int(p.x), int(p.y)))

        b = self.bleed
        return LayoutResult(
            canvas=canvas, placements=placements, warnings=warnings,
            template_key=self.template.key, bleed=self.bleed,
            bleed_box=(b, b, self.canvas_width - 2 * b,
                       self.canvas_height - 2 * b),
            used_spacing=self.gap,
        )

    def _resolve_canvas(self, params: Mapping[str, object]) -> Tuple[int, int]:
        """按参数里的相纸 key 决定画布尺寸。

        只有**声明了 paper_key 的模板**（自动排版）才允许换相纸；
        其余模板一律沿用自带画布，行为与改动前完全一致——
        否则在自动排版里选了 A4 再切回别的模板，画布会被带过去。
        """
        if not self.template.paper_key:
            return (int(self.template.canvas_width), int(self.template.canvas_height))
        key = str(params.get("paper_key") or self.template.paper_key)
        paper = PAPER_SIZES.get(key)
        if paper is not None:
            return (paper.width_px, paper.height_px)
        return (int(self.template.canvas_width), int(self.template.canvas_height))

    # -- 版式构建器（网格类模板）--------------------------------------------
    def _cols_fit(self, width: int) -> int:
        """当前安全区宽度内，一行最多能放多少张该尺寸的照片。"""
        avail = self.canvas_width - 2 * (self.bleed + self.margin)
        n = (avail + self.gap) // (width + self.gap)
        return max(1, int(n))

    def _build_rows(self, builder: str, params: Mapping[str, object]
                    ) -> Tuple[List[List[Cell]], List[str]]:
        """按模板生成"行 → 格子"结构，返回 (行, 提示)。"""
        if builder == "one_inch":
            return self._rows_one_inch(params)
        if builder == "count":
            t = self.template
            spec_key = t.spec_key or "one_inch"
            count = int(params.get("count", t.default_count) or t.default_count)
            landscape = bool(params.get("landscape", t.default_landscape))
            person = str(params.get("count_person", CAT_P1) or CAT_P1)
            if person not in PEOPLE_CATEGORIES:
                person = CAT_P1
            return self._rows_count(spec_key, count, landscape, person), []
        if builder == "couple":
            return self._rows_couple(params)
        if builder == "mixed":
            return self._rows_mixed(params)
        if builder == "auto":
            return self._rows_auto(params)
        return [], []

    # -- 自动排版：按相纸算张数与间隔 ----------------------------------------
    def _auto_spec(self, params: Mapping[str, object]
                   ) -> Tuple[PhotoSpec, int, int]:
        """自动排版用的照片规格：预设规格，或按毫米自定义。

        返回 (规格, 竖版宽, 竖版高)。规格保持**原始朝向**，
        需要横躺时由格子自己带 rotate_cw=90。
        """
        spec_key = str(params.get("auto_spec") or "one_inch")
        if spec_key == "custom":
            try:
                w_mm = float(params.get("auto_custom_w_mm", 25.0) or 25.0)
                h_mm = float(params.get("auto_custom_h_mm", 35.0) or 35.0)
            except (TypeError, ValueError):
                w_mm, h_mm = 25.0, 35.0
            spec = mm_to_spec(w_mm, h_mm)
            return spec, spec.width_px, spec.height_px
        spec = self._specs.get(spec_key, self._specs["one_inch"])
        return spec, spec.width_px, spec.height_px

    def _rows_auto(self, params: Mapping[str, object]
                   ) -> Tuple[List[List[Cell]], List[str]]:
        """自动排版：相纸 → 减出血线 → 算能排几张 → 反算间隔 → 竖版优先再横版。

        具体算法见 auto_layout.plan_auto_layout()。
        """
        spec, w, h = self._auto_spec(params)
        count = int(params.get("auto_count", 8) or 8)
        fill = bool(params.get("auto_fill", True))
        allow_rotate = bool(params.get("auto_rotate", True))
        person = str(params.get("auto_person", CAT_P1) or CAT_P1)
        if person not in PEOPLE_CATEGORIES:
            person = CAT_P1

        avail_w, avail_h = self._safe_area()
        result = plan_auto_layout(count, w, h, avail_w, avail_h,
                                   min_gap=self.gap, fill=fill,
                                   allow_rotate=allow_rotate)
        self._auto_plan = result.plan
        warnings = list(result.notes)

        plan = result.plan
        if plan is None:
            return [], warnings

        # 方案 → 行结构（最后一行只放实际有的张数）
        rows: List[List[Cell]] = []
        for r in range(plan.rows):
            k = plan.last_row if r == plan.rows - 1 else plan.cols
            row: List[Cell] = []
            for _ in range(max(0, k)):
                row.append(Cell(person, spec, rotate_cw=90 if plan.rotated else 0))
            if row:
                rows.append(row)
        return rows, warnings

    def _safe_area(self) -> Tuple[int, int]:
        """安全区（出血线 + 内缩 1mm）的可用宽高 —— 所有内容都必须落在这里面。"""
        return (self.canvas_width - 2 * (self.bleed + self.margin),
                self.canvas_height - 2 * (self.bleed + self.margin))

    @staticmethod
    def _overlaps(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> bool:
        """两个矩形是否真的相交（刚好相邻不算）。"""
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah

    def _enforce_bounds(self, placements: List[Placement],
                        warnings: List[str]) -> List[Placement]:
        """边界硬约束（两种版式共用的最终兜底）：

        · 任何一张照片都不许越过安全区（出血线 + 1mm 内缩）；
        · 任何两张照片都不许互相重叠（校准系数过大时宁可丢弃，也不出界）。
        """
        left = self.bleed + self.margin
        top = left
        right = self.canvas_width - left
        bottom = self.canvas_height - top
        kept: List[Placement] = []
        for p in placements:
            box = (int(p.x), int(p.y), int(p.width), int(p.height))
            if (box[0] < left or box[1] < top
                    or box[0] + box[2] > right or box[1] + box[3] > bottom):
                warnings.append(
                    f"「{CATEGORY_LABELS.get(p.slot_key, p.slot_key)}」落位超出安全区"
                    f"（{box[0]},{box[1]},{box[2]}×{box[3]}px），已移除；"
                    f"请减小校准系数 / 张数 / 间隔。")
                continue
            if any(self._overlaps(box, (int(q.x), int(q.y), int(q.width), int(q.height)))
                   for q in kept):
                warnings.append(
                    f"「{CATEGORY_LABELS.get(p.slot_key, p.slot_key)}」与其他照片重叠，已移除；"
                    f"请减小校准系数。")
                continue
            kept.append(p)
        return kept

    def _rows_one_inch(self, params: Mapping[str, object]
                       ) -> Tuple[List[List[Cell]], List[str]]:
        """一寸多人版式：**先排满 9 张竖版（3×3），剩余的进底部 2 张横版行**。

        · 竖版区固定 3 列 × 3 行 = 9 个位置，先填满；
        · 底部横版行 2 个位置（旋转 90°，高 295 而非 413），一页上限 11 张；
        · 总量超过 11 时，**最后一个人少拿**（前几个人先多拿）；
        · 分配按人物顺序填入 9 个竖版位，再填横版位。
        """
        people = max(1, min(3, int(params.get("people", 1) or 1)))
        per = max(1, int(params.get("per_person", 9) or 9))
        spec = self._specs["one_inch"]
        cols = self._cols_fit(spec.placed_width)
        avail_w, _ = self._safe_area()
        warnings: List[str] = []

        portrait_slots = cols * 3          # 竖版区 3 行
        landscape_slots = 2                # 底部横版行（宽度不够时自动减少）
        capacity = portrait_slots + landscape_slots

        # 每人份数：总量超限时前面的人多拿、最后的人少拿
        total = min(people * per, capacity)
        base = total // people
        extra = total % people
        shares = [base + (1 if i < extra else 0) for i in range(people)]

        # 按人物顺序生成照片序列 → 竖版位填满后再填横版位
        sequence: List[Cell] = []
        for p in range(people):
            for _ in range(shares[p]):
                sequence.append(Cell(PEOPLE_CATEGORIES[p], spec))

        rows: List[List[Cell]] = []
        head = sequence[:portrait_slots]
        for i in range(0, len(head), cols):
            rows.append(head[i:i + cols])
        tail = [Cell(c.category, spec, rotate_cw=90)
                for c in sequence[portrait_slots:portrait_slots + landscape_slots]]
        # 边界：底部横版行放不下那么多张时，只排放得下的
        while tail and (sum(c.width for c in tail) + self.gap * (len(tail) - 1)) > avail_w:
            tail.pop()
        if tail:
            rows.append(tail)
        missing = (len(sequence) - portrait_slots) - len(tail)
        if missing > 0:
            warnings.append(
                f"安全区宽度不足，底部横版行少排 {missing} 张；请降低校准系数或调小间隔。")
        return rows, warnings

    def _rows_count(self, spec_key: str, count: int, landscape: bool,
                    person: str = CAT_P1) -> List[List[Cell]]:
        """按张数排版（小一寸 / 一寸 / 大一寸 / 小二寸 / 二寸 / 大二寸），
        可指定用哪个人物的照片，底部可加 1 张横版合照。"""
        spec = self._specs[spec_key]
        cols = self._cols_fit(spec.placed_width)
        cells = [Cell(person, spec) for _ in range(max(0, count))]
        rows = [cells[i:i + cols] for i in range(0, len(cells), cols)]
        if landscape:
            rows.append([Cell(CAT_COUPLE, self._specs["couple"])])
        return rows

    # -- 结婚照双人：单独排版 ------------------------------------------------
    def _rows_couple(self, params: Mapping[str, object]
                     ) -> Tuple[List[List[Cell]], List[str]]:
        """结婚照双人单独排版：整版只放双人照 5.3×3.5 cm。

        可选的"行"只有两种：
            · 正放行：1 张（626 × 413）
            · 混排行：1 张转 90°（413 × 626）+ 1 张正放（626 × 413），同行 1051 宽

        在安全区 1128 × 1728（1mm 间隔）下：
            1~4 张 → 全部正放（每行 1 张，最多 4 行 = 1688px）；
            5 张   → 1 行正放 + 2 行混排（413 + 626 + 626 + 24 = 1689px）；
            ≥6 张  → 放不下，排到放得下为止并提示。
        """
        spec = self._specs["couple"]
        count = max(0, int(params.get("count", 4) or 0))
        warnings: List[str] = []
        if count == 0:
            return [], warnings

        avail_w, avail_h = self._safe_area()
        gap = self.gap
        w, h = spec.placed_width, spec.placed_height      # 626 × 413

        solo_ok = w <= avail_w and h <= avail_h           # 正放：每行 1 张
        mixed_ok = (h + gap + w) <= avail_w and w <= avail_h  # 混排：每行 2 张
        if not solo_ok and not mixed_ok:
            warnings.append(f"双人照 {w}×{h}px 放不进安全区（{avail_w}×{avail_h}px），"
                            f"请降低校准系数。")
            return [], warnings

        # 枚举所有可行摆法：a = 混排行数，b = 正放行数
        best: Optional[Tuple[int, int, List[List[Cell]]]] = None
        for a in range(0, 4):
            if a and not mixed_ok:
                break
            for b in range(0, 6):
                if b and not solo_ok:
                    break
                rows = a + b
                if rows == 0:
                    continue
                height = a * w + b * h + gap * (rows - 1)
                if height > avail_h:
                    continue
                photos = 2 * a + b
                score = (min(photos, count), -a)       # 先够数，再少横躺
                if best is None or score > (min(best[0], count), -best[1]):
                    best = (photos, a, self._couple_rows(spec, a, b, count))
        if best is None:
            warnings.append(f"校准系数过大，双人照排不进安全区（{avail_w}×{avail_h}px），"
                            f"请调小校准系数。")
            return [], warnings

        rows = best[2]
        placed = sum(len(r) for r in rows)
        if placed < count:
            warnings.append(
                f"6R 安全区最多排 {placed} 张双人照，{count} 张里有 "
                f"{count - placed} 张未排入；请减少张数或调小间隔。")
        return rows, warnings

    @staticmethod
    def _couple_rows(spec: PhotoSpec, a: int, b: int, take: int) -> List[List[Cell]]:
        """按 (a 行混排, b 行正放) 生成行，正放行在上、横躺行在下，总数截到 take。"""
        rows: List[List[Cell]] = []
        for _ in range(b):
            if take <= 0:
                break
            rows.append([Cell(CAT_COUPLE, spec)])
            take -= 1
        for _ in range(a):
            if take <= 0:
                break
            row = [Cell(CAT_COUPLE, spec, rotate_cw=90)]
            take -= 1
            if take > 0:
                row.append(Cell(CAT_COUPLE, spec))
                take -= 1
            rows.append(row)
        return rows

    def _rows_mixed(self, params: Mapping[str, object]) -> Tuple[List[List[Cell]], List[str]]:
        """合并排版：小一寸 / 一寸 / 大一寸 / 小二寸 / 二寸 / 大二寸 任意组合，按数量装箱。

        每种规格可**分别指定用哪个人物**的照片（默认都是人物1）。
        宽度够但高度不足时，该张自动旋转 90° 横躺。
        """
        items: List[Tuple[str, PhotoSpec, int]] = []
        for key in MIX_KEYS:
            n = int(params.get(key, 0) or 0)
            if n <= 0:
                continue
            person = str(params.get(f"{key}_person", CAT_P1) or CAT_P1)
            if person not in PEOPLE_CATEGORIES:
                person = CAT_P1
            items.append((person, self._specs[key], n))
        if not items:
            return [], ["合并排版：张数全部为 0，页面为空；请至少给一种尺寸设置张数。"]
        return self._pack(items)

    def _pack(self, items: Sequence[Tuple[str, PhotoSpec, int]]
              ) -> Tuple[List[List[Cell]], List[str]]:
        """把 (类别, 规格, 数量) 依次装入若干行。

        宽度够但**高度不足**时，把该张旋转 90°（横躺）再放。
        """
        canvas_w = self.canvas_width
        canvas_h = self.canvas_height
        avail_w = canvas_w - 2 * (self.bleed + self.margin)
        avail_h = canvas_h - 2 * (self.bleed + self.margin)
        gap = self.gap
        rows: List[List[Cell]] = []
        warnings: List[str] = []
        row: List[Cell] = []
        row_w = 0
        row_h = 0
        y = 0.0

        for cat, spec, count in items:
            for _ in range(count):
                w, h = spec.placed_width, spec.placed_height
                rot = 0
                # 本张需要的宽度（空行不加行首间隔）
                req = (row_w + gap + w) if row else w
                if row and req > avail_w:
                    rows.append(row)
                    y += row_h + gap
                    row, row_w, row_h = [], 0, 0
                    req = w
                # 高度不够但宽度够 → 旋转 90° 横躺
                if y + max(row_h, h) > avail_h and h > w:
                    rw, rh = h, w
                    req_r = (row_w + gap + rw) if row else rw
                    if req_r <= avail_w and y + max(row_h, rh) <= avail_h:
                        w, h, rot, req = rw, rh, 90, req_r
                if y + max(row_h, h) > avail_h or req > avail_w:
                    warnings.append(
                        f"「{CATEGORY_LABELS.get(cat, cat)}」空间不足（{spec.placed_width}×"
                        f"{spec.placed_height}px 放不下），该张已跳过。")
                    continue
                row.append(Cell(cat, spec, rot))
                row_w += (gap if row_w else 0) + w
                row_h = max(row_h, h)
        if row:
            rows.append(row)
        return rows, warnings

    # -- 网格版式：排布 + 边界硬约束 ----------------------------------------
    def _layout_grid(self, rows: Sequence[Sequence[Cell]],
                     sources: Dict[str, List[PhotoInput]]
                     ) -> Tuple[List[Placement], List[str]]:
        """把行结构落位到画布。

        边界规则（绝不让照片越过安全区）：
            1. 安全区 = 画布 - 出血线 - 1mm 内缩（宽 1128 / 高 1728 @6R）；
            2. 内容超宽 → 间隔先压到 0，仍超宽的行整行不排；
            3. 内容超高 → 间隔按超出量压缩，仍超高则丢弃末尾的行；
            4. 所有坐标用整数计算并以安全区左上角为基准，杜绝四舍五入越界。
        """
        canvas_w = self.canvas_width
        canvas_h = self.canvas_height
        left = self.bleed + self.margin
        top = self.bleed + self.margin
        avail_w = canvas_w - 2 * left
        avail_h = canvas_h - 2 * top
        gap = self.gap
        warnings: List[str] = []
        placements: List[Placement] = []
        rows: List[List[Cell]] = [list(r) for r in rows if r]

        def measure(g: int) -> Tuple[List[int], List[int], int, int]:
            heights = [max((c.height for c in row), default=0) for row in rows]
            widths = [sum(c.width for c in row) + g * (len(row) - 1) for row in rows]
            total_h = sum(heights) + g * max(len(rows) - 1, 0)
            total_w = max(widths) if widths else 0
            return heights, widths, total_w, total_h

        row_heights, row_widths, total_w, total_h = measure(gap)

        # 1) 超宽 → 间隔压到 0
        if total_w > avail_w:
            warnings.append(
                f"内容宽度 {px_to_cm(total_w):.1f}cm 超出安全区 {px_to_cm(avail_w):.1f}cm，"
                f"间隔已压缩为 0；请减少张数、调小间隔或降低校准系数。")
            gap = 0
            row_heights, row_widths, total_w, total_h = measure(gap)

        # 2) 超高 → 按超出量压缩间隔
        if total_h > avail_h:
            over = total_h - avail_h
            n_gaps = max(len(rows) - 1, 1)
            new_gap = max(0, gap - int(math.ceil(over / n_gaps)))
            warnings.append(
                f"内容高度超出安全区 {px_to_cm(over):.2f}cm（约 {over}px），"
                f"间隔已自动压缩为 {new_gap}px；建议减少张数或调小间隔。")
            gap = new_gap
            row_heights, row_widths, total_w, total_h = measure(gap)

        # 3) 仍超宽的行整行不排
        keep: List[List[Cell]] = []
        for i, row in enumerate(rows):
            rw = sum(c.width for c in row) + gap * (len(row) - 1)
            if rw > avail_w:
                warnings.append(
                    f"第 {i + 1} 行宽 {px_to_cm(rw):.1f}cm 仍超出安全区 "
                    f"{px_to_cm(avail_w):.1f}cm，该行 {len(row)} 张未排入。")
                continue
            keep.append(row)
        rows = keep
        row_heights, row_widths, total_w, total_h = measure(gap)

        # 4) 仍超高 → 丢弃末尾的行
        dropped = 0
        while rows and total_h > avail_h:
            dropped += len(rows[-1])
            rows.pop()
            row_heights, row_widths, total_w, total_h = measure(gap)
        if dropped:
            warnings.append(
                f"高度放不下，末尾 {dropped} 张未排入安全区；请减少张数或调小间隔。")
        self.gap = gap

        if not rows:
            return [], warnings

        # 整数落位：以安全区左上角为基准居中，保证不越界
        y = top + (avail_h - total_h) // 2
        cursor: Dict[str, int] = {}
        block_left = left + (avail_w - total_w) // 2

        for row, rh in zip(rows, row_heights):
            rw = sum(c.width for c in row) + gap * (len(row) - 1)
            # 横版行（旋转 90° 的照片）整行居中，其余行统一左对齐成网格
            if any(c.rotate_cw in (90, 270) for c in row):
                x = left + (avail_w - rw) // 2
            else:
                x = block_left
            for cell in row:
                pool = sources.get(cell.category, [])
                if not pool:
                    warnings.append(
                        f"「{CATEGORY_LABELS.get(cell.category, cell.category)}」没有照片，"
                        f"该位置留空。")
                    x += cell.width + gap
                    continue
                k = cursor.get(cell.category, 0) % len(pool)
                cursor[cell.category] = cursor.get(cell.category, 0) + 1
                src = pool[k]
                image = self._prepare_cell(src, cell)
                placements.append(Placement(
                    slot_key=cell.category, source_index=k, image=image,
                    x=int(x), y=int(y + (rh - cell.height) // 2),
                    width=cell.width, height=cell.height))
                x += cell.width + gap
            y += rh + gap

        # 5) 边界与重叠的最终兜底在 build() 里统一执行（_enforce_bounds）

        return placements, warnings

    @staticmethod
    def _prepare_cell(src: PhotoInput, cell: Cell) -> Image.Image:
        """按格子规格准备照片（含格子级别的旋转）。"""
        image = fit_cover(src.image, cell.spec.width_px, cell.spec.height_px, src.crop)
        if cell.rotate_cw:
            image = rotate_cw(image, cell.rotate_cw)
        return image

    # -- 自动排版的落位 -----------------------------------------------------
    def _layout_auto(self, rows: Sequence[Sequence[Cell]],
                     plan: Optional[auto_layout.GridPlan],
                     sources: Dict[str, List[PhotoInput]]
                     ) -> Tuple[List[Placement], List[str]]:
        """按 auto_layout 算好的格数与间隔落位。

        与普通网格不同，这里**每一行都独立撑满可用宽度**（横向间隔由剩余空间
        反算），纵向也正好排满可用高度，最后一行不足时居中。
        """
        warnings: List[str] = []
        if plan is None or not rows:
            return [], warnings

        left = self.bleed + self.margin
        top = self.bleed + self.margin
        avail_w, avail_h = self._safe_area()
        gap_x, gap_y = plan.gap_x, plan.gap_y
        placements: List[Placement] = []
        cursor: Dict[str, int] = {}

        # 纵向：整块正好等于可用高度时就从顶部开始，否则垂直居中
        block_h = plan.rows * plan.photo_h + (plan.rows - 1) * gap_y
        y0 = top + max(0.0, (avail_h - block_h) / 2.0)

        for r, row in enumerate(rows):
            k = len(row)
            if k <= 0:
                continue
            # 最后一行不足 cols 张时，在整行网格里居中
            offset = 0.0
            if k < plan.cols:
                row_w = k * plan.photo_w + (k - 1) * gap_x
                offset = max(0.0, (plan.block_w - row_w) / 2.0)
            x = left + offset
            y = y0 + r * (plan.photo_h + gap_y)
            for cell in row:
                pool = sources.get(cell.category, [])
                if not pool:
                    warnings.append(
                        f"「{CATEGORY_LABELS.get(cell.category, cell.category)}」没有照片，"
                        f"该位置留空。")
                    x += cell.width + gap_x
                    continue
                idx = cursor.get(cell.category, 0) % len(pool)
                cursor[cell.category] = cursor.get(cell.category, 0) + 1
                image = self._prepare_cell(pool[idx], cell)
                placements.append(Placement(
                    slot_key=cell.category, source_index=idx, image=image,
                    x=int(x), y=int(y), width=cell.width, height=cell.height))
                x += cell.width + gap_x

        return placements, warnings

    # -- 版面一：结婚套装 ---------------------------------------------------
    def _layout_wedding(self, sources: Dict[str, List[PhotoInput]],
                        params: Optional[Mapping[str, object]] = None
                        ) -> Tuple[List[Placement], List[str]]:
        """结婚证件照套装版面（画布 1200×1800）。

            ┌──────────────── 1200 ────────────────┐
            │ ┌──────────┐  ┌────────────────────┐ │
            │ │ 双人照 1 │  │ 人物1-1            │ │
            │ └──────────┘  ├────────────────────┤ │
            │ ┌──────────┐  │ 人物1-2            │ │
            │ │ 双人照 2 │  ├────────────────────┤ │
            │ └──────────┘  │ 人物1-3            │ │
            │ ┌──────────┐  ├────────────────────┤ │
            │ │ 双人照 3 │  │ 人物2-1            │ │
            │ └──────────┘  ├────────────────────┤ │
            │ ┌──────────┐  │ 人物2-2（右下吸附）│ │
            │ │ 人物2-1  │  └────────────────────┘ │
            │ └──────────┘                          │
            └───────────────────────────────────────┘
        """
        canvas_w = self.canvas_width
        canvas_h = self.canvas_height
        gap, margin, bleed = self.gap, self.margin, self.bleed
        plan = self.template.wedding_plan
        warnings: List[str] = []
        placements: List[Placement] = []
        # 分布方式可以由界面覆盖（均匀留白 / 固定间隔）；
        # 不传就用模板自带的默认值 —— 既有调用方行为不变。
        distribution = str((params or {}).get("distribution") or plan.distribution)

        left_edge = bleed + margin
        right_edge = canvas_w - bleed - margin
        top_edge = bleed + margin
        bottom_edge = canvas_h - bleed - margin

        couple = sources.get(plan.couple_slot, [])
        male = sources.get(plan.male_slot, [])
        female = sources.get(plan.female_slot, [])

        c_spec = self._specs["couple"]
        cw, ch = c_spec.placed_width, c_spec.placed_height
        s_spec = self._specs["single"]
        sw, sh = s_spec.placed_width, s_spec.placed_height

        def prepared(pool: Sequence[PhotoInput], spec: PhotoSpec) -> List[Image.Image]:
            return [p.prepared(spec) for p in pool]

        couple_imgs = prepared(couple, c_spec)
        male_imgs = prepared(male, s_spec)
        female_imgs = prepared(female, s_spec)

        # ---------- 左列内容：双人照 + 1 张人物2 ----------
        left_items: List[Tuple[str, int, Image.Image, int, int]] = []
        for i in range(plan.couple_count):
            if couple_imgs:
                left_items.append((plan.couple_slot, i, couple_imgs[i % len(couple_imgs)], cw, ch))
        if female_imgs and plan.female_left_count:
            idx = len(female_imgs) - plan.female_left_count
            for k in range(plan.female_left_count):
                j = idx + k
                left_items.append((plan.female_slot, j, female_imgs[j % len(female_imgs)], sw, sh))

        # ---------- 右列内容：人物1 + 人物2 ----------
        right_items: List[Tuple[str, int, Image.Image]] = []
        for i in range(plan.male_count):
            if male_imgs:
                right_items.append((plan.male_slot, i, male_imgs[i % len(male_imgs)]))
        for i in range(plan.female_right_count):
            if female_imgs:
                right_items.append((plan.female_slot, i, female_imgs[i % len(female_imgs)]))

        # ---------- 横向边界：左右两列并排放不下时，右列整体不排（一条明确提示）----------
        if left_items and right_items and left_edge + cw > right_edge - sw:
            warnings.append(
                f"校准系数 {self.print_scale:.4f} 过大：双人照 {cw}px + 单人照 {sw}px "
                f"并排放不进可用宽度 {right_edge - left_edge}px，右列未排入；"
                f"请减小校准系数（建议回到 1.0 附近重新校准）。")
            right_items = []

        # ---------- 新版：两列上对齐 + 等间距（align="top"）----------
        if plan.align == "top":
            placements, more = self._layout_wedding_top(
                left_items, right_items, left_edge, right_edge,
                top_edge, bottom_edge, gap, distribution, sw, sh)
            warnings.extend(more)
            return placements, warnings

        # ---------- 旧版：左列独立分布 ----------
        if left_items:
            heights = [it[4] for it in left_items]
            if distribution == "tight":
                ys, real_gap = self._tight_positions(heights, top_edge, bottom_edge, gap)
            else:
                ys, real_gap = self._even_positions(heights, top_edge, bottom_edge)
            if real_gap < 0:
                warnings.append(
                    f"校准系数 {self.print_scale:.4f} 过大，左列内容超出安全区 "
                    f"{-real_gap:.0f}px；请减小校准系数。")
            kept_l = 0
            for (key, idx, img, w, h), yy in zip(left_items, ys):
                if int(yy) + h > bottom_edge:       # 放不下 → 不排，绝不越出安全区
                    continue
                placements.append(Placement(key, idx, img, left_edge, int(yy), w, h))
                kept_l += 1
            if kept_l < len(left_items):
                warnings.append(
                    f"左列底部 {len(left_items) - kept_l} 张放不下（校准系数 "
                    f"{self.print_scale:.4f} 过大），未排入。")

        # ---------- 旧版：右列独立分布 ----------
        if right_items:
            heights = [sh] * len(right_items)
            if distribution == "tight":
                ys, real_gap = self._tight_positions(heights, top_edge, bottom_edge, gap)
            else:
                ys, real_gap = self._even_positions(heights, top_edge, bottom_edge)
            if real_gap < 0:
                warnings.append(
                    f"校准系数 {self.print_scale:.4f} 过大，右列内容超出安全区 "
                    f"{-real_gap:.0f}px；请减小校准系数。")
            right_x = right_edge - sw            # 紧贴右侧安全边
            start = len(placements)
            for (key, idx, img), yy in zip(right_items, ys):
                if int(yy) + sh > bottom_edge:    # 放不下 → 不排，绝不越出安全区
                    continue
                placements.append(Placement(key, idx, img, right_x, int(yy), sw, sh))
            kept_r = len(placements) - start
            if kept_r < len(right_items):
                warnings.append(
                    f"右列底部 {len(right_items) - kept_r} 张放不下（校准系数 "
                    f"{self.print_scale:.4f} 过大），未排入。")

            # 最后一张人物2仍吸附右下角（仅右列完整排下、且不会压到上一张时）
            if (kept_r == len(right_items) and right_items
                    and right_items[-1][0] == plan.female_slot):
                last = placements[-1]
                snap_x = right_edge - sw
                snap_y = bottom_edge - sh
                prev_bottom = (placements[-2].y + placements[-2].height
                               if len(placements) > 1 else top_edge)
                if snap_y >= prev_bottom and (
                        abs(last.x - snap_x) > 0.5 or abs(last.y - snap_y) > 0.5):
                    placements[-1] = Placement(last.slot_key, last.source_index, last.image,
                                               snap_x, snap_y, sw, sh)

        return placements, warnings

    # -- 结婚套装：两列上对齐 + 上下/左右间距独立 ---------------------------
    def _layout_wedding_top(self, left_items: Sequence[Tuple[str, int, Image.Image, int, int]],
                            right_items: Sequence[Tuple[str, int, Image.Image]],
                            left_edge: int, right_edge: int,
                            top_edge: int, bottom_edge: int, gap: int,
                            distribution: str, sw: int, sh: int
                            ) -> Tuple[List[Placement], List[str]]:
        """两列上对齐的落位，**上下间距与左右间距互相独立**。

        · **上下间距**：左侧「照片间隔」滑块（固定间隔模式）；「均匀留白」
          模式下自动算均匀间隔，但不会小于滑块值；
        · **左右间距**：左侧「左右间隔」滑块 —— 就是两列之间那道缝；
        · **左右放不下时，间隔自动压到能放下的最大值**（最小 0，绝不重叠）；
        · 上下放不下时，间隔同样压到能放下的最大值；
        · 两列**顶部对齐**（同一 y 起步），末张不再单独贴底，
          同列照片之间的间距**完全相等**。
        """
        warnings: List[str] = []
        placements: List[Placement] = []
        height = bottom_edge - top_edge
        avail_w = right_edge - left_edge
        left_h = [int(it[4]) for it in left_items]
        right_h = [int(sh)] * len(right_items)

        def _even_gap(hs: Sequence[int]) -> float:
            """这一列在"均匀分布"下能用的间隔。"""
            if not hs:
                return 0.0
            return max(0.0, (height - sum(hs)) / (len(hs) + 1))

        # ---------- 纵向间隔 ----------
        if distribution == "tight":
            g_v = float(gap)                     # 固定间隔：严格按滑块值
        else:
            # 均匀留白：自动均匀，但**不小于**滑块值（滑块调大一定有效）
            g_v = max(min(_even_gap(left_h), _even_gap(right_h)), float(gap))
        # 上下放不下 → 压到能放下的最大值（精确计算，不用"试错减半"的粗略估计）
        def _max_gap(hs: Sequence[int]) -> float:
            """这一列在固定间隔下，理论能用的最大间隔。"""
            if len(hs) < 2:
                return float("inf")
            return (height - sum(hs)) / float(len(hs) - 1)

        limit = min(_max_gap(left_h), _max_gap(right_h))
        if g_v > limit:
            g_v = max(0.0, limit)
        # 取整成像素间隔：用整数累加落位，避免浮点取整造成 ±1px 的间隔抖动
        g_v = int(round(g_v))
        if g_v < 0:
            g_v = 0

        # ---------- 横向间隔 ----------
        left_w = max([int(it[3]) for it in left_items], default=0)
        right_w = int(sw) if right_items else 0
        g_h = float(self.gap_h)
        # 左右放不下 → 压到能放下的最大值（最小 0，绝不重叠）
        g_h = max(0.0, min(g_h, avail_w - left_w - right_w))
        g_h = int(round(g_h))
        block_w = left_w + g_h + right_w
        if block_w > avail_w:
            warnings.append(
                f"校准系数 {self.print_scale:.4f} 过大：左列 {left_w}px + 右列 "
                f"{right_w}px 超过可用宽度 {avail_w}px，左右间隔已压到 0。")
        # 整块横向居中（左右留白均分）
        x_left = left_edge + max(0, (avail_w - block_w) // 2)
        x_right = x_left + left_w + g_h

        def _margin(hs: Sequence[int]) -> float:
            """这一列在共用间隔 g_v 之后，剩下的上下边距。"""
            if not hs:
                return 0.0
            rest = height - sum(hs) - max(len(hs) - 1, 0) * g_v
            return max(0.0, rest / 2.0)

        if left_h and right_h:
            m = min(_margin(left_h), _margin(right_h))
        else:
            m = max(_margin(left_h), _margin(right_h), 0.0)

        def _ys(hs: Sequence[int]) -> List[int]:
            ys: List[int] = []
            y = int(round(top_edge + m))
            for i, h in enumerate(hs):
                ys.append(y)
                if i < len(hs) - 1:
                    y += h + g_v
            return ys

        left_ys = _ys(left_h)
        right_ys = _ys(right_h)

        # 左列
        kept_l = 0
        for (key, idx, img, w, h), yy in zip(left_items, left_ys):
            if int(yy) + h > bottom_edge:       # 放不下 → 不排，绝不越出安全区
                continue
            placements.append(Placement(key, idx, img, x_left, int(yy), w, h))
            kept_l += 1
        if kept_l < len(left_items):
            warnings.append(
                f"左列底部 {len(left_items) - kept_l} 张放不下（校准系数 "
                f"{self.print_scale:.4f} 过大），未排入。")

        # 右列
        start = len(placements)
        for (key, idx, img), yy in zip(right_items, right_ys):
            if int(yy) + sh > bottom_edge:    # 放不下 → 不排，绝不越出安全区
                continue
            placements.append(Placement(key, idx, img, x_right, int(yy), sw, sh))
        kept_r = len(placements) - start
        if kept_r < len(right_items):
            warnings.append(
                f"右列底部 {len(right_items) - kept_r} 张放不下（校准系数 "
                f"{self.print_scale:.4f} 过大），未排入。")

        return placements, warnings

    # -- 工具：纵向排布 -----------------------------------------------------
    @staticmethod
    def _even_positions(heights: Sequence[int], top: int, bottom: int
                        ) -> Tuple[List[float], float]:
        """均匀分布：剩余空间在上边距 / 照片间距 / 下边距之间平分。"""
        count = len(heights)
        if count == 0:
            return [], 0.0
        raw_gap = (bottom - top - sum(heights)) / (count + 1)
        gap = max(raw_gap, 0.0)
        ys: List[float] = []
        y = float(top) + gap
        for h in heights:
            ys.append(y)
            y += h + gap
        return ys, raw_gap

    @staticmethod
    def _tight_positions(heights: Sequence[int], top: int, bottom: int,
                         gap: int) -> Tuple[List[float], float]:
        """紧凑排布：照片之间保持 gap，剩余空间全部给上下边距。"""
        count = len(heights)
        if count == 0:
            return [], 0.0
        block = sum(heights) + gap * (count - 1)
        offset = max((bottom - top - block) / 2.0, 0.0)
        ys: List[float] = []
        y = float(top) + offset
        for h in heights:
            ys.append(y)
            y += h + gap
        return ys, float(gap)


# ---------------------------------------------------------------------------
# 4. 辅助裁切线 / 出血线
# ---------------------------------------------------------------------------
def _dashed_line(draw, p0, p1, dash: int = 6, gap: int = 4,
                 width: int = 1, fill=(150, 150, 150)) -> None:
    """画一条虚线（PIL 没有原生虚线，这里手工分段）。"""
    x0, y0 = p0
    x1, y1 = p1
    length = math.hypot(x1 - x0, y1 - y0)
    if length <= 0:
        return
    dx, dy = (x1 - x0) / length, (y1 - y0) / length
    pos = 0.0
    while pos < length:
        end = min(pos + dash, length)
        draw.line([(x0 + dx * pos, y0 + dy * pos), (x0 + dx * end, y0 + dy * end)],
                  fill=fill, width=width)
        pos = end + gap


def draw_guide_lines(canvas: Image.Image, placements: Sequence[Placement],
                     color=(150, 150, 150)) -> Image.Image:
    """在画布上绘制每张照片的 1px 灰色虚线边界。"""
    out = canvas.copy()
    draw = ImageDraw.Draw(out)
    for p in placements:
        x, y, w, h = p.box
        _dashed_line(draw, (x, y), (x + w, y), fill=color)
        _dashed_line(draw, (x + w, y), (x + w, y + h), fill=color)
        _dashed_line(draw, (x + w, y + h), (x, y + h), fill=color)
        _dashed_line(draw, (x, y + h), (x, y), fill=color)
    return out


def draw_bleed_line(canvas: Image.Image, bleed_box: Tuple[int, int, int, int],
                    color=(214, 69, 69)) -> Image.Image:
    """绘制出血线（冲印裁切安全边界）。"""
    out = canvas.copy()
    draw = ImageDraw.Draw(out)
    x, y, w, h = bleed_box
    _dashed_line(draw, (x, y), (x + w, y), dash=10, gap=6, width=1, fill=color)
    _dashed_line(draw, (x + w, y), (x + w, y + h), dash=10, gap=6, width=1, fill=color)
    _dashed_line(draw, (x + w, y + h), (x, y + h), dash=10, gap=6, width=1, fill=color)
    _dashed_line(draw, (x, y + h), (x, y), dash=10, gap=6, width=1, fill=color)
    return out


# ---------------------------------------------------------------------------
# 5. 导出
# ---------------------------------------------------------------------------
def ensure_jfif_dpi(path: str, dpi: int = DPI) -> bool:
    """强制写入 JFIF APP0 段的 DPI 元数据（units=1 英寸，X/Y density = dpi）。"""
    with open(path, "rb") as f:
        data = bytearray(f.read())
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return False

    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            return False
        marker = data[i + 1]
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > len(data):
            return False
        seg_len = int.from_bytes(data[i + 2: i + 4], "big")
        if marker == 0xE0 and data[i + 4: i + 9] == b"JFIF\x00":
            # FF E0 | len(2) | "JFIF\0"(5) | version(2) | units(1) | Xdensity(2) | Ydensity(2)
            units_pos = i + 11
            data[units_pos] = 1
            data[units_pos + 1: units_pos + 3] = int(dpi).to_bytes(2, "big")
            data[units_pos + 3: units_pos + 5] = int(dpi).to_bytes(2, "big")
            with open(path, "wb") as f:
                f.write(bytes(data))
            return True
        i += 2 + seg_len
    return False


def export_jpeg(canvas: Image.Image, path: str, dpi: Tuple[int, int] = (DPI, DPI),
                quality: int = 95, placements: Optional[Sequence[Placement]] = None,
                with_guides: bool = False,
                bleed_box: Optional[Tuple[int, int, int, int]] = None) -> str:
    """导出高质量 JPG（quality=95 + 4:4:4，DPI 固定 300）。"""
    img = canvas.convert("RGB")
    if with_guides and placements:
        img = draw_guide_lines(img, placements)
    if bleed_box:
        img = draw_bleed_line(img, bleed_box)

    img.save(path, format="JPEG", quality=quality, dpi=dpi, subsampling=0, optimize=True)
    ensure_jfif_dpi(path, int(dpi[0]))
    return path


# ---------------------------------------------------------------------------
# 6. 命令行自检
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    print("=== 模板自检 ===")
    for t in TEMPLATES.values():
        print(f"[{t.key}] {t.title}  画布 {t.canvas_size_text}")

    def make(w, h, color):
        return Image.new("RGB", (w, h), color)

    fake = {CAT_COUPLE: [make(1600, 1200, (200, 120, 120)) for _ in range(3)],
            CAT_P1: [make(1000, 1500, (120, 160, 220)) for _ in range(3)],
            CAT_P2: [make(1000, 1500, (220, 160, 200)) for _ in range(3)]}

    for key in ("one_inch", "two_inch", "small_two_inch", "mixed"):
        t = TEMPLATES[key]
        params = {"people": 2, "per_person": 4} if key == "one_inch" else {}
        r = LayoutEngine(t).build(fake, params)
        print(f"\n--- {t.title}：{len(r.placements)} 张 ---")
        for p in r.placements:
            print(f"    {p.slot_key:7s} x={p.x:5d} y={p.y:5d} w={p.width} h={p.height}")
        for w in r.warnings:
            print("    !", w)
