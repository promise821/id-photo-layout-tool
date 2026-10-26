# -*- coding: utf-8 -*-
"""
build.py —— PyInstaller 打包脚本：把 GUI 打成单文件 exe（自带图标）。

用法：
    py -m pip install pyinstaller
    py build.py

产物：dist/证件照排版工具.exe
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "证件照排版工具"
ICON = os.path.join(HERE, "icon.ico")

# 排除用不到的重型 Qt 模块，体积能砍掉一大半
# 注意：numpy 不能排除 —— photo_tools 的渐变/漫水算法依赖它；
#      opencv-python 是可选增强（缺失时自动退回纯 numpy 漫水算法），
#      PyInstaller 会自动收集，无需手写 hiddenimport。
EXCLUDES = [
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebEngine",
    "PyQt6.QtQuick", "PyQt6.QtQml", "PyQt6.Qt3DCore", "PyQt6.Qt3DRender",
    "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets", "PyQt6.QtBluetooth",
    "PyQt6.QtNetworkAuth", "PyQt6.QtPositioning", "PyQt6.QtSql", "PyQt6.QtTest",
    "PyQt6.QtDesigner", "PyQt6.QtHelp", "PyQt6.QtCharts", "PyQt6.QtDataVisualization",
    "PyQt6.QtPdf", "PyQt6.QtPdfWidgets", "PyQt6.QtSerialPort", "PyQt6.QtSensors",
    "tkinter", "unittest", "pydoc", "matplotlib", "pytest",
]


def ensure_icon() -> str:
    """图标不存在时自动生成。"""
    if not os.path.exists(ICON):
        print("未找到 icon.ico，正在生成……")
        subprocess.call([sys.executable, os.path.join(HERE, "make_icon.py")], cwd=HERE)
    return ICON


def pick_distpath() -> str:
    """选一个能写入的产物目录。

    上一次留下的 exe 可能正被资源管理器预览或杀毒软件占用，
    直接覆盖会报「拒绝访问」。这时退到 dist_new，等占用解除再搬回去。
    """
    dist = os.path.join(HERE, "dist")
    os.makedirs(dist, exist_ok=True)
    old = os.path.join(dist, f"{APP_NAME}.exe")
    if os.path.exists(old):
        try:
            os.remove(old)
        except OSError as exc:
            alt = os.path.join(HERE, "dist_new")
            os.makedirs(alt, exist_ok=True)
            print(f"旧产物被占用（{exc}），本次改输出到 {alt}")
            return alt
    return dist


def main() -> int:
    icon = ensure_icon()
    distpath = pick_distpath()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--windowed",
        "--name", APP_NAME,
        "--icon", icon,
        "--add-data", f"{icon};.",
        "--distpath", distpath,
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", os.path.join(HERE, "build"),
    ]
    # 模型文件必须随包带走：YuNet/Haar(人脸检测) 与 MODNet(神经网络抠图)
    # 都放在 models/，运行时由 face_tools / photo_tools 在 _MEIPASS/models 下找到。
    # onnxruntime 是可选增强（缺失时抠图自动退回 GrabCut → 漫水），PyInstaller
    # 会自动收集；onnxruntime 版本需 <1.31（1.31 在部分机器上 import 即崩）。
    models_dir = os.path.join(HERE, "models")
    if os.path.isdir(models_dir):
        cmd += ["--add-data", f"{models_dir}{os.pathsep}models"]
    else:
        print("警告：未找到 models/ 目录，exe 里的自动裁剪将退回无模型模式")
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    cmd.append(os.path.join(HERE, "main.py"))

    print("$", " ".join(cmd))
    code = subprocess.call(cmd, cwd=HERE)
    if code != 0:
        print("打包失败")
        return code

    exe = os.path.join(distpath, f"{APP_NAME}.exe")
    if os.path.exists(exe):
        size = os.path.getsize(exe) / 1024 / 1024
        print(f"\n✅ 打包完成：{exe}  ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
