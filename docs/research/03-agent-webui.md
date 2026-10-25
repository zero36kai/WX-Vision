# 微信机器人接入 LLM 智能体架构 + 本地 WebUI 最佳实践调研

> 调研日期:2026-10-23(注意资料时效性,文章/仓库状态以当日网络检索为准)
> 调研方法:GitHub + 官方文档检索(WebSearch/WebFetch),所有关键结论附来源 URL。
> 适用场景:本项目"纯视觉 + 键鼠模拟"微信 Windows 4.x 自动化工具(截屏+OCR 收消息 → 智能体生成回复 → 仿真人键鼠发送)。

---

## 0. 核心结论(TL;DR)

1. **消息循环采用"采集 → 处理 → 发送"三段式 + 队列解耦**:参照 chatgpt-on-wechat(CowAgent)的 Channel → Bridge → Bot 分层,把"OCR 采集"和"键鼠发送"隔离到独立线程,中间用消息队列,处理层做去重/白名单/上下文/频控([来源](https://zhayujie.com/chatgpt-on-wechat.html)、[主仓库](https://github.com/zhayujie/chatgpt-on-wechat))。
2. **去重是视觉方案的命门**:没有消息 ID 可用,必须用"内容规范化 + 联系人 + 时间窗"做指纹去重,并排除自己发出的消息;wxauto 社区的重复读取问题是现成的反面教材([wxauto 去重讨论](https://www.ihonker.com/thread-38374-1-1.html))。
3. **可插拔 Agent = 统一接口 + 三种内置实现**(echo / openai / webhook):OpenAI 兼容 API 只需 `base_url + api_key + model` 三参数,GLM、DeepSeek、Ollama、OpenAI 官方全部验证了这一写法(见 §2.1 各家端点表)。
4. **WebUI 未找到"FastAPI + Vue3 CDN 单文件面板"的现成开源模板**(已专门检索,见 §3.1 诚实标注),推荐按 FastAPI 官方 WebSocket 用法自组装:单 `index.html` + unpkg CDN 引入 Vue3 + Element Plus,零 Node 构建;功能清单参照 KouriChat 与 AstrBot 的 WebUI([KouriChat](https://github.com/KouriChat/KouriChat)、[AstrBot Wiki](https://github.com/AstrBotDevs/AstrBot/wiki/zh-use-webui))。
5. **小白防呆三件套**:保守默认值(echo 模式、白名单非空才自动回复、回复延迟开箱较大)+ 危险操作二次确认(启动/清空/覆盖配置)+ 日志轮转(标准库 `RotatingFileHandler`)。

---

## 1. 消息处理循环设计

### 1.1 业界架构参考

**(1) chatgpt-on-wechat(现演化为 CowAgent,47.5k stars)**

分层设计,各层解耦、可替换([README](https://github.com/zhayujie/chatgpt-on-wechat)、[作者博客](https://zhayujie.com/chatgpt-on-wechat.html)):

| 层 | 职责 | 对应本项目的模块 |
|---|---|---|
| Channel 通道层 | 消息入口,构造 Context,消费线程池异步处理 | OCR 采集器(截屏+识别) |
| Bridge 桥接层 | `fetch_reply_content(query, context)` 路由到具体机器人 | 处理调度器 |
| Bot 模型层 | `bot_factory` 工厂创建具体 Bot(OpenAI/GLM/Claude…) | **Agent 接入层(本调研 §2)** |
| Session 层 | 每会话独立上下文,超限裁剪 | 上下文管理 |
| 插件层 | godcmd(管理员指令)、keyword、banwords 等可选增强 | 可暂不实现 |

消息流:通道进 → 判断触发条件(前缀/@/白名单)→ Bridge → Bot 调 LLM → Session 保存多轮上下文并按 token 上限淘汰最早一轮 → 加回复前缀发回。

**(2) wxauto 系 LLM 项目(与本项目形态最接近:Windows 桌面微信 + UI/视觉自动化)**

- [beiyi-hub/wx_AI_bot](https://github.com/beiyi-hub/wx_AI_bot):wxauto + 角色扮演模型,长短期记忆、联网搜索。
- [iwyxdxl/WeChatBot_WXAUTO_SE](https://github.com/iwyxdxl/WeChatBot_WXAUTO_SE)(⚠ 2026-01 已归档停止维护):多用户/群聊独立 Prompt、多条消息合并处理、图片识别、**WebUI(启动程序/改配置/Prompt 管理)**。
- [ksmarisa520/My-Dream-Moments](https://github.com/ksmarisa520/My-Dream-Moments)(wxauto 分支):wxauto + DeepSeek,配置即 `config.py` 填 API Key + 监听对象昵称。
- [KouriChat/KouriChat](https://github.com/KouriChat/KouriChat):情感陪伴框架,WebUI 完善(总览/账号/聊天调试/日志/设置五页),**但新版明确禁止接入微信/QQ 等腾讯系平台**,只可参考其 WebUI 功能设计,不可参考其接入方式。

**(3) AstrBot**:多平台 LLM 机器人框架,WebUI 包含插件管理、**日志查看、可视化配置、统计信息**,且配置分两层(会话级行为配置 vs 全局系统设置),这个"配置分层"思想值得抄([AstrBot Wiki:使用 WebUI](https://github.com/AstrBotDevs/AstrBot/wiki/zh-use-webui)、[官方文档](https://docs.astrbot.app))。

### 1.2 五个关键机制(业界做法 → 本项目做法)

| 机制 | 业界做法(来源) | 本项目推荐做法 |
|---|---|---|
| **消息去重** | wxauto 社区:用 `(msg.content, msg.time)` 组合键存 set/dict,过滤 `msg.type`/`is_self` 排除自己,控制轮询间隔;`GetListenMessage` 返回的消息可能未被清空导致重复读取([ihonker 讨论](https://www.ihonker.com/thread-38374-1-1.html)、[wxauto 文档](https://docs.wxauto.org)、[CSDN 示例](https://blog.csdn.net/tzy0124wd/article/details/144836267)) | 无消息 ID → 指纹 = `hash(联系人 + 规范化OCR文本 + 秒级时间窗)`;维护有界指纹缓存(如 `deque`+`set`,容量 200,过期 10 分钟);**已回复过的指纹不再回复**;自己发出的消息(OCR 到"我"名下的气泡)直接跳过 |
| **每联系人独立上下文** | COW:Session 按 session_id(联系人/群)隔离,`conversation_max_tokens`(默认 1000)超限移除最早一问一答,`expires_in_seconds`(默认 3600)会话过期([作者博客](https://zhayujie.com/chatgpt-on-wechat.html)) | `dict[contact_id] -> deque[Message]`;上限按轮数(如 20 轮)+ 时间过期(如 2 小时);清空指令支持(参考 COW `clear_memory_commands`,用户发"清空记忆"重置) |
| **回复触发条件** | COW:私聊前缀 `single_chat_prefix`、群聊 @/前缀、群白名单 `group_name_white_list`(填 `ALL_GROUP` 全开);WXAUTO_SE:显式填入监听对象昵称并绑定 Prompt | **白名单驱动**:仅处理"监听列表"内联系人的新消息(视觉方案天然按聊天窗口监听);可叠加"忽略关键词"(如撤回提示、红包、转账系统文案);默认只有手动模式,自动回复需显式开启 |
| **人设 system prompt** | COW:全局 `character_desc`;WXAUTO_SE:**每个用户/群可绑定独立 Prompt**,WebUI 有 Prompt 管理页 | 两级人设:全局 `personas.default` + 可选 `personas.overrides[contact]` 覆盖;Prompt 存配置文件,WebUI 编辑保存 |
| **频率限制** | COW:`rate_limit_chatgpt`(每分钟最高问答速率,超速排队)、`rate_limit_dalle`([houbb 配置详解](https://houbb.github.io/2024/03/26/ai-17-chatgpt-on-wechat)) | 三层限速:①全局 QPM 上限(超速进入队列)②同一联系人冷却时间(如 5s,防连环轰炸)③随机回复延迟(本项目特有:键鼠拟人需要 1~3s 随机延迟,同时天然起限速作用)⚠ 后两条为设计推荐,非直接引用 |

### 1.3 推荐消息处理循环(伪代码)

三段式,队列解耦。**为什么不做成一个循环**:OCR 截屏要 1~3 秒,LLM 要 2~10 秒,键鼠发送也要时间;串行做会漏消息、互相阻塞。

```python
# ============ 数据结构 ============
@dataclass
class IncomingMessage:
    contact: str          # 联系人/群名(窗口标题或 tab 文本)
    sender: str           # 发送者(群内昵称;"我"=自己)
    text: str             # OCR 规范化后的文本
    ts: float             # 观察到的时间
    fingerprint: str      # 去重指纹
    is_self: bool         # 是否自己发的

# ============ 线程 1:采集(截屏+OCR)============
seen_window_state = {}   # contact -> 上次该窗口消息区指纹快照

def collector_loop():
    while running:
        for contact in monitored_contacts():        # 只看监听列表(白名单联系人窗口)
            snap = ocr_chat_area(contact)           # 截屏 + OCR 该联系人消息区
            new_msgs = diff_messages(seen_window_state.get(contact), snap)
            for m in new_msgs:                       # 仅"新出现的行"进队列
                m.fingerprint = fp(contact, m.sender, normalize(m.text), m.ts)
                inbox_queue.put(m)                   # 原始队列(采集不做业务判断)
            seen_window_state[contact] = snap
        sleep(config.poll_interval_sec)              # 默认 2~3s,过快会重复读到同一内容

# ============ 线程 2:处理(去重/过滤/上下文/Agent)============
replied_fps = FingerprintCache(cap=200, ttl_sec=600)   # 已处理指纹

def processor_loop():
    while running:
        m = inbox_queue.get()
        if m.is_self:                continue        # 自己发的跳过
        if replied_fps.has(m.fingerprint): continue  # 去重:处理过就丢
        replied_fps.add(m.fingerprint)
        if not passes_whitelist(m):  continue        # 白名单:默认仅指定联系人
        if hits_ignore_rule(m):      continue        # 忽略规则:系统提示/红包/撤回等
        ctx = sessions.get_session(m.contact)        # 每联系人独立上下文
        ctx.append(role="user", text=m.text)
        if not rate_limiter.allow(m.contact):        # 三层限速(§1.2)
            rate_limiter.requeue(m); continue
        reply = agent.reply(ctx, m)                  # 可插拔 Agent(§2)
        if reply is None or not reply.text:          # Agent 明确表示"不回复"
            continue
        ctx.append(role="assistant", text=reply.text)
        ctx.trim(max_turns=config.context_max_turns, expiry=config.context_ttl_sec)
        outbox_queue.put(reply)                      # 交给发送线程

# ============ 线程 3:发送(键鼠模拟)============
def sender_loop():
    while running:
        reply = outbox_queue.get()
        human_delay = uniform(*config.reply_delay_range_sec)   # 默认 1~3s
        sleep(human_delay)
        type_and_send(reply.contact, reply.text)               # 键鼠模拟,失败重试 N 次
        log_event("sent", reply)                                # 推送到 WebUI 日志
```

要点:
- **采集线程只负责"发现新东西"**,不做白名单/去重决策(便于调试:WebUI 可看到"看到了什么"和"为什么没回复")。
- **去重放在处理线程开头**,指纹缓存在回复成功前后都要正确(建议"取出即标记",避免 Agent 调用失败时重复轰炸;失败可降级重试一次)。
- Agent 抛异常/超时 → 记日志、**不回复**(宁可沉默,不要把报错文案发进聊天)。

---

## 2. 可插拔 Agent 接入层

### 2.1 OpenAI 兼容客户端:三参数标准写法(已逐家验证)

标准写法:`openai` 官方 SDK 是通用客户端,换 `base_url + api_key + model` 三个参数即可切换厂商:

```python
from openai import OpenAI
client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key)
resp = client.chat.completions.create(
    model=cfg.model,
    messages=[{"role": "system", "content": persona},
              {"role": "user", "content": text}],
    temperature=cfg.temperature,
    timeout=cfg.timeout_sec,
)
text = resp.choices[0].message.content
```

各家已验证的接入参数(2026-10 检索官方文档):

| 厂商 | base_url | api_key | 来源 |
|---|---|---|---|
| OpenAI 官方 | `https://api.openai.com/v1`(SDK 默认,可省略) | sk-… | SDK 默认值 |
| DeepSeek | `https://api.deepseek.com` | DeepSeek 平台申请 | [api-docs.deepseek.com](https://api-docs.deepseek.com/),官方文档明确"兼容 OpenAI SDK,改配置即可用" |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4/` | bigmodel 平台申请 | [智谱官方文档「OpenAI API 兼容」](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction) |
| Ollama(本地) | `http://localhost:11434/v1` | 任意字符串(如 `"ollama"`,必填但不校验) | [Ollama 官方博客 OpenAI compatibility](https://ollama.com/blog/openai-compatibility) |
| 任意第三方中转/自建网关 | 该服务提供的 `/v1` 地址 | 该服务签发的 key | 通用做法(OpenAI 兼容协议事实标准) |

> ⚠ 注意:各家模型名随时间变化(如 DeepSeek 文档示例当前为 `deepseek-flash`),`model` 字段必须让用户可配置,不要写死;上述端点未在本机实测连通性,列入[待实机验证](#7-待实机验证清单)。
> ⚠ GLM 另有 Responses API 兼容端点(`https://open.bigmodel.cn/api/v1`,超时 7200s),与 Chat Completions 端点不同,勿混用(同上来源)。

### 2.2 Webhook 回调模式

设计:把收到的消息 POST(JSON)到用户指定的 URL,**取响应 JSON 的 `reply` 字段作为回复文本**。这是即时通讯平台机器人回调的通用模式(公众号/企业微信回调、Slack Events 等皆同思路;此处为本项目设计推荐,不针对某平台文档)。

```
请求:POST {webhook_url}
     Headers: Content-Type: application/json
              X-Webhook-Secret: {webhook_secret}      # 可选,防别人乱调
     Body: {"contact": "测试联系人", "sender": "测试联系人", "text": "在吗",
            "is_group": false, "ts": 1761180000.0, "tool": "wechat-vision"}

响应:200 {"reply": "在的~"}       → 发送该文本
     200 {"reply": null}          → 不回复(Agent 主动沉默)
     非200 / 超时 / 无 reply 字段  → 记日志,不回复(同 §1.3 兜底原则)
```

适用:用户已有自己的服务(如接入自建工作流、n8n 类工具),不想暴露 API Key 给本工具。

### 2.3 统一可插拔 Agent 接口

**接口定义(伪代码,Python `typing.Protocol` 风格):**

```python
class IncomingMessage:      # §1.3 已定义
    contact: str; sender: str; text: str; ts: float; fingerprint: str; is_self: bool

class ChatContext:          # 每联系人独立会话上下文
    contact: str
    persona: str                        # 生效的人设 system prompt(已按 overrides 解析)
    history: list[dict]                 # [{"role": "user"|"assistant", "content": str}, ...]

class AgentReply:
    text: str                           # 要发送的文本;空/None = 不回复
    meta: dict = {}                     # 调试用:模型名、耗时、token 数等,进日志不进聊天

class Agent(Protocol):
    name: str                                        # "echo" / "openai" / "webhook"
    def setup(self, cfg: dict) -> None: ...          # 启动时用配置初始化;失败抛异常,WebUI 显示
    def reply(self, ctx: ChatContext, msg: IncomingMessage) -> AgentReply | None: ...
    def healthcheck(self) -> bool: ...               # WebUI"测通"按钮:ping 一次(伪消息或 models 接口)
```

**字段表(配置驱动,`agent` 节):**

| 字段 | 类型/默认值 | 说明 |
|---|---|---|
| `type` | `"echo"` / `"openai"` / `"webhook"`(默认 **`echo`**) | 实现选择器;默认 echo 保证零配置可跑通 |
| `base_url` | 默认 `https://api.deepseek.com` | 仅 openai 类型;三参数之一 |
| `api_key` | 默认 `""` | 仅 openai;WebUI 中脱敏显示(`sk-****`),保存时空值=不修改 |
| `model` | 默认 `"deepseek-chat"`(示例值,需用户按需改) | 仅 openai;三参数之一 |
| `temperature` | `0.8` | 仅 openai;聊天场景偏高一些更自然 |
| `timeout_sec` | `30` | openai/webhook 共用;超时=不回复 |
| `webhook_url` | `""` | 仅 webhook 类型 |
| `webhook_secret` | `""` | 仅 webhook;非空时加 `X-Webhook-Secret` 头 |
| `max_reply_chars` | `500` | 防呆:超长回复截断/拒绝(避免键鼠模拟发超长文本卡死) |

**内置实现:**

1. **EchoAgent(默认,自测用)**:`reply()` 直接返回 `"【echo】" + msg.text`,或按配置返回固定文案。用途:① 不配 API Key 也能端到端跑通"识别→回复→发送"链路 ② WebUI"测通"自检 ③ 给小白演示不炸真人小号。
2. **OpenAIAgent**:§2.1 标准写法;把 `ctx.persona` 作为 system,`ctx.history` 作为多轮 messages;捕获所有异常返回 `None`(不回复+记日志)。
3. **WebhookAgent**:§2.2;`requests.post(..., timeout=cfg.timeout_sec)`;响应解析宽松(`reply` 字段,兼容 `text`),失败不回复。

**新增一个 Agent 的成本**:实现 3 个方法 + 在 `AGENT_REGISTRY` 字典注册 + 配置里加一个 `type` 取值,主循环零改动——这就是"可插拔"的全部。

---

## 3. 本地 WebUI 设计(FastAPI + 单页,无 Node 构建)

### 3.1 技术选型(含诚实标注)

- **专门检索过"FastAPI + Vue3 CDN 单文件 dashboard"的现成开源模板,未找到**——主流 FastAPI 管理面板(fastapi/full-stack-fastapi-template、FastAPI-Vue3-Admin 等)全是 Vite/Node 构建的前后端分离方案([检索结论](https://fastapi.tiangolo.com))。因此推荐**自组装**,组合方式本身都是官方文档级的成熟做法:
  - 后端:FastAPI,`FileResponse` 返回单个 `index.html`;WebSocket 用 FastAPI 原生 `@app.websocket`(参考:[FastAPI WebSocket 官方用法](https://fastapi.tiangolo.com/advanced/websockets)、[后台任务推 WebSocket 的最小示例教程](https://python.plainenglish.io/build-a-real-time-dashboard-with-fastapi-and-websockets-eb3c949b3132)、[zhiyuan8/FastAPI-websocket-tutorial](https://github.com/zhiyuan8/FastAPI-websocket-tutorial))。
  - 前端:单 HTML 文件,`<script src="https://unpkg.com/vue@3">` CDN 引入 Vue3(全局构建版,无构建步骤),组件库用 Element Plus CDN 版;**不需要 Node/npm/webpack**。
  - ⚠ 待实机验证:CDN 引入需要联网;若用户环境断网,需把 vue.global.js 与 Element Plus 的 js/css 下载到本地 `static/` 目录随工具分发(建议直接随包附带,不依赖 CDN)。
- **功能参照对象**(功能清单抄思路,不抄代码):
  - [KouriChat WebUI](https://github.com/KouriChat/KouriChat):五页布局(总览 / 账号 / **聊天调试** / **日志** / 设置),首次访问初始化管理员账号,密码强度要求,凭据本机加密保存——本地工具防呆的标杆。
  - [AstrBot WebUI](https://github.com/AstrBotDevs/AstrBot/wiki/zh-use-webui):插件管理、**日志查看、可视化配置、统计信息**;配置分"会话级行为配置"与"全局系统设置"两层。
  - [WeChatBot_WXAUTO_SE WebUI](https://github.com/iwyxdxl/WeChatBot_WXAUTO_SE):**启动程序按钮 + 网页改配置 + Prompt 生成管理**(⚠ 该仓库 2026-01 起已归档停止维护,仅作设计参考)。
  - CowAgent:Web 控制台可直接配置通道/模型/技能"无需手动编辑文件",带 `web_password` 保护与 `cow` CLI(start/stop/logs)([仓库](https://github.com/zhayujie/chatgpt-on-wechat))。

### 3.2 页面结构与功能清单

单页应用,左侧导航 6 个页面。地址 `http://127.0.0.1:9756`(默认,仅监听 127.0.0.1,不暴露局域网)。

| 页面 | 功能 | 防呆设计 |
|---|---|---|
| **① 仪表盘** | 运行状态卡(未配置/已停止/运行中);微信窗口检测状态(找到/未找到——纯视觉方案的前提自检);今日收/回消息计数;白名单联系人头像式列表;大按钮:**启动/停止**(二次确认);一键"echo 自检" | 启动自动回复需勾选"我已知晓自动回复风险"复选框(参考 WeChatBot_WXAUTO_SE 的免责声明做法) |
| **② 实时日志** | WebSocket 推送,自动滚动;级别过滤(INFO/WARN/ERROR);暂停滚动按钮;按关键词搜索;每行含时间戳/模块/事件;缓冲最近 500 条,断线重连后可拉历史 | 日志超过缓冲上限丢最旧的(内存有界);提供"导出全部日志到文件" |
| **③ 配置** | 分四组表单:LLM 接入(§2.3 字段表,含**"测通"按钮**调 `agent.healthcheck()`);人设(全局文本框 + 每联系人覆盖);触发与安全(白名单、忽略关键词、频控参数、回复延迟区间);采集与发送(轮询间隔、OCR 区域、键鼠参数)。"保存"=校验(pydantic)→ 备份旧文件为 `config.json.bak` → 写盘;**修改 API 配置后需重启生效,弹确认** | api_key 脱敏;表单旁常驻"默认值"提示;不合法输入前端即挡 |
| **④ 手动消息** | 选联系人 + 输入框 + 发送(绕过 Agent 直接走键鼠发送);**用于人工接管对话** | 发送前显示"将把以下内容以你的身份发给 白名单联系人"确认框;运行中/停止时都可用 |
| **⑤ 收件箱预览** | 展示最近 OCR 到的原始消息(联系人/发送者/文本/时间/是否被过滤及原因)——**视觉方案专属:让小白直观看到"OCR 认成了什么"**,是排查识别错误的唯一入口 | 无 |
| **⑥ 帮助** | 首次使用向导说明(标定截屏区域→填白名单→echo 测试→启动);常见问题(微信窗口找不到/OCR 乱码/为什么不回复——联动日志) | 无 |

后端 API 草表:`GET/PUT /api/config`、`POST /api/start|/api/stop`、`GET /api/status`、`POST /api/test-agent`、`POST /api/manual-send`、`GET /api/inbox`、`WS /ws/logs`。

### 3.3 WebSocket 日志推送要点

```python
# 后端:全局内存环形缓冲 + 广播
log_buffer = deque(maxlen=500)
clients: set[WebSocket] = set()

@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    await ws.accept(); clients.add(ws)
    for line in log_buffer: await ws.send_text(line)   # 先补历史
    try:
        while True: await ws.receive_text()             # 保活/忽略客户端消息
    except WebSocketDisconnect:
        clients.discard(ws)

def log_event(level, module, text):                     # 业务线程调用(线程安全)
    line = json.dumps({"ts": time.time(), "level": level, "module": module, "text": text}, ensure_ascii=False)
    log_buffer.append(line)
    for ws in list(clients): schedule_send(ws, line)    # 异步投递,慢客户端丢弃不阻塞主流程
```

- 业务线程(OCR/LLM/键鼠)与 asyncio 事件循环跨线程:`schedule_send` 用 `loop.call_soon_threadsafe` 投递;**慢客户端超时主动踢掉**,防止日志广播阻塞发送线程。
- 同时写文件:标准库 `logging.handlers.RotatingFileHandler(filename, maxBytes=5*1024*1024, backupCount=5)` 轮转(约 25MB 上限;标准库文档:[logging.handlers](https://docs.python.org/3/library/logging.handlers.html#rotatingfilehandler))。

---

## 4. 配置文件字段设计(config.json)

设计原则:① 复制模板生成(参考 COW `config-template.json → config.json` 惯例)② 每个字段必须有安全默认值 ③ 分组注释清楚 ④ 保存前自动备份。

```jsonc
{
  "webui":   { "host": "127.0.0.1", "port": 9756, "password": "" },
  "agent": {
    "type": "echo",                       // echo | openai | webhook,默认 echo(零配置可跑)
    "base_url": "https://api.deepseek.com",
    "api_key": "",
    "model": "deepseek-chat",             // 示例值,必须按所选厂商修改
    "temperature": 0.8,
    "timeout_sec": 30,
    "webhook_url": "",
    "webhook_secret": "",
    "max_reply_chars": 500
  },
  "personas": {
    "default": "你是用户的微信助手,回复简洁口语化,不超过100字。",
    "overrides": { "测试联系人": "" }     // 每联系人可覆盖全局人设(参考 WXAUTO_SE 每用户 Prompt)
  },
  "safety": {
    "auto_reply_enabled": false,          // 总开关,默认关:不开就不自动回复
    "whitelist": ["测试联系人"],          // 自动回复仅对名单内联系人生效;为空=只允许手动
    "whitelist_mode": "strict",           // strict:不在名单一律忽略(默认);audit:只记录不回复
    "ignore_keywords": ["[转账]", "[红包]", "撤回了一条消息"],
    "require_know_risk": true             // WebUI 启动自动回复前须勾选风险确认
  },
  "rate_limit": {
    "global_qpm": 20,                     // 全局每分钟回复上限(参考 COW rate_limit_chatgpt 思想)
    "per_contact_cooldown_sec": 5,        // 同联系人最小回复间隔
    "reply_delay_range_sec": [1.0, 3.0]   // 键鼠拟人随机延迟
  },
  "context": {
    "max_turns": 20,                      // 每联系人保留轮数(参考 COW conversation_max_tokens 裁剪思想)
    "ttl_sec": 7200                       // 上下文过期(参考 COW expires_in_seconds=3600,此处放宽)
  },
  "capture": {
    "poll_interval_sec": 2.5,             // 截屏轮询间隔;过小会重复读到同一内容
    "dedup_cache_size": 200,
    "dedup_ttl_sec": 600,
    "ocr_lang": "ch"
  },
  "send":   { "type_delay_ms": [50, 150], "send_retries": 2 },
  "logging": { "level": "INFO", "file": "logs/bot.log", "max_bytes": 5242880, "backup_count": 5, "ws_buffer": 500 }
}
```

字段与来源对照:`whitelist` ← COW `group_name_white_list`;`personas` ← COW `character_desc` + WXAUTO_SE 每用户 Prompt;`context.*` ← COW `conversation_max_tokens` / `expires_in_seconds`;`rate_limit.global_qpm` ← COW `rate_limit_chatgpt`(全部来源见 §1.2)。

---

## 5. 小白用户防呆清单

| 类别 | 措施 |
|---|---|
| **默认值策略** | 开箱即安全:agent=echo、auto_reply_enabled=false、whitelist=["文件传输助手"](微信内置示例联系人)、回复延迟 1~3s、QPM=20。所有默认值组合起来 = "第一次启动只会看到日志滚动,不会真的回复任何人" |
| **危险操作二次确认** | ① 启动自动回复(需勾选风险确认,参考 WXAUTO_SE 免责声明)② 清空某联系人上下文 ③ 覆盖保存配置(先自动备份 .bak)④ 修改 API 配置后提示重启 ⑤ 手动发消息(显示将以本人身份发出) |
| **白名单默认** | 默认 `["文件传输助手"]`,strict 模式;收件箱预览页对被过滤消息**标注原因**(不在白名单/命中忽略词/重复消息),小白能看懂"为什么不回" |
| **日志轮转/上限** | 文件:`RotatingFileHandler` 5MB×5;WebUI 内存缓冲 500 条有界队列;导出功能满足排查需求 |
| **其他** | api_key 脱敏显示;仅监听 127.0.0.1;微信窗口未找到时启动按钮禁用并给出指引;echo 自检一键验证全链路;帮助页内置 FAQ |

---

## 6. 参考项目/资料链接表

### 架构与消息循环

| 项目/资料 | 链接 | 用途 |
|---|---|---|
| zhayujie/chatgpt-on-wechat(→CowAgent,47.5k★) | https://github.com/zhayujie/chatgpt-on-wechat | 分层架构(Channel/Bridge/Bot/Session)、Web 控制台 |
| 作者博客(架构+配置详解) | https://zhayujie.com/chatgpt-on-wechat.html | 触发条件、session、人设、过期时间 |
| COW 配置详解(第三方) | https://houbb.github.io/2024/03/26/ai-17-chatgpt-on-wechat | 全部配置项表格(白名单/频控/清空指令) |
| AstrBot | https://github.com/AstrBotDevs/AstrBot ・ WebUI: https://github.com/AstrBotDevs/AstrBot/wiki/zh-use-webui | WebUI 功能布局、配置分层 |
| beiyi-hub/wx_AI_bot | https://github.com/beiyi-hub/wx_AI_bot | wxauto+LLM 拟人机器人 |
| iwyxdxl/WeChatBot_WXAUTO_SE(⚠ 已归档) | https://github.com/iwyxdxl/WeChatBot_WXAUTO_SE | 每用户 Prompt、WebUI(启动/配置/Prompt 管理) |
| KouriChat(⚠ 禁止接入微信/QQ) | https://github.com/KouriChat/KouriChat | WebUI 五页设计、初始化管理员 |
| ksmarisa520/My-Dream-Moments(wxauto 分支) | https://github.com/ksmarisa520/My-Dream-Moments | wxauto+DeepSeek 最小配置形态 |
| claw-codes/wx4py | https://github.com/claw-codes/wx4py | 微信 4.x 监听指定联系人 |
| wxauto 去重实战讨论 | https://www.ihonker.com/thread-38374-1-1.html | 重复读取原因与去重方案 |
| wxauto 官方文档 | https://docs.wxauto.org | GetListenMessage / msg.type / msg.attr |
| wxauto 自动回复示例 | https://blog.csdn.net/tzy0124wd/article/details/144836267 | 监听→回复完整代码模式 |

### LLM 接入(OpenAI 兼容三参数)

| 资料 | 链接 |
|---|---|
| DeepSeek 官方(兼容 OpenAI SDK) | https://api-docs.deepseek.com/ |
| 智谱 GLM「OpenAI API 兼容」官方文档 | https://docs.bigmodel.cn/cn/guide/develop/openai/introduction |
| Ollama OpenAI compatibility 官方博客 | https://ollama.com/blog/openai-compatibility |

### WebUI 实现

| 资料 | 链接 |
|---|---|
| FastAPI WebSockets 官方文档 | https://fastapi.tiangolo.com/advanced/websockets |
| 最小"后台任务推 WS 到纯 JS 面板"教程 | https://python.plainenglish.io/build-a-real-time-dashboard-with-fastapi-and-websockets-eb3c949b3132 |
| FastAPI websocket 教程仓库 | https://github.com/zhiyuan8/FastAPI-websocket-tutorial |
| testdriven.io WS dashboard(结构参考) | https://testdriven.io/blog/fastapi-postgres-websockets |
| Python RotatingFileHandler 标准库文档 | https://docs.python.org/3/library/logging.handlers.html#rotatingfilehandler |

> 未找到(诚实声明):① "FastAPI + Vue3 CDN 单文件、零构建"的现成开源 dashboard 模板;② COW 对"每联系人冷却时间"的现成配置项(其频控为全局 QPM;冷却时间为本项目设计推荐);③ webhook 回调模式在微信桌面自动化场景的成熟开源实现(该模式设计参照通用 IM 回调惯例)。以上均已按设计推荐给出方案,非引用现成实现。

---

## 7. 待实机验证清单

1. 各家 `base_url` 实际连通性与当前模型名(GLM `https://open.bigmodel.cn/api/paas/v4/`、DeepSeek、本地 Ollama)——本调研仅验证了官方文档写法,未实机调用。
2. 自研"截屏+OCR"方案的实际重复率与指纹去重参数(`poll_interval_sec` / `dedup_ttl_sec`)——wxauto 社区结论不能直接迁移,需用真实微信 4.x 窗口实测。
3. 微信 4.x 消息区窗口结构、OCR 文本规范化效果(气泡归属"我/对方"的判定)——实机截图验证。
4. WebSocket 日志推送在低配 Windows 机器上多客户端同时观看时的表现。
5. CDN 方案在用户断网环境不可用——决定是否随包内置 vue.global.js 与 Element Plus 静态文件(建议内置)。
6. CowAgent/其他项目的微信通道均为 itchat/wechatnt/协议类方案,与本项目"纯视觉+键鼠"不同——其消息循环分层可抄,**通道实现细节不可照搬**。
