# -*- coding: utf-8 -*-
"""引擎安全闸门:白名单严格校验 / 限频 / dry_run·allow_real_send 拦截。

红线落点(ARCHITECTURE §0):
1. 白名单严格模式:不在名单即拒(SafetyRejected)并记日志;唯一默认名单 ["文件传输助手"];
2. 限频:全局 QPM(可配)+ 每联系人每分钟 3 条(可配)+ 同联系人冷却期,
   滑动窗口实现,超限拒绝并记日志(§5.6 防刷屏防封号);
3. 发送闸:真实发送必须同时满足 engine.dry_run == False 且 config.allow_real_send
   == True(双开关,默认全关 = 开箱零发送);dry_run=True → 记录发送意图,
   不执行键鼠;dry_run=False 但 allow_real_send=False → 显式拦截并告警。

注意:最终发送必经 flows.send_message(text, expect_contact),其内部还有
「expect_contact 在白名单 + OCR 标题==expect_contact」双闸门(抛 flows.SafetyError),
本模块不替代、也不绕过它——这里做的是 flows 之前的业务层拦截。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Callable

log = logging.getLogger("wxbot.engine.safety")


class SafetyRejected(RuntimeError):
    """业务层安全拒绝。code ∈ whitelist / rate_limit / send_disabled /
    allow_real_send / empty_text,便于上层(server)分类提示。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RateLimiter:
    """滑动窗口限频:全局 QPM + 每联系人 QPM + 冷却期(线程安全)。

    allow(contact) 返回 (是否放行, 拒绝原因);放行即计数。
    clock 参数便于测试注入。
    """

    def __init__(
        self,
        global_qpm: int = 20,
        per_contact_qpm: int = 3,
        cooldown_sec: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._global_qpm = max(1, int(global_qpm))
        self._per_contact_qpm = max(1, int(per_contact_qpm))
        self._cooldown = float(cooldown_sec)
        self._clock = clock
        self._lock = threading.Lock()
        self._global_win: deque[float] = deque()
        self._per_win: dict[str, deque[float]] = defaultdict(deque)
        self._last_reply: dict[str, float] = {}

    @property
    def limits(self) -> dict:
        return {
            "global_qpm": self._global_qpm,
            "per_contact_qpm": self._per_contact_qpm,
            "cooldown_sec": self._cooldown,
        }

    def allow(self, contact: str) -> tuple[bool, str]:
        now = self._clock()
        with self._lock:
            # 清理 60s 滑动窗口外的旧记录
            horizon = now - 60.0
            while self._global_win and self._global_win[0] <= horizon:
                self._global_win.popleft()
            pw = self._per_win[contact]
            while pw and pw[0] <= horizon:
                pw.popleft()

            if len(self._global_win) >= self._global_qpm:
                return False, (
                    f"全局限频:本分钟已回复 {len(self._global_win)} 条"
                    f"(上限 {self._global_qpm})")
            if len(pw) >= self._per_contact_qpm:
                return False, (
                    f"联系人限频:{contact} 本分钟已回复 {len(pw)} 条"
                    f"(上限 {self._per_contact_qpm})")
            last = self._last_reply.get(contact)
            if last is not None and self._cooldown > 0 and now - last < self._cooldown:
                return False, (
                    f"冷却期:{contact} 距上次回复 {now - last:.1f}s"
                    f" < 冷却 {self._cooldown:.1f}s")

            self._global_win.append(now)
            pw.append(now)
            self._last_reply[contact] = now
            return True, ""


class SafetyGate:
    """业务层安全闸门:白名单 / 限频 / 发送模式判定。"""

    def __init__(
        self,
        whitelist: list[str],
        *,
        dry_run: bool = True,
        allow_real_send: bool = False,
        rate_limiter: RateLimiter | None = None,
    ):
        # 名单规范化去空格;红线:名单必须非空(空名单 = 谁都不发)
        self._whitelist = {w.strip() for w in (whitelist or []) if w and w.strip()}
        self.dry_run = bool(dry_run)
        self.allow_real_send = bool(allow_real_send)
        self.rate = rate_limiter or RateLimiter()

    # ---------- 白名单 ----------
    def is_whitelisted(self, contact: str) -> bool:
        return bool(contact) and contact.strip() in self._whitelist

    @property
    def whitelist(self) -> list[str]:
        return sorted(self._whitelist)

    def check_whitelist(self, contact: str) -> None:
        """严格校验:不在名单即抛 SafetyRejected 并记日志(不静默)。"""
        if self.is_whitelisted(contact):
            return
        log.warning("安全拦截[whitelist]: %r 不在白名单 %s,拒绝操作", contact, self.whitelist)
        raise SafetyRejected(
            "whitelist",
            f"联系人 {contact!r} 不在白名单 {self.whitelist},已拒绝(红线:只对白名单操作)",
        )

    # ---------- 限频 ----------
    def check_rate(self, contact: str) -> None:
        ok, reason = self.rate.allow(contact)
        if ok:
            return
        log.warning("安全拦截[rate_limit]: %s", reason)
        raise SafetyRejected("rate_limit", reason)

    # ---------- 发送闸 ----------
    def send_mode(self) -> str:
        """返回发送模式:"dry_run"(记录意图,不碰键鼠)或 "real"。

        真实发送要求 engine.dry_run == False 且 config.allow_real_send == True;
        dry_run=False 而 allow_real_send=False → 抛 send_disabled(显式拦截,
        不静默降级,便于 WebUI 提示用户去开总开关)。
        """
        if self.dry_run:
            return "dry_run"
        if not self.allow_real_send:
            log.warning(
                "安全拦截[allow_real_send]: engine.dry_run 已关闭但 config."
                "allow_real_send=false,真实发送被总开关拦截")
            raise SafetyRejected(
                "allow_real_send",
                "真实发送被拦截:config.allow_real_send=false(默认),"
                "需在配置中显式开启后才允许真实发送",
            )
        return "real"
