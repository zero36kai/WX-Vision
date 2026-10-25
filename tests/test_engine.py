# -*- coding: utf-8 -*-
"""引擎离线验收脚本(ARCHITECTURE §6 验收标准)。

exit 0 = 通过(末行打印 ENGINE_TEST_PASS);任一检查失败 exit 1。

红线:纯离线测试——stub flows / stub agent 全程注入,绝不查找/触碰微信
窗口、绝不执行键鼠、绝不发起真实网络请求(openai/webhook 用 httpx
MockTransport),config 读写走 tempfile。

覆盖(六项 + 配套):
 0. 配置默认值 + 旧版配置向后兼容(agent.type→mode、webui→web、新段默认);
 1. 同指纹去重只触发一次(FingerprintCache 单元 + 引擎工作路径 + 采集侧 SeenSet);
 2. 白名单外拒绝(SafetyGate 单元 + 引擎工作路径 + 采集∩白名单);
 3. 限频触发(全局QPM/每联系人QPM/冷却期 单元 + 引擎路径);
 4. dry_run 记录发送意图且不调用键鼠(stub flows 零调用)+ allow_real_send 拦截
    + 双开关打开时的真实路径(stub 承载);
 5. echo 后端正常产出;
 6. openai/webhook 后端:MockTransport 验证请求格式 + 异常容错(全兜底 None);
 7. 引擎线程端到端:start→采集→回复→dry_run 意图→不重复→stop 优雅停机。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx

from wxbot.actions.flows import Message, SafetyError
from wxbot.agent.base import AGENT_REGISTRY, create_agent
from wxbot.agent.echo import EchoBackend
from wxbot.agent.openai_compat import DEFAULT_TIMEOUT_SEC as OPENAI_TIMEOUT
from wxbot.agent.openai_compat import OpenAICompatBackend
from wxbot.agent.webhook import WebhookBackend
from wxbot.config import AppConfig, load_config, save_config
from wxbot.engine.monitor import MonitorEngine
from wxbot.engine.safety import RateLimiter, SafetyGate, SafetyRejected
from wxbot.engine.state import (
    ContactContextStore,
    FingerprintCache,
    SeenSet,
    fingerprint,
    is_own,
    normalize_text,
)

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
class StubFlows:
    """离线 flows 替身:与 WeChatFlows 同接口,记录调用,零键鼠/零窗口。"""

    def __init__(self, unread=None, chats=None):
        self.unread = list(unread or [])
        self.chats = dict(chats or {})          # contact -> [Message]
        self.opened: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.send_should_fail = False

    def check_unread(self):
        return list(self.unread)

    def open_chat(self, name, force_search=False):
        self.opened.append(name)
        return True

    def read_messages(self):
        cur = self.opened[-1] if self.opened else None
        return list(self.chats.get(cur, []))

    def send_message(self, text, expect_contact):
        if self.send_should_fail:
            return False
        self.sent.append((expect_contact, text))
        return True


class StubAgent:
    """离线 agent 替身:记录调用,返回脚本化回复。"""

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


def make_cfg(auto=True, dry_run=True, whitelist=("测试联系人",)):
    """安全测试配置:零延迟、高频限额,默认 dry_run。"""
    cfg = AppConfig()
    cfg.engine.auto_reply_enabled = auto
    cfg.engine.poll_interval_sec = 0.05
    cfg.rate_limit.reply_delay_range_sec = (0.0, 0.0)
    cfg.rate_limit.per_contact_cooldown_sec = 0.0
    cfg.rate_limit.global_qpm = 100
    cfg.safety.whitelist = list(whitelist)
    assert cfg.engine.dry_run is dry_run
    return cfg


def make_engine(cfg, flows, agent=None):
    """离线引擎:注入临时目录信箱服务,真实路径的外发入信箱也绝不写项目 data/。"""
    from wxbot.mailbox.service import MailboxService
    if getattr(make_engine, "_mb", None) is None:
        td = tempfile.TemporaryDirectory(prefix="wxbot_engine_mb_")
        make_engine._mb = MailboxService(Path(td.name) / "data")
        make_engine._mb_td = td                  # 保活临时目录
    return MonitorEngine(cfg, flows_factory=lambda: flows, agent=agent,
                         mailbox=make_engine._mb)


def left(text, sender="测试联系人", ts="10:00"):
    return Message(sender=sender, text=text, side="left", ts=ts)


# ---------------------------------------------------------------- 0. 配置
def test_config():
    cfg = AppConfig()
    assert cfg.agent.mode == "echo", cfg.agent.mode
    assert cfg.agent.type == "echo"          # 旧版兼容别名
    assert cfg.agent.timeout_sec == 30.0
    assert cfg.engine.poll_interval_sec == 5.0
    assert cfg.engine.max_replies_per_contact_per_min == 3
    assert cfg.engine.context_window == 20
    assert cfg.engine.dry_run is True
    assert cfg.engine.auto_reply_enabled is False
    assert cfg.allow_real_send is False      # 红线:默认零发送能力
    assert cfg.web.host == "127.0.0.1" and cfg.web.port == 8765
    assert cfg.validate() == []
    assert cfg.safety.whitelist == ["文件传输助手"]

    # 旧版 config.json 兼容加载:webui→web、agent.type→mode、未知键忽略
    legacy = AppConfig.from_dict({
        "webui": {"host": "127.0.0.1", "port": 9756},
        "agent": {"type": "webhook", "webhook_url": "http://10.0.0.2/hook",
                  "timeout_sec": 30},
        "capture": {"poll_interval_sec": 2.5},
        "personas": {"default": "旧人设", "overrides": {}},
        "totally_unknown": {"x": 1},
    })
    assert legacy.agent.mode == "webhook" and legacy.agent.webhook_url == "http://10.0.0.2/hook"
    assert legacy.web.port == 9756 and legacy.webui.port == 9756
    assert legacy.capture.poll_interval_sec == 2.5
    assert legacy.personas["default"] == "旧人设"
    assert legacy.engine.dry_run is True     # 旧配置无 engine 段 → 安全默认
    legacy.agent.type = "echo"
    assert legacy.agent.mode == "echo"

    # 新段 roundtrip:保存写 web/engine/mode/allow_real_send,读回一致
    cfg.agent.mode = "openai"
    cfg.agent.system_prompt = "测试人设"
    cfg.allow_real_send = False
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.json"
        save_config(cfg, p, backup=False)
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert "web" in raw and raw["web"]["port"] == 8765
        assert "webui" not in raw
        assert raw["agent"]["mode"] == "openai" and "type" not in raw["agent"]
        assert raw["engine"]["dry_run"] is True and raw["allow_real_send"] is False
        back = load_config(p, create_default=False)
        assert back.to_dict() == cfg.to_dict()
    return "默认零发送组合 OK;旧版配置(type/webui)自动迁移;roundtrip 一致"


# ---------------------------------------------------------------- 1. 去重
def test_dedup():
    # 指纹:规范化空白后相同文本同指纹;联系人/发送者参与哈希
    assert fingerprint("测试联系人", "测试联系人", "你好") == fingerprint("测试联系人", "测试联系人", "  你好 ")
    assert normalize_text("a\tb\u3000c\nd") == "a b c d"
    assert fingerprint("测试联系人", "测试联系人", "你好") != fingerprint("测试联系人", "测试联系人", "你好呀")
    assert fingerprint("测试联系人", "测试联系人", "你好") != fingerprint("Bob", "测试联系人", "你好")
    assert fingerprint("测试联系人", "测试联系人", "你好") != fingerprint("测试联系人", "self", "你好")

    # TTL 缓存:同指纹 mark 两次 → 第二次 False;过期后重新视为新
    clock_t = [100.0]
    fc = FingerprintCache(capacity=2, ttl_sec=10, clock=lambda: clock_t[0])
    fp = fingerprint("测试联系人", "测试联系人", "hi")
    assert fc.mark(fp) is True
    assert fc.mark(fp) is False
    clock_t[0] = 111.0                        # 超过 TTL 10s
    assert fc.mark(fp) is True
    # 容量淘汰(最旧先出)
    fpa, fpb, fpc = (fingerprint(*t) for t in
                     (("A", "s", "1"), ("B", "s", "1"), ("C", "s", "1")))
    clock_t[0] = 112.0
    assert fc.mark(fpa) and fc.mark(fpb) and len(fc) == 2
    assert fc.mark(fpc) and len(fc) == 2
    assert fc.seen(fpa) is False and fc.seen(fpc) is True

    # 自己消息排除规则
    assert is_own(Message(sender="self", text="x", side="right")) is True
    assert is_own(Message(sender="self", text="x", side="left")) is True
    assert is_own(Message(sender="测试联系人", text="x", side="left")) is False

    # 上下文窗口:上限裁旧 + assistant 记录 + 清空
    store = ContactContextStore(window=3)
    for i in range(5):
        store.append_user("测试联系人", left(f"m{i}"))
    h = store.history("测试联系人")
    assert len(h) == 3 and h[0].text == "m2" and h[-1].text == "m4"
    store.append_assistant("测试联系人", "回复")
    assert store.history("测试联系人")[-1].side == "right"
    assert store.history("测试联系人")[-1].sender == "self"
    store.clear("测试联系人")
    assert store.history("测试联系人") == []

    # 引擎工作路径:同指纹只触发一次 agent(空白差异不影响)
    eng = make_engine(make_cfg(), StubFlows(), StubAgent("回"))
    eng._handle("测试联系人", left("你好  世界"))
    eng._handle("测试联系人", left("  你好 世界 "))   # 同指纹
    assert len(eng._agent.calls) == 1, eng._agent.calls
    assert eng.counters["deduped"] == 1
    assert eng.counters["send_intent"] == 1
    eng._handle("测试联系人", Message(sender="self", text="你好 世界", side="right"))
    assert eng.counters["self_msg"] == 1 and len(eng._agent.calls) == 1

    # 采集侧 SeenSet:同一消息不重复入队(防 TTL 过期后旧消息重触发)
    ss = SeenSet()
    assert ss.is_new("x") is True and ss.is_new("x") is False
    return "指纹/TTL/容量淘汰 OK;引擎路径同文只回一次;自己消息排除;上下文窗口裁旧"


# ---------------------------------------------------------------- 2. 白名单
def test_whitelist():
    gate = SafetyGate(["测试联系人"])
    assert gate.is_whitelisted("测试联系人") and not gate.is_whitelisted("Bob")
    try:
        gate.check_whitelist("Bob")
        raise AssertionError("非白名单未被拒绝")
    except SafetyRejected as e:
        assert e.code == "whitelist"

    # 引擎工作路径:非白名单联系人 → agent 不被调用、无任何发送
    eng = make_engine(make_cfg(), StubFlows(), StubAgent("回"))
    eng._handle("陌生人", left("在吗"))
    assert len(eng._agent.calls) == 0
    assert eng.counters["not_whitelisted"] == 1
    assert eng.counters["send_intent"] == 0

    # send_now 手动发送同样拦截
    try:
        eng.send_now("陌生人", "hi")
        raise AssertionError("send_now 非白名单未被拒绝")
    except SafetyRejected as e:
        assert e.code == "whitelist"
    return "白名单外:SafetyRejected(code=whitelist);agent 零调用;send_now 同拦"


# ---------------------------------------------------------------- 3. 限频
def test_rate_limit():
    rl = RateLimiter(global_qpm=100, per_contact_qpm=2, cooldown_sec=0.0)
    assert rl.allow("测试联系人") == (True, "")
    assert rl.allow("测试联系人") == (True, "")
    ok, why = rl.allow("测试联系人")
    assert ok is False and "联系人限频" in why, why
    assert rl.allow("Bob") == (True, "")      # 其他联系人不受影响

    rl2 = RateLimiter(global_qpm=1, per_contact_qpm=100, cooldown_sec=0.0)
    assert rl2.allow("A")[0] and rl2.allow("B")[0] is False
    assert "全局限频" in rl2.allow("B")[1]

    rl3 = RateLimiter(global_qpm=100, per_contact_qpm=100, cooldown_sec=5.0)
    assert rl3.allow("测试联系人")[0] is True
    ok, why = rl3.allow("测试联系人")
    assert ok is False and "冷却" in why, why

    # 引擎路径:agent 被调用(agent 在限频检查之前),但发送被限
    cfg = make_cfg()
    cfg.engine.max_replies_per_contact_per_min = 1
    eng = make_engine(cfg, StubFlows(), StubAgent("回"))
    eng._handle("测试联系人", left("第一条"))
    eng._handle("测试联系人", left("第二条"))
    assert len(eng._agent.calls) == 2
    assert eng.counters["rate_limited"] == 1
    assert eng.counters["send_intent"] == 1
    ev = [e for e in eng.events if e["type"] == "rate_limited"]
    assert ev and "联系人限频" in ev[0]["reason"]
    return "每联系人QPM/全局QPM/冷却期均拦截;引擎路径 agent 后限频,拒绝记事件"


# ---------------------------------------------------------------- 4. dry_run
def test_dry_run():
    # 默认配置:dry_run 记录意图,绝不调用键鼠(stub flows 零交互)
    flows = StubFlows()
    eng = make_engine(make_cfg(), flows, StubAgent("拟回复"))
    eng._handle("测试联系人", left("说句话"))
    intents = [e for e in eng.events if e["type"] == "send_intent"]
    assert len(intents) == 1
    assert intents[0]["contact"] == "测试联系人" and intents[0]["text"] == "拟回复"
    assert intents[0]["auto"] is True
    assert flows.sent == [] and flows.opened == []   # 键鼠零调用(连 open_chat 都没有)
    assert eng.counters["send_intent"] == 1
    # 意图已进对话上下文(模拟对话连续性)
    assert eng.ctx.history("测试联系人")[-1].text == "拟回复"

    # auto_reply_enabled=False(出厂默认):只记录不回复
    eng2 = make_engine(make_cfg(auto=False), StubFlows(), StubAgent("拟回复"))
    eng2._handle("测试联系人", left("在吗"))
    assert len(eng2._agent.calls) == 0 and eng2.counters["auto_disabled"] == 1

    # allow_real_send 红线:dry_run=False 但总开关 false → 显式拦截
    cfg3 = make_cfg()
    cfg3.engine.dry_run = False
    cfg3.allow_real_send = False
    flows3 = StubFlows()
    eng3 = make_engine(cfg3, flows3, StubAgent())
    try:
        eng3.send_now("测试联系人", "x")
        raise AssertionError("allow_real_send=false 未拦截真实发送")
    except SafetyRejected as e:
        assert e.code == "allow_real_send", e.code
    assert flows3.sent == []
    try:
        eng3.gate.send_mode()
        raise AssertionError("send_mode 未拦截")
    except SafetyRejected as e:
        assert e.code == "allow_real_send"

    # 双开关全开(dry_run=False + allow_real_send=True)→ 真实路径(由 stub 承载)
    cfg4 = make_cfg()
    cfg4.engine.dry_run = False
    cfg4.allow_real_send = True
    flows4 = StubFlows()
    eng4 = make_engine(cfg4, flows4, StubAgent())
    assert eng4.send_now("测试联系人", "手动消息") is True
    assert flows4.sent == [("测试联系人", "手动消息")]
    assert eng4.counters["sent"] == 1

    # 发送失败与 flows 双闸门(SafetyError)路径
    flows5 = StubFlows()
    flows5.send_should_fail = True
    eng5 = make_engine(cfg4, flows5, StubAgent())
    assert eng5.send_now("测试联系人", "x") is False and eng5.counters["send_failed"] == 1
    flows6 = StubFlows()

    class _TitleGateFlows(StubFlows):
        def open_chat(self, name, force_search=False):
            self.opened.append(name)
            return True

        def send_message(self, text, expect_contact):
            raise SafetyError("红线拦截:当前聊天标题 != 目标")  # 模拟标题双闸门

    eng6 = make_engine(cfg4, _TitleGateFlows(), StubAgent())
    assert eng6.send_now("测试联系人", "x") is False
    assert any(e["type"] == "safety_block" for e in eng6.events)
    assert flows6.sent == []
    return "dry_run 意图事件+零键鼠;auto 开关默认关;allow_real_send=false 拦截;真实路径/失败/flows 闸门均正确"


# ---------------------------------------------------------------- 5. echo
def test_echo():
    be = EchoBackend()
    assert be.name() == "echo" and be.healthcheck() is True
    out = be.reply("测试联系人", [], left("在吗"))
    assert out == "【echo】在吗", out
    assert be.reply("测试联系人", [], left("  ")) is None       # 空文本沉默
    # 工厂:默认配置 → echo;注册表齐全
    assert create_agent(AppConfig().agent).name() == "echo"
    assert sorted(AGENT_REGISTRY) == ["echo", "openai", "webhook"]
    try:
        from wxbot.config import AgentConfig
        create_agent(AgentConfig(mode="nope"))
        raise AssertionError("未知 mode 未报错")
    except ValueError:
        pass
    return "echo 回复=【echo】+原文;空文本 None;工厂默认 echo;未知 mode 报 ValueError"


# ---------------------------------------------------------------- 6. httpx 后端
def test_openai_backend():
    assert OPENAI_TIMEOUT == 30.0
    cap: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cap["method"], cap["url"] = req.method, str(req.url)
        cap["auth"] = req.headers.get("authorization")
        cap["body"] = json.loads(req.content.decode("utf-8"))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "回复!"}}]})

    be = OpenAICompatBackend("https://api.test/v1/", "sk-test", "m1",
                             system_prompt="你是测试",
                             client=httpx.Client(transport=httpx.MockTransport(handler)))
    history = [left("上次"), Message(sender="self", text="我说", side="right")]
    out = be.reply("测试联系人", history, left("新消息"))
    assert out == "回复!", out
    assert cap["method"] == "POST"
    assert cap["url"] == "https://api.test/v1/chat/completions", cap["url"]
    assert cap["auth"] == "Bearer sk-test"
    assert cap["body"]["model"] == "m1"
    assert cap["body"]["messages"] == [
        {"role": "system", "content": "你是测试"},
        {"role": "user", "content": "上次"},
        {"role": "assistant", "content": "我说"},
        {"role": "user", "content": "新消息"},
    ], cap["body"]["messages"]

    # 异常容错:网络错误 / 非2xx / 缺 choices / 空内容 → 一律 None
    def boom(req):
        raise httpx.ConnectError("connection refused")

    def e500(req):
        return httpx.Response(500, json={"error": "boom"})

    def nochoices(req):
        return httpx.Response(200, json={"object": "list"})

    def empty(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})

    for h in (boom, e500, nochoices, empty):
        b = OpenAICompatBackend("https://api.test/v1", "k", "m",
                                client=httpx.Client(transport=httpx.MockTransport(h)))
        assert b.reply("测试联系人", [], left("x")) is None, h.__name__

    # healthcheck:GET /models,<400 True;404/网络错误 False
    def models(req):
        cap["hc_url"] = str(req.url)
        return httpx.Response(200, json={"data": []})

    b_ok = OpenAICompatBackend("https://api.test/v1", "k", "m",
                               client=httpx.Client(transport=httpx.MockTransport(models)))
    assert b_ok.healthcheck() is True and cap["hc_url"] == "https://api.test/v1/models"
    b_404 = OpenAICompatBackend("https://api.test/v1", "k", "m",
                                client=httpx.Client(transport=httpx.MockTransport(e500)))
    assert b_404.healthcheck() is False
    b_err = OpenAICompatBackend("https://api.test/v1", "k", "m",
                                client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert b_err.healthcheck() is False
    # 工厂构造(三参数来自配置)
    cfg = AppConfig()
    cfg.agent.mode = "openai"
    cfg.agent.base_url = "https://x/v1"
    cfg.agent.api_key = "k"
    cfg.agent.model = "m"
    from wxbot.agent.base import create_agent as ca
    assert ca(cfg.agent).name() == "openai"
    return "openai:URL/Bearer/model/messages(含 system+多轮)格式正确;4类异常全兜底 None;healthcheck /models"


def test_webhook_backend():
    cap: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        cap["url"] = str(req.url)
        cap["secret"] = req.headers.get("x-webhook-secret")
        cap["ctype"] = req.headers.get("content-type")
        cap["body"] = json.loads(req.content.decode("utf-8"))
        return httpx.Response(200, json={"reply": "好的"})

    be = WebhookBackend("https://cb.example/hook", secret="s3cret",
                        client=httpx.Client(transport=httpx.MockTransport(handler)))
    hist = [left("早")]
    out = be.reply("测试联系人", hist, left("在吗", ts="10:01"))
    assert out == "好的", out
    assert cap["url"] == "https://cb.example/hook"
    assert cap["secret"] == "s3cret"
    assert cap["ctype"] == "application/json"
    # v1.1(PROTOCOL §11):报文顶层与 history/incoming 每条均含 msg_id/chat_id;
    # 本测试直接构造 Message(未经引擎分发编号)→ 两字段为 null
    assert set(cap["body"].keys()) == {"contact", "msg_id", "chat_id",
                                       "history", "incoming"}, cap["body"]
    assert cap["body"]["contact"] == "测试联系人"
    assert cap["body"]["msg_id"] is None and cap["body"]["chat_id"] == "测试联系人"
    assert cap["body"]["history"] == [{"sender": "测试联系人", "text": "早",
                                       "side": "left", "ts": "10:00",
                                       "msg_id": None, "chat_id": None}]
    assert cap["body"]["incoming"]["msg_id"] is None
    assert cap["body"]["incoming"]["chat_id"] is None
    assert cap["body"]["incoming"]["text"] == "在吗"

    # 容错:null reply→None;兼容 text 字段;非2xx/网络错误→None;未配置 URL→None
    def nullreply(req):
        return httpx.Response(200, json={"reply": None})

    def textfield(req):
        return httpx.Response(200, json={"text": "备用字段"})

    def e500(req):
        return httpx.Response(503)

    def boom(req):
        raise httpx.ConnectError("timeout")

    for h, expect in ((nullreply, None), (textfield, "备用字段"), (e500, None), (boom, None)):
        b = WebhookBackend("https://cb.example/hook",
                           client=httpx.Client(transport=httpx.MockTransport(h)))
        assert b.reply("测试联系人", [], left("x")) == expect, h.__name__
    assert WebhookBackend("").reply("测试联系人", [], left("x")) is None
    assert WebhookBackend("").healthcheck() is False

    def ok200(req):
        return httpx.Response(200, json={})

    b_hc = WebhookBackend("https://cb.example/hook",
                          client=httpx.Client(transport=httpx.MockTransport(ok200)))
    assert b_hc.healthcheck() is True
    b_hc2 = WebhookBackend("https://cb.example/hook",
                           client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert b_hc2.healthcheck() is False
    # 工厂构造
    cfg = AppConfig()
    cfg.agent.mode = "webhook"
    cfg.agent.webhook_url = "https://cb.example/hook"
    from wxbot.agent.base import create_agent as ca
    assert ca(cfg.agent).name() == "webhook"
    return "webhook:POST {contact,msg_id,chat_id,history,incoming}/X-Webhook-Secret 正确(v1.1 每条含 msg_id/chat_id);reply/text/容错/healthcheck 全通过"


# ---------------------------------------------------------------- 7. 端到端线程
def test_engine_e2e():
    flows = StubFlows(unread=["陌生人", "测试联系人"],
                      chats={"测试联系人": [left("e2e-hello")]})
    agent = StubAgent("e2e-回复")
    eng = make_engine(make_cfg(), flows, agent)
    eng.start()
    double_start_rejected = False
    try:
        eng.start()
    except RuntimeError:
        double_start_rejected = True
    deadline = time.time() + 10
    while time.time() < deadline and eng.counters["send_intent"] < 1:
        time.sleep(0.05)
    assert eng.counters["send_intent"] >= 1, f"未产出发送意图: {eng.counters}"
    assert double_start_rejected, "重复 start 未被拒绝"
    assert flows.sent == [], "dry_run 下 stub flows 不应有任何发送"
    assert flows.opened == ["测试联系人"], f"应只 open_chat 白名单成员: {flows.opened}"
    inbox = [e for e in eng.events if e["type"] == "inbox"]
    assert inbox and inbox[0]["contact"] == "测试联系人"
    assert any(e["type"] == "unread_skipped" for e in eng.events), "非白名单未读应记跳过事件"
    ctx = eng.ctx.history("测试联系人")
    assert len(ctx) == 2 and ctx[0].text == "e2e-hello" and ctx[-1].text == "e2e-回复"

    time.sleep(0.5)   # 多个轮询周期:采集侧去重应保证不重复回复
    assert eng.counters["send_intent"] == 1, f"同指纹被重复回复: {eng.counters}"

    st = eng.status()
    assert st["running"] is True and st["agent"] == "stub"
    assert st["engine"]["dry_run"] is True and st["whitelist"] == ["测试联系人"]

    eng.stop(timeout=5.0)
    assert not eng.running, "stop 后线程未退出"
    eng.stop()        # 幂等
    assert eng.status()["running"] is False
    return "线程端到端:采集∩白名单→agent→dry_run 意图;不重复回复;status/stop 优雅"


# ---------------------------------------------------------------- 汇总
def main() -> int:
    check("0", "配置默认值 + 旧版配置向后兼容(type→mode / webui→web)", test_config)
    check("1", "同指纹去重只触发一次(缓存/TTL/容量/自己消息/引擎路径)", test_dedup)
    check("2", "白名单外拒绝(闸门 + 引擎路径 + send_now)", test_whitelist)
    check("3", "限频触发(全局QPM/每联系人QPM/冷却期 + 引擎路径)", test_rate_limit)
    check("4", "dry_run 记录意图且不调用键鼠 + allow_real_send 拦截", test_dry_run)
    check("5", "echo 后端正常产出 + 工厂/注册表", test_echo)
    check("6a", "openai 后端 httpx MockTransport 请求格式与异常容错", test_openai_backend)
    check("6b", "webhook 后端 MockTransport 请求格式与异常容错", test_webhook_backend)
    check("7", "引擎线程端到端(启动/采集/回复/去重/优雅停止)", test_engine_e2e)

    failed = [r for r in RESULTS if not r[1]]
    if failed:
        print(f"\n失败项: {[(i,) for i, _, _ in failed]}")
        print("ENGINE_TEST_FAIL")
        return 1
    print("\n全部检查通过:纯离线,未触碰微信窗口、未执行键鼠、未发真实网络请求。")
    print("ENGINE_TEST_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
