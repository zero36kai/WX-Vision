# -*- coding: utf-8 -*-
"""Muse 信箱协议 v1 离线验收(docs/PROTOCOL.md,12 项验收 + 配套)。

exit 0 = 通过(末行打印 MAILBOX_TEST_PASS);任一检查失败 exit 1。

红线:纯离线 stub——零微信交互、零键鼠、零真实网络(HTTP 仅走本机
ASGI/TestClient);config 与 data 全部用 tempfile,不污染真实配置。
新增测试一律用「测试联系人/测试群」等中性名,不出现任何真实联系人/群名。

覆盖(12 项 + 配套):
 ① 计数器单调持久(重载不回退)
 ② after=cursor 只取新消息(分页/has_more)
 ③ ack 后归档不再投递(history 仍可查;ack 幂等)
 ④ 未 ack 重投 + 按 msg_id 去重(编号稳定)
 ⑤ 回复路由:范围外 chat_id 403;范围内 202
 ⑥ 投递状态机 queued→delivering→delivered/failed + 重试3次指数退避 + 重启恢复
 ⑦ client_ref 幂等(相同 ref 只投递一次)
 ⑧ is_from_me 消息可取但不触发引擎处理(外发真实发送后入信箱)
 ⑨ routing:不同联系人走不同后端(mailbox 后端入信箱不同步回复)
 ⑩ 信箱端点 401(无 token);带 token 正常
 ⑪ 媒体 URL 可下载 PNG、非 base64(完整带 token)
 ⑫ 协议版本响应头 X-Mailbox-Protocol: 1
 13(配套)群发三闸门:默认关=只记意图;三闸全开+双开关=真实路径;
    flows 群分支三闸校验(关→SafetyError,开→放行;README 配置=群名仅入
    groups.whitelist 三闸全开亦可放行;标题无成员数后缀 fail-closed)
 14(2026-10-24 审查修复)信箱投递闸门不放行(dry_run/群三闸不全)→
    终态 failed 附中文原因,绝不虚标 delivered(PROTOCOL §2/§6)
 15(2026-10-24 v1.1 增补)notify 唤醒通知(§2.1):配置 notify_url 后新消息
    入信箱触发后台 POST(mock transport 离线)——被调用且 body 含 msg_id
    (与信箱同源、字段完整、3 秒超时);stop 收尾
 16(v1.1)notify 失败/超时完全忽略:绝不阻塞入信、消息仍在、编号照常;
    失败仅记 DEBUG 日志
 17(v1.1)msg_id 贯通旧 webhook 后端(§11):引擎分发消息给任何后端时
    消息对象统一携带 msg_id/chat_id(与信箱同源计数器,全局唯一、持久化);
    webhook 报文顶层与 history/incoming 每条均含 msg_id/chat_id
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
import traceback
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass
warnings.filterwarnings("ignore")

import httpx
import numpy as np
from fastapi.testclient import TestClient

import cv2

from wxbot.actions.flows import Message, SafetyError
from wxbot.agent.webhook import WebhookBackend
from wxbot.config import AgentBackendConfig, AppConfig, GroupsConfig
from wxbot.engine.monitor import MonitorEngine
from wxbot.mailbox import PROTOCOL_VERSION
from wxbot.mailbox.notify import NotifyDispatcher
from wxbot.mailbox.queue import DeliveryQueue
from wxbot.mailbox.service import MailboxService
from wxbot.mailbox.store import MailboxStore
from wxbot.server.events import EventBus
from wxbot.server.main import create_app

TOKEN = "mailbox-test-token-12345"
RESULTS: list[tuple[str, bool, str]] = []


def check(idx: str, desc: str, fn):
    print(f"\n[{idx}] {desc}")
    try:
        detail = fn() or ""
        print(f"    => PASS {detail}")
        RESULTS.append((idx, True, detail))
        return True
    except Exception as e:
        print(f"    => FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()
        RESULTS.append((idx, False, f"{type(e).__name__}: {e}"))
        return False


# ---------------------------------------------------------------- 测试替身
class StubEngine:
    """按 create_app 契约的最小引擎替身(信箱 API 测试用,零微信)。"""

    def start(self):
        pass

    def stop(self):
        pass

    def status(self):
        return {"running": False, "agent": {"mode": "echo"}}

    def send_now(self, contact: str, text: str):
        return True


class StubAgent:
    def __init__(self, reply="好的"):
        self._reply = reply
        self.calls: list[tuple[str, list, str]] = []

    def name(self):
        return "stub"

    def healthcheck(self):
        return True

    def reply(self, contact, history, incoming):
        self.calls.append((contact, [(m.sender, m.text) for m in history],
                           incoming.text))
        return self._reply


class StubFlows:
    """离线 flows 替身:记录调用,零键鼠/零窗口。"""

    def __init__(self):
        self.opened: list[str] = []
        self.sent: list[tuple[str, str]] = []

    def open_chat(self, name, force_search=False):
        self.opened.append(name)
        return True

    def read_messages(self):
        return []

    def send_message(self, text, expect_contact):
        self.sent.append((expect_contact, text))
        return True


def make_cfg(whitelist=("测试联系人",)) -> AppConfig:
    """安全测试配置:零延迟、高频限额、默认 dry_run、auto 开。"""
    cfg = AppConfig()
    cfg.engine.auto_reply_enabled = True
    cfg.engine.poll_interval_sec = 0.05
    cfg.rate_limit.reply_delay_range_sec = (0.0, 0.0)
    cfg.rate_limit.per_contact_cooldown_sec = 0.0
    cfg.rate_limit.global_qpm = 100
    cfg.safety.whitelist = list(whitelist)
    return cfg


def left(text, sender="测试联系人", ts="10:00"):
    return Message(sender=sender, text=text, side="left", ts=ts)


def make_client(td):
    """带口令的信箱 API 测试客户端(临时 config + 临时 data;不入 lifespan,
    投递队列不开工作线程 → 状态变化完全由测试驱动,结果确定)。"""
    td = Path(td)
    cp = td / "config.json"
    cp.write_text(json.dumps({
        "web": {"host": "127.0.0.1", "port": 8766, "password": TOKEN},
        "agent": {"mode": "echo"},
        "safety": {"whitelist": ["测试联系人"]},
        "groups": {"enabled": False, "reply_mode": "off", "whitelist": []},
        "allow_real_send": False,
    }, ensure_ascii=False), encoding="utf-8")
    svc = MailboxService(Path(td) / "data")
    app = create_app(engine=StubEngine(), config_path=cp,
                     bus=EventBus(buffer_size=20), window_finder=lambda: None,
                     enable_logging_bridge=False, mailbox=svc)
    return TestClient(app), svc


def auth_headers(token: str = TOKEN) -> dict:
    return {"X-Access-Token": token}


# ---------------------------------------------------------------- ①②③④ store
def test_counter_monotonic():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "mailbox_state.json"
        s1 = MailboxStore(p, retention_hours=48)
        i1 = s1.append("测试联系人", "private", "测试联系人", text="一")
        i2 = s1.append("测试联系人", "private", "测试联系人", text="二")
        assert (i1, i2) == (1, 2), (i1, i2)
        assert s1.max_msg_id == 2
        # 重载(模拟 PC 重启):计数器从磁盘恢复,继续递增不回退(§4)
        s2 = MailboxStore(p, retention_hours=48)
        assert s2.max_msg_id == 2 and s2.confirmed_cursor == 0
        i3 = s2.append("测试联系人", "private", "测试联系人", text="三")
        assert i3 == 3, f"重载后 msg_id 回退: {i3}"
        s3 = MailboxStore(p)
        assert s3.max_msg_id == 3
        # 空信箱 / 跳号语义:分配即计数,允许跳号(§4)
        assert MailboxStore(Path(td) / "none.json").max_msg_id == 0
    return "1,2,3 → 重载 → 4 号续编,计数器持久不回退"


def test_after_cursor():
    with tempfile.TemporaryDirectory() as td:
        s = MailboxStore(Path(td) / "st.json")
        for t in ("m1", "m2", "m3"):
            s.append("测试联系人", "private", "测试联系人", text=t)
        # after=1 → 只取 >1 的新消息,升序(§2/§4)
        page = s.messages_after(after=1, limit=50)
        assert [m["msg_id"] for m in page["messages"]] == [2, 3]
        assert page["next_cursor"] == 3 and page["has_more"] is False
        # after=3(已到底)→ 空
        page2 = s.messages_after(after=3)
        assert page2["messages"] == [] and page2["next_cursor"] == 3
        # 分页:limit=2 → [1,2] + has_more
        page3 = s.messages_after(after=0, limit=2)
        assert [m["msg_id"] for m in page3["messages"]] == [1, 2]
        assert page3["has_more"] is True and page3["next_cursor"] == 2
        # limit 收敛到 ≤50
        assert s.messages_after(after=0, limit=999)["messages"].__len__() == 3
    return "after=1→[2,3];after=3→空;limit 分页 + has_more 正确"


def test_ack_archive():
    with tempfile.TemporaryDirectory() as td:
        s = MailboxStore(Path(td) / "st.json")
        for t in ("m1", "m2", "m3"):
            s.append("测试联系人", "private", "测试联系人", text=t)
        # ack(2):≤2 归档,不再投递;未 ack 的 3 仍可取(§6)
        assert s.ack(2) == 2
        page = s.messages_after(after=0)
        assert [m["msg_id"] for m in page["messages"]] == [3]
        assert s.confirmed_cursor == 2 and s.pending_count() == 1
        # /history 仍可查到归档段(§2/§6)
        hist = s.history("测试联系人", limit=20)
        assert [m["msg_id"] for m in hist] == [1, 2, 3]
        assert hist[0]["archived"] is True and hist[2]["archived"] is False
        # ack 幂等:cursor ≤ 已确认值 → no-op 返回当前值(§6)
        assert s.ack(1) == 2 and s.ack(2) == 2
        # 重载恢复:游标与归档标记从磁盘恢复,不重发已确认段(§4)
        s2 = MailboxStore(Path(td) / "st.json")
        assert s2.confirmed_cursor == 2
        assert [m["msg_id"] for m in s2.messages_after(after=0)["messages"]] == [3]
        # 跳号安全:ack 超过现存最大编号,以编号比较为准
        assert s2.ack(999) == 999
        assert s2.messages_after(after=0)["messages"] == []
    return "ack 归档不再投递;history 含归档;幂等 no-op;重载恢复;跳号安全"


def test_redelivery_dedup():
    with tempfile.TemporaryDirectory() as td:
        s = MailboxStore(Path(td) / "st.json")
        s.append("测试联系人", "private", "测试联系人", text="a")
        s.append("测试联系人", "private", "测试联系人", text="b")
        # 未 ack:每次轮询都重投(§6/§8),msg_id 稳定 → Muse 按 msg_id 去重
        f1 = s.messages_after(after=0)
        f2 = s.messages_after(after=0)
        ids1 = [m["msg_id"] for m in f1["messages"]]
        ids2 = [m["msg_id"] for m in f2["messages"]]
        assert ids1 == ids2 == [1, 2], (ids1, ids2)
        assert len(set(ids2)) == len(ids2), "msg_id 必须唯一(去重唯一依据)"
        assert s.max_msg_id == 2, "重投不得新增/改号"
        # 重投期间新消息照常追加、编号继续
        i3 = s.append("测试联系人", "private", "测试联系人", text="c")
        assert i3 == 3
        ids3 = [m["msg_id"] for m in s.messages_after(after=0)["messages"]]
        assert ids3 == [1, 2, 3]
    return "未 ack 每次轮询重投且编号稳定([1,2]==[1,2]),msg_id 唯一可去重"


# ---------------------------------------------------------------- ⑤⑩⑪⑫ API
def test_reply_scope_403():
    with tempfile.TemporaryDirectory() as td:
        c, svc = make_client(td)
        # 范围外 → 403 拒收(§5:信箱回复永远不可能发到范围外会话)
        r = c.post("/api/mailbox/replies", headers=auth_headers(),
                   json={"chat_id": "范围外联系人", "reply_to_msg_id": 1,
                         "text": "你好", "client_ref": "muse-x1"})
        assert r.status_code == 403, (r.status_code, r.text)
        assert "error" in r.json() and "监听范围" in r.json()["error"], r.text
        assert svc.queue.pending_total() == 0, "拒收的回复不得入队"
        # 范围内 → 202 {delivery_id}(§2)
        ok = c.post("/api/mailbox/replies", headers=auth_headers(),
                    json={"chat_id": "测试联系人", "reply_to_msg_id": 1,
                          "text": "好的,已处理", "client_ref": "muse-x2"})
        assert ok.status_code == 202, (ok.status_code, ok.text)
        did = ok.json()["delivery_id"]
        assert did.startswith("d-"), did
        # 缺字段/空文本 → 400 中文错误(§2 错误约定)
        bad = c.post("/api/mailbox/replies", headers=auth_headers(),
                     json={"chat_id": "测试联系人", "text": "  "})
        assert bad.status_code == 400 and "error" in bad.json()
        # 状态查询可用
        st = c.get(f"/api/mailbox/deliveries/{did}", headers=auth_headers())
        assert st.status_code == 200 and st.json()["status"] in ("queued", "delivering")
        # 未知 delivery_id → 404
        assert c.get("/api/mailbox/deliveries/d-999999",
                     headers=auth_headers()).status_code == 404
    return "范围外 403 拒收不入队;范围内 202 {delivery_id};空文本 400;状态查询/404 正常"


def test_auth_401():
    with tempfile.TemporaryDirectory() as td:
        c, _svc = make_client(td)
        # 无 token → 401(§1:无/错 token → 401;中间件覆盖全部 /api/*)
        for path in ("/api/mailbox/status", "/api/mailbox/messages",
                     "/api/mailbox/config", "/api/media/m-abc"):
            r = c.get(path)
            assert r.status_code == 401, (path, r.status_code)
            assert "error" in r.json(), r.text
        # 错 token → 401
        assert c.get("/api/mailbox/status",
                     headers=auth_headers("wrong-token")).status_code == 401
        # 带 token(头)→ 200;兼容 ?token= 查询参数(§1)
        assert c.get("/api/mailbox/status", headers=auth_headers()).status_code == 200
        r = c.get(f"/api/mailbox/status?token={TOKEN}")
        assert r.status_code == 200, (r.status_code, r.text)
        # 写接口同样受保护
        assert c.post("/api/mailbox/ack", json={"cursor": 1}).status_code == 401
        assert c.post("/api/mailbox/replies",
                      json={"chat_id": "测试联系人", "text": "x"}).status_code == 401
    return "信箱端点无/错 token → 401;头与 ?token= 均可;写接口同受保护"


def test_media_url_png():
    with tempfile.TemporaryDirectory() as td:
        c, svc = make_client(td)
        # 生成真实 PNG(image = 气泡区截图裁剪,纯视觉边界 §3)
        img = np.full((30, 50, 3), (120, 200, 160), dtype=np.uint8)
        ok, buf = cv2.imencode(".png", img)
        assert ok
        mid = svc.media.save_bubble_png(img, box=(0, 0, 50, 30))
        assert mid.startswith("m-")
        svc.ingest(chat_id="测试联系人", chat_type="private", sender="测试联系人",
                   text="", msg_type="image", media_id=mid)
        page = c.get("/api/mailbox/messages", headers=auth_headers()).json()
        rec = next(m for m in page["messages"] if m["msg_type"] == "image")
        url = rec["media_url"]
        # 完整 URL + token;严禁 base64(§3 红线)
        assert url.startswith("http"), url
        assert "token=" in url and TOKEN in url, url
        assert not url.startswith("data:"), "media_url 禁止 base64"
        assert rec["text"] == ""
        # 可下载:URL 自带 token,直接 GET 即 200,返回 PNG 字节(魔数校验)
        dl = c.get("/api/media/" + url.split("/api/media/")[1])
        assert dl.status_code == 200, (dl.status_code, dl.text)
        body = dl.content
        assert body[:4] == b"\x89PNG", body[:8]
        assert dl.headers["content-type"] == "image/png"
        try:  # 双保险:确认字节是真 PNG 而非 base64 文本
            decoded = body.decode("utf-8")
            assert not decoded.lstrip().startswith("data:"), "不应是 base64 文本"
        except UnicodeDecodeError:
            pass                      # 二进制 PNG,正是预期
        # 非文本消息 voice/file:仅事件通知(text 说明),media_url 为 null
        svc.ingest(chat_id="测试联系人", chat_type="private", sender="测试联系人",
                   text="[事件]收到一条语音(纯视觉无法提取内容)", msg_type="voice")
        page2 = c.get("/api/mailbox/messages", headers=auth_headers()).json()
        v = next(m for m in page2["messages"] if m["msg_type"] == "voice")
        assert v["media_url"] is None and "语音" in v["text"]
        # 非法 media_id → 404(不泄露文件系统)
        assert c.get("/api/media/..%2Fconfig", headers=auth_headers()).status_code == 404
    return "media_url=完整带token URL、非base64;下载 200 且 PNG 魔数;voice 仅事件通知"


def test_protocol_header():
    with tempfile.TemporaryDirectory() as td:
        c, svc = make_client(td)
        r = c.get("/api/mailbox/status", headers=auth_headers())
        assert r.headers.get("x-mailbox-protocol") == str(PROTOCOL_VERSION), dict(r.headers)
        assert r.json()["protocol"] == PROTOCOL_VERSION
        # 消息/历史/媒体/错误响应一律带版本头(§1:所有响应)
        for path in ("/api/mailbox/messages", "/api/mailbox/history?chat_id=x",
                     "/api/mailbox/config", "/api/media/m-nope"):
            rr = c.get(path, headers=auth_headers())
            assert rr.headers.get("x-mailbox-protocol") == "1", (path, dict(rr.headers))
        rr = c.post("/api/mailbox/ack", headers=auth_headers(), json={"cursor": 0})
        assert rr.headers.get("x-mailbox-protocol") == "1"
    return "status/messages/history/config/media/ack 响应均带 X-Mailbox-Protocol: 1"


# ---------------------------------------------------------------- ⑥⑦ 投递队列
def test_delivery_state_machine():
    calls: list[str] = []
    during: list[str] = []
    clock = [1000.0]
    state: dict = {"did": [""]}

    def deliver(chat_id, text):
        calls.append(text)
        during.append(q2.status(state["did"][0])["status"])   # 投递中可见 delivering
        return len(calls) >= 3          # 前 2 次失败,第 3 次成功

    q2 = DeliveryQueue(deliver, max_retries=3, backoff_base_sec=0.5,
                       wall=lambda: clock[0], sleep=lambda s: None)
    did, _ = q2.submit("测试联系人", "合并后的连贯回复", reply_to_msg_id=7)
    state["did"][0] = did
    assert did.startswith("d-") and q2.status(did)["status"] == "queued"

    # 第 1 次:delivering(投递中可见)→ 失败 → 退回 queued(指数退避)
    assert q2.poll_once() is True
    st = q2.status(did)
    assert st["attempts"] == 1 and st["status"] == "queued" and st["reason"], st
    # 退避期内不处理(next_attempt 未到)
    clock[0] += 0.1
    assert q2.poll_once() is False
    # 第 2 次失败(退避 1s = 0.5×2^1)
    clock[0] += 1.0
    assert q2.poll_once() is True
    assert q2.status(did)["attempts"] == 2
    # 第 3 次成功 → delivered
    clock[0] += 2.0
    assert q2.poll_once() is True
    st = q2.status(did)
    assert st["status"] == "delivered" and st["attempts"] == 3, st
    assert st["reason"] is None
    assert during and all(s == "delivering" for s in during), during
    assert q2.poll_once() is False
    assert q2.depths() == {}, "终态后队列深度清零"

    # 一直失败 → 首投 + 3 次重试后 failed(附原因,§5/§8)
    clock2 = [5000.0]

    def always_fail(chat_id, text):
        raise RuntimeError("标题校验失败")

    q3 = DeliveryQueue(always_fail, max_retries=3, backoff_base_sec=0.5,
                       wall=lambda: clock2[0], sleep=lambda s: None)
    d2, _ = q3.submit("测试联系人", "必失败")
    for _ in range(4):
        clock2[0] += 10
        q3.poll_once()
    st2 = q3.status(d2)
    assert st2["status"] == "failed" and st2["attempts"] == 4, st2
    assert "标题校验失败" in (st2["reason"] or ""), st2
    assert q3.poll_once() is False, "failed 后不再投递"

    # 重启恢复:queued/delivering 复位为 queued 继续投;终态保留;幂等映射保留
    with tempfile.TemporaryDirectory() as td:
        clock3 = [100.0]
        pp = Path(td) / "deliveries.json"
        qa = DeliveryQueue(lambda c, t: t == "已完成", max_retries=3,
                           wall=lambda: clock3[0], sleep=lambda s: None,
                           persist_path=pp)
        da, _ = qa.submit("测试联系人", "重启恢复", client_ref="ref-recover")
        db, _ = qa.submit("测试联系人", "已完成", client_ref="ref-done")
        clock3[0] += 1
        qa.poll_once()                  # da(先创建先投)失败 → attempts=1 退避
        clock3[0] += 10
        qa.poll_once()                  # da 第二次失败 → attempts=2
        clock3[0] += 10
        qa.poll_once()                  # da 第三次失败
        assert qa.status(db)["status"] == "queued", "db 在 da 串行之后,尚未投递"
        # 串行性:同一 chat_id 前一条未完成,后一条不先投(da 反复失败期间 db 不动)
        # 新实例(模拟重启):未完成 → queued 继续投;delivered/failed 终态保留
        qb = DeliveryQueue(lambda c, t: True, max_retries=3,
                           wall=lambda: clock3[0], sleep=lambda s: None,
                           persist_path=pp)
        assert qb.status(da)["status"] == "queued"
        assert qb.status(db)["status"] == "queued"
        for _ in range(4):              # 恢复后全部投完(da 两次成功/db 一次)
            clock3[0] += 60
            qb.poll_once()
        clock3[0] += 60
        qb.poll_once()
        assert qb.status(da)["status"] == "delivered", qb.status(da)
        assert qb.status(db)["status"] == "delivered", qb.status(db)
        # client_ref 幂等映射跨重启保留
        again, dup = qb.submit("测试联系人", "重启恢复", client_ref="ref-recover")
        assert again == da and dup is True
    return "queued→delivering→delivered/failed;重试3次指数退避;同会话串行;重启恢复+幂等映射"


def test_client_ref_idempotent():
    calls: list[str] = []
    clock = [9000.0]
    q = DeliveryQueue(lambda c, t: (calls.append(t) or True),
                      max_retries=3, wall=lambda: clock[0], sleep=lambda s: None)
    d1, dup1 = q.submit("测试联系人", "回复A", client_ref="muse-abc")
    assert dup1 is False
    d2, dup2 = q.submit("测试联系人", "回复A", client_ref="muse-abc")
    assert d1 == d2 and dup2 is True, "相同 client_ref 重复提交应返回既有 id"
    q.poll_once()
    assert calls == ["回复A"], calls
    # 投递完成后同 ref 再提交 → 仍幂等(不二次投递,§8)
    d3, dup3 = q.submit("测试联系人", "回复A", client_ref="muse-abc")
    assert d3 == d1 and dup3 is True
    clock[0] += 10
    q.poll_once()
    assert calls == ["回复A"], "client_ref 相同只投递一次"
    # 不同 ref → 正常新投
    d4, dup4 = q.submit("测试联系人", "回复B", client_ref="muse-xyz")
    assert d4 != d1 and dup4 is False
    q.poll_once()
    assert calls == ["回复A", "回复B"]
    # 无 ref 提交各自独立(不去重)
    e1, _ = q.submit("测试联系人", "无ref-1")
    e2, _ = q.submit("测试联系人", "无ref-2")
    assert e1 != e2
    return "相同 client_ref 去重(投递前/后均幂等);不同 ref 正常;无 ref 互不影响"


# ---------------------------------------------------------------- ⑧⑨ 信箱×引擎
def test_outbound_ingested_not_processed():
    """⑧ 本机外发(is_from_me)入信箱可取,但绝不触发引擎处理(防死循环 §3/§8)。"""
    with tempfile.TemporaryDirectory() as td:
        svc = MailboxService(Path(td) / "data")
        cfg = make_cfg()
        cfg.engine.dry_run = False           # 双开关全开 → 真实路径(stub 承载)
        cfg.allow_real_send = True
        flows, agent = StubFlows(), StubAgent("拟回复")
        eng = MonitorEngine(cfg, flows_factory=lambda: flows, agent=agent,
                            mailbox=svc)
        # 手动外发 → 真实发送成功 → 同时入信箱(is_from_me=true, sender=self)
        assert eng.send_now("测试联系人", "手动外发消息") is True
        assert flows.sent == [("测试联系人", "手动外发消息")]
        assert eng.counters["sent"] == 1 and eng.counters["mailbox_outbound"] == 1
        # 自动回复外发同样入信箱
        eng._handle("测试联系人", left("自动回复触发"))
        assert eng.counters["sent"] == 2 and eng.counters["mailbox_outbound"] == 2

        # 信箱里可取到 is_from_me 记录(API 视角)
        cp = Path(td) / "config.json"
        cp.write_text(json.dumps({
            "web": {"password": TOKEN}, "safety": {"whitelist": ["测试联系人"]},
        }, ensure_ascii=False), encoding="utf-8")
        app = create_app(engine=StubEngine(), config_path=cp,
                         bus=EventBus(buffer_size=10), window_finder=lambda: None,
                         enable_logging_bridge=False, mailbox=svc)
        c = TestClient(app)
        page = c.get("/api/mailbox/messages", headers=auth_headers()).json()
        mine = [m for m in page["messages"] if m["is_from_me"]]
        assert len(mine) == 2, page
        assert all(m["sender"] == "self" for m in mine), mine

        # 引擎处理层面:自己的消息(self 气泡 / is_from_me)绝不触发回复
        before_calls = len(agent.calls)
        eng._handle("测试联系人", Message(sender="self", text="手动外发消息",
                                          side="right"))
        assert eng.counters["self_msg"] == 1
        assert len(agent.calls) == before_calls, "is_from_me 消息不得触发 agent"
        assert eng.counters["send_intent"] == 0
        assert eng.counters["mailbox_ingested"] == 0, "外发记录不得再次入信箱"
    return "真实外发(手动+自动)入信箱 is_from_me=true;API 可取;引擎零处理(防死循环)"


def test_routing_backends():
    """⑨ 双模路由:mailbox 后端的联系人入信箱不同步回复;其余联系人走各自后端。"""
    with tempfile.TemporaryDirectory() as td:
        svc = MailboxService(Path(td) / "data")
        cfg = make_cfg(whitelist=("测试联系人", "另一位测试联系人", "第三位测试联系人"))
        cfg.agent.backends = [
            AgentBackendConfig(id="echo-main", type="echo"),
            AgentBackendConfig(id="muse", type="mailbox"),
        ]
        cfg.agent.default_backend = "echo-main"
        cfg.agent.routing = {"测试联系人": "muse",
                             "另一位测试联系人": "echo-main"}
        flows, agent = StubFlows(), StubAgent("本地回复")
        eng = MonitorEngine(cfg, flows_factory=lambda: flows, agent=agent,
                            mailbox=svc)
        # 路由到 muse(信箱):入信箱、不调 agent、不发送(§11)
        eng._handle("测试联系人", left("帮我查天气", sender="测试联系人"))
        assert eng.counters["mailbox_ingested"] == 1
        assert all(c[0] != "测试联系人" for c in agent.calls), agent.calls
        assert eng.counters["send_intent"] == 0 and flows.sent == []
        page = svc.store.messages_after(after=0)
        assert [m["chat_id"] for m in page["messages"]] == ["测试联系人"]
        assert page["messages"][0]["is_from_me"] is False
        # 路由到 echo-main:本地后端同步回复(dry_run 意图)
        eng._handle("另一位测试联系人", left("在吗", sender="另一位测试联系人"))
        assert any(c[0] == "另一位测试联系人" for c in agent.calls)
        assert eng.counters["send_intent"] == 1
        assert svc.store.history("另一位测试联系人") == [], "非信箱路由不入信箱"
        # 未配置路由 → 默认后端 echo-main
        eng._handle("第三位测试联系人", left("默认路由", sender="第三位测试联系人"))
        assert any(c[0] == "第三位测试联系人" for c in agent.calls)
        assert eng.counters["send_intent"] == 2
        # 信箱联系人的第二条继续入信箱(编号递增)。v1.1 起计数器与引擎
        # 分发编号同源(echo 联系人的分发/演练意图也占用编号、允许跳号),
        # 故断言信箱内两条记录递增,不再断言全局计数器恰为 2
        eng._handle("测试联系人", left("第二条", sender="测试联系人"))
        assert eng.counters["mailbox_ingested"] == 2
        ids = [m["msg_id"] for m in svc.store.messages_after(after=0)["messages"]]
        assert len(ids) == 2 and ids[0] < ids[1], ids
    return "mailbox 路由→入信箱零同步回复;显式路由/默认后端各走各的;编号正常递增"


# ---------------------------------------------------------------- ⑬(配套)群发三闸门
class _StubWin:
    size = (896, 648)
    rect_screen = (0, 0, 896, 648)

    def ensure_active(self):
        return True

    def capture(self):
        return np.full((648, 896, 3), 255, dtype=np.uint8)


class _StubHi:
    def __init__(self):
        self.actions: list[str] = []

    def click(self, x, y):
        self.actions.append(f"click({x},{y})")

    def human_delay(self, lo, hi):
        pass

    def paste_text(self, text, restore_clipboard=True):
        self.actions.append(f"paste({text!r})")
        return True

    def press(self, *keys):
        self.actions.append(f"press({keys})")


def _ocr_item(text, x0, y0, x1, y1):
    from wxbot.vision.ocr import OcrItem
    return OcrItem(text=text, box=((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
                   center=((x0 + x1) // 2, (y0 + y1) // 2), conf=0.99)


def _group_scene():
    """合成 OCR:拆项群标题「测试群」「(23)」+ 发送按钮 + 自己右侧气泡。"""
    return [
        _ocr_item("测试群", 320, 45, 420, 66),
        _ocr_item("(23)", 430, 45, 470, 66),
        _ocr_item("发送", 815, 600, 850, 618),
        _ocr_item("测试回复", 680, 210, 780, 230),      # 右侧气泡(rel>0.53)
    ]


def _group_scene_no_member_suffix():
    """合成 OCR:标题只有群名、完全读不到成员数后缀(视觉上与私聊无法区分)。"""
    return [
        _ocr_item("测试群", 320, 45, 420, 66),
        _ocr_item("发送", 815, 600, 850, 618),
        _ocr_item("测试回复", 680, 210, 780, 230),
    ]


def test_group_three_gates():
    """⑬ 群发三闸门(默认关=现状;三闸全开+双开关=真实路径;flows 群分支)。

    配置对齐 README 4.2 指引:群名只入 groups.whitelist、**不**入联系人
    白名单(2026-10-24 审查指出旧测试把「测试群」放进 safety.whitelist,
    掩盖了 flows 护栏①与 README 矛盾的问题)。"""
    with tempfile.TemporaryDirectory() as td:
        svc = MailboxService(Path(td) / "data")

        # (a) 默认(groups.allow_real_send=False)+ 双开关全开 → 只记意图(现状)
        cfg = make_cfg()                     # 群名不入联系人白名单(README 4.2)
        cfg.groups.whitelist = ["测试群"]    # 测试群受管(在 groups.whitelist)
        cfg.engine.dry_run = False
        cfg.allow_real_send = True
        flows_a = StubFlows()
        eng_a = MonitorEngine(cfg, flows_factory=lambda: flows_a,
                              agent=StubAgent(), mailbox=svc)
        assert eng_a._deliver("测试群", "往群里发?", auto=True) is True
        assert ("测试群", "往群里发?") not in flows_a.sent
        intents = [e for e in eng_a.events if e["type"] == "send_intent"]
        assert intents and "三闸门" in intents[0]["note"], intents[0]
        assert eng_a.counters["mailbox_outbound"] == 0, "意图不入信箱(未真实外发)"

        # (b) 三闸全开(enabled + 群∈whitelist + allow_real_send)→ 真实路径
        cfg_b = make_cfg()                   # 同样不把群名放入联系人白名单
        cfg_b.engine.dry_run = False
        cfg_b.allow_real_send = True
        cfg_b.groups = GroupsConfig(enabled=True, reply_mode="all",
                                    whitelist=["测试群"], allow_real_send=True)
        flows_b = StubFlows()
        eng_b = MonitorEngine(cfg_b, flows_factory=lambda: flows_b,
                              agent=StubAgent(), mailbox=svc)
        assert eng_b._deliver("测试群", "三闸放行", auto=True) is True
        assert ("测试群", "三闸放行") in flows_b.sent, flows_b.sent
        assert eng_b.counters["sent"] == 1
        # 真实外发 → 入信箱(is_from_me),群聊 chat_type
        assert eng_b.counters["mailbox_outbound"] == 1
        rec = svc.store.history("测试群")[-1]
        assert rec["is_from_me"] is True and rec["chat_type"] == "group"
        # 三闸但缺一(enabled=False)→ 回到只记意图
        cfg_c = make_cfg()
        cfg_c.groups.whitelist = ["测试群"]  # 受管但 enabled=False → 三闸缺一
        cfg_c.engine.dry_run = False
        cfg_c.allow_real_send = True
        cfg_c.groups = GroupsConfig(enabled=False, reply_mode="all",
                                    whitelist=["测试群"], allow_real_send=True)
        flows_c = StubFlows()
        eng_c = MonitorEngine(cfg_c, flows_factory=lambda: flows_c,
                              agent=StubAgent(), mailbox=svc)
        assert eng_c._deliver("测试群", "x", auto=True) is True
        assert flows_c.sent == []

        # (d) flows 群分支:三闸关 → SafetyError 键鼠零执行;三闸开 → 放行
        from wxbot.actions.flows import WeChatFlows
        hi_d = _StubHi()
        flows_d = WeChatFlows(_StubWin(), hi_d,
                              lambda img, min_conf=0.5: _group_scene(),
                              whitelist=["测试联系人", "测试群"])   # 未接 groups_cfg
        try:
            flows_d.send_message("测试回复", "测试群")
            raise AssertionError("三闸关:群形态标题未被拦截")
        except SafetyError as e:
            assert "群聊形态" in str(e) and "三闸门" in str(e), str(e)
        assert hi_d.actions == [], "拦截时键鼠零执行"
        hi_e = _StubHi()
        flows_e = WeChatFlows(_StubWin(), hi_e,
                              lambda img, min_conf=0.5: _group_scene(),
                              whitelist=["测试联系人", "测试群"],
                              groups_cfg=GroupsConfig(enabled=True, reply_mode="all",
                                                      whitelist=["测试群"],
                                                      allow_real_send=True))
        assert flows_e.send_message("测试回复", "测试群") is True
        assert any(a.startswith("click") for a in hi_e.actions)
        # 群名不在 groups.whitelist(即使 allow_real_send=True)→ 仍拦
        hi_f = _StubHi()
        flows_f = WeChatFlows(_StubWin(), hi_f,
                              lambda img, min_conf=0.5: _group_scene(),
                              whitelist=["测试联系人", "测试群"],
                              groups_cfg=GroupsConfig(enabled=True, reply_mode="all",
                                                      whitelist=["别的群"],
                                                      allow_real_send=True))
        try:
            flows_f.send_message("测试回复", "测试群")
            raise AssertionError("群不在 groups.whitelist 未被拦截")
        except SafetyError:
            assert hi_f.actions == []

        # (e) README 4.2 指引配置:群名只入 groups.whitelist、不入联系人白名单
        #     + 三闸全开 → flows 凭三闸门放行(2026-10-24 审查修复:修复前
        #     被护栏①「不在白名单」拦死,README 承诺的能力按其指引不可达)
        hi_g = _StubHi()
        flows_g = WeChatFlows(_StubWin(), hi_g,
                              lambda img, min_conf=0.5: _group_scene(),
                              whitelist=["测试联系人"],
                              groups_cfg=GroupsConfig(enabled=True, reply_mode="all",
                                                      whitelist=["测试群"],
                                                      allow_real_send=True))
        assert flows_g.send_message("测试回复", "测试群") is True
        assert any(a.startswith("click") for a in hi_g.actions)
        # (f) 同 (e) 但标题读不到成员数后缀(视觉=私聊)→ fail-closed 拦截:
        #     绝不向「联系人白名单之外、疑似私聊」的会话发消息
        hi_h = _StubHi()
        flows_h = WeChatFlows(_StubWin(), hi_h,
                              lambda img, min_conf=0.5: _group_scene_no_member_suffix(),
                              whitelist=["测试联系人"],
                              groups_cfg=GroupsConfig(enabled=True, reply_mode="all",
                                                      whitelist=["测试群"],
                                                      allow_real_send=True))
        try:
            flows_h.send_message("测试回复", "测试群")
            raise AssertionError("无成员数后缀标题(疑似私聊)未被 fail-closed 拦截")
        except SafetyError as e:
            assert "无法区分" in str(e), str(e)
        assert hi_h.actions == [], "拦截时键鼠零执行"
    return ("默认关=只记意图(现状);三闸全开+双开关=真实路径且外发入信箱;"
            "缺任一闸回退演练;flows 群分支三闸校验(关→拦/开→放行/群不在名单→拦);"
            "README 配置(群名仅入群白名单)三闸全开可真实发送;无成员数后缀标题 fail-closed")


# ---------------------------------------------------------------- ⑭(审查修复)投递状态诚实性
def test_mailbox_delivery_never_fake_delivered():
    """⑭ 信箱投递闸门不放行 → 终态 failed 附中文原因,绝不虚标 delivered。

    PROTOCOL §2 状态机(delivered=已送达)+ §6 ack 语义(Muse 见 delivered
    才推进 ack,消息归档不再重投):修复前 dry_run / 群三闸不全的「只记发送
    意图」分支返回 True → 队列把从未发出的回复标成 delivered → Muse 误推进
    ack,回复静默丢失。修复后(require_real):这些场景抛 SafetyRejected,
    队列重试尽后 failed(附原因);闸门全开场景仍 delivered。"""
    from wxbot.engine.safety import SafetyRejected
    with tempfile.TemporaryDirectory() as td:
        # (a) 默认 dry_run=True(开箱默认配置):信箱回复不得 delivered
        svc_a = MailboxService(Path(td) / "a")
        cfg = make_cfg()
        flows_a = StubFlows()
        eng_a = MonitorEngine(cfg, flows_factory=lambda: flows_a,
                              agent=StubAgent(), mailbox=svc_a)
        clock = [1000.0]
        q_a = DeliveryQueue(eng_a.mailbox_deliver, max_retries=3,
                            backoff_base_sec=0.5, wall=lambda: clock[0],
                            sleep=lambda s: None,
                            persist_path=Path(td) / "a" / "q.json")
        d_a, _ = q_a.submit("测试联系人", "演练模式下的回复", reply_to_msg_id=1)
        for _ in range(4):                     # 首投 + 3 次重试(指数退避)
            clock[0] += 10
            q_a.poll_once()
        st_a = q_a.status(d_a)
        assert st_a["status"] == "failed", st_a
        assert "SafetyRejected" in (st_a["reason"] or "") \
            and "演练" in (st_a["reason"] or ""), st_a
        assert flows_a.sent == [], "演练模式绝不真实发送"
        assert eng_a.counters["send_intent"] == 0, \
            "信箱投递被闸门拦下时不得记本机发送意图"
        # 引擎层直调同样抛(队列的 failed 行为源于此)
        try:
            eng_a.mailbox_deliver("测试联系人", "直调")
            raise AssertionError("dry_run 下 mailbox_deliver 不应返回 True")
        except SafetyRejected as e:
            assert "演练" in str(e), e
        # 本机路径行为不变:auto/_deliver 演练仍记意图(既有语义)
        eng_a._deliver("测试联系人", "本机演练", auto=True)
        assert eng_a.counters["send_intent"] == 1

        # (b) 真实运行(dry_run=False + allow_real_send=True)+ 群三闸不全:
        #     群回复未发出,投递按失败处理并附三闸门原因
        svc_b = MailboxService(Path(td) / "b")
        cfg_b = make_cfg()
        cfg_b.engine.dry_run = False
        cfg_b.allow_real_send = True
        cfg_b.groups.whitelist = ["测试群"]    # 受管但 enabled=False → 三闸缺一
        flows_b = StubFlows()
        eng_b = MonitorEngine(cfg_b, flows_factory=lambda: flows_b,
                              agent=StubAgent(), mailbox=svc_b)
        clock_b = [2000.0]
        q_b = DeliveryQueue(eng_b.mailbox_deliver, max_retries=3,
                            backoff_base_sec=0.5, wall=lambda: clock_b[0],
                            sleep=lambda s: None,
                            persist_path=Path(td) / "b" / "q.json")
        d_b, _ = q_b.submit("测试群", "群回复未发", reply_to_msg_id=2)
        for _ in range(4):
            clock_b[0] += 10
            q_b.poll_once()
        st_b = q_b.status(d_b)
        assert st_b["status"] == "failed", st_b
        assert "三闸门" in (st_b["reason"] or ""), st_b
        assert flows_b.sent == [], "三闸不全绝不真实发送"
        assert eng_b.counters["send_intent"] == 0

        # (c) 闸门全开(私聊、真实路径):delivered 且真的发出(stub 承载)
        svc_c = MailboxService(Path(td) / "c")
        cfg_c = make_cfg()
        cfg_c.engine.dry_run = False
        cfg_c.allow_real_send = True
        flows_c = StubFlows()
        eng_c = MonitorEngine(cfg_c, flows_factory=lambda: flows_c,
                              agent=StubAgent(), mailbox=svc_c)
        clock_c = [3000.0]
        q_c = DeliveryQueue(eng_c.mailbox_deliver, max_retries=3,
                            backoff_base_sec=0.5, wall=lambda: clock_c[0],
                            sleep=lambda s: None,
                            persist_path=Path(td) / "c" / "q.json")
        d_c, _ = q_c.submit("测试联系人", "真实回复", reply_to_msg_id=3)
        clock_c[0] += 10
        q_c.poll_once()
        st_c = q_c.status(d_c)
        assert st_c["status"] == "delivered" and st_c["reason"] is None, st_c
        assert flows_c.sent == [("测试联系人", "真实回复")]
        assert eng_c.counters["mailbox_outbound"] == 1, "真实外发应入信箱(is_from_me)"
    return ("dry_run / 群三闸不全 → 队列终态 failed 附中文原因,绝不 delivered;"
            "闸门全开 → delivered 且真实发出;本机演练意图行为不变")


# ---------------------------------------------------------------- ⑮⑯⑰(v1.1 增补)
class _RecordingTransport:
    """离线 mock transport:记录 notify POST(零网络);可注入失败/超时。"""

    def __init__(self, error: Exception | None = None):
        self.calls: list[tuple[str, dict, float]] = []
        self.error = error

    def __call__(self, url: str, payload: dict, timeout_sec: float) -> None:
        self.calls.append((url, payload, timeout_sec))
        if self.error is not None:
            raise self.error


def _wait_until(fn, timeout: float = 2.0, step: float = 0.01) -> bool:
    """轮询等待后台线程副作用(notify worker);超时返回末次判定。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(step)
    return bool(fn())


