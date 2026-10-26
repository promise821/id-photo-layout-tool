# -*- coding: utf-8 -*-
"""face_tools.py —— 人脸检测与自动裁剪框计算。

对外接口：
    detect_face(image, mode, ...)      -> (FaceBox | None, 说明文字)
    face_box_to_crop(box, size, ...)   -> 归一化裁剪框 (left, top, right, bottom)
    auto_crop_box(image, ratio, ...)   -> (归一化裁剪框 | None, 说明文字)

检测方式（mode）：
    "auto"    有 Face++ 密钥时优先云端，失败/无结果回退本地 OpenCV；
    "opencv"  完全离线，不需要任何密钥；
    "facepp"  只用 Face++ 云端；
    "none"    不检测。

安全约定：api_key / api_secret 只用于请求本身，绝不出现在任何返回文本、
异常信息或日志里（项目安全要求）。
"""

from __future__ import annotations

import base64
import io
import os
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

from PIL import Image

# 归一化裁剪框 (left, top, right, bottom)，取值 0~1，相对原图
CropBox = Tuple[float, float, float, float]

# Face++ 中国区检测接口
FACEPP_URL = "https://api-cn.faceplusplus.com/facepp/v3/detect"

# 本地检测模型（放在 models/ 目录，随程序一起打包）
#   · YuNet：DNN 人脸检测，精度高、能认侧脸，证件照首选；
#   · Haar：传统级联，作为 YuNet 不可用时的兜底。
YUNET_MODEL = "face_detection_yunet_2023mar.onnx"
HAAR_CASCADES = ("haarcascade_frontalface_default.xml",
                 "haarcascade_frontalface_alt2.xml",
                 "haarcascade_profileface.xml")

# 检测前把长边缩到不超过该像素数：Haar 级联在千像素级最快，且足够准
MAX_DETECT_SIDE = 1000

# YuNet 检测参数：阈值放低一些（证件照基本是单人正面，误检可由
# "分数 + 居中度"择优压掉），同时在两个尺度上各跑一次，
# 兼顾"大头照"和"半身/全身照"两种输入。
YUNET_SCORE_THRESHOLD = 0.5
YUNET_NMS_THRESHOLD = 0.3
YUNET_TOPK = 5000
# 多尺度金字塔：1.0 = 原图；>1 表示把小图放大后再检一次，
# 小脸（半身照里的脸只有一两百像素）靠这一步才稳。
YUNET_SCALES = (1.0, 1.6)


@dataclass(frozen=True)
class FaceBox:
    """人脸外接框（像素，原图坐标）。"""

    left: int
    top: int
    width: int
    height: int
    # 可选：5 个关键点（左右眼、鼻尖、左右嘴角），像素坐标，顺序固定。
    # YuNet 能给出；Haar / Face++ 路径可能没有（None）。
    landmarks: Optional[Tuple[float, ...]] = None
    # 检测置信度 0~1；未知来源时为 None
    score: Optional[float] = None

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center(self) -> Tuple[float, float]:
        return (self.left + self.width / 2.0, self.top + self.height / 2.0)


# ---------------------------------------------------------------------------
# 1. 内部工具
# ---------------------------------------------------------------------------
def _scrub(text: str, *secrets: str) -> str:
    """抹掉文本中可能出现的密钥，避免 Face++ 报错信息把密钥带出去。"""
    out = text or ""
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


def _error_message(resp) -> str:
    """从 Face++ 响应里取出 error_message（解析失败就当没有）。"""
    try:
        data = resp.json()
    except Exception:
        return ""
    if isinstance(data, dict):
        return str(data.get("error_message") or "")
    return ""


def _max_inscribed_box(w0: int, h0: int, ratio: float) -> CropBox:
    """按目标比例裁出以原图中心为准的最大内接框（归一化）。"""
    if w0 / h0 > ratio:            # 原图更宽 → 裁两侧
        cw = h0 * ratio
        ch = float(h0)
    else:                          # 原图更高 → 裁上下
        cw = float(w0)
        ch = w0 / ratio
    left = (w0 - cw) / 2.0
    top = (h0 - ch) / 2.0
    return (left / w0, top / h0, (left + cw) / w0, (top + ch) / h0)


