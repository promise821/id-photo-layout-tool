# -*- coding: utf-8 -*-
"""
auto_layout.py —— 自动排版计算器（纯计算，不依赖任何 UI 框架）。

计算顺序（与需求一致）：
    1. 相纸尺寸 → 减去出血线与内缩 → 得到**可用高度、可用宽度**；
    2. 按最小间隔算出这一页的**总限制**（最多能排几张）；
    3. **反算照片间隔宽度**：由剩余空间算出横、纵两个方向的间隔，
       让整版照片正好把可用区排满（不留参差边）；
    4. 若某个朝向排不满（间隔算出来是负数 / 小于最小间隔），
       就**减少一张再重新计算剩余空间**，直到有可行方案；
    5. **先算竖版，再算横版**（照片顺时针旋转 90° 横躺），
       取能放更多张的那个；张数相同时优先竖版。

本模块只产出"方案"，不碰画布；真正落位由 layout_engine 的 _layout_auto 完成。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# 1. 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GridPlan:
    """一种朝向下的排版方案（照片尺寸固定，格数与间隔已算好）。"""

    cols: int                       # 列数
    rows: int                       # 行数
    count: int                      # 实际排入的张数
    gap_x: float                    # 横向照片间隔（px，由剩余空间反算）
    gap_y: float                    # 纵向照片间隔（px，由剩余空间反算）
    photo_w: int                    # 落版单张宽（已含旋转）
    photo_h: int                    # 落版单张高（已含旋转）
    rotated: bool                   # 是否把照片旋转了 90°
    last_row: int                   # 最后一行的张数
    usable_w: int                   # 可用区宽
    usable_h: int                   # 可用区高

    @property
    def capacity(self) -> int:
        """这一页最多能排几张（格数上限）。"""
        return self.cols * self.rows

    @property
    def block_w(self) -> float:
        """整块照片占的宽（照片 + 间隔）。"""
        return self.cols * self.photo_w + (self.cols - 1) * self.gap_x

    @property
    def block_h(self) -> float:
        """整块照片占的高（照片 + 间隔）。"""
        return self.rows * self.photo_h + (self.rows - 1) * self.gap_y

    @property
    def last_row_offset(self) -> float:
        """最后一行不足 cols 张时，相对整行网格的左偏移（居中用）。"""
        if self.last_row >= self.cols or self.last_row <= 0:
            return 0.0
        row_w = self.last_row * self.photo_w + (self.last_row - 1) * self.gap_x
        return max(0.0, (self.block_w - row_w) / 2.0)

    @property
    def gap_mm_text(self) -> str:
        return f"{self.gap_x / 11.811:.1f} × {self.gap_y / 11.811:.1f} mm"


@dataclass
class AutoLayout:
    """自动排版的结果：最终方案 + 两种朝向的中间结果 + 提示。"""

    plan: Optional[GridPlan] = None
    portrait: Optional[GridPlan] = None
    landscape: Optional[GridPlan] = None
    notes: List[str] = None

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []


# ---------------------------------------------------------------------------
# 2. 可用区
# ---------------------------------------------------------------------------
def usable_area(paper_w: int, paper_h: int, bleed: int, margin: int
                ) -> Tuple[int, int]:
    """相纸减去出血线与内缩 → 可用宽、可用高。

    bleed 为出血线内缩量，margin 为照片到出血线的安全距离，两者都左右上下各扣一次。
    """
    cut = 2 * (int(bleed) + int(margin))
    return (int(paper_w) - cut, int(paper_h) - cut)


# ---------------------------------------------------------------------------
# 3. 单个朝向下找最优方案
# ---------------------------------------------------------------------------
def _score(cols: int, rows: int, gap_x: float, gap_y: float,
           w: int, h: int, usable_w: int, usable_h: int) -> Tuple[float, int]:
    """给一个候选方案打分（越小越好）。

    · 主项：整块照片的长宽比与页面长宽比差多少 —— 差得越少，版面越"正"；
    · 次项：横向间隔与纵向间隔差多少 —— 差得越少，留白越均匀。
    两种模式下主次项恰好只有一个起作用，所以可以直接相加：
      · fill=True（排满）时整块尺寸 == 可用区，主项恒为 0，由次项决定；
      · fill=False（固定间隔）时横纵间隔相同，次项恒为 0，由主项决定。
    """
    block_w = cols * w + (cols - 1) * gap_x
    block_h = rows * h + (rows - 1) * gap_y
    if block_w <= 0 or block_h <= 0 or usable_w <= 0 or usable_h <= 0:
        return (float("inf"), -cols)

    page_ratio = usable_w / usable_h
    block_ratio = block_w / block_h
    try:
        primary = abs(math.log(block_ratio / page_ratio))
    except (ValueError, ZeroDivisionError):
        primary = 0.0

    if gap_x > 0 and gap_y > 0:
        secondary = abs(math.log(gap_x / gap_y))
    else:
        secondary = 0.0
    return (primary + secondary, -cols)      # 列数多者优先（-cols 越小越靠前）


def _plan_orientation(count: int, w: int, h: int, usable_w: int, usable_h: int,
                      min_gap: int, fill: bool) -> Optional[GridPlan]:
    """在**一个固定朝向**（w×h 不再变化）下，为 count 张照片找最优方案。

    找不到可行方案返回 None（交给上层"减一张再算"）。
    """
    if count <= 0 or w <= 0 or h <= 0 or usable_w <= 0 or usable_h <= 0:
        return None
    if w > usable_w or h > usable_h:
        return None                       # 单张都放不进去，这个朝向直接出局

    max_cols = max(1, usable_w // w)
    best: Optional[GridPlan] = None
    best_score: Optional[Tuple[float, int]] = None

    for cols in range(1, max_cols + 1):
        rows = (count + cols - 1) // cols
        if rows <= 0:
            continue

        # ---- 横向间隔 ----
        if cols > 1:
            if fill:
                gap_x = (usable_w - cols * w) / (cols - 1)
            else:
                gap_x = float(min_gap)
            if gap_x < min_gap - 1e-9:
                continue
            if not fill and cols * w + (cols - 1) * min_gap > usable_w:
                continue
        else:
            gap_x = 0.0        # 只有一列时本来就没有横向间隔

        # ---- 纵向间隔 ----
        if rows > 1:
            if fill:
                gap_y = (usable_h - rows * h) / (rows - 1)
            else:
                gap_y = float(min_gap)
            if gap_y < min_gap - 1e-9:
                continue
            if not fill and rows * h + (rows - 1) * min_gap > usable_h:
                continue
        else:
            gap_y = 0.0        # 只有一行时本来就没有纵向间隔

        last_row = count - (rows - 1) * cols
        score = _score(cols, rows, gap_x, gap_y, w, h, usable_w, usable_h)
        if best_score is None or score < best_score:
            best_score = score
            best = GridPlan(cols=cols, rows=rows, count=count,
                            gap_x=gap_x, gap_y=gap_y, photo_w=w, photo_h=h,
                            rotated=False, last_row=last_row,
                            usable_w=usable_w, usable_h=usable_h)
    return best


def _plan_orientation_with_rotate(count: int, w: int, h: int, usable_w: int,
                                  usable_h: int, min_gap: int, fill: bool
                                  ) -> Optional[GridPlan]:
    """竖版优先的方案搜索：先按原朝向试，再按旋转 90° 试。"""
    plan = _plan_orientation(count, w, h, usable_w, usable_h, min_gap, fill)
    if plan is not None:
        return plan
    # 原朝向排不下 → 换成旋转 90° 横躺再试
    rotated = _plan_orientation(count, h, w, usable_w, usable_h, min_gap, fill)
    if rotated is None:
        return None
    return GridPlan(cols=rotated.cols, rows=rotated.rows, count=rotated.count,
                    gap_x=rotated.gap_x, gap_y=rotated.gap_y,
                    photo_w=rotated.photo_w, photo_h=rotated.photo_h,
                    rotated=True, last_row=rotated.last_row,
                    usable_w=rotated.usable_w, usable_h=rotated.usable_h)


# ---------------------------------------------------------------------------
# 4. 对外主入口
# ---------------------------------------------------------------------------
def plan_auto_layout(count: int, photo_w: int, photo_h: int,
                     usable_w: int, usable_h: int, min_gap: int = 0,
                     fill: bool = True, allow_rotate: bool = True) -> AutoLayout:
    """算出自动排版方案。

    参数：
        count      想要排多少张；
        photo_w/h  照片**原始朝向**的落版尺寸（竖排时 w<h）；
        usable_w/h 可用区尺寸（相纸 − 出血线 − 内缩）；
        min_gap    最小照片间隔（px），反算出来的间隔不得小于它；
        fill       True = 反算间隔把版面排满；False = 用固定间隔；
        allow_rotate 是否允许把照片旋转 90° 横躺。

    返回的 plan 是最终采用的方案；portrait / landscape 是两种朝向各自的中间结果，
    方便界面上把"竖版能放几张 / 横版能放几张"讲清楚。
    """
    notes: List[str] = []
    photo_w = max(1, int(photo_w))
    photo_h = max(1, int(photo_h))
    min_gap = max(0, int(min_gap))

    if usable_w <= 0 or usable_h <= 0:
        notes.append("相纸减去出血线后没有可用空间，请换更大的相纸或调小出血线。")
        return AutoLayout(None, None, None, notes)

    if photo_w > usable_w or photo_h > usable_h:
        notes.append(
            f"照片 {photo_w}×{photo_h}px 超出可用区 {usable_w}×{usable_h}px，"
            f"单张就放不下；请换小一档规格或换更大的相纸。")
        return AutoLayout(None, None, None, notes)

    # ---- 总限制：这一页最多能排几张（竖版与横版取大）----
    _p_cap, _l_cap, cap = capacity_hint(photo_w, photo_h, usable_w, usable_h, min_gap)
    if count > cap:
        notes.append(f"这一页的总限制是 {cap} 张，已按上限计算。")
        count = cap

    # ---- 竖版 ----
    portrait = _plan_orientation(count, photo_w, photo_h, usable_w, usable_h,
                                  min_gap, fill)
    # ---- 横版（旋转 90°）----
    landscape: Optional[GridPlan] = None
    if allow_rotate:
        landscape = _plan_orientation(count, photo_h, photo_w, usable_w, usable_h,
                                      min_gap, fill)
        if landscape is not None:
            landscape = GridPlan(
                cols=landscape.cols, rows=landscape.rows, count=landscape.count,
                gap_x=landscape.gap_x, gap_y=landscape.gap_y,
                photo_w=landscape.photo_w, photo_h=landscape.photo_h,
                rotated=True, last_row=landscape.last_row,
                usable_w=landscape.usable_w, usable_h=landscape.usable_h)

    # ---- 选方案：能放更多张的优先；一样多则优先竖版 ----
    chosen: Optional[GridPlan] = None
    if portrait is not None and landscape is not None:
        if landscape.count > portrait.count:
            chosen = landscape
        else:
            chosen = portrait
    elif portrait is not None:
        chosen = portrait
    elif landscape is not None:
        chosen = landscape

    # ---- 排不满 → 减少一张，再重新计算剩余空间 ----
    if chosen is None:
        reduced = count
        while reduced >= 1:
            reduced -= 1
            if reduced < 1:
                break
            p = _plan_orientation(reduced, photo_w, photo_h, usable_w, usable_h,
                                  min_gap, fill)
            l = None
            if allow_rotate:
                l = _plan_orientation(reduced, photo_h, photo_w, usable_w, usable_h,
                                      min_gap, fill)
                if l is not None:
                    l = GridPlan(cols=l.cols, rows=l.rows, count=l.count,
                                 gap_x=l.gap_x, gap_y=l.gap_y,
                                 photo_w=l.photo_w, photo_h=l.photo_h,
                                 rotated=True, last_row=l.last_row,
                                 usable_w=l.usable_w, usable_h=l.usable_h)
            if p is not None and (l is None or p.count >= l.count):
                portrait, landscape, chosen = p, l, p
                notes.append(
                    f"{count} 张排不满，已减为 {reduced} 张并重新计算剩余空间。")
                break
            if l is not None and p is None:
                portrait, landscape, chosen = p, l, l
                notes.append(
                    f"{count} 张排不满，已减为 {reduced} 张并重新计算剩余空间。")
                break

    if chosen is None:
        notes.append("这一页连 1 张都排不下，请检查相纸尺寸、出血线与照片间隔。")
        return AutoLayout(None, portrait, landscape, notes)

    # ---- 说明文字 ----
    if chosen.rotated:
        notes.append(
            f"竖版放不下，已改用**横躺（旋转 90°）**："
            f"{chosen.cols} 列 × {chosen.rows} 行，共 {chosen.count} 张。")

    if chosen.count < count:
        notes.append(
            f"相纸最多排 {chosen.count} 张（{chosen.cols} 列 × {chosen.rows} 行），"
            f"比计划的 {count} 张少 {count - chosen.count} 张。")
    if fill:
        notes.append(
            f"已反算照片间隔：横向 {chosen.gap_x:.0f}px / 纵向 {chosen.gap_y:.0f}px"
            f"（{chosen.gap_mm_text}），整版正好排满可用区。")
    else:
        notes.append(
            f"使用固定间隔 {min_gap}px，{chosen.cols} 列 × {chosen.rows} 行，"
            f"共 {chosen.count} 张。")

    return AutoLayout(chosen, portrait, landscape, notes)


def capacity_hint(photo_w: int, photo_h: int, usable_w: int, usable_h: int,
                  min_gap: int = 0) -> Tuple[int, int, int]:
    """快速估算"总限制"：返回 (竖版最多几张, 横版最多几张, 取大)。

    竖版 = (可用宽能放几列) × (可用高能放几行)，横版把宽高对调再算一次。
    """
    min_gap = max(0, int(min_gap))
    def _cap(w: int, h: int) -> int:
        cols = max(1, (usable_w + min_gap) // (w + min_gap))
        rows = max(1, (usable_h + min_gap) // (h + min_gap))
        return cols * rows
    portrait = _cap(photo_w, photo_h)
    landscape = _cap(photo_h, photo_w)
    return (portrait, landscape, max(portrait, landscape))
