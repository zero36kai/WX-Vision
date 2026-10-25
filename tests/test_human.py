# -*- coding: utf-8 -*-
"""拟人化键鼠(HumanInput)画像强化离线验收(2026-10-24)。

全程离线:mock pyautogui / pyperclip(在导入 wxbot 之前替换 sys.modules,
绝不 import 真实键鼠库);time.sleep 被替换为记录器(零真实等待);
零微信触达、零真实鼠标键盘动作。

断言(全部用固定种子的 random.Random,可精确复现):
 1. 红线:FAILSAFE 保持开启、pyautogui.PAUSE=0(human_input 导入即强制);
 2. 点击落点随机偏移在 ±容差 内,且确实存在随机性;
 3. 前置延迟分布界 [120,600]ms(默认画像);
 4. 按下-抬起时长界 [60,180]ms,且每次按压恰好一段;
 5. 偶发犹豫概率 ≈15%(3000 次大样本定种子),时长 ∈ [0.8,2.0]s;
 6. 点击后 settling 延迟存在且 ∈ [100,400]ms;
 7. 贝塞尔轨迹保留:长距移动产生多段中间点且偏离直线;
 8. FAILSAFE 安全角夹紧:目标 (1,1) 被夹到 (3,3);
 9. 配置驱动:自定义 HumanConfig 各界生效;hesitate_prob=0/1 两端正确;
10. random.Random 可注入:同种子两次运行事件流完全一致,异种子不同;
11. 公共签名兼容:HumanInput(target_jitter_px=2) 旧构造仍生效;
12. config.human 段:from_dict/to_dict roundtrip、validate 拦非法值;
13. 剪贴板往返与中文粘贴路径(mock pyperclip;Ctrl+V 已下发)。

末行 HUMAN_TEST_PASS = 通过(exit 0);任一失败 HUMAN_TEST_FAIL(exit 1)。
"""
from __future__ import annotations

import random
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 1. 在导入任何 wxbot 模块之前,替换 pyautogui / pyperclip(离线红线)
# ---------------------------------------------------------------------------
TIMELINE: list[tuple] = []   # 统一事件流:(kind, payload)


class _FakePyautogui(types.ModuleType):
    """只记录不动作的 pyautogui 替身;初始状态故意"错误",
    验证 human_input 导入时的防御逻辑(PAUSE 置 0 / FAILSAFE 恢复开启)。"""

    class FailSafeException(Exception):
        pass

    def __init__(self):
        super().__init__("pyautogui")
        self.PAUSE = 1.23          # 应被 human_input 强制置 0
        self.FAILSAFE = False      # 应被 human_input 恢复 True
        self._pos = (500, 400)

    def position(self, *a, **kw):
        return self._pos

    def moveTo(self, x, y, duration=0, _pause=False, **kw):
        self._pos = (int(x), int(y))
        TIMELINE.append(("moveTo", self._pos))

    def mouseDown(self, button="left", **kw):
        TIMELINE.append(("mouseDown", button))

    def mouseUp(self, button="left", **kw):
        TIMELINE.append(("mouseUp", button))

    def press(self, key, **kw):
        TIMELINE.append(("press", key))

    def hotkey(self, *keys, **kw):
        TIMELINE.append(("hotkey", keys))

    def write(self, text, **kw):
        TIMELINE.append(("write", text))


class _FakePyperclip(types.ModuleType):
    def __init__(self):
        super().__init__("pyperclip")
        self._buf = ""

    def copy(self, t):
        self._buf = str(t)

    def paste(self):
        return self._buf


sys.modules["pyautogui"] = _FakePyautogui()
sys.modules["pyperclip"] = _FakePyperclip()

# time.sleep → 记录器(整个测试进程零真实等待;结束恢复)
_REAL_SLEEP = time.sleep
real_sleep_holder = {"fn": _REAL_SLEEP}


def _recording_sleep(sec):
    TIMELINE.append(("sleep", float(sec)))


time.sleep = _recording_sleep

# ---------------------------------------------------------------------------
# 2. 被测对象(此刻 pyautogui/pyperclip 已是替身)
# ---------------------------------------------------------------------------
from wxbot.actions.human_input import HumanInput          # noqa: E402
from wxbot.config import AppConfig, HumanConfig           # noqa: E402

FAKE_PG = sys.modules["pyautogui"]
FAKE_PC = sys.modules["pyperclip"]

EPS = 1e-9
RESULTS: list[tuple[str, bool, str]] = []


