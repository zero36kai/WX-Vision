# -*- coding: utf-8 -*-
"""信箱协议 v1 的 8 个 HTTP 端点(docs/PROTOCOL.md §2),挂到现有 FastAPI server。

鉴权:复用 server 的 X-Access-Token 中间件(所有 /api/* 路径一律受保护,
无/错 token → 401,PROTOCOL §1);媒体下载兼容 ?token= 查询参数——
media_url 里带的正是这个 token。

协议版本:全部响应带 X-Mailbox-Protocol: 1(中间件统一追加,Muse 发现
版本不匹配应停止并提示)。

错误约定:4xx 一律 {"error": "<中文原因>"}(§2)。
"""
from __future__ import annotations

from typing import Any, Callable
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import MutableHeaders

from wxbot.config import AppConfig
from wxbot.mailbox import PROTOCOL_VERSION
from wxbot.mailbox.media import MEDIA_ID_RE
from wxbot.mailbox.service import MailboxService

MAX_MESSAGES_LIMIT = 50        # PROTOCOL §2:M≤50,默认 20
DEFAULT_MESSAGES_LIMIT = 20


def _norm_set(names) -> set[str]:
    return {str(n or "").strip() for n in (names or []) if str(n or "").strip()}


def listen_scope(raw: dict) -> list[str]:
    """信箱监听会话范围(§9:默认 = 私聊白名单 ∪ 群白名单)。"""
    cfg = AppConfig.from_dict(raw or {})
    return sorted(_norm_set(cfg.safety.whitelist) | _norm_set(cfg.groups.whitelist))


def exec_senders(raw: dict) -> list[str]:
    """可被 Muse 当指令执行的发送者(§9:默认 = 私聊白名单)。"""
    return sorted(_norm_set(AppConfig.from_dict(raw or {}).safety.whitelist))