# ---------------------------------------------------------------------------
# 2. Face++ 云端检测
# ---------------------------------------------------------------------------
def _detect_face_facepp(image: Image.Image, api_key: str, api_secret: str,
                        timeout: float) -> Tuple[Optional[FaceBox], str]:
    """调用 Face++ 检测人脸。任何失败都转成中文原因，绝不抛异常。"""
    try:
        import requests
    except Exception:
        return None, "未安装 requests，无法调用 Face++"

    try:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=90)
        # 只要裸 base64，Face++ 不接受 data URI 前缀
        image_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:
        return None, "图片编码失败，无法调用 Face++"

    try:
        response = requests.post(
            FACEPP_URL,
            data={"api_key": api_key, "api_secret": api_secret,
                  "image_base64": image_base64},
            timeout=timeout,
        )
    except Exception as exc:
        detail = _scrub(f"{type(exc).__name__}: {exc}", api_key, api_secret)
        return None, f"Face++ 请求失败：{detail}"

    if response.status_code != 200:
        detail = _scrub(_error_message(response), api_key, api_secret)
        if response.status_code in (401, 403):
            # 密钥本身不出现在文案里，只说明"无效/无权限"
            return None, "Face++ 密钥无效或无权访问" + (f"（{detail}）" if detail else "")
        return None, (f"Face++ 请求失败：HTTP {response.status_code}"
                      + (f"（{detail}）" if detail else ""))

    try:
        payload = response.json()
    except Exception:
        return None, "Face++ 返回内容解析失败"

    if not isinstance(payload, dict):
        return None, "Face++ 返回内容解析失败"

    faces = payload.get("faces") or []
    if not faces:
        detail = _scrub(_error_message(response), api_key, api_secret)
        return None, "Face++ 未检测到人脸" + (f"（{detail}）" if detail else "")

    # 多张脸时取面积最大的那张（主体更可能是正脸）
    best_rect = None
    best_area = 0
    for face in faces:
        if not isinstance(face, dict):
            continue
        rect = face.get("face_rectangle") or {}
        try:
            fw = int(rect.get("width", 0))
            fh = int(rect.get("height", 0))
        except Exception:
            continue
        if fw <= 0 or fh <= 0:
            continue
        if fw * fh > best_area:
            best_area = fw * fh
            best_rect = rect

    if best_rect is None:
        return None, "Face++ 返回的人脸框无效"

    try:
        left = int(best_rect.get("left", 0))
        top = int(best_rect.get("top", 0))
        width = int(best_rect.get("width", 0))
        height = int(best_rect.get("height", 0))
    except Exception:
        return None, "Face++ 返回的人脸框无效"

    return FaceBox(left, top, width, height), "已检测到人脸（Face++）"


# ---------------------------------------------------------------------------
# 3. 本地 OpenCV 检测（YuNet 优先，Haar 兜底）
# ---------------------------------------------------------------------------
def _model_dirs() -> list:
    """模型文件的候选目录。

    PyInstaller 打包后 cv2.data 指向的是不存在的路径，所以除了它之外，
    还要找应用目录、models 子目录和 _MEIPASS —— 否则 exe 里会提示
    "未找到 OpenCV 人脸级联文件"。
    """
    dirs = []
    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(here)
    dirs.append(os.path.join(here, "models"))
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        dirs.append(meipass)
        dirs.append(os.path.join(meipass, "models"))
    try:
        import cv2
        base = getattr(getattr(cv2, "data", None), "haarcascades", "") or ""
        if base:
            dirs.append(base)
    except Exception:
        pass
    out = []
    for d in dirs:
        if d and os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def _ascii_safe_path(path: str) -> str:
    """让 OpenCV 能打开模型文件。

    OpenCV 的 FileStorage 在 Windows 上**打不开含中文的路径**（本项目目录
    就有中文），模型加载会直接失败。这里把模型复制一份到纯 ASCII 的临时
    目录再返回，源文件保持不动。
    """
    try:
        path.encode("ascii")
        return path
    except UnicodeEncodeError:
        pass
    try:
        import shutil
        import tempfile
        target_dir = os.path.join(tempfile.gettempdir(), "photolayout_models")
        target = os.path.join(target_dir, os.path.basename(path))
        if (not os.path.isfile(target)
                or os.path.getsize(target) != os.path.getsize(path)):
            os.makedirs(target_dir, exist_ok=True)
            shutil.copyfile(path, target)
        return target
    except Exception:
        return path