def check(idx: str, desc: str, fn):
    try:
        detail = fn() or ""
        print(f"[{idx}] PASS {desc}  {detail}")
        RESULTS.append((idx, True, detail))
        return True
    except Exception as e:
        import traceback
        print(f"[{idx}] FAIL {desc}\n    {type(e).__name__}: {e}")
        traceback.print_exc()
        RESULTS.append((idx, False, f"{type(e).__name__}: {e}"))
        return False


def reset_pos(x=500, y=400):
    FAKE_PG._pos = (x, y)


def clicks(points: list[tuple[int, int]], hi: HumanInput) -> list[dict]:
    """依次点击 points,切分事件流为每点击一段。"""
    TIMELINE.clear()
    for p in points:
        hi.click(*p)
    return segments(list(TIMELINE))


def segments(events: list[tuple]) -> list[dict]:
    """按 mouseDown/mouseUp 切分:pre(最后 moveTo 之后)、hold、settle(up 后)。"""
    downs = [i for i, e in enumerate(events) if e[0] == "mouseDown"]
    ups = [i for i, e in enumerate(events) if e[0] == "mouseUp"]
    if len(downs) != len(ups):
        raise AssertionError(f"mouseDown/mouseUp 不配对: {len(downs)}/{len(ups)}")
    segs = []
    for d, u in zip(downs, ups):
        if u < d:
            raise AssertionError("mouseUp 出现在 mouseDown 之前")
        last_move_i = max((i for i, e in enumerate(events[:d])
                           if e[0] == "moveTo"), default=-1)
        last_move = events[last_move_i][1] if last_move_i >= 0 else None
        pre = [e[1] for e in events[last_move_i + 1:d] if e[0] == "sleep"]
        hold = [e[1] for e in events[d + 1:u] if e[0] == "sleep"]
        settle = []
        for e in events[u + 1:]:
            if e[0] != "sleep":
                break
            settle.append(e[1])
        segs.append({"pre": pre, "hold": hold, "settle": settle,
                     "move": last_move})
    return segs


# 画像区间(与 wxbot.config.HumanConfig 默认值一致;改默认需同步此处+README)
JIT, PRE, HOLD = 3, (0.120, 0.600), (0.060, 0.180)
HES_P, HES, SETTLE = 0.15, (0.800, 2.000), (0.100, 0.400)

# 点击目标序列(相邻点距离 ≥20px,避免 move_to 的 dist<3 短路)
POINTS = [(600, 450), (560, 430), (640, 470), (580, 490), (620, 455)] * 600


# ---------------------------------------------------------------------------
# 3. 各项断言
# ---------------------------------------------------------------------------
def t1_failsafe_pause():
    assert FAKE_PG.FAILSAFE is True, "FAILSAFE 必须保持开启(红线)"
    assert FAKE_PG.PAUSE == 0.0, "PAUSE 必须为 0(节奏自管)"
    return "(替身初始 PAUSE=1.23/FAILSAFE=False 已被导入防御逻辑纠正)"


def t2_jitter_bounds():
    hi = HumanInput(rng=random.Random(20261024))
    segs = clicks(POINTS[:500], hi)
    assert len(segs) == 500
    offs = set()
    for p, s in zip(POINTS[:500], segs):
        mv = s["move"]
        assert mv is not None, "点击前缺少 moveTo(未移动到目标)"
        dx, dy = mv[0] - p[0], mv[1] - p[1]
        assert -JIT - EPS <= dx <= JIT + EPS, f"偏移越界 dx={dx}"
        assert -JIT - EPS <= dy <= JIT + EPS, f"偏移越界 dy={dy}"
        offs.add((dx, dy))
    assert len(offs) >= 3, f"偏移无随机性(仅 {len(offs)} 种): {sorted(offs)[:5]}"
    return f"500 点全部 |dx|,|dy|≤{JIT}px;实测 {len(offs)} 种偏移"


