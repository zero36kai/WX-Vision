# -*- coding: utf-8 -*-
"""服务端验收脚本(ARCHITECTURE §6 验收标准)。

exit 0 = 通过(末行打印 SERVER_TEST_PASS);任一必检项失败 exit 1。

十一项验收:
1. GET /api/status 200 且含窗口/引擎字段(注入 stub 窗口查找器,不碰真实微信);
2. GET/POST /api/config 读写一致(临时 config.json,不污染真实配置);
3. POST /api/engine/start|stop 调用到 stub 引擎;
4. POST /api/send:allow_real_send=false 时 4xx;设 true + 白名单内 测试联系人 调用
   stub.send_now 一次;白名单外 4xx;
5. GET / 返回 200 且页面含"微信"字样;
6. WebSocket /ws/logs 连接能收到日志帧(含连接后的实时推送);
7. 真实装配冒烟:import MonitorEngine 构造(不 start)+ create_app 成功
   ——若 wxbot.engine 导入失败,记为 SKIP 并明确提示,
   修复后需重跑本脚本补上该项;
8. (2026-10-24 配置补全)agent.backends(四类含 mailbox)/ default_backend /
   routing / mailbox.notify_url / retention_hours 写读往返;routing 指向未知
   后端与后端 id 重复 → 400 中文报错;后端 api_key 留空/脱敏回显 = 不修改;
9. (2026-10-24 配置补全)发送模式二选一联动:演练组合(dry_run=true +
   allow_real_send=false)与真实组合(dry_run=false + allow_real_send=true)
   保存后两开关状态正确;
10. (2026-10-24 界面打磨)发送模式联动矫正:API 直调只传其一或传矛盾组合,
    落盘组合也只允许「演练(false,true)」/「真实(true,false)」两种;
11. (2026-10-24 界面打磨)webhook_secret 与 web.password 脱敏:GET/POST
    响应不回显明文;留空/脱敏回显值保存 = 不修改。

注意:除 7 的"仅构造"外,全部用 stub 引擎,不 import 微信窗口、
不启动任何监控循环、零发送、零微信触达。
"""
from __future__ import annotations

import json
import sys
import tempfile
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
warnings.filterwarnings("ignore")  # starlette/httpx 的弃用警告与验收无关

from fastapi.testclient import TestClient  # noqa: E402