def test_notify_wakeup():
    """⑮ v1.1 §2.1:配置 mailbox.notify_url 后,新消息入信箱由后台线程
    尽力而为 POST 该 URL——mock transport 离线验证 notify 被调用、
    body 含 msg_id(与信箱编号一致)且字段完整、超时 3 秒、stop 收尾。"""
    tr = _RecordingTransport()
    with tempfile.TemporaryDirectory() as td:
        svc = MailboxService(
            Path(td) / "data",
            notify_dispatcher=NotifyDispatcher("http://127.0.0.1:9/muse-knock",
                                               transport=tr))
        # ① 来信入信箱 → notify 被调用,body=该消息完整字段含 msg_id
        mid = svc.ingest(chat_id="文件传输助手", chat_type="private",
                         sender="文件传输助手", text="记:明天上午十点例会")
        assert mid == 1, mid
        assert _wait_until(lambda: len(tr.calls) == 1), "notify 未被调用"
        url, body, timeout = tr.calls[0]
        assert url == "http://127.0.0.1:9/muse-knock", url
        assert timeout == 3.0, timeout                    # §2.1 短超时 3 秒
        assert body["msg_id"] == mid, body                # 编号与信箱一致
        assert body["chat_id"] == "文件传输助手"
        assert body["chat_type"] == "private"
        assert body["sender"] == "文件传输助手"
        assert body["is_from_me"] is False
        assert body["msg_type"] == "text"
        assert body["text"] == "记:明天上午十点例会"
        assert body["ts"], body                           # ISO 时间在
        # 第二条:同源计数器递增,notify 逐条跟随且编号一致
        mid2 = svc.ingest(chat_id="文件传输助手", chat_type="private",
                          sender="文件传输助手", text="下午三点提醒我取快递")
        assert mid2 == 2
        assert _wait_until(lambda: len(tr.calls) == 2)
        assert tr.calls[1][1]["msg_id"] == mid2, tr.calls[1][1]
        # 外发(is_from_me=true)同样通知,字段完整
        svc.ingest_outbound("文件传输助手", "好的,已记下")
        assert _wait_until(lambda: len(tr.calls) == 3)
        assert tr.calls[2][1]["is_from_me"] is True
        assert tr.calls[2][1]["sender"] == "self"
        assert tr.calls[2][1]["msg_id"] == 3
        # 进程退出收尾:stop 停 worker、注销 atexit(线程不再存活)
        svc.stop()
        assert svc.notify._thread is None, "stop 后 notify 工作线程应已收尾"
    return "notify 被调用且 body 含 msg_id(与信箱同源、字段完整、3 秒超时);stop 收尾"


