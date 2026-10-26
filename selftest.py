# -*- coding: utf-8 -*-
"""引擎自检：尺寸表、归类、各模板版面、一寸多人规则、双人单独排版、
混排装箱 + 旋转、**边界硬约束**、溢出提示、校准、DPI。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw
import face_tools
import layout_engine as engine
import photo_tools
from auto_layout import plan_auto_layout
from config import (BLEED, CAT_COUPLE, CAT_P1, CAT_P2, CAT_P3, MARGIN,
                     MIX_DEFAULT, MIX_KEYS, PAPER_SIZES, SPECS, TEMPLATES, WEDDING_TEMPLATE,
                     cm_to_px)

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("    ✗ " + msg)
    return cond


def make_sample(path, w, h, color, text):
    img = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w - 1, h - 1], outline=(255, 255, 255), width=8)
    d.text((20, 20), text, fill=(255, 255, 255))
    img.save(path)
    return img


def bounds_ok(eng, res):
    """所有照片必须完整落在安全区（出血线 + 1mm 内缩）内。"""
    left = eng.bleed + eng.margin
    right = res.canvas.width - left
    bottom = res.canvas.height - left
    bad = [p for p in res.placements
           if p.x < left or p.y < left or p.x + p.width > right or p.y + p.height > bottom]
    return not bad, (bad[0].box if bad else None), (left, right, bottom)


def main():
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest")
    os.makedirs(tmp, exist_ok=True)

    couple = [make_sample(os.path.join(tmp, f"c{i}.jpg"), 1600, 1200, (198, 96, 96), f"c{i}") for i in range(3)]
    male = [make_sample(os.path.join(tmp, f"m{i}.jpg"), 900, 1400, (86, 130, 200), f"m{i}") for i in range(3)]
    female = [make_sample(os.path.join(tmp, f"f{i}.jpg"), 1000, 1500, (214, 132, 176), f"f{i}") for i in range(3)]
    third = [make_sample(os.path.join(tmp, f"t{i}.jpg"), 1000, 1500, (150, 200, 150), f"t{i}") for i in range(3)]

    fake = {CAT_COUPLE: couple, CAT_P1: male, CAT_P2: female, CAT_P3: third}

    # ---------- 1) 尺寸表（对照《证件照规格尺寸》）----------
    print("=== 1) 尺寸表 @300DPI ===")
    expect = {"tiny_one_inch": (2.2, 3.2), "one_inch": (2.5, 3.5), "big_one_inch": (3.3, 4.8),
              "small_two_inch": (3.5, 4.5), "two_inch": (3.5, 4.9), "big_two_inch": (3.5, 5.3),
              "couple": (5.3, 3.5)}
    for key, (wcm, hcm) in expect.items():
        s = SPECS[key]
        check(abs(s.width_px - cm_to_px(wcm)) <= 1 and abs(s.height_px - cm_to_px(hcm)) <= 1,
              f"{key} 尺寸不符：{s.width_px}x{s.height_px}")
        print(f"  {key:15s} {s.width_px:4d}x{s.height_px:<4d} = {s.cm_text}")

    # ---------- 2) 自动归类 ----------
    print("\n=== 2) 自动归类 ===")
    for size, want in [((1600, 1200), "couple"), ((900, 1400), "p1"),
                       ((1200, 1600), "p1"), ((2000, 2000), "p1")]:
        got = engine.classify_by_size(size)
        check(got == want, f"归类 {size} → {got}，应为 {want}")
        print(f"    {size} → {got}")

    # ---------- 3) 结婚套装（两列上对齐 + 上下/左右间距独立）----------
    print("\n=== 3) 结婚套装（两列上对齐 + 上下/左右间距独立）===")
    eng = engine.LayoutEngine(WEDDING_TEMPLATE, gap_h=60)
    res = eng.build(fake)
    edge = eng.bleed + eng.margin                       # 出血线 + 1mm 内缩
    avail_w = 1200 - 2 * edge
    left_w = SPECS["couple"].width_px
    right_w = SPECS["single"].height_px                # 单人照横躺后是 413 宽
    gap_h = 60
    block_w = left_w + gap_h + right_w
    x_left = edge + (avail_w - block_w) // 2            # 整块横向居中
    x_right = x_left + left_w + gap_h
    left_col = [p for p in res.placements if p.x == x_left]
    right_col = [p for p in res.placements if p.x == x_right]
    check([p.slot_key for p in left_col] == [CAT_COUPLE] * 3 + [CAT_P2], "结婚套装左列")
    check([p.slot_key for p in right_col] == [CAT_P1] * 3 + [CAT_P2] * 2, "结婚套装右列")
    check(len(left_col) == 4 and len(right_col) == 5,
          f"结婚套装列数 左{len(left_col)}/右{len(right_col)}")
    # 两列顶部对齐
    check(left_col[0].y == right_col[0].y,
          f"两列未上对齐 左{left_col[0].y} 右{right_col[0].y}")
    # 同一列内照片间距必须相等（整数间隔，完全相等）
    for name, col in (("左", left_col), ("右", right_col)):
        gaps = [col[i + 1].y - (col[i].y + col[i].height) for i in range(len(col) - 1)]
        check(len(set(gaps)) == 1 and (not gaps or gaps[0] >= 0),
              f"{name}列间距不等：{gaps}")
    # 末张不再吸附右下角（避免与其它照片不等距）
    last = right_col[-1]
    check(last.y + last.height < 1800 - edge,
          f"末张仍贴底 y={last.y}（应留出间距）")
    # 上下间隔与左右间隔互不相同也要成立
    v_gap = right_col[1].y - (right_col[0].y + right_col[0].height)
    check(v_gap != gap_h, f"上下间隔({v_gap})不应等于左右间隔({gap_h})")
    ok_b, box, area = bounds_ok(eng, res)
    check(ok_b, f"结婚套装越界 {box}")
    print(f"  {len(res.placements)} 张；左列 x={x_left} / 右列 x={x_right}，"
          f"左右间隔 {gap_h}px，上下间隔 {v_gap}px，两列顶部 y={left_col[0].y}，"
          f"末张 y={last.y}（未贴底），边界 OK")

    # ---------- 3b) 左右间隔放不下时 → 压到能放下的最大值 ----------
    print("\n=== 3b) 左右间隔过大 → 压到最大值（不重叠、不越界）===")
    eng2 = engine.LayoutEngine(WEDDING_TEMPLATE, gap_h=9999)
    res2 = eng2.build(fake)
    max_gap = avail_w - left_w - right_w
    xs = sorted({p.x for p in res2.placements})
    check(all(x >= edge for x in xs), f"列越界 {xs}")
    ok_b2, box2, _ = bounds_ok(eng2, res2)
    check(ok_b2, f"左右压到最大仍越界 {box2}")
    print(f"  请求 9999px → 实际压到 {max_gap}px，列 x={xs}，边界 OK")

    # ---------- 3c) 「照片间隔」滑块必须真的影响上下间距 ----------
    print("\n=== 3c) 照片间隔滑块 → 上下间距（与左右间隔独立）===")
    avail_h = 1800 - 2 * edge
    left_sum = 3 * SPECS["couple"].height_px + SPECS["one_inch"].width_px
    right_sum = 5 * SPECS["one_inch"].width_px
    gap_limit = int((avail_h - left_sum) / 3)          # 左列先到顶（4 张 → 3 个缝）
    gap_limit = min(gap_limit, int((avail_h - right_sum) / 4))
    for spacing in (1.0, 4.0, 8.0):
        eng3 = engine.LayoutEngine(WEDDING_TEMPLATE, gap=int(round(spacing * 300 / 25.4)),
                                   gap_h=60)
        res3 = eng3.build(fake, {"distribution": "tight"})
        right3 = [p for p in res3.placements if p.x == x_right]
        vgap = right3[1].y - (right3[0].y + right3[0].height)
        request = int(round(spacing * 300 / 25.4))
        expect_gap = min(request, gap_limit)           # 放不下就压到能放下的最大值
        check(vgap == expect_gap,
              f"照片间隔 {spacing}mm → 上下间距 {vgap}px（应为 {expect_gap}px）")
        # 左右间隔不受上下间隔影响
        check(right3[0].x == x_right, f"左右间隔被上下间隔影响：x={right3[0].x}")
        note = "（已压到最大值）" if request > gap_limit else ""
        print(f"  照片间隔 {spacing:>4g}mm → 上下间距 {vgap}px{note}，左右间隔 {gap_h}px 不变")

    # ---------- 4) 一寸：先满 9 竖，剩余进底部横版 ----------
    print("\n=== 4) 一寸多人（先满 9 竖 + 底部横版）===")
    for people, per, want_total, want_port in ((1, 11, 11, 9), (2, 5, 10, 9),
                                                (3, 4, 11, 9), (3, 3, 9, 9), (2, 6, 11, 9)):
        eng = engine.LayoutEngine(TEMPLATES["one_inch"])
        res = eng.build(fake, {"people": people, "per_person": per})
        port = sum(1 for p in res.placements if p.height > p.width)
        land = sum(1 for p in res.placements if p.height <= p.width)
        counts = {}
        for p in res.placements:
            counts[p.slot_key] = counts.get(p.slot_key, 0) + 1
        ok_b, box, _ = bounds_ok(eng, res)
        check(len(res.placements) == want_total, f"{people}人×{per} 共 {len(res.placements)} 张")
        check(port == want_port, f"{people}人×{per} 竖版 {port} 张")
        check(ok_b, f"{people}人×{per} 越界 {box}")
        if people == 3 and per == 4:
            check(counts.get(CAT_P3) == 3, f"3人×4 最后一人应为 3 张，实为 {counts.get(CAT_P3)}")
        print(f"  {people}人×{per} → 竖 {port} / 横 {land}，共 {len(res.placements)} 张 {counts} 边界OK")

    # ---------- 5) 各尺寸模板：张数 + 横版开关 + 边界 ----------
    print("\n=== 5) 各尺寸模板（张数 / 用谁 / 底部横版 / 边界）===")
    for key in ("tiny_one_inch", "big_one_inch", "small_two_inch", "two_inch", "big_two_inch"):
        t = TEMPLATES[key]
        # 满张数（模板标称上限）
        eng = engine.LayoutEngine(t)
        res = eng.build(fake, {"count": t.max_count, "landscape": False,
                               "count_person": CAT_P2})
        ok_b, box, _ = bounds_ok(eng, res)
        check(len(res.placements) == t.max_count, f"{key} 满张 {len(res.placements)}/{t.max_count}")
        check(all(p.slot_key == CAT_P2 for p in res.placements), f"{key} 用谁未生效")
        check(ok_b, f"{key} 满张越界 {box}")
        # 加底部横版
        eng2 = engine.LayoutEngine(t)
        res2 = eng2.build(fake, {"count": t.default_count, "landscape": True,
                                  "count_person": CAT_P1})
        ok_b2, box2, _ = bounds_ok(eng2, res2)
        n_land = sum(1 for p in res2.placements if p.slot_key == CAT_COUPLE)
        check(n_land == 1, f"{key} 底部横版 {n_land}")
        check(ok_b2, f"{key} 带横版越界 {box2}")
        print(f"  {key:15s} 满 {t.max_count} 张 OK；默认 {t.default_count} 张 + 横版 "
              f"= {len(res2.placements)} 张，边界 OK")

    # ---------- 6) 结婚照双人（单独排版）----------
    print("\n=== 6) 结婚照双人单独排版 ===")
    for n in range(1, 7):
        eng = engine.LayoutEngine(TEMPLATES["couple_only"])
        res = eng.build(fake, {"count": n})
        ok_b, box, area = bounds_ok(eng, res)
        rot = sum(1 for p in res.placements if p.height > p.width)   # 被转成竖版的
        total_h = (max(p.y + p.height for p in res.placements)
                   - min(p.y for p in res.placements)) if res.placements else 0
        check(ok_b, f"双人 {n} 张越界 {box}（安全区 {area}）")
        if n <= 5:
            check(len(res.placements) == n, f"双人 {n} 张只排了 {len(res.placements)}")
            check(rot == (2 if n == 5 else 0), f"双人 {n} 张应有 {2 if n == 5 else 0} 张转90°，实为 {rot}")
        else:
            check(len(res.placements) == 5 and res.warnings, f"双人 6 张应排 5 并提示")
        print(f"  {n} 张 → 排入 {len(res.placements)}（转90° {rot}），块高 {total_h}px，"
              f"边界 {'OK' if ok_b else 'FAIL'} {res.warnings[:1]}")

    # ---------- 7) 合并排版：六尺寸混排 / 每规格指定人物 / 旋转 / 边界 ----------
    print("\n=== 7) 合并排版 ===")
    mix_all = dict(zip(MIX_KEYS, (1, 1, 1, 1, 1, 1)))
    eng = engine.LayoutEngine(TEMPLATES["mixed"])
    res = eng.build(fake, mix_all)
    ok_b, box, _ = bounds_ok(eng, res)
    check(len(res.placements) == 6, f"六尺寸各 1 张 → {len(res.placements)}")
    check(ok_b, f"六尺寸混排越界 {box}")
    sizes = sorted({(p.width, p.height) for p in res.placements})
    print(f"  六尺寸各1张 → {len(res.placements)} 张，落位尺寸 {sizes} 边界OK")

    # 每种规格指定人物
    eng = engine.LayoutEngine(TEMPLATES["mixed"])
    res = eng.build(fake, {**mix_all,
                           "one_inch_person": CAT_P1, "two_inch_person": CAT_P2,
                           "big_two_inch_person": CAT_P3})
    by_size = {}
    for p in res.placements:
        by_size.setdefault((p.width, p.height), set()).add(p.slot_key)
    one = SPECS["one_inch"]; two = SPECS["two_inch"]; big = SPECS["big_two_inch"]
    check(by_size.get((one.width_px, one.height_px)) == {CAT_P1}, "一寸应为人1")
    check(by_size.get((two.width_px, two.height_px)) == {CAT_P2}, "二寸应为人2")
    # 大二寸可能被自动旋转成 626×413，两个方向都算它
    big_set = (by_size.get((big.width_px, big.height_px), set())
               | by_size.get((big.height_px, big.width_px), set()))
    check(big_set == {CAT_P3}, f"大二寸应为人3，实为 {big_set}")
    print("  指定人物 →", {k: sorted(v) for k, v in by_size.items()})

    # 高度不足自动旋转
    eng = engine.LayoutEngine(TEMPLATES["mixed"])
    res = eng.build(fake, {"two_inch": 6})
    rot = [p for p in res.placements if p.width > p.height]
    check(len(res.placements) + len([w for w in res.warnings if "跳过" in w]) == 6, "二寸6张账目")
    check(len(rot) >= 1, "应有自动旋转")
    ok_b, box, _ = bounds_ok(eng, res)
    check(ok_b, f"旋转后越界 {box}")
    print(f"  二寸×6 → 排入 {len(res.placements)}，横躺 {len(rot)}，提示 {res.warnings[:1]}")

    # 默认混排（3+2+2）
    eng = engine.LayoutEngine(TEMPLATES["mixed"])
    res = eng.build(fake, dict(zip(MIX_KEYS, MIX_DEFAULT)))
    ok_b, _, _ = bounds_ok(eng, res)
    check(len(res.placements) == 7 and ok_b, f"默认混排 {len(res.placements)} 张")
    print(f"  默认 3+2+2 → {len(res.placements)} 张 边界OK")

    # ---------- 8) 边界硬约束：大间隔 / 大校准也不能越界 ----------
    print("\n=== 8) 边界硬约束（极端参数不许越界）===")
    cases = [
        ("一寸11张+间隔10mm", "one_inch", 118, 1.0, {"people": 1, "per_person": 11}),
        ("一寸11张+间隔20mm", "one_inch", 236, 1.0, {"people": 1, "per_person": 11}),
        ("二寸4张+校准1.25", "two_inch", 12, 1.25, {"count": 4, "landscape": False}),
        ("大二寸4张+校准1.30", "big_two_inch", 12, 1.30, {"count": 4, "landscape": False}),
        ("小一寸16张+校准1.2", "tiny_one_inch", 12, 1.20, {"count": 16, "landscape": False}),
        ("双人4张+间隔10mm", "couple_only", 118, 1.0, {"count": 4}),
        ("混排满载+间隔8mm", "mixed", 94, 1.0, dict(zip(MIX_KEYS, (2, 3, 2, 2, 2, 1)))),
        ("结婚套装+校准1.30", "wedding", 12, 1.30, {}),
        ("结婚套装+校准1.15", "wedding", 12, 1.15, {}),
        ("结婚套装+校准1.0583", "wedding", 12, 1.0583, {}),
    ]
    for label, key, gap, scale, params in cases:
        eng = engine.LayoutEngine(TEMPLATES[key], gap=gap, print_scale=scale)
        res = eng.build(fake, params)
        ok_b, box, area = bounds_ok(eng, res)
        check(ok_b, f"{label} 越界 {box}，安全区 {area}")
        print(f"  {label:22s} → {len(res.placements):2d} 张，"
              f"间隔 {res.used_spacing}px，边界 {'OK' if ok_b else 'FAIL'}"
              f"{'，提示: ' + res.warnings[0][:34] if res.warnings else ''}")

    # 结婚套装在真实校准系数（≈1.06）下必须 9 张全部排下
    eng = engine.LayoutEngine(TEMPLATES["wedding"], print_scale=1.0583)
    res = eng.build(fake)
    ok_b, box, _ = bounds_ok(eng, res)
    check(len(res.placements) == 9 and not res.warnings and ok_b,
          f"结婚套装@1.0583 → {len(res.placements)} 张，警告 {res.warnings[:1]}")
    print(f"  结婚套装@校准1.0583 → {len(res.placements)} 张，无警告，边界 "
          f"{'OK' if ok_b else 'FAIL'}")

    # ---------- 9) 打印校准 ----------
    PRINTABLE_CM = 9.6
    k = PRINTABLE_CM / 10.16
    base = engine.LayoutEngine(WEDDING_TEMPLATE).build(fake)
    cal = engine.LayoutEngine(WEDDING_TEMPLATE, print_scale=1 / k).build(fake)
    cp = next(p for p in base.placements if p.slot_key == CAT_COUPLE)
    cp2 = next(p for p in cal.placements if p.slot_key == CAT_COUPLE)
    print("\n=== 9) 打印校准（冲印机缩到 9.6cm）===")
    print(f"  未校准 {cp.width/1200*PRINTABLE_CM:.2f}cm → 校准后 "
          f"{cp2.width/1200*PRINTABLE_CM:.2f}cm（标称 5.3）")
    check(abs(cp2.width / 1200 * PRINTABLE_CM - 5.3) < 0.05, "校准后尺寸")

    # ---------- 10) 导出 DPI ----------
    out = os.path.join(tmp, "合并排版_6R.jpg")
    eng = engine.LayoutEngine(TEMPLATES["mixed"])
    res = eng.build(fake, dict(zip(MIX_KEYS, (1, 3, 1, 1, 1, 1))))
    engine.export_jpeg(res.canvas, out, placements=res.placements, bleed_box=res.bleed_box)
    with Image.open(out) as chk:
        print(f"\n=== 10) 导出 ===  size={chk.size} dpi={chk.info.get('dpi')}")
        check(chk.size == (1200, 1800), "导出尺寸")
        check(tuple(chk.info.get("dpi", (0, 0))) == (300, 300), "导出 DPI")

    # ---------- 11) 自动排版（相纸 → 可用区 → 张数 → 间隔 → 竖版/横版）----------
    print("\n=== 11) 自动排版 ===")
    auto_t = TEMPLATES["auto"]
    for paper_key, count in (("6r_portrait", 4), ("6r_portrait", 11),
                             ("6r_portrait", 30), ("a4_portrait", 8),
                             ("5r_landscape", 11)):
        eng = engine.LayoutEngine(auto_t, gap=12, margin=12, bleed=12)
        res = eng.build(fake, {"paper_key": paper_key, "auto_count": count,
                               "auto_spec": "one_inch", "auto_fill": True,
                               "auto_rotate": True, "auto_person": CAT_P1})
        plan = eng._auto_plan
        ok_b, box, area = bounds_ok(eng, res)
        check(plan is not None, f"自动排版 {paper_key}/{count} 无方案")
        if plan is None:
            continue
        check(ok_b, f"自动排版 {paper_key}/{count} 越界 {box}")
        check(len(res.placements) == plan.count,
              f"自动排版 {paper_key}/{count} 落位 {len(res.placements)} ≠ 方案 {plan.count}")
        # 相纸尺寸必须真的换掉
        paper = PAPER_SIZES[paper_key]
        check((res.canvas.width, res.canvas.height) == (paper.width_px, paper.height_px),
              f"自动排版画布 {res.canvas.size} ≠ 相纸 {paper.size_text}")
        print(f"  {paper_key:15s} n={count:2d} → {plan.cols}列×{plan.rows}行 "
              f"共{plan.count}张 rot={plan.rotated} gap=({plan.gap_x:.0f},"
              f"{plan.gap_y:.0f}) 画布{res.canvas.size} 边界OK")

    # 排不满 → 自动减一张再重算（间隔 20mm 时 11 张放不下）
    eng = engine.LayoutEngine(auto_t, gap=236, margin=12, bleed=12)
    res = eng.build(fake, {"paper_key": "6r_portrait", "auto_count": 11,
                           "auto_spec": "one_inch", "auto_fill": True,
                           "auto_rotate": True})
    plan = eng._auto_plan
    check(plan is not None and plan.count < 11,
          f"大间隔时应自动减张，实为 {plan.count if plan else None}")
    check(any(("减" in w) or ("上限" in w) for w in res.warnings),
          f"减张应有提示，实为 {res.warnings}")
    print(f"  间隔20mm 请求11张 → 自动减为 {plan.count if plan else 0} 张"
          f"（提示：{res.warnings[0][:28] if res.warnings else '无'}）")

    # 竖版优先；竖版放不下时自动横躺（旋转 90°），禁用旋转则排得更少
    p5 = PAPER_SIZES["5r_landscape"]
    uw5 = p5.width_px - 2 * 24
    uh5 = p5.height_px - 2 * 24
    r_p = plan_auto_layout(11, 295, 413, uw5, uh5, 12, True, True)
    r_l = plan_auto_layout(11, 295, 413, uw5, uh5, 12, True, False)
    check(r_p.plan is not None and r_p.plan.rotated,
          f"5R横版放不下应自动横躺，rot={r_p.plan.rotated if r_p.plan else None}")
    check(r_l.plan is None or (r_p.plan and r_l.plan.count < r_p.plan.count),
          "禁用旋转后排得应更少")
    print(f"  竖版优先：5R横版 允许旋转 → rot={r_p.plan.rotated if r_p.plan else None}"
          f"（{r_p.plan.count if r_p.plan else 0} 张）；禁用旋转 → "
          f"{r_l.plan.count if r_l.plan else 0} 张")

    # 其它模板不受相纸影响（回归防护：相纸只能作用在自动排版模板上）
    eng = engine.LayoutEngine(TEMPLATES["one_inch"], gap=12, margin=12, bleed=12)
    res = eng.build(fake, {"paper_key": "a4_portrait", "count": 4})
    check(res.canvas.size == (1200, 1800),
          f"非自动排版模板不应受相纸影响，画布 {res.canvas.size}")
    print(f"  相纸隔离：one_inch 模板传 a4_portrait → 画布仍为 {res.canvas.size}")

    # ---------- 12) 换背景 + 尺寸/体积限制 ----------
    print("\n=== 12) 换背景与尺寸限制 ===")
    bg = Image.new("RGB", (600, 800), (60, 90, 200))
    d = ImageDraw.Draw(bg)
    d.rectangle([250, 340, 350, 460], fill=(20, 20, 20))
    out_img = photo_tools.replace_background(bg, (255, 255, 255),
                                             photo_tools.BG_SOLID,
                                             method=photo_tools.METHOD_FLOOD)
    check(out_img.size == bg.size, "换背景后尺寸变了")
    check(out_img.getpixel((5, 5)) == (255, 255, 255),
          f"换背景后角落不是白色 {out_img.getpixel((5, 5))}")
    check(out_img.getpixel((300, 400)) == (20, 20, 20),
          f"换背景后人像被改动了 {out_img.getpixel((300, 400))}")
    print(f"  换背景：尺寸 {out_img.size}，角落 {out_img.getpixel((5,5))}，"
          f"人像 {out_img.getpixel((300,400))}")

    data, q, scale = photo_tools.fit_jpeg(bg, max_kb=50)
    check(len(data) <= 50 * 1024, f"体积限制未生效 {len(data)}")
    single = os.path.join(tmp, "_selftest_single.jpg")
    info = photo_tools.export_single(bg, single, size_px=(295, 413), max_kb=100)
    with Image.open(single) as chk:
        check(chk.size == (295, 413), f"单张导出尺寸 {chk.size}")
    check(info["kb"] <= 100.5, f"单张导出版积 {info['kb']}KB")
    print(f"  单张导出 {info['width']}×{info['height']}px {info['kb']:.1f}KB "
          f"quality={info['quality']}")

    # ---------- 13) 人脸检测与自动裁剪框 ----------
    print("\n=== 13) 人脸检测与自动裁剪 ===")
    box, reason = face_tools.detect_face(bg, mode="opencv")
    check(reason != "", "人脸检测应给出说明")
    fb = face_tools.FaceBox(left=200, top=150, width=200, height=250)
    crop = face_tools.face_box_to_crop(fb, (800, 1000), 295 / 413)
    cw = (crop[2] - crop[0]) * 800
    ch = (crop[3] - crop[1]) * 1000
    check(0 <= crop[0] < crop[2] <= 1 and 0 <= crop[1] < crop[3] <= 1,
          f"裁剪框越界 {crop}")
    check(abs(cw / ch - 295 / 413) < 0.02,
          f"裁剪框比例 {cw/ch:.4f} ≠ {295/413:.4f}")
    check(box is None or isinstance(box, face_tools.FaceBox), "人脸框类型")
    print(f"  OpenCV 检测：{reason}")
    print(f"  自动裁剪框：{tuple(round(v, 4) for v in crop)} → "
          f"{cw:.0f}×{ch:.0f}px，比例 {cw/ch:.4f}")

    print("\n自检结果:", "通过 ✅" if not FAILS else f"失败 ❌ {len(FAILS)} 项")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
