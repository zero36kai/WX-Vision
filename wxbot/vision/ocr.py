# -*- coding: utf-8 -*-
"""OCR 封装:RapidOCR(onnxruntime CPU)→ [OcrItem(text, box, center, conf)]。

契约(ARCHITECTURE §3):
    @dataclass
    class OcrItem:
        text: str; box: tuple; center: tuple[int, int]; conf: float
    def run_ocr(img_bgr) -> list[OcrItem]

要点(调研02 §1.4):
- RapidOCR 惰性加载模型,进程内只初始化一次(模块级单例);
- 原生输出 boxes(N,4,2)+ txts + scores;坐标系 = 输入图像像素坐标,
  与截屏图一致,可直接映射屏幕坐标;
- 过滤低置信度行(默认 <0.5 丢弃,可调;02 文档建议 0.7~0.8,微信小字号
  场景实测留 0.5 防漏检,业务层可按需再筛)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class OcrItem:
    text: str
    box: tuple            # 4 点坐标 ((x0,y0),(x1,y1),(x2,y2),(x3,y3)),左上/右上/右下/左下
    center: tuple[int, int]
    conf: float


_ENGINE = None          # RapidOCR 单例
_ENGINE_ERR: Optional[str] = None


def get_engine():
    """惰性初始化 RapidOCR 引擎(首次约 3~5s,之后复用)。"""
    global _ENGINE, _ENGINE_ERR
    if _ENGINE is not None:
        return _ENGINE
    if _ENGINE_ERR is not None:
        raise RuntimeError(f"RapidOCR 初始化曾失败,不重试: {_ENGINE_ERR}")
    try:
        from rapidocr import RapidOCR
    except ImportError as e:  # pragma: no cover
        _ENGINE_ERR = f"rapidocr 未安装: {e}"
        raise RuntimeError(_ENGINE_ERR) from e
    try:
        _ENGINE = RapidOCR()
    except Exception as e:
        _ENGINE_ERR = f"{type(e).__name__}: {e}"
        raise
    return _ENGINE


def run_ocr(img_bgr: np.ndarray, min_conf: float = 0.5) -> list[OcrItem]:
    """对 BGR 图像跑 OCR,返回按 y 升序的 OcrItem 列表。"""
    if img_bgr is None or img_bgr.size == 0:
        return []
    eng = get_engine()
    out = eng(img_bgr)
    if out is None:
        return []

    # rapidocr>=3.x 返回 RapidOCROutput(boxes/txts/scores);兼容旧版四元组
    if hasattr(out, "txts"):
        boxes, txts, scores = out.boxes, out.txts, out.scores
    else:  # pragma: no cover 旧版 (boxes, txts, scores, elapse)
        boxes, txts, scores = out[0], out[1], out[2]

    items: list[OcrItem] = []
    if boxes is None or txts is None or len(boxes) == 0:
        return []
    for box, text, score in zip(boxes, txts, scores):
        try:
            conf = float(score)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_conf:
            continue
        pts = [(int(round(float(p[0]))), int(round(float(p[1])))) for p in np.asarray(box)]
        if len(pts) != 4:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        center = (int(sum(xs) // 4), int(sum(ys) // 4))
        items.append(OcrItem(text=str(text).strip(), box=tuple(pts), center=center, conf=round(conf, 4)))
    items.sort(key=lambda it: (it.center[1], it.center[0]))
    return items
