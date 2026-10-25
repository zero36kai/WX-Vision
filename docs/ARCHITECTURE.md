# 架构设计:微信纯视觉自动化助手(WeChatVision)

> 本文档是 WeChatVision 的方向性设计文件,实现与改动须与之对齐。
> 依据:docs/research/01~03 号调研文档(公开资料调研)+ 实机验证结论(涉及本机私有信息的过程记录不入库)。

## 0. 项目定位与红线(优先级最高)

**定位**:纯视觉 + 仿真人键鼠的微信 Windows 4.x 自动化助手。不注入 DLL、不读内存、不依赖 UIAutomation 控件树——只用「截图像素分析 + 模拟人的鼠标键盘」操作真实微信客户端。

**红线(违反任何一条即任务失败)**:
1. **私聊真实发送仅限白名单成员**(本机白名单属私有配置,不入库);**群聊真实发送仅限用户明确授权的测试群**且需同时满足:groups.enabled + 群 ∈ groups.whitelist + groups.allow_real_send=true(默认 false)。任何发送动作前,必须 OCR 校验当前聊天标题 == 目标(群标题含"(N)"后缀容错),不匹配则中止并告警。信箱(mailbox)回复的投递走同一闸门。
1b. **信箱协议红线**:信箱回复只允许投递到监听范围内的会话;`is_from_me=true` 的消息不得触发任何处理(防死循环);未 ack 的消息绝不删除。实现以 docs/PROTOCOL.md 为准(两侧的通信契约)。
2. **只读优先**:允许打开聊天/读消息/截图,严禁发送任何内容;首次真实发送前必须先以 dry_run 演练确认,目标仅限授权范围。
3. pyautogui FAILSAFE 必须保持开启(鼠标移到屏幕左上角(0,0)立即抛异常中止)。
4. 所有产出文件 UTF-8;Python 文件读写显式 encoding='utf-8'。
5. 禁止使用 UIAutomation(uiautomation/comtypes)、微信 hook、内存读取、微信协议库。

## 1. 技术栈

| 组件 | 选型 | 状态 |
|---|---|---|
| Python | 3.12(本机) | ✅ 定稿 |
| 窗口管理 | ctypes(Win32 API:FindWindow/ShowWindow/SetForegroundWindow/GetWindowRect/PrintWindow) | ✅ 定稿(实机已验证) |
| 截屏 | PrintWindow(PW_RENDERFULLCONTENT)为主 + mss 回退(前台ROI) | ✅ 定稿(实机验证可用;调研02的"无公开证据"已被实机实测推翻) |
| OCR | RapidOCR(pip 包 `rapidocr` + `onnxruntime`,本地CPU,原生返回四点坐标框+文本+置信度) | ✅ 定稿(调研02确认) |
| 模板匹配 | OpenCV(opencv-python;灰度+TM_CCOEFF_NORMED+阈值0.8+0.8~1.2多尺度;红点走HSV掩膜) | ✅ 定稿 |
| 键鼠模拟 | pyautogui(PAUSE=0 自管随机节奏,FAILSAFE 绝不关)+ pynput(急停监听)+ pyperclip(剪贴板);贝塞尔轨迹自实现,human-mouse 库仅参考 | ✅ 定稿(调研02确认) |
| 中文输入 | 剪贴板 + Ctrl+V(保存/恢复用户剪贴板) | ✅ 定稿(见 §5.4) |
| 后端 | FastAPI + uvicorn + WebSocket | ✅ 定稿 |
| WebUI | 原生 HTML/JS/CSS 单页,**无构建、无 CDN 依赖**(离线可用) | ✅ 定稿(倾向,若调研03强烈反对再议) |
| HTTP客户端 | httpx(agent 后端调用) | ✅ 定稿 |
| 配置 | JSON 文件 + dataclass 校验(不引入 pydantic,减依赖) | ✅ 定稿 |

## 2. 目录结构

```
WeChatVision\(项目根)\
├── wxbot\                    # 核心包
│   ├── config.py             # 配置加载/保存/校验/默认值
│   ├── vision\
│   │   ├── window.py         # WeChatWindow:查找/恢复/置前/矩形/截屏
│   │   ├── ocr.py            # OCR 封装 → [OcrItem(text, box, center, conf)]
│   │   └── locate.py         # 区域锚点(相对比例)+ find_text/find_template
│   ├── actions\
│   │   ├── human_input.py    # 仿真人键鼠:贝塞尔轨迹/随机停顿/打字节奏
│   │   └── flows.py          # 高层流程:open_chat/read_messages/send_message/check_unread
│   ├── agent\                # 可插拔智能体接入层(harness)
│   │   ├── base.py           # AgentBackend 协议 + 工厂函数
│   │   ├── echo.py           # 回声测试模式(默认,零配置可跑)
│   │   ├── openai_compat.py  # OpenAI 兼容 API(base_url+api_key+model)
│   │   └── webhook.py        # HTTP 回调模式(外部 harness 接入)
│   ├── engine\
│   │   ├── monitor.py        # 监控循环:感知→检测→回复→验证
│   │   ├── state.py          # 消息指纹去重 + 每联系人会话上下文
│   │   └── safety.py         # 白名单校验/限频/发送前二次确认钩子
│   └── server\
│       ├── main.py           # FastAPI 入口 + 静态文件 + 生命周期
│       └── events.py         # 日志总线 + WebSocket 推送
├── web\
│   ├── index.html            # 单页控制台(小白向,中文)
│   ├── app.js
│   └── style.css
├── tests\                    # 离线回归 + 只读实机验收
├── docs\                     # research 01-03 + 本文件 + 协议/截图
├── config.json               # 运行时配置(首次运行自动生成默认值)
├── logs\                     # 运行日志(轮转,上限50MB)
├── run.py                    # 一键启动 → 起服务+自动开浏览器
├── requirements.txt
└── README.md                 # 小白向使用说明
```

