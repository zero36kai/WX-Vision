# -*- coding: utf-8 -*-
"""窗口管理:查找微信4.x主窗口 / 恢复+置前 / 矩形 / PrintWindow截屏。

依据 docs/ARCHITECTURE.md §3 契约与实机验证结论(标定过程记录不入库):
- 主窗口 class=Qt51514QWindowIcon,title=微信(句柄会变,必须动态查找);
- 截屏 PrintWindow+PW_RENDERFULLCONTENT(=2) 为主(抗遮挡,实机已验证),
  mss 前台区域截屏为回退;最小化时两者都不可用,须先恢复;
- 感知与行动分离:点击必须窗口前台可见,ensure_active 负责保证。

仅用 ctypes 直调 Win32 API,不使用 pywin32/UIAutomation(ARCHITECTURE §1 定稿)。
"""
from __future__ import annotations

import ctypes
import random
import time
from ctypes import wintypes
from typing import Optional

import numpy as np

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# ---- 显式签名,避免 64 位句柄截断 ----
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, wintypes.INT]
user32.ShowWindow.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, wintypes.DWORD]
user32.keybd_event.restype = None
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

WECHAT_WINDOW_CLASS = "Qt51514QWindowIcon"
WECHAT_WINDOW_TITLE = "微信"

SW_RESTORE = 9
PW_RENDERFULLCONTENT = 2


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biHeight", wintypes.LONG),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _enum_toplevel_windows(visible_only: bool = True) -> list[int]:
    out: list[int] = []

    def cb(hwnd, _lparam):
        if (not visible_only) or user32.IsWindowVisible(hwnd):
            out.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_title(hwnd: int) -> str:
    ln = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(ln + 1)
    user32.GetWindowTextW(hwnd, buf, ln + 1)
    return buf.value


