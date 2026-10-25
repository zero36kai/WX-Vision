# -*- coding: utf-8 -*-
"""非文本消息媒体落盘(docs/PROTOCOL.md §3 纯视觉能力边界 + §2 /api/media)。

能力边界(诚实声明,协议 §3):
- image  = 消息气泡可见区截图裁剪 PNG(纯视觉拿不到原图文件);
- voice / file = **仅事件通知**(text 中说明"收到语音/文件"),纯视觉无法
  提取其内容——这类消息 media_url 为 null,只有文本事件说明;
- media_url 为**完整带 token 的可下载链接**(由 API 层用当前访问口令现生成),
  **禁止 base64**(协议红线:截图可能很大,base64 会让每次轮询载荷爆炸)。

落盘位置:data/media/<media_id>.png(media_id = m-<16hex>,随机不可枚举;
下载仍需 token,双保险)。文件名经严格白名单校验,杜绝路径穿越。
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path

# media_id 白名单:字母数字下划线连字符,1~64 位(防路径穿越/任意文件读)
MEDIA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

PNG_SUFFIX = ".png"


class MediaStore:
    """媒体文件存取(data/media/,PNG 为主)。目录惰性创建(首次写入时)。"""

    def __init__(self, directory: str | Path):
        self._dir = Path(directory)

    @property
    def directory(self) -> Path:
        return self._dir

    # ------------------------------------------------------------------ 写入
    def save_png(self, data: bytes) -> str:
        """保存一份 PNG 字节,返回 media_id。调用方需保证 data 是 PNG。"""
        if not data:
            raise ValueError("媒体内容为空")
        media_id = "m-" + secrets.token_hex(8)
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / (media_id + PNG_SUFFIX)).write_bytes(bytes(data))
        return media_id

    def save_bubble_png(self, img_bgr, box=None) -> str:
        """气泡区截图裁剪 PNG:image 消息的落盘入口(纯视觉边界,协议 §3)。

        img_bgr: 全窗截图(numpy BGR 数组,来自 WeChatWindow.capture);
        box:     气泡 (x0, y0, x1, y1) 像素框(OCR/模板定位得到);None = 全图。
        返回 media_id。cv2/numpy 按需导入,保持本模块离线轻量。"""
        import cv2

        crop = img_bgr
        if box is not None:
            h, w = img_bgr.shape[:2]
            x0, y0, x1, y1 = (int(v) for v in box)
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(w, x1), min(h, y1)
            if x1 <= x0 or y1 <= y0:
                raise ValueError("气泡裁剪框非法(裁剪后为空)")
            crop = img_bgr[y0:y1, x0:x1]
        ok, buf = cv2.imencode(PNG_SUFFIX, crop)
        if not ok:
            raise RuntimeError("PNG 编码失败")
        return self.save_png(buf.tobytes())

    # ------------------------------------------------------------------ 读取
    def path_of(self, media_id: str) -> Path | None:
        """media_id → 已存在文件的路径;非法 id / 不存在 → None。"""
        mid = str(media_id or "")
        if not MEDIA_ID_RE.match(mid):
            return None
        p = self._dir / (mid + PNG_SUFFIX)
        return p if p.is_file() else p

    def read(self, media_id: str) -> bytes | None:
        """读取媒体字节;非法 id / 不存在 → None(下载端点据此 404)。"""
        p = self.path_of(media_id)
        if p is None or not p.is_file():
            return None
        try:
            return p.read_bytes()
        except OSError:
            return None