def _find_model(name: str) -> str:
    """在候选目录里找模型文件，找不到返回空串。"""
    for d in _model_dirs():
        path = os.path.join(d, name)
        if os.path.isfile(path):
            return _ascii_safe_path(path)
    return ""


def _yunet_pick(faces, width: int, height: int):
    """从多尺度检测结果里挑一张脸。

    择优规则：置信度为主，兼顾"更接近画面中心"。证件照是单人正面照，
    贴边的小块误检（手、耳朵、海报上的人脸）会因为置信度低 + 明显偏心
    而被压下去。
    """
    best = None
    best_score = -1e9
    cx_img, cy_img = width / 2.0, height / 2.0
    for f in faces:
        try:
            x, y, fw, fh = (float(v) for v in f[:4])
        except Exception:
            continue
        if fw <= 1 or fh <= 1:
            continue
        score = float(f[4]) if len(f) > 4 else YUNET_SCORE_THRESHOLD

        cx = x + fw / 2.0
        cy = y + fh / 2.0
        # 归一化偏心距（0 = 正中，1 = 角落）
        off = min(1.0, (abs(cx - cx_img) / max(1.0, width / 2.0)
                        + abs(cy - cy_img) / max(1.0, height / 2.0)) / 2.0)
        # 面积权重：太小的脸（< 24×24）不可信，太大的脸（整图都是脸）
        # 通常是误检，各自打折
        area = fw * fh
        area_ratio = area / max(1.0, float(width * height))
        area_weight = 1.0
        if area_ratio < 0.002:
            area_weight = 0.6
        elif area_ratio > 0.6:
            area_weight = 0.7

        total = (score * 10.0 - off * 3.0) * area_weight
        if total > best_score:
            best_score = total
            best = (x, y, fw, fh, score, f)

    if best is None:
        return None
    x, y, fw, fh, score, f = best
    left = int(round(x))
    top = int(round(y))
    box_w = max(1, int(round(fw)))
    box_h = max(1, int(round(fh)))
    left = max(0, min(left, width - 1))
    top = max(0, min(top, height - 1))
    box_w = max(1, min(box_w, width - left))
    box_h = max(1, min(box_h, height - top))

    landmarks = None
    try:
        pts = [float(v) for v in f[5:15]]
        if len(pts) == 10:
            landmarks = tuple(pts)
    except Exception:
        landmarks = None
    return FaceBox(left, top, box_w, box_h, landmarks=landmarks, score=score)


