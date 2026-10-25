# -*- coding: utf-8 -*-
"""引擎状态:消息指纹去重 + 每联系人会话上下文(线程安全,纯内存)。

去重设计(ARCHITECTURE §5.5,调研03 §1.2/§1.3):
- 视觉方案没有消息 ID,指纹 = hash(联系人 + 发送者 + 规范化OCR文本);
- 有界缓存 200 条、TTL 600 秒:过期与容量淘汰防止内存无界;
- 排除自己消息(side=="right" / sender=="self"):自己气泡不触发回复;
- 已回复过的指纹不再回复;「取出即标记」,agent 失败也不重复轰炸。

上下文:每联系人独立 deque,上限 20 条(engine.context_window),
超过裁掉最旧;仅供注入 agent 的 history,不落盘。
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Callable

DEFAULT_CACHE_SIZE = 200    # ARCHITECTURE §5.5
DEFAULT_TTL_SEC = 600.0     # 10 分钟
DEFAULT_CONTEXT_WINDOW = 20

_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """OCR 文本规范化:首尾空白去除、连续空白(含全角空格/换行)折叠为单空格。

    仅做确定性归一,不做模糊纠错——指纹唯一目的是「同一条消息别回两次」,
    规范化越保守越不易把两条不同消息误并。
    """
    return _WS_RE.sub(" ", (text or "").replace("\u3000", " ")).strip()


def is_own(msg: Any) -> bool:
    """是否自己发出的消息(flows.Message.side=="right" 或 sender=="self")。"""
    return getattr(msg, "side", "") == "right" or getattr(msg, "sender", "") == "self"


def fingerprint(contact: str, sender: str, text: str) -> str:
    """指纹 = sha1(联系人 ␟ 发送者 ␟ 规范化文本)。同内容不同联系人/发送者不冲突。"""
    norm = normalize_text(text)
    raw = f"{contact or ''}\x1f{sender or ''}\x1f{norm}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def fingerprint_of(contact: str, msg: Any) -> str:
    return fingerprint(contact, getattr(msg, "sender", ""), getattr(msg, "text", ""))


class FingerprintCache:
    """有界 + TTL 的「已处理指纹」缓存(取出即标记语义)。

    mark(fp) → True 表示首次见到(并登记);False 表示 TTL 内重复。
    thread-safe。
    """

    def __init__(
        self,
        capacity: int = DEFAULT_CACHE_SIZE,
        ttl_sec: float = DEFAULT_TTL_SEC,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._capacity = max(1, int(capacity))
        self._ttl = float(ttl_sec)
        self._clock = clock
        self._lock = threading.Lock()
        self._items: OrderedDict[str, float] = OrderedDict()  # fp -> 登记时间

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def _prune(self, now: float) -> None:
        if self._ttl > 0:
            deadline = now - self._ttl
            while self._items:
                _, ts = next(iter(self._items.items()))
                if ts >= deadline:
                    break
                self._items.popitem(last=False)
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)

    def mark(self, fp: str) -> bool:
        """登记指纹。TTL 内已存在 → False(重复);否则登记并返回 True。"""
        with self._lock:
            now = self._clock()
            ts = self._items.get(fp)
            if ts is not None and (self._ttl <= 0 or now - ts <= self._ttl):
                self._items.move_to_end(fp)
                return False
            self._items[fp] = now
            self._items.move_to_end(fp)
            self._prune(now)
            return True

    def seen(self, fp: str) -> bool:
        """TTL 内是否已登记(不改动状态)。"""
        with self._lock:
            ts = self._items.get(fp)
            if ts is None:
                return False
            if self._ttl > 0 and self._clock() - ts > self._ttl:
                return False
            return True

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class SeenSet:
    """采集线程用的「本轮运行以来见过的消息」指纹集合(无 TTL、有界)。

    与 FingerprintCache 的分工:工作线程的 TTL 缓存过期后,同一屏幕上的旧消息
    会被重新读出;若仅靠 TTL 缓存,600 秒后旧消息会被当成新消息重复回复。
    采集侧用本集合(引擎运行期内不遗忘,容量淘汰最旧)拦截已入队过的消息,
    TTL 缓存只作为第二道防线(跨重启/竞态)。
    """

    def __init__(self, capacity: int = 1000):
        self._capacity = max(1, int(capacity))
        self._lock = threading.Lock()
        self._items: OrderedDict[str, None] = OrderedDict()

    def is_new(self, fp: str) -> bool:
        """首次见到 → True 并登记;已见过 → False。"""
        with self._lock:
            if fp in self._items:
                self._items.move_to_end(fp)
                return False
            self._items[fp] = None
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
            return True

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class ContactContextStore:
    """每联系人独立会话上下文(内存,上限 context_window 条,时间序)。

    值元素是 flows.Message(鸭子类型:.sender/.text/.side/.ts);
    自己的回复以 Message(sender="self", side="right") 形式入列,
    供 agent 侧 role_of() 区分 user/assistant。
    """

    def __init__(self, window: int = DEFAULT_CONTEXT_WINDOW):
        self._window = max(1, int(window))
        self._lock = threading.Lock()
        self._by_contact: dict[str, deque] = {}

    def history(self, contact: str) -> list:
        """该联系人当前上下文的快照副本(时间序)。"""
        with self._lock:
            dq = self._by_contact.get(contact)
            return list(dq) if dq else []

    def append_user(self, contact: str, msg: Any) -> None:
        with self._lock:
            self._by_contact.setdefault(
                contact, deque(maxlen=self._window)).append(msg)

    def append_assistant(self, contact: str, text: str, *,
                         msg_id: int | None = None,
                         chat_id: str | None = None) -> None:
        """记录本引擎(或 agent)发出的回复,作为下一轮的 assistant 上下文。

        v1.1(PROTOCOL §11):统一携带 msg_id/chat_id——webhook 报文的
        history 每条均含 msg_id 与 chat_id。msg_id 由引擎传入(真实外发用
        入信箱返回的编号,同一条消息全局一个编号;演练意图用同源计数器
        补分配);chat_id 缺省 = 会话键 contact。"""
        self._append_right(contact, text, sender="self", msg_id=msg_id,
                           chat_id=chat_id if chat_id is not None else contact)

    def _append_right(self, contact: str, text: str, sender: str = "self",
                      msg_id: int | None = None,
                      chat_id: str | None = None) -> None:
        msg = _SimpleMessage(sender=sender, text=text, side="right", ts=None,
                             msg_id=msg_id, chat_id=chat_id)
        with self._lock:
            self._by_contact.setdefault(
                contact, deque(maxlen=self._window)).append(msg)

    def clear(self, contact: str) -> None:
        with self._lock:
            self._by_contact.pop(contact, None)

    def clear_all(self) -> None:
        with self._lock:
            self._by_contact.clear()

    def contacts(self) -> list[str]:
        with self._lock:
            return list(self._by_contact.keys())


class _SimpleMessage:
    """上下文里自己回复的轻量消息载体(与 flows.Message 同形,避免反向依赖)。

    v1.1:附加 msg_id/chat_id(PROTOCOL §11:webhook history 每条均含;
    未编号时 msg_id=None)。"""

    __slots__ = ("sender", "text", "side", "ts", "msg_id", "chat_id")

    def __init__(self, sender: str, text: str, side: str, ts: str | None,
                 msg_id: int | None = None, chat_id: str | None = None):
        self.sender = sender
        self.text = text
        self.side = side
        self.ts = ts
        self.msg_id = msg_id
        self.chat_id = chat_id
