# -*- coding: utf-8 -*-
"""区域锚点与定位:窗口相对比例(REGIONS)+ find_text / find_template。

坐标策略(ARCHITECTURE §5.2):区域锚点存"窗口相对比例",来自实机
像素标定(基准窗口 896x648,标定过程记录不入库),
运行时先用比例换算出粗区域,再用 OCR 文字/模板匹配精修到像素——
窗口大小变化不崩。

ratio 定义:(x0, y0, x1, y1) ∈ [0,1],相对窗口宽高(左上为原点);
point 定义:(x, y) ∈ [0,1]。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from wxbot.vision.ocr import OcrItem

# ---- 区域锚点(实机像素标定表 ÷ 896x648)----
REGIONS: dict[str, tuple[float, float, float, float]] = {
    # 左侧导航栏 x 0-65
    "nav": (0.0000, 0.0000, 0.0725, 1.0000),
    # 导航"聊天"图标 (33,110) 周边(未读红点像素预检用,含图标右上角)
    "nav_chat_icon": (0.0112, 0.1312, 0.0647, 0.2160),
    # 聊天列表 x 65-305
    "chat_list": (0.0725, 0.0000, 0.3404, 1.0000),
    # 搜索框 x 88-238, y 47-67
    "search_box": (0.0982, 0.0725, 0.2656, 0.1034),
    # 搜索结果弹层(4.x 出现在窗口中部偏左,01 文档 §[1];取宽松区域 OCR 精修)
    "search_popup": (0.0725, 0.1200, 0.6500, 0.8700),
    # 聊天标题栏(标题文字 ≈(325,55) 起,y≈80 为分隔线)
    "title_bar": (0.3404, 0.0300, 0.9500, 0.1250),
    # 消息气泡区 y≈80-500
    "message_area": (0.3404, 0.1235, 1.0000, 0.7716),
    # 输入区 y≈505-648(发送流程用;演练模式红线:严禁触碰)
    "input_box": (0.3404, 0.7793, 1.0000, 1.0000),
    # 发送按钮 x 812-855, y 598-620(发送流程用)
    "send_btn": (0.9063, 0.9228, 0.9542, 0.9568),
    # 消息区两侧头像列(消息归属判定的头像信号;自己头像 x≈830-878)
    "avatar_self_col": (0.9263, 0.1235, 0.9799, 0.7716),
    "avatar_other_col": (0.3571, 0.1235, 0.4107, 0.7716),
}

# ---- 单点锚点 ----
POINTS: dict[str, tuple[float, float]] = {
    "search_box_click": (163 / 896, 57 / 648),   # 搜索框中心(实机标定)
    "input_click": (600 / 896, 575 / 648),       # 输入区点击落点参考(发送流程用;演练模式禁触)
    "send_btn_click": (833 / 896, 609 / 648),    # 发送按钮中心(发送流程用)
    "nav_chat_icon": (33 / 896, 110 / 648),      # 导航"聊天"图标中心
}

# 配色特征(实机采样,BGR 顺序)
COLOR_SELF_BUBBLE_BGR = (158, 226, 149)   # #95E29E 绿气泡(自己)
SELF_BUBBLE_GREENISH = lambda b, g, r: g > r + 20 and g > b + 20 and g > 120  # noqa: E731

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


@dataclass
class Match:
    name: str
    center: tuple[int, int]
    box: tuple[int, int, int, int]   # (x0, y0, x1, y1)
    score: float
    scale: float


def region_px(window_size: tuple[int, int], name: str) -> tuple[int, int, int, int]:
    """把 REGIONS 中的比例锚点换算为窗口本地像素矩形 (x0,y0,x1,y1)。"""
    w, h = window_size
    rx0, ry0, rx1, ry1 = REGIONS[name]
    x0 = max(0, min(int(rx0 * w), w - 1))
    y0 = max(0, min(int(ry0 * h), h - 1))
    x1 = max(x0 + 1, min(int(rx1 * w), w))
    y1 = max(y0 + 1, min(int(ry1 * h), h))
    return (x0, y0, x1, y1)


def point_px(window_size: tuple[int, int], name: str) -> tuple[int, int]:
    """把 POINTS 中的比例锚点换算为窗口本地像素点。"""
    w, h = window_size
    rx, ry = POINTS[name]
    return (int(rx * w), int(ry * h))


def _in_rect(pt: tuple[int, int], rect: tuple[int, int, int, int], margin: int = 0) -> bool:
    x, y = pt
    x0, y0, x1, y1 = rect
    return (x0 - margin) <= x <= (x1 + margin) and (y0 - margin) <= y <= (y1 + margin)


def find_text(
    items: list[OcrItem],
    pattern: str,
    region: Optional[tuple[int, int, int, int]] = None,
    exact: bool = False,
) -> Optional[OcrItem]:
    """在 OCR 结果中找文本项。region 为像素矩形(与 items 同一坐标系)。

    匹配优先级:精确相等 > 包含 pattern;同优先级取置信度最高者
    (exact=True 时只接受精确相等)。未命中返回 None。
    """
    pattern = pattern.strip()
    if not pattern:
        return None
    candidates = [it for it in items if region is None or _in_rect(it.center, region)]
    if exact:
        candidates = [it for it in candidates if it.text == pattern]
        return max(candidates, key=lambda it: it.conf) if candidates else None
    eq = [it for it in candidates if it.text == pattern]
    if eq:
        return max(eq, key=lambda it: it.conf)
    sub = [it for it in candidates if pattern in it.text]
    if sub:
        return max(sub, key=lambda it: it.conf)
    return None


def find_text_all(
    items: list[OcrItem],
    pattern: str,
    region: Optional[tuple[int, int, int, int]] = None,
) -> list[OcrItem]:
    """find_text 的全部命中版(精确相等优先排序,再按包含,再按 y)。"""
    pattern = pattern.strip()
    cands = [it for it in items if region is None or _in_rect(it.center, region)]
    eq = [it for it in cands if it.text == pattern]
    sub = [it for it in cands if it.text != pattern and pattern in it.text]
    eq.sort(key=lambda it: it.center[1])
    sub.sort(key=lambda it: it.center[1])
    return eq + sub


def find_template(
    img: np.ndarray,
    tpl_name: str,
    region: Optional[tuple[int, int, int, int]] = None,
    threshold: float = 0.8,
) -> Optional[Match]:
    """多尺度模板匹配(灰度+TM_CCOEFF_NORMED,0.8x~1.2x 步长0.05,02 文档 §3.3)。

    模板库存于 wxbot/vision/templates/<tpl_name>.png(与运行时同机同 DPI 采集)。
    模板不存在或未达阈值返回 None。region 为像素矩形 ROI,先裁剪再匹配。
    """
    tpl_path = TEMPLATES_DIR / f"{tpl_name}.png"
    tpl = imread_u(tpl_path, cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        return None
    src = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    off = (0, 0)
    if region is not None:
        x0, y0, x1, y1 = [int(v) for v in region]
        off = (x0, y0)
        src = src[y0:y1, x0:x1]
    if src.shape[0] < tpl.shape[0] or src.shape[1] < tpl.shape[1]:
        # 源比模板还小:反向多尺度也无意义,直接判未命中
        return None

    best: Optional[Match] = None
    scale = 0.8
    while scale <= 1.201:
        tw = int(round(tpl.shape[1] * scale))
        th = int(round(tpl.shape[0] * scale))
        if 4 <= tw <= src.shape[1] and 4 <= th <= src.shape[0]:
            tpl_s = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(src, tpl_s, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= threshold and (best is None or max_val > best.score):
                bx0, by0 = max_loc[0] + off[0], max_loc[1] + off[1]
                best = Match(
                    name=tpl_name,
                    center=(bx0 + tw // 2, by0 + th // 2),
                    box=(bx0, by0, bx0 + tw, by0 + th),
                    score=round(float(max_val), 4),
                    scale=round(float(scale), 3),
                )
        scale += 0.05
    return best


def red_pixels_in(img_bgr: np.ndarray, rect: tuple[int, int, int, int]) -> int:
    """统计矩形区域内"红色像素"数(R>G 且 R>B 且 R>150,wxauto IsRedPixel 思路,
    调研01 §[4]A)。用于导航"聊天"图标未读红点毫秒级预检。"""
    x0, y0, x1, y1 = [int(v) for v in rect]
    roi = img_bgr[y0:y1, x0:x1]
    if roi.size == 0:
        return 0
    b = roi[:, :, 0].astype(int)
    g = roi[:, :, 1].astype(int)
    r = roi[:, :, 2].astype(int)
    return int(((r > g + 40) & (r > b + 40) & (r > 150)).sum())


# ---- UTF-8 路径安全的图像读写(cv2 在 Windows 上对含中文路径会静默失败) ----
def imwrite_u(path: str | Path, img: np.ndarray) -> bool:
    """cv2.imwrite 的 UTF-8 路径安全版。父目录不存在会自动创建。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(str(p.suffix) or ".png", img)
    if not ok:
        return False
    p.write_bytes(buf.tobytes())
    return True


def imread_u(path: str | Path, flags: int = cv2.IMREAD_COLOR) -> Optional[np.ndarray]:
    """cv2.imread 的 UTF-8 路径安全版;失败返回 None。"""
    p = Path(path)
    if not p.exists():
        return None
    data = p.read_bytes()
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, flags)