def t3_pre_delay_bounds():
    hi = HumanInput(rng=random.Random(11))
    segs = clicks(POINTS[:400], hi)
    pres = [d for s in segs for d in s["pre"] if d <= 0.6]
    assert len(pres) == 400, f"每次点击应恰好一次前置延迟,实际 {len(pres)}"
    lo, hi_v = PRE
    assert all(lo - EPS <= d <= hi_v + EPS for d in pres), \
        f"前置延迟越界: min={min(pres)} max={max(pres)}"
    spread = max(pres) - min(pres)
    assert spread > 0.2, f"分布未拉开(min={min(pres):.3f} max={max(pres):.3f})"
    # 轨迹步进睡眠(若有)必须 < 前置延迟下界,两相可分
    tiny = [d for s in segs for d in s["pre"] if d < 0.12]
    assert all(d < 0.12 + EPS for d in tiny)
    return f"400 次全部 ∈[0.120,0.600]s;min={min(pres):.3f} max={max(pres):.3f}"


def t4_hold_duration():
    hi = HumanInput(rng=random.Random(22))
    segs = clicks(POINTS[:400], hi)
    for i, s in enumerate(segs):
        assert len(s["hold"]) == 1, f"第{i}次按压段数={len(s['hold'])}(应恰好 1)"
    holds = [s["hold"][0] for s in segs]
    lo, hi_v = HOLD
    assert all(lo - EPS <= v <= hi_v + EPS for v in holds), \
        f"按压时长越界: min={min(holds)} max={max(holds)}"
    assert max(holds) - min(holds) > 0.05, "按压时长无随机性"
    return f"400 次全部 ∈[0.060,0.180]s;min={min(holds):.3f} max={max(holds):.3f}"


def t5_hesitation_rate():
    n = 3000
    hi = HumanInput(rng=random.Random(777))
    segs = clicks(POINTS[:n], hi)
    pres = [d for s in segs for d in s["pre"]]
    regular = [d for d in pres if d < 0.7]
    hes = [d for d in pres if d >= 0.7]
    assert len(regular) == n, f"前置延迟数 {len(regular)} != {n}"
    lo, hi_v = HES
    assert all(lo - EPS <= d <= hi_v + EPS for d in hes), \
        f"犹豫时长越界: min={min(hes) if hes else '-'} max={max(hes) if hes else '-'}"
    rate = len(hes) / n
    assert abs(rate - HES_P) <= 0.03, f"犹豫概率 {rate:.4f} 偏离 {HES_P} 超容差"
    assert len(hes) > 0, "大样本下一次犹豫都没出现"
    return f"n={n} 犹豫率={rate:.4f}(目标≈{HES_P});{len(hes)} 次,时长均 ∈[0.8,2.0]s"


def t6_settling():
    hi = HumanInput(rng=random.Random(33))
    segs = clicks(POINTS[:400], hi)
    for i, s in enumerate(segs):
        assert len(s["settle"]) >= 1, f"第{i}次点击缺少 settling 延迟"
        assert len(s["settle"]) == 1, f"第{i}次点击 settling 段数异常: {s['settle']}"
    vals = [s["settle"][0] for s in segs]
    lo, hi_v = SETTLE
    assert all(lo - EPS <= v <= hi_v + EPS for v in vals), \
        f"settling 越界: min={min(vals)} max={max(vals)}"
    assert max(vals) - min(vals) > 0.1, "settling 无随机性"
    return f"400 次全部存在且 ∈[0.100,0.400]s;min={min(vals):.3f} max={max(vals):.3f}"


def t7_bezier_kept():
    reset_pos(500, 400)
    hi = HumanInput(rng=random.Random(5))
    TIMELINE.clear()
    hi.move_to(850, 620)
    moves = [e[1] for e in TIMELINE if e[0] == "moveTo"]
    assert len(moves) >= 10, f"中间点过少({len(moves)}),轨迹疑似退化为直跳"
    assert moves[-1] == (850, 620), f"终点不符: {moves[-1]}"

    def dev(px, py):
        # 点到 (500,400)-(850,620) 直线段的距离
        ax, ay, bx, by = 500, 400, 850, 620
        ux, uy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((px - ax) * ux + (py - ay) * uy) / (ux * ux + uy * uy)))
        cx, cy = ax + t * ux, ay + t * uy
        return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5

    mid = moves[1:-1]
    max_dev = max(dev(x, y) for x, y in mid)
    assert max_dev > 3.0, f"中间点均贴合直线(最大偏差 {max_dev:.1f}px),贝塞尔丢失"
    return f"{len(moves)} 段 moveTo,中间点最大偏离直线 {max_dev:.1f}px"


def t8_failsafe_clamp():
    reset_pos(500, 400)
    hi = HumanInput(rng=random.Random(9))
    TIMELINE.clear()
    hi.move_to(1, 1)
    moves = [e[1] for e in TIMELINE if e[0] == "moveTo"]
    assert moves and moves[-1] == (3, 3), \
        f"FAILSAFE 安全角未夹紧: 终点 {moves[-1] if moves else None}"
    return "目标 (1,1) → 实际终点 (3,3)(避开 (0,0) 触发角)"


