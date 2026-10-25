# -*- coding: utf-8 -*-
"""信箱消息存储与计数器(docs/PROTOCOL.md §3/§4/§6)。

契约要点:
- msg_id 全局唯一、单调递增(允许跳号);计数器持久化到
  data/mailbox_state.json,**重启不回退**——文件存在时 next_msg_id 只增不减;
  next_msg_id() 供引擎分发编号复用同一计数器(PROTOCOL §11,不产生记录);
- 每条消息字段对齐协议 §3(msg_id/chat_id/chat_type/sender/is_from_me/
  msg_type/text/media_id/ts);media_id 仅存编号,完整带 token 的 media_url
  由 API 层按当前口令现生成(口令轮换后旧 URL 自动失效,新响应永远可用);
- ack 语义(§6):POST ack {cursor} = ≤cursor 的全部消息已处理完;
  PC 只在收到 ack 后才归档(标记 archived,不再投递);未 ack 永久保留、
  每次轮询重投;/history 仍可查到归档段;
- ack 幂等:cursor ≤ 已确认值 → no-op 并返回当前值;跳号安全(编号比较);
- 保留期:归档消息默认保留 retention_hours=48 小时(可配 mailbox.
  retention_hours)供审计,之后物理清理;
- 并发安全:全部公开方法持 threading.RLock;
- 持久化:原子写(临时文件 + os.replace);单进程单写者假设(引擎线程 +
  FastAPI 线程共用同一实例,进程内并发由本锁保护)。

红线:未 ack 的消息绝不删除(§1b 信箱协议红线);删除仅限"已归档且超保留期"。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

STATE_VERSION = 1

log = logging.getLogger("wxbot.mailbox.store")

CHAT_TYPES = ("private", "group")
MSG_TYPES = ("text", "image", "voice", "file", "system")

DEFAULT_RETENTION_HOURS = 48   # PROTOCOL §6:归档消息默认保留 48 小时


def _now_iso() -> str:
    """ISO 8601 带时区(PROTOCOL §1:时间一律带时区,如 2026-10-24T10:30:00+08:00)。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class MailboxStore:
    """消息存储 + 单调计数器 + ack 游标 + 保留期清理(线程安全)。"""

    def __init__(
        self,
        path: str | Path,
        *,
        retention_hours: int = DEFAULT_RETENTION_HOURS,
        wall: Callable[[], float] = time.time,
    ):
        self._path = Path(path)
        self._retention_hours = max(1, int(retention_hours))
        self._wall = wall
        self._lock = threading.RLock()
        self._on_append: Callable[[dict], None] | None = None  # 入件旁路回调(notify)
        self._next_msg_id = 1          # 下一个待分配 msg_id(持久化,不回退)
        self._cursor = 0               # 已确认(ack)游标 = archived_until
        self._messages: list[dict] = []  # msg_id 升序(含归档段)
        self._load()

    # ------------------------------------------------------------------ 属性
    @property
    def path(self) -> Path:
        return self._path

    @property
    def retention_hours(self) -> int:
        return self._retention_hours

    @property
    def confirmed_cursor(self) -> int:
        """已确认(ack)游标,即 archived_until。"""
        with self._lock:
            return self._cursor

    @property
    def max_msg_id(self) -> int:
        """已分配的最大 msg_id(允许跳号:分配即计数)。空信箱为 0。"""
        with self._lock:
            return self._next_msg_id - 1

    # ------------------------------------------------------------------ 写入
    def set_on_append(self, fn: Callable[[dict], None] | None) -> None:
        """注册「新消息入信箱」旁路回调(成功落盘后调用,传入该消息完整记录)。

        供 §2.1 入信唤醒通知(notify)使用。回调在锁外执行,回调自身须
        不阻塞(NotifyDispatcher 入队即返回);回调异常一律吞掉(仅 DEBUG),
        **绝不影响入件本身**。传 None 注销。"""
        with self._lock:
            self._on_append = fn

    def append(
        self,
        chat_id: str,
        chat_type: str,
        sender: str,
        *,
        is_from_me: bool = False,
        msg_type: str = "text",
        text: str = "",
        media_id: str | None = None,
        ts: str | None = None,
    ) -> int:
        """入信箱一条消息,返回分配的 msg_id(单调递增,重启不回退)。

        计数器与消息在同一次原子落盘里推进:进程崩溃最坏丢最后一条、
        绝不出现"计数器已推进但消息可见"以外的错序;编号只增不减。"""
        if not str(chat_id or "").strip():
            raise ValueError("chat_id 不能为空")
        with self._lock:
            mid = self._next_msg_id
            self._next_msg_id = mid + 1     # 分配即计数(事务失败允许跳号)
            rec = {
                "msg_id": mid,
                "chat_id": str(chat_id),
                "chat_type": chat_type if chat_type in CHAT_TYPES else "private",
                "sender": str(sender or ("self" if is_from_me else chat_id)),
                "is_from_me": bool(is_from_me),
                "msg_type": msg_type if msg_type in MSG_TYPES else "text",
                "text": str(text or ""),
                "media_id": str(media_id) if media_id else None,
                "ts": str(ts) if ts else _now_iso(),
                "archived": False,
                "archived_at": None,
            }
            self._messages.append(rec)
            self._save_locked()
            hook = self._on_append           # 锁外回调(见 set_on_append)
            snapshot = dict(rec)
        if hook is not None:
            try:
                hook(snapshot)
            except Exception as e:
                # 旁路回调(notify 等)异常绝不影响入件(§2.1 尽力而为)
                log.debug("mailbox on_append 回调异常(已忽略): %s: %s",
                          type(e).__name__, e)
        return mid

    def next_msg_id(self) -> int:
        """仅分配一个 msg_id(**不产生信箱记录**),返回该编号。

        供「与信箱同源计数器」的引擎分发编号使用(PROTOCOL §11:发给任何
        后端的消息对象统一携带 msg_id):与入件共用同一持久化计数器,
        全局唯一、单调递增、重启不回退;未入信箱的消息即跳号(§4 允许)。"""
        with self._lock:
            mid = self._next_msg_id
            self._next_msg_id = mid + 1     # 分配即计数,落盘持久化
            self._save_locked()
            return mid

    # ------------------------------------------------------------------ 读取
    def messages_after(self, after: int = 0, limit: int = 20) -> dict:
        """取 msg_id > after 的**未归档**消息,升序(PROTOCOL §2/§4:只取新消息)。

        返回 {"messages", "next_cursor", "has_more"};未 ack 的消息每次轮询
        都会再次返回(重投),去重由 Muse 按 msg_id 进行。"""
        with self._lock:
            after = max(0, int(after))
            limit = max(1, min(50, int(limit)))
            page = [dict(m) for m in self._messages
                    if not m["archived"] and m["msg_id"] > after]
            has_more = len(page) > limit
            page = page[:limit]
            next_cursor = page[-1]["msg_id"] if page else after
            return {"messages": page, "next_cursor": next_cursor,
                    "has_more": has_more}

    def history(self, chat_id: str, limit: int = 20) -> list[dict]:
        """某会话历史消息(**含已归档**,PROTOCOL §2),升序取末尾 limit 条。"""
        with self._lock:
            limit = max(1, int(limit))
            rows = [dict(m) for m in self._messages if m["chat_id"] == chat_id]
            return rows[-limit:]

    def pending_count(self) -> int:
        """未归档(待取/待 ack)消息总数。"""
        with self._lock:
            return sum(1 for m in self._messages if not m["archived"])

    def unarchived_max_id(self) -> int:
        """未归档消息的最大 msg_id(0 = 无)。"""
        with self._lock:
            ids = [m["msg_id"] for m in self._messages if not m["archived"]]
            return max(ids) if ids else 0

    # ------------------------------------------------------------------ ack
    def ack(self, cursor: int) -> int:
        """确认归档:≤cursor 的全部消息标记 archived(不再投递)。

        - 幂等:cursor ≤ 已确认值 → no-op,返回当前值(PROTOCOL §6);
        - 跳号安全:以编号比较为准,cursor 可大于现存最大编号(跳号);
        - 归档后 /messages 不再返回,/history 仍可查;随后执行保留期清理。
        返回 archived_until。"""
        with self._lock:
            cursor = int(cursor)
            if cursor <= self._cursor:
                return self._cursor          # 重复 ack:幂等 no-op
            self._cursor = cursor
            now_iso = _now_iso()
            for m in self._messages:
                if m["msg_id"] <= cursor and not m["archived"]:
                    m["archived"] = True
                    m["archived_at"] = now_iso
            self._cleanup_retention_locked()
            self._save_locked()
            return self._cursor

    def cleanup_retention(self) -> int:
        """物理清理「已归档且超保留期」的消息,返回清理条数(幂等)。"""
        with self._lock:
            removed = self._cleanup_retention_locked()
            if removed:
                self._save_locked()
            return removed

    def _cleanup_retention_locked(self) -> int:
        horizon = self._wall() - self._retention_hours * 3600.0
        keep: list[dict] = []
        removed = 0
        for m in self._messages:
            if m["archived"] and m["archived_at"]:
                try:
                    at = datetime.fromisoformat(m["archived_at"]).timestamp()
                except ValueError:
                    at = 0.0                 # 时间戳损坏按最旧处理(可清理)
                if at < horizon:
                    removed += 1
                    continue
            keep.append(m)
        self._messages = keep
        return removed

    # ------------------------------------------------------------------ 持久化
    def _load(self) -> None:
        p = self._path
        if not p.exists():
            return                            # 空信箱:首次写入时才创建目录/文件
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            # 状态文件损坏:绝不静默清零(会令 msg_id 回退、违反协议 §4)。
            raise RuntimeError(
                f"信箱状态文件损坏,拒绝加载以免计数器回退: {p} ({e});"
                f"请人工修复或备份后删除该文件") from e
        if not isinstance(raw, dict):
            raise RuntimeError(f"信箱状态文件格式非法(顶层应为 JSON 对象): {p}")
        self._next_msg_id = max(1, int(raw.get("next_msg_id", 1)))
        self._cursor = max(0, int(raw.get("confirmed_cursor", 0)))
        msgs = raw.get("messages") or []
        self._messages = [m for m in msgs if isinstance(m, dict)]
        self._messages.sort(key=lambda m: int(m.get("msg_id", 0)))
        # 迁移期兼容:retention 以构造参数为准(配置文件是权威来源)
        removed = self._cleanup_retention_locked()
        if removed:
            self._save_locked()

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "next_msg_id": self._next_msg_id,
            "confirmed_cursor": self._cursor,
            "retention_hours": self._retention_hours,
            "messages": self._messages,
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self._path)          # 原子替换:崩溃不留半截文件