class WeChatWindow:
    """微信 4.x 主窗口句柄封装:查找 / 激活 / 矩形 / 截屏。"""

    def __init__(self, hwnd: int):
        self.hwnd = int(hwnd)

    # ---------- 查找 ----------
    @classmethod
    def find(cls) -> Optional["WeChatWindow"]:
        """class=Qt51514QWindowIcon 且 title=微信 的顶层窗口(独立聊天窗口
        同类名但标题=联系人名,不会误中)。

        优先可见窗口;找不到再扫隐藏窗口——实测微信可被隐藏到托盘
        (IsWindowVisible=False 但句柄仍在,rect 保留),此时由
        ensure_active 负责恢复显示。"""
        for visible_only in (True, False):
            for hwnd in _enum_toplevel_windows(visible_only=visible_only):
                if (_window_class(hwnd) == WECHAT_WINDOW_CLASS
                        and _window_title(hwnd) == WECHAT_WINDOW_TITLE):
                    return cls(hwnd)
        return None

    # ---------- 矩形 ----------
    @property
    def rect_screen(self) -> tuple[int, int, int, int]:
        """窗口屏幕绝对坐标 (left, top, right, bottom),每次实时获取。"""
        rc = wintypes.RECT()
        if not user32.GetWindowRect(self.hwnd, ctypes.byref(rc)):
            raise RuntimeError("GetWindowRect 失败,窗口句柄可能已失效")
        return (rc.left, rc.top, rc.right, rc.bottom)

    @property
    def size(self) -> tuple[int, int]:
        l, t, r, b = self.rect_screen
        return (r - l, b - t)

    def is_minimized(self) -> bool:
        return bool(user32.IsIconic(self.hwnd))

    def is_foreground(self) -> bool:
        return user32.GetForegroundWindow() == self.hwnd

    # ---------- 激活 ----------
    def ensure_active(self) -> bool:
        """最小化则 SW_RESTORE;隐藏到托盘则 SW_SHOW;再置前;最后校验场景
        已真实渲染(防"僵尸空白壳",见 _revive_render)。ARCHITECTURE §3:
        允许最小闪烁方案。"""
        if not user32.IsWindow(self.hwnd):
            return False
        if not user32.IsWindowVisible(self.hwnd):
            user32.ShowWindow(self.hwnd, 5)  # SW_SHOW:实测可从托盘隐藏态恢复
            time.sleep(random.uniform(0.3, 0.6))
        if self.is_minimized():
            user32.ShowWindow(self.hwnd, SW_RESTORE)
            time.sleep(random.uniform(0.4, 0.8))
        ok = self._force_foreground()
        # 实测(2026-10-24):从托盘态被 SW_SHOW 拉起的窗口可能只剩空白壳
        # (Qt Quick 场景未重建,PrintWindow 失败、屏幕内容为纯色)——需要自愈
        if self._interior_blank():
            self._revive_render()
            ok = self._force_foreground()
        return ok

    def _interior_blank(self) -> bool:
        """窗口中心 1/2 区域是否近乎纯色(僵尸空白壳探测器)。

        用 mss 读屏幕(此时窗口应在前台);被遮挡/多屏时可能误判为不空白,
        保守方向(不触发自愈),可接受。"""
        try:
            img = self._capture_mss()
        except Exception:
            return False
        if img is None:
            return True
        h, w = img.shape[:2]
        crop = img[h // 4: h * 3 // 4, w // 4: w * 3 // 4]
        if crop.size == 0:
            return True
        return float(crop.std()) < 5.0

    def _revive_render(self, max_presses: int = 5) -> bool:
        """恢复"可见但空白"的僵尸渲染壳。

        实测有效手段:Ctrl+Alt+W(微信默认"显示/隐藏主界面"全局热键,
        调研01 §③)反复 toggle,让微信走自己的显示/隐藏代码路径重建
        Qt Quick 渲染场景;ShowWindow/resize/hover 均无效(已实测)。
        热键被用户改绑时 toggle 无效果,循环自动放弃(保守无害)。"""
        try:
            import pyautogui
        except ImportError:  # pragma: no cover
            return False
        revived = False
        for _ in range(max_presses):
            pyautogui.hotkey("ctrl", "alt", "w")
            time.sleep(random.uniform(0.9, 1.5))
            if not user32.IsWindowVisible(self.hwnd):
                # 被热键隐藏了:再 toggle 一次拉回(经微信自有路径,场景重建)
                pyautogui.hotkey("ctrl", "alt", "w")
                time.sleep(random.uniform(1.3, 2.0))
            if not self._interior_blank():
                revived = True
                break
        if self.is_minimized():
            user32.ShowWindow(self.hwnd, SW_RESTORE)
            time.sleep(random.uniform(0.3, 0.6))
        if not user32.IsWindowVisible(self.hwnd):
            user32.ShowWindow(self.hwnd, 5)
            time.sleep(random.uniform(0.3, 0.6))
        return revived

    def _force_foreground(self) -> bool:
        """SetForegroundWindow 有前台锁限制(调研02 §5.2),按推荐顺序绕:
        1) AttachThreadInput 附着前台线程后置前(最稳);
        2) ALT 键技巧(发一次 ALT 按下/抬起骗过前台锁);
        3) 最小化→恢复闪烁。
        每次尝试后校验 GetForegroundWindow()==hwnd,成功即返回。"""
        for _ in range(3):
            if self.is_foreground():
                return True
            if self._attach_thread_foreground():
                return True
            time.sleep(random.uniform(0.2, 0.4))
            if self.is_foreground():
                return True
            # ALT 键技巧
            user32.keybd_event(0x12, 0, 0, 0)       # VK_MENU down
            user32.keybd_event(0x12, 0, 2, 0)       # VK_MENU up (KEYEVENTF_KEYUP)
            user32.SetForegroundWindow(self.hwnd)
            time.sleep(random.uniform(0.2, 0.4))
            if self.is_foreground():
                return True
            # 最小闪烁:最小化再恢复(ARCHITECTURE 允许)
            user32.ShowWindow(self.hwnd, 6)          # SW_MINIMIZE
            time.sleep(0.15)
            user32.ShowWindow(self.hwnd, SW_RESTORE)
            time.sleep(random.uniform(0.3, 0.6))
        return self.is_foreground()

    def _attach_thread_foreground(self) -> bool:
        user32.BringWindowToTop(self.hwnd)
        fg = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        fg_thread = user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        cur_thread = kernel32.GetCurrentThreadId()
        attached = False
        if fg_thread and fg_thread != cur_thread:
            attached = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
        try:
            user32.SetForegroundWindow(self.hwnd)
            user32.BringWindowToTop(self.hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur_thread, fg_thread, False)
        time.sleep(random.uniform(0.1, 0.25))
        return self.is_foreground()

    # ---------- 截屏 ----------
    def capture(self, use_printwindow: bool = True) -> np.ndarray:
        """截取窗口内容,返回 BGR ndarray(窗口本地坐标,含边框)。

        主路 PrintWindow+PW_RENDERFULLCONTENT(可抗遮挡,04 文档 §2 实测可用);
        黑屏/空白帧自动重试并回退 mss;仍取不到有效内容且判定为"僵尸空白壳"
        时,自动走 _revive_render 自愈后重截。窗口最小化时抛 RuntimeError
        (调用方应先 ensure_active)。
        """
        if self.is_minimized():
            raise RuntimeError("微信窗口处于最小化,PrintWindow/mss 均不可用;请先 ensure_active()")
        last: Optional[np.ndarray] = None
        for attempt in range(4):
            if use_printwindow:
                img = self._capture_printwindow()
                if _valid_frame(img):
                    return img
                last = img if img is not None else last
            img = self._capture_mss()
            if _valid_frame(img):
                return img
            last = img if img is not None else last
            time.sleep(random.uniform(0.25, 0.4))  # 等待窗口完成绘制
        # 自愈路径:窗口可能处于"可见但空白"的僵尸渲染态
        if self._interior_blank() and self._revive_render():
            img = self._capture_printwindow() if use_printwindow else None
            if _valid_frame(img):
                return img
            img = self._capture_mss()
            if _valid_frame(img):
                return img
        if last is not None and not _looks_black(last):
            return last  # 内容存疑但非黑,交由上层 OCR/业务判断
        raise RuntimeError("截屏失败:PrintWindow 与 mss 均未取到有效图像")

    def _capture_printwindow(self) -> Optional[np.ndarray]:
        l, t, r, b = self.rect_screen
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            return None
        hwnd_dc = user32.GetWindowDC(self.hwnd)
        try:
            mdc = gdi32.CreateCompatibleDC(hwnd_dc)
            bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
            old = gdi32.SelectObject(mdc, bmp)
            ok = user32.PrintWindow(self.hwnd, mdc, PW_RENDERFULLCONTENT)
            gdi32.SelectObject(mdc, old)  # GetDIBits 要求位图不被选入 DC
            bi = BITMAPINFO()
            bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bi.bmiHeader.biWidth = w
            bi.bmiHeader.biHeight = -h  # 负值 = 自顶向下
            bi.bmiHeader.biPlanes = 1
            bi.bmiHeader.biBitCount = 32
            bi.bmiHeader.biCompression = 0  # BI_RGB
            buf = ctypes.create_string_buffer(w * h * 4)
            got = gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mdc)
            if not (ok and got):
                return None
            arr = np.frombuffer(buf.raw, dtype=np.uint8).reshape(h, w, 4)
            return arr[:, :, :3].copy()  # BGRX → BGR
        finally:
            user32.ReleaseDC(self.hwnd, hwnd_dc)

    def _capture_mss(self) -> Optional[np.ndarray]:
        try:
            import mss
        except ImportError:  # pragma: no cover
            return None
        l, t, r, b = self.rect_screen
        region = {"left": l, "top": t, "width": r - l, "height": b - t}
        with mss.mss() as sct:
            shot = sct.grab(region)
        arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(
            shot.height, shot.width, 4)
        return arr[:, :, :3].copy()  # BGRA → BGR


def _looks_black(img: np.ndarray) -> bool:
    """近乎全黑视为截屏失败(GPU 内容未渲染)。"""
    return float(img.mean()) < 2.0 and float(img.std()) < 2.0


def _valid_frame(img: Optional[np.ndarray]) -> bool:
    """帧有效性:非 None、非全黑、中心区域非纯色。

    中心区检测针对实测发现的"僵尸空白壳":窗口边缘会残留其他窗口碎片
    (整图 std 被抬高),但中心 1/2 区域是纯色(Qt Quick 场景未重建)。"""
    if img is None:
        return False
    if _looks_black(img):
        return False
    h, w = img.shape[:2]
    crop = img[h // 4: h * 3 // 4, w // 4: w * 3 // 4]
    if crop.size == 0:
        return False
    return float(crop.std()) >= 5.0