def _detect_face_yunet(image: Image.Image) -> Tuple[Optional[FaceBox], str]:
    """用 YuNet（DNN 人脸检测）检测，精度比 Haar 高得多，证件照场景更准。

    多尺度各跑一遍再统一择优：单尺度容易在"小脸半身照"上漏检，
    或在"大头特写"上把贴边杂物判成人脸。
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return None, "未安装 OpenCV，无法本地检测人脸"

    model = _find_model(YUNET_MODEL)
    if not model:
        return None, "未找到 YuNet 人脸检测模型"

    try:
        rgb = np.array(image.convert("RGB"))
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        if h <= 0 or w <= 0:
            return None, "图片尺寸无效，无法本地检测人脸"
    except Exception:
        return None, "YuNet 图像转换失败"

    all_faces = []
    for scale in YUNET_SCALES:
        sw = max(8, int(round(w * scale)))
        sh = max(8, int(round(h * scale)))
        try:
            work = bgr if (sw, sh) == (w, h) else cv2.resize(
                bgr, (sw, sh), interpolation=cv2.INTER_CUBIC)
            detector = cv2.FaceDetectorYN.create(
                model, "", (sw, sh),
                YUNET_SCORE_THRESHOLD, YUNET_NMS_THRESHOLD, YUNET_TOPK)
            _, faces = detector.detect(work)
        except Exception:
            continue
        if faces is None:
            continue
        for f in faces:
            try:
                if len(f) > 4 and float(f[4]) < YUNET_SCORE_THRESHOLD:
                    continue
            except Exception:
                pass
            if scale != 1.0:                    # 把放大尺度的坐标映回原图
                f = list(f)
                f[0] /= scale
                f[1] /= scale
                f[2] /= scale
                f[3] /= scale
                for i in range(5, min(len(f), 15)):
                    f[i] /= scale
                f = np.array(f, dtype=np.float32)
            all_faces.append(f)

    if not all_faces:
        return None, "未检测到人脸（YuNet）"

    best = _yunet_pick(all_faces, w, h)
    if best is None:
        return None, "未检测到人脸（YuNet）"
    return best, "已检测到人脸（YuNet）"


def _detect_face_opencv(image: Image.Image) -> Tuple[Optional[FaceBox], str]:
    """离线检测人脸：先试 YuNet，再退回 Haar 级联。"""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None, "未安装 OpenCV，无法本地检测人脸"

    # ---- YuNet（更准，证件照/侧脸都能检出）----
    box, yunet_reason = _detect_face_yunet(image)
    if box is not None:
        return box, yunet_reason

    # ---- Haar 级联（兜底）----
    try:
        rgb = np.array(image.convert("RGB"))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        # 直方图均衡：弱对比度照片上能明显提高检出率
        gray = cv2.equalizeHist(gray)
    except Exception:
        return None, "OpenCV 图像转换失败，无法本地检测人脸"

    h0, w0 = gray.shape[:2]
    if h0 <= 0 or w0 <= 0:
        return None, "图片尺寸无效，无法本地检测人脸"

    # 等比缩小以加速检测，检测完再把坐标映射回原图尺度
    scale = min(1.0, MAX_DETECT_SIDE / float(max(h0, w0)))
    small_w = max(1, int(round(w0 * scale)))
    small_h = max(1, int(round(h0 * scale)))
    small = gray
    if (small_w, small_h) != (w0, h0):
        try:
            small = cv2.resize(gray, (small_w, small_h),
                               interpolation=cv2.INTER_AREA)
        except Exception:
            small = gray
            small_w, small_h = w0, h0

    best = None
    best_area = 0
    used = 0
    for name in HAAR_CASCADES:
        path = _find_model(name)
        if not path:
            continue
        try:
            classifier = cv2.CascadeClassifier(path)
            if classifier.empty():
                continue
            faces = classifier.detectMultiScale(
                small, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
        except Exception:
            continue
        if faces is None:
            continue
        if not isinstance(faces, tuple):        # 兼容返回单个 ndarray 的情况
            faces = (faces,)
        used += 1
        for face in faces:
            try:
                x, y, fw, fh = (int(v) for v in face[:4])
            except Exception:
                continue
            if fw <= 0 or fh <= 0:
                continue
            if fw * fh > best_area:
                best_area = fw * fh
                best = (x, y, fw, fh)

    if best is None:
        if used == 0:
            return None, (f"{yunet_reason}；未找到 OpenCV 人脸级联文件，"
                           f"无法本地检测人脸")
        return None, "未检测到人脸（本地 OpenCV）"

    x, y, fw, fh = best
    sx = w0 / float(small_w)                  # 缩放图 → 原图的坐标系数
    sy = h0 / float(small_h)
    left = int(round(x * sx))
    top = int(round(y * sy))
    width = int(round(fw * sx))
    height = int(round(fh * sy))

    # 贴回原图边界内，避免浮点误差把框推出画面
    left = max(0, min(left, w0 - 1))
    top = max(0, min(top, h0 - 1))
    width = max(1, min(width, w0 - left))
    height = max(1, min(height, h0 - top))

    return FaceBox(left, top, width, height), "已检测到人脸（本地 OpenCV）"


# ---------------------------------------------------------------------------
# 4. 对外接口
# ---------------------------------------------------------------------------
def detect_face(image: Image.Image, mode: str = "auto",
                api_key: str = "", api_secret: str = "",
                timeout: float = 10.0) -> Tuple[Optional[FaceBox], str]:
    """检测人脸。返回 (人脸框 or None, 说明文字)。"""
    kind = (mode or "auto").strip().lower()

    if kind == "none":
        return None, "已跳过人脸检测"

    if kind == "facepp":
        if not api_key or not api_secret:
            return None, "未配置 Face++ API Key/Secret"
        return _detect_face_facepp(image, api_key, api_secret, timeout)

    if kind == "opencv":
        return _detect_face_opencv(image)

    # "auto"：有密钥先试云端，失败再回退本地
    if api_key and api_secret:
        box, reason = _detect_face_facepp(image, api_key, api_secret, timeout)
        if box is not None:
            return box, reason
        local_box, local_reason = _detect_face_opencv(image)
        if local_box is not None:
            return local_box, f"{reason}；已回退本地 OpenCV 完成检测"
        return None, f"{reason}；{local_reason}"

    return _detect_face_opencv(image)


def face_box_to_crop(box: FaceBox, image_size: Tuple[int, int],
                     ratio: float, head_ratio: float = 0.60,
                     face_center_y: float = 0.40) -> CropBox:
    """由人脸框 + 目标宽高比算出归一化裁剪框。"""
    w0 = int(image_size[0])
    h0 = int(image_size[1])
    if w0 <= 0 or h0 <= 0:
        return (0.0, 0.0, 1.0, 1.0)

    # 非法参数兜底，绝不抛异常
    if not (ratio > 0):
        ratio = 1.0
    if not (head_ratio > 0):
        head_ratio = 0.60

    face_w = max(1.0, float(box.width))
    face_h = max(1.0, float(box.height))
    cx, cy = box.center

    # 成片尺寸：人脸高度占成片高度 head_ratio
    film_h = face_h / head_ratio
    film_w = film_h * ratio

    # 成片比原图还大 → 等比整体缩小（保持 ratio），再重新居中
    if film_w > w0 or film_h > h0:
        shrink = min(w0 / film_w, h0 / film_h)
        film_w *= shrink
        film_h *= shrink

    left = cx - film_w / 2.0
    top = cy - face_center_y * film_h
    right = left + film_w
    bottom = top + film_h

    # 平移回画幅内：比直接夹紧更能保住目标比例与人脸构图
    if left < 0:
        left, right = 0.0, film_w
    if top < 0:
        top, bottom = 0.0, film_h
    if right > w0:
        left, right = w0 - film_w, w0
    if bottom > h0:
        top, bottom = h0 - film_h, h0

    # 兜底：仍放不下就退化成"按 ratio 居中的最大内接框"
    if (right - left) <= 0 or (bottom - top) <= 0 or film_w > w0 or film_h > h0:
        return _max_inscribed_box(w0, h0, ratio)

    l = max(0.0, min(1.0, left / w0))
    t = max(0.0, min(1.0, top / h0))
    r = max(0.0, min(1.0, right / w0))
    b = max(0.0, min(1.0, bottom / h0))

    # 夹紧后仍需保证 l<r、t<b
    if r <= l:
        l = max(0.0, 1.0 - 1.0 / w0)
        r = 1.0
    if b <= t:
        t = max(0.0, 1.0 - 1.0 / h0)
        b = 1.0

    return (l, t, r, b)


# 头顶留白规范（占成片高度的比例区间，max/min），移植自
# HivisionIDPhotos 的 head_top_range=(0.12, 0.10)：证件照里头顶既不能
# 顶着边（<0.10 显得压抑），也不能留一大片空白（>0.12 人像偏小偏下）。
HEAD_TOP_RANGE = (0.12, 0.10)
# 人像左右两侧允许的最大留白（占成片宽度比例）：超过就说明裁剪框偏了，
# 应当把多出来的部分裁掉
HEAD_SIDE_MAX = 0.10
# 底部留白上限（占成片高度比例）：证件照人像应坐实到底边
BOTTOM_MAX = 0.02


def _subject_bbox(alpha: Optional[np.ndarray],
                  min_ratio: float = 0.002) -> Optional[Tuple[int, int, int, int]]:
    """从抠图 alpha 里取出人像外接框 (left, top, right, bottom)；没有则 None。"""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    try:
        if alpha is None:
            return None
        core = (np.asarray(alpha, dtype=np.float32) > 0.5).astype(np.uint8)
        if not core.any():
            return None
        num, labels, stats, _ = cv2.connectedComponentsWithStats(core, 8)
        best = 0
        best_area = 0
        for idx in range(1, num):
            area = int(stats[idx, cv2.CC_STAT_AREA])
            if area > best_area:
                best_area = area
                best = idx
        if not best or best_area < core.size * min_ratio:
            return None
        x, y, w, h = (int(stats[best, col]) for col in
                      (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP,
                       cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT))
        return (x, y, x + w, y + h)
    except Exception:
        return None


def _refine_crop_with_subject(image_size: Tuple[int, int],
                              crop: CropBox, ratio: float,
                              subject: Tuple[int, int, int, int]) -> CropBox:
    """用"人像外接框"修正裁剪框（HivisionIDPhotos 的三步校正）。

    1. 左右留白超过 HEAD_SIDE_MAX → 裁掉多余一侧，保持宽高比同步收窄；
    2. 头顶留白不在 HEAD_TOP_RANGE 内 → 上下平移裁剪框；
    3. 底部留白超过 BOTTOM_MAX → 裁剪框上移，让人像坐到底边。

    全程只在归一化坐标里平移/收缩，且保证宽高比不变。
    """
    try:
        w0 = int(image_size[0])
        h0 = int(image_size[1])
        if w0 <= 0 or h0 <= 0 or not (ratio > 0):
            return crop

        left, top, right, bottom = (float(v) for v in crop)
        # 归一化 → 像素
        cx1, cy1 = left * w0, top * h0
        cx2, cy2 = right * w0, bottom * h0
        cw, ch = cx2 - cx1, cy2 - cy1
        if cw <= 1 or ch <= 1:
            return crop

        sx1, sy1, sx2, sy2 = (float(v) for v in subject)

        # ---- 1. 左右留白裁剪（保持比例：宽减多少，高就按比例减多少）----
        left_gap = max(0.0, sx1 - cx1)
        right_gap = max(0.0, cx2 - sx2)
        side_budget = HEAD_SIDE_MAX * cw
        shrink = 0.0
        if left_gap > side_budget:
            shrink += left_gap - side_budget
        if right_gap > side_budget:
            shrink += right_gap - side_budget
        if shrink > 0:
            shrink = min(shrink, cw * 0.4)          # 防御：别把框裁没了
            cx1 += left_gap if left_gap > side_budget else 0.0
            cx2 -= right_gap if right_gap > side_budget else 0.0
            # 高度按比例收窄，上下各让一半（与 Hivision 一致）
            dh = min(shrink / ratio, ch * 0.4)
            cy1 += dh / 2.0
            cy2 -= dh / 2.0
            cw, ch = cx2 - cx1, cy2 - cy1

        if cw <= 1 or ch <= 1:
            return crop

        # ---- 2. 头顶留白校正 ----
        head_gap = sy1 - cy1
        head_ratio = head_gap / ch
        if head_ratio > HEAD_TOP_RANGE[0]:
            cy1 += head_gap - HEAD_TOP_RANGE[0] * ch
        elif head_ratio < HEAD_TOP_RANGE[1]:
            cy1 -= (HEAD_TOP_RANGE[1] * ch - head_gap)
        cy2 = cy1 + ch

        # ---- 3. 底部坐实 ----
        bottom_gap = cy2 - sy2
        if bottom_gap > BOTTOM_MAX * ch:
            cy1 -= bottom_gap
            cy2 -= bottom_gap

        # ---- 收尾：等比回缩到画幅内（保持比例）----
        if cw > w0 or ch > h0:
            k = min(w0 / cw, h0 / ch)
            cx_mid = (cx1 + cx2) / 2.0
            cy_mid = (cy1 + cy2) / 2.0
            cw, ch = cw * k, ch * k
            cx1, cx2 = cx_mid - cw / 2.0, cx_mid + cw / 2.0
            cy1, cy2 = cy_mid - ch / 2.0, cy_mid + ch / 2.0

        if cx1 < 0:
            cx2 -= cx1
            cx1 = 0.0
        if cy1 < 0:
            cy2 -= cy1
            cy1 = 0.0
        if cx2 > w0:
            cx1 -= (cx2 - w0)
            cx2 = float(w0)
        if cy2 > h0:
            cy1 -= (cy2 - h0)
            cy2 = float(h0)
        cx1 = max(0.0, min(float(w0), cx1))
        cy1 = max(0.0, min(float(h0), cy1))
        cx2 = max(0.0, min(float(w0), cx2))
        cy2 = max(0.0, min(float(h0), cy2))
        if cx2 - cx1 <= 1 or cy2 - cy1 <= 1:
            return crop

        return (cx1 / w0, cy1 / h0, cx2 / w0, cy2 / h0)
    except Exception:
        return crop


def auto_crop_box(image: Image.Image, ratio: float,
                  head_ratio: float = 0.60, mode: str = "auto",
                  api_key: str = "", api_secret: str = "",
                  timeout: float = 10.0,
                  refine: bool = True) -> Tuple[Optional[CropBox], str]:
    """一步到位：检测人脸 → 换算裁剪框 → 按人像留白校正。

    返回 (裁剪框 or None, 说明文字)。
    refine=True 时会先抠图取人像外接框，再按证件照规范校正头顶/左右/底部
    留白（移植自 HivisionIDPhotos）；抠图不可用时直接返回人脸框结果。
    """
    box, reason = detect_face(image, mode=mode, api_key=api_key,
                              api_secret=api_secret, timeout=timeout)
    if box is None:
        # 检测不到人脸就不给框，绝不用居中框冒充成功
        return None, reason

    crop = face_box_to_crop(box, image.size, ratio, head_ratio=head_ratio)
    if not refine:
        return crop, f"{reason}，已生成自动裁剪框"

    # 抠图取人像外接框：MODNet 不可用时退回漫水（再不行就跳过校正）
    subject = None
    try:
        import numpy as np

        import photo_tools
        alpha = photo_tools.neural_alpha(image, face_box=(box.left, box.top,
                                                          box.width, box.height))
        if alpha is None:
            alpha = photo_tools._flood_alpha(
                np.asarray(image.convert("RGB"), dtype=np.float32), 1.0)
        subject = _subject_bbox(alpha)
    except Exception:
        subject = None

    if subject is None:
        return crop, f"{reason}，已生成自动裁剪框（无人像留白数据，未做校正）"

    refined = _refine_crop_with_subject(image.size, crop, ratio, subject)
    return refined, f"{reason}，已生成自动裁剪框（已按人像留白校正）"