## 3. 模块接口契约(实现必须遵守的签名)

```python
# vision/window.py
class WeChatWindow:
    @classmethod
    def find(cls) -> "WeChatWindow | None"     # class=Qt51514QWindowIcon 且 title=微信
    def ensure_active(self) -> bool            # 最小化则SW_RESTORE;置前(允许最小闪烁方案)
    rect_screen: tuple[int, int, int, int]     # 屏幕绝对坐标
    def capture(self, use_printwindow: bool = True) -> "np.ndarray"  # BGR

# vision/ocr.py
@dataclass
class OcrItem:
    text: str; box: tuple; center: tuple[int, int]; conf: float
def run_ocr(img_bgr) -> list[OcrItem]

# vision/locate.py
REGIONS = {...}   # 窗口相对比例锚点:nav/chat_list/message_area/input_box/search_box/title_bar/send_btn
                 # 依据实机像素标定(过程记录不入库),运行时再经 OCR/模板精修
def find_text(items: list[OcrItem], pattern: str, region=None) -> OcrItem | None
def find_template(img, tpl_name: str, region=None, threshold=0.8) -> Match | None

# actions/human_input.py
class HumanInput:
    def move_to(self, x: int, y: int)          # 三阶贝塞尔+随机控制点+变速
    def click(self, x: int, y: int)            # 移动→停顿80-400ms→按下/抬起分离
    def double_click / right_click
    def press(self, *keys: str)                # 组合键
    def type_text(self, text: str)             # 中文:剪贴板+Ctrl+V;英文数字:可选逐字符随机节奏
    def human_delay(self, lo: float, hi: float)

# actions/flows.py
@dataclass
class Message:
    sender: str          # "self" | 联系人名
    text: str
    side: str            # "left"|"right" 三信号判定:气泡颜色(#95E29E≈自己)+左右位置+左右头像区方差/颜色评分(wxauto4算法,头像侧=发送者)
    ts: str | None

class WeChatFlows:
    def __init__(self, win, hi: HumanInput, ocr_fn)
    def open_chat(self, name: str) -> bool
        # 点搜索框→清空→剪贴板输入name→等待结果→OCR结果列表→点击 name 精确匹配项
        # →等绘制→OCR标题栏验证==name,失败重试1次后放弃并返回 False
    def read_messages(self) -> list[Message]
    def send_message(self, text: str, expect_contact: str) -> bool
        # 【红线】发送前 OCR 标题栏必须==expect_contact(且expect_contact必须在白名单)
        # 点输入区→粘贴→(Enter 或 点发送按钮,按实测可用性定)→等待→OCR验证新气泡出现
    def check_unread(self) -> list[str]        # 先扫导航"聊天"图标区红色像素(有无未读,毫秒级预检,wxauto IsRedPixel 思路)→ 再OCR聊天列表角标 → 未读联系人名列表

# agent/base.py
class AgentBackend(Protocol):
    def name(self) -> str
    def healthcheck(self) -> bool   # WebUI 状态卡显示后端健康度(调研03)
    def reply(self, contact: str, history: list[Message], incoming: Message) -> str | None
def create_backend(cfg: AgentConfig) -> AgentBackend   # mode: echo|openai|webhook

# engine/monitor.py
class MonitorEngine:
    def start(self) / stop(self) / status() -> dict
    # 循环:ensure_active → check_unread ∩ whitelist → open_chat → read_messages
    #       → state.dedup(新消息) → agent.reply → safety.check(限频/白名单)
    #       → send_message(verify=contact) → 事件推送WebUI
```

## 4. 核心时序

**发送消息(手动/自动同路)**:
```
ensure_active → PrintWindow截图 → OCR标题栏 → [红线校验:==白名单目标]
→ 点击输入区(输入区锚点OCR精修) → human_delay → 剪贴板置入文本 → Ctrl+V
→ human_delay → 点击"发送(S)"按钮(备用:Enter/Alt+S) → 等300-800ms → 再截图OCR验证
   自己气泡出现且文本一致 → 成功;否则重试1次;仍失败→告警+中止该条
```

**监控循环(默认 5s 间隔,可配)**:
```
check_unread → 有白名单内未读 → open_chat(该联系人) → read_messages
→ 去重出新增 → 逐条喂 agent(带最近20条上下文) → 限频检查 → 逐条 send_message
```

