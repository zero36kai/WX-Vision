# -*- coding: utf-8 -*-
"""echo 回声后端:默认模式,零配置可跑(ARCHITECTURE §5.7、调研03 §2.3)。

用途:
1. 不配 API Key 也能端到端跑通「识别→回复→safety→dry_run 记录」链路;
2. WebUI「测通」自检(恒健康);
3. 给小白演示:回复 = 前缀 + 原文,一眼可辨是机器人行为,不炸真人小号。
"""
from __future__ import annotations

import logging

from wxbot.agent.base import register_backend
from wxbot.config import AgentConfig

log = logging.getLogger("wxbot.agent.echo")


class EchoBackend:
    """回声测试模式:reply = 【echo】+ 来信原文。"""

    def __init__(self, cfg: AgentConfig | None = None, prefix: str = "【echo】"):
        self._prefix = prefix
        # 预留:后续可用 cfg 自定义固定文案,当前零配置即可用

    def name(self) -> str:
        return "echo"

    def healthcheck(self) -> bool:
        return True

    def reply(self, contact, history, incoming) -> str | None:
        text = (getattr(incoming, "text", "") or "").strip()
        if not text:
            log.debug("echo: 来信为空,不回复 (contact=%s)", contact)
            return None
        reply = f"{self._prefix}{text}"
        log.info("echo: %s -> %s (回复 %d 字)", contact, text[:40], len(reply))
        return reply


@register_backend("echo")
def _create(cfg: AgentConfig) -> EchoBackend:
    return EchoBackend(cfg)