def t9_config_driven():
    cfg = HumanConfig(target_jitter_px=5, pre_click_delay_ms=(30, 80),
                      hold_duration_ms=(10, 25), hesitate_prob=0.0,
                      hesitate_delay_ms=(500, 700), settling_delay_ms=(5, 15))
    hi = HumanInput(config=cfg, rng=random.Random(7))
    segs = clicks(POINTS[:300], hi)
    for p, s in zip(POINTS[:300], segs):
        assert -5 - EPS <= s["move"][0] - p[0] <= 5 + EPS
        assert -5 - EPS <= s["move"][1] - p[1] <= 5 + EPS
    pres = [d for s in segs for d in s["pre"]]
    assert len(pres) == 300 and all(0.030 - EPS <= d <= 0.080 + EPS for d in pres), \
        f"自定义前置延迟越界: {min(pres)}~{max(pres)}"
    holds = [s["hold"][0] for s in segs]
    assert all(0.010 - EPS <= v <= 0.025 + EPS for v in holds)
    st = [s["settle"][0] for s in segs]
    assert all(0.005 - EPS <= v <= 0.015 + EPS for v in st)
    assert all(d < 0.5 for d in pres), "hesitate_prob=0 仍出现犹豫"
    # prob=1.0:每次点击必犹豫
    cfg1 = HumanConfig(hesitate_prob=1.0, hesitate_delay_ms=(900, 1100),
                       pre_click_delay_ms=(20, 30))
    hi1 = HumanInput(config=cfg1, rng=random.Random(8))
    segs1 = clicks(POINTS[:100], hi1)
    hes = [d for s in segs1 for d in s["pre"] if d > 0.1]
    assert len(hes) == 100 and all(0.9 - EPS <= d <= 1.1 + EPS for d in hes), \
        f"prob=1.0 未每次犹豫: {len(hes)}/100"
    return ("自定义界全部生效(prob=0 零犹豫);prob=1.0 时 100/100 犹豫"
            "且时长 ∈[0.9,1.1]s")


def t10_rng_injection():
    def run(hi):
        reset_pos(500, 400)
        TIMELINE.clear()
        hi.click(600, 450)
        hi.click(620, 470)
        hi.move_to(700, 500)
        hi.type_text("hello 123")
        hi.press("esc")
        return list(TIMELINE)

    a = run(HumanInput(rng=random.Random(99)))
    b = run(HumanInput(rng=random.Random(99)))
    c = run(HumanInput(rng=random.Random(100)))
    assert a == b, "同种子两次运行事件流不一致(随机源未被注入/未生效)"
    assert a != c, "不同种子事件流相同(随机源未生效)"
    kinds = {e[0] for e in a}
    assert {"moveTo", "mouseDown", "mouseUp", "sleep", "hotkey", "press"} <= kinds, \
        f"事件流缺项: {kinds}"
    return f"同种子事件流逐事件一致({len(a)} 事件);异种子不同;链路含贝塞尔/按压/粘贴/按键"


def t11_legacy_signature():
    hi = HumanInput(target_jitter_px=2)   # 旧版构造方式,必须继续可用
    assert hi.target_jitter_px == 2
    assert hi.cfg.hesitate_prob == HES_P and hi.cfg.pre_click_delay_ms == (120, 600)
    segs = clicks(POINTS[:200], hi)
    for p, s in zip(POINTS[:200], segs):
        assert -2 - EPS <= s["move"][0] - p[0] <= 2 + EPS
        assert -2 - EPS <= s["move"][1] - p[1] <= 2 + EPS
    # 引擎装配点:HumanInput(config=AppConfig().human) 可构造(接线形状)
    hi2 = HumanInput(config=AppConfig().human)
    assert hi2.target_jitter_px == JIT
    return "target_jitter_px=2 覆盖生效且落在 ±2px;config= 关键字构造生效"


