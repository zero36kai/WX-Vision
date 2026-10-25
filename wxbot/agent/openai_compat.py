# -*- coding: utf-8 -*-
"""OpenAI 兼容 API 后端(httpx 直连,不引入 openai SDK)。

三参数标准写法(调研03 §2.1,已逐家核对官方文档):
    base_url + api_key + model 即可切换 OpenAI / DeepSeek / GLM / Ollama:
      - DeepSeek: https://api.deepseek.com
      - GLM:      https://open.bigmodel.cn/api/paas/v4/
      - Ollama:   http://localhost:11434/v1(api_key 填任意非空串)
    各家模型名随时间变化,model 必须可配,不写死。

容错契约(调研03 §1.3):超时/网络/解析任何异常 → 记日志返回 None(不回复,
绝不把报错文案发进聊天)。默认超时 30s。

注意:各家 base_url 未在本机实测连通性(03 文档 §7 待实机验证 #1),
本实现以 httpx MockTransport 做了请求格式/容错的离线验证(tests/test_engine.py)。
"""
from __future__ import annotations

import logging

import httpx

from wxbot.agent.base import register_backend, role_of
from wxbot.config import AgentConfig

log = logging.getLogger("wxbot.agent.openai")

DEFAULT_TIMEOUT_SEC = 30.0   # 03 文档 §2.3 字段表 timeout_sec 默认 30


class OpenAICompatBackend:
    """OpenAI 兼容 chat/completions 客户端。

    仅需三参数 base_url/api_key/model(其余为可选注入,便于测试):
      timeout_sec: 请求超时,默认 30s;
      system_prompt: 人设(空 = 不带 system 消息);取自 agent.system_prompt;
      client: 外部注入 httpx.Client(测试用 MockTransport),None 则自建。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        system_prompt: str = "",
        client: httpx.Client | None = None,
    ):
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self._model = model or ""
        self._timeout = float(timeout_sec)
        self._system_prompt = (system_prompt or "").strip()
        self._client = client  # 惰性创建,见 _ensure_client

    def name(self) -> str:
        return "openai"

    # ---------- 内部 ----------
    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _build_messages(self, history, incoming) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = []
        if self._system_prompt:
            msgs.append({"role": "system", "content": self._system_prompt})
        for m in history or []:
            text = (getattr(m, "text", "") or "").strip()
            if text:
                msgs.append({"role": role_of(m), "content": text})
        inc = (getattr(incoming, "text", "") or "").strip()
        if inc:
            msgs.append({"role": "user", "content": inc})
        return msgs

    # ---------- 协议 ----------
    def healthcheck(self) -> bool:
        """GET {base_url}/models 探活(03 文档 §2.3「测通」建议)。"""
        if not self._base_url:
            return False
        try:
            resp = self._ensure_client().get(
                f"{self._base_url}/models", headers=self._headers())
            ok = resp.status_code < 400
            if not ok:
                log.warning("openai healthcheck: HTTP %d", resp.status_code)
            return ok
        except Exception as e:
            log.warning("openai healthcheck 失败: %s: %s", type(e).__name__, e)
            return False

    def reply(self, contact, history, incoming) -> str | None:
        """POST {base_url}/chat/completions,取 choices[0].message.content。
        任何异常(网络/超时/HTTP 非 2xx/解析失败)→ None + 日志。"""
        if not self._base_url or not self._model:
            log.warning("openai: base_url/model 未配置,跳过回复 (contact=%s)", contact)
            return None
        messages = self._build_messages(history, incoming)
        if not messages:
            return None
        payload = {"model": self._model, "messages": messages}
        try:
            resp = self._ensure_client().post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content")
            text = (content or "").strip()
            if not text:
                log.info("openai: 模型返回空内容,不回复 (contact=%s)", contact)
                return None
            log.info("openai: %s <- model=%s (回复 %d 字)", contact, self._model, len(text))
            return text
        except Exception as e:
            log.warning("openai reply 失败(不回复): %s: %s", type(e).__name__, e)
            return None

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


@register_backend("openai")
def _create(cfg: AgentConfig) -> OpenAICompatBackend:
    return OpenAICompatBackend(
        cfg.base_url,
        cfg.api_key,
        cfg.model,
        timeout_sec=cfg.timeout_sec,
        system_prompt=getattr(cfg, "system_prompt", "") or "",
    )
