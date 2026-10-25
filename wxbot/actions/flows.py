# -*- coding: utf-8 -*-
"""高层流程:open_chat / read_messages / send_message / check_unread。

契约(ARCHITECTURE §3、§4):
- open_chat:点搜索框→剪贴板输入name→等待结果→OCR结果列表→点击 name
  精确匹配项→等绘制→OCR标题栏验证==name,失败重试1次后放弃返回 False;
- read_messages:截消息区→OCR→按 三信号(气泡颜色+左右位置+头像区评分)
  判定归属(wxauto4 算法,调研01 §0.4);
- send_message:【红线】发送前 OCR 标题栏必须==expect_contact 且其在白名单;
  受管群例外(2026-10-24 审查修复):三闸门(groups.enabled + 群∈groups.
  whitelist + groups.allow_real_send)全开的受管群凭三闸门放行,无需把群名
  加入联系人白名单(README 4.2 明确禁止),但标题必须呈「群名 + 成员数
  后缀」群聊形态(否则与私聊无法区分,fail-closed);标题栏全部文本项呈
  群聊形态时按**群聊三闸门**校验(默认关闭一律拦截),
  见 send_message 护栏①/①b/③;
- check_unread:导航"聊天"图标红像素预检(毫秒级)→聊天列表角标定位来源。

只读红线:演练模式下严禁发送任何消息、严禁触碰聊天输入框(消息编辑框)。
send_message 已实现但仅供引擎投递路径调用,入口带双重安全护栏。
"""
from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from wxbot.actions.human_input import HumanInput
from wxbot.config import GroupsConfig
from wxbot.vision import locate
from wxbot.vision.ocr import OcrItem
from wxbot.vision.window import WeChatWindow


class SafetyError(RuntimeError):
    """安全护栏触发(白名单/标题校验失败等)。"""


@dataclass
class Message:
    sender: str          # "self" | 联系人名
    text: str
    side: str            # "left"|"right"(left=对方,right=自己)
    ts: Optional[str] = None


# 时间分割线的常见形态(居中灰色小字,调研01 §2)
_TS_PATTERN = re.compile(
    r"^(昨天|今天|前天|星期[一二三四五六日天]|\d{1,2}月\d{1,2}日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})"
    r"(\s*(上午|中午|下午|晚上)?\s*\d{1,2}:\d{2})?$"
    r"|^\d{1,2}:\d{2}$"
)
_SYSTEM_HINTS = ("撤回了一条消息", "以上是新消息", "开启了朋友验证", "不是你的朋友",
                 "加入了群聊")   # 末项:2026-10-24 实机群校准(邀请进群系统条会混入消息流)

# 群聊标题 = 群名 + 成员数后缀「(23)」(调研01:4.x 标题栏含 current_chat_count_label;
# OCR 可能与群名合并读作一项「测试群 (23)」,或分开两项)。实机待校准:当前账号
# 聊天列表无群聊(2026-10-24 只读校准),按 4.x 常见形态实现。
_GROUP_TITLE_RE = re.compile(r"^(.*?)\s*[（(](\d{1,5})\s*/?\s*[)）]\s*$")

# 「昵称: 正文」前缀形态(群聊 OCR 把昵称与正文合并为一行时)
_NICK_PREFIX_RE = re.compile(r"^([^:：\n]{1,24})[:：]\s*(.*)$")


def split_group_title(title: str) -> tuple[str, Optional[int]]:
    """「测试群 (23)」→ ("测试群", 23);非成员数形态 → (原文, None)。

    只在有成员数后缀时才认定是群标题:普通联系人名(哪怕带括号内容,
    如「张三(公司)」)因括号内非纯数字不会被误判。"""
    t = (title or "").strip()
    m = _GROUP_TITLE_RE.match(t)
    if not m:
        return t, None
    name = m.group(1).strip()
    if not name:
        return t, None
    try:
        return name, int(m.group(2))
    except ValueError:
        return t, None


def _looks_like_nickname(text: str) -> bool:
    """「昵称: 正文」前缀是否像昵称(防把时间/URL/数字误当昵称)。"""
    nick = (text or "").strip()
    if not nick or len(nick) > 24:
        return False
    low = nick.lower()
    if "http" in low or "/" in nick or "\\" in nick:
        return False
    if nick.isdigit():
        return False
    if _TS_PATTERN.match(nick):            # "10:00" 等时间形态
        return False
    return True


