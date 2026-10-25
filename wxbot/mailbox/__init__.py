# -*- coding: utf-8 -*-
"""Muse 信箱协议 v1 实现(wxbot.mailbox,docs/PROTOCOL.md)。

组成:
- store.py  消息存储 + 单调计数器 + ack 游标 + 保留期清理(data/mailbox_state.json);
- queue.py  回复投递队列(chat_id 串行 / 状态机 / 重试3次指数退避 / client_ref 幂等);
- media.py  非文本媒体落盘(data/media/,image=气泡裁剪 PNG;voice/file 仅事件通知);
- notify.py 入信唤醒通知(§2.1 可选 mailbox.notify_url:后台线程尽力而为
  POST、3 秒超时、失败仅 DEBUG、绝不阻塞入信);
- service.py 四件套门面 + 进程内单例(引擎与 server 共享);
- api.py    PROTOCOL §2 的 8 个端点(仅 server/main.py 导入,本包 __init__
  不引入 fastapi,保持 `import wxbot.mailbox` 轻量、引擎可离线使用)。

红线(PROTOCOL §1b / ARCHITECTURE §0):
- 信箱回复只投递到监听范围内的会话(API 层 403 兜底);
- is_from_me=true 的消息仅作上下文,绝不触发任何处理;
- 未 ack 的消息绝不删除;本包全部离线可测,零微信交互。
"""
from wxbot.mailbox.store import MailboxStore  # noqa: F401
from wxbot.mailbox.queue import DeliveryQueue  # noqa: F401
from wxbot.mailbox.media import MediaStore, MEDIA_ID_RE  # noqa: F401
from wxbot.mailbox.notify import NotifyDispatcher  # noqa: F401
from wxbot.mailbox.service import (  # noqa: F401
    MailboxService,
    get_mailbox_service,
)

PROTOCOL_VERSION = 1

__all__ = [
    "MailboxStore", "DeliveryQueue", "MediaStore", "MEDIA_ID_RE",
    "NotifyDispatcher", "MailboxService", "get_mailbox_service",
    "PROTOCOL_VERSION",
]
