# -*- coding: utf-8 -*-
"""回复投递队列(docs/PROTOCOL.md §2/§5/§7/§8)。

契约要点:
- **按 chat_id 分队列串行**:同一 chat_id 的投递严格按提交顺序、同一时刻
  至多一条在投(实现:单工作线程逐条取件,天然满足"每会话串行";PROTOCOL
  §7 "PC 端:每会话投递队列串行(天然满足不抢答)");
- 状态机:queued → delivering → delivered / failed(附 reason);
- 重试:失败最多重试 3 次(指数退避 0.5s→1s→2s,封顶 backoff_max_sec),
  即首投 + 3 次重试、总尝试 ≤ 4 次;最终 failed 附原因,Muse 查到后可
  重新提交;
- 幂等:delivery_id 全局唯一;相同 client_ref 的重复提交只投递一次,
  返回既有 delivery_id(§5/§8 幂等去重);
- 持久化:deliveries(含计数器与 client_ref 映射)落盘
  data/mailbox_deliveries.json;重启后 queued/delivering 一律复位为 queued
  继续投递(PROTOCOL §8 "PC 重启:队列从磁盘恢复");
- 离线可测:deliver_fn / 时钟 / sleep 全部可注入,worker 可不启动、
  由 poll_once() 驱动(tests/test_mailbox.py 全程零微信、零网络)。

deliver_fn(chat_id, text) -> bool | None:
  None/True = 成功;False 或抛异常 = 失败(异常文本作为失败原因)。
"""
from __future__ import annotations

import itertools
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

QUEUE_VERSION = 1

DEFAULT_MAX_RETRIES = 3          # PROTOCOL §5:投递重试最多 3 次(指数退避)
DEFAULT_BACKOFF_BASE = 0.5       # 首次重试前 0.5s,之后 ×2 指数退避
DEFAULT_BACKOFF_MAX = 30.0
MAX_DELIVERY_TEXT_CHARS = 8000   # 防呆:单条回复长度上限

