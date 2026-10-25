# -*- coding: utf-8 -*-
"""群聊离线验收脚本(2026-10-24 群聊支持)。

exit 0 = 通过(末行打印 GROUP_TEST_PASS);任一检查失败 exit 1。

红线:纯离线测试——合成 OCR 条目 + stub flows/agent,绝不查找/触碰微信
窗口、绝不执行键鼠、绝不向任何群或联系人发送任何内容。

覆盖:
 0. config groups 段:默认全关 + 校验 + 旧配置兼容 + roundtrip;
 1. 群 sender 提取(合成 OCR):昵称行形态 A /「昵称: 正文」前缀形态 B /
    无昵称回落群名 / 自己消息 sender=self / 私聊解析不受影响;
 2. @ 提及检测:extract_at_targets 各种 OCR 形态;
 3. reply_mode 三种路由 + 各前置条件不满足一律忽略(GroupRouter 单元);
 4. 引擎路径:默认全关零交互零回复;采集/工作全链路(at 命中/未命中);
    群回复强制演练(双开关全开也不真实发送);私聊不受影响;
 5. flows 红线兜底:群真实发送在任何键鼠前硬禁——白名单闸门 + OCR 标题
    相等 + 标题栏全项群形态判定(合并/拆项两种 OCR 形态都拦;群名被误混入
    safety.whitelist 且双开关全开也不放行,2026-10-24 审查修复)。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from wxbot.actions.flows import Message, SafetyError, WeChatFlows, split_group_title
from wxbot.config import AppConfig
from wxbot.engine.groups import GroupRouter, extract_at_targets
from wxbot.engine.monitor import MonitorEngine
from wxbot.vision.ocr import OcrItem

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
class StubWin:
    """离线窗口替身:只提供解析所需的 size。"""

    size = (896, 648)
    rect_screen = (0, 0, 896, 648)


class StubFlows:
    """离线 flows 替身(带群接口):记录调用,零键鼠/零窗口。"""

    def __init__(self, unread=None, chats=None, group_chats=None,
                 with_group_reader=True):
        self.unread = list(unread or [])
        self.chats = dict(chats or {})            # 私聊: contact -> [Message]
        self.group_chats = dict(group_chats or {})  # 群: group -> [Message]
        self.opened: list[str] = []
        self.sent: list[tuple[str, str]] = []
        if not with_group_reader:
            # 模拟旧版 flows:实例属性覆盖,使 getattr(...) 返回 None(不可调用)
            self.read_group_messages = None

    def check_unread(self):
        return list(self.unread)

    def open_chat(self, name, force_search=False):
        self.opened.append(name)
        return True

    def read_messages(self):
        cur = self.opened[-1] if self.opened else None
        return list(self.chats.get(cur, self.group_chats.get(cur, [])))

    def read_group_messages(self):                    # 群聊读取(可被实例属性屏蔽)
        cur = self.opened[-1] if self.opened else None
        return list(self.group_chats.get(cur, []))

    def send_message(self, text, expect_contact):
        self.sent.append((expect_contact, text))
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


def make_cfg(**groups) -> AppConfig:
    """安全测试配置:零延迟、高频限额、默认 dry_run;groups 按参覆盖。"""
    cfg = AppConfig()
    cfg.engine.auto_reply_enabled = True
    cfg.engine.poll_interval_sec = 0.05
    cfg.rate_limit.reply_delay_range_sec = (0.0, 0.0)
    cfg.rate_limit.per_contact_cooldown_sec = 0.0
    cfg.rate_limit.global_qpm = 100
    cfg.safety.whitelist = ["测试联系人"]
    for k, v in groups.items():
        setattr(cfg.groups, k, v)
    assert cfg.groups.enabled is ("enabled" in groups and groups["enabled"])
    return cfg


_MB_TD: list = []


def make_engine(cfg: AppConfig, flows, agent=None) -> MonitorEngine:
    """离线引擎:注入临时目录信箱服务——真实路径的外发入信箱
    绝不写项目 data/(信箱单例默认指向项目根 data/)。"""
    import tempfile
    from wxbot.mailbox.service import MailboxService
    if not _MB_TD:
        _MB_TD.append(tempfile.TemporaryDirectory(prefix="wxbot_group_mb_"))
    mb = MailboxService(Path(_MB_TD[0].name) / "data")
    return MonitorEngine(cfg, flows_factory=lambda: flows, agent=agent,
                         mailbox=mb)


# ---------------------------------------------------------------- 合成 OCR
def item(text: str, x0: int, y0: int, x1: int, y1: int,
         conf: float = 0.99) -> OcrItem:
    """合成 OCR 条目(box 四点 + 中心)。坐标基于 896x648 基准窗口:
    消息区 x≈305..896 y≈80..500;左气泡文本中心 rel<0.47,右气泡 rel>0.53。"""
    return OcrItem(
        text=text,
        box=((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
        center=((x0 + x1) // 2, (y0 + y1) // 2),
        conf=conf,
    )


WHITE = np.full((648, 896, 3), 255, dtype=np.uint8)  # 纯白:归属由左右位置决定


def group_scene_items() -> list[OcrItem]:
    """合成群聊截图 OCR 结果(微信 4.x 常见布局):
    标题「测试群 (23)」;时间线;张三昵称行+气泡;李四昵称行+@气泡;
    自己右侧气泡;「王五: 早上好」前缀单行;无名氏左气泡(回落群名)。"""
    return [
        item("测试群 (23)", 320, 45, 420, 66),          # 标题栏
        item("10:00", 570, 88, 620, 104),               # 居中时间线
        # 张三:昵称行(矮,14px)+ 正文气泡
        item("张三", 320, 100, 348, 114),
        item("大家好", 320, 118, 390, 138),
        # 李四:昵称行 + @正文
        item("李四", 320, 160, 348, 174),
        item("@助手 今天天气怎么样", 320, 178, 500, 198),
        # 自己(右侧,rel>0.53)
        item("哈哈", 680, 210, 720, 230),
        # 前缀形态 B:OCR 单行「王五: 早上好」(行高 20,不会被当昵称行)
        item("王五: 早上好", 320, 240, 440, 260),
        # 无昵称无前缀的左气泡 → sender 回落群名
        item("嗯嗯", 320, 280, 360, 300),
    ]


def make_flows() -> WeChatFlows:
    # 离线:win 只提供 size;hi/ocr_fn 不会被 parse_* 路径调用
    return WeChatFlows(StubWin(), None, None, whitelist=["测试联系人"])


# ---------------------------------------------------------------- 0. 配置
def test_config():
    cfg = AppConfig()
    g = cfg.groups
    assert g.enabled is False and g.reply_mode == "off"       # 默认全关
    assert g.whitelist == [] and g.bot_names == []
    assert cfg.validate() == []
    assert "groups" in cfg.to_dict()

    # 旧配置(无 groups 段)→ 安全默认
    old = AppConfig.from_dict({"safety": {"whitelist": ["测试联系人"]}})
    assert old.groups.enabled is False and old.groups.reply_mode == "off"

    # 非法值被校验拦截
    assert AppConfig.from_dict({"groups": {"reply_mode": "x"}}).validate()
    assert AppConfig.from_dict({"groups": {"enabled": True}}).validate()  # 无名单
    assert AppConfig.from_dict(
        {"groups": {"enabled": True, "whitelist": [""]}}).validate()

    # 合法开启 + roundtrip
    ok = AppConfig.from_dict({"groups": {
        "enabled": True, "reply_mode": "at",
        "whitelist": ["测试群"], "bot_names": ["助手"]}})
    assert ok.validate() == []
    import tempfile
    from wxbot.config import load_config, save_config
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.json"
        save_config(ok, p, backup=False)
        back = load_config(p, create_default=False)
        assert back.to_dict() == ok.to_dict()
    return "groups 默认全关;非法值/空名单拦截;roundtrip 一致"


# ---------------------------------------------------------------- 1. sender 提取
def test_sender_extract():
    flows = make_flows()
    items = group_scene_items()
    msgs = flows.parse_group_messages(WHITE, items)

    assert len(msgs) == 5, [m.text for m in msgs]
    m0, m1, m2, m3, m4 = msgs
    # 形态 A:昵称行
    assert m0.sender == "张三" and m0.text == "大家好" and m0.side == "left", (m0.sender, m0.text)
    assert m1.sender == "李四" and m1.text == "@助手 今天天气怎么样", (m1.sender, m1.text)
    # 自己消息
    assert m2.sender == "self" and m2.side == "right"
    # 形态 B:前缀「王五: 早上好」
    assert m3.sender == "王五" and m3.text == "早上好", (m3.sender, m3.text)
    # 回落:无昵称无前缀 → 群名
    assert m4.sender == "测试群" and m4.text == "嗯嗯", (m4.sender, m4.text)
    # 时间线归属
    assert m0.ts == "10:00" and m4.ts == "10:00"

    # 前缀防呆:时间/URL/数字前缀不当昵称
    s, t = flows._split_nick_prefix("10:30: 开会啦", fallback="G")
    assert (s, t) == ("G", "10:30: 开会啦"), (s, t)
    s, t = flows._split_nick_prefix("https://x.com/a: 说明", fallback="G")
    assert (s, t) == ("G", "https://x.com/a: 说明")
    s, t = flows._split_nick_prefix("2026: 年份", fallback="G")
    assert (s, t) == ("G", "2026: 年份")

    # 多行前缀气泡
    s, t = flows._split_nick_prefix("李四: 第一行\n第二行", fallback="G")
    assert s == "李四" and t == "第一行\n第二行", (s, t)

    # 私聊解析不受影响:同一解析入口下,私聊场景 sender=联系人名,
    # 昵称行逻辑不参与(parse_messages 不做昵称剥离)
    priv = [
        item("测试联系人", 320, 45, 350, 66),
        item("你好呀", 320, 100, 400, 120),               # 左
        item("你好!有什么可以帮你?", 640, 140, 800, 160),  # 右
    ]
    pm = flows.parse_messages(WHITE, priv)
    assert len(pm) == 2
    assert pm[0].sender == "测试联系人" and pm[0].side == "left" and pm[0].text == "你好呀"
    assert pm[1].sender == "self" and pm[1].side == "right"

    # 标题成员数识别:群标题「群名 (N)」;普通联系人名不受影响
    assert split_group_title("测试群 (23)") == ("测试群", 23)
    assert split_group_title("测试群（23）") == ("测试群", 23)
    assert split_group_title("测试联系人") == ("测试联系人", None)
    assert split_group_title("张三(公司)") == ("张三(公司)", None)  # 括号内非数字
    assert split_group_title("") == ("", None)
    return ("昵称行/前缀/回落/时间线 全命中;前缀防呆;私聊解析不变;"
            "标题「群名 (N)」识别且不误伤普通名")


# ---------------------------------------------------------------- 2. @ 检测
def test_at_detect():
    assert extract_at_targets("@助手 帮我查天气") == ["助手"]
    assert extract_at_targets("@ 助手 在吗") == ["助手"]          # @ 后多空格
    assert extract_at_targets("大家好") == []
    assert extract_at_targets("邮件我 a@b.com 发你") == []        # 邮箱不算提及
    assert extract_at_targets("你好 @张三 看看") == ["张三"]      # @ 前有空格
    assert extract_at_targets("@张三 @李四 都看一下") == ["张三", "李四"]
    assert extract_at_targets("@所有人 通知") == ["所有人"]
    assert extract_at_targets("") == []
    assert extract_at_targets("@") == []
    return "@昵称/@后空格/@前空格/多个@/邮箱不误报/无@/空文本 均正确"


# ---------------------------------------------------------------- 3. 路由
def test_reply_mode_router():
    msg_at = Message(sender="张三", text="@助手 帮忙", side="left")
    msg_plain = Message(sender="张三", text="大家好", side="left")
    msg_other_at = Message(sender="李四", text="@别人 你看", side="left")

    # 默认(全关):任何群任何消息都不路由
    r = GroupRouter(AppConfig().groups)
    for m in (msg_at, msg_plain):
        ok, why = r.should_handle("任意群", m)
        assert ok is False and why == "groups_disabled", (ok, why)
    assert r.collectible("任意群") is False

    # enabled 但群不在名单
    r = GroupRouter(make_cfg(enabled=True, reply_mode="all",
                             whitelist=["测试群"]).groups)
    ok, why = r.should_handle("别的群", msg_at)
    assert ok is False and why == "group_not_whitelisted"

    # reply_mode=off(即使 enabled + 在名单)
    r = GroupRouter(make_cfg(enabled=True, reply_mode="off",
                             whitelist=["测试群"]).groups)
    ok, why = r.should_handle("测试群", msg_at)
    assert ok is False and why == "reply_mode_off"
    assert r.collectible("测试群") is False       # off:采集阶段也不打开群

    # at 模式
    r = GroupRouter(make_cfg(enabled=True, reply_mode="at",
                             whitelist=["测试群"], bot_names=["助手"]).groups)
    assert r.should_handle("测试群", msg_plain) == (False, "no_at_mention")
    assert r.should_handle("测试群", msg_other_at)[0] is False    # @了别人
    ok, why = r.should_handle("测试群", msg_at)
    assert ok is True and why == "at_bot:助手", why
    # 严格性:@ 后目标提不全且机器人名未紧随 @ → 不算(@AI 是别人的昵称)
    m_strict = Message(sender="张三", text="@ AI 助手 在吗", side="left")
    ok, why = r.should_handle("测试群", m_strict)
    assert ok is False and why == "at_not_me", (ok, why)
    assert r.collectible("测试群") is True

    # OCR 断字容错:机器人名本身被拆进空格(bot「AI助手」↔ OCR「@ AI 助手」)
    r_fuzzy = GroupRouter(make_cfg(enabled=True, reply_mode="at",
                                   whitelist=["测试群"],
                                   bot_names=["AI助手"]).groups)
    ok, why = r_fuzzy.should_handle("测试群", m_strict)
    assert ok is True and why == "at_bot_fuzzy", (ok, why)
    # 模糊兜底不放大:@AI 后未紧跟机器人名 → 仍拒绝
    m_sp2 = Message(sender="张三", text="@AI 大家好", side="left")
    ok, why = r_fuzzy.should_handle("测试群", m_sp2)
    assert ok is False and why == "at_not_me", (ok, why)

    # at 模式 + bot_names 空 → 任意 @ 命中
    r2 = GroupRouter(make_cfg(enabled=True, reply_mode="at",
                              whitelist=["测试群"]).groups)
    ok, why = r2.should_handle("测试群", msg_other_at)
    assert ok is True and why == "at_any", why
    ok, why = r2.should_handle("测试群", msg_plain)
    assert ok is False and why == "no_at_mention"

    # all 模式:任何成员消息都触发
    r3 = GroupRouter(make_cfg(enabled=True, reply_mode="all",
                              whitelist=["测试群"]).groups)
    for m in (msg_at, msg_plain, msg_other_at):
        ok, why = r3.should_handle("测试群", m)
        assert ok is True and why == "reply_mode_all"
    # is_managed 与 enabled 无关(发送侧红线用)
    assert GroupRouter(make_cfg(enabled=False, whitelist=["G1"]).groups
                       ).is_managed("G1") is True
    return "disabled/不在名单/off 一律忽略;at 需@机器人(空名单=任意@);all 全触发"


# ---------------------------------------------------------------- 4. 引擎路径
def test_engine_default_off():
    """默认全关:群未读不打开、不读取、不回复;私聊逻辑不受影响。"""
    cfg = make_cfg()                       # groups 默认全关
    flows = StubFlows(unread=["测试群", "测试联系人"],
                      chats={"测试联系人": [Message(sender="测试联系人", text="在吗", side="left")]},
                      group_chats={"测试群": [
                          Message(sender="张三", text="@助手 帮忙", side="left")]})
    agent = StubAgent("回")
    eng = make_engine(cfg, flows, agent)
    eng._ensure_flows()                    # 与 start() 同路:惰性建 flows(离线注入)

    eng._collect_once()                    # 采集一轮
    assert "测试群" not in flows.opened, flows.opened   # 群根本没被打开
    assert flows.opened == ["测试联系人"]                       # 私聊正常
    assert any(e["type"] == "unread_skipped" and "测试群" in e.get("contacts", [])
               for e in eng.events)

    # 工作线程路径:群消息若出现在队列(不可能,但兜底验证)也会被忽略
    eng._handle_group("测试群", Message(sender="张三", text="@助手 帮忙", side="left"))
    eng._handle("测试联系人", Message(sender="测试联系人", text="在吗", side="left"))
    assert len(agent.calls) == 1 and agent.calls[0][0] == "测试联系人"  # 只有私聊到 agent
    assert eng.counters["group_ignored"] == 1
    assert eng.counters["send_intent"] == 1                     # 仅 测试联系人 的演练意图
    st = eng.status()
    assert st["groups"]["enabled"] is False and st["groups"]["reply_mode"] == "off"
    return "默认全关:群零交互零回复(unread_skipped);测试联系人 私聊照常产生演练意图"


def test_engine_group_flow():
    """开启 at 模式:采集→sender→@路由→agent→强制演练;未命中@被忽略。"""
    cfg = make_cfg(enabled=True, reply_mode="at",
                   whitelist=["测试群"], bot_names=["助手"])
    flows = StubFlows(
        unread=["测试群"],
        group_chats={"测试群": [
            Message(sender="张三", text="大家好", side="left"),
            Message(sender="李四", text="@助手 今天天气怎么样", side="left"),
        ]})
    agent = StubAgent("群回复")
    eng = make_engine(cfg, flows, agent)
    eng._ensure_flows()                    # 与 start() 同路:惰性建 flows(离线注入)

    eng._collect_once()
    assert flows.opened == ["测试群"]                  # 群被打开(只读读取)
    assert eng.counters["seen"] == 2                   # 两条群消息入队

    # 未 @ → 路由忽略
    eng._handle_group("测试群", Message(sender="张三", text="大家好", side="left"))
    assert len(agent.calls) == 0
    assert eng.counters["group_ignored"] == 1
    assert any(e["type"] == "group_ignored" and e["reason"] == "no_at_mention"
               for e in eng.events)

    # @机器人 → agent,回复只记演练意图(默认 dry_run),绝不真实发送
    eng._handle_group("测试群",
                      Message(sender="李四", text="@助手 今天天气怎么样", side="left"))
    assert len(agent.calls) == 1
    assert agent.calls[0][0] == "测试群"
    assert agent.calls[0][2] == "@助手 今天天气怎么样"
    assert agent.calls[0][1] == []                     # 上下文:此前未@消息未入上下文
    intents = [e for e in eng.events if e["type"] == "send_intent"]
    assert len(intents) == 1 and intents[0]["contact"] == "测试群"
    assert flows.sent == []                            # 零键鼠发送
    assert eng.ctx.history("测试群")[-1].text == "群回复"   # 每群独立上下文
    assert eng.ctx.history("测试群")[0].sender == "李四"

    # all 模式:无需 @
    cfg_all = make_cfg(enabled=True, reply_mode="all", whitelist=["测试群"])
    flows_all = StubFlows(unread=[], group_chats={})
    eng_all = make_engine(cfg_all, flows_all, StubAgent("回"))
    eng_all._handle_group("测试群", Message(sender="张三", text="随便聊聊", side="left"))
    assert len(eng_all._agent.calls) == 1
    return ("at:未@忽略/no_at_mention,@机器人→agent→演练意图,零真实发送;"
            "all:任意消息触发;群上下文独立")


def test_engine_group_never_real_send():
    """红线:双开关全开(dry_run=False + allow_real_send=True)时,
    群回复仍只记意图、绝不真实发送;私聊同配置可走真实路径(stub 承载)。"""
    cfg = make_cfg(enabled=True, reply_mode="all", whitelist=["测试群"])
    cfg.engine.dry_run = False
    cfg.allow_real_send = True
    flows = StubFlows()
    agent = StubAgent("群回")
    eng = make_engine(cfg, flows, agent)
    assert eng._deliver("测试群", "往群里发?", auto=True) is True
    assert ("测试群", "往群里发?") not in flows.sent      # 群零键鼠
    intents = [e for e in eng.events if e["type"] == "send_intent"]
    assert intents and "群聊回复红线" in intents[0]["note"]
    # 私聊不受影响:双开关全开 → 真实路径(stub 承载)
    assert eng.send_now("测试联系人", "私聊消息") is True
    assert flows.sent == [("测试联系人", "私聊消息")]
    assert eng.counters["sent"] == 1 and eng.counters["send_intent"] == 1
    return "群回复强制演练(双开关全开也不发);私聊真实路径不受影响"


def test_engine_e2e_threads():
    """线程端到端:群+私聊未读同时存在,at 模式下只有@消息产出演练意图。"""
    import time
    cfg = make_cfg(enabled=True, reply_mode="at",
                   whitelist=["测试群"], bot_names=["助手"])
    flows = StubFlows(
        unread=["测试群", "测试联系人"],
        chats={"测试联系人": [Message(sender="测试联系人", text="hello", side="left")]},
        group_chats={"测试群": [
            Message(sender="张三", text="纯聊天", side="left"),
            Message(sender="李四", text="@助手 到", side="left"),
        ]})
    agent = StubAgent("回")
    eng = make_engine(cfg, flows, agent)
    eng.start()
    deadline = time.time() + 10
    while time.time() < deadline and eng.counters["send_intent"] < 2:
        time.sleep(0.05)
    time.sleep(0.3)
    # 两个演练意图:测试联系人 私聊 + 群@;纯聊天不触发
    texts = {e["contact"] for e in eng.events if e["type"] == "send_intent"}
    assert texts == {"测试联系人", "测试群"}, texts
    assert flows.sent == []                            # 全程零真实发送
    kinds = {("group", e["contact"]) for e in eng.events if e["type"] == "inbox"
             and e.get("group")}
    assert ("group", "测试群") in kinds
    assert any(e["type"] == "group_ignored" for e in eng.events)
    st = eng.status()
    assert st["groups"]["enabled"] is True and st["groups"]["whitelist"] == ["测试群"]
    eng.stop(timeout=5.0)
    assert not eng.running
    return "线程端到端:群@+私聊各产生1条演练意图;纯聊天被忽略;零发送;优雅停止"


def test_engine_collect_fallback():
    """flows 没有 read_group_messages(旧版)时,采集回退 read_messages。"""
    cfg = make_cfg(enabled=True, reply_mode="all", whitelist=["测试群"])
    flows = StubFlows(unread=["测试群"],
                      group_chats={"测试群": [
                          Message(sender="张三", text="老版本flows", side="left")]},
                      with_group_reader=False)
    eng = make_engine(cfg, flows, StubAgent("回"))
    eng._ensure_flows()                    # 与 start() 同路:惰性建 flows(离线注入)
    eng._collect_once()
    assert eng.counters["seen"] == 1
    return "无 read_group_messages 的 flows 回退 read_messages 正常"


# ---------------------------------------------------------------- 5. flows 红线
class _StubWin:
    """离线窗口替身:只提供解析/定位所需的 size 与白图。"""

    size = (896, 648)
    rect_screen = (0, 0, 896, 648)

    def ensure_active(self):
        return True

    def capture(self):
        return np.full((648, 896, 3), 255, dtype=np.uint8)


class _StubHi:
    """键鼠替身:只记录动作,绝不执行真实键鼠。"""

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


def _title_scene(group_form: str) -> list[OcrItem]:
    """合成标题栏 OCR:group_form=None 为私聊 测试联系人;split=拆项;merged=合并。
    另含发送按钮项(供私聊正常路径走完键鼠)与自己右侧气泡(发送成功判定)。"""
    items: list[OcrItem] = []
    if group_form == "split":
        items += [item("测试群", 320, 45, 420, 66),
                  item("(23)", 430, 45, 470, 66)]
    elif group_form == "merged":
        items += [item("测试群 (23)", 320, 45, 440, 66)]
    else:
        items += [item("测试联系人", 320, 45, 350, 66)]
    items += [
        item("发送", 815, 600, 850, 618),                    # send_btn 区域内
        item("测试回复", 680, 210, 780, 230),                # 右侧气泡(rel>0.53)
    ]
    return items


def _make_real_flows(scene: list[OcrItem], whitelist: list[str]):
    """真实 WeChatFlows + 离线替身(win/hi/ocr 全 stub,零窗口零键鼠)。"""
    hi = _StubHi()
    flows = WeChatFlows(
        _StubWin(), hi, lambda img, min_conf=0.5: list(scene),
        whitelist=list(whitelist))
    return flows, hi


def test_flows_group_send_blocked():
    """兜底红线:群真实发送在任何键鼠之前被硬禁(2026-10-24 审查修复:
    群名被误混入 safety.whitelist 且标题拆项成「群名」「(23)」两项时,
    原 _ocr_title 最左项==群名会骗过护栏②——现由护栏③全项群形态判定拦截)。
    四场景与审查复现一致,全部离线 stub。"""
    # 单元:标题栏群形态判定(_title_group_form,全项拼接)
    flows, _hi = _make_real_flows(_title_scene("split"), ["测试联系人"])
    assert flows._title_group_form(_title_scene("split")) is not None
    assert flows._title_group_form(_title_scene("merged")) is not None
    # 图标干扰项(纯符号)被过滤,不影响拼接判定
    junk = [item("测试群", 320, 45, 420, 66), item("⊙⋯", 500, 45, 540, 66),
            item("(23)", 430, 45, 470, 66)]
    assert flows._title_group_form(junk) is not None
    assert flows._title_group_form(_title_scene(None)) is None      # 私聊 测试联系人
    assert flows._title_group_form(
        [item("测试联系人", 320, 45, 350, 66), item("📞", 500, 45, 540, 66)]) is None

    # A. 群名在白名单 + 拆项标题 → 护栏③拦截,键鼠零执行(修复前此场景放行)
    flows_a, hi_a = _make_real_flows(_title_scene("split"), ["测试联系人", "测试群"])
    try:
        flows_a.send_message("测试", "测试群")
        raise AssertionError("拆项群标题未被群形态闸门拦截")
    except SafetyError as e:
        assert "群聊形态" in str(e), str(e)
    assert hi_a.actions == [], hi_a.actions

    # B. 群名在白名单 + 合并标题 → 护栏②(标题≠目标)拦截
    flows_b, hi_b = _make_real_flows(_title_scene("merged"), ["测试联系人", "测试群"])
    try:
        flows_b.send_message("测试", "测试群")
        raise AssertionError("合并群标题未被拦截")
    except SafetyError as e:
        assert "!=" in str(e), str(e)
    assert hi_b.actions == [], hi_b.actions

    # C. 群名不在白名单 → 护栏①拦截(原行为不变)
    flows_c, hi_c = _make_real_flows(_title_scene("split"), ["测试联系人"])
    try:
        flows_c.send_message("测试", "测试群")
        raise AssertionError("群名发送未被白名单闸门拦截")
    except SafetyError as e:
        assert "不在白名单" in str(e)
    assert hi_c.actions == [], hi_c.actions

    # D. 私聊 测试联系人 同配置(双开关语义由引擎层负责,flows 层)→ 正常走完键鼠
    flows_d, hi_d = _make_real_flows(_title_scene(None), ["测试联系人"])
    assert flows_d.send_message("测试回复", "测试联系人") is True
    assert any(a.startswith("click") for a in hi_d.actions)
    assert "paste('测试回复')" in hi_d.actions
    assert len([a for a in hi_d.actions if a.startswith("click")]) == 2

    # E. 引擎路径:双开关全开 + 群名混入 safety.whitelist(groups 默认全关)
    #    → send_now 走真实路径,在 send_message 护栏③被拦 → safety_block 事件
    from wxbot.engine.monitor import MonitorEngine
    cfg = AppConfig()
    cfg.engine.dry_run = False
    cfg.allow_real_send = True
    cfg.rate_limit.reply_delay_range_sec = (0.0, 0.0)
    cfg.rate_limit.per_contact_cooldown_sec = 0.0
    cfg.rate_limit.global_qpm = 100
    cfg.safety.whitelist = ["测试联系人", "测试群"]
    flows_e, hi_e = _make_real_flows(_title_scene("split"), ["测试联系人", "测试群"])
    eng = make_engine(cfg, flows_e)
    assert eng.send_now("测试群", "测试") is False
    blocks = [e for e in eng.events if e["type"] == "safety_block"]
    assert blocks and "群聊形态" in str(blocks[-1].get("reason", "")), blocks
    assert hi_e.actions == [], hi_e.actions          # 键鼠零执行
    return ("拆项/合并群标题均拦(全项群形态判定);白名单闸门不变;"
            "私聊 测试联系人 正常;引擎双开关全开+群名混入白名单也被拦,键鼠零执行")


# ---------------------------------------------------------------- 6. 服务端
def test_server_groups_config():
    """POST /api/config 群字段:字符串列表归一 + 校验 + 读回(WebUI 保存路径)。"""
    import json as _json
    import tempfile
    import warnings
    warnings.filterwarnings("ignore")
    from fastapi.testclient import TestClient
    from wxbot.server.events import EventBus
    from wxbot.server.main import create_app

    class StubEngine:
        started = 0
        stopped = 0

        def start(self):
            self.started += 1

        def stop(self):
            self.stopped += 1

        def status(self):
            return {"running": False, "whitelist": ["测试联系人"]}

        def send_now(self, contact, text):
            return True

    def make_client(td_name: str):
        """每个场景独立临时配置(POST /api/config 是与旧文件合并的累积语义)。"""
        cp = Path(td_name) / "config.json"
        app = create_app(StubEngine(), config_path=cp, bus=EventBus(buffer_size=10),
                         window_finder=lambda: None, enable_logging_bridge=False)
        return TestClient(app), cp

    with tempfile.TemporaryDirectory() as td1, \
            tempfile.TemporaryDirectory() as td2, \
            tempfile.TemporaryDirectory() as td3:
        # 场景 A:WebUI 形态(enabled + at + 名单/昵称用「每行一个」字符串)→ 归一读回
        c, cp = make_client(td1)
        r = c.post("/api/config", json={"groups": {
            "enabled": True, "reply_mode": "at",
            "whitelist": "测试群\n隔壁群", "bot_names": "助手, AI助手"}})
        assert r.status_code == 200, r.text
        got = c.get("/api/config").json()["config"]["groups"]
        # allow_real_send(2026-10-24 三闸门,默认 false)为新增字段
        assert got == {"enabled": True, "reply_mode": "at",
                       "whitelist": ["测试群", "隔壁群"],
                       "bot_names": ["助手", "AI助手"],
                       "allow_real_send": False}, got

        # 场景 B(全新配置):非法 reply_mode / enabled 而无名单 → 400 中文错误
        c2, _ = make_client(td2)
        r = c2.post("/api/config", json={"groups": {"reply_mode": "x"}})
        assert r.status_code == 400 and any("reply_mode" in e for e in r.json()["errors"])
        r = c2.post("/api/config", json={"groups": {"enabled": True}})
        assert r.status_code == 400 and any("whitelist" in e for e in r.json()["errors"])

        # 场景 C(全新配置):开启后把 groups 段从文件移除再保存其他段
        # → 服务端对缺段原样透传(既有语义),绝不处于开启态
        c3, cp3 = make_client(td3)
        assert c3.post("/api/config", json={"groups": {
            "enabled": True, "reply_mode": "at",
            "whitelist": ["测试群"]}}).status_code == 200
        raw = _json.loads(cp3.read_text(encoding="utf-8"))
        raw.pop("groups", None)
        cp3.write_text(_json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        assert c3.post("/api/config", json={"logging": {"level": "INFO"}}).status_code == 200
        got = c3.get("/api/config").json()["config"].get("groups") or {}
        assert got.get("enabled", False) is False and got.get("reply_mode", "off") == "off", got
    return "groups 列表归一/400 拦截/缺段绝不处于开启态(WebUI 保存路径)"


# ---------------------------------------------------------------- 汇总
def main() -> int:
    check("0", "config groups 段:默认全关/校验/旧配置兼容/roundtrip", test_config)
    check("1", "群 sender 提取(昵称行/前缀/回落)+ 私聊解析不受影响", test_sender_extract)
    check("2", "@ 提及检测(extract_at_targets)", test_at_detect)
    check("3", "reply_mode 三种路由 + 前置条件不满足一律忽略", test_reply_mode_router)
    check("4a", "引擎默认全关:群零交互零回复,私聊不受影响", test_engine_default_off)
    check("4b", "引擎 at/all 路由全链路(采集→@→agent→演练意图)", test_engine_group_flow)
    check("4c", "红线:双开关全开群回复仍只记意图不真实发送", test_engine_group_never_real_send)
    check("4d", "线程端到端(群+私聊混合)", test_engine_e2e_threads)
    check("4e", "旧版 flows(无 read_group_messages)采集回退", test_engine_collect_fallback)
    check("5", "flows 红线兜底:群真实发送硬禁(拆项/合并/白名单缺口/私聊正常)", test_flows_group_send_blocked)
    check("6", "服务端 /api/config 群字段归一与拦截(WebUI 保存路径)", test_server_groups_config)

    failed = [r for r in RESULTS if not r[1]]
    if failed:
        print(f"\n失败项: {[(i,) for i, _, _ in failed]}")
        print("GROUP_TEST_FAIL")
        return 1
    print("\n全部检查通过:纯离线(合成 OCR + stub),未触碰微信窗口、未执行键鼠、"
          "未向任何群/联系人发送任何内容。")
    print("GROUP_TEST_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