def test_notify_failure_ignored():
    """⑯ v1.1 §2.1:notify 失败/超时完全忽略——绝不阻塞入信,消息仍在信箱、
    编号照常推进;失败仅记 DEBUG 日志(无 WARNING 及以上噪声)。"""
    captured: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    tr = _RecordingTransport(error=TimeoutError("notify 等待超时(模拟)"))
    nlog = logging.getLogger("wxbot.mailbox.notify")
    cap = _Cap(level=logging.DEBUG)
    nlog.addHandler(cap)
    old_level = nlog.level
    nlog.setLevel(logging.DEBUG)
    try:
        with tempfile.TemporaryDirectory() as td:
            svc = MailboxService(
                Path(td) / "data",
                notify_dispatcher=NotifyDispatcher("http://127.0.0.1:9/muse-knock",
                                                   transport=tr))
            # 超时失败:入信照常成功,消息仍在信箱(尽力而为,绝不阻塞入信)
            mid = svc.ingest(chat_id="文件传输助手", chat_type="private",
                             sender="文件传输助手", text="记:下午三点取快递")
            assert mid == 1, mid
            assert _wait_until(lambda: len(tr.calls) == 1), "失败 transport 未被调用"
            page = svc.store.messages_after(after=0)
            assert [m["msg_id"] for m in page["messages"]] == [1], page
            assert page["messages"][0]["text"] == "记:下午三点取快递"
            # 连续入信不受先前失败影响,编号照常推进
            mid2 = svc.ingest(chat_id="文件传输助手", chat_type="private",
                              sender="文件传输助手", text="晚饭想吃什么")
            assert mid2 == 2 and svc.store.pending_count() == 2, (mid2,)
            assert _wait_until(lambda: len(tr.calls) == 2)
            # 换成一般性异常(连接被拒):同样只忽略
            tr.error = RuntimeError("连接被拒(模拟)")
            mid3 = svc.ingest(chat_id="文件传输助手", chat_type="private",
                              sender="文件传输助手", text="周五晚上一起吃饭吗")
            assert mid3 == 3 and svc.store.pending_count() == 3
            assert _wait_until(lambda: len(tr.calls) == 3)
            assert [m["msg_id"] for m in
                    svc.store.messages_after(after=0)["messages"]] == [1, 2, 3]
            # 失败仅 DEBUG 日志:有 DEBUG 记录,且无 WARNING/ERROR 噪声
            assert _wait_until(lambda: any(
                r.levelno == logging.DEBUG and "notify" in r.getMessage()
                for r in captured)), "notify 失败未记 DEBUG 日志"
            noisy = [r for r in captured if r.levelno >= logging.WARNING]
            assert not noisy, [(r.levelname, r.getMessage()) for r in noisy]
            svc.stop()
    finally:
        nlog.removeHandler(cap)
        nlog.setLevel(old_level)
    return "notify 失败/超时仅 DEBUG 日志;入信不受影响(消息仍在、编号照常)"


