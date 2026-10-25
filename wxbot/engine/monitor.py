# -*- coding: utf-8 -*-
"""监控引擎:感知(采集线程)→ 处理(工作线程)→ 发送(串行锁),队列解耦。

线程模型(ARCHITECTURE §5.5/§5.6、调研03 §1.3 三段式):
- 采集线程:轮询 check_unread ∩ 白名单 → open_chat → read_messages → 新消息入队
  (采集只负责「发现新东西」,不做业务决策;SeenSet 防旧消息反复入队);
- 工作线程:出队 → 去重(取出即标记)→ 白名单/忽略词 → agent.reply → 限频
  → 拟人延迟(1~3s 随机)→ _deliver;
- 发送串行锁:真实键鼠发送同一时刻只有一条在执行;
- stop():置停机事件 + 哨兵唤醒,join 线程,优雅退出。

安全(不可绕过):
- auto_reply_enabled 默认 False:不开就不自动回复(开箱零发送);
- 自动回复与 send_now 全部经过 safety.SafetyGate(白名单/限频/发送闸);
- dry_run=True(默认)→ 只记录发送意图到日志/事件流,绝不执行键鼠;
  真实发送必须 dry_run=False 且 config.allow_real_send=True 双开关;
- 真实发送最终必经 flows.send_message(内部白名单+OCR标题双闸门);
- 群聊(2026-10-24 起三闸门,默认全关):群消息真实发送必须同时满足
  groups.enabled + 群∈groups.whitelist + groups.allow_real_send(默认 false);
  三闸不全 → 只记发送意图(与旧「双层硬禁」行为一致);三闸全开仍需
  dry_run=False + allow_real_send=True 双开关,且 flows.send_message 的
  OCR 标题闸门兜底。开启后仍需 reply_mode 触发(at/all)才交给 agent;
  flows.send_message 群分支同步按三闸门校验(2026-10-24 信箱功能改造),
  三闸全开的受管群凭三闸门放行,无需(也不应)把群名加入联系人白名单。

双模路由(2026-10-24,PROTOCOL §11):
- agent.backends 列表 + agent.routing{联系人→backend id} + default_backend;
- mailbox 类型后端:该联系人的消息**入信箱**(is_from_me=false),不同步
  回复、不调本机 agent——Muse 经 /api/mailbox/* 异步取信/回信;
- 本机一切外发消息(手动 send_now / 任何后端的自动回复)**真实发送成功后**
  同时入信箱(is_from_me=true、sender=self),供 Muse 作上下文,绝不触发处理;
- dry_run 意图不入信箱(消息并未真的发出,入件会让 Muse 误以为已回复);
- 信箱回复经 /api/mailbox/replies 回流 → 投递队列 → mailbox_deliver →
  与手动发送完全相同的闸门链(_deliver,require_real=True):闸门不放行
  (演练模式/群三闸不全/白名单外)时投递按失败处理(终态 failed 附中文
  原因),绝不虚报 delivered——否则 Muse 会据假 ack 推进游标,回复静默
  丢失(2026-10-24 审查修复,PROTOCOL §2/§6)。

分发编号(v1.1,PROTOCOL §11):
- 引擎分发消息给**任何**后端(echo/openai/webhook)时,消息对象统一携带
  msg_id/chat_id:编号取自信箱**同源持久化计数器**(allocate_msg_id,
  全局唯一、单调递增、重启不回退;未入信箱的消息即跳号,§4 允许),
  chat_id=联系人/群显示名(回复路由键);
- 信箱路由的消息对象携带的编号 = 信箱记录编号(与 Muse 轮询一致);
  真实外发入信箱返回的编号同样复用到 assistant 上下文条目(一条消息
  全局一个编号);webhook 报文顶层与 history/incoming 每条均含
  msg_id/chat_id(未编号条目为 null——仅计数器不可用/直接单测构造时);
- 编号失败(信箱服务不可用)绝不阻塞分发,报文该字段为 null。

可测试性:构造函数只收配置(不创建微信窗口/flows);flows 工厂与 agent
均可注入 stub,tests/test_engine.py 全程离线,绝不触碰微信窗口。
信箱服务可注入(tests/test_mailbox.py 用临时目录),缺省懒加载进程单例
(项目根 data/,首次真正写入才落盘)。
"""
from __future__ import annotations

import logging
import queue
import random
import threading
import time
import traceback
from collections import deque
from typing import Any, Callable

from wxbot.agent.base import create_agent
from wxbot.config import AppConfig
from wxbot.engine.groups import GroupRouter
from wxbot.engine.safety import RateLimiter, SafetyGate, SafetyRejected
from wxbot.engine.state import (
    ContactContextStore,
    FingerprintCache,
    SeenSet,
    fingerprint_of,
    is_own,
)

log = logging.getLogger("wxbot.engine.monitor")


