# -*- coding: utf-8 -*-
"""
make_icon.py —— 生成应用图标（icon.ico / icon.png）。

用 PIL 以 4× 超采样绘制后缩放，保证 16px 小图标依然清晰：
    · 蓝色圆角渐变底板
    · 白色 6R 竖版相纸
    · 相纸上 3 张双人照（左）+ 3 张单人照（右），与实际排版一致
    · 左上角加一个人像剪影，一眼能看出是"证件照"

用法：py make_icon.py
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFilter

S = 1024                     # 超采样画布
OUT = os.path.dirname(os.path.abspath(__file__))


def lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def make_icon() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # ---------- 底板：竖向渐变 + 圆角 ----------
    grad = Image.new("RGB", (1, S))
    for y in range(S):
        t = y / (S - 1)
        grad.putpixel((0, y), (lerp(96, 26, t), lerp(150, 74, t), lerp(252, 208, t)))
    grad = grad.resize((S, S))
    img.paste(grad, (0, 0), rounded_mask(S, 220))

    # 顶部高光
    gloss = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(gloss).ellipse([-S // 3, -S // 2, S + S // 3, S // 3],
                                  fill=(255, 255, 255, 46))
    gloss = gloss.filter(ImageFilter.GaussianBlur(90))
    img.alpha_composite(Image.composite(gloss, Image.new("RGBA", (S, S), (0, 0, 0, 0)),
                                        rounded_mask(S, 220)))

    d = ImageDraw.Draw(img)

    # ---------- 相纸阴影 ----------
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([226, 116, 798, 900], radius=52,
                                             fill=(10, 25, 60, 70))
    shadow = shadow.filter(ImageFilter.GaussianBlur(22))
    img.alpha_composite(shadow)

    # ---------- 白色相纸（6R 竖版）----------
    d.rounded_rectangle([212, 100, 784, 884], radius=52, fill=(255, 255, 255, 255))

    # ---------- 相纸内的照片 ----------
    photo_light = (226, 234, 246, 255)
    photo_mid = (198, 212, 232, 255)
    border = (214, 222, 234, 255)

    def frame(box: tuple[int, int, int, int], tone=photo_light) -> None:
        d.rounded_rectangle(box, radius=18, fill=tone, outline=border, width=6)

    # 左列：3 张双人照（横版）
    left_x0, left_x1 = 262, 512
    for top in (162, 372, 582):
        frame((left_x0, top, left_x1, top + 166), photo_light)

    # 右列：3 张单人照（横版）
    right_x0, right_x1 = 546, 734
    for top in (162, 372, 582):
        frame((right_x0, top, right_x1, top + 166), photo_mid)

    # ---------- 人像剪影（第一张双人照里）----------
    head_cx, head_cy, head_r = (left_x0 + left_x1) // 2, 218, 38
    d.ellipse([head_cx - head_r, head_cy - head_r, head_cx + head_r, head_cy + head_r],
              fill=(126, 149, 186, 255))
    d.rounded_rectangle([head_cx - 62, head_cy + 30, head_cx + 62, 330],
                        radius=58, fill=(126, 149, 186, 255))

    # ---------- 小高光装饰 ----------
    d.rounded_rectangle([262, 806, 512, 828], radius=11, fill=(226, 234, 246, 255))
    d.rounded_rectangle([546, 806, 640, 828], radius=11, fill=(226, 234, 246, 255))

    return img


def main() -> int:
    big = make_icon()
    png = big.resize((512, 512), Image.Resampling.LANCZOS)
    png_path = os.path.join(OUT, "icon.png")
    png.save(png_path)
    print("已生成:", png_path)

    ico_path = os.path.join(OUT, "icon.ico")
    big.save(ico_path, format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                    (128, 128), (256, 256)])
    print("已生成:", ico_path)

    # 预览图
    sheet = Image.new("RGB", (560, 290), (244, 246, 250))
    for i, size in enumerate((16, 24, 32, 48, 64, 128)):
        icon = big.resize((size, size), Image.Resampling.LANCZOS)
        x = 20 + i * 90
        sheet.paste(icon, (x, 40 + (128 - size) // 2), icon)
    sheet.save(os.path.join(OUT, "_selftest", "icon_sizes.png"))
    print("已生成尺寸预览: _selftest/icon_sizes.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
