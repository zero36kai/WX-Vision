# -*- coding: utf-8 -*-
"""wxbot —— 微信纯视觉自动化助手(WeChatVision)核心包。

红线(见 docs/ARCHITECTURE.md §0,违反任何一条即任务失败):
1. 只对白名单联系人(默认 ["文件传输助手"])操作;
2. 默认零发送:未开启允许真实发送前,严禁发送任何消息、严禁触碰聊天输入框;
3. pyautogui FAILSAFE 保持开启;
4. 所有文件 UTF-8,Python 读写显式 encoding='utf-8';
5. 禁止 UIAutomation / hook / 内存读取 / 微信协议库。

本包导入保持轻量:重依赖(rapidocr / cv2 / pyautogui)在各子模块内部
按需导入,`import wxbot` 不会加载模型或驱动键鼠。
"""

# 进程 DPI 感知必须最先声明:保证截图像素坐标与键鼠屏幕坐标一致(调研02 §2.4)。
# 若宿主进程已设置过 DPI 感知,重复设置会失败,静默忽略即可。
def _enable_dpi_awareness() -> None:
    import ctypes

    try:  # Win10 1703+ 优先 PerMonitorV2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:  # Win8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:  # Vista 兜底
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_dpi_awareness()

__version__ = "0.1.0"