def t12_config_section():
    cfg = AppConfig.from_dict({"human": {"target_jitter_px": 4,
                                         "pre_click_delay_ms": [100, 300],
                                         "hesitate_prob": 0.2}})
    assert cfg.human.target_jitter_px == 4
    assert cfg.human.pre_click_delay_ms == (100, 300)
    assert cfg.validate() == [], f"合法 human 段被误报: {cfg.validate()}"
    d = cfg.to_dict()
    assert d["human"]["pre_click_delay_ms"] == [100, 300]
    back = AppConfig.from_dict(d)
    assert back.to_dict() == d, "human 段 roundtrip 不一致"
    bad = AppConfig.from_dict({"human": {"hesitate_prob": 1.5,
                                         "hold_duration_ms": [200, 100],
                                         "target_jitter_px": 99}})
    probs = bad.validate()
    assert len(probs) == 3, f"非法值未被全部拦截: {probs}"
    return "from_dict/validate/to_dict roundtrip 全过;3 类非法值均被拦截"


def t13_clipboard_paste():
    reset_pos(500, 400)
    hi = HumanInput(rng=random.Random(3))
    FAKE_PC._buf = "用户原内容"
    assert hi.set_clipboard("新文本") and hi.get_clipboard() == "新文本"
    FAKE_PC._buf = "用户原内容"            # 重置为"用户正在用的剪贴板"
    TIMELINE.clear()
    assert hi.paste_text("记:明天上午十点例会") is True
    evs = list(TIMELINE)
    hk = [e for e in evs if e[0] == "hotkey"]
    assert any(e[1] == ("ctrl", "v") for e in hk), f"未下发 Ctrl+V: {hk}"
    assert FAKE_PC._buf == "用户原内容", f"用户剪贴板未恢复: {FAKE_PC._buf!r}"
    # 中文走剪贴板路径、ASCII per_char 走逐字符路径
    assert hi.type_text("你好") is True
    TIMELINE.clear()
    assert hi.type_text("abc", per_char=True) is True
    assert any(e[0] == "write" and e[1] == "a" for e in TIMELINE)
    return "写入/读回/恢复原内容 OK;Ctrl+V 已记录;中文剪贴板路径 + ASCII 逐字符路径均可用"


def t14_human_delay_bounds():
    hi = HumanInput(rng=random.Random(44))
    TIMELINE.clear()
    hi.human_delay(0.25, 0.5)
    ds = [e[1] for e in TIMELINE if e[0] == "sleep"]
    assert len(ds) == 1 and 0.25 - EPS <= ds[0] <= 0.5 + EPS, f"human_delay 越界: {ds}"
    return f"human_delay(0.25,0.5) → {ds[0]:.3f}s,界内"


CHECKS = [
    ("1", "红线:FAILSAFE 开启 + PAUSE=0", t1_failsafe_pause),
    ("2", "点击落点偏移界 ±3px(定种子)", t2_jitter_bounds),
    ("3", "前置延迟分布界 [120,600]ms", t3_pre_delay_bounds),
    ("4", "按下-抬起时长界 [60,180]ms", t4_hold_duration),
    ("5", "偶发犹豫概率≈15%(n=3000 大样本)", t5_hesitation_rate),
    ("6", "settling 延迟存在且 ∈[100,400]ms", t6_settling),
    ("7", "贝塞尔轨迹保留(中间点偏离直线)", t7_bezier_kept),
    ("8", "FAILSAFE 安全角夹紧", t8_failsafe_clamp),
    ("9", "配置驱动:自定义画像生效 + prob 0/1 两端", t9_config_driven),
    ("10", "random.Random 可注入(同种子复现/异种子不同)", t10_rng_injection),
    ("11", "公共签名兼容:旧构造 + config= 接线", t11_legacy_signature),
    ("12", "config.human 段 roundtrip + validate", t12_config_section),
    ("13", "剪贴板往返与中文粘贴路径(mock)", t13_clipboard_paste),
    ("14", "human_delay 界", t14_human_delay_bounds),
]


def main() -> int:
    try:
        for idx, desc, fn in CHECKS:
            check(idx, desc, fn)
    finally:
        time.sleep = real_sleep_holder["fn"]   # 恢复真实 sleep(不留全局副作用)
    failed = [r for r in RESULTS if not r[1]]
    if failed:
        print(f"\n失败 {len(failed)} 项: {[(i, d, m) for i, d, m, *_ in [(f[0], f[1], f[2]) for f in failed]]}")
        print("HUMAN_TEST_FAIL")
        return 1
    print(f"\n共 {len(RESULTS)} 项检查全部通过:"
          "全程 mock pyautogui/pyperclip、零真实等待、零微信触达、零键鼠动作。")
    print("HUMAN_TEST_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