def test_webhook_payload_msg_id_chat_id():
    """⑰ v1.1 §11:msg_id 贯通旧 webhook 后端——引擎分发消息给任何后端时,
    消息对象统一携带 msg_id/chat_id(与信箱同源计数器,全局唯一、持久化);
    webhook 报文顶层与 history/incoming 每条均含 msg_id 与 chat_id。"""
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content.decode("utf-8")))
        return httpx.Response(200, json={"reply": None})   # 主动沉默,纯观察报文

    with tempfile.TemporaryDirectory() as td:
        svc = MailboxService(Path(td) / "data")
        cfg = make_cfg(whitelist=("文件传输助手", "另一位联系人"))
        cfg.agent.backends = [
            AgentBackendConfig(id="hook", type="webhook",
                               webhook_url="http://hook.local/callback"),
            AgentBackendConfig(id="muse", type="mailbox"),
        ]
        cfg.agent.default_backend = "hook"
        cfg.agent.routing = {"文件传输助手": "hook", "另一位联系人": "muse"}
        agent = WebhookBackend(
            "http://hook.local/callback",
            client=httpx.Client(transport=httpx.MockTransport(handler)))
        eng = MonitorEngine(cfg, flows_factory=lambda: StubFlows(),
                            agent=agent, mailbox=svc)
        # 第 1 条(webhook 路由):分发即编号,对象与报文均携带
        m1 = left("记:明天上午十点例会", sender="文件传输助手")
        eng._handle("文件传输助手", m1)
        assert len(captured) == 1, captured
        p1 = captured[0]
        # 消息对象统一携带 msg_id/chat_id
        assert getattr(m1, "msg_id", None) == 1, getattr(m1, "msg_id", None)
        assert getattr(m1, "chat_id", None) == "文件传输助手"
        # 报文顶层含 msg_id 与 chat_id(顶层 msg_id=incoming 编号)
        assert p1["msg_id"] == 1 and p1["chat_id"] == "文件传输助手", p1
        assert p1["incoming"]["msg_id"] == 1
        assert p1["incoming"]["chat_id"] == "文件传输助手"
        assert p1["incoming"]["text"] == "记:明天上午十点例会"
        assert p1["history"] == []                       # 首条无上下文
        assert eng.counters["send_intent"] == 0          # 对端沉默 → 不发送
        # 第 2 条:编号唯一递增;history 随上下文回流且每条均含 msg_id/chat_id
        m2 = left("下午三点提醒我取快递", sender="文件传输助手")
        eng._handle("文件传输助手", m2)
        assert len(captured) == 2
        p2 = captured[1]
        assert p2["msg_id"] == p2["incoming"]["msg_id"] == 2, p2
        assert p2["incoming"]["chat_id"] == "文件传输助手"
        assert len(p2["history"]) == 1, p2["history"]
        assert p2["history"][0]["msg_id"] == p1["msg_id"], p2["history"]
        assert p2["history"][0]["chat_id"] == "文件传输助手"
        # 第 3 条(mailbox 路由):入信箱编号紧接分发编号之后——同源计数器
        m3 = left("在吗", sender="另一位联系人")
        eng._handle("另一位联系人", m3)
        assert eng.counters["mailbox_ingested"] == 1
        page = svc.store.messages_after(after=0)
        rec = page["messages"][-1]
        assert rec["msg_id"] == 3 and rec["chat_id"] == "另一位联系人", rec
        # 信箱路由的消息对象同样携带与信箱记录一致的 msg_id/chat_id
        assert getattr(m3, "msg_id", None) == 3
        assert getattr(m3, "chat_id", None) == "另一位联系人"
        # 持久化:同源计数器落盘,重开不回退(§4 重启不回退)
        s2 = MailboxStore(Path(td) / "data" / "mailbox_state.json")
        assert s2.max_msg_id == 3, s2.max_msg_id
        eng.stop()
    return ("webhook 报文顶层与 history/incoming 均含 msg_id/chat_id;消息对象"
            "分发即编号;信箱同源计数器全局唯一递增且持久化")


