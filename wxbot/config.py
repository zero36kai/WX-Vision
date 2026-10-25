# -*- coding: utf-8 -*-
"""配置骨架:dataclass + JSON 读写(不引入 pydantic,见 ARCHITECTURE §1)。

字段来源:
- docs/ARCHITECTURE.md §5(关键设计决策:白名单/限频/去重/上下文/日志等);
- docs/research/03-agent-webui.md §4(config.json 字段设计);
- 扩展:agent.mode/system_prompt、engine 段、顶层 allow_real_send、web 段。

旧版配置兼容性约定:
- agent.type(旧字段名)→ agent.mode:读取时自动映射,旧 config.json 可直接加载;
  `AgentConfig.type` 保留为只读写兼容属性;
- webui(旧段名)→ web(port 默认 9756 → 8765):读取时自动映射,
  `AppConfig.webui` 保留为兼容属性;保存统一写新段名;
- 旧段(capture/rate_limit/context/safety/send/personas/logging)原样保留:
  safety.whitelist 仍是唯一白名单源,rate_limit 仍供限频/延迟参数,
  capture 仍供 OCR/去重参数,engine 段是监控引擎的运行参数权威来源。

所有字段必须带安全默认值(03 号文档 §4 设计原则):
默认组合 = agent=echo + dry_run + allow_real_send=false + 不自动回复 = 零发送。
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class WebConfig:
    """WebUI 监听配置(server 使用)。仅监听本机回环。

    旧段名 webui(默认端口 9756)已更名 web(默认端口 8765);
    读取旧 config.json 时自动把 webui 段映射到本段。"""
    host: str = "127.0.0.1"
    port: int = 8765
    password: str = ""


# 旧版段名兼容别名
WebUIConfig = WebConfig


@dataclass
class AgentBackendConfig:
    """双模路由的单个后端配置(agent.backends 列表成员,PROTOCOL §11)。

    - id:路由键(agent.routing 的值、agent.default_backend 的取值),须唯一;
    - type:echo | openai | webhook | mailbox;mailbox = Muse 信箱模式
      (消息入信箱,由 Muse 异步处理,不走本机同步回复,PROTOCOL §11);
    - 其余字段按 type 取用:echo 忽略全部;openai 用 base_url/api_key/model/
      system_prompt/timeout_sec;webhook 用 webhook_url/webhook_secret/
      timeout_sec;mailbox 用 mailbox 段(retention_hours 等)。"""
    id: str = ""
    type: str = "echo"
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-chat"
    system_prompt: str = ""
    timeout_sec: float = 30.0
    webhook_url: str = ""
    webhook_secret: str = ""
    max_reply_chars: int = 500

    BACKEND_TYPES = ("echo", "openai", "webhook", "mailbox")


@dataclass
class AgentConfig:
    """智能体接入配置。mode 见 ARCHITECTURE §5.7,默认 echo 零配置可跑。

    openai 模式仅需 base_url/api_key/model 三参数(调研03 §2.1);
    system_prompt 为空时回落 personas.default(两级人设,03 §1.2)。

    双模路由(PROTOCOL §11):
    - backends:后端列表(type ∈ echo|openai|webhook|mailbox),按联系人路由;
    - default_backend:路由表未命中的联系人用哪个后端 id;
    - routing:{联系人显示名 → backend id};两者同时启用、按联系人分流;
    - 向后兼容:旧 config.json 只有 mode 时,加载时自动迁移为等价的
      backends=[{id: mode, type: mode, ...mode 的各参数}] + default_backend=mode
      (from_dict 迁移,保存时写新结构;旧字段 mode/base_url/... 原样保留,
      已有 config.json 不丢字段)。"""
    mode: str = "echo"            # echo | openai | webhook(默认 echo;legacy 单模式)
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-chat"  # 必须按所选厂商修改,不写死(03 文档 §2.3)
    system_prompt: str = ""       # 人设;空 = 用 personas.default
    temperature: float = 0.8
    timeout_sec: float = 30.0     # openai/webhook 共用;超时=不回复
    webhook_url: str = ""
    webhook_secret: str = ""
    max_reply_chars: int = 500    # 防呆:超长回复截断(防键鼠发超长文本卡死)
    backends: list[AgentBackendConfig] = field(default_factory=list)
    default_backend: str = ""     # 默认后端 id;空 = backends[0]
    routing: dict[str, str] = field(default_factory=dict)  # 联系人 → backend id

    @property
    def type(self) -> str:
        """旧版字段名兼容别名(旧代码/旧配置读法)。"""
        return self.mode

    @type.setter
    def type(self, value: str) -> None:
        self.mode = value

    def backend_by_id(self, backend_id: str) -> AgentBackendConfig | None:
        for b in self.backends:
            if b.id == backend_id:
                return b
        return None


@dataclass
class SafetyConfig:
    """安全配置。默认组合 = 不自动回复 + 仅白名单(开箱即安全)。"""
    auto_reply_enabled: bool = False          # 总开关,默认关(ARCHITECTURE §5.5)
    # 红线:默认仅"文件传输助手"(微信内置、人人都有,不含任何真实用户信息)。
    # 首次使用请改为自己的测试联系人;真实发送仅限白名单成员。
    whitelist: list[str] = field(default_factory=lambda: ["文件传输助手"])
    whitelist_mode: str = "strict"            # strict:不在名单一律忽略;audit:只记录
    ignore_keywords: list[str] = field(
        default_factory=lambda: ["[转账]", "[红包]", "撤回了一条消息"])
    require_know_risk: bool = True            # WebUI 启动自动回复前须勾选风险确认


@dataclass
class RateLimitConfig:
    """限频配置(ARCHITECTURE §5.6:每联系人每分钟最多 3 条自动回复)。"""
    global_qpm: int = 20                      # 全局每分钟回复上限
    per_contact_qpm: int = 3                  # 每联系人每分钟上限(ARCHITECTURE §5.6)
    per_contact_cooldown_sec: float = 5.0     # 同联系人最小回复间隔
    reply_delay_range_sec: tuple[float, float] = (1.0, 3.0)  # 拟人随机延迟


@dataclass
class ContextConfig:
    """会话上下文(ARCHITECTURE §5.5:每联系人独立、内存保存、上限 20 条)。"""
    max_turns: int = 20
    ttl_sec: int = 7200


@dataclass
class CaptureConfig:
    """感知采集配置。"""
    poll_interval_sec: float = 2.5            # 截屏轮询间隔
    dedup_cache_size: int = 200               # 指纹去重有界缓存(ARCHITECTURE §5.5)
    dedup_ttl_sec: int = 600                  # 去重 TTL 10 分钟
    ocr_min_conf: float = 0.5                 # 低于此置信度的 OCR 行丢弃(02 文档 §1.4)
    printwindow_first: bool = True            # PrintWindow 为主,mss 回退(ARCHITECTURE §1)


@dataclass
class SendConfig:
    """发送参数(发送流程使用;演练模式下不执行任何发送)。"""
    type_delay_ms: tuple[int, int] = (50, 150)
    send_retries: int = 2
    send_key: str = "button"                  # button|enter|alt_s,按实测可用性定(§5.4)


@dataclass
class LoggingConfig:
    """日志:logs/ 按大小轮转,总上限 50MB(ARCHITECTURE §5.9);WebUI 环形缓冲 500 条。"""
    level: str = "INFO"
    file: str = "logs/bot.log"
    max_bytes: int = 10 * 1024 * 1024         # 10MB × 5 份 ≈ 50MB 上限
    backup_count: int = 5
    ws_buffer: int = 500


@dataclass
class GroupsConfig:
    """群聊配置(默认全关:不开就不碰任何群,红线)。

    - enabled=False:引擎完全不监控群聊(采集阶段即跳过);
    - reply_mode:off=不回复(默认) / at=仅当消息@了机器人 / all=所有群消息都回复;
    - whitelist:群名白名单——即使 enabled=True,不在名单的群一律忽略;
    - bot_names:机器人昵称(用于识别"@昵称");留空 = 任意 @提及都算命中 at 模式;
    - allow_real_send(2026-10-24 三闸门,默认 false):群聊真实发送的
      第三道闸。群消息真实发送必须**同时**满足:groups.enabled + 群 ∈
      groups.whitelist + groups.allow_real_send=true(引擎 _deliver 与
      flows.send_message 群分支两处按此校验)。默认 false 时行为与旧版
      「双层硬禁」完全一致:群回复只记发送意图,绝不真实发送。"""
    enabled: bool = False                     # 默认关:群功能整体不启用
    reply_mode: str = "off"                   # off(默认)| at | all
    whitelist: list[str] = field(default_factory=list)   # 群名列表,默认空
    bot_names: list[str] = field(default_factory=list)   # 机器人昵称,默认空
    allow_real_send: bool = False             # 三闸门第三闸(默认关,红线)

    REPLY_MODES = ("off", "at", "all")


@dataclass
class HumanConfig:
    """拟人化键鼠画像(2026-10-24 拟人强化;HumanInput 行为参数的配置权威来源)。

    所有 *_ms 区间均为闭区间 [lo, hi](毫秒);随机源是可注入的
    random.Random 实例(wxbot/actions/human_input.py),便于测试定种子复现。
    以下五项强化参数(默认值即推荐值,调小更快但更"机器"、调大更拟人):
    - target_jitter_px:点击落点在目标 ±N px 内随机偏移(容差内命中);
    - pre_click_delay_ms:移动到位 → 按下 之前的前置随机停顿;
    - hold_duration_ms:按下 → 抬起 的按压时长随机;
    - hesitate_prob / hesitate_delay_ms:偶发犹豫——每次点击前以该概率
      附加一次停顿(默认 15% 概率、0.8~2s);
    - settling_delay_ms:抬起 之后的 settling 随机延迟。
    贝塞尔移动轨迹不受本段影响(始终保留)。"""
    target_jitter_px: int = 3
    pre_click_delay_ms: tuple[int, int] = (120, 600)
    hold_duration_ms: tuple[int, int] = (60, 180)
    hesitate_prob: float = 0.15
    hesitate_delay_ms: tuple[int, int] = (800, 2000)
    settling_delay_ms: tuple[int, int] = (100, 400)


@dataclass
class MailboxConfig:
    """Muse 信箱配置(PROTOCOL §6 保留期 + §2.1 可选唤醒通知)。

    data 目录固定为项目根 data/(.gitignore 已排除,聊天内容严禁入库)。"""
    retention_hours: int = 48                 # 归档消息保留小时数(默认 48)
    # v1.1 §2.1 可选:入信唤醒通知 URL。每有新消息入信箱,PC 尽力而为地
    # 向该 URL POST 一条短超时(3 秒)通知(body=该消息完整字段,含 msg_id);
    # 失败/超时仅 DEBUG 日志、绝不阻塞入信。空(默认)=不通知,零影响。
    notify_url: str = ""


@dataclass
class EngineConfig:
    """监控引擎运行参数(engine 段为权威来源)。

    默认组合安全:dry_run=true + auto_reply_enabled=false → 引擎只记录
    发送意图,绝不真实发送(红线)。"""
    poll_interval_sec: float = 5.0            # 采集轮询间隔(ARCHITECTURE §4 默认 5s)
    max_replies_per_contact_per_min: int = 3  # 每联系人每分钟回复上限(§5.6)
    context_window: int = 20                  # 每联系人上下文条数上限(§5.5)
    dry_run: bool = True                      # True=只记录发送意图,不执行键鼠
    auto_reply_enabled: bool = False          # 自动回复总开关,默认关(§5.5)


@dataclass
class AppConfig:
    """顶层配置。字段分组与 03 号调研文档 §4 的 config.json 结构一致。"""
    web: WebConfig = field(default_factory=WebConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    groups: GroupsConfig = field(default_factory=GroupsConfig)
    mailbox: MailboxConfig = field(default_factory=MailboxConfig)
    human: HumanConfig = field(default_factory=HumanConfig)
    # 真实发送总开关(红线):false 时即使 dry_run=false 也拦截真实发送,
    # server 手动发送 API 同受此拦截;默认 false = 开箱零发送能力
    allow_real_send: bool = False
    personas: dict[str, Any] = field(default_factory=lambda: {
        "default": "你是用户的微信助手,回复简洁口语化,不超过100字。",
        "overrides": {},
    })
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    send: SendConfig = field(default_factory=SendConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @property
    def webui(self) -> WebConfig:
        """旧版属性名兼容别名。"""
        return self.web

    def __post_init__(self) -> None:
        """默认构造(非 from_dict)时也保证 agent.backends 非空:
        空 backends → 由 mode 迁移出等价单后端(校验要求至少一个后端)。"""
        if not self.agent.backends:
            self.agent = _agent_from_dict({}, AgentConfig())

    # ---------- 序列化 ----------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # tuple → list,保证 JSON 可序列化
        for group in ("rate_limit", "send", "human"):
            for k, v in list(d[group].items()):
                if isinstance(v, tuple):
                    d[group][k] = list(v)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        """宽松合并:缺字段用默认值,多余字段忽略(旧版配置可直接加载)。"""
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        data = dict(data)
        # ---- 旧段名/旧字段名迁移(向后兼容) ----
        if "web" not in data and isinstance(data.get("webui"), dict):
            data["web"] = data["webui"]          # webui → web
        if isinstance(data.get("agent"), dict):
            ag = dict(data["agent"])
            if "mode" not in ag and "type" in ag:
                ag["mode"] = ag["type"]          # agent.type → agent.mode
            data["agent"] = ag
        group_map = {
            "web": WebConfig, "engine": EngineConfig,
            "groups": GroupsConfig, "mailbox": MailboxConfig,
            "human": HumanConfig,
            "safety": SafetyConfig, "rate_limit": RateLimitConfig,
            "context": ContextConfig, "capture": CaptureConfig,
            "send": SendConfig, "logging": LoggingConfig,
        }
        for name, dc in group_map.items():
            if name in data and isinstance(data[name], dict):
                cur = asdict(getattr(cfg, name))
                for k, v in data[name].items():
                    if k in cur:
                        cur[k] = _coerce(cur[k], v)
                setattr(cfg, name, dc(**cur))
        # ---- agent 段(含 backends/routing 嵌套结构,单独合并+迁移) ----
        # 用纯净骨架作回退:cls() 的 __post_init__ 已把默认 mode=echo 迁移进
        # backends,直接拿它当回退会让"旧 mode=openai 无 backends"的配置
        # 误判为"已有显式 backends"而漏迁移
        if isinstance(data.get("agent"), dict):
            cfg.agent = _agent_from_dict(data["agent"], AgentConfig())
        if "personas" in data and isinstance(data["personas"], dict):
            cfg.personas = data["personas"]
        if "allow_real_send" in data:
            cfg.allow_real_send = bool(_coerce(False, data["allow_real_send"]))
        return cfg

    def validate(self) -> list[str]:
        """返回问题列表(空列表 = 通过)。安全相关字段强校验。"""
        problems: list[str] = []
        s = self.safety
        if not s.whitelist or any(not isinstance(x, str) or not x.strip() for x in s.whitelist):
            problems.append("safety.whitelist 必须为非空字符串列表(红线:只对白名单操作)")
        if s.whitelist_mode not in ("strict", "audit"):
            problems.append("safety.whitelist_mode 只能是 strict|audit")
        if self.agent.mode not in ("echo", "openai", "webhook"):
            problems.append("agent.mode 只能是 echo|openai|webhook")
        # 双模路由(agent.backends / default_backend / routing)校验
        ids = [b.id for b in self.agent.backends]
        if not ids:
            problems.append("agent.backends 不能为空(至少一个后端)")
        if len(ids) != len(set(ids)):
            problems.append("agent.backends 的 id 必须唯一")
        for b in self.agent.backends:
            if b.type not in b.BACKEND_TYPES:
                problems.append(
                    f"agent.backends[{b.id or '?'}].type 只能是 "
                    f"{'|'.join(b.BACKEND_TYPES)}(mailbox=Muse 信箱模式)")
        if self.agent.backends and self.agent.default_backend not in ids:
            problems.append("agent.default_backend 必须是 backends 中已有的 id")
        for contact, bid in self.agent.routing.items():
            if bid not in ids:
                problems.append(
                    f"agent.routing[{contact}] 指向未知后端 {bid!r}"
                    "(必须是 backends 中已有的 id)")
        if not (0.0 <= self.agent.temperature <= 2.0):
            problems.append("agent.temperature 应在 [0, 2]")
        if self.capture.ocr_min_conf < 0.0 or self.capture.ocr_min_conf > 1.0:
            problems.append("capture.ocr_min_conf 应在 [0, 1]")
        if self.rate_limit.per_contact_qpm < 1:
            problems.append("rate_limit.per_contact_qpm 至少为 1")
        e = self.engine
        if e.poll_interval_sec < 0.5:
            problems.append("engine.poll_interval_sec 建议 ≥ 0.5(过快会重复读到同一内容)")
        if e.max_replies_per_contact_per_min < 1:
            problems.append("engine.max_replies_per_contact_per_min 至少为 1")
        if e.context_window < 1:
            problems.append("engine.context_window 至少为 1")
        g = self.groups
        if g.reply_mode not in g.REPLY_MODES:
            problems.append("groups.reply_mode 只能是 off|at|all(默认 off)")
        if g.enabled and not [x for x in g.whitelist if str(x).strip()]:
            problems.append(
                "groups.enabled=true 时 groups.whitelist 不能为空"
                "(红线:群聊必须显式列入白名单,否则一律忽略)")
        if g.whitelist and any(not isinstance(x, str) or not x.strip() for x in g.whitelist):
            problems.append("groups.whitelist 必须为非空字符串列表")
        if not isinstance(g.allow_real_send, bool):
            problems.append("groups.allow_real_send 必须是布尔值(默认 false,三闸门第三闸)")
        if self.mailbox.retention_hours < 1:
            problems.append("mailbox.retention_hours 至少为 1(归档保留小时数)")
        # 拟人画像(2026-10-24):区间 [lo, hi] 须 0 ≤ lo ≤ hi(≤60s 防呆)
        h = self.human
        if not (0 <= int(h.target_jitter_px) <= 50):
            problems.append("human.target_jitter_px 应在 [0, 50](像素)")
        for fname in ("pre_click_delay_ms", "hold_duration_ms",
                      "hesitate_delay_ms", "settling_delay_ms"):
            lo, hi = getattr(h, fname)
            if not (0 <= lo <= hi <= 60_000):
                problems.append(f"human.{fname} 应满足 0 ≤ lo ≤ hi ≤ 60000(毫秒)")
        if not (0.0 <= float(h.hesitate_prob) <= 1.0):
            problems.append("human.hesitate_prob 应在 [0, 1](偶发犹豫概率)")
        return problems


def _coerce(default: Any, value: Any) -> Any:
    """把 JSON 值收敛到默认值同型(tuple 字段接受 list)。"""
    if isinstance(default, tuple) and isinstance(value, (list, tuple)):
        if len(value) == len(default) and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
        ):
            return tuple(value)
        return default
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int) and not isinstance(default, bool):
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    if isinstance(default, float):
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    if isinstance(default, list) and isinstance(value, list):
        return value
    if isinstance(default, str):
        return str(value)
    return value


def _agent_from_dict(raw_agent: dict, fallback: AgentConfig) -> AgentConfig:
    """agent 段 → AgentConfig:平铺字段合并 + backends/routing 嵌套解析
    + 旧 mode 配置自动迁移为等价 backends+default(向后兼容,不丢字段)。"""
    cur = asdict(fallback)
    for k, v in raw_agent.items():
        if k in cur:
            cur[k] = _coerce(cur[k], v)

    # backends:JSON list[dict] → list[AgentBackendConfig](未知键忽略)
    backends: list[AgentBackendConfig] = []
    for item in (cur.get("backends") or []):
        if isinstance(item, AgentBackendConfig):
            backends.append(item)
        elif isinstance(item, dict):
            fields = {
                k: _coerce(getattr(AgentBackendConfig(), k), v)
                for k, v in item.items()
                if k in AgentBackendConfig.__dataclass_fields__
            }
            backends.append(AgentBackendConfig(**fields))

    # 迁移:无显式 backends → 由旧 mode + mode 的各参数构造等价单后端
    if not backends:
        backends = [AgentBackendConfig(
            id=str(cur.get("mode") or "echo"),
            type=str(cur.get("mode") or "echo"),
            base_url=str(cur.get("base_url") or ""),
            api_key=str(cur.get("api_key") or ""),
            model=str(cur.get("model") or ""),
            system_prompt=str(cur.get("system_prompt") or ""),
            timeout_sec=float(cur.get("timeout_sec") or 30.0),
            webhook_url=str(cur.get("webhook_url") or ""),
            webhook_secret=str(cur.get("webhook_secret") or ""),
            max_reply_chars=int(cur.get("max_reply_chars") or 500),
        )]

    default_backend = str(cur.get("default_backend") or "").strip()
    if not default_backend or not any(b.id == default_backend for b in backends):
        # 未配置/配置悬空 → 首个后端(迁移场景下即旧 mode,语义完全一致)
        default_backend = backends[0].id

    routing_raw = cur.get("routing") or {}
    routing: dict[str, str] = {}
    if isinstance(routing_raw, dict):
        for k, v in routing_raw.items():
            if str(k).strip() and str(v).strip():
                routing[str(k).strip()] = str(v).strip()

    return AgentConfig(
        mode=str(cur.get("mode") or "echo"),
        base_url=str(cur.get("base_url") or ""),
        api_key=str(cur.get("api_key") or ""),
        model=str(cur.get("model") or ""),
        system_prompt=str(cur.get("system_prompt") or ""),
        temperature=float(cur.get("temperature") or 0.8),
        timeout_sec=float(cur.get("timeout_sec") or 30.0),
        webhook_url=str(cur.get("webhook_url") or ""),
        webhook_secret=str(cur.get("webhook_secret") or ""),
        max_reply_chars=int(cur.get("max_reply_chars") or 500),
        backends=backends,
        default_backend=default_backend,
        routing=routing,
    )


def load_config(path: str | Path = "config.json", create_default: bool = True) -> AppConfig:
    """读取配置;文件不存在时写入默认配置(ARCHITECTURE §2:首次运行自动生成)。"""
    p = Path(path)
    if not p.exists():
        cfg = AppConfig()
        if create_default:
            save_config(cfg, path, backup=False)
        return cfg
    raw = p.read_text(encoding="utf-8")
    cfg = AppConfig.from_dict(json.loads(raw))
    problems = cfg.validate()
    if problems:
        raise ValueError("config.json 校验失败: " + "; ".join(problems))
    return cfg


def save_config(cfg: AppConfig, path: str | Path = "config.json", backup: bool = True) -> None:
    """写盘;默认先备份旧文件为 config.json.bak(03 文档 §4 设计原则)。"""
    p = Path(path)
    if backup and p.exists():
        shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
