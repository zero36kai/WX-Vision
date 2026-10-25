# -*- coding: utf-8 -*-
"""可插拔智能体接入层:AgentBackend 协议 + 注册表工厂。

契约(ARCHITECTURE §3、调研03 §2.3):
    class AgentBackend(Protocol):
        def name(self) -> str                     # "echo" / "openai" / "webhook"
        def healthcheck(self) -> bool             # WebUI 状态卡/「测通」按钮
        def reply(self, contact, history, incoming) -> str | None
def create_agent(cfg: AgentConfig) -> AgentBackend   # mode: echo|openai|webhook

约定:
- reply 返回 None / 空串 = 智能体主动沉默,引擎不回复;
- 实现内部必须捕获所有异常返回 None 并记日志(宁可沉默,不把报错发进聊天,
  调研03 §1.3 兜底原则);
- 本层不 import 键鼠/视觉模块(flows.Message 仅 TYPE_CHECKING 引用,运行时
  鸭子类型访问 .sender/.text/.side/.ts),保证 `import wxbot.agent` 轻量。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

from wxbot.config import AgentConfig

if TYPE_CHECKING:  # 运行时不导入 actions 层(避免拖起 pyautogui/cv2)
    from wxbot.actions.flows import Message

log = logging.getLogger("wxbot.agent")


@runtime_checkable
class AgentBackend(Protocol):
    """智能体后端协议(结构化类型,第三方实现无需继承)。"""

    def name(self) -> str:
        """后端标识名,用于日志与 WebUI 状态卡。"""
        ...

    def healthcheck(self) -> bool:
        """健康检查:WebUI「测通」用。echo 恒 True;网络后端 ping 一次。"""
        ...

    def reply(
        self,
        contact: str,
        history: list["Message"],
        incoming: "Message",
    ) -> str | None:
        """生成回复。

        contact: 联系人名(白名单成员);
        history: 该联系人最近上下文(flows.Message 列表,时间序,
                 side=="right"/sender=="self" 为自己说过的话);
        incoming: 本次要回复的新消息;
        返回:回复文本;None/空 = 不回复。实现必须自行兜底所有异常 → None。
        """
        ...


def message_to_dict(msg: "Message") -> dict:
    """flows.Message → 可 JSON 序列化的字典(webhook 载荷用)。

    v1.1(PROTOCOL §11):统一携带 msg_id/chat_id——引擎分发消息时从
    信箱同源计数器编号并附加到消息对象(§3:全局唯一、持久化,允许跳号);
    未经引擎编号的条目(直接单测构造的 Message 等)两字段为 null。"""
    msg_id = getattr(msg, "msg_id", None)
    chat_id = getattr(msg, "chat_id", None)
    return {
        "sender": getattr(msg, "sender", ""),
        "text": getattr(msg, "text", ""),
        "side": getattr(msg, "side", ""),
        "ts": getattr(msg, "ts", None),
        "msg_id": int(msg_id) if msg_id is not None else None,
        "chat_id": str(chat_id) if chat_id is not None else None,
    }


def role_of(msg: "Message") -> str:
    """上下文消息 → OpenAI chat 角色:right/self 气泡 = assistant,否则 user。"""
    return "assistant" if (
        getattr(msg, "side", "") == "right" or getattr(msg, "sender", "") == "self"
    ) else "user"


# ---- 注册表工厂(调研03 §2.3:新增后端 = 实现 3 方法 + 注册,主循环零改动) ----
AGENT_REGISTRY: dict[str, Callable[[AgentConfig], AgentBackend]] = {}


def register_backend(mode: str) -> Callable:
    """把工厂函数登记进注册表的装饰器。"""
    def deco(fn: Callable[[AgentConfig], AgentBackend]) -> Callable[[AgentConfig], AgentBackend]:
        AGENT_REGISTRY[mode] = fn
        return fn
    return deco


def create_agent(cfg: AgentConfig) -> AgentBackend:
    """按 cfg.mode 构造后端(内置实现惰性导入,保持本模块零重依赖)。

    ARCHITECTURE §3 契约名 create_backend 保留为别名。
    未知 mode 抛 ValueError(config.validate 已前置拦截,此处为最后防线)。
    """
    mode = (getattr(cfg, "mode", "") or "echo").strip().lower()
    factory = AGENT_REGISTRY.get(mode)
    if factory is None:
        # 惰性导入内置实现,填充注册表(impl 反向 import 本模块,无环:
        # create_agent 是运行时才执行的函数体)
        from wxbot.agent import echo as _echo  # noqa: F401
        from wxbot.agent import openai_compat as _openai  # noqa: F401
        from wxbot.agent import webhook as _webhook  # noqa: F401
        factory = AGENT_REGISTRY.get(mode)
    if factory is None:
        raise ValueError(
            f"未知 agent.mode: {mode!r}(可选 echo|openai|webhook,"
            f"已注册: {sorted(AGENT_REGISTRY)})")
    backend = factory(cfg)
    log.info("agent 后端已创建: mode=%s backend=%s", mode, backend.name())
    return backend


# ARCHITECTURE §3 中的契约名,保留别名以兼容文档/外部引用
create_backend = create_agent
