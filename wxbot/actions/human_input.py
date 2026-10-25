# -*- coding: utf-8 -*-
"""仿真人键鼠输入:三阶贝塞尔轨迹 / 随机停顿 / 打字节奏 / 剪贴板。

契约(ARCHITECTURE §3):
    class HumanInput:
        move_to(x, y)          # 三阶贝塞尔+随机控制点+变速
        click(x, y)            # 移动→犹豫?→前置停顿→按下/抬起分离→settling
        double_click / right_click
        press(*keys)           # 组合键
        type_text(text)        # 中文:剪贴板+Ctrl+V;英文数字:可选逐字符随机节奏
        human_delay(lo, hi)

拟人画像强化(2026-10-24,配置驱动 config.human,见 wxbot.config.HumanConfig):
- 点击落点在目标 ±target_jitter_px(默认 ±3px)内随机偏移;
- 点击前置随机停顿 pre_click_delay_ms(默认 120~600ms);
- 按下-抬起时长随机 hold_duration_ms(默认 60~180ms);
- 偶发犹豫:每次点击前以 hesitate_prob(默认 15%)概率附加
  hesitate_delay_ms(默认 0.8~2s)停顿;
- 点击后 settling 随机延迟 settling_delay_ms(默认 100~400ms);
- 贝塞尔移动轨迹保留(不受画像参数影响);
- 全部随机数经可注入的 random.Random 实例(构造参数 rng),测试可定种子
  精确复现;未注入时新建独立实例(线程安全性同标准库 random.Random)。

守则(调研02 §4.4):
- pyautogui.PAUSE = 0(节奏由本类自管随机延迟,避免两层延迟叠加);
- FAILSAFE 绝不关闭:鼠标到达 (0,0) 角立即抛 FailSafeException 中止;
  轨迹点若恰好落在左上角安全区,做像素级夹紧避开;
- 中文输入一律剪贴板+Ctrl+V(pyautogui.write 不支持中文),粘贴前后
  保存/恢复用户剪贴板(ARCHITECTURE §5.3)。
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import replace

import pyautogui
import pyperclip

from wxbot.config import HumanConfig

pyautogui.PAUSE = 0.0        # 节奏自管;FAILSAFE 保持默认开启,绝不关闭
if not pyautogui.FAILSAFE:   # 防御:若被上游误关,恢复开启
    pyautogui.FAILSAFE = True

_FAILSAFE_ZONE = 3           # 距 (0,0) 小于此像素的点视为安全绳区,夹紧避开


def _clamp_failsafe(x: int, y: int) -> tuple[int, int]:
    """避开 pyautogui FAILSAFE 触发角(仅主屏左上 (0,0) 一点)。"""
    if abs(x) < _FAILSAFE_ZONE and abs(y) < _FAILSAFE_ZONE:
        return (_FAILSAFE_ZONE, _FAILSAFE_ZONE)
    return (x, y)


class HumanInput:
    """人因级键鼠输入。坐标一律为屏幕绝对坐标(物理像素)。

    行为画像由 wxbot.config.HumanConfig 描述(配置段 config.human);
    引擎装配传 HumanInput(config=cfg.human) 即全链路生效。
    公共签名兼容:HumanInput() 与 HumanInput(target_jitter_px=2) 的旧
    调用方式不变——显式传 target_jitter_px 时覆盖画像里的同名项。"""

    def __init__(
        self,
        target_jitter_px: int | None = None,
        config: HumanConfig | None = None,
        rng: random.Random | None = None,
    ):
        cfg = config if isinstance(config, HumanConfig) else HumanConfig()
        if target_jitter_px is not None:
            cfg = replace(cfg, target_jitter_px=int(target_jitter_px))
        self.cfg = cfg
        # 兼容旧公开属性:外部若直接读写 self.target_jitter_px 仍然有效
        self.target_jitter_px = int(cfg.target_jitter_px)
        self._rng = rng if rng is not None else random.Random()

    # ---------- 基础延迟 ----------
    def human_delay(self, lo: float, hi: float) -> None:
        time.sleep(self._rng.uniform(lo, hi))

    def _sleep_ms(self, lo_ms: float, hi_ms: float) -> None:
        """画像参数均为毫秒闭区间 [lo, hi];换算为秒睡眠。"""
        time.sleep(self._rng.uniform(lo_ms, hi_ms) / 1000.0)

    def _jitter(self, x: int, y: int) -> tuple[int, int]:
        """目标点在 ±target_jitter_px 容差内随机偏移(整数像素)。"""
        j = int(self.target_jitter_px)
        if j <= 0:
            return (int(x), int(y))
        return (int(x) + self._rng.randint(-j, j),
                int(y) + self._rng.randint(-j, j))

    def _maybe_hesitate(self) -> None:
        """偶发犹豫:以 hesitate_prob 概率附加一次 hesitate_delay_ms 停顿。"""
        p = float(self.cfg.hesitate_prob)
        if p > 0 and self._rng.random() < p:
            self._sleep_ms(*self.cfg.hesitate_delay_ms)

    def _press_hold(self, button: str = "left") -> None:
        """按下 → 随机按压时长(hold_duration_ms)→ 抬起。"""
        pyautogui.mouseDown(button=button)
        self._sleep_ms(*self.cfg.hold_duration_ms)
        pyautogui.mouseUp(button=button)

    # ---------- 鼠标 ----------
    def move_to(self, x: int, y: int) -> None:
        """三阶贝塞尔轨迹移动:随机控制点 + 变速(先快后慢渐入渐出)。"""
        x, y = int(x), int(y)
        x0, y0 = pyautogui.position()
        dist = math.hypot(x - x0, y - y0)
        if dist < 3:
            pyautogui.moveTo(x, y, duration=0)
            return
        # 随机控制点:沿起点→终点方向的 30%/70% 处,横向随机偏移 ±15% 距离
        def _ctrl(f: float) -> tuple[float, float]:
            bx = x0 + (x - x0) * f
            by = y0 + (y - y0) * f
            off = self._rng.uniform(-0.15, 0.15) * dist
            # 垂直方向偏移
            dx, dy = (x - x0) / dist, (y - y0) / dist
            return (bx - dy * off, by + dx * off)

        c1, c2 = _ctrl(self._rng.uniform(0.25, 0.4)), _ctrl(self._rng.uniform(0.6, 0.75))
        duration = min(1.1, max(0.28, 0.25 + dist / 1600.0))
        steps = int(min(70, max(14, dist / 9.0)))
        for i in range(1, steps + 1):
            t = i / steps
            # 渐入渐出变速(smoothstep)
            te = t * t * (3 - 2 * t)
            mt = 1 - te
            px = mt**3 * x0 + 3 * mt**2 * te * c1[0] + 3 * mt * te**2 * c2[0] + te**3 * x
            py = mt**3 * y0 + 3 * mt**2 * te * c1[1] + 3 * mt * te**2 * c2[1] + te**3 * y
            sx, sy = _clamp_failsafe(int(px), int(py))
            pyautogui.moveTo(sx, sy, duration=0, _pause=False)
            if i < steps:   # 末步不再睡眠:轨迹到此为止,后续停顿交给画像参数
                time.sleep(duration / steps * self._rng.uniform(0.5, 1.5))

    def click(self, x: int, y: int) -> None:
        """点击(拟人画像全链路):
        落点 ±jitter 随机偏移 → 贝塞尔移动 → 前置随机停顿(120~600ms)
        → 偶发犹豫(15% 概率 +0.8~2s)→ 按下-抬起(60~180ms)
        → settling 随机延迟(100~400ms)。"""
        jx, jy = self._jitter(x, y)
        self.move_to(jx, jy)
        self._sleep_ms(*self.cfg.pre_click_delay_ms)
        self._maybe_hesitate()
        self._press_hold("left")
        self._sleep_ms(*self.cfg.settling_delay_ms)

    def double_click(self, x: int, y: int) -> None:
        """双击:完整拟人单击(含画像停顿)+ 短间隔 + 第二次按下/抬起。"""
        self.click(x, y)
        self._sleep_ms(40, 120)          # 两次按压之间的间隔
        self._press_hold("left")

    def right_click(self, x: int, y: int) -> None:
        """右键:与 click 同一拟人画像(偏移/停顿/犹豫/按压/settling)。"""
        jx, jy = self._jitter(x, y)
        self.move_to(jx, jy)
        self._sleep_ms(*self.cfg.pre_click_delay_ms)
        self._maybe_hesitate()
        self._press_hold("right")
        self._sleep_ms(*self.cfg.settling_delay_ms)

    # ---------- 键盘 ----------
    def press(self, *keys: str) -> None:
        """组合键(单个键用 press,多个键用 hotkey)。"""
        self.human_delay(0.10, 0.30)
        if not keys:
            return
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
        self.human_delay(0.08, 0.20)

    # ---------- 剪贴板 ----------
    def set_clipboard(self, text: str) -> bool:
        """写入剪贴板,带重试(剪贴板可能被其他进程短暂占用)。

        注:旧版本为 @staticmethod,2026-10-24 改为实例方法以统一走可注入
        随机源(重试退避抖动);全部既有调用均为实例调用,签名兼容。"""
        for i in range(5):
            try:
                pyperclip.copy(str(text))
                return True
            except Exception:
                time.sleep(0.12 + 0.08 * i + self._rng.uniform(0, 0.05))
        return False

    def get_clipboard(self) -> str:
        """读取剪贴板文本;非文本内容或被占用返回空串。"""
        for i in range(3):
            try:
                data = pyperclip.paste()
                return data if isinstance(data, str) else ""
            except Exception:
                time.sleep(0.12 + 0.08 * i)
        return ""

    def paste_text(self, text: str, restore_clipboard: bool = True) -> bool:
        """剪贴板 + Ctrl+V 粘贴(中文输入标准路径)。

        保存用户原剪贴板 → 写入 text → Ctrl+V → 等待粘贴生效 → 恢复原内容。"""
        original = self.get_clipboard()
        if not self.set_clipboard(text):
            return False
        self.human_delay(0.10, 0.25)
        pyautogui.hotkey("ctrl", "v")
        self.human_delay(0.30, 0.60)   # 等目标窗口完成粘贴,再恢复剪贴板
        if restore_clipboard:
            # 恢复失败不影响主流程(仅用户剪贴板状态),静默重试即可
            self.set_clipboard(original)
        return True

    def type_text(self, text: str, per_char: bool = False) -> bool:
        """输入文本。中文/含非 ASCII:剪贴板+Ctrl+V;纯 ASCII 可选逐字符随机节奏。"""
        if not text:
            return True
        ascii_only = text.isascii()
        if per_char and ascii_only:
            for i, ch in enumerate(text):
                pyautogui.write(ch)
                self.human_delay(0.05, 0.18)
                if ch == " " and self._rng.random() < 0.2:
                    self.human_delay(0.20, 0.35)   # 空格后偶发思考停顿
            return True
        return self.paste_text(text)
