# -*- coding: utf-8 -*-
"""只读验收脚本(ARCHITECTURE §6 验收标准)。

exit 0 = 通过(末行打印 READONLY_PASS);任一检查失败 exit 1。

验收内容:
1. 找到微信窗口并输出 hwnd/rect;
2. ensure_active(恢复+置前);
3. PrintWindow 截图 + RapidOCR 识别;
4. open_chat(目标联系人) 成功且聊天标题 OCR 结果 == 目标联系人
   (目标联系人 = 环境变量 WECHAT_TEST_CONTACT → config.json 白名单[0]
   → 兜底"文件传输助手";不硬编码);
5. read_messages() 输出结构化消息(含 side 归属判定);
6. human_input 剪贴板往返自测(置入测试文本→读回比对→恢复用户原剪贴板);
7. 末行打印 READONLY_PASS 并 exit 0。

红线:全程零发送、不触碰聊天输入框(消息编辑框)、不按 Enter/Alt+S;
pyautogui FAILSAFE 保持开启;只读操作 + 搜索框内的粘贴(边界允许)。
附加的信息性探测(Ctrl+F 聚焦、搜索流程实测)不影响退出码,结论写入 issues。
"""
from __future__ import annotations

import os
import sys
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from wxbot.actions.flows import WeChatFlows
from wxbot.actions.human_input import HumanInput
from wxbot.vision import locate
from wxbot.vision.ocr import run_ocr
from wxbot.vision.window import WeChatWindow

DEBUG_DIR = ROOT / "tests" / "_debug"
RESULTS: list[tuple[str, bool, str]] = []


def _target_contact() -> str:
    """验收目标联系人,按优先级取:
    1) 环境变量 WECHAT_TEST_CONTACT(显式指定,优先);
    2) config.json 白名单[0](跟随本机配置);
    3) 兜底"文件传输助手"(微信内置联系人,人人都有,不含任何用户信息)。

    红线:测试只对该联系人做只读操作,代码与文档不硬编码任何真实联系人名
    (隐私要求)。"""
    env = os.environ.get("WECHAT_TEST_CONTACT", "").strip()
    if env:
        return env
    try:
        from wxbot.config import load_config
        cfg = load_config(ROOT / "config.json", create_default=False)
        for name in cfg.safety.whitelist:
            if isinstance(name, str) and name.strip():
                return name.strip()
    except Exception:
        pass  # config 缺失/损坏:回退到微信内置联系人
    return "文件传输助手"


