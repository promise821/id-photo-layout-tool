# -*- coding: utf-8 -*-
"""离屏 UI 冒烟测试：归类 → 人数自动判定 → 全部 9 个模板 → 张数/人物/横版开关
→ 结婚双人单独排版 → 六尺寸混排 → 溢出与边界 → 设置持久化。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication
from PIL import Image

import main as ui
import layout_engine as engine
from config import (BLEED, MARGIN, MIX_DEFAULT, MIX_KEYS, SPECS, TEMPLATES,
                    load_settings, settings_location)

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, "_selftest")


def in_safe(win, res):
    """所有照片必须完整落在安全区内（边界硬约束）。"""
    left = win.bleed_px + int(round(MARGIN * win.print_scale))
    right = res.canvas.width - left
    bottom = res.canvas.height - left
    bad = [p for p in res.placements
           if p.x < left or p.y < left or p.x + p.width > right or p.y + p.height > bottom]
    return not bad, bad[:1]


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(ui.STYLESHEET)

    win = ui.MainWindow()
    win.resize(1380, 920)
    win.show()

    # 固定初始参数，避免受上次运行残留的设置影响
    win.spacing_spin.setValue(1.0)
    win.scale_spin.setValue(1.0)
    app.processEvents()

    # ---- 1) 导入 2 张 → 自动判定 2 人 ----
    win.template_combo.setCurrentIndex(win.template_combo.findData("one_inch"))
    win._add_files([os.path.join(TMP, "m0.jpg"), os.path.join(TMP, "f0.jpg")])
    app.processEvents()
    print(f"导入 2 张 → {win.people} 人，每人 {win.per_person} 张")
    assert win.people == 2 and win.per_person == 5

    # ---- 2) 导入 3 张 → 3 人 ----
    win._add_files([os.path.join(TMP, "t0.jpg")])
    app.processEvents()
    print(f"导入 3 张 → {win.people} 人，每人 {win.per_person} 张")
    assert win.people == 3 and win.per_person == 3

    # ---- 3) 3 人各 4 张 = 12 → 最后一人少一张，共 11 ----
    win.per_spin.setValue(4)
    app.processEvents()
    win.generate_layout()
    app.processEvents()
    n_port = sum(1 for p in win.result.placements if p.height > p.width)
    n_land = sum(1 for p in win.result.placements if p.height <= p.width)
    print(f"  一寸 3 人×4 → {len(win.result.placements)} 张（竖 {n_port} / 横 {n_land}）")
    assert len(win.result.placements) == 11 and n_port == 9 and n_land == 2
    assert in_safe(win, win.result)[0]
    win.preview.grab().save(os.path.join(TMP, "ui_one_inch_3p.png"))

    # ---- 4) 全部模板（原有 9 套 + 新增「自动排版」）----
    win._add_files([os.path.join(TMP, "c0.jpg"), os.path.join(TMP, "m1.jpg")])
    expected_keys = ["wedding", "couple_only", "one_inch", "tiny_one_inch",
                     "big_one_inch", "small_two_inch", "two_inch",
                     "big_two_inch", "mixed", "auto"]
    assert [t.key for t in TEMPLATES.values()] == expected_keys, "模板顺序/数量不符"
    for key in expected_keys:
        win.template_combo.setCurrentIndex(win.template_combo.findData(key))
        win.generate_layout()
        app.processEvents()
        assert win.result is not None, key
        ok, bad = in_safe(win, win.result)
        print(f"  {key:15s} → {len(win.result.placements):2d} 张  "
              f"警告 {len(win.result.warnings)}  边界 {'OK' if ok else 'FAIL ' + str(bad)}  "
              f"{win.stat_label.text()[:60]}")
        assert ok, f"{key} 越界 {bad}"
        win.grab().save(os.path.join(TMP, f"ui_{key}.png"))

    # ---- 5) 小二寸：张数 + 底部横版开关 ----
    win.template_combo.setCurrentIndex(win.template_combo.findData("small_two_inch"))
    app.processEvents()
    assert not win.row_couple.isVisible() and win.row_count.isVisible()
    win.count_spin.setValue(2)
    win.landscape_check.setChecked(True)
    app.processEvents()
    win.generate_layout()
    app.processEvents()
    print(f"  小二寸 2 张 + 横版 → {len(win.result.placements)} 张")
    assert len(win.result.placements) == 3
    win.landscape_check.setChecked(False)
    app.processEvents()
    win.generate_layout()
    app.processEvents()
    print(f"  小二寸 2 张 无横版 → {len(win.result.placements)} 张")
    assert len(win.result.placements) == 2
    win.landscape_check.setChecked(True)

    # ---- 5b) 各尺寸模板的张数上限与用谁 ----
    for key in ("tiny_one_inch", "big_one_inch", "two_inch", "big_two_inch"):
        win.template_combo.setCurrentIndex(win.template_combo.findData(key))
        app.processEvents()
        assert win.count_spin.maximum() == TEMPLATES[key].max_count, key
        win.count_spin.setValue(TEMPLATES[key].max_count)
        win.count_person_combo.setCurrentIndex(1)          # 人物2
        app.processEvents()
        win.generate_layout()
        app.processEvents()
        ok, bad = in_safe(win, win.result)
        who = {p.slot_key for p in win.result.placements}
        print(f"  {key:15s} 满 {win.count_spin.value():.0f} 张 → "
              f"{len(win.result.placements)} 张，用谁 {sorted(who)}，边界 {'OK' if ok else 'FAIL'}")
        assert ok and len(win.result.placements) == TEMPLATES[key].max_count
        assert who == {"p2"}

    # ---- 5c) 结婚照双人（单独排版）：张数 1~6（UI 上限 5，超出自动钳制）----
    win.template_combo.setCurrentIndex(win.template_combo.findData("couple_only"))
    app.processEvents()
    assert win.row_couple.isVisible() and not win.row_count.isVisible()
    assert win.couple_spin.maximum() == 5
    for n in (1, 3, 4, 5, 6):
        win.couple_spin.setValue(n)
        app.processEvents()
        want = min(n, 5)                        # 旋钮上限 5
        win.generate_layout()
        app.processEvents()
        ok, bad = in_safe(win, win.result)
        placed = len(win.result.placements)
        print(f"  双人请求 {n} 张（旋钮 {win.couple_spin.value():.0f}）→ 排入 {placed}"
              f"（提示 {win.result.warnings[:1]}），边界 {'OK' if ok else 'FAIL'}")
        assert ok and placed == want, f"双人 {n} 张排了 {placed}"
        if n == 5:
            win.preview.grab().save(os.path.join(TMP, "ui_couple_5.png"))
    win.couple_spin.setValue(4)

    # ---- 6) 合并排版：六尺寸混排 ----
    win.template_combo.setCurrentIndex(win.template_combo.findData("mixed"))
    app.processEvents()
    assert win.row_mixed.isVisible() and not win.row_count.isVisible()
    assert set(win.mix_spins) == set(MIX_KEYS), "混排控件应有 6 种尺寸"
    for k in MIX_KEYS:
        win.mix_spins[k].setValue(1)
    app.processEvents()
    win.generate_layout()
    app.processEvents()
    ok, bad = in_safe(win, win.result)
    sizes = sorted({(p.width, p.height) for p in win.result.placements})
    print(f"  六尺寸各 1 张 → {len(win.result.placements)} 张，{sizes}，边界 {'OK' if ok else 'FAIL'}")
    assert ok and len(win.result.placements) == 6
    win.preview.grab().save(os.path.join(TMP, "ui_mixed_six.png"))

    # 默认混排 3/2/2（切到别的模板再切回来会恢复该模板默认值）
    win.template_combo.setCurrentIndex(win.template_combo.findData("wedding"))
    win.template_combo.setCurrentIndex(win.template_combo.findData("mixed"))
    app.processEvents()
    print("  默认混排 →", {k: win.mix[k] for k in MIX_KEYS}, win.stat_label.text()[:80])
    assert tuple(win.mix[k] for k in MIX_KEYS) == MIX_DEFAULT
    win.generate_layout()
    app.processEvents()
    assert len(win.result.placements) == 7
    assert in_safe(win, win.result)[0]

    # 高度不足时自动旋转
    for k in MIX_KEYS:
        win.mix_spins[k].setValue(0)
    win.mix_spins["two_inch"].setValue(6)
    app.processEvents()
    win.generate_layout()
    app.processEvents()
    rot = [p for p in win.result.placements if p.width > p.height]
    ok, _ = in_safe(win, win.result)
    print(f"  混排 二寸×6 → 排入 {len(win.result.placements)} 张，横躺 {len(rot)} 张，"
          f"提示 {win.result.warnings[:1]}")
    assert len(rot) >= 1 and ok
    win.preview.grab().save(os.path.join(TMP, "ui_mixed_rotate.png"))

    # ---- 6b) 每种规格指定人物 ----
    for k in MIX_KEYS:
        win.mix_spins[k].setValue(1)
    win.mix_persons["one_inch"].setCurrentIndex(1)         # 人物2
    win.mix_persons["small_two_inch"].setCurrentIndex(2)   # 人物3
    app.processEvents()
    win.generate_layout()
    app.processEvents()
    seen = {}
    for p in win.result.placements:
        seen.setdefault((p.width, p.height), set()).add(p.slot_key)
    print(f"  每规格指定人物 → {seen}")
    print("  统计:", win.stat_label.text())
    assert seen.get((SPECS["one_inch"].width_px, SPECS["one_inch"].height_px)) == {"p2"}
    assert seen.get((SPECS["small_two_inch"].width_px, SPECS["small_two_inch"].height_px)) == {"p3"}
    win.preview.grab().save(os.path.join(TMP, "ui_mixed_people.png"))

    # ---- 7) 按钮点击（clicked(bool) 信号）不应报错 ----
    win.template_combo.setCurrentIndex(win.template_combo.findData("wedding"))
    app.processEvents()
    win.generate_btn.click()
    app.processEvents()
    assert win.result is not None
    print("  点击「一键生成排版」：OK")

    # ---- 8) 自定义间隔 → 溢出提示 + 边界不越界 ----
    win.template_combo.setCurrentIndex(win.template_combo.findData("one_inch"))
    win.people_combo.setCurrentIndex(2)      # 3 人
    win.per_spin.setValue(4)                  # 11 张（3 竖版行 + 1 横版行）
    win.spacing_spin.setValue(10.0)           # 10mm 间隔下放不下
    app.processEvents()
    win.generate_layout()
    app.processEvents()
    ok, bad = in_safe(win, win.result)
    print("  一寸 11 张 + 间隔 10mm 警告:", win.result.warnings[:1], "边界", "OK" if ok else bad)
    assert win.result.warnings
    assert win.result.used_spacing < 118, "超界时应自动压缩间隔"
    assert ok, f"压缩后仍越界 {bad}"
    win.spacing_spin.setValue(1.0)

    # ---- 8b) 校准系数过大 → 仍不许越界 ----
    win.scale_spin.setValue(1.3)
    for key in expected_keys:
        win.template_combo.setCurrentIndex(win.template_combo.findData(key))
        app.processEvents()
        win.generate_layout()
        app.processEvents()
        ok, bad = in_safe(win, win.result)
        print(f"  校准1.3 {key:15s} → {len(win.result.placements):2d} 张 边界 "
              f"{'OK' if ok else 'FAIL ' + str(bad)}")
        assert ok, f"校准1.3 {key} 越界 {bad}"
    win.scale_spin.setValue(1.0)

    # ---- 9) 设置持久化（注册表优先）----
    win.scale_spin.setValue(1.0583)
    win.spacing_spin.setValue(2.5)
    win.count_spin.setValue(3)
    app.processEvents()
    print("  设置位置:", settings_location())
    s = load_settings()
    print("  已保存:", {k: s[k] for k in ("print_scale", "spacing_mm", "count", "template")})
    assert abs(float(s["print_scale"]) - 1.0583) < 1e-6
    assert int(s["count"]) == 3

    win2 = ui.MainWindow()
    print(f"  重开窗口: 校准 ×{win2.print_scale:.4f}, 间隔 {win2.spacing_mm}mm, 张数 {win2.count}")
    assert abs(win2.print_scale - 1.0583) < 1e-6 and win2.count == 3

    # ---- 10) 自动排版：切相纸 / 改张数 / 排满开关 ----
    # 先把校准系数与间隔复位（前面步骤留了 1.0583 / 2.5mm，会放大规格）
    win.scale_spin.setValue(1.0)
    win.spacing_spin.setValue(1.0)
    app.processEvents()
    win.template_combo.setCurrentIndex(win.template_combo.findData("auto"))
    app.processEvents()
    assert win.row_auto.isVisible(), "自动排版参数行应可见"
    win.paper_combo.setCurrentIndex(win.paper_combo.findData("6r_portrait"))
    win.auto_count_spin.setValue(11)
    app.processEvents()
    win.generate_layout()
    app.processEvents()
    ok, bad = in_safe(win, win.result)
    print(f"  自动排版 6R竖版 11 张 → {len(win.result.placements)} 张 "
          f"画布 {win.result.canvas.size} 边界 {'OK' if ok else bad}")
    assert ok, f"自动排版越界 {bad}"
    assert len(win.result.placements) == 11

    win.paper_combo.setCurrentIndex(win.paper_combo.findData("a4_portrait"))
    app.processEvents()
    win.generate_layout()
    app.processEvents()
    ok, bad = in_safe(win, win.result)
    print(f"  切到 A4 → 画布 {win.result.canvas.size} "
          f"{len(win.result.placements)} 张 边界 {'OK' if ok else bad}")
    assert win.result.canvas.size == (2480, 3508), f"A4 画布 {win.result.canvas.size}"
    assert ok, f"A4 越界 {bad}"

    win.auto_spec_combo.setCurrentIndex(win.auto_spec_combo.findData("custom"))
    app.processEvents()
    assert win.auto_custom_box.isVisible(), "自定义尺寸输入应可见"
    win.auto_custom_w_spin.setValue(30.0)
    win.auto_custom_h_spin.setValue(40.0)
    app.processEvents()
    win.generate_layout()
    app.processEvents()
    sizes = {(p.width, p.height) for p in win.result.placements}
    print(f"  自定义 30×40mm → 落位尺寸 {sizes}")
    assert sizes == {(354, 472)}, f"自定义尺寸落位 {sizes}"
    win.paper_combo.setCurrentIndex(win.paper_combo.findData("6r_portrait"))
    win.auto_spec_combo.setCurrentIndex(win.auto_spec_combo.findData("one_inch"))
    app.processEvents()

    # 回归防护：在自动排版里选过 A4 后，切回其它模板画布必须恢复原尺寸
    win.template_combo.setCurrentIndex(win.template_combo.findData("one_inch"))
    app.processEvents()
    win.generate_layout()
    app.processEvents()
    print(f"  切回一寸模板 → 画布 {win.result.canvas.size}（应为 1200×1800）")
    assert win.result.canvas.size == (1200, 1800), \
        f"相纸串到别的模板了：{win.result.canvas.size}"
    win.template_combo.setCurrentIndex(win.template_combo.findData("auto"))
    app.processEvents()

    # ---- 11) 换背景（已下沉到每张照片：可混排不同底色）----
    for p in win.photos:
        p.bg_mode, p.bg_color, p.bg_method = "solid", "#FFFFFF", "flood"
    win._bg_cache.clear()
    app.processEvents()
    win.generate_layout()
    app.processEvents()
    corner = win.result.canvas.getpixel((2, 2))
    print(f"  每张照片纯白底 → 画布角落 {corner}")
    assert corner == (255, 255, 255), f"背景色未生效 {corner}"
    assert all(p.has_custom_bg for p in win.photos)
    # 只给第一张换底：其余保留原图（混排不同底色）
    for p in win.photos[1:]:
        p.bg_mode = "keep"
    win._bg_cache.clear()
    win._update_info()
    win.generate_layout()
    app.processEvents()
    bgn = sum(1 for p in win.photos if p.has_custom_bg)
    print(f"  混排底色：{bgn} 张换底 / {len(win.photos) - bgn} 张原图")
    assert bgn == 1
    assert "已换底" in win.info_label.text(), "信息栏应显示换底张数"
    for p in win.photos:
        p.bg_mode = "keep"
    win._bg_cache.clear()
    app.processEvents()

    # ---- 12) 上传尺寸 + 单张导出 ----
    win.upload_w, win.upload_h = 300, 400
    win.upload_max_kb = 50
    win._update_upload_info()
    print("  上传尺寸:", win.upload_info_label.text())
    assert "300×400" in win.upload_info_label.text()
    # 导出单张会弹模态对话框，离屏测试里先屏蔽掉
    from PyQt6.QtWidgets import QMessageBox as _MB
    _orig_exec, _orig_crit = _MB.exec, _MB.critical
    _MB.exec = lambda self, *a, **k: 0
    _MB.critical = staticmethod(lambda *a, **k: 0)
    try:
        win.export_single()
    finally:
        _MB.exec, _MB.critical = _orig_exec, _orig_crit
    app.processEvents()
    out_dir = os.path.join(TMP, "上传单张")
    files = [f for f in os.listdir(out_dir) if f.lower().endswith(".jpg")] \
        if os.path.isdir(out_dir) else []
    print(f"  单张导出 → {out_dir} 共 {len(files)} 个文件")
    assert files, "单张导出应产生文件"
    from PIL import Image as _I
    with _I.open(os.path.join(out_dir, files[0])) as chk:
        print(f"  导出尺寸 {chk.size}")
        assert chk.size == (300, 400), f"单张导出尺寸 {chk.size}"

    # ---- 13) 结婚套装：上下间隔与左右间隔独立 ----
    print("\n---- 13) 结婚套装：左右间隔 / 照片间隔 独立生效 ----")
    win.template_combo.setCurrentIndex(win.template_combo.findData("wedding"))
    app.processEvents()
    assert win.gap_h_spin.isVisible(), "结婚套装应显示「左右间隔」"
    win.dist_combo.setCurrentIndex(win.dist_combo.findData("tight"))
    win.spacing_spin.setValue(1.0)
    app.processEvents()

    def _col_diffs():
        win.generate_layout()
        app.processEvents()
        xs = sorted({p.x for p in win.result.placements})
        return xs

    def _v_gap():
        """右列相邻两张之间的竖直间距。"""
        win.generate_layout()
        app.processEvents()
        res = win.result
        right = sorted([p for p in res.placements if p.width < 500],
                       key=lambda p: p.y)
        return right[1].y - (right[0].y + right[0].height)

    win.gap_h_spin.setValue(2.0)
    app.processEvents()
    xs_small = _col_diffs()
    v_small = _v_gap()
    win.gap_h_spin.setValue(10.0)
    app.processEvents()
    xs_big = _col_diffs()
    v_big = _v_gap()
    print(f"  左右间隔 2mm → 列 x={xs_small}；10mm → 列 x={xs_big}")
    assert (xs_big[-1] - xs_big[0]) > (xs_small[-1] - xs_small[0]), \
        "左右间隔滑块没有改变两列位置"
    assert v_small == v_big, f"左右间隔不应影响上下间距（{v_small} vs {v_big}）"
    # 照片间隔 → 只影响上下间距
    win.spacing_spin.setValue(4.0)
    app.processEvents()
    v4 = _v_gap()
    print(f"  照片间隔 1mm → 上下间距 {v_small}px；4mm → {v4}px")
    assert v4 > v_small, f"照片间隔滑块没有改变上下间距（{v_small} → {v4}）"
    win.spacing_spin.setValue(1.0)
    win.gap_h_spin.setValue(9.5)
    app.processEvents()

    # 切到别的模板 → 「左右间隔」应隐藏
    win.template_combo.setCurrentIndex(win.template_combo.findData("one_inch"))
    app.processEvents()
    assert not win.gap_h_spin.isVisible(), "非结婚套装不应显示「左右间隔」"
    print("  非结婚套装 → 「左右间隔」已隐藏")

    # ---- 14) 统一裁剪对话框（三条路：CUSTOM / TEMPLATE / SETTINGS）----
    print("\n---- 14) 统一裁剪对话框 ----")
    img = Image.new("RGB", (900, 1400), (200, 160, 130))
    dlg = ui.CropToolDialog(img, 295 / 413, preset_key=ui.PRESET_DEFAULT,
                            width_px=295, height_px=413, parent=win,
                            mode=ui.CropToolDialog.MODE_CUSTOM)
    # 自定义裁剪：尺寸可改、有预览/导出、画布可自由拖动
    assert dlg.canvas is not None and not dlg.canvas._read_only
    assert dlg.w_spin.isVisibleTo(dlg) and dlg.preview_btn.isVisibleTo(dlg)
    idx = next(i for i in range(dlg.preset_combo.count())
               if dlg.preset_combo.itemData(i) == "two_inch")
    dlg.preset_combo.setCurrentIndex(idx)
    s = dlg.settings()
    print(f"  [自定义] 选「二寸」预设 → {s.width_px}×{s.height_px}px")
    assert (s.width_px, s.height_px) == (413, 579), f"二寸预设 {s.width_px}×{s.height_px}"

    dlg.preset_combo.setCurrentIndex(dlg.preset_combo.findData(ui.PRESET_CUSTOM))
    dlg.w_spin.setValue(240)
    dlg.h_spin.setValue(320)
    s = dlg.settings()
    print(f"  [自定义] 240×320 → {s.width_px}×{s.height_px}px，比例 {s.width_px / s.height_px:.4f}")
    assert (s.width_px, s.height_px) == (240, 320)
    assert abs(dlg.canvas._ratio - 240 / 320) < 1e-6, "画布比例没有跟随自定义尺寸"
    # 导出载荷：含裁剪框 / 目标尺寸 / 体积上限
    payload = dlg.export_payload()
    assert payload["width_px"] == 240 and payload["height_px"] == 320
    assert payload["crop"] is not None and payload["image"] is img
    print(f"  [自定义] 导出载荷 → {payload['width_px']}×{payload['height_px']} "
          f"crop={tuple(round(v) for v in payload['crop'])}")
    dlg.close()

    # 预设分组齐全（证件照 / 考试系统 / 驾照）
    groups = {dlg.preset_combo.itemData(i) for i in range(dlg.preset_combo.count())}
    for key in ("one_inch", "exam_300x400", "license_22x32"):
        assert key in groups, f"缺少尺寸预设 {key}"
    print(f"  尺寸预设共 {len(groups) - 2} 个（含证件照 / 考试系统 / 驾照）")

    # 单位切换不改变实际像素尺寸
    dlg.preset_combo.setCurrentIndex(dlg.preset_combo.findData(ui.PRESET_CUSTOM))
    dlg.unit_combo.setCurrentIndex(dlg.unit_combo.findData("mm"))
    s_mm = dlg.settings()
    print(f"  切成 mm → {s_mm.width_px}×{s_mm.height_px}px（应仍是 240×320）")
    assert (s_mm.width_px, s_mm.height_px) == (240, 320), \
        f"换单位后尺寸漂移：{s_mm.width_px}×{s_mm.height_px}"
    dlg.close()

    # 图片列表裁剪：画布只读、不给改尺寸/限制、无导出按钮
    dlg2 = ui.CropToolDialog(img, 295 / 413, preset_key=ui.PRESET_DEFAULT,
                             width_px=295, height_px=413, parent=win,
                             mode=ui.CropToolDialog.MODE_TEMPLATE)
    assert dlg2.canvas is not None and dlg2.canvas._read_only, "模板裁剪画布应只读"
    assert not dlg2.maxkb_spin.isVisibleTo(dlg2), "模板裁剪不给设体积上限"
    assert not dlg2.export_btn.isVisibleTo(dlg2), "模板裁剪没有「导出单张」"
    assert not dlg2.preview_btn.isVisibleTo(dlg2), "模板裁剪没有「预览成片」"
    assert dlg2.face_mode_combo.isVisibleTo(dlg2), "模板裁剪保留人脸检测"
    print("  [模板] 画布只读、无尺寸/限制/导出、保留人脸检测：OK")
    dlg2.close()

    # 尺寸/限制设置：连画布都没有
    dlg3 = ui.CropToolDialog(None, 295 / 413, parent=win,
                             mode=ui.CropToolDialog.MODE_SETTINGS)
    assert dlg3.canvas is None, "设置模式不应有画布"
    assert dlg3.preset_combo.isVisibleTo(dlg3) and dlg3.face_mode_combo.isVisibleTo(dlg3)
    print("  [设置] 无画布、只有尺寸/限制/检测：OK")
    dlg3.close()

    # 单张照片的自定义尺寸 → 覆盖原本要求的尺寸
    win.photos[0].target_size = (240, 320)
    size = win._upload_size_for(win.photos[0])
    print(f"  单张覆盖尺寸 → {size}")
    assert size == (240, 320), f"单张目标尺寸未生效 {size}"
    win.photos[0].target_size = None
    assert win._upload_size_for(win.photos[0]) == win._upload_size()

    print("UI 冒烟测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
