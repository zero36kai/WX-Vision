# -*- coding: utf-8 -*-
"""群聊路由(默认全关):@提及提取 + enabled/群白名单/reply_mode 三重门。

红线与语义:
- 默认组合 groups.enabled=False + reply_mode="off" + whitelist=[] →
  引擎完全不碰任何群(采集阶段即跳过,连 open_chat 都不会发生);
- 群消息要交给 agent 必须同时满足:groups.enabled 且 群名 ∈ groups.whitelist
  且 reply_mode 触发条件满足;「off」或任何条件不满足一律忽略;
- at 模式:消息文本含「@昵称」且昵称 ∈ bot_names 才触发;
  bot_names 为空 = 任意 @ 提及都算命中(宽松语义,配置项注释已写明);
- all 模式:白名单群的所有成员消息都触发(自己消息由调用方排除);
- 私聊逻辑完全不经过本模块(监控引擎按消息种类分流)。
"""
from __future__ import annotations

import re
from typing import Any

from wxbot.config import GroupsConfig

# 「@昵称」提取:@ 需出现在文本开头或空白之后(排除邮箱 a@b.com 这类
# 误报);@ 后允许 OCR 多识别的空格;昵称取一段非空白/非@的连续字符,
# 截至常见中英文标点前。
_AT_RE = re.compile(r"(?:(?<=\s)|^)@\s*([^\s@,，。.!?:：;；?？!！]+)")


def extract_at_targets(text: str) -> list[str]:
    """提取消息文本里被 @ 的昵称(去重,保持出现顺序)。

    例:「@助手 帮我查天气」→ ["助手"];「@ 张三 @李四」→ ["张三","李四"]
    (OCR 在 @ 后多读的空格被容忍);邮箱 a@b.com 不算提及;无 @ → []。
    注意:昵称内部被 OCR 拆进空格(如「@AI 助手」)时只能提出「AI」,
    该场景由 GroupRouter 的机器人名模糊匹配兜底,不靠本函数。"""
    targets: list[str] = []
    seen: set[str] = set()
    for m in _AT_RE.finditer(text or ""):
        # 昵称内部也可能被 OCR 插入空格(如「AI 助手」),比对前归一
        norm = _norm_name(m.group(1))
        if norm and norm not in seen:
            seen.add(norm)
            targets.append(m.group(1).strip())
    return targets


def _norm_name(name: str) -> str:
    """昵称归一:去所有空白 + 小写(OCR 断字/大小写差异容错)。"""
    return re.sub(r"\s+", "", name or "").lower()


class GroupRouter:
    """群消息路由决策(纯函数式,离线可测;engine 采集/工作两个阶段共用)。

    - is_managed(name):name 是否被配置为受管群(在 groups.whitelist 中,
      与 enabled 无关——发送侧红线用它做「群名永不真实发送」的判定);
    - collectible(name):采集阶段是否值得打开该群( enabled + 在名单 +
      reply_mode 非 off——off 时连 open_chat 的键鼠交互都不该发生);
    - should_handle(group, msg):工作阶段是否把该消息交给 agent,
      返回 (是否, 原因);原因用于日志/事件,WebUI 可读。
    """

    def __init__(self, cfg: GroupsConfig):
        self.cfg = cfg
        self._names = {_norm_name(n) for n in (cfg.whitelist or []) if str(n).strip()}
        self._bots = {_norm_name(n) for n in (cfg.bot_names or []) if str(n).strip()}

    @property
    def managed_groups(self) -> list[str]:
        return sorted(self._names)

    def is_managed(self, name: str) -> bool:
        return _norm_name(name) in self._names

    def collectible(self, name: str) -> bool:
        return bool(
            self.cfg.enabled
            and self.is_managed(name)
            and self.cfg.reply_mode in ("at", "all")
        )

    def _at_bot_hit(self, text: str) -> bool:
        """OCR 断字兜底:消息文本里「@ + 机器人名(字符间允许可变空白)」
        是否出现。覆盖 extract_at_targets 提不全的形态,如「@AI 助手 在吗」
        (机器人名 "AI助手" 被 OCR 拆进空格)。_norm_name 已把名字转小写,
        故匹配对原文大小写不敏感。"""
        for b in self._bots:
            pat = r"@\s*" + r"\s*".join(re.escape(ch) for ch in b)
            if re.search(pat, text or "", re.IGNORECASE):
                return True
        return False

    def should_handle(self, group: str, msg: Any) -> tuple[bool, str]:
        """工作线程路由:默认全关,任一条件不满足即 (False, 原因)。"""
        if not self.cfg.enabled:
            return False, "groups_disabled"
        if not self.is_managed(group):
            return False, "group_not_whitelisted"
        mode = self.cfg.reply_mode
        if mode == "off":
            return False, "reply_mode_off"
        if mode == "all":
            return True, "reply_mode_all"
        # mode == "at"
        text = getattr(msg, "text", "") or ""
        targets = extract_at_targets(text)
        if not targets:
            # 提不出 @目标:可能是真没有,也可能是 OCR 断字(如「@AI 助手」)
            if self._bots and self._at_bot_hit(text):
                return True, "at_bot_fuzzy"
            return False, "no_at_mention"
        if not self._bots:
            return True, "at_any"          # bot_names 空 = 任意 @ 都算
        for t in targets:
            if _norm_name(t) in self._bots:
                return True, f"at_bot:{t}"
        if self._at_bot_hit(text):         # 目标提不全时的模糊兜底
            return True, "at_bot_fuzzy"
        return False, "at_not_me"
