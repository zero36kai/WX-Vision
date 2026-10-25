# -*- coding: utf-8 -*-
"""日志/事件总线:内存环形缓冲(默认500条)+ WebSocket 推送 + 慢客户端不阻塞。

设计依据 docs/ARCHITECTURE.md §5.9(WebUI 看到的是内存环形缓冲,最近500条)
与 docs/research/03-agent-webui.md §3.3(环形缓冲 + 广播 + 慢客户端丢弃)。

线程模型:
- `publish()` 可被任意线程调用(引擎采集/工作线程、FastAPI 协程等);
  deque.append 与加锁后的订阅者快照均为线程安全;
- 跨线程投递用 `loop.call_soon_threadsafe`,把"向各订阅者队列投递"这一步
  调度回事件循环线程执行(asyncio.Queue 只允许在循环线程内操作);
- 每个订阅者一个**有界** asyncio.Queue:投递时 `put_nowait`,队列满立即丢弃
  并计数 —— 慢客户端绝不会阻塞总线或撑爆内存(03 文档 §3.3);
- 发送侧(见 wxbot/server/main.py 的泵协程)对每帧 `wait_for` 超时,
  超时即断开该客户端,防止一条卡死的 TCP 连接拖住整个广播。

本模块不依赖 fastapi/starlette,可独立单测。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

try:  # Python 3.9+: QueueFull 位于 asyncio 模块
    from asyncio import QueueFull
except ImportError:  # pragma: no cover
    raise

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class Subscriber:
    """一个 WebSocket 客户端对应的收件箱(有界队列,慢客户端满则丢帧)。"""

    def __init__(self, queue_size: int = 200):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.dropped = 0            # 该客户端因消费慢被丢弃的帧数
        self.last_seq = 0           # 已收到的最大事件序号(重连补历史去重用)

    def offer(self, event: dict) -> bool:
        """非阻塞投递;队列满返回 False 并计数(绝不等待、绝不阻塞总线)。"""
        try:
            self.queue.put_nowait(event)
            self.last_seq = event.get("seq", self.last_seq)
            return True
        except QueueFull:
            self.dropped += 1
            return False


class EventBus:
    """线程安全的事件总线:环形缓冲 + 广播 + stdlib logging 桥接。"""

    def __init__(
        self,
        buffer_size: int = 500,
        client_queue_size: int = 200,
        send_timeout: float = 5.0,
    ):
        self.buffer_size = int(buffer_size)
        self.client_queue_size = int(client_queue_size)
        self.send_timeout = float(send_timeout)
        self._buf: deque[dict] = deque(maxlen=self.buffer_size)
        self._subs: set[Subscriber] = set()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._seq = itertools.count(1)
        self._dropped_frames = 0    # 全局丢弃计数(所有客户端累计)
        self._logging_handler: Optional[logging.Handler] = None

    # ---------- 事件循环挂钩 ----------
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """在事件循环线程内调用(WS 连接建立 / 应用启动时),记录循环引用。"""
        self._loop = loop

    # ---------- 发布 ----------
    def publish(self, text: str, level: str = "INFO", module: str = "app", **extra: Any) -> dict:
        """记录并广播一条事件。任意线程可调用,永不抛出(日志失败不能影响业务)。"""
        try:
            level = str(level).upper() if str(level).upper() in _LEVELS or str(level).upper() == "WARN" else "INFO"
            if level == "WARN":
                level = "WARNING"
            event = {
                "seq": next(self._seq),
                "ts": round(time.time(), 3),
                "level": level,
                "module": str(module)[:40],
                "text": str(text),
            }
            if extra:
                event.update(extra)
            self._buf.append(event)
            self._dispatch(event)
            return event
        except Exception:
            return {}

    def _dispatch(self, event: dict) -> None:
        """把事件投递给所有订阅者(线程安全入口)。"""
        with self._lock:
            subs = list(self._subs)
            loop = self._loop
        if not subs:
            return
        if loop is not None and not loop.is_closed():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                self._offer_all(event, subs)          # 已在循环线程:直接投
            else:
                try:                                   # 工作线程:调度回循环线程
                    loop.call_soon_threadsafe(self._offer_all, event, subs)
                except RuntimeError:
                    pass                               # 循环正关闭:丢弃,不影响业务
        # loop 为 None(尚无任何 WS 连接):只进环形缓冲,连接后补发历史

    def _offer_all(self, event: dict, subs: list[Subscriber]) -> None:
        """仅在事件循环线程内执行:逐个非阻塞投递,慢客户端丢帧不等待。"""
        for sub in subs:
            if not sub.offer(event):
                self._dropped_frames += 1

    # ---------- 订阅 ----------
    def subscribe(self) -> Subscriber:
        sub = Subscriber(queue_size=self.client_queue_size)
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)

    # ---------- 历史 ----------
    def recent(self, n: Optional[int] = None) -> list[dict]:
        """返回缓冲快照(新→旧存储,返回按时间正序),供 WS 连接后补发历史。"""
        with self._lock:
            items = list(self._buf)
        if n is not None:
            items = items[-int(n):]
        return [dict(e) for e in items]

    def stats(self) -> dict:
        with self._lock:
            n_subs = len(self._subs)
            drops = self._dropped_frames + sum(s.dropped for s in self._subs)
        return {
            "buffered": len(self._buf),
            "buffer_size": self.buffer_size,
            "clients": n_subs,
            "dropped_frames": drops,
        }

    # ---------- stdlib logging 桥接 ----------
    def attach_logging(
        self,
        level: Optional[str] = None,
        skip_prefixes: tuple[str, ...] = ("uvicorn", "websockets"),
    ) -> logging.Handler:
        """把标准 logging(root)桥接到总线:引擎等任何模块用 logging 输出,
        WebUI 实时日志即可见。过滤 uvicorn/websockets 自身的访问日志刷屏。
        幂等:重复调用先移除旧 handler。"""
        self.detach_logging()
        handler = _BusHandler(self)
        if level:
            handler.setLevel(getattr(logging, str(level).upper(), logging.INFO))

        class _SkipFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                return not record.name.startswith(skip_prefixes)

        handler.addFilter(_SkipFilter())
        root = logging.getLogger()
        root.addHandler(handler)
        if level:
            try:  # 若 root 级别更高会吞掉事件,放宽到请求级别
                if root.level > handler.level:
                    root.setLevel(handler.level)
            except Exception:
                pass
        self._logging_handler = handler
        return handler

    def detach_logging(self) -> None:
        if self._logging_handler is not None:
            try:
                logging.getLogger().removeHandler(self._logging_handler)
            except Exception:
                pass
            self._logging_handler = None


class _BusHandler(logging.Handler):
    """stdlib logging → EventBus 桥。emit 绝不抛异常(logging 硬性要求)。"""

    def __init__(self, bus: EventBus):
        super().__init__()
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg = f"{msg} | {logging.format_exception(record.exc_info)[-1].strip()}"
            self.bus.publish(msg, level=record.levelname, module=record.name)
        except Exception:
            pass


def dumps(event: dict) -> str:
    """事件 → 单行 JSON(UTF-8,不转义中文),供 WebSocket send_text。"""
    return json.dumps(event, ensure_ascii=False)


def make_pump(bus: EventBus, sub: Subscriber, send_coro: Callable[[str], Any]) -> "asyncio.Task":
    """泵协程工厂:从订阅者队列取事件逐帧发送。

    - 每帧 `wait_for` 限时(bus.send_timeout 秒):慢/死客户端发送超时即退出,
      由调用方关闭 WebSocket 并 unsubscribe —— 总线永不被单个客户端拖住;
    - min_seq 起:跳过补历史之前已推送过的帧(连接时刻与泵启动间隙的去重)。
    """
    min_seq = sub.last_seq

    async def _pump() -> None:
        while True:
            event = await sub.queue.get()
            if event.get("seq", 0) <= min_seq:
                continue
            await asyncio.wait_for(send_coro(dumps(event)), timeout=bus.send_timeout)

    return asyncio.ensure_future(_pump())
