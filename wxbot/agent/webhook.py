# -*- coding: utf-8 -*-
"""Webhook 回调后端:POST {contact, msg_id, chat_id, history, incoming} → 取 JSON {reply}。

设计(调研03 §2.2,IM 平台回调通用模式;v1.1 增补见 PROTOCOL §11):
    请求:POST {webhook_url}  Content-Type: application/json
          X-Webhook-Secret: {webhook_secret}      # 配置了才带,防乱调
          Body: {"contact": "文件传输助手",
                 "msg_id": 108,                   # v1.1:与信箱同源编号(顶层=incoming 编号)
                 "chat_id": "文件传输助手",        # v1.1:会话路由键(=contact)
                 "history": [{"sender","text","side","ts","msg_id","chat_id"}, ...],
                 "incoming": {"sender","text","side","ts","msg_id","chat_id"}}
          (history/incoming 每条均含 msg_id 与 chat_id;未经引擎编号的
           条目两字段为 null——直接单测构造 Message 才会出现)
    响应:200 {"reply": "..."}   → 发送该文本
          200 {"reply": null}    → 不回复(主动沉默)
          非200 / 超时 / 解析失败 → 记日志不回复(03 §1.3 兜底原则)

适用:用户已有自己的服务(n8n / 自建工作流),不想把 API Key 交给本工具。
响应解析宽松:优先 reply 字段,兼容 text(03 §2.3)。
"""
from __future__ import annotations

import logging

import httpx

from wxbot.agent.base import message_to_dict, register_backend
from wxbot.config import AgentConfig

log = logging.getLogger("wxbot.agent.webhook")

DEFAULT_TIMEOUT_SEC = 30.0


class WebhookBackend:
    """HTTP 回调模式后端(容错契约与 openai 后端一致:任何异常 → None)。"""

    def __init__(
        self,
        url: str,
        *,
        secret: str = "",
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        client: httpx.Client | None = None,
    ):
        self._url = (url or "").strip()
        self._secret = secret or ""
        self._timeout = float(timeout_sec)
        self._client = client

    def name(self) -> str:
        return "webhook"

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._secret:
            h["X-Webhook-Secret"] = self._secret
        return h

    def healthcheck(self) -> bool:
        """探测 webhook_url 可达性:POST 一条 probe 载荷。

        对端只是被询问方(回复只回到本引擎,不会自行触碰微信),probe 副作用
        仅为一次 HTTP 请求;HTTP <400 视为健康,连接失败/超时 → False。
        """
        if not self._url:
            return False
        payload = {
            "probe": True,
            "contact": "__healthcheck__",
            "history": [],
            "incoming": {"sender": "", "text": "ping", "side": "left", "ts": None},
        }
        try:
            resp = self._ensure_client().post(
                self._url, json=payload, headers=self._headers())
            ok = resp.status_code < 400
            if not ok:
                log.warning("webhook healthcheck: HTTP %d", resp.status_code)
            return ok
        except Exception as e:
            log.warning("webhook healthcheck 失败: %s: %s", type(e).__name__, e)
            return False

    def reply(self, contact, history, incoming) -> str | None:
        if not self._url:
            log.warning("webhook: webhook_url 未配置,跳过回复 (contact=%s)", contact)
            return None
        # v1.1(PROTOCOL §11):报文顶层与 history/incoming 每条均携带
        # msg_id/chat_id(与信箱同源编号器,全局唯一);顶层 msg_id 即本次
        # incoming 的编号,chat_id 即会话路由键(=contact,原样带回即可回复)。
        # 旧字段 contact 保留,兼容既有接收端。
        payload = {
            "contact": contact,
            "msg_id": getattr(incoming, "msg_id", None),
            "chat_id": str(contact),
            "history": [message_to_dict(m) for m in history or []],
            "incoming": message_to_dict(incoming),
        }
        try:
            resp = self._ensure_client().post(
                self._url, json=payload, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                log.warning("webhook: 响应非 JSON 对象(%s),不回复", type(data).__name__)
                return None
            text = data.get("reply")
            if text is None:
                text = data.get("text")   # 宽松兼容(03 §2.3)
            text = str(text).strip() if text is not None else ""
            if not text:
                log.info("webhook: 对端返回空 reply(主动沉默) (contact=%s)", contact)
                return None
            log.info("webhook: %s <- %s (回复 %d 字)", contact, self._url, len(text))
            return text
        except Exception as e:
            log.warning("webhook reply 失败(不回复): %s: %s", type(e).__name__, e)
            return None

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


@register_backend("webhook")
def _create(cfg: AgentConfig) -> WebhookBackend:
    return WebhookBackend(
        cfg.webhook_url,
        secret=getattr(cfg, "webhook_secret", ""),
        timeout_sec=cfg.timeout_sec,
    )
