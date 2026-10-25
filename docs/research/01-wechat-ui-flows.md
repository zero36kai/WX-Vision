# 微信 Windows 4.x 客户端 UI 操作流程调研(供"纯视觉+键鼠模拟"方案复用)

> 调研日期:2026-10-23
> 调研范围:微信 Windows 4.x(进程 `Weixin.exe`)。微信官网当前同时提供 **4.1.15(64位)** 与 **3.9.12(32位)** 下载(https://windows.weixin.qq.com/?lang=zh_CN)。
> 调研方法:网络调研(wxauto/wxauto4 官方文档、3 个开源项目源码、技术博客)。所有结论附来源;网上查不到 4.x 实况的条目已标注 **【待实机验证】**。
> 核心依据源码(本次已实际抓取阅读):
> - `cluic/wxauto`(面向微信 3.9,UIAutomation)— `wxauto/wxauto.py`、`wxauto/elements.py`(github.com/cluic/wxauto, main 分支)
> - `AngeCoo/wxauto-4.0`(面向微信 4.0.5,UIA+截图混合)— `wxauto4/ui/main.py`、`sessionbox.py`、`chatbox.py`、`utils/tools.py`、`utils/win32.py`、`msgs/msg.py`(github.com/AngeCoo/wxauto-4.0, main 分支)
> - `Hello-Mr-Crab/pywechat` 之 `pyweixin`(面向微信 4.1.6+,pywinauto+pyautogui)— `Mcp/pyweixin_rpa/pyweixin/WeChatAuto.py`、`Uielements.py`(github.com/Hello-Mr-Crab/pywechat, main 分支)

---

## 0. 一页结论(最重要的可复用知识)

1. **微信 4.x 收紧了 UIAutomation 无障碍树**:新注册账号即使开启"讲述人"也拿不到 UI 树,官方文档明确 4.x "只有屏幕可见范围内的消息才会注册 UI 控件"(虚拟化渲染)。**OCR/纯视觉因此成为 4.x 上最稳的感知途径**(来源:pywechat `Weixin4.0.md` https://github.com/Hello-Mr-Crab/pywechat;wxauto4 文档 https://docs.wxauto.org/llms-full.txt)。
2. **三大开源项目的发送动作高度一致:剪贴板复制 → 点输入框 → Ctrl+V → Enter / Alt+S / 点"发送(S)"按钮**。直接 `type_keys` 中文会受输入法干扰,所有成熟项目一律走剪贴板(来源:wxauto `wxauto.py:420-429`;pyweixin `WeChatAuto.py:1897-1901`;AngeCoo `chatbox.py:124-153`)。
3. **未读检测可纯视觉化**:wxauto 判断"是否有新消息"就是截取左侧导航"聊天"图标区域、检查是否存在 `R>G 且 R>B` 的红色像素(`IsRedPixel`,wxauto `wxauto.py:179-182`、AngeCoo `win32.py:373-378`);会话未读数在角标中显示为 `[N条]`(AngeCoo `sessionbox.py:160-167`)。
4. **自己/对方消息的判定就是"截图看左右"**:wxauto4 对每条消息截图后用列扫描/左右头像区方差+颜色多样性算法判定方向,left=friend、right=self(AngeCoo `msgs/msg.py:26-56`、`utils/tools.py:133-230`)——这正是纯视觉方案可直接搬用的算法。
5. **4.x 窗口关键标识**(来自 AngeCoo/wxauto-4.0 源码,适配 4.0.5):Win32 窗口类名 `Qt51514QWindowIcon`(Qt 5.15.14);主窗口 UIA 类名 `mmui::MainWindow`、**窗口标题就是"微信"**;独立聊天窗口 UIA 类名 `mmui::FramelessMainWindow`、**标题=联系人昵称**。3.9 对应为 `WeChatMainWndForPC` / `ChatWnd`。

---

## ① 推荐操作流程(纯视觉+键鼠复刻版)

### 流程总览

```
[0] 启动/激活微信窗口
      ↓
[1] 搜索联系人 (Ctrl+F 或点击左上搜索框 → 粘贴关键词 → 等待 → OCR 匹配结果 → 点击)
      ↓
[2] 打开聊天 (单击结果;如需稳定监听 → 双击开独立窗口)
      ↓
[3] 读取消息 (截消息区 → OCR → 按"左右位置+颜色"区分自己/对方/系统 → 解析类型)
      ↓
[4] 发送消息 (复制到剪贴板 → 点输入框 → Ctrl+V → 校验 → Enter 或 Alt+S → 校验输入框已清空)
      ↓
[5] 检测新消息 (轮询:导航"聊天"图标红点像素 / 会话列表角标 OCR / 独立窗口消息区尾部变化)
      ↺ 回到 [1]
```

### [0] 前置:启动与激活窗口

| 项 | 内容 |
|---|---|
| 进程/窗口识别 | 4.x 进程 `Weixin.exe`(3.9 为 `WeChat.exe`);主窗口 Win32 类名 `Qt51514QWindowIcon`,标题"微信";3.9 为 `WeChatMainWndForPC`(来源:cnblogs wxauto4 教程 https://www.cnblogs.com/travel92/p/19812181;AngeCoo `ui/main.py:24-32,129-132`;wxauto `wxauto.py:26`) |
| 视觉特征 | 任务栏绿色微信图标;主窗口顶部为三栏布局(见 ②) |
| 键鼠动作 | wxauto 的做法:`ShowWindow(hwnd,1)` → `SetWindowPos` 置顶再取消置顶 → `SwitchToThisWindow`(wxauto `wxauto.py:85-90`)。纯视觉等价:用 win32gui 找到 hwnd 后 `SetForegroundWindow`,或点击任务栏图标 |
| 坑 | **窗口不能最小化、不能被完全遮挡**:4.x 只为"屏幕可见部分"注册控件(wxAUTO 文档),纯视觉更是要求截图可见。wxauto 初始化默认 `resize=True` 自动调整窗口尺寸 |

### [1] 搜索联系人并打开聊天

**视觉特征/定位策略:**
- 搜索框位于**会话列表列顶部**(第一栏顶部),放大镜图标+灰色占位字"搜索"(4.x 控件:`mmui::XSearchField` 内 EditControl,占位标题"搜索",类 `mmui::XValidatorTextEdit`。来源:AngeCoo `sessionbox.py:27`;pyweixin `Uielements.py` `Edits.SearchEdit`)
- 搜索结果以**弹出层**出现在窗口中部偏左(4.x 控件 `mmui::SearchContentPopover`,AngeCoo `sessionbox.py:30`);结果项文本含联系人名及灰色后缀" 微信号: xxx"/" 昵称: xxx"(AngeCoo `sessionbox.py:84-98`)
- **命中关键词会高亮显示**(3.9 中 UIA Name 为 `<em>名字</em>`,wxauto `wxauto.py:321`)——视觉上关键词变色,可作为模板/颜色特征

**键鼠动作序列(综合三大项目):**
1. (可选快路径)目标已在会话列表中 → 直接点该会话项,跳过搜索(wxauto `ChatWith` 优先走会话列表直点,`wxauto.py:313-317`)
2. 否则:`Ctrl+F` 激活搜索(3.9 实证,wxauto `wxauto.py:319`)或直接点击搜索框。**4.x 的 Ctrl+F 聚焦行为【待实机验证】**(4.x 设置中存在可自定义的"激活搜索"快捷键项,见 ③)
3. **将关键词写入剪贴板 → Ctrl+V 粘贴**(AngeCoo 用右键菜单"粘贴"实现,`sessionbox.py:46-65`;pyweixin 用 `set_text`,`WeChatAuto.py:1999-2000`)。避免直接键入中文触发输入法
4. 等待 0.5~1.5s(实测值:pyweixin 0.8s,wxauto 1.5s,wxAUTO4 `SEARCH_CHAT_TIMEOUT=2s`,AngeCoo `param.py`)
5. OCR 结果弹层:优先点"完全等于关键词"的项;其次匹配" 微信号: / 昵称: "后缀完全相等的项;最后点第一个结果(wxAUTO4 匹配逻辑,AngeCoo `sessionbox.py:78-103`;wxauto 精确匹配优先,`wxauto.py:321-336`)
6. 单击结果项 → 右侧打开聊天。**用消息区顶部标题 OCR 验证是否打开成功**(4.x 标题栏 AutomationId `current_chat_name_label`,群聊还有 `(N)` 人数标签 `current_chat_count_label`,AngeCoo `chatbox.py:63-96`)
7. (可选,监听用)**双击**会话项 → 弹出独立聊天窗口(标题=昵称)。wxauto4 `open_separate_window` = switch_chat → 双击(AngeCoo `sessionbox.py:107-118`);pyweixin `Navigator.open_seperate_dialog_window`;无障碍键盘路径:会话列表中按 Enter 也可打开独立窗口(nvdacn https://www.nvdacn.com/index.php/archives/947)。**双击开独立窗口在 4.x 的等效操作【待实机验证】**
8. 按 `Esc` 关闭搜索弹层(AngeCoo `sessionbox.py:276-277`)

### [2] 读取消息列表

**视觉特征/定位策略:**
- 消息区在主窗口第三栏;4.x 控件为 `mmui::ChatMessagePage` → `mmui::XSplitterView` → 消息列表 `mmui::MessageView`(ListControl)(AngeCoo `ui/main.py:162-164`、`chatbox.py:104`)
- **4.x 为虚拟化渲染:只有可见的消息才有 UI/在屏幕上**(wxauto4 文档明示,https://docs.wxauto.org/llms-full.txt)。对纯视觉这是天然一致:只 OCR 可见区域,滚动即"翻页"
- 消息行构成(从上到下):时间分割线(居中灰色小字)、系统消息(居中灰条)、普通消息(左=对方/右=自己)
- 自己 vs 对方:右侧=自己,绿色气泡;左侧=对方,白色/浅灰气泡;深色模式下绿色变暗、对方为深灰底(来源:少数派深色模式分析 https://sspai.com/post/59609;PC 4.x 具体色值【待实机验证】,手机端经典绿约 #95EC69 可作初值)
- wxauto4 判向算法(可直接移植):
  - 基础版:取消息行截图中部 80% 高度带,从左向右/从右向左扫描,第一个"颜色不均匀列"(出现气泡/头像)所在侧即发送方,同时得到气泡起始 x 列(AngeCoo `utils/tools.py:133-178`)
  - 增强版:比较左右两端 15% 宽度区域(头像位置)的像素方差(权重0.7)+颜色多样性(权重0.3),得分高的一侧有头像(AngeCoo `utils/tools.py:269-310`)。**头像在哪侧,发送者就在哪侧**
- 消息类型区分(供 OCR 后分类):文本/引用(带灰色引用块)/图片(圆角图块)/语音(带条形声纹+时长)/文件(文件名+大小图标)/合并转发/红包/转账/系统提示(来源:wxauto4 Message.type 枚举 text,quote,voice,image,video,file,location,link,emotion,merge,personal_card,note,other,https://docs.wxauto.org/llms-full.txt)
- 点击某条消息的坐标:wxauto4 用"头像坐标 + X 偏移 51px、Y 偏移 30px"定位气泡(`DEFAULT_MESSAGE_XBIAS=51/YBIAS=30`,AngeCoo `param.py`;wxAUTO 文档同)— 该数值可作视觉方案点击气泡的初始偏移参考
- 翻历史消息:在消息区滚轮向上;停止条件="再滚内容不再移动"(wxauto `LoadMoreMessage` 实现,`wxauto.py:506-527`);4.x 官方库改为 `GetHistoryMessage` 回调式(https://docs.wxauto.org/llms-full.txt)

**键鼠动作序列:**
1. 截取消息区矩形 → OCR 全部可见行 → 按行分类(时间线/系统/左侧/右侧)
2. 需要更早消息:鼠标悬停消息区 → `WheelUp` N 格 → 再截再 OCR,直到目标内容出现或滚动无效
3. 需要点击某条消息(引用/复制等):OCR 定位其气泡矩形 → 右键单击弹出上下文菜单 → OCR 菜单项后点击(复制/引用/转发/撤回等菜单文字)

### [3] 发送文本消息

**视觉特征/定位策略:**
- 输入框:消息区底部大编辑区(4.x 控件 `mmui::ChatInputField`,auto_id `chat_input_field`;AngeCoo `chatbox.py:105`、pyweixin `Uielements.py` `Edits.CurrentChatEdit`)
- 输入框上方是工具栏:表情、发送文件、截屏、语音聊天等图标;右下角"发送(S)"按钮(4.x 按钮标题"发送(S)",AngeCoo `chatbox.py:106`、pyweixin `Uielements.py` `SendButton`)。无内容时按钮通常呈禁用态(灰色)【待实机验证 4.x 具体颜色】
- 发送成功的外观验证:输入框清空 + 消息区底部出现新的右侧绿色气泡(AngeCoo 发送循环即以"输入框 Value 变空"为成功判据,`chatbox.py:143-153`)

**键鼠动作序列(本项目主推,与三大项目一致):**
1. (保险)清空输入框:点输入框 → `Ctrl+A` → `Delete`(AngeCoo `clear_edit`,`chatbox.py:117-121`)
2. 把文本写入剪贴板 → 点输入框(确保焦点)→ `Ctrl+V`(wxAUTO4 失败重试路径:再 Ctrl+V 一次,仍失败则右键菜单"粘贴",`chatbox.py:124-142`)
3. 校验:OCR 输入框区域,与待发文本比对。wxauto 用相似度阈值 0.9(`SEND_CONTENT_RATIO=0.9`,因特殊字符转码不会 100% 相等,https://docs.wxauto.org/llms-full.txt)
4. 发送三选一:
   - 按 `Enter`(默认设置下)
   - 按 `Alt+S`(pyweixin 在 4.1.6+ 实测使用 `pyautogui.hotkey('alt','s')` 发送,`WeChatAuto.py:1902-1904`——与"发送(S)"按钮助记键吻合)
   - OCR 定位并点击"发送(S)"按钮(AngeCoo 用点击按钮发送,`chatbox.py:149`;**最稳,不依赖用户改过设置**)
5. 校验发送成功:OCR 输入框已清空;消息区尾部新增右侧气泡且内容一致
6. **长文本:PC 微信单条上限 2000 字**,超限转成 txt 文件发送(pyweixin `WeChatAuto.py:1899-1901`)

**发送文件/图片:**
1. 把文件路径列表写入剪贴板(资源管理器格式)→ 点输入框 → `Ctrl+V` → 消息区出现文件/图片卡片缩略图 → `Alt+S` 或点"发送(S)"(pyweixin `WeChatAuto.py:1904-1933`;AngeCoo `chatbox.py:168-178` 用 `SetClipboardFiles`+`Ctrl+V`+点按钮)
2. **单次最多 9 个文件**,超过需分批(pyweixin 源码注释"必须9个9个发")

**群聊 @:**
1. 在输入框键入 `@` → 弹出成员选择浮窗(4.x 控件 `mmui::XPopover`,AutomationId `MentionPopover`;AngeCoo `chatbox.py:363-374`;3.9 中为 `ChatContactMenu`,wxauto `wxauto.py:365`)
2. 继续键入成员名过滤(wxAUTO4 做法:`'@'+名字`,`chatbox.py:184-187`)或 OCR 浮窗列表点击目标;选"所有人"即 @全体成员(wxauto `AtAll`,`wxauto.py:338-373`)
3. `Esc` 取消浮窗;@失败时退格删掉已输入的 `@名字`(AngeCoo `AtMenu.clear`,`chatbox.py:376-381`)

### [4] 检测新消息(三种可纯视觉化的机制)

| 机制 | 做法 | 来源 |
|---|---|---|
| **A. 导航图标红点(像素检测)** | 截取左侧导航"聊天"图标小矩形,存在 `R>G 且 R>B` 像素 → 有新消息。wxauto 的 `CheckNewMessage` 就是这么实现的;AngeCoo 的 `IsRedPixel` 用 `capture(hwnd,bbox)` 截图后逐像素判断 | wxauto `wxauto.py:179-182`;AngeCoo `win32.py:373-378` |
| **B. 会话列表角标 OCR** | 双击导航"聊天"图标回到会话页并刷新(wxAUTO3.9 `A_ChatIcon.DoubleClick`,`wxauto.py:220`);OCR 会话列表,未读角标文本形态 `[N条]`;免打扰会话只显示小圆点(数字变圆点)【圆点样式待实机验证】 | wxauto `GetNextNewMessage`,`wxauto.py:184-233`;AngeCoo `sessionbox.py:160-167` |
| **C. 独立窗口消息区尾部监控** | 双击会话开独立窗口(标题=昵称),轮询消息区最后一条(间隔 1s,`LISTEN_INTERVAL=1`);以"消息条数变化/尾部内容 OCR 变化"判定新消息。官方 wxautox4 把监听窗口拉到 800×6000 尽量容纳更多消息以抗虚拟化 | https://docs.wxauto.org/llms-full.txt;AngeCoo `chatbox.py:get_new_msgs:198-282` |

**推荐纯视觉组合**:**A(有/无新消息,零成本轮询)+ B(定位是谁来的消息)+ C(对重点会话常驻独立窗口监听)**。
注意 4.x 的坑:虚拟化导致"消息一闪而过会丢失锚点",官方建议监听窗口尽量拉高、甚至用竖屏显示器(wxAUTO 文档)。

---

## ② 微信 4.x 布局与视觉特征清单

### 窗口层级

| 对象 | Win32 | UIA 类名(4.x) | 标题/名称 | 来源 |
|---|---|---|---|---|
| 主窗口 | ClassName=`Qt51514QWindowIcon` | `mmui::MainWindow` | **"微信"** | AngeCoo `ui/main.py:129-132` |
| 独立聊天子窗口 | `Qt51514QWindowIcon`(同主窗口类名,多实例) | `mmui::FramelessMainWindow` | **联系人昵称** | AngeCoo `ui/main.py:24-26`;`FindWindow(classname, name=昵称)` |
| 弹出资料卡 | — | `mmui::ProfileUniquePop` | 数字 | pyweixin `WeChatAuto.py:1813` |
| @ 成员浮窗 | — | `mmui::XPopover`(AutomationId `MentionPopover`) | "Weixin" | AngeCoo `chatbox.py:363-374` |
| 搜索结果弹层 | — | `mmui::SearchContentPopover` | — | AngeCoo `sessionbox.py:30` |
| 公众号文章/小程序内嵌页 | 进程 `WeChatAppEx.exe`(勿禁用) | — | — | pywechat README https://github.com/Hello-Mr-Crab/pywechat |

> 3.9 对照:主窗口 `WeChatMainWndForPC`,独立窗口 `ChatWnd`(标题=昵称),图片查看器 `ImagePreviewWnd`,群设置 `SessionChatRoomDetailWnd`,合并记录 `ChatRecordWnd`,通讯录管理 `ContactManagerWindow`(wxauto `wxauto.py`/`elements.py`)。**纯视觉方案主要用窗口标题+类名定位 hwnd,再用屏幕坐标工作,类名差异必须适配(见 ④)。**

### 主窗口三分栏(左→右)

1. **左侧导航栏**(`mmui::MainTabBar`,AutomationId `main_tabbar`;约 60~80px 宽,垂直图标列):
   图标自上而下:**头像、聊天(微信)、通讯录、收藏、聊天文件、朋友圈、搜一搜、视频号、看一看**;底部:**小程序面板、手机、更多(⚙设置)**(AngeCoo `ui/navigationbox.py:14-26` 按钮标题表)。
   视觉要点:"聊天"图标带未读红点(新消息检测锚点,见 ①[4]A)。
2. **会话列表栏**(`mmui::ChatMasterView` → `mmui::ChatSessionList` → 列表 `mmui::XTableView`,Name="会话";AngeCoo `sessionbox.py:27-29`):顶部搜索框(`mmui::XSearchField`,占位字"搜索");每个会话项=头像+昵称+时间+摘要+未读角标(`[N条]`);**置顶会话常驻列表头部且背景色略深**(视觉特征【待实机验证具体色值】);列表滚动定位可用 Home/End 键跳顶/底(AngeCoo `go_top/go_bottom`,`sessionbox.py:121-129`)
3. **聊天区**(`mmui::ChatMessagePage` → `mmui::XSplitterView`):顶部标题栏(昵称,群聊带 `(N)` 成员数;右上角"聊天信息"三点按钮)、中部消息列表(`mmui::MessageView`)、底部输入工具栏+输入框(`mmui::ChatInputField`)+右下角"发送(S)"按钮(AngeCoo `chatbox.py:103-107`;pyweixin `Uielements.py`:ChatInfoButton=“聊天信息”、QuickActionsButton(＋号)=“快捷操作”)

### 颜色与样式

- 4.0 全新 UI:圆角、立体感、浅灰+白配色,适配 Win11 风格(快科技 https://news.mydrivers.com/1/1039/1039280.htm)
- 消息气泡:**自己=右侧绿色,对方=左侧白/浅灰**;深色模式下绿色变暗降饱和、对方深灰底(少数派 https://sspai.com/post/59609;站酷分析 https://m.zcool.com.cn/article/ZMTExNzc4NA==.html)。PC 4.x 精确色值【待实机验证】
- 深色模式:设置 → 通用 → 外观,可选 浅色/深色/跟随系统(简录 https://www.jianlu365.com/info/2737.html)。**做模板匹配必须锁定外观模式**
- 会话列表:灰底,选中项背景为微信绿(网易报道 4.0 新界面 https://www.163.com/dy/article/KO2GDECM0511RFLI.html)【待实机验证】

### 消息类型视觉特征(OCR/模板分类用)

| 类型 | 视觉特征 |
|---|---|
| 时间分割线 | 居中、小号灰字("昨天 20:15") |
| 系统消息 | 居中灰条("你邀请了…进群") |
| 文本 | 纯文字气泡,左灰右绿 |
| 引用 | 气泡内嵌灰色小块(被引内容+发送者) |
| 图片/表情 | 圆角图块;表情较大无明显文字 |
| 语音 | 声纹条形图+秒数;右侧绿底(自己) |
| 文件 | 文件名+大小+类型图标横条 |
| 红包 | 橙色横幅卡片,点击弹"拆开"按钮(pyweixin `Buttons.OpenButton`=“拆开”) |
| 转账 | 橙色卡片+"收款"按钮 |
| 合并转发 | "聊天记录"卡片,点开弹 `ChatRecordWnd` 类窗口(3.9 类名;4.x【待实机验证】) |
(类型枚举来源:wxAUTO 文档 https://docs.wxauto.org/llms-full.txt)

---

## ③ 快捷键清单

### 全局快捷键(微信未聚焦也可用,可在 设置→快捷键 修改)

| 快捷键 | 功能 | 来源 |
|---|---|---|
| `Ctrl + Alt + W` | 显示/隐藏微信主界面(默认值) | nvdacn https://www.nvdacn.com/index.php/archives/947;cnblogs https://www.cnblogs.com/zhangyunxi/p/Shortcut-in-wechat-for-windows.html |
| `Alt + A` | 截屏工具(默认值) | 同上 |

### 发送/换行(设置→快捷键 二选一)

| 快捷键 | 功能 | 来源 |
|---|---|---|
| `Enter` | 发送消息(默认);此时 `Ctrl+Enter` 为换行 | 同上;简录 https://www.jianlu365.com/info/2738.html |
| `Ctrl + Enter` | 发送消息(用户改过后);此时 `Enter` 为换行 | 同上;百度经验 https://jingyan.baidu.com/article/c1a3101eba06d39f646deb71.html |
| `Alt + S` | **发送按钮"发送(S)"的助记键**;pyweixin 在微信 4.1.6+ 上实测用 `pyautogui.hotkey('alt','s')` 完成发送 | pyweixin `WeChatAuto.py:1902-1933`(源码实证) |
| 通用编辑 | `Ctrl+A` 全选 / `Ctrl+V` 粘贴 / `Delete` 删除(wxAUTO/pyweixin 源码大量使用) | wxauto `wxauto.py:404,426,429`;AngeCoo `chatbox.py:117-121` |

### 键盘导航/无障碍快捷键(主窗口内;源自 NVDA 视障用户社区,3.9 时代文档)

| 快捷键 | 功能 | 来源 |
|---|---|---|
| `Alt + L` | 焦点移至会话列表 | cnblogs/zhangyunxi(同上) |
| `空格` | 会话列表中,将所选聊天平铺到主界面右侧 | 同上 |
| `Enter` | 会话列表中,以**独立窗口**打开所选聊天 | 同上 |
| `Alt + M` | 焦点移至消息列表(可浏览,Applications 键出右键菜单) | 同上 |
| `Alt + S` | 焦点移至消息输入框;**再按一次直接发送** | 同上 |
| `Alt + C` | 关闭当前窗口 | 同上 |
| **在 4.x 是否全部保留:【待实机验证】** | | |

### 搜索与列表滚动(源码实证)

| 快捷键 | 功能 | 来源 |
|---|---|---|
| `Ctrl + F` | 聚焦搜索框(3.9 实证;设置中有"激活搜索"可自定义项;4.x 默认值【待实机验证】) | wxauto `wxauto.py:319`;企业微信官方帮助(类似设置项说明)https://open.work.weixin.qq.com/help2/pc/13352 |
| `Esc` | 关闭搜索弹层/图片查看器/设置窗口等 | AngeCoo `sessionbox.py:277`;wxauto `elements.py:398,507` |
| `Home` / `End` | 会话列表跳到顶部/底部(列表聚焦后) | AngeCoo `sessionbox.py:121-129` |
| `Ctrl + 滚轮` | 4.x 支持字体大小调整(掘金 4.0 架构文)https://juejin.cn/post/7455984186912129074 【具体键待实机验证】 | |

> 设置→快捷键 可自定义项(4.x):发送消息按键、截屏、打开微信(显示/隐藏)、锁定微信(掘金 https://juejin.cn/post/7532753909337604096)。**纯视觉方案应在跑流程前 OCR 该设置页确认用户实际绑定,或直接点击"发送(S)"按钮规避 Enter/Alt+S 不确定性。**

---

## ④ 风险与坑(纯视觉方案专项)

1. **弹窗遮挡与浮层**
   - 搜索结果弹层、@浮窗(MentionPopover)、右键菜单、图片查看器、朋友圈窗口、二维码登录窗、**顶部"当前网络不可用"横幅**(pyweixin `Buttons.OffLineButton`,Uielements.py:64)都会遮挡布局;发送前应检测输入框区域可见性。
   - 语音/视频来电弹窗(接听/拒绝按钮)会突然抢占前台(pyweixin `AnswerButton/DeclineButton`)。
2. **置顶聊天**:置顶会话常驻会话列表头部,"新消息排到最上"的假设失效;wxauto 因此 ChatWith **优先搜索而不是按位置点列表**(wxauto `wxauto.py:303-336` 哲学:以名字匹配,不信位置)。纯视觉同样要**以 OCR 文本定位,不要记坐标/序号**。
3. **会话顺序漂移**:非置顶会话收到新消息会移动到列表最上(除非开了免打扰/置顶),两帧之间的坐标全部失效;每次操作前重新定位。
4. **输入法状态**:直接向输入框注入中文键序可能被 IME 拦截/重组。**三大项目一致采用剪贴板+Ctrl+V**,无一处直接 type 中文(除 pyweixin 添加好友的纯数字/字母输入)。粘贴前先 `Ctrl+A`+`Delete` 清空,防残留拼串。
5. **发送校验不能只看"按了键"**:特殊字符粘贴后显示形式会变,wxAUTO 用 0.9 相似度而非全等(`SEND_CONTENT_RATIO`,wxAUTO 文档);纯视觉可用"输入框已清空+消息区尾部新气泡"双重校验。
6. **多显示器 / DPI 缩放**:
   - Qt 窗口在 DPI 缩放下,逻辑坐标与物理像素不一致;截图应统一用物理像素(如 `GetDpiForWindow` 换算),模板匹配模板图必须在**相同缩放比+相同主题(浅/深色)+相同字号**下截取。
   - wxAUTO 官方提到"有些系统显示器最大分辨率上限小"会挤压 800×6000 监听窗口,建议高分辨率/竖屏(wxAUTO 文档)。
   - 【待实机验证】4.x 在 125%/150% 缩放下的坐标换算与边缘圆角遮挡。
7. **窗口状态**:最小化/完全遮挡时 4.x 不渲染目标区域(虚拟化只注册可见控件,wxAUTO 文档);纯视觉必须保证微信在前台且目标区域无遮挡。注意 4.x 主窗口新增"置顶"小钉按钮(PinButton,pyweixin `Uielements.py:90-91`),置顶后窗口可能压在别的应用上,截图前先 SetForegroundWindow。
8. **4.x 虚拟化渲染**:滚出屏幕的历史消息 UI 即被卸载,滚回后运行时 ID 改变——UIA 方案因此翻车(wxAUTO 博客说明),纯视觉反而免疫;但"长截图拼接历史"时必须自己维护滚动偏移,防止重复/丢行。
9. **深色模式/外观切换**:跟随系统的深色模式会让所有颜色模板失效;方案内固定设置→通用→外观,或准备两套模板。
10. **语言版本**:控件文字有三语(简/繁/英)差异(pyweixin `Uielements.py` 三语字典),OCR 关键字("发送(S)"、"搜索"、"粘贴"、"置顶"等)要按语言准备。
11. **版本漂移**:4.x 小版本间 UI 类名会变(如好友资料面板 4.0.5 `mmui::ContactProfileView` → 4.1.9 `mmui::ProfileView`,pyweixin `Uielements.py:333-337`);wxAUTO 作者原话"如果你们的需求在某个版本稳定使用,尽量也不要升级"(wxAUTO 文档)。**建议锁定微信版本并在启动时 OCR"关于微信"校验版本号。**
12. **无障碍收紧**:4.x 新账号可能连 UIA 树都拿不到(pywechat `Weixin4.0.md`),这同时意味着**官方渠道(视障用户)也依赖 OCR 化的交互**,纯视觉与微信设计方向一致,长期风险低于 UIA 方案。
13. **账号风控**:所有项目共同警告:频繁加好友、群发、骚扰行为会触发限制甚至封号(pywechat README/Weixin4.0.md;wxAUTO 使用政策 https://docs.wxauto.org/)。RPA 频率要拟人化(随机延迟)。
14. **批量限制**:文件单次≤9个;单条消息≤2000字(超限转 txt 文件);pyweixin 源码。
15. **锁定微信功能**:用户开启"锁定微信"后界面被锁屏遮盖,自动化需先检测(掘金 https://juejin.cn/post/7532753909337604096)【锁定界面视觉特征待实机验证】。

---

## ⑤ 参考项目对比表

| 项目 | 方案类型 | 微信 3.9 | 微信 4.x | 对纯视觉方案的可借鉴点 | 链接 |
|---|---|---|---|---|---|
| **cluic/wxauto** | UIAutomation(控件树+模拟点击,无注入) | ✅(锁定 3.9.x,窗口 `WeChatMainWndForPC`) | ❌ | 操作流程蓝本:ChatWith(会话直点→Ctrl+F 搜索→点高亮/第一项)、剪贴板发送、红点像素检测、@浮窗交互 | https://github.com/cluic/wxauto |
| **wxauto4 / wxautox4**(cluic 官方 4.x 版) | UIA+截图混合(免费版 pip `wxauto4`;增强版付费 `wxautox4`) | ❌ | ✅ 免费版支持到 4.1.8.107(文档要求微信 4.1) | 4.x 独立窗口监听架构、虚拟化对策(拉高窗口)、GetHistoryMessage 回调、消息 X/Y 偏移常量 | https://docs.wxauto.org/ ;https://pypi.org/project/wxauto4/ |
| **AngeCoo/wxauto-4.0** | UIA+视觉混合(**开源**,适配 4.0.5) | ❌ | ✅ 4.0.5 | **最有价值的参考**:全部 `mmui::*` 控件类名/AutomationId、剪贴板粘贴搜索、`detect_message_direction` 截图判向算法、`IsRedPixel` 未读检测、发送(S) 按钮点击 | https://github.com/AngeCoo/wxauto-4.0 |
| **Hello-Mr-Crab/pywechat(pyweixin)** | pywinauto + pyautogui(键盘宏流) | ✅ 3.9.12.x(旧模块) | ✅ 4.1.6+(语音功能要求 4.1.9) | **Alt+S 发送、0.8s 搜索等待、9 文件分批、2000 字转 txt、三语控件标题字典、maxPages 滚动监听、"全屏+置顶+page0=稳定"经验** | https://github.com/Hello-Mr-Crab/pywechat |
| **cluic/wxauto-mcp / wxauto-skill** | MCP/REST 封装(底层 wxautox4) | ❌ | ✅(同 wxautox4) | AI Agent 调用范式(与本方案无直接关系) | https://github.com/cluic/wxauto-mcp |
| **lich0821/WeChatFerry** | **DLL 注入 + 内存 hook**(拦截收发函数,NanoRPC 转发) | ✅(锁定 v39.0.14 指定 3.9 安装包) | ❌ 无 4.x 适配 | **反面教材:纯视觉要避开的东西**——注入被腾讯风控重点盯防、需专用旧版安装包、封号风险高 | https://github.com/lich0821/WeChatFerry |
| **weixin-auto(PyPI)** | wxauto 变体 | ✅ 仅 3.9.11.17 | ❌ | 无 | https://pypi.org/project/weixin-auto |
| **NGCBot 等** | 基于 hook 的机器人框架 | ✅(3.9) | ❌ | 同 WeChatFerry(注入类,规避) | (对比性提及,见知乎方案综述) |

> 注入/hook 方案(WeChatFerry 等)与纯视觉的本质差异:hook 能拿到**不可见**的消息数据(不需窗口在前台)、无 OCR 误差,但需要向微信进程写代码,风控/封号风险与合规风险最高;纯视觉一切以"人眼可见"为准,速度慢、有 OCR 误差,但零侵入。wxAUTO 文档对其定位:"只取人眼可见的信息,模拟人工操作"(https://docs.wxauto.org/llms-full.txt)。

---

## 附录:主要来源清单

**文档/官网**
- wxauto 官方文档(v4.0/v3.9 双版本,含 llms-full.txt 全文):https://docs.wxauto.org/
- 微信 Windows 官网(版本下载):https://windows.weixin.qq.com/?lang=zh_CN

**源码仓库(本次实际抓取并阅读关键文件)**
- https://github.com/cluic/wxauto — `wxauto/wxauto.py`(ChatWith/SendMsg/SendFiles/GetNextNewMessage)、`wxauto/elements.py`(@/引用/转发键序)
- https://github.com/AngeCoo/wxauto-4.0 — `wxauto4/ui/{main,sessionbox,chatbox,navigationbox}.py`、`wxauto4/utils/{tools,win32}.py`、`wxauto4/msgs/{msg,mattr}.py`、`wxauto4/param.py`
- https://github.com/Hello-Mr-Crab/pywechat — `Mcp/pyweixin_rpa/pyweixin/{WeChatAuto,Uielements}.py`、`Weixin4.0.md`

**技术文章/帮助页**
- 微信 4.0 新架构(Qt+C++ vs WebView):https://juejin.cn/post/7455984186912129074
- 4.0.3 正式版 UI 变化(圆角/立体感):https://news.mydrivers.com/1/1039/1039280.htm
- 深色模式气泡颜色分析:https://sspai.com/post/59609 ;https://m.zcool.com.cn/article/ZMTExNzc4NA==.html
- 4.0 新界面(列表灰底/选中绿):https://www.163.com/dy/article/KO2GDECM0511RFLI.html
- PC 快捷键(含无障碍键):https://www.nvdacn.com/index.php/archives/947 ;https://www.cnblogs.com/zhangyunxi/p/Shortcut-in-wechat-for-windows.html ;https://www.jianlu365.com/info/2738.html ;https://jingyan.baidu.com/article/c1a3101eba06d39f646deb71.html ;https://juejin.cn/post/7532753909337604096
- 企业微信设置页(发送/截屏/激活搜索可自定义的旁证):https://open.work.weixin.qq.com/help2/pc/13352
- wxauto4 4.x 适配说明(Weixin.exe 进程):https://www.cnblogs.com/travel92/p/19812181

---

## 待实机验证清单(汇总)

1. 4.x 主窗口 `Ctrl+F` 是否默认聚焦搜索框;"激活搜索"默认绑定键
2. `Alt+L/空格/Enter/Alt+M/Alt+S/Alt+C` 无障碍键在 4.x 的保留情况
3. PC 4.x 气泡精确色值(浅色/深色两套)、发送按钮可用/禁用态颜色、置顶会话背景色
4. 免打扰会话未读角标的形状与颜色(数字变圆点?)
5. 双击会话开独立窗口(及右键"在独立窗口打开")在 4.x 的可用性;合并转发记录窗口类名
6. 125%/150% DPI 缩放下 4.x 的坐标换算、圆角/阴影对边缘定位的影响
7. "锁定微信"界面外观与解锁流程
8. 托盘图标闪烁/任务栏角标是否可作为新消息补充信号
9. 4.1.15(最新版)相对 4.0.5/4.1.8 的布局与控件漂移程度