class WeChatFlows:
    def __init__(
        self,
        win: WeChatWindow,
        hi: HumanInput,
        ocr_fn: Callable[..., list[OcrItem]],
        whitelist: Optional[list[str]] = None,
        debug_dir: Optional[str | Path] = None,
        groups_cfg: Optional[GroupsConfig] = None,
    ):
        self.win = win
        self.hi = hi
        self.ocr = ocr_fn
        # 红线:白名单默认 ["文件传输助手"],发送动作只对名单内联系人放行
        self.whitelist = list(whitelist) if whitelist else ["文件传输助手"]
        # 群聊三闸门配置(2026-10-24):None = 未接入配置 → fail-closed,
        # 群真实发送一律拦截(与旧「群形态硬禁」行为一致)
        self.groups_cfg = groups_cfg
        self.debug_dir = Path(debug_dir) if debug_dir else None
        if self.debug_dir:
            self.debug_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 基础件 ----------
    def _capture_ocr(self, min_conf: float = 0.5) -> tuple[np.ndarray, list[OcrItem]]:
        self.win.ensure_active()
        img = self.win.capture()
        return img, self.ocr(img, min_conf=min_conf)

    def _to_screen(self, lx: int, ly: int) -> tuple[int, int]:
        """窗口本地坐标 → 屏幕绝对坐标(点击必须用屏幕坐标,ARCHITECTURE §5.1)。"""
        l, t, _, _ = self.win.rect_screen
        return (l + int(lx), t + int(ly))

    def _debug_save(self, tag: str, img: np.ndarray) -> None:
        if self.debug_dir is None:
            return
        try:
            locate.imwrite_u(self.debug_dir / f"{tag}_{int(time.time())}.png", img)
        except Exception:
            pass

    def _ocr_title(self, items: Optional[list[OcrItem]] = None) -> str:
        """OCR 聊天标题栏文本。这是'只发给白名单联系人'红线的技术落点。

        标题文字(如 "文件传输助手")在区域最左侧;右侧 ⊙📞⋯ 等图标可能被 OCR 误读为
        符号,故取区域内最左的"有效文本项"(含 CJK/字母数字,非纯符号)。"""
        if items is None:
            _, items = self._capture_ocr()
        rect = locate.region_px(self.win.size, "title_bar")

        def _valid(t: str) -> bool:
            return bool(t) and any(ch.isalnum() or ord(ch) > 0x2E80 for ch in t)

        cands = [
            it for it in items
            if locate._in_rect(it.center, rect) and _valid(it.text.strip())
        ]
        if not cands:
            return ""
        cands.sort(key=lambda it: it.center[0])
        return cands[0].text.strip()

    def _title_group_form(self, items: list[OcrItem]) -> Optional[str]:
        """标题栏是否呈群聊形态(红线专用:全部有效文本项参与判定)。

        依据本模块头部已知 OCR 两种形态:群名与成员数后缀可能合并成一项
        (「测试群 (23)」),也可能拆成两项(「测试群」「(23)」)。
        _ocr_title 只取最左项,拆项形态下最左项恰为群名本身——若只比较
        最左项,「群名被混入人名白名单」时护栏②会被骗过(2026-10-24
        审查发现的发送缺口)。本方法把标题栏全部有效文本项按 x 序拼接
        后再做群形态判定;任一单项自身或拼接结果命中「名称 + 纯数字
        成员数后缀」(split_group_title → members 非 None)即认定群聊。

        返回触发判定的文本(用于报错信息);非群形态返回 None。
        失败关闭(fail-closed):普通私聊标题若被 OCR 读出「(数字)」形
        干扰项同样会被拦——宁可错拦,群真实发送是红线。残余边界:群标题
        若连成员数后缀都完全 OCR 不到,视觉上与私聊无异——此时即使受管群
        三闸全开,send_message 护栏①b 也会 fail-closed 拦截(群名不在
        safety.whitelist 时),见 README 4.2。"""
        rect = locate.region_px(self.win.size, "title_bar")

        def _valid(t: str) -> bool:
            return bool(t) and any(ch.isalnum() or ord(ch) > 0x2E80 for ch in t)

        cands = [
            it for it in items
            if locate._in_rect(it.center, rect) and _valid(it.text.strip())
        ]
        if not cands:
            return None
        # 单项判定(合并形态;拆出的「(23)」单项因群名为空不算,靠拼接兜住)
        for it in cands:
            _, members = split_group_title(it.text.strip())
            if members is not None:
                return it.text.strip()
        # 全项拼接判定(拆项形态:「测试群」+「(23)」→「测试群 (23)」)
        joined = " ".join(it.text.strip() for it in
                          sorted(cands, key=lambda it: it.center[0]))
        _, members = split_group_title(joined)
        if members is not None:
            return joined
        return None

    def _title_hit(self, items: list[OcrItem], name: str) -> Optional[OcrItem]:
        """标题栏中是否存在与 name 匹配的标题项。

        匹配规则:① OCR 项文本精确等于 name(私聊原行为);
        ② 群聊形态「name + 成员数后缀」——标题栏整体是「群名 (23)」,
        OCR 可能把群名与 (23) 合并读作一项(如 '测试群 (23)'),此时按
        split_group_title 剥离成员数后与群名相等也算命中。
        私聊行为完全不变(规则①优先);普通联系人名不会被规则②误伤
        (括号内必须为纯数字成员数)。"""
        rect = locate.region_px(self.win.size, "title_bar")
        hit = locate.find_text(items, name, rect, exact=True)
        if hit is not None:
            return hit
        for it in items:
            if locate._in_rect(it.center, rect):
                gname, members = split_group_title(it.text.strip())
                if members is not None and gname == name:
                    return it
        return None

    # ---------- open_chat ----------
    def open_chat(self, name: str, force_search: bool = False) -> bool:
        """打开与 name 的聊天。全程只允许:点击搜索框、粘贴联系人名、
        点击搜索结果项、Esc 关闭搜索(本阶段边界);严禁 Enter/Alt+S。

        force_search=True 时跳过"当前聊天已是目标"快路径,强制走完整搜索
        流程(只读验收用它实测搜索链路;契约默认行为不变)。"""
        if not name or not name.strip():
            return False
        name = name.strip()
        last_err = ""
        for attempt in (1, 2):   # 失败重试 1 次(契约)
            try:
                if self._open_chat_once(name, force_search=force_search):
                    return True
                last_err = "标题栏验证未通过"
            except Exception as e:  # 单次尝试失败不致命,记录后重试
                last_err = f"{type(e).__name__}: {e}"
            # 关闭可能残留的搜索弹层
            self.win.ensure_active()
            self.hi.press("esc")
            time.sleep(random.uniform(0.4, 0.7))
        # 降级路径:目标本就是当前聊天且标题校验通过,视为成功(边界允许)
        try:
            gname, _ = split_group_title(self._ocr_title())
            if gname == name:
                return True
        except Exception:
            pass
        self._last_open_chat_error = last_err
        return False

    def _open_chat_once(self, name: str, force_search: bool = False) -> bool:
        img, items = self._capture_ocr()
        # 快路径:当前聊天已是目标(标题精确相等,或群聊「群名 (N)」形态);
        # force_search 时跳过
        if not force_search and self._title_hit(items, name):
            return True

        # 1) 点击搜索框:优先 OCR 占位字"搜索"精修,失败用比例锚点
        sb_rect = locate.region_px(self.win.size, "search_box")
        target = locate.find_text(items, "搜索", sb_rect)
        if target is not None:
            lx, ly = target.center
        else:
            lx, ly = locate.point_px(self.win.size, "search_box_click")
        sx, sy = self._to_screen(lx, ly)
        self.hi.click(sx, sy)
        self.hi.human_delay(0.5, 0.9)

        # 2) 剪贴板粘贴联系人名(保存/恢复用户剪贴板)
        if not self.hi.paste_text(name, restore_clipboard=True):
            raise RuntimeError("剪贴板写入失败")

        # 3) 等待结果(0.5~2.5s 内轮询,01 文档实测等待 0.8~2s)
        popup_rect = locate.region_px(self.win.size, "search_popup")
        picked: Optional[OcrItem] = None
        deadline = time.time() + 2.5
        result_img = None
        while time.time() < deadline:
            time.sleep(random.uniform(0.3, 0.5))
            result_img, ritems = self._capture_ocr()
            hits = locate.find_text_all(ritems, name, popup_rect)
            exact = [h for h in hits if h.text == name]
            # 精确相等的项里取最靠上的(联系人区在聊天记录区之前)
            if exact:
                picked = exact[0]
                break
            # 次选:以 name 开头且带灰色后缀的结果项(如 "测试联系人 微信号: xxx")
            starts = [h for h in hits if h.text.startswith(name)]
            if starts:
                picked = starts[0]
                break
        self._debug_save("search_popup", result_img if result_img is not None else img)
        if picked is None:
            raise RuntimeError("搜索结果中未找到精确匹配项")

        # 4) 点击结果项 → 等待聊天页绘制
        sx, sy = self._to_screen(*picked.center)
        self.hi.click(sx, sy)
        self.hi.human_delay(0.6, 1.1)

        # 5) OCR 标题栏验证 == name(或群聊「name (N)」形态)
        _, items2 = self._capture_ocr()
        return self._title_hit(items2, name) is not None

    # ---------- read_messages ----------
    def read_messages(self) -> list[Message]:
        """读取当前聊天可见消息,返回结构化 Message 列表(按时间序)。

        归属三信号判定(ARCHITECTURE §3,wxauto4 算法):
        ① 气泡颜色:文字框边缘采样到微信绿(#95E29E≈自己)→ right;
        ② 左右位置:文本中心在消息区的相对横向位置;
        ③ 头像区评分:行带内左右头像列的灰度方差,方差大的一侧有头像=发送侧。
        """
        img, items = self._capture_ocr()
        return self.parse_messages(img, items)

    def parse_messages(self, img: np.ndarray, items: list[OcrItem]) -> list[Message]:
        size = self.win.size
        area = locate.region_px(size, "message_area")
        contact = self._ocr_title(items) or "contact"

        lines, ts_candidates = self._collect_message_lines(img, items, area)
        merged = self._merge_bubble_lines(lines)

        msgs: list[Message] = []
        for group in merged:
            text = "\n".join(g[0].text for g in group)
            side = group[0][1]
            y_mid = sum(g[0].center[1] for g in group) // len(group)
            ts = self._ts_for(y_mid, ts_candidates)
            msgs.append(Message(
                sender="self" if side == "right" else contact,
                text=text,
                side=side,
                ts=ts,
            ))
        return msgs

    # ---------- read_group_messages(群聊;默认配置下引擎不会走到) ----------
    def read_group_messages(self) -> list[Message]:
        """读取当前群聊可见消息:在私聊解析基础上提取成员昵称(sender)。

        前置:调用方需已 open_chat(群名)。只读操作,与 read_messages 同级。
        形态依据微信 4.x 常见布局(2026-10-24 实机校准时本账号聊天列表无群,
        待有群后实测校准,详见 issues):
        A. 昵称行:成员消息气泡上方一行小号灰字昵称(自己消息无昵称);
        B. 前缀式:OCR 把昵称与正文合并读作「昵称: 正文」一行。
        两种形态都识别;都未命中时 sender 回落为群名(不猜昵称)。"""
        img, items = self._capture_ocr()
        return self.parse_group_messages(img, items)

    def parse_group_messages(self, img: np.ndarray, items: list[OcrItem]) -> list[Message]:
        size = self.win.size
        area = locate.region_px(size, "message_area")
        # 群标题形态「测试群 (23)」→ sender 回落用纯群名(剥成员数后缀)
        contact = split_group_title(self._ocr_title(items) or "group")[0] or "group"

        lines, ts_candidates = self._collect_message_lines(img, items, area)
        nick_idx, nicks = self._extract_nick_lines(lines, area)
        body = [ln for i, ln in enumerate(lines) if i not in nick_idx]
        merged = self._merge_bubble_lines(body)

        msgs: list[Message] = []
        for group in merged:
            text = "\n".join(g[0].text for g in group)
            side = group[0][1]
            y_mid = sum(g[0].center[1] for g in group) // len(group)
            ts = self._ts_for(y_mid, ts_candidates)
            if side == "right":
                sender = "self"
            else:
                sender = self._nick_for_group(group, nicks)
                if sender is None:
                    sender, text = self._split_nick_prefix(text, fallback=contact)
            msgs.append(Message(sender=sender, text=text, side=side, ts=ts))
        return msgs

    # ---------- 解析共用内部件 ----------
    def _collect_message_lines(
        self, img: np.ndarray, items: list[OcrItem], area: tuple[int, int, int, int],
    ) -> tuple[list[tuple[OcrItem, str]], list[tuple[int, str]]]:
        """解析第一阶段(私聊/群聊共用):过滤消息区 OCR 行 → 分类归属,
        返回 (行列表[(item, side)], 时间线候选[(y, text)])。行为与原
        parse_messages 第一段完全一致。"""
        ax0, ay0, ax1, ay1 = area
        area_w = ax1 - ax0
        ts_candidates: list[tuple[int, str]] = []   # (y, text) 居中时间线
        lines: list[tuple[OcrItem, str]] = []       # (item, side)
        for it in items:
            text = it.text.strip()
            if not text or not locate._in_rect(it.center, area):
                continue
            cx, cy = it.center
            rel = (cx - ax0) / area_w
            centered = abs(rel - 0.5) <= 0.14
            if centered and (_TS_PATTERN.match(text) or text in ("昨天", "今天")):
                ts_candidates.append((cy, text))
                continue
            if any(h in text for h in _SYSTEM_HINTS):
                # 系统提示(居中灰条)不计入消息流
                if centered:
                    continue
            side = self._classify_side(img, it, area)
            lines.append((it, side))
        lines.sort(key=lambda p: (p[0].center[1], p[0].center[0]))
        ts_candidates.sort(key=lambda p: p[0])
        return lines, ts_candidates

    @staticmethod
    def _merge_bubble_lines(
        lines: list[tuple[OcrItem, str]],
    ) -> list[list[tuple[OcrItem, str]]]:
        """解析第二阶段:相邻同类行合并为一个气泡(OCR 行框垂直相接/重叠:
        同气泡内行距≤6px,不同气泡间隔远大于此;实测 URL 四行的框间 gap
        为 -1~1px)。"""
        merged: list[list[tuple[OcrItem, str]]] = []
        for it, side in lines:
            if merged:
                last_group = merged[-1]
                last_it = last_group[-1][0]
                last_y1 = max(p[1] for p in last_it.box)
                cur_y0 = min(p[1] for p in it.box)
                if last_group[0][1] == side and cur_y0 - last_y1 <= 6:
                    last_group.append((it, side))
                    continue
            merged.append([(it, side)])
        return merged

    @staticmethod
    def _ts_for(y_mid: int, ts_candidates: list[tuple[int, str]]) -> Optional[str]:
        ts = None
        for ty, ttext in ts_candidates:
            if ty <= y_mid:
                ts = ttext
            else:
                break
        return ts

    @staticmethod
    def _box_bounds(it: OcrItem) -> tuple[int, int, int, int]:
        xs = [p[0] for p in it.box]
        ys = [p[1] for p in it.box]
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def _extract_nick_lines(
        lines: list[tuple[OcrItem, str]], area: tuple[int, int, int, int],
    ) -> tuple[set[int], list[tuple[OcrItem, tuple[int, int, int, int]]]]:
        """群聊「昵称行」识别(形态 A):从行列表中剥离悬在气泡上方的小字昵称。

        判据(全部满足才认定,宁缺勿滥——误把正文当昵称比漏提 sender 更糟):
        ① side=left(自己消息无昵称);
        ② 文本短(≤24 字符)且不含 URL/句末标点;
        ③ 行框矮:高度 ≤ max(17px, 0.8×全部行高中位数)——昵称字号小于正文
          (17px 来自 896×648 基准窗口的实测字号;中位数使阈值随分辨率自适应);
        ④ 行框左缘在消息区左侧 45% 以内(群昵称紧贴左头像列);
        ⑤ 垂直关系:到下方最近一行(自己的气泡)的 gap_down ∈ [-2, 14]px,
          且到上方最近一行的 gap_up ≥ max(12, gap_down + 4)px——昵称紧贴
          自己的气泡、与上一条消息明显拉开;多行气泡的首行因 gap_up≈gap_down
          不会被误判。
        返回 (被剥离行的下标集合, [(item, (x0,y0,x1,y1))])。"""
        if len(lines) < 2:
            return set(), []
        heights = []
        bounds = []
        for it, _side in lines:
            x0, y0, x1, y1 = WeChatFlows._box_bounds(it)
            bounds.append((x0, y0, x1, y1))
            heights.append(y1 - y0)
        med_h = sorted(heights)[len(heights) // 2]
        h_cap = max(17.0, 0.8 * med_h)
        ax0, _, ax1, _ = area

        nick_idx: set[int] = set()
        nicks: list[tuple[OcrItem, tuple[int, int, int, int]]] = []
        for i, (it, side) in enumerate(lines):
            if side != "left":
                continue
            text = it.text.strip()
            if not text or len(text) > 24:
                continue
            low = text.lower()
            if "http" in low or any(ch in text for ch in "。!?!?;"):
                continue
            x0, y0, x1, y1 = bounds[i]
            if (y1 - y0) > h_cap:
                continue
            if x0 > ax0 + 0.45 * (ax1 - ax0):
                continue
            # 下方最近一行(任意 side,通常是自己气泡的左行或同侧正文)
            below = [
                (bounds[j][1], j) for j in range(len(lines))
                if j != i and bounds[j][1] >= bounds[i][3]
            ]
            if not below:
                continue
            _, j_down = min(below)
            gap_down = bounds[j_down][1] - y1
            if not (-2 <= gap_down <= 14):
                continue
            # 横向:昵称与其气泡左缘接近(同一起排版,容差 80px)
            if abs(bounds[j_down][0] - x0) > 80:
                continue
            # 上方最近一行
            above = [
                bounds[j][3] for j in range(len(lines))
                if j != i and bounds[j][3] <= y0
            ]
            gap_up = (y0 - max(above)) if above else 10 ** 9
            if gap_up < max(12, gap_down + 4):
                continue
            nick_idx.add(i)
            nicks.append((it, (x0, y0, x1, y1)))
        return nick_idx, nicks

    @staticmethod
    def _nick_for_group(
        group: list[tuple[OcrItem, str]],
        nicks: list[tuple[OcrItem, tuple[int, int, int, int]]],
    ) -> Optional[str]:
        """为左侧气泡组匹配上方最近的昵称行;未命中返回 None。"""
        if not nicks:
            return None
        bx0 = min(p[0] for p in group[0][0].box)
        by0 = min(p[1] for p in group[0][0].box)
        best: Optional[tuple[int, str]] = None
        for it, (x0, _y0, _x1, y1) in nicks:
            gap = by0 - y1
            if -2 <= gap <= 14 and abs(x0 - bx0) <= 80:
                if best is None or gap < best[0]:
                    best = (gap, it.text.strip())
        return best[1] if best else None

    @staticmethod
    def _split_nick_prefix(text: str, fallback: str) -> tuple[str, str]:
        """形态 B:「昵称: 正文」前缀拆分;不像昵称则原样返回 (fallback, text)。"""
        first, nl, rest = text.partition("\n")
        m = _NICK_PREFIX_RE.match(first)
        if not m:
            return fallback, text
        nick = m.group(1).strip()
        if not _looks_like_nickname(nick):
            return fallback, text
        body = m.group(2).strip()
        new_text = body + (("\n" + rest) if nl and rest.strip() else "")
        return nick, (new_text.strip() or text)

    def _classify_side(self, img: np.ndarray, it: OcrItem, area: tuple[int, int, int, int]) -> str:
        """三信号判定:left=对方 / right=自己。

        信号① 气泡颜色为一票否决:文字框边缘采样到微信绿(实测 BGR≈159,242,157,
        即 #95E29E 一族)→ 必是自己;对方气泡为白底、与背景同色,颜色信号无区分度。
        颜色未知时,用 ②左右位置 + ③头像区方差 投票。"""
        ax0, ay0, ax1, ay1 = area
        area_w = ax1 - ax0
        cx, cy = it.center
        rel = (cx - ax0) / area_w

        xs = [p[0] for p in it.box]
        ys = [p[1] for p in it.box]
        bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
        h, w = img.shape[:2]
        samples = [
            (bx0 - 6, cy), (bx1 + 6, cy), (bx0 - 3, cy - 4), (bx1 + 3, cy + 4),
            ((bx0 + bx1) // 2, by1 + 5), ((bx0 + bx1) // 2, by0 - 5),
        ]
        for sx, sy in samples:
            if 0 <= sx < w and 0 <= sy < h:
                b, g, r = (int(v) for v in img[sy, sx])
                if locate.SELF_BUBBLE_GREENISH(b, g, r):
                    return "right"   # 绿气泡=自己(一票否决)

        # 信号② 左右位置
        s_pos = 1 if rel > 0.53 else (-1 if rel < 0.47 else 0)

        # 信号③ 头像区评分(行带 ±18px,灰度方差大的一侧有头像)
        band0 = max(ay0, cy - 18)
        band1 = min(ay1, cy + 18)
        s_av = 0
        try:
            def _strip_std(rx0, ry0, rx1, ry1):
                roi = img[ry0:ry1, rx0:rx1]
                if roi.size == 0:
                    return 0.0
                gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                return float(gray.std())

            sl0, st0, sl1, st1 = locate.region_px(self.win.size, "avatar_self_col")
            ol0, ot0, ol1, ot1 = locate.region_px(self.win.size, "avatar_other_col")
            std_self = _strip_std(sl0, band0, sl1, band1)
            std_other = _strip_std(ol0, band0, ol1, band1)
            if std_self - std_other > 8 and std_self > 15:
                s_av = 1
            elif std_other - std_self > 8 and std_other > 15:
                s_av = -1
        except Exception:
            s_av = 0

        score = s_pos + s_av
        return "right" if score > 0 else "left"

    # ---------- send_message(引擎投递路径专用;默认零发送) ----------
    @staticmethod
    def _norm_group_name(name: str) -> str:
        """群名归一:去所有空白(与 wxbot.engine.groups._norm_name 同规则)。"""
        return re.sub(r"\s+", "", name or "")

    def _group_send_allowed(self, name: str) -> bool:
        """群聊真实发送三闸门(2026-10-24,替代原「群形态硬禁」):
        groups.enabled + 群 ∈ groups.whitelist + groups.allow_real_send。

        groups_cfg 未接入(None)或任一闸不满足 → False(fail-closed)。"""
        g = self.groups_cfg
        if g is None:
            return False
        if not (g.enabled and g.allow_real_send):
            return False
        target = self._norm_group_name(name)
        return any(self._norm_group_name(n) == target
                   for n in (g.whitelist or []) if str(n).strip())

    def send_message(self, text: str, expect_contact: str) -> bool:
        """发送文本给 expect_contact。【红线】全部护栏在任何键鼠(点击/粘贴)
        之前;①的前置截屏 OCR 为只读操作,不算键鼠。
        ① expect_contact 必须在联系人白名单;**受管群例外**(2026-10-24 审查
        修复,对齐 ARCHITECTURE 红线1 与 README 4.2):群名按 README 指引只入
        groups.whitelist、绝不入联系人白名单,故三闸门全开(groups.enabled +
        群∈groups.whitelist + groups.allow_real_send)的受管群凭三闸门放行,
        但标题栏必须呈「群名 + 成员数后缀」群聊形态——OCR 连成员数后缀都
        识别不到时视觉上与私聊无法区分,fail-closed 拦截(绝不向联系人
        白名单之外、疑似私聊的会话发消息);
        ② 发送前 OCR 标题栏必须 == expect_contact(或群聊「群名 (N)」形态
        剥离成员数后相等);③ 标题栏全部有效文本项呈群聊形态(群名+成员数
        后缀,合并/拆项两种 OCR 形态)时,按**群聊三闸门**校验:groups.enabled
        + 群∈groups.whitelist + groups.allow_real_send 全部满足才放行
        (默认 false → 拦截,行为与旧「双层硬禁」一致);
        任一闸不满足 → SafetyError,键鼠零执行。
        演练模式(dry_run/allow_real_send 双开关默认全关)下不得调用本方法。"""
        # 护栏① 配置闸(任何窗口交互之前):不在联系人白名单时,仅当
        # 受管群三闸门全开才放行(README 4.2:群名不得加入联系人白名单,
        # 群真实发送的红线是三闸门而非联系人白名单)
        group_allowed = self._group_send_allowed(expect_contact)
        if expect_contact not in self.whitelist and not group_allowed:
            raise SafetyError(
                f"红线拦截:目标联系人 {expect_contact!r} 不在白名单 "
                f"{self.whitelist},且群真实发送三闸门未全部满足(groups.enabled"
                f" + 群∈groups.whitelist + groups.allow_real_send),中止发送")
        self.win.ensure_active()
        img, items = self._capture_ocr()
        title = self._ocr_title(items)
        group_form = self._title_group_form(items)
        # 护栏①b 群白名单放行的前提:标题确呈群聊形态。三闸全开但标题读不出
        # 「群名 + 成员数后缀」→ 视觉上与私聊无法区分,fail-closed:绝不向
        # 联系人白名单之外、疑似私聊的会话发消息(README 4.2 边界)
        if expect_contact not in self.whitelist and group_form is None:
            raise SafetyError(
                f"红线拦截:目标 {expect_contact!r} 在群白名单且三闸门全开,"
                f"但当前标题栏识别不到「群名 + 成员数后缀」的群聊形态,视觉上"
                f"与私聊无法区分,中止发送(私聊真实发送仅限联系人白名单)")
        # 群聊三闸门(仅当三闸全开时,合并形态标题「群名 (23)」按剥离成员数
        # 后相等放行护栏②;三闸未全开维持严格相等——原行为不变)
        tname, tmembers = split_group_title(title)
        title_hit = (title == expect_contact) or (
            group_allowed and tmembers is not None and tname == expect_contact)
        if not title_hit:
            raise SafetyError(
                f"红线拦截:当前聊天标题 {title!r} != 目标 {expect_contact!r},中止发送")
        # 护栏③ 群形态 → 三闸门:_ocr_title 只取最左项,标题拆项形态(「测试群」
        # 「(23)」两项)下最左项==群名会骗过护栏②,必须看全项拼接;三闸门
        # 全开才放行群真实发送,否则一律拦截(fail-closed)
        if group_form is not None and not group_allowed:
            raise SafetyError(
                f"红线拦截:当前聊天标题栏呈群聊形态({group_form!r}),"
                f"群真实发送三闸门未全部满足(groups.enabled + 群∈groups."
                f"whitelist + groups.allow_real_send,默认关闭),中止发送")

        # 点输入区(锚点坐标;OCR 精修为增强项,仅真实发送路径生效)
        lx, ly = locate.point_px(self.win.size, "input_click")
        sx, sy = self._to_screen(lx, ly)
        self.hi.click(sx, sy)
        self.hi.human_delay(0.2, 0.5)
        if not self.hi.paste_text(text, restore_clipboard=True):
            return False
        # 点击"发送(S)"按钮(优先)——先 OCR 后锚点
        btn_rect = locate.region_px(self.win.size, "send_btn")
        _, items3 = self._capture_ocr()
        btn = locate.find_text(items3, "发送", btn_rect)
        if btn is not None:
            bx, by = self._to_screen(*btn.center)
        else:
            bx, by = self._to_screen(*locate.point_px(self.win.size, "send_btn_click"))
        self.hi.click(bx, by)
        self.hi.human_delay(0.3, 0.8)

        # 验证:消息区尾部出现新的 right 气泡且文本包含待发内容
        _, items4 = self._capture_ocr()
        msgs = self.parse_messages(img, items4)
        return bool(msgs) and msgs[-1].side == "right" and text[:20] in msgs[-1].text

    # ---------- check_unread ----------
    def has_nav_unread_dot(self, img: Optional[np.ndarray] = None) -> bool:
        """导航"聊天"图标区红色像素预检(毫秒级,wxauto IsRedPixel 思路)。

        实测注意(2026-10-24):免打扰会话有未读角标时导航图标不亮红点,
        故本方法只能作"有未读"的快速确认,不能作"无未读"的依据。"""
        if img is None:
            img = self.win.capture()
        nav_rect = locate.region_px(self.win.size, "nav_chat_icon")
        return locate.red_pixels_in(img, nav_rect) >= 4

    def check_unread(self) -> list[str]:
        """未读联系人列表:扫描聊天列表头像右上角红色角标(连通域,02 文档
        §3.2 配方),把角标映射到同行 OCR 文本。只读操作。

        注:不依赖导航图标红点预检做门控——实测免打扰会话不点亮导航图标,
        漏检不可接受;列表 ROI 连通域扫描本身毫秒级。"""
        img, items = self._capture_ocr()
        size = self.win.size
        cl_rect = locate.region_px(size, "chat_list")
        x0, y0, x1, y1 = cl_rect
        roi = img[y0:y1, x0:x1]
        if roi.size == 0:
            return []
        b = roi[:, :, 0].astype(int)
        g = roi[:, :, 1].astype(int)
        r = roi[:, :, 2].astype(int)
        mask = ((r > g + 40) & (r > b + 40) & (r > 150)).astype(np.uint8) * 255
        n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        names: list[str] = []
        list_items = [it for it in items if locate._in_rect(it.center, cl_rect)]
        for i in range(1, n):
            x, y, bw, bh, area = (int(v) for v in stats[i])
            # 角标形态:实测直径~8-10px 圆点(面积 30~300,近方形);
            # 排除头像内的红色图案(长条/大块,如实测 25x9 / 36x36 均被排除)
            if not (25 <= area <= 300 and 5 <= bw <= 24 and 5 <= bh <= 24
                    and 0.6 <= bw / max(1, bh) <= 1.7):
                continue
            ccx, ccy = x0 + int(centroids[i][0]), y0 + int(centroids[i][1])
            # 同一行(±28px)、位于角标右侧(名字区)的 OCR 文本作为联系人名
            row_items = [
                it for it in list_items
                if abs(it.center[1] - ccy) <= 28 and it.center[0] > ccx
            ]
            if row_items:
                name_item = min(row_items, key=lambda it: (it.center[1], it.center[0]))
                if name_item.text.strip() and name_item.text.strip() not in names:
                    names.append(name_item.text.strip())
        return names