# ---------------------------------------------------------------- 汇总
def main() -> int:
    check("①", "计数器单调持久(重载不回退)", test_counter_monotonic)
    check("②", "after=cursor 只取新消息(分页/has_more)", test_after_cursor)
    check("③", "ack 后归档不再投递(history 可查;幂等;重载恢复)", test_ack_archive)
    check("④", "未 ack 重投 + msg_id 稳定去重", test_redelivery_dedup)
    check("⑤", "回复路由:范围外 chat_id 403;范围内 202", test_reply_scope_403)
    check("⑥", "投递状态机+重试3次指数退避+failed+重启恢复", test_delivery_state_machine)
    check("⑦", "client_ref 幂等(重复提交只投一次)", test_client_ref_idempotent)
    check("⑧", "is_from_me 可取但不触发引擎处理(外发入信箱)", test_outbound_ingested_not_processed)
    check("⑨", "routing:不同联系人走不同后端(mailbox 入信箱)", test_routing_backends)
    check("⑩", "信箱端点 401(无 token);?token= 兼容", test_auth_401)
    check("⑪", "媒体 URL 可下载 PNG、非 base64;voice 仅事件通知", test_media_url_png)
    check("⑫", "协议版本响应头 X-Mailbox-Protocol: 1", test_protocol_header)
    check("⑬", "群发三闸门(默认关=现状;全开=真实;flows 群分支)", test_group_three_gates)
    check("⑭", "信箱投递闸门不放行→failed 附原因,绝不虚标 delivered",
          test_mailbox_delivery_never_fake_delivered)
    check("⑮", "v1.1 §2.1 notify:被调用且 body 含 msg_id(同源/3 秒超时/收尾)",
          test_notify_wakeup)
    check("⑯", "v1.1 §2.1 notify 失败/超时:仅 DEBUG,绝不阻塞入信",
          test_notify_failure_ignored)
    check("⑰", "v1.1 §11 msg_id 贯通 webhook:报文/对象携带,同源计数器持久化",
          test_webhook_payload_msg_id_chat_id)

    failed = [r for r in RESULTS if not r[1]]
    if failed:
        print(f"\n失败项: {[i for i, _, _ in failed]}")
        print("MAILBOX_TEST_FAIL")
        return 1
    print("\n全部检查通过:纯离线 stub,零微信交互、零键鼠、零真实网络。")
    print("MAILBOX_TEST_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