# 投递记录保留的终态条数上限(磁盘有界;queued/failed 一律保留直至终态淘汰)
DEFAULT_MAX_HISTORY = 500


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class DeliveryQueue:
    """chat_id 串行回复投递队列(状态机 + 重试 + client_ref 幂等,线程安全)。"""

    def __init__(
        self,
        deliver_fn: Callable[[str, str], Any] | None = None,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_sec: float = DEFAULT_BACKOFF_BASE,
        backoff_max_sec: float = DEFAULT_BACKOFF_MAX,
        poll_interval_sec: float = 0.2,
        persist_path: str | Path | None = None,
        max_history: int = DEFAULT_MAX_HISTORY,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._deliver_fn = deliver_fn
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.0, float(backoff_base_sec))
        self._backoff_max = max(self._backoff_base, float(backoff_max_sec))
        self._poll_interval = max(0.02, float(poll_interval_sec))
        self._persist_path = Path(persist_path) if persist_path else None
        self._max_history = max(10, int(max_history))
        self._wall = wall
        self._sleep = sleep

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._deliveries: list[dict] = []          # 创建序(含终态历史)
        self._by_id: dict[str, dict] = {}
        self._by_ref: dict[str, str] = {}          # client_ref -> delivery_id
        self._seq = itertools.count(1)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._load()

    # ------------------------------------------------------------------ 配置
    def set_deliver_fn(self, fn: Callable[[str, str], Any] | None) -> None:
        """注册/更换投递函数(幂等;引擎装配时调用)。"""
        with self._lock:
            self._deliver_fn = fn

    # ------------------------------------------------------------------ 提交
    def submit(
        self,
        chat_id: str,
        text: str,
        reply_to_msg_id: int | None = None,
        client_ref: str | None = None,
    ) -> tuple[str, bool]:
        """提交一条回复,返回 (delivery_id, 是否命中 client_ref 幂等去重)。

        相同 client_ref 的重复提交不重复入队(§8:PC 只投递一次)。"""
        chat_id = str(chat_id or "").strip()
        text = str(text or "")
        if not chat_id:
            raise ValueError("chat_id 不能为空")
        if not text.strip():
            raise ValueError("回复内容不能为空")
        if len(text) > MAX_DELIVERY_TEXT_CHARS:
            raise ValueError(f"回复过长(上限 {MAX_DELIVERY_TEXT_CHARS} 字)")
        with self._lock:
            ref = str(client_ref or "").strip() or None
            if ref and ref in self._by_ref:
                return self._by_ref[ref], True      # 幂等去重:不重复投递
            try:
                seq = next(self._seq)
            except StopIteration:                    # pragma: no cover
                self._seq = itertools.count(1)
                seq = next(self._seq)
            delivery_id = f"d-{seq:06d}"
            rec = {
                "delivery_id": delivery_id,
                "chat_id": chat_id,
                "text": text,
                "reply_to_msg_id": (int(reply_to_msg_id)
                                    if reply_to_msg_id is not None else None),
                "client_ref": ref,
                "status": "queued",
                "reason": None,
                "attempts": 0,
                "next_attempt_ts": self._wall(),
                "created_ts": _now_iso(),
                "updated_ts": _now_iso(),
            }
            self._deliveries.append(rec)
            self._by_id[delivery_id] = rec
            if ref:
                self._by_ref[ref] = delivery_id
            self._prune_history_locked()
            self._save_locked()
            self._cond.notify()
            return delivery_id, False

    # ------------------------------------------------------------------ 查询
    def status(self, delivery_id: str) -> dict | None:
        """投递状态快照(§2:queued → delivering → delivered/failed(reason))。"""
        with self._lock:
            rec = self._by_id.get(str(delivery_id))
            if rec is None:
                return None
            out = {k: rec[k] for k in (
                "delivery_id", "chat_id", "status", "reason", "attempts",
                "reply_to_msg_id", "client_ref", "created_ts", "updated_ts")}
            return out

    def depths(self) -> dict[str, int]:
        """各会话待投队列深度(queued+delivering;§7 /status 暴露供排查)。"""
        with self._lock:
            out: dict[str, int] = {}
            for rec in self._deliveries:
                if rec["status"] in ("queued", "delivering"):
                    out[rec["chat_id"]] = out.get(rec["chat_id"], 0) + 1
            return out

    def pending_total(self) -> int:
        with self._lock:
            return sum(1 for r in self._deliveries
                       if r["status"] in ("queued", "delivering"))

    # ------------------------------------------------------------------ 处理
    def poll_once(self) -> bool:
        """处理一条到期投递(工作线程主循环体,亦可由测试同步驱动)。

        返回是否处理了一条(False = 当前无到期任务)。deliver_fn 在锁外调用
        (真实投递含键鼠/OCR,耗时秒级,绝不阻塞 submit/status)。"""
        with self._lock:
            now = self._wall()
            item = next((r for r in self._deliveries
                         if r["status"] == "queued"
                         and r["next_attempt_ts"] <= now), None)
            if item is None:
                return False
            item["status"] = "delivering"
            item["attempts"] += 1           # 本次尝试计数(含失败与成功的总尝试)
            item["updated_ts"] = _now_iso()
            delivery_id, chat_id, text = (
                item["delivery_id"], item["chat_id"], item["text"])
            self._save_locked()

        fn = self._deliver_fn
        error: str | None = None
        ok = False
        try:
            if fn is None:
                raise RuntimeError("投递通道未就绪(投递函数未注册,引擎未启动?)")
            result = fn(chat_id, text)
            ok = True if result is None else bool(result)
            if not ok:
                error = "投递失败:发送流程返回失败"
        except Exception as e:                   # 引擎安全闸/键鼠异常 → 失败原因
            error = f"{type(e).__name__}: {e}"

        with self._lock:
            rec = self._by_id.get(delivery_id)
            if rec is not None:
                if ok:
                    rec["status"] = "delivered"
                    rec["reason"] = None
                else:
                    # 首投 + max_retries 次重试;超过即 failed(附原因)
                    if rec["attempts"] > self._max_retries:
                        rec["status"] = "failed"
                    else:
                        rec["status"] = "queued"
                        backoff = min(
                            self._backoff_max,
                            self._backoff_base * (2 ** (rec["attempts"] - 1)))
                        rec["next_attempt_ts"] = self._wall() + backoff
                    rec["reason"] = error      # 重试期间也保留最近失败原因
                rec["updated_ts"] = _now_iso()
                self._save_locked()
        return True

    # ------------------------------------------------------------------ 线程
    def start(self) -> None:
        """启动投递工作线程(幂等;daemon,进程退出不阻塞)。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._worker_loop, name="wxbot-mailbox-delivery",
                daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                did = self.poll_once()
            except Exception:                    # 防热循环:单次异常不致命
                did = False
                self._sleep(0.5)
            if not did:
                with self._cond:
                    self._cond.wait(timeout=self._poll_interval)

    # ------------------------------------------------------------------ 持久化
    def _prune_history_locked(self) -> None:
        """有界历史:超出 max_history 时优先淘汰最旧的终态记录;
        若全是待投记录则不动(待投绝不丢)。"""
        if len(self._deliveries) <= self._max_history:
            return
        for rec in list(self._deliveries):
            if len(self._deliveries) <= self._max_history:
                break
            if rec["status"] in ("delivered", "failed"):
                self._deliveries.remove(rec)
                self._by_id.pop(rec["delivery_id"], None)

    def _save_locked(self) -> None:
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": QUEUE_VERSION,
            "next_seq": (int(self._deliveries[-1]["delivery_id"][2:]) + 1
                         if self._deliveries else 1),
            "client_refs": self._by_ref,
            "deliveries": self._deliveries,
        }
        tmp = self._persist_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self._persist_path)

    def _load(self) -> None:
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except Exception:
            return                               # 投递历史损坏:丢弃重建(消息主存储不受影响)
        deliveries = raw.get("deliveries") or []
        max_id = 0
        for rec in deliveries:
            if not isinstance(rec, dict) or "delivery_id" not in rec:
                continue
            if rec.get("status") in ("queued", "delivering"):
                # 重启恢复:未完成的投递复位为 queued 继续投(§8)
                rec["status"] = "queued"
                rec["next_attempt_ts"] = self._wall()
            self._deliveries.append(rec)
            self._by_id[rec["delivery_id"]] = rec
            try:
                max_id = max(max_id, int(rec["delivery_id"][2:]))
            except (ValueError, IndexError):
                pass
        self._by_ref = {str(k): str(v)
                        for k, v in (raw.get("client_refs") or {}).items()}
        self._by_ref = {k: v for k, v in self._by_ref.items() if v in self._by_id}
        n = max(max_id + 1, int(raw.get("next_seq", 1)))
        self._seq = itertools.count(n)
