# -*- coding: utf-8 -*-
"""
main.py —— 证件照自动排版工具的 PyQt6 界面。

交互要点：
    * 照片池支持一次拖入任意多张图片，系统按图片尺寸自动归类
      （横版 → 双人照，竖版 → 人物1/2/3），类别可一键切换；
    * 每张照片可单独"裁剪"（拖动/缩放裁剪框，固定目标比例）；
    * 模板可切换：结婚套装 / 一寸（多人自动分布）/ 二寸 / 小二寸 / 合并排版；
    * 可自定义照片间隔、出血线、打印校准比例，全部设置持久化（注册表优先）；
    * 右侧预览显示画布、照片辅助线与红色出血线，超出安全区会给出提示。

内存与生命周期注意：
    * PIL Image → QPixmap 统一走 pil_to_qpixmap()，内部用 QImage.copy() 深拷贝，
      切断与 Python bytes 缓冲区的引用，避免 QImage 访问已释放内存；
    * 预览位图只保留一份，缩放结果做缓存，模板切换时主动释放旧 QPixmap。
"""

from __future__ import annotations

import functools
import os
import sys
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image
from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (QColor, QDesktopServices, QDragEnterEvent, QDropEvent, QFont,
                         QImage, QMouseEvent, QPainter, QPen, QPixmap)
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox,
                             QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                             QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                             QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

import face_tools
import layout_engine as engine
import photo_tools
from config import (AUTO_SPEC_KEYS, AUTO_SPEC_LABELS, BLEED, BLEED_MM, BLEED_OPTIONS_MM,
                    CALIB_TARGETS, CATEGORIES, CATEGORY_LABELS, CATEGORY_SHORT,
                    DEFAULT_PAPER_KEY, DEFAULT_PRINT_SCALE, DPI,
                    GAP, MARGIN, MIX_DEFAULT, MIX_KEYS, MIX_LABELS,
                    PAPER_SIZES, PRESET_CUSTOM, PRESET_DEFAULT, PRINT_SCALE_RANGE,
                    SINGLE_CATEGORIES, SIZE_PRESETS, SIZE_PRESET_BY_KEY, SPECS, SPACING_MM,
                    SPACING_OPTIONS_MM, TEMPLATES, Template, load_settings, mm_to_px,
                    normalize_mix, normalize_mix_people, save_settings,
                    settings_location)
