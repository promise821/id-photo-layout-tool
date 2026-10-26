# -*- coding: utf-8 -*-
"""
config.py —— 证件照自动排版工具的全局配置。

职责：
    1. DPI / 毫米 / 厘米 → 像素 的换算逻辑；
    2. 出血线、照片间距、打印校准等排版参数；
    3. 照片类别（双人照 / 人物1~3）与照片规格（PhotoSpec）；
    4. 排版模板：结婚套装、一寸（多人可调）、二寸、小二寸、合并排版；
    5. 设置持久化：Windows 注册表（主）+ JSON 文件（备）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, Tuple

# ---------------------------------------------------------------------------
# 1. 分辨率与单位换算
# ---------------------------------------------------------------------------
DPI = 300                       # 冲印分辨率：像素 / 英寸
MM_PER_INCH = 25.4              # 1 英寸 = 25.4 mm
CM_PER_INCH = 2.54              # 1 英寸 = 2.54 cm

PX_PER_CM = DPI / CM_PER_INCH   # 118.110236... px/cm
PX_PER_MM = DPI / MM_PER_INCH   # 11.8110236... px/mm


def mm_to_px(mm: float) -> float:
    """毫米 → 像素（按 300 PPI）。1mm = 11.811 px。"""
    return mm * PX_PER_MM


def cm_to_px(cm: float) -> float:
    """厘米 → 像素（按 300 PPI）。1cm = 300/2.54 = 118.11 px。"""
    return cm * PX_PER_CM


def inch_to_px(inch: float) -> float:
    return inch * DPI


def px_to_cm(px: float) -> float:
    return px / PX_PER_CM


# ---------------------------------------------------------------------------
# 2. 排版参数
# ---------------------------------------------------------------------------
GAP = 12        # 照片与照片之间的最小间距：1mm @300PPI ≈ 11.81 ≈ 12 px
MARGIN = 12     # 照片到出血线之间的安全距离：1mm

# 出血线（裁切线 / 安全区）内缩量。照片必须完整落在出血线以内，防止冲印裁切时切到人脸。
# 2026-10 调整：默认由 2mm 改为 1mm（冲印裁切误差通常只有 1mm 以内，1mm 更贴近实际）。
BLEED_MM = 1.0
BLEED = int(round(mm_to_px(BLEED_MM)))       # 1mm ≈ 12 px
BLEED_OPTIONS_MM: Tuple[float, ...] = (0.0, 1.0, 2.0, 3.0, 5.0)

# 照片之间的自定义间隔（网格类模板使用）
SPACING_MM = 1.0
SPACING_OPTIONS_MM: Tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0)

# 6R 竖版画布
CANVAS_6R_W = int(round(cm_to_px(10.16)))    # 1200
CANVAS_6R_H = int(round(cm_to_px(15.24)))    # 1800


# ---------------------------------------------------------------------------
# 2.1 相纸尺寸（自动排版用）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PaperSize:
    """相纸尺寸：width_px/height_px 为 300DPI 下的像素。"""

    key: str
    label: str
    width_px: int
    height_px: int

    @property
    def size_text(self) -> str:
        return f"{self.width_px} × {self.height_px} px"

    @property
    def cm_text(self) -> str:
        return f"{px_to_cm(self.width_px):.2f} × {px_to_cm(self.height_px):.2f} cm"


def _cm_paper(key: str, label: str, w_cm: float, h_cm: float) -> PaperSize:
    return PaperSize(key, label, int(round(cm_to_px(w_cm))), int(round(cm_to_px(h_cm))))


# 常用冲印相纸。默认仍是 6R 竖版（与既有 9 套模板画布完全一致，行为不变）。
PAPER_SIZES: Dict[str, PaperSize] = {
    p.key: p for p in (
        _cm_paper("6r_portrait", "6R 竖版（10.16×15.24cm）", 10.16, 15.24),
        _cm_paper("6r_landscape", "6R 横版（15.24×10.16cm）", 15.24, 10.16),
        _cm_paper("5r_portrait", "5R 竖版（8.89×12.70cm）", 8.89, 12.70),
        _cm_paper("5r_landscape", "5R 横版（12.70×8.89cm）", 12.70, 8.89),
        _cm_paper("4r_portrait", "4R 竖版（10.16×12.70cm）", 10.16, 12.70),
        _cm_paper("photo5_landscape", "5 寸相纸（15.20×10.20cm）", 15.20, 10.20),
        _cm_paper("a4_portrait", "A4 竖版（21.00×29.70cm）", 21.00, 29.70),
        _cm_paper("a4_landscape", "A4 横版（29.70×21.00cm）", 29.70, 21.00),
        _cm_paper("a5_portrait", "A5 竖版（14.80×21.00cm）", 14.80, 21.00),
        _cm_paper("a6_portrait", "A6 竖版（10.50×14.80cm）", 10.50, 14.80),
    )
}
DEFAULT_PAPER_KEY = "6r_portrait"


# ---------------------------------------------------------------------------
# 3. 照片类别
# ---------------------------------------------------------------------------
CAT_COUPLE = "couple"      # 双人照（横版）
CAT_P1 = "p1"              # 人物 1
CAT_P2 = "p2"              # 人物 2
CAT_P3 = "p3"              # 人物 3

CATEGORIES: Tuple[str, ...] = (CAT_COUPLE, CAT_P1, CAT_P2, CAT_P3)
CATEGORY_LABELS: Dict[str, str] = {
    CAT_COUPLE: "双人照",
    CAT_P1: "人物1",
    CAT_P2: "人物2",
    CAT_P3: "人物3",
}
CATEGORY_SHORT: Dict[str, str] = {
    CAT_COUPLE: "双人",
    CAT_P1: "人1",
    CAT_P2: "人2",
    CAT_P3: "人3",
}
SINGLE_CATEGORIES: Tuple[str, ...] = (CAT_P1, CAT_P2, CAT_P3)


# ---------------------------------------------------------------------------
# 4. 照片规格
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PhotoSpec:
    """width_px/height_px 为"原始朝向"下的裁切尺寸；rotate_cw 为顺时针旋转角（0/90）。"""

    key: str
    label: str
    width_px: int
    height_px: int
    rotate_cw: int = 0

    @property
    def placed_width(self) -> int:
        return self.height_px if self.rotate_cw in (90, 270) else self.width_px

    @property
    def placed_height(self) -> int:
        return self.width_px if self.rotate_cw in (90, 270) else self.height_px

    @property
    def ratio(self) -> float:
        """原始朝向下的宽高比，用于自动归类与裁剪框。"""
        return self.width_px / self.height_px

    @property
    def size_text(self) -> str:
        return f"{self.width_px} × {self.height_px} px"

    @property
    def cm_text(self) -> str:
        """标称物理尺寸，如 '3.5×4.9cm'（按未校准的原始规格）。"""
        return f"{px_to_cm(self.width_px):.1f}×{px_to_cm(self.height_px):.1f}cm"


# ---- 证件照规格尺寸表（与《证件照规格尺寸》一致，单位 cm @300DPI）----------
#   小一寸 2.2×3.2   一寸 2.5×3.5   大一寸 3.3×4.8
#   小二寸 3.5×4.5   二寸 3.5×4.9   大二寸 3.5×5.3
def _cm_spec(key: str, label: str, w_cm: float, h_cm: float,
             rotate_cw: int = 0) -> PhotoSpec:
    return PhotoSpec(key, label, int(round(cm_to_px(w_cm))),
                     int(round(cm_to_px(h_cm))), rotate_cw)


# 双人照（横版）：5.3 cm × 3.5 cm → 626 × 413 px
SPEC_COUPLE = _cm_spec("couple", "双人照（横版）", 5.3, 3.5)
# 单人照（竖版，排版时顺时针 90° 横躺）：2.5 cm × 3.5 cm → 295 × 413 → 413 × 295
SPEC_SINGLE = _cm_spec("single", "单人照（竖版）", 2.5, 3.5, rotate_cw=90)

SPEC_TINY_ONE_INCH = _cm_spec("tiny_one_inch", "小一寸（竖版）", 2.2, 3.2)      # 260 × 378
SPEC_ONE_INCH = _cm_spec("one_inch", "一寸照（竖版）", 2.5, 3.5)                # 295 × 413
SPEC_BIG_ONE_INCH = _cm_spec("big_one_inch", "大一寸（竖版）", 3.3, 4.8)        # 390 × 567
SPEC_SMALL_TWO_INCH = _cm_spec("small_two_inch", "小二寸（竖版）", 3.5, 4.5)    # 413 × 531
SPEC_TWO_INCH = _cm_spec("two_inch", "二寸照（竖版）", 3.5, 4.9)                # 413 × 579
SPEC_BIG_TWO_INCH = _cm_spec("big_two_inch", "大二寸（竖版）", 3.5, 5.3)        # 413 × 626

# 自动归类用的目标宽高比
COUPLE_RATIO = SPEC_COUPLE.ratio              # 1.5157（横版）
SINGLE_RATIO = SPEC_ONE_INCH.ratio            # 0.7143（竖版）

# 全部规格（供引擎按 key 索引，自动套用校准系数）
SPECS: Dict[str, PhotoSpec] = {
    "couple": SPEC_COUPLE,
    "single": SPEC_SINGLE,
    "tiny_one_inch": SPEC_TINY_ONE_INCH,
    "one_inch": SPEC_ONE_INCH,
    "big_one_inch": SPEC_BIG_ONE_INCH,
    "small_two_inch": SPEC_SMALL_TWO_INCH,
    "two_inch": SPEC_TWO_INCH,
    "big_two_inch": SPEC_BIG_TWO_INCH,
}


def make_spec(width_px: int, height_px: int, key: str = "custom",
              label: str = "自定义") -> PhotoSpec:
    """按像素临时造一个规格（自定义尺寸 / 考试系统上传用）。"""
    return PhotoSpec(key, label, max(1, int(width_px)), max(1, int(height_px)))


def mm_to_spec(w_mm: float, h_mm: float, key: str = "custom",
               label: str = "自定义") -> PhotoSpec:
    """按毫米造规格（自动按 300DPI 换算成像素）。"""
    return make_spec(int(round(mm_to_px(w_mm))), int(round(mm_to_px(h_mm))), key, label)

# ---- 合并排版里可选的六种尺寸（顺序即设置存储顺序）----
MIX_KEYS: Tuple[str, ...] = ("tiny_one_inch", "one_inch", "big_one_inch",
                             "small_two_inch", "two_inch", "big_two_inch")
MIX_LABELS: Dict[str, str] = {
    "tiny_one_inch": "小一寸", "one_inch": "一寸", "big_one_inch": "大一寸",
    "small_two_inch": "小二寸", "two_inch": "二寸", "big_two_inch": "大二寸",
}
MIX_DEFAULT: Tuple[int, ...] = (0, 3, 0, 2, 2, 0)     # 一寸3 + 二寸2 + 小二寸2
MIX_PEOPLE_DEFAULT: Tuple[str, ...] = ("p1",) * 6

# 自动排版模板可选的照片规格（"custom" = 按毫米自定义，用于考试系统上传）
AUTO_SPEC_KEYS: Tuple[str, ...] = ("tiny_one_inch", "one_inch", "big_one_inch",
                                   "small_two_inch", "two_inch", "big_two_inch",
                                   "couple", "custom")
AUTO_SPEC_LABELS: Dict[str, str] = {
    "tiny_one_inch": "小一寸 2.2×3.2cm", "one_inch": "一寸 2.5×3.5cm",
    "big_one_inch": "大一寸 3.3×4.8cm", "small_two_inch": "小二寸 3.5×4.5cm",
    "two_inch": "二寸 3.5×4.9cm", "big_two_inch": "大二寸 3.5×5.3cm",
    "couple": "双人照 5.3×3.5cm", "custom": "自定义尺寸",
}


# ---------------------------------------------------------------------------
# 4.1 尺寸预设（裁剪对话框 / 单张导出用）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SizePreset:
    """一个尺寸预设：像素尺寸 + 所属分组。"""

    key: str
    label: str
    group: str
    width_px: int
    height_px: int

    @property
    def cm_text(self) -> str:
        return f"{px_to_cm(self.width_px):.1f}×{px_to_cm(self.height_px):.1f}cm"

    @property
    def ratio(self) -> float:
        return self.width_px / self.height_px


SIZE_PRESETS: Tuple[SizePreset, ...] = (
    # —— 证件照规格 ——
    SizePreset("tiny_one_inch", "小一寸 2.2×3.2cm", "证件照规格", 260, 378),
    SizePreset("one_inch", "一寸 2.5×3.5cm", "证件照规格", 295, 413),
    SizePreset("big_one_inch", "大一寸 3.3×4.8cm", "证件照规格", 390, 567),
    SizePreset("small_two_inch", "小二寸 3.5×4.5cm", "证件照规格", 413, 531),
    SizePreset("two_inch", "二寸 3.5×4.9cm", "证件照规格", 413, 579),
    SizePreset("big_two_inch", "大二寸 3.5×5.3cm", "证件照规格", 413, 626),
    SizePreset("couple", "双人照 5.3×3.5cm", "证件照规格", 626, 413),
    # —— 考试系统常用上传尺寸 ——
    SizePreset("exam_1inch", "一寸 295×413px", "考试系统", 295, 413),
    SizePreset("exam_2inch", "二寸 413×579px", "考试系统", 413, 579),
    SizePreset("exam_240x320", "240×320px", "考试系统", 240, 320),
    SizePreset("exam_300x400", "300×400px", "考试系统", 300, 400),
    SizePreset("exam_360x480", "360×480px", "考试系统", 360, 480),
    SizePreset("exam_480x640", "480×640px", "考试系统", 480, 640),
    SizePreset("exam_540x720", "540×720px", "考试系统", 540, 720),
    # —— 驾照 ——
    SizePreset("license_22x32", "22×32mm（260×378px）", "驾照", 260,  378),
    SizePreset("license_15x22", "15×22mm（177×260px）", "驾照", 177,  260),
    SizePreset("license_1inch", "一寸 25×35mm（295×413px）", "驾照", 295, 413),
)

SIZE_PRESET_BY_KEY: Dict[str, SizePreset] = {p.key: p for p in SIZE_PRESETS}

# 尺寸预设下拉里的两个特殊项（不是真实尺寸，只是行为开关）
PRESET_DEFAULT = "__default__"   # 跟随类别默认规格
PRESET_CUSTOM = "__custom__"     # 用户自定义宽高

# 旧版设置只有 3 个值（一寸 / 二寸 / 小二寸），读取时补齐成 6 个
_LEGACY_MIX_POS = {"one_inch": 1, "two_inch": 4, "small_two_inch": 3}


def normalize_mix(raw) -> Tuple[int, ...]:
    """把设置里的 mix 还原成 6 个整数（兼容旧版 3 个值 / 带括号的字符串）。"""
    try:
        vals = [int(x) for x in _split_seq(raw)]
    except Exception:
        return MIX_DEFAULT
    if len(vals) == 6:
        return tuple(max(0, v) for v in vals)
    out = [0] * 6
    if len(vals) == 3:                      # 旧版：一寸, 二寸, 小二寸
        for legacy_i, name in enumerate(("one_inch", "two_inch", "small_two_inch")):
            out[_LEGACY_MIX_POS[name]] = max(0, vals[legacy_i])
        return tuple(out)
    for i, v in enumerate(vals[:6]):
        out[i] = max(0, v)
    return tuple(out)


def normalize_mix_people(raw) -> Tuple[str, ...]:
    """把设置里的 mix_people 还原成 6 个人物 key（兼容旧版 3 个值）。"""
    vals = _split_seq(raw) or ["p1"]
    out = ["p1"] * 6
    if len(vals) == 3:
        for legacy_i, name in enumerate(("one_inch", "two_inch", "small_two_inch")):
            out[_LEGACY_MIX_POS[name]] = str(vals[legacy_i])
        return tuple(out)
    for i, v in enumerate(vals[:6]):
        out[i] = str(v)
    return tuple(out)


# ---------------------------------------------------------------------------
# 5. 模板
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SlotSpec:
    """一个照片类别（槽位）及其规格与需要的张数。"""

    key: str
    label: str
    hint: str
    spec: PhotoSpec
    count: int


@dataclass(frozen=True)
class WeddingPlan:
    """结婚套装版面：左列 3 张双人照 + 1 张人物2；右列 3 张人物1 + 2 张人物2。"""

    couple_slot: str = CAT_COUPLE
    male_slot: str = CAT_P1
    female_slot: str = CAT_P2
    couple_count: int = 3
    male_count: int = 3
    female_right_count: int = 2     # 右列人物2
    female_left_count: int = 1      # 挪到左列的人物2
    distribution: str = "even"      # "even" 均匀留白 / "tight" 紧凑 1mm
    align: str = "top"              # "top" 两列上对齐且间距相等 /
                                     # "snap_bottom" 末张人物2吸附右下角（旧行为）


@dataclass(frozen=True)
class Template:
    """一个排版模板（预设）。"""

    key: str
    title: str
    description: str
    canvas_width: int = CANVAS_6R_W
    canvas_height: int = CANVAS_6R_H
    kind: str = "wedding"                      # "wedding" | "grid"
    builder: str = ""                          # 网格类模板的版式构建器
    spec_key: str = ""                         # "count" 构建器使用的规格 key
    slots: Tuple[SlotSpec, ...] = ()
    wedding_plan: WeddingPlan = WeddingPlan()
    export_name: str = "排版.jpg"
    adjustable: bool = False                   # 是否支持"人数 / 每人张数"调节
    default_people: int = 1
    default_per_person: int = 11
    default_count: int = 4                     # 张数类模板的默认张数
    max_count: int = 12                        # 张数上限（按安全区容量预计算）
    default_landscape: bool = False            # 底部是否加 1 张横版合照
    default_mix: Tuple[int, ...] = MIX_DEFAULT  # 合并排版：六种尺寸各自数量
    # 相纸 key（"" = 沿用 canvas_width/canvas_height）。自动排版模板用它切换相纸。
    paper_key: str = ""

    def slot(self, key: str) -> SlotSpec:
        for s in self.slots:
            if s.key == key:
                return s
        raise KeyError(key)

    @property
    def canvas_size_text(self) -> str:
        w = px_to_cm(self.canvas_width)
        h = px_to_cm(self.canvas_height)
        return f"{self.canvas_width} × {self.canvas_height} px（{w:.2f} × {h:.2f} cm @ {DPI} DPI）"


# ---- 模板一：结婚证件照套装（6R 竖版）------------------------------------
WEDDING_TEMPLATE = Template(
    key="wedding",
    title="结婚证件照套装（6R 竖版）",
    description="左列 3 张双人照 + 1 张人物2，右列 3 张人物1 + 2 张人物2，"
                "输出 1200×1800 px（10.16×15.24 cm @300DPI）",
    kind="wedding",
    wedding_plan=WeddingPlan(),
    export_name="结婚证件照排版_6R.jpg",
    slots=(
        SlotSpec(CAT_COUPLE, "双人照（横版）", "5.3 × 3.5 cm", SPEC_COUPLE, 3),
        SlotSpec(CAT_P1, "人物1（竖版）", "2.5 × 3.5 cm，旋转 90° 横躺", SPEC_SINGLE, 3),
        SlotSpec(CAT_P2, "人物2（竖版）", "2.5 × 3.5 cm，旋转 90° 横躺", SPEC_SINGLE, 3),
    ),
)

# ---- 模板二：一寸照（多人可调，2 人时底部加横版合照）---------------------
ONE_INCH_TEMPLATE = Template(
    key="one_inch",
    title="一寸照（多人自动分布）",
    description="一页最多 11 张：上方 3 列竖版（最多 9 张）+ 底部 2 张横版（旋转 90°）；"
                "人数按导入的人物照片自动判定，每人张数自动计算",
    kind="grid",
    builder="one_inch",
    export_name="一寸照排版_6R.jpg",
    adjustable=True,
    default_people=1,
    default_per_person=11,
    slots=(
        SlotSpec(CAT_P1, "人物1（一寸）", "2.5 × 3.5 cm", SPEC_ONE_INCH, 11),
        SlotSpec(CAT_P2, "人物2（一寸）", "2.5 × 3.5 cm", SPEC_ONE_INCH, 11),
        SlotSpec(CAT_P3, "人物3（一寸）", "2.5 × 3.5 cm", SPEC_ONE_INCH, 11),
    ),
)

# ---- 模板三：小一寸（张数可调）-------------------------------------------
TINY_ONE_INCH_TEMPLATE = Template(
    key="tiny_one_inch",
    title="小一寸（2.2×3.2）",
    description="小一寸 2.2×3.2 cm（260×378 px），一页最多 16 张，张数/人物可调",
    kind="grid",
    builder="count",
    spec_key="tiny_one_inch",
    export_name="小一寸照排版_6R.jpg",
    default_count=4,
    max_count=16,
    default_landscape=False,
    slots=(SlotSpec(CAT_P1, "人物1（小一寸）", "2.2 × 3.2 cm", SPEC_TINY_ONE_INCH, 4),),
)

# ---- 模板四：二寸照（张数可调，默认 4 张）---------------------------------
TWO_INCH_TEMPLATE = Template(
    key="two_inch",
    title="二寸照（3.5×4.9）",
    description="二寸照 3.5×4.9 cm（413×579 px），一页最多 4 张，张数/人物/底部合照可调",
    kind="grid",
    builder="count",
    spec_key="two_inch",
    export_name="二寸照排版_6R.jpg",
    default_count=4,
    max_count=4,
    default_landscape=False,
    slots=(
        SlotSpec(CAT_P1, "人物1（二寸）", "3.5 × 4.9 cm", SPEC_TWO_INCH, 4),
        SlotSpec(CAT_P2, "人物2（二寸）", "3.5 × 4.9 cm", SPEC_TWO_INCH, 4),
    ),
)

# ---- 模板五：大一寸（张数可调）-------------------------------------------
BIG_ONE_INCH_TEMPLATE = Template(
    key="big_one_inch",
    title="大一寸（3.3×4.8）",
    description="大一寸 3.3×4.8 cm（390×567 px），一页最多 6 张，张数/人物可调",
    kind="grid",
    builder="count",
    spec_key="big_one_inch",
    export_name="大一寸照排版_6R.jpg",
    default_count=4,
    max_count=6,
    default_landscape=False,
    slots=(SlotSpec(CAT_P1, "人物1（大一寸）", "3.3 × 4.8 cm", SPEC_BIG_ONE_INCH, 4),),
)

# ---- 模板六：小二寸（先排 2 张 + 底部横版可选）----------------------------
SMALL_TWO_INCH_TEMPLATE = Template(
    key="small_two_inch",
    title="小二寸（3.5×4.5）",
    description="小二寸 3.5×4.5 cm（413×531 px），先排 2 张，底部横版合照可选；一页最多 6 张",
    kind="grid",
    builder="count",
    spec_key="small_two_inch",
    export_name="小二寸照排版_6R.jpg",
    default_count=2,
    max_count=6,
    default_landscape=True,
    slots=(
        SlotSpec(CAT_P1, "人物1（小二寸）", "3.5 × 4.5 cm", SPEC_SMALL_TWO_INCH, 2),
        SlotSpec(CAT_P2, "人物2（小二寸）", "3.5 × 4.5 cm", SPEC_SMALL_TWO_INCH, 2),
        SlotSpec(CAT_COUPLE, "合照（横版）", "5.3 × 3.5 cm", SPEC_COUPLE, 1),
    ),
)

# ---- 模板七：大二寸（张数可调）-------------------------------------------
BIG_TWO_INCH_TEMPLATE = Template(
    key="big_two_inch",
    title="大二寸（3.5×5.3）",
    description="大二寸 3.5×5.3 cm（413×626 px），一页最多 4 张，张数/人物可调",
    kind="grid",
    builder="count",
    spec_key="big_two_inch",
    export_name="大二寸照排版_6R.jpg",
    default_count=4,
    max_count=4,
    default_landscape=False,
    slots=(SlotSpec(CAT_P1, "人物1（大二寸）", "3.5 × 5.3 cm", SPEC_BIG_TWO_INCH, 4),),
)

# ---- 模板八：结婚照双人（单独排版）----------------------------------------
COUPLE_ONLY_TEMPLATE = Template(
    key="couple_only",
    title="结婚照双人（单独排版）",
    description="整版只排双人照 5.3×3.5 cm：1~4 张竖着横躺排，5 张时自动把两张转 90° 收进安全区",
    kind="grid",
    builder="couple",
    export_name="结婚双人照排版_6R.jpg",
    default_count=4,
    max_count=5,
    default_landscape=False,
    slots=(SlotSpec(CAT_COUPLE, "双人照（横版）", "5.3 × 3.5 cm", SPEC_COUPLE, 5),),
)

# ---- 模板九：合并排版（六种尺寸任意混排）---------------------------------
MIXED_TEMPLATE = Template(
    key="mixed",
    title="合并排版（六种尺寸任意混排）",
    description="小一寸 / 一寸 / 大一寸 / 小二寸 / 二寸 / 大二寸 任意组合，"
                "按数量自动装箱，高度不足自动旋转 90°；每种尺寸可分别指定用谁的照片",
    kind="grid",
    builder="mixed",
    export_name="合并排版_6R.jpg",
    default_mix=MIX_DEFAULT,
    slots=(
        SlotSpec(CAT_P1, "人物1", "小一寸 / 一寸 / 大一寸 / 小二寸 / 二寸 / 大二寸",
                 SPEC_ONE_INCH, 3),
        SlotSpec(CAT_P2, "人物2", "小一寸 / 一寸 / 大一寸 / 小二寸 / 二寸 / 大二寸",
                 SPEC_ONE_INCH, 2),
    ),
)

# ---- 模板十：自动排版（按相纸自动算张数与间隔）----------------------------
AUTO_TEMPLATE = Template(
    key="auto",
    title="自动排版（按相纸自动算张数）",
    description="选定相纸与照片尺寸后自动计算：相纸 − 出血线 = 可用区，"
                "算出能排满几张、需要多大照片间隔；排不满自动减一张重算；"
                "先算竖版再算横版（可旋转 90°）",
    kind="grid",
    builder="auto",
    export_name="自动排版.jpg",
    default_count=8,
    max_count=60,
    paper_key=DEFAULT_PAPER_KEY,
    slots=(
        SlotSpec(CAT_P1, "人物1", "按所选尺寸自动裁切", SPEC_ONE_INCH, 8),
        SlotSpec(CAT_P2, "人物2", "按所选尺寸自动裁切", SPEC_ONE_INCH, 8),
    ),
)

TEMPLATES: Dict[str, Template] = {
    t.key: t for t in (WEDDING_TEMPLATE, COUPLE_ONLY_TEMPLATE, ONE_INCH_TEMPLATE,
                       TINY_ONE_INCH_TEMPLATE, BIG_ONE_INCH_TEMPLATE,
                       SMALL_TWO_INCH_TEMPLATE, TWO_INCH_TEMPLATE,
                       BIG_TWO_INCH_TEMPLATE, MIXED_TEMPLATE, AUTO_TEMPLATE)
}


# ---------------------------------------------------------------------------
# 6. 打印校准
# ---------------------------------------------------------------------------
# 冲印/打印机常把整张图像缩放到"可打印区域"（如 6R 相纸实际只印 9.6×14.4cm），
# 导致成品比设计尺寸小一圈（实测约 0.943 倍）。
# 校准系数 = 标称尺寸 / 实测尺寸，用来把设计尺寸放大回去。
DEFAULT_PRINT_SCALE = 1.0
PRINT_SCALE_RANGE = (0.80, 1.40)


@dataclass(frozen=True)
class CalibTarget:
    """校准基准：量的是哪个尺寸，标称值是多少厘米。"""

    key: str
    label: str
    nominal_cm: float
    hint: str = ""


CALIB_TARGETS: Tuple[CalibTarget, ...] = (
    CalibTarget("paper_w", "相纸宽度（整张 6R）", 10.16, "量相纸左右最外沿"),
    CalibTarget("paper_h", "相纸高度（整张 6R）", 15.24, "量相纸上下最外沿"),
    CalibTarget("couple_w", "双人照宽度", 5.3, "量冲印件上双人照的宽"),
    CalibTarget("couple_h", "双人照高度", 3.5, "量冲印件上双人照的高"),
    CalibTarget("one_w", "一寸宽度", 2.5),
    CalibTarget("one_h", "一寸高度", 3.5),
    CalibTarget("tiny_w", "小一寸宽度", 2.2),
    CalibTarget("tiny_h", "小一寸高度", 3.2),
    CalibTarget("big_one_w", "大一寸宽度", 3.3),
    CalibTarget("big_one_h", "大一寸高度", 4.8),
    CalibTarget("small_w", "小二寸宽度", 3.5),
    CalibTarget("small_h", "小二寸高度", 4.5),
    CalibTarget("two_w", "二寸宽度", 3.5),
    CalibTarget("two_h", "二寸高度", 4.9),
    CalibTarget("big_two_w", "大二寸宽度", 3.5),
    CalibTarget("big_two_h", "大二寸高度", 5.3),
)


# ---------------------------------------------------------------------------
# 7. 设置持久化：注册表（Windows，优先） + JSON 文件（备）
# ---------------------------------------------------------------------------
REG_PATH = r"Software\PhotoLayout\PhotoLayoutTool"
APP_SETTINGS_DIR = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), "PhotoLayout")
APP_SETTINGS_FILE = os.path.join(APP_SETTINGS_DIR, "settings.json")

# 设置版本号：用于一次性迁移旧设置
#   v2 = 出血线默认值 2mm → 1mm
#   v3 = 结婚套装"均匀留白" → "固定间隔"（照片间隔滑块在"均匀留白"下不起作用）
SETTINGS_SCHEMA_VERSION = 3

# 设置项：名称 -> (存储类型, 默认值)
SETTINGS_SCHEMA: Dict[str, Tuple[str, object]] = {
    "print_scale": ("scale", DEFAULT_PRINT_SCALE),
    # 照片间隔（通用；结婚套装里作为**上下**间距）
    "spacing_mm": ("float", SPACING_MM),
    # 结婚套装：**左右**两列之间的间距（与上下间距独立）。
    # 9.5mm ≈ 6R 竖版下两列刚好贴边时的那道缝，默认值即"经典版面"。
    "wedding_gap_h_mm": ("float", 9.5),
    "bleed_mm": ("float", BLEED_MM),
    "people": ("int", 1),
    "per_person": ("int", 11),
    "count": ("int", 4),
    "landscape": ("bool", True),
    "mix": ("str", ",".join(str(x) for x in MIX_DEFAULT)),          # 六种尺寸张数
    "mix_people": ("str", ",".join(MIX_PEOPLE_DEFAULT)),            # 六种尺寸用谁
    "count_person": ("str", "p1"),          # 张数类模板用哪个人物
    "guides": ("bool", False),
    "direct_save": ("bool", True),
    # 结婚套装分布方式："even" 均匀留白（自动均分剩余空间）/
    # "tight" 固定间隔（照片间距 = 照片间隔滑块）。
    # 默认 tight —— 否则滑块调小没反应（用户反馈"修改无效"）。
    "distribution": ("str", "tight"),
    "template": ("str", "wedding"),
    # ---- 以下为新增项（不影响既有 9 套模板的任何行为）----
    "schema_version": ("int", SETTINGS_SCHEMA_VERSION),
    # 换背景
    "bg_mode": ("str", "keep"),             # keep / solid / gradient_v / gradient_center
    "bg_color": ("str", "#FFFFFF"),
    "bg_method": ("str", "auto"),           # auto / grabcut / flood
    "bg_strength": ("float", 1.0),
    # 人脸检测
    "face_mode": ("str", "auto"),           # auto / opencv / facepp / none
    "facepp_api_key": ("str", ""),
    "facepp_api_secret": ("str", ""),
    "head_ratio": ("float", 0.60),          # 人脸高度占成片高度的比例
    # 自动排版
    "auto_paper": ("str", DEFAULT_PAPER_KEY),
    "auto_spec": ("str", "one_inch"),
    "auto_count": ("int", 8),
    "auto_person": ("str", "p1"),           # 自动排版用谁的照片
    "auto_rotate": ("bool", True),          # 允许旋转 90° 横躺
    "auto_fill": ("bool", True),            # 自动算间隔把版面排满
    "auto_custom_w_mm": ("float", 25.0),    # 自定义尺寸（毫米）
    "auto_custom_h_mm": ("float", 35.0),
    # 单张导出（考试系统上传）
    "upload_w": ("int", 295),
    "upload_h": ("int", 413),
    "upload_unit": ("str", "px"),           # px / mm / cm
    "upload_max_kb": ("int", 0),            # 0 = 不限制文件体积
    "upload_max_side": ("int", 0),          # 0 = 不限制最长边像素
}


def _split_seq(raw) -> list:
    """把 'a,b,c' / ['a','b'] / '[a, b, c]' 都拆成列表（兼容各种存储形态）。"""
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if isinstance(raw, str):
        cleaned = raw.replace("[", "").replace("]", "").replace("(", "").replace(")", "")
        return [x.strip() for x in cleaned.split(",") if x.strip() != ""]
    return [raw]


def _encode(value, kind: str):
    if kind == "str":
        if isinstance(value, (tuple, list)):
            return ",".join(str(x) for x in value)
        return str(value)
    if kind == "scale":
        return int(round(float(value) * 10000))
    if kind == "float":
        return int(round(float(value) * 1000))
    if kind == "int":
        return int(value)
    if kind == "bool":
        return 1 if value else 0
    return str(value)


def _decode(raw, kind: str, default):
    """还原设置值。兼容旧版直接存浮点的 JSON（未做 ×10000 编码）。"""
    try:
        if kind == "scale":
            v = float(raw)
            return v if isinstance(raw, float) else v / 10000.0
        if kind == "float":
            v = float(raw)
            return v if isinstance(raw, float) else v / 1000.0
        if kind == "int":
            return int(raw)
        if kind == "bool":
            return bool(int(raw))
        if kind == "str":
            if isinstance(raw, (list, tuple)):
                return ",".join(str(x) for x in raw)
            return str(raw)
        return str(raw)
    except Exception:
        return default


def _reg_read() -> Dict[str, object]:
    """从 Windows 注册表读取设置。"""
    try:
        import winreg
    except ImportError:
        return {}
    out: Dict[str, object] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH) as key:
            for name, (kind, default) in SETTINGS_SCHEMA.items():
                try:
                    raw, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                out[name] = _decode(raw, kind, default)
    except OSError:
        return {}
    return out


def _reg_write(data: Dict[str, object]) -> bool:
    """写入 Windows 注册表。"""
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_PATH) as key:
            for name, (kind, _default) in SETTINGS_SCHEMA.items():
                if name not in data:
                    continue
                if kind == "str":
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, _encode(data[name], kind))
                else:
                    winreg.SetValueEx(key, name, 0, winreg.REG_DWORD,
                                      _encode(data[name], kind))
        return True
    except Exception:
        return False


def _json_read() -> Dict[str, object]:
    try:
        with open(APP_SETTINGS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        out = {}
        for name, (kind, default) in SETTINGS_SCHEMA.items():
            if name in raw:
                out[name] = _decode(raw[name], kind, default)
        return out
    except Exception:
        return {}


def _json_write(data: Dict[str, object]) -> bool:
    try:
        os.makedirs(APP_SETTINGS_DIR, exist_ok=True)
        payload = {}
        for k, v in data.items():
            if k not in SETTINGS_SCHEMA:
                continue
            kind = SETTINGS_SCHEMA[k][0]
            # 列表类（mix / mix_people）以逗号串落盘，避免读回时变成 "[0, 3, …]"
            payload[k] = _encode(v, kind) if kind == "str" else v
        with open(APP_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def load_settings() -> Dict[str, object]:
    """读取设置：注册表优先，JSON 兜底，都没有则返回默认值。"""
    data = _json_read()
    data.update(_reg_read())
    for name, (kind, default) in SETTINGS_SCHEMA.items():
        data.setdefault(name, default)
    # 列表类设置统一还原成定长元组（兼容旧版 3 元素）
    data["mix"] = normalize_mix(data.get("mix"))
    data["mix_people"] = normalize_mix_people(data.get("mix_people"))
    # 一次性迁移：旧版默认出血线是 2mm，升级后统一到 1mm（用户可在界面随时改回）
    try:
        version = int(data.get("schema_version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 2:
        data["bleed_mm"] = BLEED_MM
    if version < 3:
        # "均匀留白"下照片间隔滑块不起作用（用户反馈"修改无效"），
        # 升级后默认改成"固定间隔"，滑块与版面一一对应
        data["distribution"] = "tight"
    if version < SETTINGS_SCHEMA_VERSION:
        data["schema_version"] = SETTINGS_SCHEMA_VERSION
        save_settings(data)
    return data


def save_settings(data: Dict[str, object]) -> bool:
    """保存设置：写注册表 + JSON 双保险。返回是否成功。"""
    payload = {k: v for k, v in data.items() if k in SETTINGS_SCHEMA}
    ok_reg = _reg_write(payload)
    ok_json = _json_write(payload)
    return ok_reg or ok_json


def settings_location() -> str:
    """设置实际保存位置的说明（用于界面提示）。"""
    if _reg_write({}):          # 试探性写入，失败说明没有注册表权限
        return f"注册表 HKEY_CURRENT_USER\\{REG_PATH}"
    return f"文件 {APP_SETTINGS_FILE}"


if __name__ == "__main__":  # 简易自检
    for t in TEMPLATES.values():
        print(f"[{t.key}] {t.title}  画布 {t.canvas_size_text}")
    print("设置位置:", settings_location())
    print("默认设置:", load_settings())
