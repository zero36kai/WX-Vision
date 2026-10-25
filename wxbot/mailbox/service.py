# -*- coding: utf-8 -*-
"""信箱服务门面:store + queue + media + notify 四件套的进程内单例装配。

双模路由与 server 共用**同一个** MailboxService 实例(按 data 目录单例):
- 引擎侧:mailbox 路由的会话 ingest() 入信箱(is_from_me=false);
  本机一切外发成功后 ingest_outbound()(is_from_me=true);
  allocate_msg_id() 供引擎分发编号(与信箱同源计数器,PROTOCOL §11 v1.1);
  并把真实投递函数(与手动发送同一闸门链)注册给投递队列;
- server 侧:register_mailbox_routes(app, service) 暴露 PROTOCOL §2 的
  8 个端点,media_url 用当前访问口令现生成;
- notify(§2.1):配置 mailbox.notify_url 后,新消息入信箱由后台线程
  尽力而为 POST 唤醒通知(3 秒超时、失败仅 DEBUG、绝不阻塞入信);
  MailboxService.stop() 收尾(进程退出/服务关闭调用)。

默认 data 目录 = 项目根/data(.gitignore 已排除:含聊天内容,严禁入库)。
构造不建目录不写盘——首次真正写入时才落盘,离线测试零副作用。
"""
from __future__ import annotations

import threading
from pathlib import Path

from wxbot.mailbox.media import MediaStore
from wxbot.mailbox.notify import NotifyDispatcher
from wxbot.mailbox.queue import DeliveryQueue
from wxbot.mailbox.store import DEFAULT_RETENTION_HOURS, MailboxStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

_STATE_FILENAME = "mailbox_state.json"
_QUEUE_FILENAME = "mailbox_deliveries.json"
_MEDIA_DIRNAME = "media"

_INSTANCES: dict[str, "MailboxService"] = {}
_INSTANCES_LOCK = threading.Lock()


class MailboxService:
    """store + queue + media + notify 门面(线程安全;各组件自带锁)。"""

    def __init__(self, data_dir: str | Path = DEFAULT_DATA_DIR,
                 *, retention_hours: int = DEFAULT_RETENTION_HOURS,
                 notify_url: str = "",
                 notify_dispatcher: NotifyDispatcher | None = None):
        data_dir = Path(data_dir)
        self.store = MailboxStore(
            data_dir / _STATE_FILENAME,
            retention_hours=retention_hours if retention_hours and int(retention_hours) > 0
            else DEFAULT_RETENTION_HOURS,
        )
        self.media = MediaStore(data_dir / _MEDIA_DIRNAME)
        self.queue = DeliveryQueue(persist_path=data_dir / _QUEUE_FILENAME)
        # §2.1 入信唤醒通知:注入的 dispatcher 优先(离线测试注入 mock
        # transport);否则按 notify_url 构造(空 = 不通知,零行为)。
        self.notify = (notify_dispatcher if notify_dispatcher is not None
                       else NotifyDispatcher(notify_url))
        # 新消息成功落盘 → 旁路通知(入队即返回,绝不阻塞入信,§2.1)
        self.store.set_on_append(self.notify.notify)

    # ------------------------------------------------------------------ 入件
    def ingest(
        self,
        *,
        chat_id: str,
        chat_type: str = "private",
        sender: str,
        text: str = "",
        is_from_me: bool = False,
        msg_type: str = "text",
        media_id: str | None = None,
        ts: str | None = None,
    ) -> int:
        """入信箱一条消息,返回 msg_id。失败向上抛(调用方决定是否吞掉)。"""
        return self.store.append(
            chat_id, chat_type, sender,
            is_from_me=is_from_me, msg_type=msg_type, text=text,
            media_id=media_id, ts=ts)

    def ingest_outbound(self, chat_id: str, text: str,
                        chat_type: str = "private") -> int:
        """本机外发消息(手动/任何后端自动回复)入信箱:is_from_me=true、
        sender="self"(PROTOCOL §3:Muse 一律忽略、不触发处理,仅作上下文)。"""
        return self.ingest(chat_id=chat_id, chat_type=chat_type, sender="self",
                           text=text, is_from_me=True, msg_type="text")

    # ------------------------------------------------------------------ 编号
    def allocate_msg_id(self) -> int:
        """分配一个与信箱**同源计数器**的 msg_id(不产生信箱记录)。

        PROTOCOL §11(v1.1):引擎分发消息给任何后端时,消息对象统一携带
        msg_id/chat_id——编号与信箱入件共用同一持久化计数器,全局唯一、
        单调递增、重启不回退;未入信箱的消息即跳号(§4 允许跳号)。"""
        return self.store.next_msg_id()

    # ------------------------------------------------------------------ 投递
    def set_deliver_fn(self, fn) -> None:
        """注册真实投递函数(引擎的 mailbox_deliver:同一套安全闸门)。"""
        self.queue.set_deliver_fn(fn)

    def start(self) -> None:
        """启动投递队列工作线程(幂等)。"""
        self.queue.start()

    def stop(self) -> None:
        """停止后台线程并收尾(投递队列 + 唤醒通知;进程退出/服务关闭调用)。"""
        self.queue.stop()
        try:
            self.notify.stop()             # §2.1 收尾:停 worker、关传输资源
        except Exception:
            pass


def get_mailbox_service(
    data_dir: str | Path | None = None,
    *,
    retention_hours: int | None = None,
    notify_url: str | None = None,
) -> MailboxService:
    """按 data 目录取进程内单例(引擎与 server 必须共享同一份状态)。

    notify_url(§2.1 唤醒通知):None = 不改动既有实例;非 None(含空串)
    = 以本次传入为准(配置文件是权威来源,既有实例随之更新)。"""
    key = str(Path(data_dir).resolve()) if data_dir else str(DEFAULT_DATA_DIR)
    with _INSTANCES_LOCK:
        svc = _INSTANCES.get(key)
        if svc is None:
            kw = {}
            if retention_hours and int(retention_hours) > 0:
                kw["retention_hours"] = int(retention_hours)
            if notify_url is not None:
                kw["notify_url"] = str(notify_url)
            svc = MailboxService(key, **kw)
            _INSTANCES[key] = svc
        elif notify_url is not None:
            svc.notify.set_url(str(notify_url))
        return svc