class MonitorEngine:
    """微信消息监控/自动回复引擎(由 server 直接托管)。"""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        flows_factory: Callable[[], Any] | None = None,
        agent: Any | None = None,
        on_event: Callable[[dict], None] | None = None,
        mailbox: Any | None = None,
    ):
        self.cfg = cfg
        self._flows_factory = flows_factory
        self._on_event = on_event
        self._agent_injected = agent is not None   # 注入桩:非信箱路由一律复用

        # 双模路由:backends/routing 解析(2026-10-24)
        self._backends_by_id = {b.id: b for b in (cfg.agent.backends or [])}
        self._routing: dict[str, str] = dict(cfg.agent.routing or {})
        self._default_backend_id = (
            cfg.agent.default_backend
            or (cfg.agent.backends[0].id if cfg.agent.backends else cfg.agent.mode)
        )
        self._backend_cache: dict[str, Any] = {}

        # 信箱服务:注入(测试)优先;缺省懒加载进程单例(项目根 data/)
        self._mailbox: Any | None = mailbox
        self._mailbox_failed = False   # 单例创建失败只报一次,绝不影响主流程

        # 安全闸门(公开属性:改配置后可直接同步 dry_run/allow_real_send)
        self.gate = SafetyGate(
            cfg.safety.whitelist,
            dry_run=cfg.engine.dry_run,
            allow_real_send=cfg.allow_real_send,
            rate_limiter=RateLimiter(
                global_qpm=cfg.rate_limit.global_qpm,
                per_contact_qpm=cfg.engine.max_replies_per_contact_per_min,
                cooldown_sec=cfg.rate_limit.per_contact_cooldown_sec,
            ),
        )
        self.cache = FingerprintCache(
            capacity=cfg.capture.dedup_cache_size, ttl_sec=cfg.capture.dedup_ttl_sec)
        self.ctx = ContactContextStore(window=cfg.engine.context_window)
        self._seen = SeenSet(capacity=1000)
        self.groups = GroupRouter(cfg.groups)   # 群聊路由(默认全关)

        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self.counters: dict[str, int] = {
            "seen": 0, "self_msg": 0, "deduped": 0, "not_whitelisted": 0,
            "ignored": 0, "auto_disabled": 0, "agent_none": 0,
            "rate_limited": 0, "send_intent": 0, "sent": 0, "send_failed": 0,
            "errors": 0,
            "group_ignored": 0,          # 群消息被路由规则忽略(默认全关的主去处)
            "mailbox_ingested": 0,       # 入信箱消息数(来信 is_from_me=false)
            "mailbox_outbound": 0,       # 外发消息入信箱数(is_from_me=true)
        }
        self.events: deque[dict] = deque(maxlen=500)

        # 注入的 agent 立即可用(离线单测不经 start() 直接调 _handle);
        # 未注入则 start() 时经工厂创建
        self._agent: Any | None = agent
        self._flows: Any | None = None
        self._thread_collector: threading.Thread | None = None
        self._thread_worker: threading.Thread | None = None
        self._started_at: float | None = None
        self._last_error: str = ""

    # ------------------------------------------------------------------ 事件
    def emit(self, ev_type: str, **fields: Any) -> None:
        """结构化事件:进内存环形缓冲(供 status/测试断言),可选回调给 server。"""
        ev = {"ts": time.time(), "type": ev_type, **fields}
        self.events.append(ev)
        level = logging.ERROR if ev_type in ("error", "safety_block") else logging.INFO
        log.log(level, "[%s] %s", ev_type,
                {k: v for k, v in fields.items() if k != "text"} or "")
        if self._on_event is not None:
            try:
                self._on_event(ev)
            except Exception:   # 回调异常绝不影响引擎线程
                log.exception("on_event 回调异常(已忽略)")

    def _bump(self, key: str, n: int = 1) -> None:
        with self._counter_lock:
            self.counters[key] = self.counters.get(key, 0) + n

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """初始化 agent + flows 并拉起两个线程。构造时不创建窗口,start 才初始化。"""
        if self._thread_worker is not None and self._thread_worker.is_alive():
            raise RuntimeError("MonitorEngine 已在运行")
        self._stop.clear()
        self._last_error = ""
        try:
            if self._agent is None and not self._default_is_mailbox():
                self._agent = create_agent(self.cfg.agent)   # 默认后端(非信箱)
            self._flows = self._ensure_flows()
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            self.emit("error", where="start", message=self._last_error)
            raise
        # 信箱:注册投递函数(与手动发送同一闸门链)+ 启动投递队列线程
        svc = self._ensure_mailbox()
        if svc is not None:
            try:
                svc.set_deliver_fn(self.mailbox_deliver)
                svc.start()
            except Exception as e:
                self.emit("error", where="mailbox_start", message=str(e))
        self._started_at = time.time()
        self._thread_collector = threading.Thread(
            target=self._collector_loop, name="wxbot-collector", daemon=True)
        self._thread_worker = threading.Thread(
            target=self._worker_loop, name="wxbot-worker", daemon=True)
        self._thread_collector.start()
        self._thread_worker.start()
        self.emit("engine_start",
                  agent=(self._agent.name() if self._agent else "mailbox"),
                  default_backend=self._default_backend_id,
                  backends={b.id: b.type for b in self._backends_by_id.values()},
                  routing=len(self._routing),
                  dry_run=self.gate.dry_run,
                  allow_real_send=self.gate.allow_real_send,
                  auto_reply_enabled=self.cfg.engine.auto_reply_enabled,
                  whitelist=self.gate.whitelist,
                  groups_enabled=self.cfg.groups.enabled,
                  groups_reply_mode=self.cfg.groups.reply_mode,
                  groups_whitelist=self.groups.managed_groups,
                  poll_interval_sec=self.cfg.engine.poll_interval_sec)

    def _build_default_flows(self) -> Any:
        """真实依赖装配(仅 start 时执行;离线测试注入 stub 不会走到这里)。"""
        from wxbot.actions.flows import WeChatFlows
        from wxbot.actions.human_input import HumanInput
        from wxbot.vision.ocr import run_ocr
        from wxbot.vision.window import WeChatWindow

        win = WeChatWindow.find()
        if win is None:
            raise RuntimeError(
                "未找到微信主窗口(class=Qt51514QWindowIcon 且 title=微信),"
                "请先登录并显示微信")
        # 白名单与引擎同一份配置:flows.send_message 内部双闸门用同一名单;
        # 群聊三闸门配置同步传给 flows 群分支(groups.allow_real_send 等);
        # 拟人画像(2026-10-24):HumanInput 全链路走 config.human 配置段
        return WeChatFlows(win, HumanInput(config=self.cfg.human), run_ocr,
                           whitelist=list(self.cfg.safety.whitelist),
                           groups_cfg=self.cfg.groups)

    def _ensure_flows(self) -> Any:
        """惰性初始化 flows(手动发送在引擎未 start 时也可用,调研03 §3.2④)。"""
        if self._flows is None:
            self._flows = (self._flows_factory() if self._flows_factory
                           else self._build_default_flows())
        return self._flows

    # ------------------------------------------------------------------ 双模路由(2026-10-24)
    def _default_is_mailbox(self) -> bool:
        b = self._backends_by_id.get(self._default_backend_id)
        return bool(b and b.type == "mailbox")

    def _resolve_backend(self, contact: str) -> tuple[Any | None, Any | None]:
        """解析联系人 → (后端实例, 后端配置)。

        返回 (None, mailbox配置) = 信箱路由:消息入信箱,不调本机 agent。
        注入的 agent 桩(测试)优先服务所有非信箱路由;
        默认后端复用 legacy self._agent(与旧单模式行为完全一致)。"""
        bid = self._routing.get(contact) or self._default_backend_id
        bcfg = self._backends_by_id.get(bid)
        if bcfg is not None and bcfg.type == "mailbox":
            return None, bcfg
        if self._agent is not None and (self._agent_injected or bcfg is None
                                        or bid == self._default_backend_id):
            return self._agent, bcfg
        if bcfg is None:
            return self._agent, None
        be = self._backend_cache.get(bid)
        if be is None:
            be = self._create_backend(bcfg)
            self._backend_cache[bid] = be
        return be, bcfg

    def _create_backend(self, bcfg: Any) -> Any:
        """由 backend 配置构造非信箱后端(echo|openai|webhook,经注册表工厂)。"""
        from wxbot.config import AgentConfig
        legacy = AgentConfig(
            mode=bcfg.type,
            base_url=bcfg.base_url,
            api_key=bcfg.api_key,
            model=bcfg.model,
            system_prompt=bcfg.system_prompt,
            timeout_sec=bcfg.timeout_sec,
            webhook_url=bcfg.webhook_url,
            webhook_secret=bcfg.webhook_secret,
            max_reply_chars=bcfg.max_reply_chars,
        )
        return create_agent(legacy)

    def _ensure_mailbox(self) -> Any | None:
        """信箱服务(注入优先,否则进程单例);失败只报一次并返回 None。"""
        if self._mailbox is not None:
            return self._mailbox
        if self._mailbox_failed:
            return None
        try:
            from wxbot.mailbox import get_mailbox_service
            self._mailbox = get_mailbox_service(
                retention_hours=self.cfg.mailbox.retention_hours,
                notify_url=self.cfg.mailbox.notify_url)   # §2.1 可选唤醒通知
        except Exception as e:
            self._mailbox_failed = True
            log.warning("信箱服务不可用(本进程不入信箱): %s", e)
        return self._mailbox

    def _next_dispatch_msg_id(self) -> int | None:
        """分配一个与信箱同源计数器的 msg_id(PROTOCOL §11 v1.1)。

        引擎分发消息给任何后端时统一编号:与信箱入件共用同一持久化计数器,
        全局唯一、单调递增、重启不回退(未入信箱的消息即跳号,§4 允许)。
        信箱服务不可用(磁盘故障等)→ 返回 None:消息对象/报文该字段为
        null,**绝不因编号失败阻塞分发**。"""
        try:
            svc = self._ensure_mailbox()
            if svc is None:
                return None
            return int(svc.allocate_msg_id())
        except Exception as e:
            log.debug("分配 msg_id 失败(按无编号处理): %s: %s",
                      type(e).__name__, e)
            return None

    def _tag_dispatch_ids(self, msg: Any, contact: str,
                          msg_id: int | None = None) -> None:
        """分发编号:消息对象附加 msg_id/chat_id(v1.1,duck-type 附加)。

        msg_id 缺省时从同源计数器分配;chat_id=联系人显示名(回复路由键)。
        附加失败(只读对象等)仅 DEBUG、绝不影响分发。消息对象此后进入
        ctx 上下文,作为 history 再分发给后端时编号随行(ContactContextStore
        存原对象引用)。"""
        try:
            if msg_id is None:
                msg_id = self._next_dispatch_msg_id()
            msg.chat_id = str(contact)
            if msg_id is not None:
                msg.msg_id = int(msg_id)
        except Exception as e:
            log.debug("消息对象附加 msg_id/chat_id 失败(忽略): %s: %s",
                      type(e).__name__, e)

    def _ctx_register_assistant(self, contact: str, text: str,
                                msg_id: int | None = None) -> None:
        """把本机回复登记进上下文(assistant 条目),统一携带 msg_id/chat_id。

        msg_id 优先用真实外发入信箱返回的编号(同一条消息全局一个编号);
        未提供(演练意图/外发入件失败)时从同源计数器补分配,保证 webhook
        history 每条均含 msg_id/v1.1;计数器不可用则为 None(报文 null)。"""
        if msg_id is None:
            msg_id = self._next_dispatch_msg_id()
        self.ctx.append_assistant(contact, text, msg_id=msg_id, chat_id=contact)

    def _mailbox_ingest(self, contact: str, msg: Any,
                        chat_type: str = "private") -> None:
        """来信入信箱(mailbox 路由):is_from_me=false,等 Muse 异步处理。"""
        svc = self._ensure_mailbox()
        if svc is None:
            self.emit("error", where="mailbox_ingest",
                      message="信箱服务不可用,消息未入信箱")
            return
        sender = str(getattr(msg, "sender", "") or "").strip() or contact
        text = str(getattr(msg, "text", "") or "")
        mid = svc.ingest(chat_id=contact, chat_type=chat_type, sender=sender,
                         text=text, is_from_me=False, msg_type="text")
        # v1.1:信箱路由的消息对象同样统一携带 msg_id/chat_id
        # (编号即信箱记录编号,与 Muse 轮询到的完全一致)
        self._tag_dispatch_ids(msg, contact, msg_id=mid)
        self._bump("mailbox_ingested")
        self.emit("mailbox_ingest", contact=contact, sender=sender,
                  text=text[:60], msg_id=mid, chat_type=chat_type)

    def _mailbox_ingest_outbound(self, contact: str, text: str) -> int | None:
        """外发消息(真实发送成功后)入信箱:is_from_me=true、sender=self。
        失败绝不影响发送主流程(信箱是旁路);成功返回 msg_id(供上下文
        assistant 条目复用同一编号),失败返回 None。"""
        try:
            svc = self._ensure_mailbox()
            if svc is None:
                return None
            chat_type = "group" if self.groups.is_managed(contact) else "private"
            mid = svc.ingest_outbound(chat_id=contact, text=text,
                                      chat_type=chat_type)
            self._bump("mailbox_outbound")
            return int(mid)
        except Exception as e:
            self.emit("error", where="mailbox_outbound", message=str(e))
            return None

    def _group_send_allowed(self, contact: str) -> bool:
        """群聊真实发送三闸门(2026-10-24):groups.enabled + 群∈groups.whitelist
        + groups.allow_real_send。默认(false)行为与旧「双层硬禁」一致。"""
        g = self.cfg.groups
        return bool(g.enabled and g.allow_real_send
                    and self.groups.is_managed(contact))

    def mailbox_deliver(self, chat_id: str, text: str) -> bool:
        """信箱回复投递回调(投递队列调用):与手动发送完全相同的闸门链。

        chat_id 即聊天窗口显示名(私聊=联系人,群聊=群名);监听范围已在
        API 层 403 把关,这里再经 _deliver 的 dry_run/allow_real_send/
        白名单/OCR 标题/群三闸门全链路。

        与本机发送的本质差异(require_real=True,PROTOCOL §2/§6):本方法
        返回 True 当且仅当消息**真的发出去了**(delivered)。dry_run、
        群三闸不全等「只记发送意图」分支对信箱投递一律视为失败:抛
        SafetyRejected → 投递队列计为失败并附中文原因,重试尽后终态
        failed,Muse 读到可重试或告警——绝不虚报 delivered 让 Muse 误
        推进 ack(§6:delivered 才等于「回复已成功送达」,ack 后消息
        归档不再重投;虚报 = 回复静默丢失)。"""
        return bool(self._deliver(chat_id, text, auto=False, require_real=True))

    def stop(self, timeout: float = 5.0) -> None:
        """优雅停止:置停机事件、哨兵唤醒工作线程、join、关闭 agent 连接。"""
        self._stop.set()
        try:
            self._queue.put_nowait(None)   # 哨兵:立即唤醒 get(timeout) 阻塞
        except queue.Full:
            pass
        for t in (self._thread_collector, self._thread_worker):
            if t is not None and t.is_alive():
                t.join(timeout=timeout)
        self._thread_collector = None
        self._thread_worker = None
        svc = self._mailbox
        if svc is not None:
            try:
                svc.stop()                     # 投递队列线程(幂等)
            except Exception:
                log.exception("停止信箱投递队列失败(忽略)")
        if self._agent is not None:
            closer = getattr(self._agent, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    log.exception("关闭 agent 连接失败(忽略)")
        self.emit("engine_stop")

    @property
    def running(self) -> bool:
        return bool(self._thread_worker is not None and self._thread_worker.is_alive())

    # ------------------------------------------------------------------ 采集线程
    def _collector_loop(self) -> None:
        log.info("采集线程启动: poll=%.1fs 白名单=%s",
                 self.cfg.engine.poll_interval_sec, self.gate.whitelist)
        while not self._stop.is_set():
            try:
                self._collect_once()
            except Exception as e:
                self._bump("errors")
                self._last_error = f"collector {type(e).__name__}: {e}"
                self.emit("error", where="collector", message=self._last_error,
                          trace=traceback.format_exc(limit=3))
                self._stop.wait(1.0)   # 防异常热循环
            self._stop.wait(self.cfg.engine.poll_interval_sec)
        log.info("采集线程退出")

    def _collect_once(self) -> None:
        flows = self._flows
        if flows is None:
            return
        unread = flows.check_unread() or []
        if not unread:
            return
        # 红线:check_unread ∩ 白名单 严格交集(名单匹配用去空格规范化)
        stripped = [(u or "").strip() for u in unread]
        named = [u for u in stripped if u]
        persons = [u for u in named if self.gate.is_whitelisted(u)]
        # 群聊(默认全关):collectible = enabled + 群名∈groups.whitelist +
        # reply_mode 非 off——不满足时连 open_chat 的键鼠交互都不会发生。
        # 同名冲突(某名字既是私聊白名单成员又是群名)时私聊优先,避免歧义。
        group_targets = [
            u for u in named
            if u not in persons and self.groups.collectible(u)
        ]
        skipped = [u for u in named if u not in persons and u not in group_targets]
        if skipped:
            self.emit("unread_skipped", contacts=skipped[:10])
        for contact in persons:
            if self._stop.is_set():
                return
            if not flows.open_chat(contact):
                self.emit("open_chat_failed", contact=contact)
                continue
            for msg in (flows.read_messages() or []):
                fp = fingerprint_of(contact, msg)
                if not self._seen.is_new(fp):
                    continue                     # 本轮运行已入队过(含自己消息)
                if is_own(msg):
                    self._bump("self_msg")
                    continue                     # 自己气泡不入队(state 排除规则)
                self._bump("seen")
                self.emit("inbox", contact=contact, sender=msg.sender,
                          text=str(msg.text)[:80])
                self._queue.put(("msg", contact, msg))
        for group in group_targets:
            if self._stop.is_set():
                return
            self._collect_group(flows, group)

    def _collect_group(self, flows: Any, group: str) -> None:
        """采集一个群的新消息(只读:open_chat + read_group_messages)。"""
        if not flows.open_chat(group):
            self.emit("open_chat_failed", contact=group, group=True)
            return
        reader = getattr(flows, "read_group_messages", None)
        msgs = reader() if callable(reader) else (flows.read_messages() or [])
        for msg in msgs:
            fp = fingerprint_of(group, msg)
            if not self._seen.is_new(fp):
                continue
            if is_own(msg):
                self._bump("self_msg")
                continue
            self._bump("seen")
            self.emit("inbox", contact=group, sender=msg.sender,
                      text=str(msg.text)[:80], group=True)
            self._queue.put(("group", group, msg))

    # ------------------------------------------------------------------ 工作线程
    def _worker_loop(self) -> None:
        log.info("工作线程启动 (auto_reply=%s dry_run=%s allow_real_send=%s)",
                 self.cfg.engine.auto_reply_enabled, self.gate.dry_run,
                 self.gate.allow_real_send)
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
            if item is None:                     # stop() 哨兵
                break
            kind, contact, msg = item
            try:
                if kind == "group":
                    self._handle_group(contact, msg)
                else:
                    self._handle(contact, msg)
            except Exception as e:
                self._bump("errors")
                self._last_error = f"worker {type(e).__name__}: {e}"
                self.emit("error", where="worker", contact=contact,
                          message=self._last_error,
                          trace=traceback.format_exc(limit=3))
        log.info("工作线程退出")

    def _handle_group(self, group: str, msg: Any) -> None:
        """群消息处理:去重→忽略词→群路由(默认全关)→[信箱路由]→auto开关
        →agent→限频→延迟→交付(_deliver 按三闸门校验,见其注释)。

        与私聊 _handle 的差异:白名单/发送闸之外多一道 GroupRouter
        (enabled + 群白名单 + reply_mode);不经 safety.gate 白名单
        (群名不在私聊名单里,语义不同,由群白名单把关)。"""
        if is_own(msg):
            self._bump("self_msg")
            return
        # ① 指纹去重(同私聊:联系人=群名,发送者=成员昵称,均参与指纹)
        fp = fingerprint_of(group, msg)
        if not self.cache.mark(fp):
            self._bump("deduped")
            self.emit("duplicate", contact=group, text=str(msg.text)[:60])
            return
        # ② 忽略关键词(系统提示/转账/红包等,与私聊同一份配置)
        for kw in (self.cfg.safety.ignore_keywords or []):
            if kw and kw in (msg.text or ""):
                self._bump("ignored")
                self.emit("ignored", contact=group, keyword=kw)
                return
        # ③ 群路由:enabled + 群白名单 + reply_mode 三重门,默认全关
        backend, bcfg = self._resolve_backend(group)
        if bcfg is not None and bcfg.type == "mailbox":
            # 信箱路由的群:Muse 负责回复,本机不调 agent;入信箱闸门 =
            # groups.enabled + 群∈groups.whitelist(监听范围,§9),不看
            # reply_mode(at/all 是本机同步回复的触发条件)
            if self.cfg.groups.enabled and self.groups.is_managed(group):
                self._mailbox_ingest(group, msg, chat_type="group")
            else:
                self._bump("group_ignored")
                self.emit("group_ignored", contact=group, reason="mailbox_scope",
                          text=str(msg.text)[:60])
            return
        ok, reason = self.groups.should_handle(group, msg)
        if not ok:
            self._bump("group_ignored")
            self.emit("group_ignored", contact=group, reason=reason,
                      text=str(msg.text)[:60])
            return
        # ④ 自动回复总开关(与私聊共用;默认 False)
        if not self.cfg.engine.auto_reply_enabled:
            self._bump("auto_disabled")
            self.emit("auto_reply_disabled", contact=group,
                      text=str(msg.text)[:60])
            return

        # ⑤ 上下文(每群独立)→ 调 agent(群名与成员昵称一并可见:
        # incoming.sender = 成员昵称)
        # v1.1 分发编号(同私聊 _handle ⑥):消息对象统一携带 msg_id/chat_id
        self._tag_dispatch_ids(msg, group)
        history = self.ctx.history(group)
        self.ctx.append_user(group, msg)
        agent = backend if backend is not None else self._agent
        self.emit("agent_call", contact=group, agent=agent.name(),
                  history_len=len(history), group=True)
        reply = agent.reply(group, history, msg)
        if not reply or not str(reply).strip():
            self._bump("agent_none")
            self.emit("agent_none", contact=group)
            return
        reply = str(reply).strip()
        max_chars = int(self.cfg.agent.max_reply_chars)
        if max_chars > 0 and len(reply) > max_chars:
            self.emit("reply_truncated", contact=group,
                      origin_len=len(reply), limit=max_chars)
            reply = reply[:max_chars]

        # ⑥ 限频(按群名限,防刷屏)
        try:
            self.gate.check_rate(group)
        except SafetyRejected as e:
            self._bump("rate_limited")
            self.emit("rate_limited", contact=group, reason=str(e))
            return

        # ⑦ 拟人随机延迟(与私聊一致)
        lo, hi = self.cfg.rate_limit.reply_delay_range_sec
        lo, hi = float(lo), float(hi)
        delay = random.uniform(lo, hi) if hi > lo else max(0.0, hi)
        if delay > 0:
            self.emit("reply_delay", contact=group, delay=round(delay, 2))
            if self._stop.wait(delay):
                return

        # ⑧ 交付(群强制演练:只记发送意图,绝不真实发送,见 _deliver)
        self._deliver(group, reply, auto=True)

    def _handle(self, contact: str, msg: Any) -> None:
        """单条消息处理:去重→白名单→忽略词→[信箱路由]→auto开关→agent
        →限频→延迟→发送。"""
        if is_own(msg):
            self._bump("self_msg")
            return
        # ① 指纹去重:取出即标记(同指纹只触发一次;agent 失败也不重复轰炸)
        fp = fingerprint_of(contact, msg)
        if not self.cache.mark(fp):
            self._bump("deduped")
            self.emit("duplicate", contact=contact, text=str(msg.text)[:60])
            return
        # ② 白名单严格校验(双保险:采集已过滤,这里兜底并记日志)
        try:
            self.gate.check_whitelist(contact)
        except SafetyRejected as e:
            self._bump("not_whitelisted")
            self.emit("rejected", reason="whitelist", contact=contact)
            return
        # ③ 忽略关键词(系统提示/转账/红包等,沿用旧版配置)
        for kw in (self.cfg.safety.ignore_keywords or []):
            if kw and kw in (msg.text or ""):
                self._bump("ignored")
                self.emit("ignored", contact=contact, keyword=kw)
                return
        # ④ 双模路由(2026-10-24):mailbox 后端 → 入信箱后返回,
        #    不同步回复、不调本机 agent(Muse 异步取信处理,PROTOCOL §11);
        #    注意:信箱入件不受 auto_reply_enabled 约束(回复者是 Muse)
        backend, bcfg = self._resolve_backend(contact)
        if bcfg is not None and bcfg.type == "mailbox":
            self._mailbox_ingest(contact, msg, chat_type="private")
            return
        # ⑤ 自动回复总开关(默认 False:只记录不回复)
        if not self.cfg.engine.auto_reply_enabled:
            self._bump("auto_disabled")
            self.emit("auto_reply_disabled", contact=contact,
                      text=str(msg.text)[:60])
            return

        # ⑥ 上下文快照 → 登记 → 调 agent(后端内部异常兜底返回 None)
        # v1.1 分发编号(PROTOCOL §11):分发给任何后端的消息对象统一携带
        # msg_id(与信箱同源计数器,全局唯一、持久化)与 chat_id(回复路由键)
        self._tag_dispatch_ids(msg, contact)
        history = self.ctx.history(contact)
        self.ctx.append_user(contact, msg)
        agent = backend if backend is not None else self._agent
        self.emit("agent_call", contact=contact, agent=agent.name(),
                  history_len=len(history))
        reply = agent.reply(contact, history, msg)
        if not reply or not str(reply).strip():
            self._bump("agent_none")
            self.emit("agent_none", contact=contact)
            return
        reply = str(reply).strip()
        max_chars = int(self.cfg.agent.max_reply_chars)
        if max_chars > 0 and len(reply) > max_chars:
            self.emit("reply_truncated", contact=contact,
                      origin_len=len(reply), limit=max_chars)
            reply = reply[:max_chars]

        # ⑥ 限频检查(agent 之后、发送之前)
        try:
            self.gate.check_rate(contact)
        except SafetyRejected as e:
            self._bump("rate_limited")
            self.emit("rate_limited", contact=contact, reason=str(e))
            return

        # ⑦ 拟人随机延迟(默认 1~3s,可被 stop 打断)
        lo, hi = self.cfg.rate_limit.reply_delay_range_sec
        lo, hi = float(lo), float(hi)
        delay = random.uniform(lo, hi) if hi > lo else max(0.0, hi)
        if delay > 0:
            self.emit("reply_delay", contact=contact, delay=round(delay, 2))
            if self._stop.wait(delay):
                return

        # ⑧ 发送(dry_run 记录意图 / 真实发送;发送闸拒绝计入 send_failed)
        try:
            self._deliver(contact, reply, auto=True)
        except SafetyRejected:
            self._bump("send_failed")   # _deliver 内已 emit safety_block
            return

    # ------------------------------------------------------------------ 发送
    def _deliver(self, contact: str, text: str, auto: bool,
                 require_real: bool = False) -> bool:
        """发送一条回复。dry_run → 记录意图;真实发送走 flows 全闸门。串行锁。

        群聊三闸门(2026-10-24,替代原「双层硬禁」):凡 groups.whitelist
        里的群名,真实发送必须同时满足 groups.enabled + 群∈群白名单 +
        groups.allow_real_send(默认 false)。三闸不全 → 一律只记录发送意图,
        绝不执行键鼠(与旧版行为完全一致);三闸全开仍需 dry_run=False 且
        allow_real_send=True,且 flows.send_message 群分支再按同一三闸校验、
        OCR 标题闸门兜底。非受管名字走真实路径时,flows.send_message 的
        闸门(白名单 + OCR 标题相等 + 群形态三闸判定)兜底:即使群名被误混入
        safety.whitelist、标题拆项成「群名」「(23)」两项,也会被拦
        (SafetyError → safety_block 事件)。

        require_real(2026-10-24 审查修复,信箱投递专用=True):「只记发送
        意图」的分支(群三闸不全 / dry_run)对信箱投递不算成功——消息从未
        发出,却返回 True 会让投递队列虚标 delivered(Muse 据此推进 ack,
        回复静默丢失,违反 PROTOCOL §2 状态机与 §6 ack 语义)。此模式下
        两分支改为抛 SafetyRejected(附中文原因),投递队列按失败处理,
        重试尽后终态 failed,Muse 可读原因后重试/告警。本机路径
        (auto 回复 / send_now)不受影响:演练模式记意图是既有行为。

        信箱旁路:真实发送成功后,本机外发消息同时入信箱(is_from_me=true,
        PROTOCOL §3);dry_run 意图不入信箱(消息并未真的发出)。"""
        with self._send_lock:
            if self.groups.is_managed(contact) and not self._group_send_allowed(contact):
                if require_real:
                    reason = (
                        f"群 {contact!r} 真实发送三闸门未全部满足(groups.enabled"
                        f" + 群∈groups.whitelist + groups.allow_real_send),"
                        f"群消息不会真实发送,信箱回复未投递")
                    self._bump("send_failed")
                    self.emit("safety_block", contact=contact, reason=reason)
                    raise SafetyRejected("group_send_disabled", reason)
                self._bump("send_intent")
                self.emit("send_intent", contact=contact, text=text, auto=auto,
                          note="群聊回复红线:只记录发送意图,不执行键鼠"
                               "(群真实发送三闸门未全部满足:groups.enabled + "
                               "群∈groups.whitelist + groups.allow_real_send)")
                log.info("[群演练] 将回复群 %s: %r(未执行键鼠)", contact, text)
                self._ctx_register_assistant(contact, text)
                return True
            try:
                mode = self.gate.send_mode()
            except SafetyRejected as e:
                self.emit("safety_block", contact=contact, reason=str(e))
                raise
            if mode == "dry_run":
                if require_real:
                    reason = (
                        "当前处于演练模式(engine.dry_run=True),信箱回复不会"
                        "真实发送;如需投递请取消演练模式并开启允许真实发送"
                        "(allow_real_send)")
                    self._bump("send_failed")
                    self.emit("safety_block", contact=contact, reason=reason)
                    raise SafetyRejected("dry_run", reason)
                self._bump("send_intent")
                self.emit("send_intent", contact=contact, text=text, auto=auto,
                          note="dry_run:已记录发送意图,未执行键鼠")
                log.info("[dry_run] 将发送给 %s: %r(未执行键鼠)", contact, text)
                self._ctx_register_assistant(contact, text)
                return True
            # 真实发送(需 dry_run=False 且 allow_real_send=True;群还需三闸门)
            flows = self._ensure_flows()   # 引擎未 start 时也可手动发送
            from wxbot.actions.flows import SafetyError  # 惰性导入,避免拖起键鼠
            try:
                if not flows.open_chat(contact):
                    self._bump("send_failed")
                    self.emit("send_failed", contact=contact, stage="open_chat")
                    return False
                ok = bool(flows.send_message(text, expect_contact=contact))
            except SafetyError as e:   # flows 白名单/OCR标题/群三闸门拦截
                self._bump("send_failed")
                self.emit("safety_block", contact=contact, reason=str(e))
                return False
            if ok:
                self._bump("sent")
                self.emit("sent", contact=contact, text=text, auto=auto)
                # 旁路:真实外发入信箱(编号即信箱记录编号),assistant 上下文
                # 条目复用同一编号(同一条消息全局一个 msg_id,v1.1)
                mid_out = self._mailbox_ingest_outbound(contact, text)
                self._ctx_register_assistant(contact, text, msg_id=mid_out)
                return True
            self._bump("send_failed")
            self.emit("send_failed", contact=contact, stage="send_message")
            return False

    def send_now(self, contact: str, text: str) -> bool:
        """手动发送(server 手动消息 API 复用入口)。

        与自动回复同一套 safety:白名单严格校验 → 限频 → 发送闸
        (dry_run 记录意图 / allow_real_send=false 拦截)。
        失败抛 SafetyRejected(带 code),由 server 转成 HTTP 错误。
        """
        text = (text or "").strip()
        if not text:
            raise SafetyRejected("empty_text", "发送内容为空")
        # 白名单严格校验:受管群三闸门全开时豁免私聊白名单(README §4.2:
        # 群名只入 groups.whitelist、绝不入联系人白名单;豁免后仍过 _deliver
        # 的三闸门 + flows 的 OCR 标题复核,fail-closed 层层兜底)
        if not self._group_send_allowed(contact):
            self.gate.check_whitelist(contact)     # 不在名单即拒并记日志
        self.gate.check_rate(contact)          # 手动发送同样计入限频(防刷屏)
        self.emit("send_now", contact=contact, text_len=len(text))
        return self._deliver(contact, text, auto=False)

    # ------------------------------------------------------------------ 状态
    def healthcheck(self) -> bool:
        """agent 后端健康度(WebUI「测通」;网络后端会发一次探测请求)。"""
        if self._agent is None:
            if self._default_is_mailbox():     # 信箱模式:本地存储可用即健康
                return self._ensure_mailbox() is not None
            return False
        try:
            return bool(self._agent.healthcheck())
        except Exception:
            log.exception("agent healthcheck 异常")
            return False

    def status(self) -> dict:
        """引擎状态快照(server /api/status 直接可用;不做网络调用)。"""
        with self._counter_lock:
            counters = dict(self.counters)
        mailbox_info: dict = {"available": self._mailbox is not None}
        if self._mailbox is not None:
            try:
                mailbox_info.update({
                    "max_msg_id": self._mailbox.store.max_msg_id,
                    "cursor": self._mailbox.store.confirmed_cursor,
                    "pending": self._mailbox.store.pending_count(),
                    "deliveries_pending": self._mailbox.queue.pending_total(),
                })
            except Exception as e:
                mailbox_info["error"] = str(e)
        agent_name = None
        if self._agent is not None:
            agent_name = self._agent.name()
        elif self._default_is_mailbox():
            agent_name = "mailbox"
        return {
            "running": self.running,
            "started_at": self._started_at,
            "uptime_sec": (time.time() - self._started_at) if self._started_at else 0.0,
            "agent": agent_name,
            "backends": {b.id: b.type for b in self._backends_by_id.values()},
            "default_backend": self._default_backend_id,
            "routing": dict(self._routing),
            "mailbox": mailbox_info,
            "whitelist": self.gate.whitelist,
            "groups": {
                "enabled": self.cfg.groups.enabled,
                "reply_mode": self.cfg.groups.reply_mode,
                "whitelist": self.groups.managed_groups,
                "bot_names": list(self.cfg.groups.bot_names or []),
            },
            "engine": {
                "poll_interval_sec": self.cfg.engine.poll_interval_sec,
                "dry_run": self.gate.dry_run,
                "allow_real_send": self.gate.allow_real_send,
                "auto_reply_enabled": self.cfg.engine.auto_reply_enabled,
                "context_window": self.cfg.engine.context_window,
                "max_replies_per_contact_per_min":
                    self.cfg.engine.max_replies_per_contact_per_min,
            },
            "queue_size": self._queue.qsize(),
            "counters": counters,
            "last_error": self._last_error,
            "recent_events": list(self.events)[-20:],
        }