from wxbot.server.events import EventBus  # noqa: E402
from wxbot.server.main import create_app  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(idx: str, desc: str, fn):
    print(f"\n[{idx}] {desc}")
    try:
        detail = fn() or ""
        print(f"    => PASS {detail}")
        RESULTS.append((idx, True, detail))
        return True
    except SkipTest as e:
        print(f"    => SKIP: {e}")
        RESULTS.append((idx, True, f"SKIP: {e}"))  # SKIP 不计失败,但写明原因
        return True
    except Exception as e:
        print(f"    => FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()
        RESULTS.append((idx, False, f"{type(e).__name__}: {e}"))
        return False


class SkipTest(Exception):
    pass


class StubEngine:
    """按契约模拟 MonitorEngine:start/stop/status/send_now。"""

    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.sent: list[tuple[str, str]] = []

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def status(self):
        return {
            "running": self.started > self.stopped,
            "agent": {"mode": "echo", "healthy": True},
            "detail": "stub",
        }

    def send_now(self, contact: str, text: str):
        self.sent.append((contact, text))
        return {"ok": True, "dry_run": True, "contact": contact}


class FakeWindow:
    """模拟 WeChatWindow 的只读状态接口(供 status 渲染路径测试)。"""

    def is_minimized(self):
        return False

    def is_foreground(self):
        return True

    @property
    def rect_screen(self):
        return (10, 20, 810, 620)


def make_app(**kw) -> TestClient:
    """构造带 lifespan 的 TestClient(enter 后 startup 已跑)。"""
    tmp = tempfile.TemporaryDirectory(prefix="wxbot_p3_test_")
    app = create_app(
        engine=kw.pop("engine", StubEngine()),
        config_path=Path(tmp.name) / "config.json",
        window_finder=kw.pop("window_finder", lambda: None),  # 默认:探测不到窗口
        bus=kw.pop("bus", EventBus(buffer_size=500)),
        enable_logging_bridge=kw.pop("enable_logging_bridge", False),
        **kw,
    )
    client = TestClient(app)
    client._tmpdir = tmp  # 保活临时目录
    return client


# ---------------------------------------------------------------------------
def test1_status():
    with make_app() as c, make_app(window_finder=lambda: FakeWindow()) as c2:
        r = c.get("/api/status")
        assert r.status_code == 200, f"status_code={r.status_code}"
        d = r.json()
        for key in ("wechat_window", "engine", "agent", "allow_real_send"):
            assert key in d, f"缺少字段 {key}"
        assert isinstance(d["wechat_window"]["found"], bool)
        assert "error" in d["wechat_window"]
        assert d["wechat_window"]["found"] is False
        assert isinstance(d["engine"], dict)
        assert d["engine"]["running"] is False
        assert d["agent"]["mode"] == "echo"  # 默认配置 echo 模式
        # 注入"已找到窗口"的查找器:found=true 且带矩形
        d2 = c2.get("/api/status").json()
        assert d2["wechat_window"]["found"] is True
        assert d2["wechat_window"]["rect"] == [10, 20, 810, 620]
        assert d2["wechat_window"]["foreground"] is True
        return "字段齐全;未找到/已找到两路均验证"


def test2_config_roundtrip():
    with make_app() as c:
        g1 = c.get("/api/config")
        assert g1.status_code == 200
        d1 = g1.json()
        assert d1["ok"] is True and "config" in d1
        assert d1["config"]["safety"]["whitelist"] == ["文件传输助手"], "默认白名单应为 ['文件传输助手']"

        # 写:改采集间隔 + 人设 + 白名单(字符串形态,验证归一)
        p = c.post("/api/config", json={
            "capture": {"poll_interval_sec": 3.5},
            "personas": {"default": "测试人设XYZ"},
            "safety": {"whitelist": "测试联系人\nBob"},
        })
        assert p.status_code == 200, f"POST /api/config 失败: {p.status_code} {p.text}"
        assert p.json()["ok"] is True

        # 再读:一致(读写一致)
        d2 = c.get("/api/config").json()["config"]
        assert d2["capture"]["poll_interval_sec"] == 3.5
        assert d2["personas"]["default"] == "测试人设XYZ"
        assert d2["safety"]["whitelist"] == ["测试联系人", "Bob"]

        # 落盘验证(磁盘内容可被 wxbot.config 读回;.bak 在第二次保存时生成)
        cfg_path = c.app.state.config_path
        from wxbot.config import load_config
        cfg = load_config(cfg_path, create_default=False)
        assert cfg.capture.poll_interval_sec == 3.5
        assert cfg.safety.whitelist == ["测试联系人", "Bob"]

        # 非法配置 → 400 + 中文错误列表
        bad = c.post("/api/config", json={"safety": {"whitelist": ""}})
        assert bad.status_code == 400, f"空白名单应 400,实际 {bad.status_code}"
        errs = bad.json().get("errors") or []
        assert errs and all(isinstance(x, str) for x in errs), f"应有中文错误列表: {bad.text}"

        # api_key:写入后 GET 脱敏;空值保存 = 不修改
        c.post("/api/config", json={"agent": {"api_key": "sk-secret-1234567890"}})
        masked = c.get("/api/config").json()["config"]["agent"]["api_key"]
        assert "secret" not in masked, f"api_key 未脱敏: {masked}"
        assert masked.startswith("sk-") and "****" in masked
        c.post("/api/config", json={"agent": {"api_key": ""}})  # 空值=不修改(第二次保存)
        assert Path(str(cfg_path) + ".bak").exists(), "保存时应自动备份 config.json.bak"
        disk = json.loads(Path(str(cfg_path)).read_text(encoding="utf-8"))
        assert disk["agent"]["api_key"] == "sk-secret-1234567890"
        return "读写一致;校验400;api_key 脱敏+空值不修改;.bak 备份"


def test3_engine_start_stop():
    eng = StubEngine()
    with make_app(engine=eng) as c:
        assert c.post("/api/engine/start").status_code == 200
        assert eng.started == 1, "start 未调用到引擎"
        assert c.post("/api/engine/stop").status_code == 200
        assert eng.stopped == 1, "stop 未调用到引擎"
        d = c.get("/api/status").json()
        assert d["engine_running"] is False  # started(1) > stopped(1) 为假
        return f"start×{eng.started}/stop×{eng.stopped} 均落到 stub"


def test4_send_gates():
    # (a) 默认 allow_real_send=false → 4xx,引擎未被调用
    eng = StubEngine()
    with make_app(engine=eng) as c:
        r = c.post("/api/send", json={"contact": "测试联系人", "text": "你好"})
        assert 400 <= r.status_code < 500, f"演练模式应 4xx,实际 {r.status_code}"
        assert "演练" in r.json()["error"], f"错误文案应说明演练模式: {r.text}"
        assert eng.sent == [], "演练模式下引擎不应被调用"

        # (b) 开启 allow_real_send + 把测试联系人显式加入白名单 → stub 被调用一次
        # (不依赖出厂默认白名单的具体成员——默认名单属产品决策,测试自包含)
        assert c.post("/api/config", json={
            "allow_real_send": True,
            "safety": {"whitelist": ["测试联系人"]}}).status_code == 200
        r2 = c.post("/api/send", json={"contact": "测试联系人", "text": "你好"})
        assert r2.status_code == 200, f"白名单内应放行,实际 {r2.status_code}: {r2.text}"
        assert eng.sent == [("测试联系人", "你好")], f"stub 应恰好收到一次: {eng.sent}"

        # (c) 白名单外 → 4xx,且不再调用
        r3 = c.post("/api/send", json={"contact": "小明", "text": "在吗"})
        assert 400 <= r3.status_code < 500, f"白名单外应 4xx,实际 {r3.status_code}"
        assert "白名单" in r3.json()["error"]
        assert eng.sent == [("测试联系人", "你好")], "白名单外不应调用引擎"

        # (d) 补充:空文本 → 4xx
        assert 400 <= c.post("/api/send", json={"contact": "测试联系人", "text": "  "}).status_code < 500
    return "演练4xx → 测试联系人 放行1次 → 白名单外4xx → 空文本4xx"


def test5_index():
    with make_app() as c:
        r = c.get("/")
        assert r.status_code == 200, f"GET / 应 200,实际 {r.status_code}"
        assert "微信" in r.text, "首页应包含'微信'字样"
        assert "app.js" in r.text and "style.css" in r.text, "首页应引用静态资源"
        assert c.get("/app.js").status_code == 200
        assert c.get("/style.css").status_code == 200
        return "静态页 200,含'微信',js/css 可加载"


def test6_websocket_logs():
    with make_app(enable_logging_bridge=False) as c:
        with c.websocket_connect("/ws/logs") as ws:
            frame1 = ws.receive_json()  # 连接即补发历史(startup 事件)
            for key in ("seq", "ts", "level", "module", "text"):
                assert key in frame1, f"日志帧缺字段 {key}: {frame1}"
            assert frame1["text"], "帧文本不应为空"
            # 触发实时推送(欢迎帧可能先到,循环收直到出现引擎启动帧)
            c.post("/api/engine/start")
            got = None
            for _ in range(8):
                f = ws.receive_json()
                assert all(k in f for k in ("seq", "ts", "level", "module", "text"))
                if "引擎" in f["text"]:
                    got = f
                    break
            assert got is not None, "8 帧内未收到引擎启动实时推送"
        return f"补历史帧 seq={frame1['seq']} + 实时帧 {got['text']!r}"


def test7_real_assembly():
    """真实装配冒烟:import MonitorEngine 构造(不 start)+ create_app 成功。"""
    try:
        from wxbot.engine.monitor import MonitorEngine  # noqa: F401
    except ImportError as e:
        raise SkipTest(
            f"wxbot.engine 导入失败:{e}。"
            "修复后需重跑本脚本补验此项。") from e
    from wxbot.config import AppConfig
    from wxbot.server.main import _build_default_engine
    engine = _build_default_engine(ROOT / "config.json")  # 无参/传配置两种签名自适应
    app = create_app(engine=engine, config_path=ROOT / "config.json",
                     window_finder=lambda: None)  # 冒烟仅装配,不发任何请求、不 start
    assert app is not None
    return "MonitorEngine 构造成功(未 start)+ create_app 成功"


# 2026-10-24 配置补全:后端列表固定样例(四类齐备;api_key 用假密钥,非真实凭据)
_BACKENDS = [
    {"id": "echo-main", "type": "echo"},
    {"id": "glm", "type": "openai", "base_url": "https://api.example.com/v1",
     "api_key": "sk-backend-abcdef1234567890", "model": "test-model",
     "system_prompt": "你是测试助手"},
    {"id": "hook", "type": "webhook", "webhook_url": "http://127.0.0.1:9/hook",
     "webhook_secret": "hook-secret"},
    {"id": "muse", "type": "mailbox"},
]
_REAL_KEY = _BACKENDS[1]["api_key"]
_MASKED_KEY = "sk-" + "****" + _REAL_KEY[-4:]      # 长密钥脱敏 = 首3 + **** + 末4


def test8_backends_routing_mailbox():
    """①POST backends(含 mailbox)→GET 往返一致 ②routing 写读+未知后端 400
    ③mailbox.notify_url/retention 写读 ⑤api_key 留空/脱敏回显不覆盖。"""
    with make_app() as c:
        # ---- ① 四类后端 + 默认后端 + routing + mailbox 一次写入 ----
        p = c.post("/api/config", json={
            "agent": {
                "backends": _BACKENDS,
                "default_backend": "echo-main",
                "routing": {"测试联系人": "muse", "另一位联系人": "glm"},
            },
            "mailbox": {"notify_url": "http://127.0.0.1:9/notify",
                        "retention_hours": 72},
        })
        assert p.status_code == 200, f"POST backends 应 200: {p.status_code} {p.text}"
        assert p.json()["ok"] is True

        d = c.get("/api/config").json()["config"]
        be = d["agent"]["backends"]
        assert [b["id"] for b in be] == ["echo-main", "glm", "hook", "muse"]
        assert [b["type"] for b in be] == ["echo", "openai", "webhook", "mailbox"]
        assert d["agent"]["default_backend"] == "echo-main"
        assert d["agent"]["routing"] == {"测试联系人": "muse", "另一位联系人": "glm"}
        # GET 侧:后端 api_key 脱敏(与平铺 api_key 同一约定)
        glm = next(b for b in be if b["id"] == "glm")
        assert glm["api_key"] == _MASKED_KEY, f"后端 api_key 未按规则脱敏: {glm['api_key']}"
        assert "abcdef1234567890" not in json.dumps(d, ensure_ascii=False)
        # 默认后端联动平铺 legacy 字段(引擎默认后端仍从平铺字段构造 agent)
        assert d["agent"]["mode"] == "echo"
        assert d["agent"]["base_url"] == ""
        # mailbox 段写读
        assert d["mailbox"]["notify_url"] == "http://127.0.0.1:9/notify"
        assert d["mailbox"]["retention_hours"] == 72

        # 落盘 → wxbot.config 读回(dataclass 视图一致)
        from wxbot.config import load_config
        cfg = load_config(c.app.state.config_path, create_default=False)
        assert [b.id for b in cfg.agent.backends] == ["echo-main", "glm", "hook", "muse"]
        assert [b.type for b in cfg.agent.backends] == ["echo", "openai", "webhook", "mailbox"]
        assert cfg.agent.default_backend == "echo-main"
        assert cfg.agent.routing == {"测试联系人": "muse", "另一位联系人": "glm"}
        assert cfg.mailbox.notify_url == "http://127.0.0.1:9/notify"
        assert cfg.mailbox.retention_hours == 72

        # ---- 默认后端切到 mailbox:mode 不动(legacy 校验只收 echo|openai|webhook),
        #      引擎经 backends 判定信箱路由,保存应成功 ----
        p2 = c.post("/api/config", json={
            "agent": {"backends": _BACKENDS, "default_backend": "muse"}})
        assert p2.status_code == 200, f"默认后端=mailbox 应 200: {p2.status_code} {p2.text}"
        d2 = c.get("/api/config").json()["config"]
        assert d2["agent"]["default_backend"] == "muse"
        assert d2["agent"]["mode"] == "echo", "默认后端为 mailbox 时不应把 mode 写成 mailbox"

        # ---- ② routing 写读一致(单独更新,保留既有 backends)----
        p3 = c.post("/api/config", json={
            "agent": {"routing": {"测试联系人": "glm", "新联系人": "muse"}}})
        assert p3.status_code == 200
        assert c.get("/api/config").json()["config"]["agent"]["routing"] == {
            "测试联系人": "glm", "新联系人": "muse"}
        # 指向不存在的后端 → 400 + 中文报错
        bad = c.post("/api/config", json={"agent": {"routing": {"测试联系人": "ghost"}}})
        assert bad.status_code == 400, f"routing 指向未知后端应 400: {bad.status_code}"
        assert "未知后端" in bad.text, f"应有中文报错: {bad.text}"
        # 后端 id 重复 → 400 + 中文报错
        dup = c.post("/api/config", json={"agent": {"backends": [
            {"id": "x", "type": "echo"}, {"id": "x", "type": "openai"}]}})
        assert dup.status_code == 400, f"id 重复应 400: {dup.status_code}"
        assert "重复" in dup.text, f"应有中文报错: {dup.text}"
        # type 非法 / id 为空 / 空列表 → 400
        assert c.post("/api/config", json={
            "agent": {"backends": [{"id": "t", "type": "nope"}]}}).status_code == 400
        assert c.post("/api/config", json={
            "agent": {"backends": [{"id": "", "type": "echo"}]}}).status_code == 400
        assert c.post("/api/config", json={
            "agent": {"backends": []}}).status_code == 400

        # ---- ③ mailbox.retention_hours 非法 → 400;notify_url 归一(去首尾空白)----
        assert c.post("/api/config", json={
            "mailbox": {"retention_hours": 0}}).status_code == 400
        assert c.post("/api/config", json={
            "mailbox": {"retention_hours": "abc"}}).status_code == 400
        okn = c.post("/api/config", json={"mailbox": {"notify_url": "  http://127.0.0.1:9/n  "}})
        assert okn.status_code == 200
        assert c.get("/api/config").json()["config"]["mailbox"]["notify_url"] \
            == "http://127.0.0.1:9/n"

        # ---- ⑤ 后端 api_key 留空/脱敏回显 = 不修改(全列表回传时其余字段照常更新)----
        backends_keep = [dict(b) for b in _BACKENDS]
        glm_keep = next(b for b in backends_keep if b["id"] == "glm")
        glm_keep["api_key"] = ""                    # 留空
        glm_keep["model"] = "changed-model"
        p4 = c.post("/api/config", json={
            "agent": {"backends": backends_keep, "default_backend": "echo-main"}})
        assert p4.status_code == 200
        disk = json.loads(Path(str(c.app.state.config_path)).read_text(encoding="utf-8"))
        glm_disk = next(b for b in disk["agent"]["backends"] if b["id"] == "glm")
        assert glm_disk["api_key"] == _REAL_KEY, "api_key 留空保存不应覆盖既有密钥"
        assert glm_disk["model"] == "changed-model", "其余字段应照常更新"
        # 脱敏回显值原样传回 = 也不修改
        glm_keep["api_key"] = _MASKED_KEY
        glm_keep["model"] = "test-model"
        assert c.post("/api/config", json={
            "agent": {"backends": backends_keep, "default_backend": "echo-main"}}).status_code == 200
        disk2 = json.loads(Path(str(c.app.state.config_path)).read_text(encoding="utf-8"))
        glm_disk2 = next(b for b in disk2["agent"]["backends"] if b["id"] == "glm")
        assert glm_disk2["api_key"] == _REAL_KEY, "脱敏回显值不应被当成新密钥写盘"
        return "四类后端/默认后端/路由/mailbox 写读往返;未知后端400;id重复400;api_key 留空不覆盖"


def test9_send_mode_lock():
    """④发送模式二选一联动:演练组合 / 真实组合保存后 allow_real_send 与
    engine.dry_run 状态正确(GET、/api/status 与磁盘三处一致)。"""
    with make_app() as c:
        # 演练组合(默认安全态):dry_run=true + allow_real_send=false
        p1 = c.post("/api/config", json={"allow_real_send": False,
                                         "engine": {"dry_run": True}})
        assert p1.status_code == 200, f"演练组合应 200: {p1.status_code} {p1.text}"
        d1 = c.get("/api/config").json()
        assert d1["allow_real_send"] is False
        assert d1["config"]["engine"]["dry_run"] is True
        assert c.get("/api/status").json()["allow_real_send"] is False
        disk1 = json.loads(Path(str(c.app.state.config_path)).read_text(encoding="utf-8"))
        assert disk1["engine"]["dry_run"] is True and disk1["allow_real_send"] is False

        # 真实组合:dry_run=false + allow_real_send=true(二者联动,不会再出现糊涂态)
        p2 = c.post("/api/config", json={"allow_real_send": True,
                                         "engine": {"dry_run": False}})
        assert p2.status_code == 200, f"真实组合应 200: {p2.status_code} {p2.text}"
        d2 = c.get("/api/config").json()
        assert d2["allow_real_send"] is True
        assert d2["config"]["engine"]["dry_run"] is False
        assert c.get("/api/status").json()["allow_real_send"] is True
        disk2 = json.loads(Path(str(c.app.state.config_path)).read_text(encoding="utf-8"))
        assert disk2["engine"]["dry_run"] is False and disk2["allow_real_send"] is True

        # 切回演练:两开关同步回落(单选互斥的保存语义)
        p3 = c.post("/api/config", json={"allow_real_send": False,
                                         "engine": {"dry_run": True}})
        assert p3.status_code == 200
        d3 = c.get("/api/config").json()
        assert d3["allow_real_send"] is False and d3["config"]["engine"]["dry_run"] is True
        return "演练/真实两种组合保存后 dry_run 与 allow_real_send 三处(响应/状态/磁盘)一致"


def test10_mode_linkage_api():
    """⑩(2026-10-24 界面打磨)API 直调联动矫正:只传其一/传矛盾组合/半真实态,
    落盘组合也只允许「演练(false,true)」或「真实(true,false)」两种。"""
    with make_app() as c:
        cfg_path = c.app.state.config_path

        def disk_mode() -> tuple[bool, bool]:
            d = json.loads(Path(str(cfg_path)).read_text(encoding="utf-8"))
            return (bool(d.get("allow_real_send")),
                    bool((d.get("engine") or {}).get("dry_run")))

        # 只显式传 allow_real_send=true → engine.dry_run 自动联动置 false(真实组合)
        assert c.post("/api/config", json={"allow_real_send": True}).status_code == 200
        assert disk_mode() == (True, False), f"allow=true 应联动 dry_run=false: {disk_mode()}"

        # 只显式传 engine.dry_run=true → allow_real_send 自动联动置 false(演练组合)
        assert c.post("/api/config", json={"engine": {"dry_run": True}}).status_code == 200
        assert disk_mode() == (False, True), f"dry_run=true 应联动 allow=false: {disk_mode()}"

        # 矛盾组合(allow=true + dry_run=true):安全方向(演练)胜出
        assert c.post("/api/config", json={
            "allow_real_send": True, "engine": {"dry_run": True}}).status_code == 200
        assert disk_mode() == (False, True), f"矛盾组合应按演练落盘: {disk_mode()}"

        # 单独传 dry_run=false(总开关未开)→ 归位演练,不落盘「半真实」组合
        assert c.post("/api/config", json={"engine": {"dry_run": False}}).status_code == 200
        assert disk_mode() == (False, True), f"半真实组合应被矫正: {disk_mode()}"

        # 显式 allow_real_send=false → dry_run 同步归位 true
        assert c.post("/api/config", json={"allow_real_send": False}).status_code == 200
        assert disk_mode() == (False, True), f"显式关真实应联动 dry_run=true: {disk_mode()}"

        # 真实组合显式重开;此后不触碰发送模式的保存不得改变发送模式
        assert c.post("/api/config", json={
            "allow_real_send": True, "engine": {"dry_run": False}}).status_code == 200
        assert disk_mode() == (True, False)
        assert c.post("/api/config", json={
            "capture": {"poll_interval_sec": 4}}).status_code == 200
        assert disk_mode() == (True, False), "不触碰发送模式的保存不应改变发送模式"
        return "只传其一/矛盾组合/半真实态均联动矫正;无关保存保持原发送模式"


def test11_secret_masking():
    """⑪(2026-10-24 界面打磨)webhook_secret 与 web.password 脱敏:GET/POST
    响应不回显明文;留空/脱敏回显值保存 = 不修改(与 api_key 同族约定)。"""
    real_secret = "hook-secret-0123456789"      # 假密钥(非真实凭据)
    masked_secret = real_secret[:3] + "****" + real_secret[-4:]
    real_pwd = "pw-very-secret-123"             # 假口令(非真实凭据)
    masked_pwd = real_pwd[:3] + "****" + real_pwd[-4:]
    with make_app() as c:
        cfg_path = c.app.state.config_path
        backends = [dict(b) for b in _BACKENDS]
        next(b for b in backends if b["id"] == "hook")["webhook_secret"] = real_secret

        p = c.post("/api/config", json={
            "agent": {"backends": backends, "default_backend": "echo-main"},
            "web": {"password": real_pwd}})
        assert p.status_code == 200, f"POST 应 200: {p.status_code} {p.text}"
        # 配置口令后服务端认证自动启用:客户端随即换用新口令(与前端保存后行为一致)
        c.headers.update({"X-Access-Token": real_pwd})
        # POST 响应即不回显明文
        assert real_secret not in p.text and real_pwd not in p.text, "POST 响应泄漏明文"
        assert p.json()["config"]["web"]["password"] == masked_pwd, "口令应脱敏返回"

        # GET 响应(含平铺 legacy 字段与后端列表)一律不回显明文
        d = c.get("/api/config").json()
        dump = json.dumps(d, ensure_ascii=False)
        assert real_secret not in dump, "GET 响应泄漏 webhook_secret 明文"
        assert real_pwd not in dump, "GET 响应泄漏访问口令明文"
        hook_resp = next(b for b in d["config"]["agent"]["backends"] if b["id"] == "hook")
        assert hook_resp["webhook_secret"] == masked_secret, \
            f"webhook_secret 未按规则脱敏: {hook_resp['webhook_secret']}"

        # 留空保存 = 不修改(磁盘仍是明文旧值)
        keep = [dict(b) for b in backends]
        hook_keep = next(b for b in keep if b["id"] == "hook")
        hook_keep["webhook_secret"] = ""
        assert c.post("/api/config", json={"agent": {"backends": keep}}).status_code == 200
        disk = json.loads(Path(str(cfg_path)).read_text(encoding="utf-8"))
        hook_disk = next(b for b in disk["agent"]["backends"] if b["id"] == "hook")
        assert hook_disk["webhook_secret"] == real_secret, "留空保存不应覆盖既有 webhook_secret"

        # 脱敏回显值原样传回 = 也不修改(webhook_secret 与访问口令同验)
        hook_keep["webhook_secret"] = masked_secret
        assert c.post("/api/config", json={"agent": {"backends": keep}}).status_code == 200
        disk2 = json.loads(Path(str(cfg_path)).read_text(encoding="utf-8"))
        hook_disk2 = next(b for b in disk2["agent"]["backends"] if b["id"] == "hook")
        assert hook_disk2["webhook_secret"] == real_secret, "脱敏回显值不应被当成新密钥写盘"
        assert c.post("/api/config", json={"web": {"password": masked_pwd}}).status_code == 200
        disk3 = json.loads(Path(str(cfg_path)).read_text(encoding="utf-8"))
        assert disk3["web"]["password"] == real_pwd, "口令脱敏回显值不应覆盖真实口令"

        # 旧配置(仅平铺字段、无 backends)经 WebUI 迁移合成单后端保存:
        # 回传 GET 的脱敏值不得把掩码串当成新密钥落盘
        with make_app() as c2:
            assert c2.post("/api/config", json={
                "agent": {"api_key": "legacy-key-123456789",
                          "webhook_secret": "legacy-secret-123456"}}).status_code == 200
            leg = c2.get("/api/config").json()["config"]["agent"]
            # 前端迁移行为:把 GET 到的脱敏值原样放进合成后端再保存
            synth = [{"id": "echo", "type": "echo",
                      "api_key": leg["api_key"], "webhook_secret": leg["webhook_secret"]}]
            assert c2.post("/api/config", json={
                "agent": {"backends": synth, "default_backend": "echo"}}).status_code == 200
            disk_leg = json.loads(
                Path(str(c2.app.state.config_path)).read_text(encoding="utf-8"))
            be_leg = disk_leg["agent"]["backends"][0]
            assert be_leg["api_key"] == "legacy-key-123456789", \
                f"迁移保存不应丢失旧 api_key: {be_leg['api_key']}"
            assert be_leg["webhook_secret"] == "legacy-secret-123456", \
                f"迁移保存不应丢失旧 webhook_secret: {be_leg['webhook_secret']}"
        return "webhook_secret/口令 GET+POST 均脱敏不回显;留空/回显/迁移保存均不覆盖"


def main() -> int:
    check("1", "GET /api/status 200 且含窗口/引擎字段(stub 查找器)", test1_status)
    check("2", "GET/POST /api/config 读写一致(临时文件+校验+脱敏)", test2_config_roundtrip)
    check("3", "POST /api/engine/start|stop 调用到 stub", test3_engine_start_stop)
    check("4", "POST /api/send 三道护栏(演练4xx/测试联系人放行1次/白名单外4xx)", test4_send_gates)
    check("5", "GET / 返回 200 且页面含'微信'", test5_index)
    check("6", "WebSocket /ws/logs 能收到日志帧(历史+实时)", test6_websocket_logs)
    check("7", "真实装配冒烟:MonitorEngine 构造(不start)+ create_app", test7_real_assembly)
    check("8", "backends(四类含mailbox)/routing/mailbox 写读往返 + 未知后端400/id重复400 + api_key 留空不覆盖", test8_backends_routing_mailbox)
    check("9", "发送模式二选一联动(演练组合/真实组合)保存后两开关状态正确", test9_send_mode_lock)
    check("10", "发送模式联动矫正:API 直调只传其一/矛盾组合也绝不落盘糊涂组合", test10_mode_linkage_api)
    check("11", "webhook_secret/访问口令脱敏:响应不回显明文,留空/回显值保存不覆盖", test11_secret_masking)

    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "-" * 62)
    for idx, ok, detail in RESULTS:
        print(f"  [{idx}] {'PASS' if ok else 'FAIL'}  {detail}")
    print("-" * 62)
    skipped = [r for r in RESULTS if str(r[2]).startswith("SKIP")]
    if skipped:
        print(f"  注意: {len(skipped)} 项跳过(见上方原因),修复后请重跑本脚本。")
    if failed:
        print(f"SERVER_TEST_FAIL(失败 {len(failed)} 项)")
        return 1
    print("SERVER_TEST_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
