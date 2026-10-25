# -*- coding: utf-8 -*-
"""可插拔智能体接入层(wxbot.agent)。

导入即注册全部内置后端(echo / openai / webhook)到 AGENT_REGISTRY,
之后用 wxbot.agent.base.create_agent(cfg.agent) 按配置实例化。
"""
from wxbot.agent.base import (  # noqa: F401
    AGENT_REGISTRY,
    AgentBackend,
    create_agent,
    create_backend,
    message_to_dict,
    register_backend,
)
# 导入内置实现(装饰器自注册);base.create_agent 内部另有惰性导入兜底
from wxbot.agent import echo  # noqa: F401,E402
from wxbot.agent import openai_compat  # noqa: F401,E402
from wxbot.agent import webhook  # noqa: F401,E402

__all__ = [
    "AGENT_REGISTRY", "AgentBackend", "create_agent", "create_backend",
    "message_to_dict", "register_backend",
]
