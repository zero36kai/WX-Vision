# -*- coding: utf-8 -*-
"""入信唤醒通知(docs/PROTOCOL.md §2.1,可选 mailbox.notify_url)。

契约(§2.1,逐条):
- 每有新消息入信箱,PC **尽力而为**地向 notify_url 发一个短超时(**3 秒**)
  的 POST,body 即该消息的完整字段(含 msg_id);
- 失败/超时**完全忽略**(仅 DEBUG 日志),**绝不阻塞入信**;
- 它只是"该来轮询了"的敲门砖,**不是可靠信号**——Muse 的定时轮询仍是
  唯一可靠机制;未配置(url 为空)零行为、零开销。

实现:
- 专用后台 worker 线程 + 有界队列:入信线程只做 put_nowait(满即丢、
  DEBUG 记一条),网络耗时全部发生在后台线程,入信路径零阻塞;
- transport 可注入(离线测试零网络);默认传输为进程内共享的 httpx.Client
  (POST JSON,非 2xx 视为失败);
- 进程退出收尾:MailboxService.stop() → NotifyDispatcher.stop()(停线程、
  关客户端);另注册 atexit 兜底,任何退出路径都收尾(daemon 线程本身
  也不阻塞进程退出,双保险)。
"""
from __future__ import annotations

import atexit
import logging
import queue as _queue
import threading
from typing import Any, Callable

log = logging.getLogger("wxbot.mailbox.notify")

DEFAULT_TIMEOUT_SEC = 3.0     # §2.1:短超时 3 秒
DEFAULT_QUEUE_SIZE = 100      # 有界队列:notify 绝不反压入信(满即丢)

# transport 签名:(url, payload_dict, timeout_sec) -> None;失败抛异常
Transport = Callable[[str, dict, float], None]


class _HttpxTransport:
    """默认传输:进程内共享 httpx.Client(惰性创建,close 由 stop 收尾调用)。

    POST JSON;HTTP ≥400 抛异常(调用方按失败忽略,仅 DEBUG)。"""

    def __init__(self) -> None:
        self._client: Any = None            # 惰性创建(首次通知时)
        self._lock = threading.Lock()

    def __call__(self, url: str, payload: dict, timeout_sec: float) -> None:
        client = self._ensure()
        resp = client.post(url, json=payload, timeout=timeout_sec)
        resp.raise_for_status()

    def _ensure(self) -> Any:
        with self._lock:
            if self._client is None:
                import httpx                # 惰性导入,保持包轻量
                self._client = httpx.Client(timeout=DEFAULT_TIMEOUT_SEC)
            return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None


class NotifyDispatcher:
    """入信唤醒通知调度器(尽力而为;线程安全;url 为空时零行为)。

    用法:MailboxService 把它注册为 MailboxStore 的入件回调——新消息成功
    落盘后 notify(record) 被调用,入队即返回;后台线程逐条 POST。
    """

    def __init__(
        self,
        url: str = "",
        *,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        transport: Transport | None = None,
    ):
        self._url = str(url or "").strip()
        self._timeout = max(0.1, float(timeout_sec))
        self._transport: Any = transport if transport is not None else _HttpxTransport()
        self._q: "_queue.Queue" = _queue.Queue(maxsize=max(1, int(queue_size)))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._atexit_handle = None

    # ------------------------------------------------------------------ 配置
    @property
    def url(self) -> str:
        return self._url

    def set_url(self, url: str) -> None:
        """运行期更新通知地址(空串 = 关闭通知;配置文件是权威来源)。"""
        self._url = str(url or "").strip()

    # ------------------------------------------------------------------ 入队
    def notify(self, message: dict) -> None:
        """新消息入信箱后的旁路通知:入队即返回,绝不阻塞入信(§2.1)。

        url 未配置 → 零行为;队列满 → 丢弃本条并记 DEBUG(通知本就是
        尽力而为的敲门砖,丢一条等下一轮轮询即可)。"""
        if not self._url:
            return
        try:
            self._q.put_nowait(message)
        except _queue.Full:
            log.debug("mailbox notify 队列已满,丢弃一条唤醒通知"
                      "(msg_id=%s;轮询仍是唯一可靠机制)", message.get("msg_id"))
            return
        self._ensure_worker()

    # ------------------------------------------------------------------ 线程
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._worker_loop, name="wxbot-mailbox-notify",
                daemon=True)
            self._thread.start()
            if self._atexit_handle is None:   # 进程退出兜底收尾(幂等可重注册)
                self._atexit_handle = atexit.register(self.stop)

    def _worker_loop(self) -> None:
        while True:
            try:
                msg = self._q.get(timeout=0.5)
            except _queue.Empty:
                if self._stop.is_set():
                    break
                continue
            try:
                self._transport(self._url, msg, self._timeout)
            except Exception as e:
                # §2.1:失败/超时完全忽略,仅 DEBUG(绝不是可靠信号)
                log.debug("mailbox notify 失败(已忽略,url=%s): %s: %s",
                          self._url, type(e).__name__, e)

    # ------------------------------------------------------------------ 收尾
    def stop(self, timeout: float = 3.0) -> None:
        """进程退出/服务关闭收尾:停 worker、关传输资源(幂等)。

        stop 后再次 notify 会按需重建 worker(懒启动),不影响复用。"""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=max(0.0, float(timeout)))
        self._thread = None
        closer = getattr(self._transport, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
        if self._atexit_handle is not None:
            try:
                atexit.unregister(self._atexit_handle)
            except Exception:
                pass
            self._atexit_handle = None