class MailboxProtocolHeaderMiddleware:
    """所有 HTTP 响应追加 X-Mailbox-Protocol: 1(§1 协议版本声明)。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapped(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Mailbox-Protocol"] = str(
                    PROTOCOL_VERSION)
            await send(message)

        await self.app(scope, receive, send_wrapped)


def _media_url(request: Request, media_id: str, token: str) -> str:
    """完整带 token 的媒体下载 URL(§3:完整 URL、禁止 base64)。

    经 Cloudflare 隧道访问时 Host/X-Forwarded-Proto 即公网域名,
    Muse 拿到的链接可直接下载。"""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    base = f"{proto}://{host}"
    url = f"{base}/api/media/{media_id}"
    if token:
        url += f"?token={quote(token, safe='')}"
    return url


def _to_protocol_message(rec: dict, request: Request, token: str) -> dict:
    """存储记录 → 协议 §3 消息字段(media_id 换成完整 media_url)。"""
    media_url = None
    media_id = rec.get("media_id")
    if media_id and MEDIA_ID_RE.match(str(media_id)):
        media_url = _media_url(request, str(media_id), token)
    return {
        "msg_id": int(rec.get("msg_id", 0)),
        "chat_id": rec.get("chat_id", ""),
        "chat_type": rec.get("chat_type", "private"),
        "sender": rec.get("sender", ""),
        "is_from_me": bool(rec.get("is_from_me", False)),
        "msg_type": rec.get("msg_type", "text"),
        "text": rec.get("text", ""),
        "media_url": media_url,
        "ts": rec.get("ts", ""),
    }


def register_mailbox_routes(
    app: FastAPI,
    service: MailboxService,
    *,
    config_path,
    resolve_password: Callable[[], str],
) -> None:
    """把 §2 全部端点挂到 app(create_app 调用;测试可注入独立 service)。"""

    def _read_raw() -> dict:
        import json
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _in_scope(chat_id: str) -> bool:
        return str(chat_id or "").strip() in _norm_set(listen_scope(_read_raw()))

    app.add_middleware(MailboxProtocolHeaderMiddleware)

    # ---------------- GET /api/mailbox/status ----------------
    @app.get("/api/mailbox/status")
    def mailbox_status():
        st = service.store
        return {
            "ok": True,
            "protocol": PROTOCOL_VERSION,
            "max_msg_id": st.max_msg_id,
            "cursor": st.confirmed_cursor,          # 已确认(archived_until)
            "pending": st.pending_count(),          # 待取(未归档)条数
            "queues": service.queue.depths(),       # 各会话投递队列深度(§7)
            "retention_hours": st.retention_hours,
        }

    # ---------------- GET /api/mailbox/messages ----------------
    @app.get("/api/mailbox/messages")
    def mailbox_messages(request: Request, after: str = "0", limit: str = str(DEFAULT_MESSAGES_LIMIT)):
        # 参数手工解析:非法值按协议约定回 4xx 中文错误(而非框架默认 422 英文)
        try:
            after_i = int(after)
            limit_i = int(limit)
        except (TypeError, ValueError):
            return JSONResponse({"error": "after/limit 必须是整数"}, status_code=400)
        if after_i < 0:
            return JSONResponse({"error": "after 必须为 ≥0 的整数"}, status_code=400)
        page = service.store.messages_after(after=after_i, limit=limit_i)
        token = str(resolve_password() or "")
        msgs = [_to_protocol_message(m, request, token) for m in page["messages"]]
        return {
            "messages": msgs,
            "next_cursor": page["next_cursor"],
            "has_more": page["has_more"],
        }

    # ---------------- GET /api/mailbox/history ----------------
    @app.get("/api/mailbox/history")
    def mailbox_history(request: Request, chat_id: str = "", limit: str = str(DEFAULT_MESSAGES_LIMIT)):
        if not str(chat_id or "").strip():
            return JSONResponse({"error": "缺少 chat_id 参数"}, status_code=400)
        try:
            limit_i = int(limit)
        except (TypeError, ValueError):
            return JSONResponse({"error": "limit 必须是整数"}, status_code=400)
        rows = service.store.history(str(chat_id), limit=limit_i)
        token = str(resolve_password() or "")
        return {"chat_id": str(chat_id),
                "messages": [_to_protocol_message(m, request, token) for m in rows]}

    # ---------------- POST /api/mailbox/replies ----------------
    @app.post("/api/mailbox/replies")
    async def mailbox_replies(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "请求体必须是 JSON 对象"}, status_code=400)
        chat_id = str(body.get("chat_id", "") or "").strip()
        text = str(body.get("text", "") or "")
        reply_to = body.get("reply_to_msg_id")
        client_ref = body.get("client_ref")
        if not chat_id:
            return JSONResponse({"error": "缺少 chat_id(须原样带回消息的 chat_id)"},
                                status_code=400)
        if not text.strip():
            return JSONResponse({"error": "回复内容 text 不能为空"}, status_code=400)
        if reply_to is not None:
            try:
                reply_to = int(reply_to)
            except (TypeError, ValueError):
                return JSONResponse({"error": "reply_to_msg_id 必须是整数"}, status_code=400)
        if client_ref is not None:
            client_ref = str(client_ref).strip()[:128] or None
        # 红线(§5):chat_id 必须在监听范围内,否则 403 拒收——
        # 信箱回复永远不可能发到范围外的会话
        if not _in_scope(chat_id):
            return JSONResponse({
                "error": f"chat_id「{chat_id}」不在信箱监听范围内"
                         f"(监听范围=私聊白名单∪群白名单,见 GET /api/mailbox/config),已拒收",
            }, status_code=403)
        try:
            delivery_id, dedup = service.queue.submit(
                chat_id, text, reply_to_msg_id=reply_to, client_ref=client_ref)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({
            "delivery_id": delivery_id,
            "deduplicated": dedup,
            "note": "已受理,异步投递;GET /api/mailbox/deliveries/{id} 查询状态",
        }, status_code=202)

    # ---------------- GET /api/mailbox/deliveries/{delivery_id} ----------------
    @app.get("/api/mailbox/deliveries/{delivery_id}")
    def mailbox_delivery(delivery_id: str):
        st = service.queue.status(delivery_id)
        if st is None:
            return JSONResponse({"error": f"未知 delivery_id: {delivery_id}"}, status_code=404)
        return st

    # ---------------- POST /api/mailbox/ack ----------------
    @app.post("/api/mailbox/ack")
    async def mailbox_ack(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "请求体不是合法 JSON"}, status_code=400)
        if not isinstance(body, dict) or "cursor" not in body:
            return JSONResponse({"error": "缺少 cursor(整数,已处理到的最大 msg_id)"},
                                status_code=400)
        try:
            cursor = int(body["cursor"])
        except (TypeError, ValueError):
            return JSONResponse({"error": "cursor 必须是整数"}, status_code=400)
        if cursor < 0:
            return JSONResponse({"error": "cursor 必须为 ≥0 的整数"}, status_code=400)
        archived_until = service.store.ack(cursor)   # 幂等;归档+保留期清理
        return {"archived_until": archived_until}

    # ---------------- GET /api/mailbox/config ----------------
    @app.get("/api/mailbox/config")
    def mailbox_config():
        raw = _read_raw()
        return {
            "protocol": PROTOCOL_VERSION,
            "listen_chats": listen_scope(raw),      # 私聊白名单 ∪ 群白名单
            "exec_senders": exec_senders(raw),      # 默认=私聊白名单
            "bot_identity": "self",
            "redelivery": True,
            "retention_hours": service.store.retention_hours,
        }

    # ---------------- GET /api/media/{media_id} ----------------
    @app.get("/api/media/{media_id}")
    def media_download(media_id: str):
        mid = str(media_id or "")
        if not MEDIA_ID_RE.match(mid):
            return JSONResponse({"error": "media_id 非法"}, status_code=404)
        data = service.media.read(mid)
        if data is None:
            return JSONResponse({"error": f"媒体不存在或已清理: {mid}"}, status_code=404)
        return Response(
            content=data,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'inline; filename="{mid}.png"',
            },
        )
