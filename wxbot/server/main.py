# -*- coding: utf-8 -*-
"""FastAPI 服务工厂:create_app(engine) 依赖注入,路由 + 静态页 + WebSocket。

契约依据 docs/ARCHITECTURE.md §3/§5 与 docs/research/03-agent-webui.md §3:
- 引擎通过参数注入(MonitorEngine: start/stop/status/send_now),测试可传
  stub;engine=None 时按真实引擎装配(run.py 也走这条默认路);
- 手动发送受**双重护栏**拦截(ARCHITECTURE §0 红线):
  1) config.allow_real_send(默认 false)= false 时,一律 403 拒绝,
     中文错误明确告知"演练模式";
  2) 目标联系人必须在 safety.whitelist(默认 ["文件传输助手"])内,否则 403;
  3) 通过后仍以关键字参数调用 engine.send_now(contact=..., text=...),
     引擎侧另有 dry_run 与标题栏 OCR 复核(引擎实现)。
- 配置读写基于 wxbot.config 的 dataclass 校验;allow_real_send 作为
  config.json 顶层键持久化(config.py 属配置模块独占,此处不改其 dataclass,
  读侧同时兼容该键出现在 safety/send 组内的写法)。
- 2026-10-24 界面打磨:/api/config 的 GET/POST 响应对 api_key、
  webhook_secret、web.password 统一脱敏(留空/脱敏回显保存 = 不修改);
  POST 显式传 allow_real_send / engine.dry_run 时强制发送模式联动矫正,
  「又演练又真实」的矛盾组合绝不落盘。
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from wxbot.config import AgentBackendConfig, AppConfig, load_config
from wxbot.server.events import EventBus, make_pump

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEB_DIR = PROJECT_ROOT / "web"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"

WINDOW_CACHE_TTL = 2.0          # 微信窗口状态缓存秒数(EnumWindows 只读,仍避免高频)
MAX_SEND_CHARS = 2000           # 手动发送长度上限(防呆:超长文本键鼠粘贴易卡死)

# config.json 中"允许真实发送"的读取顺序:顶层 → safety 组 → send 组(旧版兼容)
_ALLOW_SEND_PATHS = (("allow_real_send",), ("safety", "allow_real_send"), ("send", "allow_real_send"))

# 引擎业务层安全拒绝(wxbot.engine.safety.SafetyRejected);缺省容忍 stub
try:
    from wxbot.engine.safety import SafetyRejected as _SafetyRejected
    _REJECT_EXCS: tuple[type[Exception], ...] = (_SafetyRejected,)
except ImportError:  # pragma: no cover
    _SafetyRejected = None
    _REJECT_EXCS = ()


# ---------------------------------------------------------------------------
# 引擎默认装配(engine=None 时的生产路径)
# ---------------------------------------------------------------------------
# 引擎结构化事件 → WebUI 文案(小白可读);未列出的事件走通用格式
_ENGINE_EVENT_TEXT = {
    "engine_start": ("引擎已启动,开始监控白名单联系人", "INFO"),
    "engine_stop": ("引擎已停止", "INFO"),
    "inbox": ("收到新消息:{contact} ← {sender}:{msg_text}", "INFO"),
    "duplicate": ("重复消息已去重:{contact}", "DEBUG"),
    "rejected": ("白名单外联系人,已忽略:{contact}", "WARNING"),
    "ignored": ("命中忽略词,已忽略:{contact}", "INFO"),
    "auto_reply_disabled": ("自动回复未开启,仅记录:{contact}", "INFO"),
    "agent_call": ("调用智能体生成回复:{contact}(后端 {agent})", "INFO"),
    "agent_none": ("智能体选择不回复:{contact}", "INFO"),
    "reply_truncated": ("智能体回复超长,已截断:{contact}", "WARNING"),
    "rate_limited": ("限频拦截:{contact}({reason})", "WARNING"),
    "reply_delay": ("拟人延迟 {delay}s 后发送:{contact}", "DEBUG"),
    "safety_block": ("安全拦截:{contact}({reason})", "ERROR"),
    "send_intent": ("演练模式·发送意图(未真实发送):{contact} ← {msg_text}", "INFO"),
    "sent": ("已发送:{contact} ← {msg_text}", "INFO"),
    "send_failed": ("发送失败:{contact}(阶段 {stage})", "ERROR"),
    "send_now": ("手动发送请求已受理:{contact}", "INFO"),
    "open_chat_failed": ("打开聊天窗口失败:{contact}", "WARNING"),
    "unread_skipped": ("有未读但不在白名单,已跳过:{contacts}", "DEBUG"),
    "group_ignored": ("群聊消息未触发回复:{contact}(原因 {reason})", "INFO"),
    "mailbox_ingest": ("信箱入件(等 Muse 处理):{contact} ← {sender}:{msg_text}", "INFO"),
    "error": ("引擎错误({where}):{message}", "ERROR"),
}


def _engine_event_bridge(bus: EventBus) -> Callable[[dict], None]:
    """把引擎的 on_event(dict) 回调桥接到 WebUI 事件总线。

    引擎从工作线程调用本回调;EventBus.publish 自带跨线程投递与
    慢客户端丢帧,绝不阻塞引擎线程。"""
    def _on_event(ev: dict) -> None:
        try:
            ev_type = str(ev.get("type", "event"))
            tpl, level = _ENGINE_EVENT_TEXT.get(ev_type, (None, "INFO"))
            data = {k: v for k, v in ev.items()
                    if k not in ("ts", "type", "text") and v is not None}
            data = {k: (v if isinstance(v, (str, int, float, bool)) else str(v))
                    for k, v in data.items()}
            if ev.get("text"):
                data["msg_text"] = str(ev["text"])[:80]
            if tpl:
                text = tpl.format(**{**{k: "" for k in (
                    "contact", "sender", "reason", "stage", "where", "message",
                    "agent", "delay", "contacts")}, **data})
            else:
                text = f"[{ev_type}] {json.dumps(data, ensure_ascii=False)[:160]}"
            bus.publish(text, level=level, module="engine", ev_type=ev_type, **data)
        except Exception:
            pass  # 事件桥接失败绝不影响引擎线程
    return _on_event


def _build_default_engine(config_path: str | Path = DEFAULT_CONFIG_PATH,
                          bus: Optional[EventBus] = None):
    """导入并构造 MonitorEngine(不 start)。

    实际签名为 MonitorEngine(cfg, *, flows_factory=None, agent=None,
    on_event=None):cfg 必传;此处按宽容顺序尝试,并把引擎事件桥接到总线。
    """
    try:
        from wxbot.engine.monitor import MonitorEngine
    except ImportError as e:
        raise RuntimeError(
            "未找到引擎模块 wxbot.engine.monitor。"
            "请确认代码完整;测试场景请通过 create_app(engine=stub) 注入桩引擎。"
        ) from e
    cfg = load_config(config_path)  # 校验失败会抛 ValueError(中文信息)
    attempts = [
        lambda: (MonitorEngine(cfg, on_event=_engine_event_bridge(bus))
                 if bus is not None else MonitorEngine(cfg)),
        lambda: MonitorEngine(cfg),
        lambda: MonitorEngine(),
    ]
    last_err: Optional[TypeError] = None
    for make in attempts:
        try:
            return make()
        except TypeError as e:
            last_err = e
    raise RuntimeError(
        f"MonitorEngine 构造签名不兼容(既不接受 AppConfig 也不无参): {last_err}")


def _sync_engine_safety(engine: Any, raw_cfg: dict) -> list[str]:
    """配置保存后,把红线相关的两个开关即时同步到运行中的引擎
    (SafetyGate 的公开属性,见 monitor.py __init__ 注释):
    dry_run / allow_real_send 立即生效;白名单等其余参数需重启引擎。"""
    synced: list[str] = []
    try:
        cfg = AppConfig.from_dict(raw_cfg)
        if hasattr(engine, "gate"):
            engine.gate.allow_real_send = cfg.allow_real_send
            engine.gate.dry_run = cfg.engine.dry_run
            synced = ["engine.dry_run", "allow_real_send"]
    except Exception:
        pass
    return synced


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _read_raw_config(config_path: Path) -> dict:
    """读 config.json 原始 dict(缺文件返回 {});坏 JSON 也返回 {} 并由调用方告警。"""
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _allow_real_send(raw: dict) -> bool:
    for path in _ALLOW_SEND_PATHS:
        node: Any = raw
        try:
            for key in path:
                node = node[key]
            if isinstance(node, bool):
                return node
        except (KeyError, TypeError):
            continue
    return False  # 默认 false:未开启允许真实发送前严禁真实发送(红线)


def _managed_group_allowed(raw: dict, contact: str) -> bool:
    """受管群三闸门(2026-10-24;README §4.2 / ARCHITECTURE 红线1):

    groups.enabled + 群∈groups.whitelist + groups.allow_real_send 全部满足时,
    /api/send 对受管群放行(群名按 README §4.2 指引**绝不**加入联系人白名单,
    故白名单闸门须对三闸全开的受管群豁免)。任一不满足 → False(fail-closed,
    与旧「只允许联系人白名单」行为一致)。群名匹配去空白归一。"""
    g = raw.get("groups") if isinstance(raw, dict) else None
    if not isinstance(g, dict) or not (g.get("enabled") and g.get("allow_real_send")):
        return False
    target = "".join(str(contact or "").split())
    if not target:
        return False
    for name in _normalize_list_field(g.get("whitelist") or []):
        if "".join(str(name).split()) == target:
            return True
    return False


def _mask_secret(key: Any) -> str:
    """单个密钥的脱敏规则(03 文档 §3.2:≤8 位显示 ****,长密钥保留首3末4)。"""
    key = str(key or "")
    if len(key) <= 8:
        return "****" if key else ""
    return key[:3] + "****" + key[-4:]


def _mask_secrets(raw: dict) -> dict:
    """密钥/口令家族统一脱敏(03 文档 §3.2:≤8 位显示 ****,长密钥保留首3末4)。

    2026-10-24 界面打磨扩展(原 _mask_api_key,现覆盖全部敏感字段):
    - agent.api_key(平铺)与 agent.backends[].api_key
    - agent.webhook_secret(平铺)与 agent.backends[].webhook_secret
    - web.password(访问口令)
    三者沿用同一「留空 / 脱敏回显值 = 不修改」的保存约定;只影响 /api/config
    的响应展示,不改变落盘内容。"""
    out = copy.deepcopy(raw)
    agent = out.get("agent")
    if isinstance(agent, dict):
        agent["api_key"] = _mask_secret(agent.get("api_key"))
        agent["webhook_secret"] = _mask_secret(agent.get("webhook_secret"))
        backends = agent.get("backends")
        if isinstance(backends, list):
            for be in backends:
                if not isinstance(be, dict):
                    continue
                if "api_key" in be:
                    be["api_key"] = _mask_secret(be.get("api_key"))
                if "webhook_secret" in be:
                    be["webhook_secret"] = _mask_secret(be.get("webhook_secret"))
    web = out.get("web")
    if isinstance(web, dict) and "password" in web:
        web["password"] = _mask_secret(web.get("password"))
    return out


def _normalize_list_field(value: Any) -> list[str]:
    """白名单/忽略词的防呆归一:接受 list 或"每行一个/逗号分隔"的字符串。"""
    if isinstance(value, str):
        parts: list[str] = []
        for chunk in value.replace(",", "\n").splitlines():
            chunk = chunk.strip()
            if chunk:
                parts.append(chunk)
        return parts
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def _window_status(window_finder: Callable[[], Any], cache: dict) -> dict:
    """微信窗口状态(只读探测:find/IsIconic/GetForegroundWindow,不激活不截屏)。"""
    now = time.monotonic()
    if cache.get("at") and now - cache["at"] < WINDOW_CACHE_TTL:
        return cache["value"]
    info: dict[str, Any] = {
        "found": False, "minimized": None, "foreground": None,
        "rect": None, "error": None,
    }
    try:
        win = window_finder()
        if win is None:
            info["error"] = "未找到微信主窗口(class=Qt51514QWindowIcon 且 title=微信)。请登录微信。"
        else:
            info["found"] = True
            info["minimized"] = bool(win.is_minimized())
            info["foreground"] = bool(win.is_foreground())
            info["rect"] = list(win.rect_screen)
    except Exception as e:
        info["error"] = f"窗口探测失败: {type(e).__name__}: {e}"
    cache["at"] = now
    cache["value"] = info
    return info


# ---------------------------------------------------------------------------
# 访问口令认证(WebUI 口令保护 + Cloudflare 隧道远程访问的唯一门禁)
# ---------------------------------------------------------------------------
AUTH_TOKEN_HEADER = "X-Access-Token"          # HTTP 请求头名;WebSocket 用查询参数 ?token=
AUTH_WS_CLOSE_CODE = 4401                     # WS 口令错误时的自定义关闭码(前端据此弹登录框)
AUTH_REQUIRED_TEXT = (
    "需要访问口令:请在请求头携带 " + AUTH_TOKEN_HEADER +
    "(WebSocket 用查询参数 ?token=)。口令见服务启动窗口的醒目提示或 config.json"
    " 的 web.password 字段,可在 WebUI「④ 配置」页修改。"
)


def ensure_web_password(config_path: str | Path) -> tuple[str, bool]:
    """确保 web.password 非空;为空则生成随机口令(≥12 位)并写回 config.json。

    服务启动(run.py)调用:返回 (口令, 是否本次新生成)。已配置时原样返回、
    不动文件。写盘前校验合并结果,并按项目惯例把旧文件备份为 config.json.bak。
    """
    path = Path(config_path)
    raw = _read_raw_config(path) if path.exists() else {}
    if not raw:                                  # 缺文件:写一份默认配置再补口令
        raw = AppConfig().to_dict()
        raw.setdefault("allow_real_send", False)
    web = raw.get("web") if isinstance(raw.get("web"), dict) else {}
    current = str(web.get("password") or "")
    if current.strip():
        return current, False
    new_pwd = secrets.token_urlsafe(15)          # 15 字节 → 20 个 URL 安全字符(≥12)
    raw["web"] = {**web, "password": new_pwd}
    problems = AppConfig.from_dict(raw).validate()
    if problems:
        raise ValueError("生成访问口令后的配置校验失败: " + "; ".join(problems))
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return new_pwd, True


def _read_web_password_state(config_path: Path) -> tuple[str, bool]:
    """读 web.password,并区分「文件不可读」与「可读但未配置口令」。

    返回 (口令, 文件是否可读):
    - 文件缺失 / 非法 JSON / 顶层不是 JSON 对象 → ("", False) = 不可读;
    - 可读但缺 web 组或口令为空 → ("", True) = 明确未配置(未启用认证)。
    二者必须区分:不能因为配置损坏就把口令"读成空",否则认证中间件会把它
    误判为"未启用认证"而放行全部 /api/* 与 /ws/logs(fail-open 漏洞)。
    """
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return "", False
    if not isinstance(raw, dict):
        return "", False
    web = raw.get("web")
    if not isinstance(web, dict):
        return "", True
    return str(web.get("password") or ""), True


class AccessTokenMiddleware:
    """口令认证中间件:保护 HTTP 的 /api/* 与 WebSocket 的 /ws/logs。

    - 静态页(/、/app.js、/style.css 等)匿名可访问(登录框本身就要先加载);
    - 凭证:HTTP 请求头 X-Access-Token(兼容查询参数 ?token=);WebSocket
      浏览器无法自定义请求头,统一用查询参数 ?token=;
    - web.password 为空 = 未启用认证(run.py 启动时会自动生成,正常不为空;
      这也让 create_app 的既有测试在无口令临时配置下行为不变)。口令由
      create_app 注入的 _resolve_password 解析:运行期 config.json 缺失/损坏
      或口令被清空时,回退最近一次成功读取的口令并告警(fail-closed),
      "配置损坏"不会被误判成"未启用认证";
    - 401 响应带中文错误;WebSocket 拒绝 = 先 accept 完成握手再以 4401 关闭,
      浏览器 onclose 能拿到明确关闭码(若直接拒绝握手只能拿到 1006);
    - 口令比较用 secrets.compare_digest 防时序侧信道。
    """

    def __init__(self, app, password_getter: Callable[[], str]):
        self.app = app
        self._password_getter = password_getter

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        password = str(self._password_getter() or "")
        if not password:                          # 未配置口令 = 不启用认证
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if not (path.startswith("/api/") or path == "/ws/logs"):
            await self.app(scope, receive, send)  # 静态资源匿名放行
            return
        token = self._extract_token(scope)
        if token and secrets.compare_digest(
                token.encode("utf-8"), password.encode("utf-8")):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            response = JSONResponse(
                {"ok": False, "error": AUTH_REQUIRED_TEXT},
                status_code=401,
                headers={"WWW-Authenticate": AUTH_TOKEN_HEADER},
            )
            await response(scope, receive, send)
        else:
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close",
                        "code": AUTH_WS_CLOSE_CODE, "reason": "口令缺失或错误"})

    @staticmethod
    def _extract_token(scope) -> str:
        if scope["type"] == "http":
            for key, value in scope.get("headers") or []:
                if key.decode("latin-1").lower() == AUTH_TOKEN_HEADER.lower():
                    return value.decode("latin-1")
        qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        return (qs.get("token") or [""])[0]


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def create_app(
    engine: Optional[Any] = None,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    bus: Optional[EventBus] = None,
    window_finder: Optional[Callable[[], Any]] = None,
    web_dir: str | Path = DEFAULT_WEB_DIR,
    enable_logging_bridge: bool = True,
    mailbox: Optional[Any] = None,
) -> FastAPI:
    """构建 FastAPI 应用。

    - engine:实现 start/stop/status/send_now 的引擎对象(MonitorEngine 契约);
      None 时按真实引擎装配。测试传 stub 即可完全离线(不触碰微信窗口)。
    - config_path:config.json 路径(测试用临时文件,避免污染真实配置)。
    - bus:事件总线;缺省自建(环形缓冲默认 500 条,来自 ARCHITECTURE §5.9)。
    - window_finder:微信窗口查找器;缺省懒加载 WeChatWindow.find
      (纯只读 EnumWindows)。测试可注入 lambda: None。
    - mailbox:信箱服务(docs/PROTOCOL.md,MailboxService);None 时取进程
      单例(项目根 data/,首次写入才落盘)。注入后即挂载 PROTOCOL §2 的
      8 个 /api/mailbox/* + /api/media 端点;鉴权复用 X-Access-Token 中间件。
    """
    config_path = Path(config_path)
    web_dir = Path(web_dir)
    bus = bus or EventBus(buffer_size=500)
    if engine is None:
        engine = _build_default_engine(config_path, bus=bus)
    if mailbox is None:
        try:
            from wxbot.mailbox import get_mailbox_service
            raw0 = _read_raw_config(config_path)
            mb_cfg = raw0.get("mailbox") if isinstance(raw0.get("mailbox"), dict) else {}
            mailbox = get_mailbox_service(
                retention_hours=mb_cfg.get("retention_hours") or None,
                notify_url=str(mb_cfg.get("notify_url") or ""))  # §2.1 可选唤醒通知
        except Exception:                    # pragma: no cover
            mailbox = None                   # 信箱不可用不影响既有功能
    if window_finder is None:
        def window_finder():  # 延迟导入:import wxbot.server 不带动窗口模块
            from wxbot.vision.window import WeChatWindow
            return WeChatWindow.find()

    def _cfg() -> AppConfig:
        """当前配置的 dataclass 视图(白名单/agent 模式等读取用)。"""
        return AppConfig.from_dict(_read_raw_config(config_path))

    def _publish(text: str, level: str = "INFO", module: str = "server", **extra: Any) -> None:
        bus.publish(text, level=level, module=module, **extra)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: FastAPI):
        bus.attach_loop(asyncio.get_running_loop())
        if enable_logging_bridge:
            level = _cfg().logging.level
            # 过滤框架自身与 httpx 每请求一行的 INFO 噪声;引擎事件经 on_event
            # 桥接为中文文案,不再重复转发其 logger 输出
            bus.attach_logging(level=level,
                               skip_prefixes=("uvicorn", "websockets", "httpx",
                                              "wxbot.engine"))
        port_hint = ""
        webui = _read_raw_config(config_path).get("webui")
        if isinstance(webui, dict) and webui.get("port"):
            port_hint = str(webui["port"])
        _publish("WebUI 服务已启动;默认演练模式(dry_run),不会真实发送", module="server")
        # 信箱投递队列:注册引擎的真实投递函数(与手动发送同一闸门链)并启动;
        # Muse 的 /api/mailbox/replies 由此异步回流(docs/PROTOCOL.md §5)
        if mailbox is not None:
            deliver = getattr(engine, "mailbox_deliver", None)
            if callable(deliver):
                mailbox.set_deliver_fn(deliver)
            with contextlib.suppress(Exception):
                mailbox.start()
        yield
        _publish("WebUI 服务正在关闭", module="server")
        if mailbox is not None:
            with contextlib.suppress(Exception):
                mailbox.stop()         # 投递队列线程(幂等)
        with contextlib.suppress(Exception):
            engine.stop()          # 防止服务退出后残留监控线程(引擎须幂等可重复 stop)
        bus.detach_logging()

    app = FastAPI(title="WeChatVision 控制台", version="0.3.0",
                  docs_url="/api/docs", lifespan=_lifespan)
    app.state.engine = engine
    app.state.bus = bus
    app.state.mailbox = mailbox
    app.state.config_path = config_path
    app.state.window_cache: dict = {"at": 0.0, "value": None}
    app.state.window_finder = window_finder

    # ---- 口令 fail-closed 解析(2026-10-24 加固)----
    # 运行期 config.json 缺失/损坏(非法 JSON)时,若把口令"读成空",认证中间件
    # 会误判为"未启用认证"而放行全部 /api/* 与 /ws/logs(fail-open,配合常开
    # 隧道即公网裸奔且无告警)。对策:create_app 装配时快照一次口令,之后每当
    # 现读配置得到有效口令就随之更新("最近一次成功读取的口令");其余任何读
    # 不到有效口令的状态(文件缺失/损坏/口令被清空)一律回退该快照并告警
    # (fail-closed):正确口令仍可登录修复,错误口令一律 401,"配置损坏"绝不
    # 会被误判成"未启用认证"。启动时口令即为空(明确未启用认证,测试/本机
    # 自用)才维持原"免认证"行为,既有测试契约不变。
    startup_password = _read_web_password_state(config_path)[0]
    _auth_state = {"mode": "normal", "cached": startup_password}

    def _resolve_password() -> str:
        pwd, readable = _read_web_password_state(config_path)
        if readable and pwd:
            if _auth_state["mode"] != "normal":
                _auth_state["mode"] = "normal"
                _publish("config.json 已恢复可读,访问口令回到现读配置(安全告警解除)",
                         module="server")
            if pwd != _auth_state["cached"]:
                _auth_state["cached"] = pwd
            return pwd
        if not _auth_state["cached"]:
            return ""                  # 启动时即无口令 = 明确未启用认证
        # 有已知口令:运行期读不到有效口令 = 异常状态,按快照校验(fail-closed)
        mode = "corrupt" if not readable else "emptied"
        if _auth_state["mode"] != mode:  # 每段异常期只告警一次,恢复后可再触发
            _auth_state["mode"] = mode
            if mode == "corrupt":
                _publish("【安全告警】config.json 缺失或损坏(非法 JSON):口令校验已"
                         "回退到最近一次成功读取的口令,服务不会因此免认证;请把该"
                         "文件修复为合法 JSON,或停止服务后删除/改名它再重启"
                         "(run.py 会重新生成)", level="ERROR", module="server")
            else:
                _publish("【安全告警】运行期读到 web.password 为空(可能被手动清空):"
                         "仍按最近一次成功读取的口令校验(fail-closed);重启服务会"
                         "自动重新生成口令", level="WARNING", module="server")
        return _auth_state["cached"]

    # ---- 口令认证(先注册=内层;CORS 后注册=最外层,保证 localhost 预检不受影响)----
    app.add_middleware(AccessTokenMiddleware,
                       password_getter=_resolve_password)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- Muse 信箱协议 v1 端点(docs/PROTOCOL.md §2)----
    # 鉴权复用上方 X-Access-Token 中间件(/api/* 全覆盖,无/错 token → 401);
    # 协议版本头中间件最后注册 = 最外层,所有响应带 X-Mailbox-Protocol: 1
    if mailbox is not None:
        from wxbot.mailbox.api import register_mailbox_routes
        register_mailbox_routes(app, mailbox, config_path=config_path,
                                resolve_password=_resolve_password)

    def _safe_engine_status() -> dict:
        try:
            st = engine.status()
            return st if isinstance(st, dict) else {"raw": str(st)}
        except Exception as e:
            return {"running": False, "error": f"引擎状态读取失败: {type(e).__name__}: {e}"}

    # ---------------- 状态 ----------------
    @app.get("/api/status")
    def api_status():
        raw = _read_raw_config(config_path)
        win_info = _window_status(app.state.window_finder, app.state.window_cache)
        eng = _safe_engine_status()

        # agent 模式与健康:status()["agent"] 为后端名字符串;无网络探测。
        # 健康度仅对本地型后端(echo,恒本地)随状态轮询检测;
        # 网络型(openai/webhook)不在此探测,由 POST /api/agent/healthcheck 显式"测通"。
        ag = eng.get("agent")
        if isinstance(ag, dict):
            agent = dict(ag)
        elif isinstance(ag, str):
            agent = {"mode": ag}
        else:
            agent = {"mode": _cfg().agent.mode}
        if "healthy" not in agent or agent["healthy"] is None:
            mode = agent.get("mode")
            if not mode:
                mode = _cfg().agent.mode
            running = bool(eng.get("running", False))
            healthy, detail = None, None
            if str(mode) == "echo" and hasattr(engine, "healthcheck"):
                if running:
                    try:
                        healthy = bool(engine.healthcheck())
                        detail = None if healthy else "echo 健康检测异常"
                    except Exception:
                        healthy, detail = False, "echo 健康检测异常"
                else:
                    # 引擎未启动时 agent 尚未初始化(在 start() 里创建),
                    # 此时不能把"未启动"报成"不健康"
                    healthy, detail = None, "引擎未启动:启动后自动检测"
            elif str(mode) != "echo":
                detail = "网络型后端不做轮询探测;请点「测通」验证"
            agent = {"mode": mode, "healthy": healthy, "detail": detail}

        return {
            "ok": True,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "wechat_window": win_info,
            "engine": eng,
            "engine_running": bool(eng.get("running", False)),
            "agent": agent,
            "allow_real_send": _allow_real_send(raw),
            "dry_run_hint": (
                "演练模式:引擎只记录发送意图,不会真实发送"
                if not _allow_real_send(raw) else
                "真实发送已开启:请确认目标在白名单内"
            ),
            "bus": bus.stats(),
        }

    # ---------------- 配置 ----------------
    @app.get("/api/config")
    def api_config_get():
        raw = _read_raw_config(config_path)
        if not raw:
            raw = AppConfig().to_dict()          # 缺文件只回默认值,不在 GET 里写盘
            raw["allow_real_send"] = False
        return {"ok": True, "config": _mask_secrets(raw),
                "allow_real_send": _allow_real_send(raw),
                "note": "api_key / webhook_secret / 访问口令已脱敏;留空保存 = 不修改"}

    @app.post("/api/config")
    async def api_config_post(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "errors": ["请求体不是合法 JSON"]}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "errors": ["请求体必须是 JSON 对象"]}, status_code=400)

        raw_old = _read_raw_config(config_path)
        raw_new = copy.deepcopy(raw_old) if raw_old else AppConfig().to_dict()

        # 顶层键合并(allow_real_send 单独管理;组内只覆盖提交的字段,
        # 保留引擎侧独有的未知键如 dry_run 等)
        allow_send_posted: Optional[bool] = None
        for key, value in body.items():
            if key == "allow_real_send":
                allow_send_posted = bool(value)
            elif isinstance(value, dict):
                cur = raw_new.get(key)
                raw_new[key] = {**(cur if isinstance(cur, dict) else {}), **value}
            else:
                raw_new[key] = value
        if allow_send_posted is not None:
            raw_new["allow_real_send"] = allow_send_posted

        # ---- 发送模式联动矫正(2026-10-24 界面打磨;红线收口)----
        # WebUI「发送模式」二选一,总是同时提交两个开关;但 API 直调可能只传
        # 其一、甚至传矛盾组合。落盘前统一矫正(安全方向优先——真实发送只能由
        # 显式 allow_real_send=true 开启,单独传 engine.dry_run=false 绝不开启
        # 真实发送),落盘组合只允许「演练(false,true)」与「真实(true,false)」:
        #   显式 engine.dry_run=true → 演练:allow_real_send 强制 false(压倒一切);
        #   显式 allow_real_send=true(未同时显式传 dry_run=true)→ 真实:dry_run 强制 false;
        #   显式 allow_real_send=false → 演练:dry_run 同步归位 true;
        #   仅显式传 dry_run=false 且总开关未开 → 仍归位演练(不落盘「半真实」组合)。
        eng_posted = body.get("engine") if isinstance(body.get("engine"), dict) else {}
        _dry_posted = eng_posted.get("dry_run")
        dry_run_posted = _dry_posted if isinstance(_dry_posted, bool) else None
        eng_node = raw_new.get("engine")
        if not isinstance(eng_node, dict):
            eng_node = {}
            raw_new["engine"] = eng_node
        if dry_run_posted is True:
            raw_new["allow_real_send"] = False
            eng_node["dry_run"] = True
        elif allow_send_posted is True:
            eng_node["dry_run"] = False
        elif allow_send_posted is False:
            eng_node["dry_run"] = True
            raw_new["allow_real_send"] = False
        elif (dry_run_posted is False
              and raw_new.get("allow_real_send") is not True):
            eng_node["dry_run"] = True
            raw_new["allow_real_send"] = False

        # 列表字段防呆归一
        safety = raw_new.get("safety")
        if isinstance(safety, dict):
            if "whitelist" in safety:
                safety["whitelist"] = _normalize_list_field(safety["whitelist"])
            if "ignore_keywords" in safety:
                safety["ignore_keywords"] = _normalize_list_field(safety["ignore_keywords"])
        groups_cfg = raw_new.get("groups")
        if isinstance(groups_cfg, dict):
            if "whitelist" in groups_cfg:
                groups_cfg["whitelist"] = _normalize_list_field(groups_cfg["whitelist"])
            if "bot_names" in groups_cfg:
                groups_cfg["bot_names"] = _normalize_list_field(groups_cfg["bot_names"])

        # api_key / webhook_secret(平铺 legacy 字段):空值 / 脱敏回显值 = 不修改
        # (03 文档 §3.2 约定;webhook_secret 与 api_key 同族,2026-10-24 起脱敏)
        agent_new = raw_new.get("agent")
        old_agent_flat = (raw_old or {}).get("agent")
        old_agent_flat = old_agent_flat if isinstance(old_agent_flat, dict) else {}
        if isinstance(agent_new, dict):
            if "api_key" in agent_new:
                old_key = str(old_agent_flat.get("api_key") or "")
                posted_key = str(agent_new.get("api_key") or "")
                if posted_key == "" or posted_key == _mask_secret(old_key):
                    agent_new["api_key"] = old_key
            if "webhook_secret" in agent_new:
                old_sec = str(old_agent_flat.get("webhook_secret") or "")
                posted_sec = str(agent_new.get("webhook_secret") or "")
                if posted_sec == "" or posted_sec == _mask_secret(old_sec):
                    agent_new["webhook_secret"] = old_sec

        # web.password:空值 = 不修改(与 api_key 同约定);口令改动后立即生效
        # (认证中间件每次请求现读 config.json)。绝不能把口令写进日志总线。
        pwd_changed = False
        web_new = raw_new.get("web")
        if isinstance(web_new, dict) and "password" in web_new:
            old_web = (raw_old or {}).get("web")
            old_pwd = str(old_web.get("password") or "") if isinstance(old_web, dict) else ""
            posted_pwd = str(web_new.get("password") or "")
            if posted_pwd.strip() == "" or posted_pwd == _mask_secret(old_pwd):
                web_new["password"] = old_pwd     # 留空 / 脱敏回显值 = 不修改
            elif posted_pwd != old_pwd:
                pwd_changed = True

        # ---- 双模路由/后端列表/信箱段(2026-10-24 WebUI 配置补全)----
        # 可写路径:agent.backends / agent.default_backend / agent.routing /
        # mailbox.notify_url / mailbox.retention_hours。沿用既有归一与校验
        # 风格:防呆归一 + 明确 400 中文报错 + AppConfig.validate() 兜底
        # (id 重复、routing 指向未知后端等同样以中文 400 拒绝)。
        post_errors: list[str] = []
        agent_posted = body.get("agent") if isinstance(body.get("agent"), dict) else {}

        # agent.backends:后端列表(id 非空且唯一、type 四选一、api_key 空/脱敏=不修改)
        if "backends" in agent_posted:
            posted_be = agent_posted["backends"]
            if not isinstance(posted_be, list):
                post_errors.append("agent.backends 必须是列表(每项一个后端配置对象)")
            elif not posted_be:
                post_errors.append("agent.backends 不能为空(至少保留一个后端,否则机器人无法回复)")
            else:
                old_agent_raw = (raw_old or {}).get("agent")
                old_backends = (old_agent_raw.get("backends")
                                if isinstance(old_agent_raw, dict) else None)
                old_be_by_id: dict[str, dict] = {}
                for ob in (old_backends if isinstance(old_backends, list) else []):
                    if isinstance(ob, dict) and str(ob.get("id") or "").strip():
                        old_be_by_id[str(ob["id"]).strip()] = ob
                norm_be: list[dict[str, Any]] = []
                seen_ids: set[str] = set()

                def _keep_or_post(posted: str, old_val: str, flat_old: str) -> str:
                    """密钥族「不修改」判定(2026-10-24 界面打磨):
                    空 = 不修改;脱敏回显值 = 不修改。旧配置只有平铺字段、
                    无 backends 时,前端会按迁移规则合成单后端并原样回传 GET 的
                    脱敏值——此时无同名旧后端可比,回退平铺 legacy 旧值比对,
                    避免把掩码串当成新密钥落盘(仅回显命中才回退;留空不回退,
                    新增后端留空仍为空,绝不反向继承默认后端的密钥)。"""
                    if posted.strip() == "":
                        return old_val
                    if old_val and posted == _mask_secret(old_val):
                        return old_val
                    if (not old_val) and flat_old and posted == _mask_secret(flat_old):
                        return flat_old
                    return posted

                for i, item in enumerate(posted_be):
                    if not isinstance(item, dict):
                        post_errors.append(f"agent.backends[{i}] 必须是对象")
                        continue
                    bid = str(item.get("id") or "").strip()
                    btype = str(item.get("type") or "").strip().lower()
                    if not bid:
                        post_errors.append(
                            f"agent.backends[{i}].id 不能为空(id 是路由键,必须填写)")
                    elif bid in seen_ids:
                        post_errors.append(
                            f"agent.backends 存在重复的 id:{bid!r}(每个后端的 id 必须唯一)")
                    seen_ids.add(bid)
                    if btype not in AgentBackendConfig.BACKEND_TYPES:
                        post_errors.append(
                            f"agent.backends[{bid or i}].type 只能是 "
                            f"{'|'.join(AgentBackendConfig.BACKEND_TYPES)}"
                            f"(mailbox=Muse 信箱模式),收到:{btype or '(空)'}")
                    be: dict[str, Any] = {"id": bid, "type": btype}
                    for f in ("base_url", "model", "webhook_url"):
                        if f in item:
                            be[f] = str(item.get(f) or "").strip()
                    if "system_prompt" in item:
                        be["system_prompt"] = str(item.get("system_prompt") or "")
                    for f in ("timeout_sec", "max_reply_chars"):
                        v = item.get(f)
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            be[f] = v
                    if "api_key" in item:
                        be["api_key"] = _keep_or_post(
                            str(item.get("api_key") or ""),
                            str(old_be_by_id.get(bid, {}).get("api_key") or ""),
                            str(old_agent_flat.get("api_key") or ""))
                    if "webhook_secret" in item:
                        # 回调密钥与 api_key 同族:空/脱敏回显值 = 不修改
                        # (2026-10-24 界面打磨:响应脱敏后前端回传的是掩码)
                        be["webhook_secret"] = _keep_or_post(
                            str(item.get("webhook_secret") or "").strip(),
                            str(old_be_by_id.get(bid, {}).get("webhook_secret") or ""),
                            str(old_agent_flat.get("webhook_secret") or ""))
                    norm_be.append(be)
                agent_new["backends"] = norm_be

        # agent.default_backend / agent.routing(防呆归一:去空白、丢空行)
        if "default_backend" in agent_posted:
            agent_new["default_backend"] = str(agent_posted.get("default_backend") or "").strip()
        if "routing" in agent_posted:
            routing_posted = agent_posted["routing"]
            if not isinstance(routing_posted, dict):
                post_errors.append("agent.routing 必须是对象(联系人显示名 → 后端 id)")
            else:
                agent_new["routing"] = {
                    str(k).strip(): str(v).strip()
                    for k, v in routing_posted.items()
                    if str(k).strip() and str(v).strip()
                }

        # 默认后端 → 平铺 legacy 字段联动(mode/base_url/api_key/...)。
        # 仅在客户端显式提交了 backends / default_backend 时执行——平铺字段
        # 的既有约定(如「api_key 空值不修改」)不能被默认值倒灌覆盖。
        # 引擎对「默认后端」仍从平铺字段构造 agent(monitor._resolve_backend:
        # bid==default 时复用 create_agent(cfg.agent)),不同步会出现「路由表选了
        # 新后端、实际却还按旧 mode 参数回复」。默认后端为 mailbox 时不改 mode
        # (mode 是 legacy 字段且校验只收 echo|openai|webhook;引擎经
        # backends 判定 mailbox,不读 mode)。
        if "backends" in agent_posted or "default_backend" in agent_posted:
            agent_new = raw_new.get("agent")
            be_list = agent_new.get("backends") if isinstance(agent_new, dict) else None
            if isinstance(be_list, list) and be_list:
                ids_now = [b.get("id") for b in be_list]
                want = str(agent_new.get("default_backend") or "").strip()
                if want not in ids_now:
                    want = ids_now[0]
                    agent_new["default_backend"] = want
                dbe = next(b for b in be_list if b.get("id") == want)
                if dbe.get("type") in ("echo", "openai", "webhook"):
                    agent_new["mode"] = dbe["type"]
                for f in ("base_url", "api_key", "model", "system_prompt",
                          "webhook_url", "webhook_secret"):
                    agent_new[f] = str(dbe.get(f) or "")
                for f in ("timeout_sec", "max_reply_chars"):
                    if f in dbe:
                        agent_new[f] = dbe[f]

        # mailbox 段:notify_url / retention_hours(PROTOCOL §2.1/§6)
        if "mailbox" in body:
            if not isinstance(body.get("mailbox"), dict):
                post_errors.append("mailbox 必须是对象(可含 notify_url / retention_hours)")
            else:
                mailbox_new = raw_new.get("mailbox")
                if not isinstance(mailbox_new, dict):
                    mailbox_new = {}
                    raw_new["mailbox"] = mailbox_new
                if "notify_url" in mailbox_new:
                    mailbox_new["notify_url"] = str(mailbox_new.get("notify_url") or "").strip()
                if "retention_hours" in mailbox_new:
                    try:
                        mailbox_new["retention_hours"] = int(mailbox_new.get("retention_hours"))
                    except (TypeError, ValueError):
                        post_errors.append(
                            "mailbox.retention_hours 必须是整数(归档消息保留小时数,至少 1)")

        if post_errors:
            return JSONResponse({"ok": False, "errors": post_errors}, status_code=400)

        problems = AppConfig.from_dict(raw_new).validate()
        if problems:
            return JSONResponse({"ok": False, "errors": problems}, status_code=400)

        # 写盘(先备份;直接写合并后的 raw,保留未知键不丢失)
        try:
            if config_path.exists():
                shutil.copy2(config_path, config_path.with_suffix(config_path.suffix + ".bak"))
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(raw_new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as e:
            return JSONResponse({"ok": False, "errors": [f"配置写盘失败: {e}"]}, status_code=500)

        _publish("配置已保存(旧文件已备份为 config.json.bak)", module="config")
        if allow_send_posted is not None:
            _publish(
                "「允许真实发送」已" + ("开启:仅限白名单联系人,发送前后均有引擎侧校验" if allow_send_posted else "关闭"),
                level="WARNING" if allow_send_posted else "INFO", module="safety")
        synced = _sync_engine_safety(engine, raw_new)
        if synced:
            _publish(f"红线开关已即时同步到运行中的引擎:{', '.join(synced)}", module="safety")
        if pwd_changed:
            _publish("访问口令已修改并立即生效(不记录口令内容);"
                     "其他已登录的浏览器下一次请求将被要求重新登录", module="server")
        return {"ok": True, "config": _mask_secrets(raw_new),
                "allow_real_send": _allow_real_send(raw_new),
                "synced_now": synced,
                "restart_hint": "dry_run/allow_real_send 已即时生效;"
                                "其余配置(智能体接入/采集节奏等)需「停止引擎→启动引擎」后生效"}

    # ---------------- 引擎启停 ----------------
    @app.post("/api/engine/start")
    def api_engine_start():
        try:
            engine.start()
        except Exception as e:
            _publish(f"引擎启动失败: {type(e).__name__}: {e}", level="ERROR")
            return JSONResponse({"ok": False, "error": f"引擎启动失败: {e}"}, status_code=500)
        _publish("引擎已启动(监控循环运行中)", module="engine")
        return {"ok": True, "status": _safe_engine_status()}

    @app.post("/api/engine/stop")
    def api_engine_stop():
        try:
            engine.stop()
        except Exception as e:
            _publish(f"引擎停止失败: {type(e).__name__}: {e}", level="ERROR")
            return JSONResponse({"ok": False, "error": f"引擎停止失败: {e}"}, status_code=500)
        _publish("引擎已停止", module="engine")
        return {"ok": True, "status": _safe_engine_status()}

    # ---------------- 手动发送(双重护栏)----------------
    @app.post("/api/send")
    async def api_send(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
        contact = str((body or {}).get("contact", "") or "").strip()
        text = str((body or {}).get("text", "") or "")
        if not contact:
            return JSONResponse({"ok": False, "error": "请选择发送目标(仅白名单成员可选)"}, status_code=400)
        if not text.strip():
            return JSONResponse({"ok": False, "error": "消息内容不能为空"}, status_code=400)
        if len(text) > MAX_SEND_CHARS:
            return JSONResponse({"ok": False, "error": f"消息过长(上限 {MAX_SEND_CHARS} 字),请分条发送"}, status_code=400)

        raw = _read_raw_config(config_path)
        if not _allow_real_send(raw):
            _publish("手动发送被拦截:allow_real_send=false(演练模式)", level="WARNING", module="safety")
            return JSONResponse({
                "ok": False,
                "error": "当前处于演练模式(allow_real_send=false):"
                         "不会调用引擎发送任何消息。如需真实发送,请到「配置」页"
                         "开启「允许真实发送」(仅限白名单联系人,需二次确认)。",
                "allow_real_send": False,
            }, status_code=403)

        whitelist = _cfg().safety.whitelist
        # 受管群例外(2026-10-24 审查修复的收口:README §4.2「群名不需要、也
        # 不应该加进联系人白名单」):三闸门全开的受管群凭三闸门放行,其余
        # 目标仍严格限联系人白名单。引擎侧 send_now/_deliver/flows 仍有同
        # 一套三闸门 + OCR 标题复核(fail-closed,层层兜底)。
        if contact not in whitelist and not _managed_group_allowed(raw, contact):
            _publish(f"手动发送被拦截:目标「{contact}」不在白名单 {whitelist}", level="WARNING", module="safety")
            return JSONResponse({
                "ok": False,
                "error": f"目标「{contact}」不在白名单 {whitelist} 内。"
                         "红线:只允许对白名单联系人发送(默认仅白名单成员)。",
            }, status_code=403)

        _publish(f"手动发送指令已下发引擎:目标={contact},内容前40字={text[:40]!r}", module="send")
        try:
            result = engine.send_now(contact=contact, text=text)  # 引擎侧仍有 dry_run+标题OCR复核
        except _REJECT_EXCS as e:
            code = getattr(e, "code", "") or "rejected"
            http = 429 if code == "rate_limit" else 403
            _publish(f"手动发送被引擎安全闸拦截[{code}]: {e}", level="WARNING", module="safety")
            return JSONResponse({"ok": False, "error": f"引擎安全闸拦截({code}): {e}", "code": code},
                                status_code=http)
        except TypeError:
            # 兼容可能的位置参数签名 send_now(text, contact)
            result = engine.send_now(text, contact)
        except Exception as e:
            _publish(f"手动发送失败: {type(e).__name__}: {e}", level="ERROR", module="send")
            return JSONResponse({"ok": False, "error": f"引擎发送失败: {e}"}, status_code=500)
        _publish(f"引擎已受理手动发送:目标={contact},返回={result}", module="send")
        return {"ok": True, "contact": contact, "text_length": len(text), "engine_result": result}

    # ---------------- 智能体测通 ----------------
    @app.post("/api/agent/healthcheck")
    def api_agent_healthcheck():
        """「测通」按钮:显式触发一次 agent 健康检测(网络型后端会发一次探测)。"""
        if not hasattr(engine, "healthcheck"):
            return {"ok": True, "healthy": None, "detail": "当前引擎未提供 healthcheck(测试桩?)"}
        try:
            running = bool(engine.status().get("running", False))
        except Exception:
            running = False
        if not running:
            return {"ok": True, "healthy": None,
                    "detail": "引擎未启动:智能体在引擎启动时才初始化(echo 模式启动后自动显示健康)。"}
        try:
            healthy = bool(engine.healthcheck())
        except Exception as e:
            _publish(f"智能体测通异常: {type(e).__name__}: {e}", level="ERROR", module="agent")
            return JSONResponse({"ok": False, "healthy": False, "error": f"测通异常: {e}"}, status_code=500)
        _publish(f"智能体测通:{'通过' if healthy else '失败'}", level="INFO" if healthy else "ERROR",
                 module="agent")
        return {
            "ok": True, "healthy": healthy,
            "detail": ("测通通过,智能体可用" if healthy else
                       "测通失败:请检查网络/API Key/模型名(详见实时日志)"),
        }

    # ---------------- WebSocket 日志 ----------------
    @app.websocket("/ws/logs")
    async def ws_logs(ws: WebSocket):
        await ws.accept()
        bus.attach_loop(asyncio.get_running_loop())
        sub = bus.subscribe()
        pump: Optional[asyncio.Task] = None
        try:
            history = bus.recent()
            for event in history:  # 先补发历史(环形缓冲最近500条)
                await ws.send_text(json.dumps(event, ensure_ascii=False))
            # 泵以 sub.last_seq(补历史期间到达的最新序号)为去重起点
            pump = make_pump(bus, sub, ws.send_text)
            _publish("WebUI 日志通道已连接,开始实时推送", module="webui")
            while True:
                await ws.receive_text()  # 客户端消息仅作保活,全部忽略
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            with contextlib.suppress(Exception):
                await ws.close()
        finally:
            if pump is not None:
                pump.cancel()
                with contextlib.suppress(BaseException):  # CancelledError 非 Exception
                    await pump
            bus.unsubscribe(sub)

    # ---------------- 静态页(放在 API 路由之后,根路径兜底)----------------
    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    else:
        @app.get("/")
        def _missing_web():
            return JSONResponse({"ok": False, "error": f"web/ 静态目录缺失: {web_dir}"}, status_code=500)

    return app