## 5. 关键设计决策

1. **感知与行动分离**:感知可用 PrintWindow(抗遮挡);**行动(点击)必须窗口前台可见**——点击是屏幕绝对坐标,遮挡时会点错窗口。ensure_active 负责保证。
2. **坐标策略**:区域锚点存"窗口相对比例"(依据实机像素标定,过程记录不入库),运行时用 OCR 文字/模板匹配把锚点精修到像素——窗口大小变化不崩。
3. **中文输入**:剪贴板 + Ctrl+V(pyautogui 无法直接输入中文)。粘贴前后保存/恢复用户原剪贴板内容。英文/数字可选逐字符随机节奏模式(更仿真,慢一些)。
4. **发送确认键**:优先**点击"发送(S)"按钮**(模板匹配定位,视觉可验证);Enter、Alt+S(4.1.6+ 实测可用)作备用,启动自检时按可用性选定。**不得依赖修改用户微信设置**。
5. **去重(调研03采纳)**:指纹 = hash(联系人+发送者+规范化OCR文本),有界缓存(200条,TTL 10分钟),排除自己气泡;同一指纹只触发一次回复。会话上下文每联系人独立、内存保存、上限 20 条。**引擎线程模型**:采集线程(感知)与工作线程(智能体+发送)分离,发送加串行锁;自动回复默认关闭(auto_reply_enabled=false,WebUI 主动开启),开启后每次自动回复前附加 1~3s 随机延迟。
6. **限频**:每联系人每分钟最多 3 条自动回复(可配),超限静默丢弃并记日志——防刷屏、防封号。
7. **Agent 接入(可插拔 harness)**:三种模式配置切换——echo(默认,回复固定测试语句,零配置即可端到端自测)、openai(兼容 GLM/DeepSeek/OpenAI/Ollama)、webhook(POST {contact, history, incoming} 到用户 URL,取 JSON {reply})。智能体返回 None = 不回复。
8. **WebUI 面向小白**:全中文;首页即状态卡(微信窗口是否找到/引擎运行状态/当前模式);配置表单分组且有默认值;手动发送带目标显示+确认弹窗;实时日志(WebSocket);危险操作(改白名单/清上下文)二次确认。
9. **日志**:logs/ 按大小轮转;WebUI 看到的是内存环形缓冲(最近500条)。

## 6. 测试与验收标准

| 层 | 范围 | 验收 | 发送权限 |
|---|---|---|---|
| 感知与操作 | vision/ + actions/(window/ocr/locate/human_input/flows) | `python tests/test_readonly.py` 退出码0:找到窗口→open_chat(白名单[0]) 标题校验通过→read_messages 返回结构化消息;全程零发送、不触碰聊天输入框(human_input 仅剪贴板往返自测) | ❌ 禁止发送 |
| 引擎 | agent/ + engine/ + config | echo 模式:模拟一条白名单联系人新消息 → 引擎产出回复(经 safety)→ **dry_run 下不实际发送,记录将发送内容** | ❌ dry_run |
| 服务端 | server/ + web/ + run.py | WebUI:状态/配置/日志/启停/手动发送(白名单联系人,带确认)全部可用 | ✅ 仅白名单成员(需开启 allow_real_send) |
| 实机端到端 | 全链路 | 真实发送 2-3 条测试消息给白名单联系人,OCR 验证送达;LLM 模式(若用户提供 key)端到端 | ✅ 仅白名单成员 |

**验收纪律**:每层先跑离线回归(tests/,零微信交互),再跑只读实机验收;任何真实发送前先 dry_run 演练;代码改动后逐层重跑对应验收。

## 7. 调研结论(2026-10-23,设计定稿)

01-03 号调研文档已完成,结论如下(实现必须执行):
1. OCR=RapidOCR(pip 包 `rapidocr`+`onnxruntime`),原生返回四点坐标框+文本+置信度 ✅
2. 键鼠=pyautogui(PAUSE=0,FAILSAFE 不关)+pyperclip+pynput(急停);贝塞尔轨迹自实现,human-mouse 库仅参考 ✅
3. 发送=优先点击"发送(S)"按钮(模板匹配),Enter / Alt+S(4.1.6+ 实测)备用 ✅
4. 新消息检测=导航"聊天"图标区红色像素预检(快)+聊天列表角标 OCR(定位来源)✅
5. 消息方向=气泡颜色+左右位置+头像区评分三信号 ✅
6. 引擎=采集线程与工作线程解耦+发送串行锁;自动回复默认关闭,开启后回复加 1~3s 随机延迟 ✅
7. Agent 接口增加 healthcheck();注册表工厂模式 ✅
8. WebUI=原生单页无 CDN(调研03确认无现成模板,CDN 断网不可用,维持原生)✅
9. requirements.txt 版本号以 docs/research/02-vision-stack.md §7 为准(2026-10-23 PyPI 核对)✅
10. PrintWindow 后台截屏:调研02称无公开证据,但实机已验证可用,维持 PrintWindow 优先 ✅

01 号文档中标注"待实机验证"的条目,应在实现过程中逐项实测,结论记入 issues。