def check(idx: str, desc: str, fn):
    print(f"\n[{idx}] {desc}")
    try:
        detail = fn()
        detail = detail or ""
        print(f"    => PASS {detail}")
        RESULTS.append((idx, True, detail))
        return True
    except Exception as e:
        print(f"    => FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()
        RESULTS.append((idx, False, f"{type(e).__name__}: {e}"))
        return False


def main() -> int:
    import pyautogui
    assert pyautogui.FAILSAFE, "FAILSAFE 必须保持开启"

    TARGET = _target_contact()
    print(f"[目标] 联系人 = {TARGET!r}(WECHAT_TEST_CONTACT / config 白名单[0] / 兜底文件传输助手)")

    win = WeChatWindow.find()
    hi = HumanInput()
    flows = WeChatFlows(win, hi, run_ocr, debug_dir=DEBUG_DIR) if win else None

    # ---- 1. 找到微信窗口 ----
    def _check1():
        nonlocal win, flows
        if win is None:
            raise AssertionError("未找到微信窗口(class=Qt51514QWindowIcon 且 title=微信)")
        flows = WeChatFlows(win, hi, run_ocr, debug_dir=DEBUG_DIR)
        return f"hwnd={win.hwnd} rect={win.rect_screen} size={win.size}"

    check("1", "找到微信窗口并输出 hwnd/rect", _check1)
    if win is None:
        print("READONLY_FAIL: 微信窗口未找到")
        return 1

    # ---- 2. ensure_active ----
    def _check2():
        ok = win.ensure_active()
        assert ok, "ensure_active 返回 False(GetForegroundWindow != hwnd)"
        assert win.is_foreground(), "微信窗口不在前台"
        assert not win.is_minimized(), "微信窗口仍处于最小化"
        return "已恢复+置前"

    check("2", "ensure_active(恢复+置前)", _check2)

    # ---- 3. PrintWindow 截图 + RapidOCR ----
    def _check3():
        t0 = time.time()
        img = win.capture(use_printwindow=True)   # PrintWindow 主路
        t_cap = (time.time() - t0) * 1000
        assert img.ndim == 3 and img.shape[2] == 3, f"截图形状异常: {img.shape}"
        assert float(img.mean()) > 2.0, "PrintWindow 截图疑似全黑"
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        locate.imwrite_u(DEBUG_DIR / "capture_printwindow.png", img)
        t1 = time.time()
        items = run_ocr(img)
        t_ocr = (time.time() - t1) * 1000
        assert len(items) >= 5, f"OCR 结果过少({len(items)} 条),识别可能失败"
        sample = [it.text for it in items[:8]]
        mss_ok = "?"
        try:  # mss 回退路可用性(信息性)
            img2 = win.capture(use_printwindow=False)
            mss_ok = "OK" if img2.shape == img.shape else f"shape差异{img2.shape}"
        except Exception as e:
            mss_ok = f"不可用({type(e).__name__})"
        return (f"size={img.shape[1]}x{img.shape[0]} 截图{t_cap:.0f}ms "
                f"OCR {len(items)}条/{t_ocr:.0f}ms mss回退:{mss_ok} 样例:{sample}")

    check("3", "PrintWindow 截图 + RapidOCR 识别", _check3)

    # ---- 4. open_chat(TARGET) + 标题校验 ----
    def _check4():
        ok = flows.open_chat(TARGET)
        assert ok, f"open_chat({TARGET!r}) 返回 False(搜索流程受阻: {getattr(flows, '_last_open_chat_error', '?')})"
        _, items = flows._capture_ocr()
        title_item = locate.find_text(
            items, TARGET, locate.region_px(win.size, "title_bar"), exact=True)
        assert title_item is not None, f"open_chat 后标题栏 OCR 未找到精确 {TARGET!r}"
        return f"标题OCR={title_item.text!r} (conf={title_item.conf})"

    check("4", f'open_chat(目标联系人) 成功且聊天标题 OCR == 目标联系人', _check4)

    # ---- 5. read_messages 结构化消息 ----
    def _check5():
        msgs = flows.read_messages()
        assert len(msgs) >= 1, "read_messages 未解析出任何消息"
        for m in msgs:
            assert m.side in ("left", "right"), f"side 非法: {m.side!r}"
            assert m.text.strip(), "存在空文本消息"
        rights = sum(1 for m in msgs if m.side == "right")
        preview = [
            f"({'自己' if m.side == 'right' else m.sender}"
            f"{'' if not m.ts else '@' + m.ts}){m.text[:24]!r}"
            for m in msgs[:3]
        ]
        return f"{len(msgs)}条(right自={rights}, left对={len(msgs)-rights}) 前3: {preview}"

    check("5", "read_messages() 结构化消息(含 side 归属)", _check5)

    # ---- 6. 剪贴板往返自测 ----
    def _check6():
        original = hi.get_clipboard()
        sample = f"CLIPBOARD-自测-{uuid.uuid4().hex[:8]}"
        assert hi.set_clipboard(sample), "set_clipboard 写入失败"
        back = hi.get_clipboard()
        assert back == sample, f"剪贴板往返不一致: {back!r} != {sample!r}"
        assert hi.set_clipboard(original), "恢复用户原剪贴板失败"
        restored = hi.get_clipboard()
        assert restored == original, f"恢复后不一致: {restored!r} != {original!r}"
        return f"往返OK; 原剪贴板({len(original)}字符)已恢复"

    check("6", "human_input 剪贴板往返自测(含恢复原内容)", _check6)

    # ---- 信息性探测(不影响退出码):Ctrl+F 是否聚焦搜索框 ----
    def _probe_ctrl_f():
        win.ensure_active()
        img0 = win.capture()
        hi.press("ctrl", "f")
        time.sleep(0.6)
        img1 = win.capture()
        hi.human_delay(0.3, 0.5)
        img2 = win.capture()
        hi.press("esc")
        time.sleep(0.5)
        img3 = win.capture()
        import numpy as np
        def diff(a, b):
            return float(np.abs(a.astype(int) - b.astype(int)).mean())
        d1, d2, d3 = diff(img0, img1), diff(img0, img2), diff(img0, img3)
        if max(d1, d2) > 1.5 and d3 < 0.8:
            return f"聚焦出现界面变化(d={d1:.2f}/{d2:.2f})且 Esc 后复原({d3:.2f})→ Ctrl+F 可用"
        if max(d1, d2) > 1.5:
            return f"界面有变化(d={d1:.2f}/{d2:.2f})但 Esc 未完全复原(d3={d3:.2f})→ 需人工复核"
        return f"无可见变化(d={d1:.2f}/{d2:.2f}/{d3:.2f})→ Ctrl+F 疑似未聚焦搜索框"

    print("\n[附1] Ctrl+F 聚焦搜索框探测(只按键不输入,信息性):")
    try:
        print(f"    => {_probe_ctrl_f()}")
    except Exception as e:
        print(f"    => 探测异常(不影响验收): {type(e).__name__}: {e}")

    # ---- 信息性探测(不影响退出码):完整搜索流程实测 ----
    # (验收项 4 的 open_chat 走了"当前聊天已是目标联系人"快路径;此处强制走
    #  点搜索框→粘贴→点结果→标题验证 全流程,证据图存 tests/_debug/)
    def _probe_search_flow():
        ok = flows.open_chat(TARGET, force_search=True)
        time.sleep(0.3)
        title = flows._ocr_title()
        # 恢复状态:关掉可能残留的搜索弹层,确认仍停在目标联系人
        hi.press("esc")
        time.sleep(0.4)
        return f"force_search open_chat={'成功' if ok else '失败'}; 结束时标题OCR={title!r}"

    print(f"\n[附2] 完整搜索流程实测(点搜索框→粘贴{TARGET}→点结果,信息性):")
    try:
        print(f"    => {_probe_search_flow()}")
    except Exception as e:
        print(f"    => 探测异常(不影响验收): {type(e).__name__}: {e}")

    # ---- 7. 汇总 ----
    failed = [r for r in RESULTS if not r[1]]
    if failed:
        print(f"\n失败项: {[(i, d) for i, d, _ in failed]}")
        print("READONLY_FAIL")
        return 1
    print("\n全部检查通过;全程零发送、未触碰聊天输入框、未按 Enter/Alt+S。")
    print("READONLY_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