from layout_engine import CropBox, PhotoInput

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff", ".jfif"}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def is_image_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def pil_to_qpixmap(image: Image.Image) -> QPixmap:
    """PIL Image → QPixmap（安全、无泄漏）。

    QImage 可以直接引用一段 bytes 缓冲区，但其生命周期不受 Python 控制，
    因此这里用 QImage.copy() 做一次深拷贝，再交给 QPixmap.fromImage()，
    彻底断开与 PIL 内存的引用关系。
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    buffer = image.tobytes()                                   # 紧凑的 RGB 字节流
    qimage = QImage(buffer, image.width, image.height, image.width * 3,
                    QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimage.copy())                     # copy() → 深拷贝，安全


def make_thumbnail(path: str, box: Tuple[int, int]) -> QPixmap:
    """从文件生成缩略图（等比缩放到 box 内）。"""
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail(box, Image.Resampling.LANCZOS)
            return pil_to_qpixmap(im)
    except Exception:
        return QPixmap()


def spec_for_category(category: str):
    """类别 → 照片规格（决定裁剪比例与落版尺寸）。"""
    from config import SPECS
    return SPECS["couple"] if category == "couple" else SPECS["one_inch"]


def combo_index(combo: QComboBox, value: str) -> int:
    """按 itemData 找下标；找不到就返回 0。"""
    for i in range(combo.count()):
        if combo.itemData(i) == value:
            return i
    return 0


def safe_slot(func):
    """槽函数兜底：PyQt6 中槽内抛异常会导致进程 abort，这里统一拦截。

    同时兼容 clicked(bool) 这类带参数的信号（自动裁掉多余实参）。
    """
    import inspect

    try:
        sig = inspect.signature(func)
        max_args = len([p for p in sig.parameters.values()
                        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
    except (TypeError, ValueError):
        max_args = 2

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            if len(args) > max_args:
                args = args[:max_args]
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            parent = args[0] if args and isinstance(args[0], QWidget) else None
            try:
                QMessageBox.critical(parent, "操作失败",
                                     f"{func.__name__} 执行出错：\n{exc}")
            except Exception:
                pass
            return None

    return wrapper


# ---------------------------------------------------------------------------
# 裁剪对话框
# ---------------------------------------------------------------------------
class CropCanvas(QWidget):
    """显示原图 + 固定比例的裁剪框，支持拖动平移、角点缩放、滚轮缩放。"""

    HANDLE = 14          # 角点命中半径（屏幕像素）
    MIN_SIZE = 40        # 裁剪框最小边长（原图像素）

    def __init__(self, image: Image.Image, ratio: float, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(620, 480)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._img_w, self._img_h = image.size
        self._pixmap = pil_to_qpixmap(image)
        self._ratio = ratio
        self._crop = self._default_rect()
        self._scale = 1.0
        self._offset = QPointF(0, 0)
        self._mode: Optional[str] = None
        self._anchor_name: Optional[str] = None
        self._anchor = (0.0, 0.0)
        self._press_point = QPointF()
        self._press_crop = self._crop
        # 只读模式：只展示裁剪框，不接受拖动/缩放（卡片裁剪 = 纯自动人脸）
        self._read_only = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_read_only(self, on: bool) -> None:
        """只读：禁用拖动/角点缩放/滚轮（自动裁剪结果仅供查看）。"""
        self._read_only = bool(on)
        self.setCursor(Qt.CursorShape.ArrowCursor if on else Qt.CursorShape.CrossCursor)
        self.update()

    # -- 几何 ---------------------------------------------------------------
    def _default_rect(self) -> Tuple[float, float, float, float]:
        if self._img_w / self._img_h > self._ratio:
            cw, ch = self._img_h * self._ratio, float(self._img_h)
        else:
            cw, ch = float(self._img_w), self._img_w / self._ratio
        x = (self._img_w - cw) / 2.0
        y = (self._img_h - ch) / 2.0
        return (x, y, x + cw, y + ch)

    def reset(self) -> None:
        self._crop = self._default_rect()
        self.update()

    def set_ratio(self, ratio: float) -> None:
        """改变裁剪框比例（保持中心位置不变）。

        目标尺寸可以在对话框里随时改；比例一变就要重算裁剪框，
        否则会出现「框还是旧比例、和成片比例对不上」的错位。
        """
        if ratio <= 0 or abs(ratio - self._ratio) < 1e-6:
            return
        self._ratio = ratio
        l, t, r, b = self._crop
        cx, cy = (l + r) / 2.0, (t + b) / 2.0
        cw, ch = r - l, b - t
        if cw / ch > ratio:
            cw = ch * ratio
        else:
            ch = cw / ratio
        cw = min(cw, float(self._img_w))
        ch = min(ch, float(self._img_h))
        l = min(max(0.0, cx - cw / 2.0), self._img_w - cw)
        t = min(max(0.0, cy - ch / 2.0), self._img_h - ch)
        self._crop = (l, t, l + cw, t + ch)
        self.update()

    def set_crop_box(self, box: Optional[CropBox]) -> None:
        """外部（如人脸检测算出的结果）直接设置裁剪框。"""
        if not box:
            return
        l, t, r, b = box
        self._crop = (l * self._img_w, t * self._img_h,
                      r * self._img_w, b * self._img_h)
        self.update()

    def crop_box(self) -> CropBox:
        """归一化裁剪框 (l, t, r, b)。"""
        l, t, r, b = self._crop
        return (max(0.0, l / self._img_w), max(0.0, t / self._img_h),
                min(1.0, r / self._img_w), min(1.0, b / self._img_h))

    def _update_transform(self) -> None:
        s = min((self.width() - 32) / self._img_w, (self.height() - 32) / self._img_h)
        self._scale = max(s, 0.01)
        w, h = self._img_w * self._scale, self._img_h * self._scale
        self._offset = QPointF((self.width() - w) / 2.0, (self.height() - h) / 2.0)

    def _img_to_widget(self, x: float, y: float) -> QPointF:
        return QPointF(self._offset.x() + x * self._scale,
                       self._offset.y() + y * self._scale)

    def _widget_to_img(self, p: QPointF) -> QPointF:
        return QPointF((p.x() - self._offset.x()) / self._scale,
                       (p.y() - self._offset.y()) / self._scale)

    def _handle_points(self) -> Dict[str, QPointF]:
        l, t, r, b = self._crop
        return {"tl": self._img_to_widget(l, t), "tr": self._img_to_widget(r, t),
                "bl": self._img_to_widget(l, b), "br": self._img_to_widget(r, b)}

    def _hit_handle(self, p: QPointF) -> Optional[str]:
        for name, pt in self._handle_points().items():
            if (pt.x() - p.x()) ** 2 + (pt.y() - p.y()) ** 2 <= self.HANDLE ** 2:
                return name
        return None

    def _inside_crop(self, ip: QPointF) -> bool:
        l, t, r, b = self._crop
        return l <= ip.x() <= r and t <= ip.y() <= b

    # -- 绘制 ---------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        self._update_transform()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#2b3038"))

        target = QRectF(self._offset.x(), self._offset.y(),
                        self._img_w * self._scale, self._img_h * self._scale)
        painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))

        l, t, r, b = self._crop
        tl = self._img_to_widget(l, t)
        br = self._img_to_widget(r, b)
        crop_rect = QRectF(tl, br)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 130))
        full = QRectF(target)
        painter.drawRect(QRectF(full.left(), full.top(), full.width(),
                                crop_rect.top() - full.top()))
        painter.drawRect(QRectF(full.left(), crop_rect.bottom(), full.width(),
                                full.bottom() - crop_rect.bottom()))
        painter.drawRect(QRectF(full.left(), crop_rect.top(),
                                crop_rect.left() - full.left(), crop_rect.height()))
        painter.drawRect(QRectF(crop_rect.right(), crop_rect.top(),
                                full.right() - crop_rect.right(), crop_rect.height()))

        pen = QPen(QColor(255, 255, 255, 110), 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        for i in (1, 2):
            x = crop_rect.left() + crop_rect.width() * i / 3
            y = crop_rect.top() + crop_rect.height() * i / 3
            painter.drawLine(QPointF(x, crop_rect.top()), QPointF(x, crop_rect.bottom()))
            painter.drawLine(QPointF(crop_rect.left(), y), QPointF(crop_rect.right(), y))

        pen = QPen(QColor("#ffd23f") if not self._read_only else QColor("#7aa7ff"), 2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(crop_rect)

        # 只读模式不画拖拽角点 —— 免得让人以为还能手动调
        if not self._read_only:
            painter.setPen(QPen(QColor("#ffd23f"), 1))
            painter.setBrush(QColor("#ffd23f"))
            for pt in self._handle_points().values():
                painter.drawRect(QRectF(pt.x() - 5, pt.y() - 5, 10, 10))

    # -- 交互 ---------------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._read_only:
            return
        pos = event.position()
        ip = self._widget_to_img(pos)
        if not (0 <= ip.x() <= self._img_w and 0 <= ip.y() <= self._img_h):
            return
        handle = self._hit_handle(pos)
        if handle:
            self._mode = "resize"
            self._anchor_name = {"tl": "br", "tr": "bl", "bl": "tr", "br": "tl"}[handle]
            l, t, r, b = self._crop
            self._anchor = {"tl": (l, t), "tr": (r, t), "bl": (l, b), "br": (r, b)}[self._anchor_name]
        elif self._inside_crop(ip):
            self._mode = "move"
        else:
            return
        self._press_point = ip
        self._press_crop = self._crop
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._mode is None or self._read_only:
            return
        ip = self._widget_to_img(event.position())
        if self._mode == "move":
            dx = ip.x() - self._press_point.x()
            dy = ip.y() - self._press_point.y()
            l, t, r, b = self._press_crop
            l += dx
            t += dy
            l = max(0.0, min(l, self._img_w - (r - self._press_crop[0])))
            t = max(0.0, min(t, self._img_h - (b - self._press_crop[1])))
            self._crop = (l, t, l + (r - self._press_crop[0]), t + (b - self._press_crop[1]))
        else:
            self._resize_to(ip)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._mode = None

    def _resize_to(self, ip: QPointF) -> None:
        if self._anchor_name is None:
            return
        ax, ay = self._anchor
        w = abs(ip.x() - ax)
        h = abs(ip.y() - ay)
        if w < 1 or h < 1:
            return
        if w / h > self._ratio:
            w = h * self._ratio
        else:
            h = w / self._ratio
        name = self._anchor_name
        if name == "tl":
            max_w, max_h = self._img_w - ax, self._img_h - ay
        elif name == "tr":
            max_w, max_h = ax, self._img_h - ay
        elif name == "bl":
            max_w, max_h = self._img_w - ax, ay
        else:
            max_w, max_h = ax, ay
        max_w = max(max_w, self.MIN_SIZE)
        max_h = max(max_h, self.MIN_SIZE)
        if w > max_w:
            w = max_w
            h = w / self._ratio
        if h > max_h:
            h = max_h
            w = h / self._ratio
        w = max(w, self.MIN_SIZE)
        h = w / self._ratio
        if name == "tl":
            self._crop = (ax, ay, ax + w, ay + h)
        elif name == "tr":
            self._crop = (ax - w, ay, ax, ay + h)
        elif name == "bl":
            self._crop = (ax, ay - h, ax + w, ay)
        else:
            self._crop = (ax - w, ay - h, ax, ay)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._read_only:
            return
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        l, t, r, b = self._crop
        w = (r - l) * factor
        h = (b - t) * factor
        if w > self._img_w:
            w, h = float(self._img_w), self._img_w / self._ratio
        if h > self._img_h:
            w, h = self._img_h * self._ratio, float(self._img_h)
        w = max(w, self.MIN_SIZE)
        h = w / self._ratio
        l = max(0.0, min(l, self._img_w - w))
        t = max(0.0, min(t, self._img_h - h))
        self._crop = (l, t, l + w, t + h)
        self.update()


# ---------------------------------------------------------------------------
# 统一裁剪工具对话框
# ---------------------------------------------------------------------------
@dataclass
class CropSettings:
    """裁剪对话框回传的全部设置。"""

    preset_key: str = PRESET_DEFAULT       # "跟随类别默认规格"
    width_px: int = 295
    height_px: int = 413
    unit: str = "px"
    max_kb: int = 0
    max_side: int = 0
    face_mode: str = "auto"
    head_ratio: float = 0.60
    api_key: str = ""
    api_secret: str = ""


def size_to_px(w: float, h: float, unit: str) -> Tuple[int, int]:
    """按单位把尺寸换算成像素（1cm = 300/2.54 px）。"""
    if unit == "mm":
        return (int(round(mm_to_px(w))), int(round(mm_to_px(h))))
    if unit == "cm":
        return (int(round(mm_to_px(w * 10.0))), int(round(mm_to_px(h * 10.0))))
    return (int(round(w)), int(round(h)))


def size_from_px(px: int, unit: str) -> float:
    """像素 → 指定单位（回填对话框时用）。"""
    if unit == "mm":
        return float(px) / 11.8110236
    if unit == "cm":
        return float(px) / 118.110236
    return float(px)


class CropToolDialog(QDialog):
    """统一裁剪对话框，按 mode 分三条路：

    MODE_CUSTOM（自定义裁剪）
        顶部「自定义裁剪」按钮进来。画布可自由拖动缩放，可改目标尺寸、
        体积/最长边限制，有「预览成片」+「导出单张」；**不写回照片的
        裁剪框**（自由裁剪只用于出片，不参与模板排版）。
    MODE_TEMPLATE（图片列表裁剪）
        照片卡片上的「裁剪 / 自动」进来。不能自由拖动（画布只读），
        没有尺寸修改与体积限制，只保留人脸检测；「应用裁剪」把框
        **放回模板**参与排版。
    MODE_SETTINGS（尺寸/限制设置）
        左栏入口进来。没有画布，只设置尺寸、限制与检测参数。
    """

    MODE_CUSTOM = "custom"
    MODE_TEMPLATE = "template"
    MODE_SETTINGS = "settings"

    UNITS = (("px", "像素 px"), ("mm", "毫米 mm"), ("cm", "厘米 cm"))

    exportRequested = pyqtSignal(object)

    def __init__(self, image: Optional[Image.Image], ratio: float,
                 title: str = "裁剪照片",
                 preset_key: str = PRESET_DEFAULT,
                 width_px: int = 295, height_px: int = 413, unit: str = "px",
                 max_kb: int = 0, max_side: int = 0,
                 face_mode: str = "auto", head_ratio: float = 0.60,
                 api_key: str = "", api_secret: str = "",
                 auto_detect: bool = False,
                 initial_crop: Optional[CropBox] = None,
                 parent: Optional[QWidget] = None,
                 mode: str = MODE_TEMPLATE) -> None:
        super().__init__(parent)
        self._mode = str(mode or self.MODE_TEMPLATE).strip().lower()
        if self._mode not in (self.MODE_CUSTOM, self.MODE_TEMPLATE, self.MODE_SETTINGS):
            self._mode = self.MODE_TEMPLATE
        titles = {
            self.MODE_CUSTOM: "自定义裁剪 · 自由构图并导出单张",
            self.MODE_TEMPLATE: "图片裁剪 · 自动识别人脸后放回排版",
            self.MODE_SETTINGS: "尺寸 / 限制设置",
        }
        self.setWindowTitle(title or titles.get(self._mode, "裁剪照片"))
        self.setModal(True)
        self.resize(1000, 820 if self._mode != self.MODE_SETTINGS else 420)
        self._image = image
        self._ratio = ratio
        self._auto_detect = auto_detect
        self._detected = False
        self._face_box: Optional[Tuple[int, int, int, int]] = None

        layout = QVBoxLayout(self)

        has_canvas = image is not None and self._mode != self.MODE_SETTINGS
        if has_canvas:
            if self._mode == self.MODE_CUSTOM:
                hint = QLabel("自由裁剪：拖动裁剪框调整位置，拖动四角缩放，滚轮放大/缩小。"
                              "改下面的目标尺寸会同步改变裁剪比例。"
                              "确认无误后点「导出单张」出片（不会改动照片在排版里的裁剪框）。")
            else:
                hint = QLabel("图片列表裁剪：按目标比例自动识别人脸并定位裁剪框，"
                              "这里不能自由拖动（需要自由构图请用顶部的「自定义裁剪」）。"
                              "点「应用裁剪」把裁剪框放回模板参与排版。")
            hint.setObjectName("SlotHint")
            hint.setWordWrap(True)
            layout.addWidget(hint)
            self.canvas = CropCanvas(image, ratio)
            self.canvas.set_read_only(self._mode != self.MODE_CUSTOM)
            layout.addWidget(self.canvas, 1)
        else:
            self.canvas = None
            if self._mode == self.MODE_SETTINGS:
                tip = QLabel("这里只设置尺寸、体积/最长边限制与人脸检测参数，"
                             "不针对具体照片。照片卡片上的「裁剪 / 自动」才是按单张照片裁剪。")
            else:
                tip = QLabel("当前没有选中照片，这里只能设置尺寸与检测参数。"
                             "在照片卡片上点「裁剪」可以针对单张照片调整构图。")
            tip.setObjectName("SlotHint")
            tip.setWordWrap(True)
            layout.addWidget(tip)
            layout.addStretch(1)

        # ---- 设置区：左边目标尺寸，右边检测 ----
        panel = QHBoxLayout()

        size_box = QGroupBox("目标尺寸")
        size_form = QFormLayout(size_box)
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("跟随类别默认规格", PRESET_DEFAULT)
        for preset in SIZE_PRESETS:
            self.preset_combo.addItem(f"{preset.group} · {preset.label}", preset.key)
        self.preset_combo.addItem("自定义", PRESET_CUSTOM)
        idx = next((i for i in range(self.preset_combo.count())
                    if self.preset_combo.itemData(i) == preset_key), 0)
        self.preset_combo.setCurrentIndex(idx)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        size_form.addRow("尺寸预设", self.preset_combo)

        self.unit_combo = QComboBox()
        for key, label in self.UNITS:
            self.unit_combo.addItem(label, key)
        self._unit_before = unit          # 换算时要用「切换前」的单位
        self.unit_combo.setCurrentIndex(
            next((i for i, (k, _) in enumerate(self.UNITS) if k == unit), 0))
        self.unit_combo.currentIndexChanged.connect(self._on_unit_changed)
        self._lbl_unit = QLabel("单位")
        self._lbl_unit.setObjectName("FieldLabel")
        size_form.addRow(self._lbl_unit, self.unit_combo)

        self.w_spin = QDoubleSpinBox()
        self.w_spin.setRange(1, 10000)
        self.w_spin.setDecimals(1)
        self.w_spin.setSingleStep(1)
        self.w_spin.setValue(float(size_from_px(width_px, unit)))
        self.w_spin.valueChanged.connect(self._on_size_changed)
        self._lbl_w = QLabel("宽度")
        self._lbl_w.setObjectName("FieldLabel")
        size_form.addRow(self._lbl_w, self.w_spin)

        self.h_spin = QDoubleSpinBox()
        self.h_spin.setRange(1, 10000)
        self.h_spin.setDecimals(1)
        self.h_spin.setSingleStep(1)
        self.h_spin.setValue(float(size_from_px(height_px, unit)))
        self.h_spin.valueChanged.connect(self._on_size_changed)
        self._lbl_h = QLabel("高度")
        self._lbl_h.setObjectName("FieldLabel")
        size_form.addRow(self._lbl_h, self.h_spin)

        # 图片列表裁剪不改尺寸、不设限制 → 这两行只在 custom/settings 出现
        self.maxkb_spin = QDoubleSpinBox()
        self.maxkb_spin.setRange(0, 20480)
        self.maxkb_spin.setDecimals(0)
        self.maxkb_spin.setSpecialValueText("不限制")
        self.maxkb_spin.setSuffix(" KB")
        self.maxkb_spin.setValue(float(max_kb))
        self._lbl_maxkb = QLabel("文件体积上限")
        self._lbl_maxkb.setObjectName("FieldLabel")
        size_form.addRow(self._lbl_maxkb, self.maxkb_spin)

        self.maxside_spin = QDoubleSpinBox()
        self.maxside_spin.setRange(0, 20000)
        self.maxside_spin.setDecimals(0)
        self.maxside_spin.setSpecialValueText("不限制")
        self.maxside_spin.setSuffix(" px")
        self.maxside_spin.setValue(float(max_side))
        self._lbl_maxside = QLabel("最长边像素上限")
        self._lbl_maxside.setObjectName("FieldLabel")
        size_form.addRow(self._lbl_maxside, self.maxside_spin)
        panel.addWidget(size_box, 1)

        face_box = QGroupBox("人脸检测（自动裁剪）")
        face_form = QFormLayout(face_box)
        self.face_mode_combo = QComboBox()
        for key, label in (("auto", "自动"), ("opencv", "本地 OpenCV"),
                           ("facepp", "Face++ 云端"), ("none", "不检测")):
            self.face_mode_combo.addItem(label, key)
        self.face_mode_combo.setCurrentIndex(
            combo_index(self.face_mode_combo, face_mode))
        face_form.addRow("检测方式", self.face_mode_combo)

        self.head_ratio_spin = QDoubleSpinBox()
        self.head_ratio_spin.setRange(0.30, 0.90)
        self.head_ratio_spin.setDecimals(2)
        self.head_ratio_spin.setSingleStep(0.01)
        self.head_ratio_spin.setValue(float(head_ratio))
        self.head_ratio_spin.setToolTip("人脸高度占成片高度的比例，证件照一般 0.55~0.65")
        face_form.addRow("头部占比", self.head_ratio_spin)

        self.key_edit = QLineEdit(api_key)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Face++ API Key")
        face_form.addRow("API Key", self.key_edit)

        self.secret_edit = QLineEdit(api_secret)
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setPlaceholderText("Face++ API Secret")
        face_form.addRow("API Secret", self.secret_edit)

        show = QCheckBox("显示密钥")
        show.toggled.connect(self._toggle_echo)
        face_form.addRow("", show)
        face_form.addRow("说明", QLabel("不填则用本地 OpenCV 离线检测，\n照片不会上传到任何服务器。"))
        panel.addWidget(face_box, 1)
        layout.addLayout(panel)

        self.info_label = QLabel("")
        self.info_label.setObjectName("SlotHint")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        # 自定义裁剪的「成片预览」：把裁剪框内的图按目标尺寸（含限制）算出来
        self.preview_label = QLabel("")
        self.preview_label.setObjectName("Thumb")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(120)
        self.preview_label.setVisible(False)
        layout.addWidget(self.preview_label)

        # ---- 按钮 ----
        row = QHBoxLayout()
        self._reset_btn = QPushButton("重置为居中")
        self._reset_btn.clicked.connect(self._reset_canvas)
        row.addWidget(self._reset_btn)

        detect_btn = QPushButton("自动检测人脸")
        detect_btn.setToolTip("按上面的人脸检测设置定位人脸并自动算出裁剪框")
        detect_btn.clicked.connect(self.run_auto_detect)
        row.addWidget(detect_btn)

        self.preview_btn = QPushButton("预览成片")
        self.preview_btn.setToolTip("按当前裁剪框 + 目标尺寸（含体积/最长边限制）算出成片效果")
        self.preview_btn.clicked.connect(self._show_preview)
        row.addWidget(self.preview_btn)

        self.export_btn = QPushButton("导出单张…")
        self.export_btn.setToolTip("把当前裁剪结果按设定尺寸导出成独立文件；"
                                   "有体积/最长边限制时会在限制内尽量放大")
        self.export_btn.clicked.connect(self._emit_export)
        row.addWidget(self.export_btn)

        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        ok_text = ("确认裁剪" if self._mode == self.MODE_CUSTOM
                   else "应用裁剪" if self._mode == self.MODE_TEMPLATE
                   else "保存设置")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(ok_text)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

        self._apply_mode_visibility()
        self._refresh()
        # 之前裁过的照片，把原来的裁剪框带进来（避免一点「应用」就回到居中）
        if initial_crop is not None and self.canvas is not None:
            self.canvas.set_crop_box(initial_crop)
        if auto_detect and image is not None:
            self.run_auto_detect()

    # -- 模式差异 -------------------------------------------------------------
    def _apply_mode_visibility(self) -> None:
        """按 mode 隐藏不相关的控件。

        · template：不改尺寸、不设体积限制 → 只留预设比例 + 检测参数，
          不给「预览成片 / 导出单张」（裁剪框是给排版用的，不是出片）。
        · settings：连画布都没有，只留尺寸/限制/检测。
        """
        template_like = self._mode in (self.MODE_TEMPLATE,)
        # 尺寸编辑：template 不能改（比例由类别规格决定）
        for widget, lbl in ((self.w_spin, self._lbl_w), (self.h_spin, self._lbl_h),
                            (self.unit_combo, self._lbl_unit)):
            widget.setVisible(not template_like)
            lbl.setVisible(not template_like)
        # 体积/最长边限制：template 不给设
        self.maxkb_spin.setVisible(not template_like)
        self._lbl_maxkb.setVisible(not template_like)
        self.maxside_spin.setVisible(not template_like)
        self._lbl_maxside.setVisible(not template_like)
        # 自定义裁剪必须能选预设；template 也可换规格（比例会跟着变）
        self.preset_combo.setVisible(True)
        # 导出/预览只在自定义裁剪里出现
        self.preview_btn.setVisible(self._mode == self.MODE_CUSTOM and self._image is not None)
        self.export_btn.setVisible(self._mode == self.MODE_CUSTOM and self._image is not None)
        # 没有画布就没什么可「重置为居中」的
        self._reset_btn.setVisible(self.canvas is not None)
        self.preview_label.setVisible(False)

    # -- 内部 ---------------------------------------------------------------
    def _toggle_echo(self, on: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
        self.key_edit.setEchoMode(mode)
        self.secret_edit.setEchoMode(mode)

    def _reset_canvas(self) -> None:
        if self.canvas is not None:
            self.canvas.reset()

    def _current_preset(self) -> str:
        return str(self.preset_combo.currentData() or PRESET_DEFAULT)

    def _on_preset_changed(self, _index: int) -> None:
        key = self._current_preset()
        if key not in (PRESET_DEFAULT, PRESET_CUSTOM):
            preset = SIZE_PRESET_BY_KEY.get(key)
            if preset is not None:
                self._set_unit("px")
                self.w_spin.setValue(float(preset.width_px))
                self.h_spin.setValue(float(preset.height_px))
        self._refresh()

    def _on_unit_changed(self, _index: int) -> None:
        """换单位时把当前数值按原单位换算过去，别让尺寸凭空跳变。"""
        unit = str(self.unit_combo.currentData() or "px")
        w, h = size_to_px(self.w_spin.value(), self.h_spin.value(),
                          self._unit_before)
        self.w_spin.setValue(size_from_px(w, unit))
        self.h_spin.setValue(size_from_px(h, unit))
        self._unit_before = unit
        self._refresh()

    def _set_unit(self, unit: str) -> None:
        self._unit_before = str(self.unit_combo.currentData() or "px")
        idx = next((i for i, (k, _) in enumerate(self.UNITS) if k == unit), 0)
        if idx != self.unit_combo.currentIndex():
            self.unit_combo.setCurrentIndex(idx)   # 会触发 _on_unit_changed
        else:
            self._unit_before = unit

    def _on_size_changed(self, _value: float) -> None:
        self._refresh()

    def _refresh(self) -> None:
        """刷新换算结果，并把目标比例同步到裁剪画布。"""
        unit = str(self.unit_combo.currentData() or "px")
        px_w, px_h = size_to_px(self.w_spin.value(), self.h_spin.value(), unit)
        px_w = max(1, px_w)
        px_h = max(1, px_h)
        ratio = px_w / px_h
        self._ratio = ratio
        if self.canvas is not None:
            self.canvas.set_ratio(ratio)
        text = (f"{px_w} × {px_h} px　宽高比 {ratio:.4f}　"
                f"（{px_w / 11.811:.2f} × {px_h / 11.811:.2f} cm @300DPI）")
        if self._current_preset() == PRESET_DEFAULT:
            text += "　· 当前跟随类别默认规格，排版仍按模板规格放置"
        self.info_label.setText(text)

    # -- 人脸检测 -----------------------------------------------------------
    def run_auto_detect(self) -> None:
        """检测人脸并把算出来的裁剪框画到画布上（之后仍可手动调整）。"""
        if self.canvas is None or self._image is None:
            return
        mode = str(self.face_mode_combo.currentData() or "auto")
        try:
            box, reason = face_tools.detect_face(
                self._image, mode=mode,
                api_key=self.key_edit.text().strip(),
                api_secret=self.secret_edit.text().strip())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "检测失败", f"人脸检测出错：\n{exc}")
            return
        if box is None:
            QMessageBox.information(
                self, "未检测到人脸",
                f"{reason}\n\n可以改用「重置为居中」后手动拖动裁剪框，"
                f"或在上方切换检测方式。")
            return
        # 记住人脸框：换背景抠图要用（人物保护椭圆）
        self._face_box = (box.left, box.top, box.width, box.height)
        crop = face_tools.face_box_to_crop(
            box, self._image.size, self._ratio,
            head_ratio=self.head_ratio_spin.value())
        note = "已生成自动裁剪框"
        try:
            alpha = photo_tools.neural_alpha(self._image, face_box=self._face_box)
            subject = face_tools._subject_bbox(alpha)
            if subject is not None:
                crop = face_tools._refine_crop_with_subject(
                    self._image.size, crop, self._ratio, subject)
                note = "已生成自动裁剪框（已按人像留白校正）"
        except Exception:  # noqa: BLE001
            pass
        self.canvas.set_crop_box(crop)
        self._detected = True
        self.info_label.setText(f"{reason}　·　已自动算出裁剪框，可继续手动调整")

    # -- 成片预览 / 导出（仅自定义裁剪）--------------------------------------
    def _target_px(self) -> Tuple[int, int]:
        """目标像素尺寸（应用最长边限制）。"""
        unit = str(self.unit_combo.currentData() or "px")
        px_w, px_h = size_to_px(self.w_spin.value(), self.h_spin.value(), unit)
        px_w, px_h = max(1, px_w), max(1, px_h)
        max_side = int(self.maxside_spin.value())
        if max_side > 0 and max(px_w, px_h) > max_side:
            k = max_side / float(max(px_w, px_h))
            px_w = max(1, int(round(px_w * k)))
            px_h = max(1, int(round(px_h * k)))
        return px_w, px_h

    def _cropped(self) -> Optional[Image.Image]:
        """按当前裁剪框切出成片（原图不修改）。"""
        if self._image is None or self.canvas is None:
            return None
        box = self.canvas.crop_box()
        w0, h0 = self._image.size
        x1 = max(0, min(w0 - 1, int(round(box[0] * w0))))
        y1 = max(0, min(h0 - 1, int(round(box[1] * h0))))
        x2 = max(x1 + 1, min(w0, int(round(box[2] * w0))))
        y2 = max(y1 + 1, min(h0, int(round(box[3] * h0))))
        return self._image.crop((x1, y1, x2, y2))

    def _show_preview(self) -> None:
        """把裁剪结果缩到目标尺寸显示出来（含体积/最长边限制）。"""
        cropped = self._cropped()
        if cropped is None:
            return
        px_w, px_h = self._target_px()
        max_kb = int(self.maxkb_spin.value())
        try:
            fitted = cropped.resize((px_w, px_h), Image.Resampling.LANCZOS)
            detail = f"{px_w}×{px_h} px"
            if max_kb > 0:
                _, quality, scale = photo_tools.fit_jpeg(fitted, max_kb=max_kb)
                if scale < 1.0:
                    w2 = max(1, int(round(px_w * scale)))
                    h2 = max(1, int(round(px_h * scale)))
                    fitted = fitted.resize((w2, h2), Image.Resampling.LANCZOS)
                    detail += f" → 体积超限，缩到 {w2}×{h2} px（quality={quality}）"
                else:
                    detail += f" ≤{max_kb}KB（quality={quality}，尺寸不变）"
            self.preview_label.setText(detail)
            thumb = fitted.copy()
            thumb.thumbnail(QSize(560, 300), Image.Resampling.LANCZOS)
            self.preview_label.setPixmap(pil_to_qpixmap(thumb))
            self.preview_label.setVisible(True)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "预览失败", f"算成片时出错：\n{exc}")

    # -- 输出 ---------------------------------------------------------------
    def _emit_export(self) -> None:
        self.exportRequested.emit(self.export_payload())

    def export_payload(self) -> dict:
        """自定义裁剪点「导出单张」时回传给主窗口的数据。"""
        unit = str(self.unit_combo.currentData() or "px")
        px_w, px_h = self._target_px()
        return {
            "image": self._image,
            "crop": (self.canvas.crop_box() if self.canvas is not None else None),
            "width_px": px_w,
            "height_px": px_h,
            "max_kb": int(self.maxkb_spin.value()),
            "face_box": getattr(self, "_face_box", None),
        }

    def settings(self) -> CropSettings:
        unit = str(self.unit_combo.currentData() or "px")
        px_w, px_h = size_to_px(self.w_spin.value(), self.h_spin.value(), unit)
        return CropSettings(
            preset_key=self._current_preset(),
            width_px=max(1, px_w),
            height_px=max(1, px_h),
            unit=unit,
            max_kb=int(self.maxkb_spin.value()),
            max_side=int(self.maxside_spin.value()),
            face_mode=str(self.face_mode_combo.currentData() or "auto"),
            head_ratio=float(self.head_ratio_spin.value()),
            api_key=self.key_edit.text().strip(),
            api_secret=self.secret_edit.text().strip(),
        )

    def crop_box(self) -> Optional[CropBox]:
        if self.canvas is None:
            return None
        return self.canvas.crop_box()

    def face_box(self) -> Optional[Tuple[int, int, int, int]]:
        """最近一次自动检测到的人脸框（原图像素）；没检测过返回 None。"""
        return self._face_box


# ---------------------------------------------------------------------------
# 打印比例校准对话框
# ---------------------------------------------------------------------------
class CalibrationDialog(QDialog):
    """按实测尺寸自动算出补偿系数。"""

    def __init__(self, current_scale: float, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("校准打印比例")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "冲印机通常把整张图像缩放到「可打印区域」（6R 相纸实际只印约 9.6×14.4cm），\n"
            "所以成品比设计尺寸小一圈。先打印一张样片，量出下面的实际尺寸，\n"
            "工具会自动算出补偿系数并套用到所有照片上。")
        intro.setObjectName("SlotHint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.target_combo = QComboBox()
        for t in CALIB_TARGETS:
            self.target_combo.addItem(f"{t.label}（标称 {t.nominal_cm:g} cm）", t.key)
        self.target_combo.currentIndexChanged.connect(self._refresh)
        form.addRow("校准基准", self.target_combo)

        self.measured = QDoubleSpinBox()
        self.measured.setRange(0.5, 60.0)
        self.measured.setDecimals(3)
        self.measured.setSingleStep(0.1)
        self.measured.setSuffix(" cm")
        self.measured.setValue(9.6)
        self.measured.valueChanged.connect(self._refresh)
        form.addRow("实际量得", self.measured)

        self.nominal_label = QLabel("")
        self.nominal_label.setObjectName("SlotHint")
        form.addRow("标称值", self.nominal_label)

        self.factor_label = QLabel("")
        self.factor_label.setObjectName("SlotTitle")
        form.addRow("补偿系数", self.factor_label)

        self.delta_label = QLabel("")
        self.delta_label.setObjectName("SlotHint")
        form.addRow("当前系数", self.delta_label)
        layout.addLayout(form)

        notice = QLabel("提示：量相纸外沿最准；量照片时请量同一张的同一边，误差取整毫米即可。")
        notice.setObjectName("SlotHint")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用并保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._current = current_scale
        self._refresh()

    def _target(self):
        key = self.target_combo.currentData()
        for t in CALIB_TARGETS:
            if t.key == key:
                return t
        return CALIB_TARGETS[0]

    def _refresh(self) -> None:
        t = self._target()
        measured = self.measured.value()
        self.nominal_label.setText(f"{t.nominal_cm:g} cm　{t.hint}")
        factor = (t.nominal_cm / measured) if measured > 0 else 1.0
        factor = max(PRINT_SCALE_RANGE[0], min(PRINT_SCALE_RANGE[1], factor))
        self.factor_label.setText(f"× {factor:.4f}")
        delta = (factor - self._current) * 100
        self.delta_label.setText(f"× {self._current:.4f}　→　{'+' if delta >= 0 else ''}{delta:.1f}%")

    def factor(self) -> float:
        t = self._target()
        measured = self.measured.value()
        factor = (t.nominal_cm / measured) if measured > 0 else self._current
        return max(PRINT_SCALE_RANGE[0], min(PRINT_SCALE_RANGE[1], factor))


# ---------------------------------------------------------------------------
# 照片池 / 照片卡片
# ---------------------------------------------------------------------------
@dataclass
class PhotoItem:
    path: str
    category: str
    size: Tuple[int, int]
    crop: Optional[CropBox] = None
    thumb: Optional[QPixmap] = None
    face_box: Optional[Tuple[int, int, int, int]] = None    # 检测到的人脸框（原图像素）
    # 该照片单独指定的成片尺寸（像素）。None = 跟随类别默认规格 / 全局上传尺寸。
    # 在裁剪对话框里选「自定义尺寸」就等于覆盖原本要求的尺寸。
    target_size: Optional[Tuple[int, int]] = None
    # 该照片单独指定的背景（卡片上的「背景」按钮）。None/keep = 跟随原图，
    # 不设为 keep 时这张照片换底，排版里可以混排不同底色的照片。
    bg_mode: str = "keep"
    bg_color: str = "#FFFFFF"
    bg_method: str = "auto"

    @property
    def has_custom_bg(self) -> bool:
        return self.bg_mode not in ("", "keep")


class PhotoPool(QFrame):
    """照片投放区：可一次拖入多张图片，系统按尺寸自动归类。"""

    filesDropped = pyqtSignal(list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("PhotoPool")
        self.setAcceptDrops(True)
        self.setMinimumHeight(92)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)
        self.title = QLabel("把照片拖到这里（可一次拖入多张）")
        self.title.setObjectName("PoolTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title)
        self.hint = QLabel("系统按图片尺寸自动判断：横版 → 双人照，竖版 → 人物1/2/3（可点选切换）")
        self.hint.setObjectName("SlotHint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

    def set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.set_drag_active(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.set_drag_active(False)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self.set_drag_active(False)
        files = []
        for url in event.mimeData().urls():
            if url.isLocalFile() and is_image_file(url.toLocalFile()):
                files.append(url.toLocalFile())
        if files:
            self.filesDropped.emit(files)
            event.acceptProposedAction()
        else:
            event.ignore()


class CardArea(QScrollArea):
    """照片列表滚动区：照片存在时拖入框会隐藏，这里要能直接接收拖放。"""

    filesDropped = pyqtSignal(list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragActive(False)

    def setDragActive(self, active: bool) -> None:
        self._drag_active = bool(active)
        self.viewport().setStyleSheet(
            "background:#e8f0fe; border:1px dashed #4f86e0;" if active else "")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setDragActive(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.setDragActive(False)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self.setDragActive(False)
        files = []
        for url in event.mimeData().urls():
            if url.isLocalFile() and is_image_file(url.toLocalFile()):
                files.append(url.toLocalFile())
        if files:
            self.filesDropped.emit(files)
            event.acceptProposedAction()
        else:
            event.ignore()


class PhotoBgDialog(QDialog):
    """单张照片的换背景设置：模式 + 颜色 + 抠图方式。

    每张照片可以各设各的底色，排版时就能混排蓝底/白底/红底。
    """

    MODES = (("keep", "保留原背景"), ("solid", "纯色"),
             ("gradient_v", "上下渐变"), ("gradient_center", "中心渐变"))
    METHODS = (("auto", "AI 抠图（推荐）"), ("neural", "神经网络"),
               ("grabcut", "GrabCut 精修"), ("flood", "漫水快速"))

    def __init__(self, item: PhotoItem, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("替换背景色")
        self.setModal(True)
        self.setMinimumWidth(420)
        self._item = item

        layout = QVBoxLayout(self)
        intro = QLabel(f"为这张照片单独设置背景，排版时可与其他照片的不同底色混排。")
        intro.setObjectName("SlotHint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.mode_combo = QComboBox()
        for key, label in self.MODES:
            self.mode_combo.addItem(label, key)
        self.mode_combo.setCurrentIndex(
            combo_index(self.mode_combo, item.bg_mode))
        self.mode_combo.currentIndexChanged.connect(self._refresh)
        form.addRow("背景模式", self.mode_combo)

        self.color_btn = QPushButton(item.bg_color or "#FFFFFF")
        self.color_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.color_btn.clicked.connect(self._pick_color)
        form.addRow("颜色", self.color_btn)

        self.method_combo = QComboBox()
        for key, label in self.METHODS:
            self.method_combo.addItem(label, key)
        self.method_combo.setCurrentIndex(
            combo_index(self.method_combo, item.bg_method))
        self.method_combo.setToolTip(
            "AI 抠图 = MODNet 神经网络（需 onnxruntime，缺失自动退回 GrabCut）")
        form.addRow("抠图方式", self.method_combo)
        layout.addLayout(form)

        self.sample = QLabel("")
        self.sample.setObjectName("Thumb")
        self.sample.setFixedSize(160, 210)
        self.sample.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.sample)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用到这张照片")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._color = item.bg_color or "#FFFFFF"
        self._refresh()

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._color), self, "选择背景颜色")
        if not color.isValid():
            return
        self._color = photo_tools.rgb_to_hex((color.red(), color.green(), color.blue()))
        self.color_btn.setText(self._color)
        self._refresh()

    def _refresh(self, *_args) -> None:
        mode = str(self.mode_combo.currentData() or "keep")
        self.color_btn.setEnabled(mode != "keep")
        self.method_combo.setEnabled(mode != "keep")
        self._render_preview(mode)

    def _render_preview(self, mode: str) -> None:
        try:
            if mode == "keep":
                img = engine.load_image(self._item.path)
            else:
                img = photo_tools.replace_background(
                    engine.load_image(self._item.path),
                    color=photo_tools.hex_to_rgb(self._color),
                    mode=mode,
                    face_box=self._item.face_box,
                    method=str(self.method_combo.currentData() or "auto"))
            thumb = img.copy()
            thumb.thumbnail(QSize(160, 210), Image.Resampling.LANCZOS)
            self.sample.setPixmap(pil_to_qpixmap(thumb))
        except Exception:  # noqa: BLE001
            self.sample.setText("预览不可用")

    def apply_to(self, item: PhotoItem) -> None:
        item.bg_mode = str(self.mode_combo.currentData() or "keep")
        item.bg_color = self._color
        item.bg_method = str(self.method_combo.currentData() or "auto")


class PhotoCard(QFrame):
    """一张照片的卡片：缩略图 + 尺寸 + 类别切换 + 裁剪 + 换背景 + 删除。"""

    categoryChanged = pyqtSignal(str, str)
    cropRequested = pyqtSignal(str)
    autoCropRequested = pyqtSignal(str)
    removeRequested = pyqtSignal(str)
    bgRequested = pyqtSignal(str)

    def __init__(self, item: PhotoItem, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("PhotoCard")
        self.item = item
        self.setFixedHeight(76)

        grid = QGridLayout(self)
        grid.setContentsMargins(8, 5, 8, 5)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(2)

        self.thumb = QLabel()
        self.thumb.setFixedSize(84, 62)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setObjectName("Thumb")
        if item.thumb is not None:
            self.thumb.setPixmap(item.thumb)
        grid.addWidget(self.thumb, 0, 0, 2, 1)

        # 文件名太长就中间省略（完整路径放 tooltip），别把右侧按钮挤没
        base = os.path.basename(item.path)
        fm = self.fontMetrics()
        name = QLabel(fm.elidedText(base, Qt.TextElideMode.ElideMiddle, 150))
        name.setObjectName("CardName")
        name.setToolTip(item.path)
        grid.addWidget(name, 0, 1)

        marks = []
        if item.crop:
            marks.append("已裁剪")
        if item.target_size:
            marks.append(f"目标 {item.target_size[0]}×{item.target_size[1]}")
        if item.has_custom_bg:
            marks.append(f"底色 {item.bg_color}")
        info = QLabel(f"{item.size[0]} × {item.size[1]} px"
                      + (("　" + "　".join(marks)) if marks else ""))
        info.setObjectName("SlotHint")
        grid.addWidget(info, 1, 1)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: Dict[str, QPushButton] = {}
        for col, key in enumerate(CATEGORIES):
            btn = QPushButton(CATEGORY_SHORT[key])
            btn.setCheckable(True)
            btn.setFixedHeight(24)
            btn.setMinimumWidth(34)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("category", key)
            btn.setToolTip(CATEGORY_LABELS[key])
            btn.setChecked(item.category == key)
            btn.clicked.connect(lambda _=False, k=key: self._emit_category(k))
            self.group.addButton(btn)
            self.buttons[key] = btn
            grid.addWidget(btn, 0, 2 + col)

        crop_btn = QPushButton("裁剪")
        crop_btn.setFixedHeight(24)
        crop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        crop_btn.setToolTip("按目标比例自动识别人脸并裁剪（裁剪框放回排版）")
        crop_btn.clicked.connect(lambda: self.cropRequested.emit(self.item.path))
        grid.addWidget(crop_btn, 1, 2)

        auto_btn = QPushButton("自动")
        auto_btn.setFixedHeight(24)
        auto_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        auto_btn.setToolTip("打开裁剪框并自动检测人脸（可再手动微调）")
        auto_btn.clicked.connect(lambda: self.autoCropRequested.emit(self.item.path))
        grid.addWidget(auto_btn, 1, 3)

        bg_btn = QPushButton("背景")
        bg_btn.setFixedHeight(24)
        bg_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        bg_btn.setToolTip("只给这张照片换背景色（排版里可混排不同底色）")
        if item.has_custom_bg:
            bg_btn.setStyleSheet(f"color:{item.bg_color};font-weight:600;")
        bg_btn.clicked.connect(lambda: self.bgRequested.emit(self.item.path))
        grid.addWidget(bg_btn, 1, 4)

        del_btn = QPushButton("删除")
        del_btn.setFixedHeight(24)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.clicked.connect(lambda: self.removeRequested.emit(self.item.path))
        grid.addWidget(del_btn, 1, 5)

    def _emit_category(self, category: str) -> None:
        self.categoryChanged.emit(self.item.path, category)


# ---------------------------------------------------------------------------
# 预览控件
# ---------------------------------------------------------------------------
class PreviewWidget(QWidget):
    """等比缩放显示画布，叠加照片辅助线与出血线。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(380, 520)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pixmap: Optional[QPixmap] = None
        self._scaled: Optional[QPixmap] = None
        self._scaled_size: Tuple[int, int] = (0, 0)
        self._guides: List[Tuple[int, int, int, int]] = []
        self._bleed_box: Optional[Tuple[int, int, int, int]] = None

    def set_canvas(self, pixmap: QPixmap, guides: Sequence[Tuple[int, int, int, int]],
                   bleed_box: Optional[Tuple[int, int, int, int]] = None) -> None:
        self._pixmap = pixmap
        self._guides = list(guides)
        self._bleed_box = bleed_box
        self._scaled = None
        self._scaled_size = (0, 0)
        self.update()

    def _scaled_pixmap(self, w: int, h: int) -> QPixmap:
        if self._scaled is not None and self._scaled_size == (w, h):
            return self._scaled
        if self._pixmap is None:
            return QPixmap()
        self._scaled = self._pixmap.scaled(
            QSize(w, h), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self._scaled_size = (w, h)
        return self._scaled

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#eceff4"))

        if self._pixmap is None or self._pixmap.isNull():
            painter.setPen(QColor("#98a1b0"))
            font = painter.font()
            font.setPointSize(11)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "画布预览区\n拖入照片后自动排版")
            return

        pw, ph = self._pixmap.width(), self._pixmap.height()
        scale = min((self.width() - 36) / pw, (self.height() - 36) / ph)
        scale = max(scale, 0.05)
        tw, th = pw * scale, ph * scale
        x = (self.width() - tw) / 2
        y = (self.height() - th) / 2
        target = QRectF(x, y, tw, th)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 32))
        painter.drawRoundedRect(QRectF(x + 5, y + 7, tw, th), 3, 3)
        painter.setBrush(QColor("white"))
        painter.setPen(QPen(QColor("#c6ccd6"), 1))
        painter.drawRect(target)

        scaled = self._scaled_pixmap(max(int(tw), 1), max(int(th), 1))
        if not scaled.isNull():
            painter.drawPixmap(target, scaled, QRectF(scaled.rect()))

        pen = QPen(QColor(110, 118, 132, 200), 1, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for gx, gy, gw, gh in self._guides:
            painter.drawRect(QRectF(x + gx * scale, y + gy * scale, gw * scale, gh * scale))

        if self._bleed_box:
            bx, by, bw, bh = self._bleed_box
            pen = QPen(QColor(214, 69, 69, 230), 1, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawRect(QRectF(x + bx * scale, y + by * scale, bw * scale, bh * scale))
            painter.setPen(QColor(214, 69, 69))
            f = painter.font()
            f.setPointSize(9)
            painter.setFont(f)
            painter.drawText(QPointF(x + bx * scale + 4, y + by * scale - 5), "出血线")


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("证件照自动排版工具")
        self.resize(1380, 920)

        self.settings = load_settings()
        self.template: Template = TEMPLATES.get(
            str(self.settings.get("template", "wedding")), TEMPLATES["wedding"])
        self.photos: List[PhotoItem] = []
        self.bleed_px: int = int(round(mm_to_px(float(self.settings.get("bleed_mm", BLEED_MM)))))
        self.print_scale: float = float(self.settings.get("print_scale", DEFAULT_PRINT_SCALE))
        self.spacing_mm: float = float(self.settings.get("spacing_mm", SPACING_MM))
        # 结婚套装：左右两列之间的横向间距（与上下间隔独立）
        self.wedding_gap_h_mm: float = float(self.settings.get("wedding_gap_h_mm", 8.0))
        self.people: int = int(self.settings.get("people", 1))
        self.per_person: int = int(self.settings.get("per_person", 11))
        self._people_manual = False          # 用户手动改过人数
        self._per_person_manual = False      # 用户手动改过每人张数
        self._syncing = False                # 内部自动同步中（不算用户手动）
        self.count: int = int(self.settings.get("count", 4))
        self.landscape: bool = bool(self.settings.get("landscape", True))
        # 合并排版：六种尺寸各自的张数 / 用谁（兼容旧版 3 值设置）
        mix_vals = normalize_mix(self.settings.get("mix", MIX_DEFAULT))
        people_vals = normalize_mix_people(self.settings.get("mix_people", ()))
        self.mix: Dict[str, int] = dict(zip(MIX_KEYS, mix_vals))
        self.mix_person: Dict[str, str] = dict(zip(MIX_KEYS, people_vals))
        self.count_person: str = str(self.settings.get("count_person", "p1"))
        if self.count_person not in SINGLE_CATEGORIES:
            self.count_person = "p1"

        # ---- 以下为新增功能的运行状态（既有逻辑完全不动）----
        # 自动排版
        self.auto_paper: str = str(self.settings.get("auto_paper", DEFAULT_PAPER_KEY))
        if self.auto_paper not in PAPER_SIZES:
            self.auto_paper = DEFAULT_PAPER_KEY
        self.auto_spec: str = str(self.settings.get("auto_spec", "one_inch"))
        if self.auto_spec not in AUTO_SPEC_KEYS:
            self.auto_spec = "one_inch"
        self.auto_count: int = int(self.settings.get("auto_count", 8))
        self.auto_person: str = str(self.settings.get("auto_person", "p1"))
        if self.auto_person not in SINGLE_CATEGORIES:
            self.auto_person = "p1"
        self.auto_rotate: bool = bool(self.settings.get("auto_rotate", True))
        self.auto_fill: bool = bool(self.settings.get("auto_fill", True))
        self.auto_custom_w_mm: float = float(self.settings.get("auto_custom_w_mm", 25.0))
        self.auto_custom_h_mm: float = float(self.settings.get("auto_custom_h_mm", 35.0))
        # 换背景
        self.bg_mode: str = str(self.settings.get("bg_mode", "keep"))
        if self.bg_mode not in photo_tools.BG_MODES:
            self.bg_mode = "keep"
        self.bg_color: str = str(self.settings.get("bg_color", "#FFFFFF"))
        self.bg_method: str = str(self.settings.get("bg_method", "auto"))
        self.bg_strength: float = float(self.settings.get("bg_strength", 1.0))
        # 人脸检测
        self.face_mode: str = str(self.settings.get("face_mode", "auto"))
        self.facepp_api_key: str = str(self.settings.get("facepp_api_key", ""))
        self.facepp_api_secret: str = str(self.settings.get("facepp_api_secret", ""))
        self.head_ratio: float = float(self.settings.get("head_ratio", 0.60))
        # 单张导出（考试系统上传）
        self.upload_w: int = int(self.settings.get("upload_w", 295))
        self.upload_h: int = int(self.settings.get("upload_h", 413))
        self.upload_unit: str = str(self.settings.get("upload_unit", "px"))
        self.upload_max_kb: int = int(self.settings.get("upload_max_kb", 0))
        self.upload_max_side: int = int(self.settings.get("upload_max_side", 0))

        self.result: Optional[engine.LayoutResult] = None
        self._thumb_cache: Dict[str, QPixmap] = {}
        self._bg_cache: Dict[tuple, Image.Image] = {}      # 换背景结果缓存（按参数分组）

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self.generate_layout)

        self._syncing = True          # 初始化期间的信号不算用户手动操作
        self._build_ui()
        self._rebuild_cards()
        self._update_info()
        self._update_auto_custom_visibility()
        self._update_upload_info()
        self._update_gap_h_visibility()
        self._syncing = False

    # -- 界面 ---------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("Root")
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        left = QWidget()
        left.setObjectName("LeftPanel")
        left.setFixedWidth(500)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(16, 12, 16, 12)
        left_layout.setSpacing(8)

        title = QLabel("证件照自动排版")
        title.setObjectName("AppTitle")
        left_layout.addWidget(title)

        subtitle = QLabel("300 DPI 冲印 · 6R 竖版相纸 10.16 × 15.24 cm")
        subtitle.setObjectName("AppSubTitle")
        left_layout.addWidget(subtitle)

        # ---- 顶部显眼入口：自定义裁剪（自由构图 → 直接出片，不进排版）----
        self.custom_crop_btn = QPushButton("自定义裁剪…（自由构图并导出单张）")
        self.custom_crop_btn.setObjectName("PrimaryButton")
        self.custom_crop_btn.setMinimumHeight(34)
        self.custom_crop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.custom_crop_btn.setToolTip(
            "选一张照片自由拖动/缩放构图，按目标尺寸直接导出单张成片。\n"
            "这里的裁剪不会改动照片在排版里的裁剪框。")
        self.custom_crop_btn.clicked.connect(self.on_custom_crop)
        left_layout.addWidget(self.custom_crop_btn)

        row = QHBoxLayout()
        lbl = QLabel("排版模板")
        lbl.setObjectName("FieldLabel")
        row.addWidget(lbl)
        self.template_combo = QComboBox()
        for t in TEMPLATES.values():
            self.template_combo.addItem(t.title, t.key)
        idx = list(TEMPLATES.keys()).index(self.template.key)
        self.template_combo.setCurrentIndex(max(0, idx))
        self.template_combo.currentIndexChanged.connect(self._on_template_changed)
        row.addWidget(self.template_combo, 1)
        left_layout.addLayout(row)

        self.template_desc = QLabel(self.template.description)
        self.template_desc.setObjectName("SlotHint")
        self.template_desc.setWordWrap(True)
        left_layout.addWidget(self.template_desc)

        # ---- 模板参数区（按模板切换显示）----
        self.params_holder = QWidget()
        params_v = QVBoxLayout(self.params_holder)
        params_v.setContentsMargins(0, 0, 0, 0)
        params_v.setSpacing(4)

        # (a) 一寸：人数 + 每人张数
        self.row_one_inch = QWidget()
        r1 = QHBoxLayout(self.row_one_inch)
        r1.setContentsMargins(0, 0, 0, 0)
        people_lbl = QLabel("人数")
        people_lbl.setObjectName("FieldLabel")
        r1.addWidget(people_lbl)
        self.people_combo = QComboBox()
        for n in (1, 2, 3):
            self.people_combo.addItem(f"{n} 人", n)
        self.people_combo.setCurrentIndex(max(0, min(2, self.people - 1)))
        self.people_combo.currentIndexChanged.connect(self._on_people_changed)
        r1.addWidget(self.people_combo)
        per_lbl = QLabel("每人张数")
        per_lbl.setObjectName("FieldLabel")
        r1.addWidget(per_lbl)
        self.per_spin = QDoubleSpinBox()
        self.per_spin.setRange(1, 12)
        self.per_spin.setDecimals(0)
        self.per_spin.setValue(self.per_person)
        self.per_spin.valueChanged.connect(self._on_per_person_changed)
        r1.addWidget(self.per_spin)
        r1.addStretch(1)
        params_v.addWidget(self.row_one_inch)

        # (b) 小一寸 / 大一寸 / 小二寸 / 二寸 / 大二寸：张数 + 用谁 + 底部横版开关
        self.row_count = QWidget()
        r2 = QHBoxLayout(self.row_count)
        r2.setContentsMargins(0, 0, 0, 0)
        cnt_lbl = QLabel("张数")
        cnt_lbl.setObjectName("FieldLabel")
        r2.addWidget(cnt_lbl)
        self.count_spin = QDoubleSpinBox()
        self.count_spin.setRange(1, 16)
        self.count_spin.setDecimals(0)
        self.count_spin.setValue(4)
        self.count_spin.valueChanged.connect(self._on_count_changed)
        r2.addWidget(self.count_spin)
        who_lbl = QLabel("用谁")
        who_lbl.setObjectName("FieldLabel")
        r2.addWidget(who_lbl)
        self.count_person_combo = QComboBox()
        for cat in SINGLE_CATEGORIES:
            self.count_person_combo.addItem(CATEGORY_LABELS[cat], cat)
        self.count_person_combo.currentIndexChanged.connect(self._on_count_person_changed)
        r2.addWidget(self.count_person_combo)
        self.landscape_check = QCheckBox("底部横版合照")
        self.landscape_check.setObjectName("FieldLabel")
        self.landscape_check.setChecked(True)
        self.landscape_check.stateChanged.connect(self._on_landscape_changed)
        r2.addWidget(self.landscape_check)
        r2.addStretch(1)
        params_v.addWidget(self.row_count)

        # (b2) 结婚照双人（单独排版）：张数
        self.row_couple = QWidget()
        r2b = QHBoxLayout(self.row_couple)
        r2b.setContentsMargins(0, 0, 0, 0)
        cpl_lbl = QLabel("张数")
        cpl_lbl.setObjectName("FieldLabel")
        r2b.addWidget(cpl_lbl)
        self.couple_spin = QDoubleSpinBox()
        self.couple_spin.setRange(1, 5)
        self.couple_spin.setDecimals(0)
        self.couple_spin.setValue(4)
        self.couple_spin.valueChanged.connect(self._on_count_changed)
        r2b.addWidget(self.couple_spin)
        cpl_hint = QLabel("1~4 张正放，第 5 张自动把两张转 90° 收进安全区")
        cpl_hint.setObjectName("SlotHint")
        cpl_hint.setWordWrap(True)
        r2b.addWidget(cpl_hint, 1)
        params_v.addWidget(self.row_couple)

        # (c) 合并排版：六种尺寸各自指定张数 + 用哪个人物（左右两组）
        self.row_mixed = QWidget()
        grid = QGridLayout(self.row_mixed)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(5)
        grid.setVerticalSpacing(4)
        self.mix_spins: Dict[str, QDoubleSpinBox] = {}
        self.mix_persons: Dict[str, QComboBox] = {}
        for idx, key in enumerate(MIX_KEYS):   # 按 MIX_KEYS 顺序建，左列一寸系/右列二寸系
            g, r_i = divmod(idx, 3)
            lbl = QLabel(MIX_LABELS[key])
            lbl.setObjectName("FieldLabel")
            grid.addWidget(lbl, r_i, g * 3)
            spin = QDoubleSpinBox()
            spin.setRange(0, 16)
            spin.setDecimals(0)
            spin.setValue(MIX_DEFAULT[idx])
            spin.valueChanged.connect(self._on_mix_changed)
            self.mix_spins[key] = spin
            grid.addWidget(spin, r_i, g * 3 + 1)
            who = QComboBox()
            for cat in SINGLE_CATEGORIES:
                who.addItem(CATEGORY_LABELS[cat], cat)
            who.currentIndexChanged.connect(self._on_mix_person_changed)
            self.mix_persons[key] = who
            grid.addWidget(who, r_i, g * 3 + 2)
        grid.setColumnStretch(6, 1)
        params_v.addWidget(self.row_mixed)

        # (d) 自动排版：相纸 + 照片尺寸 + 张数 + 自动排满 / 允许旋转 90°
        self.row_auto = QWidget()
        ga = QGridLayout(self.row_auto)
        ga.setContentsMargins(0, 0, 0, 0)
        ga.setHorizontalSpacing(5)
        ga.setVerticalSpacing(4)

        paper_lbl = QLabel("相纸")
        paper_lbl.setObjectName("FieldLabel")
        ga.addWidget(paper_lbl, 0, 0)
        self.paper_combo = QComboBox()
        for key, paper in PAPER_SIZES.items():
            self.paper_combo.addItem(paper.label, key)
        self.paper_combo.setCurrentIndex(
            list(PAPER_SIZES).index(self.auto_paper) if self.auto_paper in PAPER_SIZES else 0)
        self.paper_combo.currentIndexChanged.connect(self._on_auto_paper_changed)
        ga.addWidget(self.paper_combo, 0, 1)

        spec_lbl = QLabel("尺寸")
        spec_lbl.setObjectName("FieldLabel")
        ga.addWidget(spec_lbl, 0, 2)
        self.auto_spec_combo = QComboBox()
        for key in AUTO_SPEC_KEYS:
            self.auto_spec_combo.addItem(AUTO_SPEC_LABELS.get(key, key), key)
        self.auto_spec_combo.setCurrentIndex(
            max(0, AUTO_SPEC_KEYS.index(self.auto_spec)
                if self.auto_spec in AUTO_SPEC_KEYS else 0))
        self.auto_spec_combo.currentIndexChanged.connect(self._on_auto_spec_changed)
        ga.addWidget(self.auto_spec_combo, 0, 3)

        acnt_lbl = QLabel("张数")
        acnt_lbl.setObjectName("FieldLabel")
        ga.addWidget(acnt_lbl, 0, 4)
        self.auto_count_spin = QDoubleSpinBox()
        self.auto_count_spin.setRange(1, 60)
        self.auto_count_spin.setDecimals(0)
        self.auto_count_spin.setValue(self.auto_count)
        self.auto_count_spin.valueChanged.connect(self._on_auto_count_changed)
        ga.addWidget(self.auto_count_spin, 0, 5)

        who_lbl2 = QLabel("用谁")
        who_lbl2.setObjectName("FieldLabel")
        ga.addWidget(who_lbl2, 1, 0)
        self.auto_person_combo = QComboBox()
        for cat in SINGLE_CATEGORIES:
            self.auto_person_combo.addItem(CATEGORY_LABELS[cat], cat)
        self.auto_person_combo.setCurrentIndex(
            max(0, SINGLE_CATEGORIES.index(self.auto_person)
                if self.auto_person in SINGLE_CATEGORIES else 0))
        self.auto_person_combo.currentIndexChanged.connect(self._on_auto_person_changed)
        ga.addWidget(self.auto_person_combo, 1, 1)

        self.auto_fill_check = QCheckBox("自动排满")
        self.auto_fill_check.setObjectName("FieldLabel")
        self.auto_fill_check.setToolTip("由剩余空间反算照片间隔，让整版正好排满可用区")
        self.auto_fill_check.setChecked(self.auto_fill)
        self.auto_fill_check.stateChanged.connect(self._on_auto_fill_changed)
        ga.addWidget(self.auto_fill_check, 1, 2)

        self.auto_rotate_check = QCheckBox("允许旋转 90°")
        self.auto_rotate_check.setObjectName("FieldLabel")
        self.auto_rotate_check.setToolTip("竖版排不下时，把照片横躺（旋转 90°）再排")
        self.auto_rotate_check.setChecked(self.auto_rotate)
        self.auto_rotate_check.stateChanged.connect(self._on_auto_rotate_changed)
        ga.addWidget(self.auto_rotate_check, 1, 3)

        # 自定义尺寸（毫米）——仅在「自定义尺寸」时显示
        self.auto_custom_box = QWidget()
        hc = QHBoxLayout(self.auto_custom_box)
        hc.setContentsMargins(0, 0, 0, 0)
        cw_lbl = QLabel("自定义")
        cw_lbl.setObjectName("FieldLabel")
        hc.addWidget(cw_lbl)
        self.auto_custom_w_spin = QDoubleSpinBox()
        self.auto_custom_w_spin.setRange(5, 200)
        self.auto_custom_w_spin.setDecimals(1)
        self.auto_custom_w_spin.setValue(self.auto_custom_w_mm)
        self.auto_custom_w_spin.setSuffix(" mm")
        self.auto_custom_w_spin.valueChanged.connect(self._on_auto_custom_changed)
        hc.addWidget(self.auto_custom_w_spin)
        x_lbl = QLabel("×")
        x_lbl.setObjectName("FieldLabel")
        hc.addWidget(x_lbl)
        self.auto_custom_h_spin = QDoubleSpinBox()
        self.auto_custom_h_spin.setRange(5, 200)
        self.auto_custom_h_spin.setDecimals(1)
        self.auto_custom_h_spin.setValue(self.auto_custom_h_mm)
        self.auto_custom_h_spin.setSuffix(" mm")
        self.auto_custom_h_spin.valueChanged.connect(self._on_auto_custom_changed)
        hc.addWidget(self.auto_custom_h_spin)
        ga.addWidget(self.auto_custom_box, 1, 4, 1, 2)
        ga.setColumnStretch(6, 1)
        params_v.addWidget(self.row_auto)

        left_layout.addWidget(self.params_holder)

        # 照片池
        self.pool = PhotoPool()
        self.pool.filesDropped.connect(self._add_files)
        left_layout.addWidget(self.pool)

        row = QHBoxLayout()
        add_btn = QPushButton("添加照片…")
        add_btn.setFixedHeight(26)
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(self._browse_photos)
        row.addWidget(add_btn)
        self.stat_label = QLabel("")
        self.stat_label.setObjectName("SlotBadge")
        row.addWidget(self.stat_label, 1)
        left_layout.addLayout(row)

        # 照片列表（已有照片时拖入框隐藏，这里直接接收拖放）
        self.card_area = CardArea()
        self.card_area.setObjectName("CardArea")
        self.card_area.setWidgetResizable(True)
        self.card_area.filesDropped.connect(self._add_files)
        self.card_holder = QWidget()
        self.card_layout = QVBoxLayout(self.card_holder)
        self.card_layout.setContentsMargins(0, 0, 0, 0)
        self.card_layout.setSpacing(6)
        self.card_area.setWidget(self.card_holder)
        left_layout.addWidget(self.card_area, 1)

        # 版式参数
        opts = QHBoxLayout()
        bleed_lbl = QLabel("出血线")
        bleed_lbl.setObjectName("FieldLabel")
        opts.addWidget(bleed_lbl)
        self.bleed_combo = QComboBox()
        for mm in BLEED_OPTIONS_MM:
            self.bleed_combo.addItem(f"{mm:g}mm", mm)
        self.bleed_combo.setCurrentIndex(
            list(BLEED_OPTIONS_MM).index(float(self.settings.get("bleed_mm", BLEED_MM)))
            if float(self.settings.get("bleed_mm", BLEED_MM)) in BLEED_OPTIONS_MM else 2)
        self.bleed_combo.currentIndexChanged.connect(self._on_bleed_changed)
        opts.addWidget(self.bleed_combo)

        gap_lbl = QLabel("照片间隔")
        gap_lbl.setObjectName("FieldLabel")
        opts.addWidget(gap_lbl)
        self.spacing_spin = QDoubleSpinBox()
        self.spacing_spin.setRange(0.0, 20.0)
        self.spacing_spin.setDecimals(1)
        self.spacing_spin.setSingleStep(0.5)
        self.spacing_spin.setSuffix(" mm")
        self.spacing_spin.setValue(self.spacing_mm)
        self.spacing_spin.valueChanged.connect(self._on_spacing_changed)
        opts.addWidget(self.spacing_spin)

        self.dist_combo = QComboBox()
        self.dist_combo.addItem("均匀留白", "even")
        self.dist_combo.addItem("固定间隔", "tight")
        self.dist_combo.setCurrentIndex(
            0 if str(self.settings.get("distribution", "even")) == "even" else 1)
        self.dist_combo.currentIndexChanged.connect(self._on_distribution_changed)
        opts.addWidget(self.dist_combo)
        opts.addStretch(1)
        left_layout.addLayout(opts)

        # 结婚套装专用的「左右间隔」：两列之间的横向缝，与上下间隔互相独立。
        # 单独占一行 —— 500px 的左栏放不下四个控件。
        gap_h_row = QHBoxLayout()
        self.gap_h_lbl = QLabel("左右间隔")
        self.gap_h_lbl.setObjectName("FieldLabel")
        gap_h_row.addWidget(self.gap_h_lbl)
        self.gap_h_spin = QDoubleSpinBox()
        self.gap_h_spin.setRange(0.0, 20.0)
        self.gap_h_spin.setDecimals(1)
        self.gap_h_spin.setSingleStep(0.5)
        self.gap_h_spin.setSuffix(" mm")
        self.gap_h_spin.setValue(self.wedding_gap_h_mm)
        self.gap_h_spin.setToolTip("结婚证件照套装：左列与右列之间的横向间距"
                                   "（与「照片间隔」的上下间距互相独立）")
        self.gap_h_spin.valueChanged.connect(self._on_gap_h_changed)
        gap_h_row.addWidget(self.gap_h_spin)
        self.gap_h_hint = QLabel("结婚套装专用：两列之间的横向间距")
        self.gap_h_hint.setObjectName("SlotHint")
        gap_h_row.addWidget(self.gap_h_hint, 1)
        left_layout.addLayout(gap_h_row)

        # ---- 换背景已移到照片卡片：每张照片单独设底色，可混排不同底色 ----
        # ---- 尺寸 / 限制设置（无画布，只设全局默认）----
        up = QHBoxLayout()
        up_lbl = QLabel("尺寸设置")
        up_lbl.setObjectName("FieldLabel")
        up.addWidget(up_lbl)
        self.crop_tool_btn = QPushButton("尺寸 / 限制 / 检测设置…")
        self.crop_tool_btn.setFixedHeight(24)
        self.crop_tool_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.crop_tool_btn.setToolTip("设置默认目标尺寸、体积上限、人脸检测方式与 Face++ 密钥\n"
                                      "（针对单张照片的裁剪请用照片卡片上的「裁剪」）")
        self.crop_tool_btn.clicked.connect(self._open_crop_tool_no_photo)
        up.addWidget(self.crop_tool_btn)
        self.upload_info_label = QLabel("")
        self.upload_info_label.setObjectName("SlotHint")
        up.addWidget(self.upload_info_label, 1)
        left_layout.addLayout(up)

        # 打印校准
        calib = QHBoxLayout()
        calib_lbl = QLabel("打印校准")
        calib_lbl.setObjectName("FieldLabel")
        calib.addWidget(calib_lbl)
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(*PRINT_SCALE_RANGE)
        self.scale_spin.setDecimals(4)
        self.scale_spin.setSingleStep(0.002)
        self.scale_spin.setValue(self.print_scale)
        self.scale_spin.setPrefix("× ")
        self.scale_spin.setToolTip("打印缩放补偿系数：1.0000 = 不补偿")
        self.scale_spin.valueChanged.connect(self._on_scale_changed)
        calib.addWidget(self.scale_spin)

        calib_btn = QPushButton("校准…")
        calib_btn.setFixedHeight(26)
        calib_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        calib_btn.setToolTip("打印一张样片，量出实际尺寸，自动算补偿系数")
        calib_btn.clicked.connect(self._open_calibration)
        calib.addWidget(calib_btn)

        reset_scale_btn = QPushButton("复位")
        reset_scale_btn.setFixedHeight(26)
        reset_scale_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_scale_btn.clicked.connect(lambda: self.scale_spin.setValue(DEFAULT_PRINT_SCALE))
        calib.addWidget(reset_scale_btn)
        calib.addStretch(1)
        left_layout.addLayout(calib)

        self.guides_check = QCheckBox("导出时叠加裁切辅助线与出血线")
        self.guides_check.setObjectName("FieldLabel")
        self.guides_check.setChecked(bool(self.settings.get("guides", False)))
        left_layout.addWidget(self.guides_check)

        self.direct_save_check = QCheckBox("直接保存到原图目录（不弹窗）")
        self.direct_save_check.setObjectName("FieldLabel")
        self.direct_save_check.setChecked(bool(self.settings.get("direct_save", True)))
        self.direct_save_check.setToolTip(
            "导出文件默认写入照片所在文件夹；取消勾选则弹出保存对话框")
        left_layout.addWidget(self.direct_save_check)

        self.generate_btn = QPushButton("一键生成排版")
        self.generate_btn.setObjectName("PrimaryButton")
        self.generate_btn.setMinimumHeight(38)
        self.generate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.generate_btn.clicked.connect(self.generate_layout)
        left_layout.addWidget(self.generate_btn)

        # 导出：排版图与单张图分成两个按钮（原先并在一起容易点错）
        export_row = QHBoxLayout()
        self.export_btn = QPushButton("导出排版图")
        self.export_btn.setObjectName("SecondaryButton")
        self.export_btn.setMinimumHeight(36)
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.setToolTip("把整版排好的画布导出成一张高清 JPG")
        self.export_btn.clicked.connect(self.export_jpeg)
        export_row.addWidget(self.export_btn, 1)

        self.export_single_btn = QPushButton("导出单张图")
        self.export_single_btn.setObjectName("SecondaryButton")
        self.export_single_btn.setMinimumHeight(36)
        self.export_single_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_single_btn.setToolTip("按上传尺寸把每张照片导出成独立文件（考试系统上传用）")
        self.export_single_btn.clicked.connect(self.export_single)
        export_row.addWidget(self.export_single_btn, 1)
        left_layout.addLayout(export_row)

        self.clear_btn = QPushButton("清空全部")
        self.clear_btn.setObjectName("LinkButton")
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(self.clear_all)
        left_layout.addWidget(self.clear_btn)

        self.info_label = QLabel("")
        self.info_label.setObjectName("SlotHint")
        self.info_label.setWordWrap(True)
        left_layout.addWidget(self.info_label)

        # 右侧预览
        right = QWidget()
        right.setObjectName("RightPanel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(16, 16, 16, 16)
        self.preview_title = QLabel("预览（1200 × 1800 px）")
        self.preview_title.setObjectName("SectionTitle")
        right_layout.addWidget(self.preview_title)
        self.preview = PreviewWidget()
        right_layout.addWidget(self.preview, 1)

        root.addWidget(left)
        root.addWidget(right, 1)

        self.statusBar().showMessage("就绪：把照片拖到左上方照片池，系统会自动归类并排版")
        self._update_params_visibility()

    # -- 照片列表 -----------------------------------------------------------
    def _rebuild_cards(self) -> None:
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for photo in self.photos:
            card = PhotoCard(photo)
            card.categoryChanged.connect(self._on_category_changed)
            card.cropRequested.connect(self._on_crop_requested)
            card.autoCropRequested.connect(self._on_auto_crop_requested)
            card.removeRequested.connect(self._on_remove_requested)
            card.bgRequested.connect(self._on_bg_requested)
            self.card_layout.addWidget(card)
        self.card_layout.addStretch(1)
        # 已有照片时藏起拖入框（照片列表本身就是拖放区），没照片时显示
        self.pool.setVisible(not self.photos)
        self._update_stats()

    # -- 单张换背景（卡片上的「背景」按钮）----------------------------------
    @safe_slot
    def _on_bg_requested(self, path: str) -> None:
        target = next((p for p in self.photos if p.path == path), None)
        if target is None:
            return
        dlg = PhotoBgDialog(target, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dlg.apply_to(target)
        # 换过背景后清掉这张照片的旧缓存，缩略图与排版都重算
        for key in [k for k in self._bg_cache if k[0] == path]:
            self._bg_cache.pop(key, None)
        self._rebuild_cards()
        self._update_info()
        self.statusBar().showMessage(
            f"已为 {os.path.basename(path)} 设置背景："
            f"{target.bg_mode} {target.bg_color}（{target.bg_method}）")
        self._debounce.start()

    def _update_stats(self) -> None:
        counts = {c: 0 for c in CATEGORIES}
        for p in self.photos:
            counts[p.category] += 1
        parts = [f"{CATEGORY_SHORT[c]} {counts[c]}" for c in CATEGORIES if counts[c] or c == "couple"]
        need = ""
        if self.template.kind == "wedding":
            need = f"（需 双人3 / 人物1:{self.template.slots[1].count} / 人物2:{self.template.slots[2].count}）"
        elif self.template.builder == "one_inch":
            total = min(self.people * self.per_person, 11)
            need = f"（{self.people} 人 × 每人 {self.per_person} 张 = {total} 张，一页上限 11）"
        elif self.template.builder == "count":
            extra = " + 底部横版" if self.landscape else ""
            need = f"（{self.count} 张 · {CATEGORY_SHORT.get(self.count_person, '人1')}{extra}）"
        elif self.template.builder == "couple":
            need = f"（双人照 {self.count} 张，一页上限 {self.template.max_count}）"
        elif self.template.builder == "mixed":
            detail = [f"{MIX_LABELS[k]} {self.mix[k]}×{CATEGORY_SHORT.get(self.mix_person[k], '人1')}"
                      for k in MIX_KEYS if self.mix[k] > 0]
            total = sum(self.mix.values())
            need = (f"（{' / '.join(detail) if detail else '未设置张数'}，共 {total} 张）"
                    if total else "（张数全为 0，页面为空）")
        self.stat_label.setText("　".join(parts) + need)

    # -- 照片增删改 ---------------------------------------------------------
    @safe_slot
    def _add_files(self, paths: Sequence[str]) -> None:
        added = 0
        for path in paths:
            if not is_image_file(path):
                continue
            if any(p.path == path for p in self.photos):
                continue
            try:
                size = engine.read_size(path)
            except Exception:
                continue
            category = engine.classify_by_size(size)
            if category != "couple":
                # 竖版照片按顺序轮转分配 人物1/2/3
                used = sum(1 for p in self.photos if p.category in SINGLE_CATEGORIES)
                category = SINGLE_CATEGORIES[used % len(SINGLE_CATEGORIES)]
            thumb = self._thumb_cache.get(path)
            if thumb is None:
                thumb = make_thumbnail(path, (168, 124))
                self._thumb_cache[path] = thumb
            self.photos.append(PhotoItem(path=path, category=category, size=size, thumb=thumb))
            added += 1
        if added:
            self._rebuild_cards()
            self._sync_people_auto()
            self._debounce.start()

    @safe_slot
    def _browse_photos(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择照片", "", "图片文件 (*.jpg *.jpeg *.png *.bmp *.webp *.tif)")
        if files:
            self._add_files(files)

    @safe_slot
    def _on_category_changed(self, path: str, category: str) -> None:
        for p in self.photos:
            if p.path == path:
                p.category = category
                break
        self._update_stats()
        self._sync_people_auto()
        self._debounce.start()

    @safe_slot
    def _on_remove_requested(self, path: str) -> None:
        self.photos = [p for p in self.photos if p.path != path]
        # 顺手把该照片的换背景缓存也释放掉（全分辨率大图占内存）
        for key in [k for k in self._bg_cache if k[0] == path]:
            self._bg_cache.pop(key, None)
        self._rebuild_cards()
        self._sync_people_auto()
        self._debounce.start()

    @safe_slot
    def _on_crop_requested(self, path: str) -> None:
        """照片卡片上的「裁剪」→ 打开统一裁剪对话框（尺寸 + 检测 + 导出）。"""
        target = next((p for p in self.photos if p.path == path), None)
        if target is None:
            return
        try:
            image = engine.load_image(path)
        except Exception as exc:
            QMessageBox.warning(self, "无法读取", f"读取图片失败：\n{exc}")
            return
        self._open_crop_tool(image, target, auto_detect=False,
                             mode=CropToolDialog.MODE_TEMPLATE)

    # -- 自动裁剪（人脸检测）-------------------------------------------------
    @safe_slot
    def _on_auto_crop_requested(self, path: str) -> None:
        """照片卡片上的「自动」→ 打开裁剪对话框并**自动检测人脸**。

        检测结果会直接画到裁剪框上，用户接着还能手动拖动微调，
        点「应用裁剪」才真正生效。
        """
        target = next((p for p in self.photos if p.path == path), None)
        if target is None:
            return
        try:
            image = engine.load_image(path)
        except Exception as exc:
            QMessageBox.warning(self, "无法读取", f"读取图片失败：\n{exc}")
            return
        self._open_crop_tool(image, target, auto_detect=True,
                             mode=CropToolDialog.MODE_TEMPLATE)

    # -- 统一裁剪对话框 -----------------------------------------------------
    def _crop_tool_spec(self, target: Optional[PhotoItem]):
        """打开对话框时的初始比例与尺寸。

        照片自己指定过目标尺寸就用它，否则用全局上传尺寸；
        都没指定就按类别默认规格的比例给画布。
        """
        spec = spec_for_category(target.category) if target is not None else SPECS["one_inch"]
        if target is not None and target.target_size:
            w, h = target.target_size
            return w / h, w, h
        return spec.ratio, self.upload_w, self.upload_h

    def _open_crop_tool(self, image: Optional[Image.Image] = None,
                        target: Optional[PhotoItem] = None,
                        auto_detect: bool = False,
                        mode: str = CropToolDialog.MODE_TEMPLATE) -> None:
        """统一裁剪对话框入口（照片卡片按钮与左栏入口按钮都走这里）。

        mode 决定三条路：TEMPLATE = 卡片裁剪（只读画布、放回模板）；
        SETTINGS = 左栏设置入口（无画布）；CUSTOM 由 on_custom_crop 单独走。
        """
        ratio, init_w, init_h = self._crop_tool_spec(target)
        preset_key = PRESET_DEFAULT
        if target is not None and target.target_size:
            preset_key = PRESET_CUSTOM
        title = "裁剪照片"
        if target is not None:
            title = f"裁剪 · {CATEGORY_LABELS.get(target.category, '照片')}"
        dlg = CropToolDialog(
            image, ratio, title=title,
            preset_key=preset_key,
            width_px=init_w, height_px=init_h, unit=self.upload_unit,
            max_kb=self.upload_max_kb, max_side=self.upload_max_side,
            face_mode=self.face_mode, head_ratio=self.head_ratio,
            api_key=self.facepp_api_key, api_secret=self.facepp_api_secret,
            auto_detect=auto_detect,
            initial_crop=(target.crop if target is not None else None),
            parent=self,
            mode=mode)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        settings = dlg.settings()
        # 模板裁剪只更新检测参数，不覆盖全局尺寸/限制
        self._apply_crop_settings(settings, apply_size=(mode != CropToolDialog.MODE_TEMPLATE))
        if target is not None:
            box = dlg.crop_box()
            if box is not None:
                target.crop = box
                if mode != CropToolDialog.MODE_TEMPLATE:
                    # 自定义尺寸覆盖原本要求的尺寸；跟随默认则清掉覆盖
                    target.target_size = (None if settings.preset_key == PRESET_DEFAULT
                                          else (settings.width_px, settings.height_px))
            face = dlg.face_box()
            if face is not None:
                target.face_box = face        # 换背景抠图要用人脸框保护人物
            self._rebuild_cards()
        self._update_upload_info()
        self._save_settings()
        self._debounce.start()

    def _apply_crop_settings(self, settings: CropSettings,
                             apply_size: bool = True) -> None:
        """把对话框里的尺寸 / 检测设置写回全局状态。

        apply_size=False 时只更新人脸检测相关参数 —— 卡片裁剪不该把
        全局上传尺寸 / 体积上限改掉。
        """
        self.face_mode = settings.face_mode
        self.head_ratio = settings.head_ratio
        self.facepp_api_key = settings.api_key
        self.facepp_api_secret = settings.api_secret
        if not apply_size:
            return
        self.upload_max_kb = max(0, settings.max_kb)
        self.upload_max_side = max(0, settings.max_side)
        if settings.preset_key != PRESET_DEFAULT:
            self.upload_w = max(1, settings.width_px)
            self.upload_h = max(1, settings.height_px)
            self.upload_unit = settings.unit

    # -- 自定义裁剪（顶部入口，出片不写回排版）-------------------------------
    @safe_slot
    def on_custom_crop(self) -> None:
        """顶部「自定义裁剪」：选一张照片 → 自由构图 → 直接导出单张。

        这条路的裁剪框**不写回照片**（photo.crop 不变），只用于出片，
        排版仍按模板里各自设定的裁剪框来。
        """
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择要裁剪的照片", "",
            "图片文件 (*.jpg *.jpeg *.png *.bmp *.webp *.tif)")
        if not files:
            return
        for path in files:
            if not is_image_file(path):
                continue
            try:
                image = engine.load_image(path)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "无法读取", f"读取图片失败：\n{path}\n{exc}")
                continue
            self._open_custom_crop_one(path, image)

    def _open_custom_crop_one(self, path: str, image: Image.Image) -> None:
        ratio, init_w, init_h = self._crop_tool_spec(None)
        dlg = CropToolDialog(
            image, ratio, title=f"自定义裁剪 · {os.path.basename(path)}",
            preset_key=PRESET_DEFAULT,
            width_px=init_w, height_px=init_h, unit=self.upload_unit,
            max_kb=self.upload_max_kb, max_side=self.upload_max_side,
            face_mode=self.face_mode, head_ratio=self.head_ratio,
            api_key=self.facepp_api_key, api_secret=self.facepp_api_secret,
            auto_detect=True, initial_crop=None,
            parent=self, mode=CropToolDialog.MODE_CUSTOM)
        dlg.exportRequested.connect(
            lambda payload, p=path: self._on_custom_crop_export(payload, p))
        dlg.exec()

    @safe_slot
    def _on_custom_crop_export(self, payload: object, source: str = "") -> None:
        """自定义裁剪点「导出单张」：按对话框里的尺寸/限制出片，不写回照片。"""
        if not isinstance(payload, dict):
            return
        image = payload.get("image")
        box = payload.get("crop")
        if image is None:
            return
        if box is not None:
            w0, h0 = image.size
            x1 = max(0, min(w0 - 1, int(round(box[0] * w0))))
            y1 = max(0, min(h0 - 1, int(round(box[1] * h0))))
            x2 = max(x1 + 1, min(w0, int(round(box[2] * w0))))
            y2 = max(y1 + 1, min(h0, int(round(box[3] * h0))))
            image = image.crop((x1, y1, x2, y2))
        size = (int(payload.get("width_px") or 0), int(payload.get("height_px") or 0))
        max_kb = int(payload.get("max_kb") or 0)
        stem = os.path.splitext(os.path.basename(source or "裁剪"))[0]
        default_dir = os.path.dirname(os.path.abspath(source)) if source \
            else self._source_dir()
        default = os.path.join(default_dir, f"{stem}_裁剪.jpg")
        out, _ = QFileDialog.getSaveFileName(
            self, "导出裁剪单张", default, "JPEG 图片 (*.jpg *.jpeg)")
        if not out:
            return
        if not out.lower().endswith((".jpg", ".jpeg")):
            out += ".jpg"
        try:
            info = photo_tools.export_single(image, out, size_px=size,
                                             max_kb=max_kb, dpi=(DPI, DPI))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", f"写入文件时发生错误：\n{exc}")
            return
        self.statusBar().showMessage(
            f"已导出自定义裁剪：{out}　{info.get('width', '?')}×{info.get('height', '?')} px"
            + (f"　{info.get('kb', 0)}KB" if info.get("kb") else ""))

    # -- 自动排版参数（新增）-------------------------------------------------
    @safe_slot
    def _on_auto_paper_changed(self, index: int) -> None:
        self.auto_paper = str(self.paper_combo.itemData(index) or DEFAULT_PAPER_KEY)
        self._update_info()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_auto_spec_changed(self, index: int) -> None:
        self.auto_spec = str(self.auto_spec_combo.itemData(index) or "one_inch")
        self._update_auto_custom_visibility()
        self._update_info()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_auto_count_changed(self, value: float) -> None:
        self.auto_count = int(value)
        self._update_info()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_auto_person_changed(self) -> None:
        self.auto_person = str(self.auto_person_combo.currentData() or "p1")
        self._update_stats()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_auto_fill_changed(self) -> None:
        self.auto_fill = self.auto_fill_check.isChecked()
        self._update_info()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_auto_rotate_changed(self) -> None:
        self.auto_rotate = self.auto_rotate_check.isChecked()
        self._update_info()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_auto_custom_changed(self, value: float) -> None:
        # 两个 spin 共用一个槽，靠 sender() 区分
        sender = self.sender()
        if sender is self.auto_custom_w_spin:
            self.auto_custom_w_mm = float(value)
        else:
            self.auto_custom_h_mm = float(value)
        self._update_info()
        self._save_settings()
        self._debounce.start()

    def _update_auto_custom_visibility(self) -> None:
        self.auto_custom_box.setVisible(self.auto_spec == "custom")

    # -- 换背景（已下沉到每张照片：卡片上的「背景」按钮）--------------------
    def _apply_background(self, image: Image.Image, photo: PhotoItem) -> Image.Image:
        """按照片自己的背景设置换底（带缓存：参数不变就不重复算）。

        照片没单独设置过（keep）就直接返回原图；老数据里没有 bg 字段时
        回落到全局默认值，行为与旧版一致。
        """
        mode = (photo.bg_mode or self.bg_mode or "keep").strip().lower()
        if mode == "keep":
            return image
        color_hex = photo.bg_color or self.bg_color
        method = photo.bg_method or self.bg_method
        key = (photo.path, mode, color_hex, method,
               round(self.bg_strength, 3))
        cached = self._bg_cache.get(key)
        if cached is not None:
            return cached
        color = photo_tools.hex_to_rgb(color_hex)
        try:
            out = photo_tools.replace_background(
                image, color=color, mode=mode,
                face_box=photo.face_box, method=method,
                strength=self.bg_strength)
        except Exception:  # noqa: BLE001
            out = image
        # 缓存里放的是全分辨率大图，条目一多就吃内存 —— 满了就淘汰最旧的
        if len(self._bg_cache) >= 8:
            self._bg_cache.pop(next(iter(self._bg_cache)))
        self._bg_cache[key] = out
        return out

    # -- 上传尺寸 / 单张导出（新增）------------------------------------------
    @safe_slot
    def _open_crop_tool_no_photo(self) -> None:
        """左栏入口：没有画布，只改尺寸、限制与检测设置。"""
        self._open_crop_tool(None, None, auto_detect=False,
                             mode=CropToolDialog.MODE_SETTINGS)

    def _upload_size(self) -> Tuple[int, int]:
        """最终导出尺寸（应用最长边像素上限）。"""
        w, h = int(self.upload_w), int(self.upload_h)
        if self.upload_max_side > 0:
            longest = max(w, h)
            if longest > self.upload_max_side:
                k = self.upload_max_side / float(longest)
                w = max(1, int(round(w * k)))
                h = max(1, int(round(h * k)))
        return w, h

    def _upload_size_for(self, photo: PhotoItem) -> Tuple[int, int]:
        """单张照片的导出尺寸：照片自己指定了就用它的，否则用全局设置。"""
        w, h = photo.target_size if photo.target_size else self._upload_size()
        w, h = int(w), int(h)
        if self.upload_max_side > 0:
            longest = max(w, h)
            if longest > self.upload_max_side:
                k = self.upload_max_side / float(longest)
                w = max(1, int(round(w * k)))
                h = max(1, int(round(h * k)))
        return w, h

    def _update_upload_info(self) -> None:
        w, h = self._upload_size()
        extra = f"　≤{self.upload_max_kb}KB" if self.upload_max_kb > 0 else ""
        self.upload_info_label.setText(f"{w}×{h} px{extra}")

    @safe_slot
    def export_single(self) -> None:
        """按上传尺寸把每张照片导出成独立文件（考试系统上传用）。"""
        if not self.photos:
            QMessageBox.warning(self, "没有照片", "请先导入照片再导出。")
            return
        default_size = self._upload_size()
        out_dir = os.path.join(self._source_dir(), "上传单张")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "无法创建目录", f"{out_dir}\n{exc}")
            return

        done = 0
        failures: List[str] = []
        used_sizes: List[str] = []
        for photo in self.photos:
            stem = os.path.splitext(os.path.basename(photo.path))[0]
            target = os.path.join(out_dir, f"{stem}.jpg")
            # 每张照片可以用自己的目标尺寸（覆盖原本要求的尺寸），否则用全局设置
            size = self._upload_size_for(photo)
            try:
                image = engine.load_image(photo.path)
                image = self._apply_background(image, photo)
                if photo.crop:
                    image = engine.apply_crop(image, photo.crop)
                photo_tools.export_single(image, target, size_px=size,
                                          max_kb=self.upload_max_kb, dpi=(DPI, DPI))
                used_sizes.append(f"{size[0]}×{size[1]}")
                done += 1
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{stem}：{exc}")

        if failures:
            QMessageBox.warning(self, "部分导出失败",
                                f"成功 {done} / {len(self.photos)} 张。\n\n" +
                                "\n".join(failures[:6]))
        else:
            box = QMessageBox(self)
            box.setWindowTitle("导出成功")
            box.setIcon(QMessageBox.Icon.Information)
            box.setText(f"已导出 {done} 张单张证件照：\n{out_dir}")
            info = f"尺寸 {default_size[0]} × {default_size[1]} px"
            if any(s != f"{default_size[0]}×{default_size[1]}" for s in used_sizes):
                uniq = sorted(set(used_sizes))
                info += "（部分照片使用各自指定的尺寸：" + "、".join(uniq) + "）"
            if self.upload_max_kb:
                info += f"，体积 ≤ {self.upload_max_kb} KB"
            box.setInformativeText(info)
            open_btn = box.addButton("打开文件夹", QMessageBox.ButtonRole.ActionRole)
            box.addButton("好", QMessageBox.ButtonRole.AcceptRole)
            box.exec()
            if box.clickedButton() is open_btn:
                QDesktopServices.openUrl(QUrl.fromLocalFile(out_dir))
        self.statusBar().showMessage(f"已导出 {done} 张单张证件照到 {out_dir}")

    # -- 参数 ---------------------------------------------------------------
    @safe_slot
    def _on_template_changed(self, index: int) -> None:
        self.template = TEMPLATES[self.template_combo.itemData(index)]
        self.template_desc.setText(self.template.description)
        self.people = self.template.default_people
        self.per_person = self.template.default_per_person
        self.count = self.template.default_count
        self.landscape = self.template.default_landscape
        # 先在局部算好默认值再回填，避免任何槽函数在中途反写 self.mix
        mix_defaults = dict(zip(MIX_KEYS, normalize_mix(self.template.default_mix)))
        self.mix = mix_defaults
        self.mix_person = {k: "p1" for k in MIX_KEYS}
        self.count_person = "p1"
        self._people_manual = False
        self._per_person_manual = False
        self._syncing = True
        try:
            self.people_combo.setCurrentIndex(max(0, min(2, self.people - 1)))
            self.per_spin.setValue(self.per_person)
            self.count_spin.setRange(1, max(1, self.template.max_count))
            self.couple_spin.setRange(1, max(1, self.template.max_count))
            self.count_spin.setValue(self.count)
            self.couple_spin.setValue(self.count)
            self.landscape_check.setChecked(self.landscape)
            self.count_person_combo.setCurrentIndex(
                max(0, SINGLE_CATEGORIES.index(self.count_person)
                      if self.count_person in SINGLE_CATEGORIES else 0))
            for key, spin in self.mix_spins.items():
                spin.setValue(mix_defaults[key])
            for key, combo in self.mix_persons.items():
                combo.setCurrentIndex(
                    max(0, SINGLE_CATEGORIES.index(self.mix_person[key])
                          if self.mix_person[key] in SINGLE_CATEGORIES else 0))
        finally:
            self._syncing = False
        self.result = None
        self.preview.set_canvas(QPixmap(), [])
        self._rebuild_cards()
        self._update_info()
        self._update_params_visibility()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_people_changed(self, index: int) -> None:
        self.people = int(self.people_combo.itemData(index))
        if not self._syncing:
            self._people_manual = True
        # 自动分配张数：一页最多 11 张（9 竖 + 2 横），按人数均分
        if not self._per_person_manual:
            target = max(1, 11 // self.people)
            if target != self.per_person:
                self.per_person = target
                self._syncing = True
                try:
                    self.per_spin.setValue(target)
                finally:
                    self._syncing = False
        self._update_stats()
        self._update_info()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_per_person_changed(self, value: float) -> None:
        self.per_person = int(value)
        if not self._syncing:
            self._per_person_manual = True   # 手动改过就不再自动覆盖
        self._update_stats()
        self._update_info()
        self._save_settings()
        self._debounce.start()

    def _on_count_changed(self, value: float) -> None:
        self.count = int(value)
        self._update_stats()
        self._update_info()
        self._save_settings()
        self._debounce.start()

    def _on_landscape_changed(self) -> None:
        self.landscape = self.landscape_check.isChecked()
        self._update_stats()
        self._update_info()
        self._save_settings()
        self._debounce.start()

    def _on_count_person_changed(self) -> None:
        self.count_person = str(self.count_person_combo.currentData() or "p1")
        self._update_stats()
        self._update_info()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_mix_person_changed(self) -> None:
        if self._syncing:          # 模板切换时由代码回填控件，不要反写状态
            return
        for key, combo in self.mix_persons.items():
            self.mix_person[key] = str(combo.currentData() or "p1")
        self._update_stats()
        self._update_info()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_mix_changed(self) -> None:
        if self._syncing:          # 模板切换时由代码回填控件，不要反写状态
            return
        for key, spin in self.mix_spins.items():
            self.mix[key] = int(spin.value())
        self._update_stats()
        self._update_info()
        self._save_settings()
        self._debounce.start()

    def _auto_people_count(self) -> int:
        """导入了几个人物类别的照片，就自动判定为几个人（1~3）。"""
        used = {p.category for p in self.photos} & set(SINGLE_CATEGORIES)
        return max(1, min(3, len(used)))

    def _sync_people_auto(self) -> None:
        """照片增删或改类别后，自动同步人数（用户手动改过人数则不覆盖）。"""
        if not self.template.adjustable or self._people_manual:
            return
        count = self._auto_people_count()
        self._syncing = True
        try:
            if count != self.people:
                self.people = count
                self.people_combo.setCurrentIndex(max(0, min(2, count - 1)))
            # 每人张数始终按人数重算（一页上限 11 张），除非用户手动改过
            if not self._per_person_manual:
                target = max(1, 11 // self.people)
                if target != self.per_person:
                    self.per_person = target
                    self.per_spin.setValue(target)
        finally:
            self._syncing = False

    @safe_slot
    def _on_bleed_changed(self, index: int) -> None:
        self.bleed_px = int(round(mm_to_px(self.bleed_combo.itemData(index))))
        self._update_info()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_spacing_changed(self, value: float) -> None:
        self.spacing_mm = float(value)
        self._update_info()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_gap_h_changed(self, value: float) -> None:
        """左右间隔（结婚套装两列之间的横向缝）。"""
        self.wedding_gap_h_mm = float(value)
        self._update_info()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_distribution_changed(self, index: int) -> None:
        mode = self.dist_combo.itemData(index)
        if self.template.kind == "wedding":
            plan = replace(self.template.wedding_plan, distribution=mode)
            self.template = replace(self.template, wedding_plan=plan)
        self._update_info()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _on_scale_changed(self, value: float) -> None:
        self.print_scale = float(value)
        self._update_info()
        self._save_settings()
        self._debounce.start()

    @safe_slot
    def _open_calibration(self) -> None:
        dlg = CalibrationDialog(self.print_scale, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            factor = dlg.factor()
            self.scale_spin.setValue(round(factor, 4))
            self.statusBar().showMessage(
                f"已应用校准系数 ×{factor:.4f}（重新导出即可得到标称尺寸），建议再打印一张样片复核。")

    def _update_params_visibility(self) -> None:
        builder = self.template.builder
        self.row_one_inch.setVisible(builder == "one_inch")
        self.row_count.setVisible(builder == "count")
        self.row_couple.setVisible(builder == "couple")
        self.row_mixed.setVisible(builder == "mixed")
        self.row_auto.setVisible(builder == "auto")
        self.params_holder.setVisible(self.template.kind == "grid")
        self.dist_combo.setVisible(self.template.kind == "wedding")
        self._update_auto_custom_visibility()
        self._update_gap_h_visibility()

    def _update_gap_h_visibility(self) -> None:
        """「左右间隔」只在结婚套装里有意义，其他模板隐藏。"""
        visible = self.template.kind == "wedding"
        self.gap_h_lbl.setVisible(visible)
        self.gap_h_spin.setVisible(visible)
        self.gap_h_hint.setVisible(visible)

    def _update_info(self) -> None:
        t = self.template
        scaled = abs(self.print_scale - 1.0) > 1e-6
        scale_text = (f"；打印校准 ×{self.print_scale:.4f}" if scaled else "")
        canvas_text = t.canvas_size_text
        preview_text = f"预览（{t.canvas_width} × {t.canvas_height} px）"
        if t.builder == "auto":
            paper = PAPER_SIZES.get(self.auto_paper)
            if paper is not None:
                canvas_text = f"{paper.size_text}（{paper.cm_text} @ {DPI} DPI）"
                preview_text = f"预览（{paper.width_px} × {paper.height_px} px）"
            target_text = AUTO_SPEC_LABELS.get(self.auto_spec, self.auto_spec)
        elif t.kind == "wedding":
            target_text = "双人照 5.3×3.5cm、单人照 2.5×3.5cm"
        elif t.key == "one_inch":
            target_text = "一寸 2.5×3.5cm（底部横版 3.5×2.5cm）"
        elif t.builder == "couple":
            target_text = f"双人照 {SPECS['couple'].cm_text}"
        elif t.builder == "count":
            target_text = SPECS[t.spec_key].cm_text
            if self.landscape:
                target_text += f"、合照 {SPECS['couple'].cm_text}"
        elif t.builder == "mixed":
            used = [f"{MIX_LABELS[k]} {SPECS[k].cm_text}"
                    for k in MIX_KEYS if self.mix.get(k, 0) > 0]
            target_text = "、".join(used) if used else "未设置张数"
        else:
            target_text = "一寸 2.5×3.5cm、二寸 3.5×4.9cm、小二寸 3.5×4.5cm"
        bg_text = ""
        bg_photos = [p for p in self.photos if p.has_custom_bg]
        if bg_photos:
            kinds = sorted({f"{p.bg_mode} {p.bg_color}" for p in bg_photos})
            bg_text = (f"\n背景：{len(bg_photos)} 张已换底"
                       f"（{'、'.join(kinds)}，其余保留原图）")
        gap_text = f"照片间隔 {self.spacing_mm:g}mm"
        if t.kind == "wedding":
            gap_text += f"，左右间隔 {self.wedding_gap_h_mm:g}mm"
        self.info_label.setText(
            f"画布 {canvas_text}；出血线 {self.bleed_px}px；"
            f"{gap_text}{scale_text}。\n"
            f"标称尺寸：{target_text}；quality=95，DPI ({DPI}, {DPI})。{bg_text}")
        self.preview_title.setText(preview_text)

    def _save_settings(self) -> None:
        self.settings.update({
            "print_scale": self.print_scale,
            "spacing_mm": self.spacing_mm,
            "bleed_mm": float(self.bleed_combo.currentData() or BLEED_MM),
            "people": self.people,
            "per_person": self.per_person,
            "count": self.count,
            "landscape": self.landscape,
            "mix": tuple(self.mix.get(k, 0) for k in MIX_KEYS),
            "mix_people": tuple(self.mix_person.get(k, "p1") for k in MIX_KEYS),
            "count_person": self.count_person,
            "guides": self.guides_check.isChecked(),
            "direct_save": self.direct_save_check.isChecked(),
            "distribution": self.dist_combo.currentData() or "even",
            "template": self.template.key,
            # 新增项
            "wedding_gap_h_mm": self.wedding_gap_h_mm,
            "bg_mode": self.bg_mode,
            "bg_color": self.bg_color,
            "bg_method": self.bg_method,
            "bg_strength": self.bg_strength,
            "face_mode": self.face_mode,
            "facepp_api_key": self.facepp_api_key,
            "facepp_api_secret": self.facepp_api_secret,
            "head_ratio": self.head_ratio,
            "auto_paper": self.auto_paper,
            "auto_spec": self.auto_spec,
            "auto_count": self.auto_count,
            "auto_person": self.auto_person,
            "auto_rotate": self.auto_rotate,
            "auto_fill": self.auto_fill,
            "auto_custom_w_mm": self.auto_custom_w_mm,
            "auto_custom_h_mm": self.auto_custom_h_mm,
            "upload_w": self.upload_w,
            "upload_h": self.upload_h,
            "upload_unit": self.upload_unit,
            "upload_max_kb": self.upload_max_kb,
            "upload_max_side": self.upload_max_side,
        })
        save_settings(self.settings)

    @safe_slot
    def clear_all(self) -> None:
        self.photos = []
        self._thumb_cache.clear()
        self._bg_cache.clear()
        self.result = None
        self.preview.set_canvas(QPixmap(), [])
        self._rebuild_cards()
        self.statusBar().showMessage("已清空")

    # -- 核心流程 -----------------------------------------------------------
    def _grouped_inputs(self) -> Dict[str, List[PhotoInput]]:
        result: Dict[str, List[PhotoInput]] = {}
        for cat in CATEGORIES:
            picked = [p for p in self.photos if p.category == cat]
            result[cat] = [self._to_input(p) for p in picked]
        return result

    def _to_input(self, photo: PhotoItem) -> PhotoInput:
        image = engine.load_image(photo.path)
        image = self._apply_background(image, photo)
        return PhotoInput(image, photo.crop)

    @safe_slot
    def generate_layout(self) -> None:
        if not self.photos:
            self.statusBar().showMessage("请先把照片拖到照片池")
            return
        params = {"people": self.people, "per_person": self.per_person,
                  "count": self.count, "landscape": self.landscape,
                  "count_person": self.count_person,
                  # 结婚套装的分布方式（均匀留白 / 固定间隔）
                  "distribution": self.dist_combo.currentData() or "even",
                  # 自动排版（新增参数；既有模板不读这些键，行为不变）
                  "paper_key": self.auto_paper,
                  "auto_spec": self.auto_spec,
                  "auto_count": self.auto_count,
                  "auto_person": self.auto_person,
                  "auto_fill": self.auto_fill,
                  "auto_rotate": self.auto_rotate,
                  "auto_custom_w_mm": self.auto_custom_w_mm,
                  "auto_custom_h_mm": self.auto_custom_h_mm}
        for key in MIX_KEYS:                       # 六种尺寸各自的张数与人物
            params[key] = int(self.mix.get(key, 0))
            params[f"{key}_person"] = self.mix_person.get(key, "p1")
        try:
            inputs = self._grouped_inputs()
            gap = max(1, int(round(mm_to_px(self.spacing_mm) * self.print_scale)))
            # 左右间隔独立于上下间隔；非结婚套装不会用到，但照样传值，
            # 这样引擎行为对所有模板保持一致。
            gap_h = max(0, int(round(mm_to_px(self.wedding_gap_h_mm) * self.print_scale)))
            result = engine.LayoutEngine(self.template, gap=gap, margin=MARGIN,
                                         bleed=self.bleed_px,
                                         print_scale=self.print_scale,
                                         gap_h=gap_h).build(inputs, params)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "生成失败", f"排版过程中发生错误：\n{exc}")
            return
        finally:
            inputs = {}

        self.result = result
        self.preview.set_canvas(pil_to_qpixmap(result.canvas), result.guide_boxes,
                                result.bleed_box)

        message = (f"排版完成：{len(result.placements)} 张照片，"
                   f"{result.canvas.width}×{result.canvas.height} px，"
                   f"实际间隔 {result.used_spacing}px")
        if result.warnings:
            message += "　|　" + "；".join(result.warnings)
            self.statusBar().setStyleSheet("color:#b26a00;")
        else:
            self.statusBar().setStyleSheet("")
        self.statusBar().showMessage(message)

    # -- 导出 ---------------------------------------------------------------
    def _source_dir(self) -> str:
        """所有照片的公共上级目录 —— 作为默认保存位置。"""
        if not self.photos:
            return os.path.expanduser("~")
        dirs = {os.path.dirname(os.path.abspath(p.path)) for p in self.photos}
        if len(dirs) == 1:
            return next(iter(dirs))
        try:
            return os.path.commonpath(list(dirs))
        except ValueError:
            return os.path.expanduser("~")

    def _default_export_path(self) -> str:
        name = self.template.export_name
        if self.template.builder == "auto":
            paper = PAPER_SIZES.get(self.auto_paper)
            if paper is not None:
                name = f"自动排版_{paper.width_px}x{paper.height_px}.jpg"
        return os.path.join(self._source_dir(), name)

    def _do_export(self, path: str) -> None:
        try:
            engine.export_jpeg(
                self.result.canvas, path, dpi=(DPI, DPI), quality=95,
                placements=self.result.placements,
                with_guides=self.guides_check.isChecked(),
                bleed_box=self.result.bleed_box if self.guides_check.isChecked() else None)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", f"写入文件时发生错误：\n{exc}")
            return

        dpi_text = "未知"
        try:
            with Image.open(path) as chk:
                dpi_text = str(chk.info.get("dpi"))
        except Exception:  # noqa: BLE001
            pass

        box = QMessageBox(self)
        box.setWindowTitle("导出成功")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"已导出：\n{path}")
        box.setInformativeText(
            f"尺寸 {self.result.canvas.width} × {self.result.canvas.height} px"
            f"（{self.result.canvas.width / (DPI / 2.54):.2f} × "
            f"{self.result.canvas.height / (DPI / 2.54):.2f} cm）\n"
            f"quality=95，4:4:4 无色度抽样，DPI 元数据 = {dpi_text}")
        open_btn = box.addButton("打开所在文件夹", QMessageBox.ButtonRole.ActionRole)
        box.addButton("好", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(os.path.dirname(os.path.abspath(path))))

    @safe_slot
    def export_jpeg(self) -> None:
        if self.result is None:
            QMessageBox.warning(self, "尚未生成", "请先点击「一键生成排版」再导出。")
            return

        default = self._default_export_path()
        if self.direct_save_check.isChecked():
            if os.path.exists(default):
                ret = QMessageBox.question(
                    self, "文件已存在", f"目标文件已存在，是否覆盖？\n{default}",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if ret != QMessageBox.StandardButton.Yes:
                    return
            self._do_export(default)
            return

        path, _ = QFileDialog.getSaveFileName(self, "导出高清 JPG", default,
                                              "JPEG 图片 (*.jpg *.jpeg)")
        if not path:
            return
        if not path.lower().endswith((".jpg", ".jpeg")):
            path += ".jpg"
        self._do_export(path)


# ---------------------------------------------------------------------------
# 样式
# ---------------------------------------------------------------------------
STYLESHEET = """
QWidget#Root, QWidget#LeftPanel { background: #f5f7fa; }
QWidget#RightPanel { background: #e9edf3; }
QLabel#AppTitle { font-size: 20px; font-weight: 700; color: #1d2531; }
QLabel#AppSubTitle { font-size: 12px; color: #5b6472; }
QLabel#SectionTitle { font-size: 13px; font-weight: 600; color: #3a4453; }
QLabel#FieldLabel { font-size: 12px; color: #4a5462; }
QLabel#PoolTitle { font-size: 13px; font-weight: 600; color: #2f6fed; }
QLabel#CardName { font-size: 12px; font-weight: 600; color: #232c3a; }
QLabel#SlotHint { font-size: 11px; color: #7a8494; }
QLabel#SlotBadge { font-size: 11px; color: #5b6472; }
QFrame#PhotoPool { background: #ffffff; border: 2px dashed #b9c4d6; border-radius: 8px; }
QFrame#PhotoPool[dragActive="true"] { border: 2px solid #2f6fed; background: #eef4ff; }
QFrame#PhotoCard { background: #ffffff; border: 1px solid #e1e6ee; border-radius: 6px; }
QScrollArea#CardArea { background: transparent; border: none; }
QLabel#Thumb { background: #f2f4f8; border: 1px solid #dfe4ec; border-radius: 4px; }
QPushButton#PrimaryButton {
    background: #2f6fed; color: #ffffff; border: none; border-radius: 6px;
    font-size: 14px; font-weight: 600; padding: 8px 12px;
}
QPushButton#PrimaryButton:hover { background: #2560d8; }
QPushButton#PrimaryButton:pressed { background: #1f52b8; }
QPushButton#SecondaryButton {
    background: #ffffff; color: #2f6fed; border: 1px solid #2f6fed; border-radius: 6px;
    font-size: 14px; font-weight: 600; padding: 7px 12px;
}
QPushButton#SecondaryButton:hover { background: #eef4ff; }
QPushButton#LinkButton { background: transparent; border: none; color: #7a8494; font-size: 12px; }
QPushButton#LinkButton:hover { color: #2f6fed; }
QPushButton[category]:checked { background: #2f6fed; color: #ffffff; border: 1px solid #2f6fed; }
QPushButton { background: #ffffff; border: 1px solid #d8dee7; border-radius: 4px; padding: 2px 6px; }
QComboBox {
    background: #ffffff; color: #232c3a;
    border: 1px solid #d8dee7; border-radius: 6px; padding: 4px 8px; min-height: 20px;
}
QComboBox:hover { border-color: #2f6fed; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox::down-arrow {
    image: none; width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid #5b6472; margin-right: 6px;
}
QComboBox QAbstractItemView {
    background-color: #ffffff; color: #232c3a; border: 1px solid #d8dee7;
    selection-background-color: #2f6fed; selection-color: #ffffff; outline: none;
    min-width: 120px;
}
QComboBox QAbstractItemView::item { color: #232c3a; background: #ffffff; padding: 5px 10px; min-height: 22px; }
QComboBox QAbstractItemView::item:hover { background: #eef4ff; color: #1d2531; }
QComboBox QAbstractItemView::item:selected { background: #2f6fed; color: #ffffff; }
QDoubleSpinBox {
    background: #ffffff; color: #232c3a;
    border: 1px solid #d8dee7; border-radius: 6px; padding: 3px 6px; min-height: 20px;
}
QDoubleSpinBox:focus { border-color: #2f6fed; }
QCheckBox { color: #4a5462; font-size: 12px; }
QStatusBar { background: #ffffff; color: #4a5462; }
"""


def apply_cjk_font(app: QApplication) -> str:
    """挑选一个支持中文的字体，避免部分系统默认字体显示成方框。"""
    from PyQt6.QtGui import QFont, QFontDatabase

    available = set(QFontDatabase.families())
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "Noto Sans CJK SC",
                 "Source Han Sans CN", "PingFang SC", "WenQuanYi Micro Hei", "SimHei",
                 "Arial Unicode MS"):
        if name in available:
            app.setFont(QFont(name, 9))
            return name
    return ""


def resource_path(name: str) -> str:
    """定位随程序打包的资源文件（兼容 PyInstaller 单文件模式）。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def apply_window_icon(app: QApplication) -> bool:
    """设置窗口/任务栏图标。"""
    from PyQt6.QtGui import QIcon

    for candidate in (resource_path("icon.ico"), resource_path("icon.png")):
        if os.path.exists(candidate):
            app.setWindowIcon(QIcon(candidate))
            return True
    return False


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("证件照自动排版工具")
    app.setOrganizationName("PhotoLayout")
    app.setStyle("Fusion")
    apply_cjk_font(app)
    apply_window_icon(app)
    app.setStyleSheet(STYLESHEET)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
