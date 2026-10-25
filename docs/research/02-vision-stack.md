# 02 视觉技术栈调研:纯视觉 + 仿真人键鼠(微信 Windows 4.x)

> 调研时间:2026-10-23 | 环境:Windows 10 + Python 3.12 + 纯本地 CPU(无 GPU 依赖)
> 目标:截屏 + OCR 中文识别(含坐标框) + 模板匹配定位图标 + 全仿真人键鼠输入
> 方法:全部结论来自本次网络检索(GitHub / PyPI / Microsoft Learn / 社区实测文章),未在本机安装或运行任何依赖;所有 PyPI 版本号均于 2026-10-23 通过 PyPI JSON API 现场核对。查不到、无法远程验证的内容统一标注【待实机验证】或【未找到】。

---

## 0. 结论速览(TL;DR)

| 组件 | 推荐 | 版本 | 一句话理由 |
|---|---|---|---|
| OCR | **RapidOCR(onnxruntime CPU)** | rapidocr 3.10.0 + onnxruntime 1.31.0 | PaddleOCR 同源模型转 ONNX,CPU 快 4~5 倍,原生返回"文字+四点坐标框+置信度",Python 3.12 无缝,2026-10 仍活跃发版 |
| 截屏(主) | **mss** | 10.2.0 | 纯 Python ctypes,1080p 约 16~47ms/张,对 GUI 自动化 1~5 FPS 轮询绰绰有余 |
| 截屏(后台/备用) | PrintWindow+PW_RENDERFULLCONTENT(pywin32 实现) | pywin32 312 | 通用硬件加速窗口可截;**微信 4.x 是否可行【待实机验证】** |
| 模板匹配 | **OpenCV matchTemplate(TM_CCOEFF_NORMED + 多尺度)** | opencv-python 5.0.0.93 | 2~3 行代码、免训练、CPU 快;灰度 + 0.8 阈值 + 0.8x~1.2x 缩放扫描 |
| 鼠标轨迹 | **human-mouse(贝塞尔+样条)** | 0.1.2 | 现成 PyPI 包,MIT,Python≥3.10,底层 pyautogui,可调速可抖动 |
| 键盘/点击基础 | **pyautogui**(0.9.54)+ **pydirectinput-rgx**(2.1.3.1 备用) | — | 普通 Win32 应用够用;rgx 分叉 2026-10 仍在维护、支持 Unicode 按键 |
| 中文输入 | **pyperclip + Ctrl+V 粘贴**(pyautogui 不支持中文) | pyperclip 1.11.0 | pyautogui.write() 只支持 ASCII,剪贴板粘贴是社区标准解 |
| 急停监听 | **pynput** | 1.8.2 | 全局键鼠监听,做"人工热键急停"和占用检测(LGPL-3.0 需注意) |
| 窗口管理 | **pywin32 直调 Win32 API**(pygetwindow 已停更) | 312 | FindWindow/GetWindowRect/SetForegroundWindow;激活绕障用 AttachThreadInput |

**关键背景发现(直接影响技术路线)**:微信 4.0 起 PC 端从 Qt Widgets 迁移到 Qt Quick/QML,UI 由 OpenGL 渲染成"一张画布",UIA 控件树消失,wxauto 等成熟 UIA 方案已于 2025-10 停更;社区共识是 4.0+ 的 Windows 自动化退化为**"截图 + OCR/OpenCV + 坐标点击"**——本项目的纯视觉路线正是当前的主流可行路线(来源见 §8.4)。

---

## 1. OCR 选型对比:RapidOCR vs PaddleOCR vs Windows WinRT OCR

### 1.1 三者本质关系

RapidOCR 不是另一个 OCR,而是 **PaddleOCR 模型(PP-OCR 系列)转成 ONNX 格式后的轻量推理封装**,默认推理引擎为 onnxruntime。因此两者准确率同源接近,差异在推理引擎、依赖体积与部署体验。WinRT OCR 则是 Windows 系统内置的传统 OCR 引擎,零安装但能力弱一档。

### 1.2 对比表(数据来源见 §1.5)

| 维度 | RapidOCR (rapidocr 3.10.0) | PaddleOCR 3.x | WinRT OCR (Windows.Media.Ocr) |
|---|---|---|---|
| 中文准确率 | 与 PaddleOCR 同源模型,基本一致(移动版模型);社区评测约 7.5/10 | PP-OCRv5/v6 精度更强,server 模型最高(约 9.5/10) | 清晰印刷体约 95%;中英混排、小字号、复杂背景明显弱于深度学习方案 |
| CPU 推理速度 | 快:单次约 20~50ms 级,社区实测比 paddle 框架快 4~5 倍;官方示例 18 行文本全流程 elapse≈0.565s(含首载) | 慢:纯 CPU 下明显慢于 onnxruntime | 极快(毫秒级,系统原生) |
| 安装体积 | 轻量:pip 包 27.3MB(纯 py3 wheel),核心模型 <20MB | 重:PaddlePaddle 框架 + PaddleX 依赖链,模型数百 MB | 零安装(系统自带,需中文语言包) |
| Python 3.12 兼容 | ✅ requires_python ">=3.8,<4",onnxruntime 1.31.0 支持 3.11~3.14 | ⚠️ 历史上 PaddleX 依赖链(pandas 1.5.3)与 3.12 冲突,官方曾建议用 3.11;paddlepaddle 框架本身宣称支持 3.9~3.13,新版已修复部分问题,但坑多 | ✅ winrt-Windows.Media.Ocr / winsdk 支持 |
| 返回"文字+坐标框" | ✅ 原生:`boxes`(N,4,2 ndarray,左上/右上/右下/左下)+ `txts`(Tuple[str])+ `scores`(Tuple[float]);`return_word_box=True` 可得**单字级**坐标 | ✅ 同样返回 box+text+score | ✅ OcrResult.Lines + WordBoundingRect(矩形) |
| GPU 需求 | 无(pip 默认即 CPU 版 onnxruntime) | 无(cpu 版 paddlepaddle) | 无 |
| 维护状态 | 活跃:3.10.0 发布于 2026-10-08 | 活跃 | 微软系统组件,稳定;新方向是 Windows App SDK Text Recognition API |
| 许可 | Apache-2.0 | Apache-2.0 | 系统组件 |

### 1.3 推荐与理由

**主选:RapidOCR(onnxruntime CPU)**

- 安装:`pip install rapidocr onnxruntime`
- 理由:
  1. **Python 3.12 零障碍**,而 PaddleOCR 3.x 在 3.12 上有 PaddleX 依赖链前科(GitHub Discussion #15549 / Issue #15307);
  2. **原生输出正是本项目需要的结构**:`result.boxes` 四点坐标框 + `result.txts` 文本 + `result.scores` 置信度,一行过滤 `scores` 即可用于点击定位;文档示例输出(节选):`txts=('正品促销','大桶装更划算',...)`、首框坐标 `[[6,2],[322,9],[320,104],[4,97]]`;
  3. **CPU 上快**,满足"截屏→OCR→决策"秒级循环;
  4. **默认模型已跟进 PP-OCRv6**(≥3.9.0 默认 Det PP-OCRv6 small + Cls PP-OCRv4 mobile + Rec PP-OCRv6 small),中文英文开箱即用;
  5. Umi-OCR、Docling 等知名项目均采用,社区成熟。

**备选:WinRT OCR(零依赖兜底)** —— 客户机器不便装大依赖、或 RapidOCR 首载慢时的快速通道;需确认系统已装中文 OCR 语言包(`OcrEngine.AvailableRecognizerLanguages`)。Python 绑定:`pip install winrt-Windows.Media.Ocr`(新版并入 winsdk 体系)。

**不推荐作为主选:PaddleOCR** —— 精度上限虽更高(server 模型),但依赖重、CPU 慢、Py3.12 兼容历史问题多;若未来 RapidOCR 精度不够,再单独建 Python 3.11 环境引入 PP-OCRv5 server 模型。

### 1.4 使用要点(来自官方文档)

- `RapidOCR()` 惰性加载模型(≥3.10.0),首次调用某阶段才载入——**进程内只初始化一次,不要每次识别都 new**;
- 过滤噪声:`score < 0.7~0.8` 的行丢弃(示例输出中低分行如 0.46/0.49 多为误识);
- 坐标系 = 输入图像的像素坐标,与截屏图一致,可直接映射屏幕坐标;
- `return_word_box=True` 得到单字坐标,适合"点某个字/某个联系人"的精细定位;
- 【待实机验证】微信 UI 小字号(12~13px 逻辑像素)在实际 DPI 下的识别率,建议截真实微信窗口实测。

### 1.5 参考来源

- RapidOCR 官方仓库:https://github.com/RapidAI/RapidOCR
- RapidOCR 使用文档(输出格式/boxes/txts/scores/word_results):https://rapidai.github.io/RapidOCRDocs/latest/install_usage/rapidocr/usage/
- PyPI rapidocr(3.10.0,2026-10-08):https://pypi.org/project/rapidocr/
- PyPI onnxruntime(1.31.0,CPU 版即默认):https://pypi.org/project/onnxruntime/
- RapidOCR vs PaddleOCR 速度社区讨论:https://github.com/RapidAI/RapidOCR/discussions/641
- 知乎《我装了四款本地OCR,最后留下了这一款》:https://zhuanlan.zhihu.com/p/2023055658082117490
- 腾讯云《PaddleOCR 太慢?换 RapidOCR 后速度起飞》:https://developer.cloud.tencent.com/article/2703010
- PaddleOCR Py3.12 依赖问题:https://github.com/PaddlePaddle/PaddleOCR/discussions/15549 、 https://github.com/PaddlePaddle/PaddleOCR/issues/15307
- PaddlePaddle 官方安装文档(支持 3.9~3.13):https://www.paddlepaddle.org.cn/documentation/docs/zh/install/index_cn.html
- winrt-Windows.Media.Ocr(PyPI):https://pypi.org/project/winrt-Windows.Media.Ocr
- WinRT OcrEngine Python 调用示例(Gist):https://gist.github.com/dantmnf/23f060278585d6243ffd9b0c538beab2
- OcrEngine 官方 API:https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine
- Windows App SDK Text Recognition(新 API 方向):https://learn.microsoft.com/en-us/windows/ai/apis/text-recognition
- Text-Grab 评测(印刷体约 95%):https://blog.csdn.net/gitblog_00936/article/details/160625369

---

## 2. 截屏方案对比:mss / PIL ImageGrab / dxcam / PrintWindow

### 2.1 性能与特性对比(数据来源见 §2.5;均未在本机实测,数字引自各来源)

| 方案 | 最新版本/状态 | 实测速度(来源口径) | 后台/被遮挡窗口 | 适用性评价 |
|---|---|---|---|---|
| **mss** | 10.2.0(2026-04-23,维护中) | 约 16~47ms/张(30~60 FPS) | ❌ 只截屏幕可见内容 | **主推荐**:纯 Python ctypes、跨平台、按显示器截图、API 干净 |
| dxcam(Desktop Duplication) | 0.3.0 稳定(2026-03-12)+ 0.4.0.dev2(2026-09-17),**2026 年已复活维护**,支持 Py3.10~3.14 | 官方 1080p 约 240FPS(画面不变时有优化) | ❌ 截桌面合成内容 | 高频视觉循环用;本项目 1~5 FPS 用 mss 足够,不必引入 |
| PIL ImageGrab | 随 Pillow 13.0.0 | 慢于 mss(社区基准) | ❌(Win10 上有 `all_screens=True` 多屏支持) | 够用但最慢;胜在 Pillow 已是普遍依赖,零成本兜底 |
| PrintWindow + **PW_RENDERFULLCONTENT(=2)** | Win32 API(pywin32 312 调用) | 单窗口一次调用,毫秒级 | ⚠️ **可截被遮挡窗口**(非最小化);DirectX/硬件加速窗口加 flag 2 才不发黑 | 后台观察用;**微信 4.x 实际效果【待实机验证】**(见 §2.3) |
| Windows.Graphics.Capture(WinRT) | 系统组件(Win10 1903+) | 极低延迟(Quicker 报告) | ✅ 明确可截被遮挡窗口 | 兼容性最好的后台方案,但 WinRT 互操作复杂,列为后备 |

### 2.2 推荐与理由

**主选:mss**(`pip install mss`)

- 理由:GUI 自动化的感知循环只需 1~5 FPS,mss 的 30~60FPS 余量巨大;纯 Python 无编译依赖;`mss.mons()` 支持多屏,`with mss.mss() as sct: sct.grab(monitor)` 返回 BGRA,可直接喂 numpy/OpenCV/RapidOCR;
- **与"仿真人输入"的设计一致性**:本项目要求键鼠仿真,输入必然发到**前台焦点窗口**——微信本来就必须保持前台可见,因此"后台截图"不是硬需求,mss 截屏 + `GetWindowRect` 裁剪窗口区域即可覆盖主流程。

**PrintWindow(PW_RENDERFULLCONTENT)作为增强选项**:用于"微信窗口被其他窗口遮挡时仍能观察界面"的辅助场景(如用户临时切走窗口时暂停前确认状态)。pywin32 实现要点:窗口**不能最小化**;flag=2 解决 Chrome/微信等硬件加速窗口黑屏。

### 2.3 PrintWindow 能否截到微信 4.x(Weixin.exe)?——【待实机验证】

检索到的证据状态:

1. 通用结论(已证实):普通 GDI 窗口 PrintWindow 直截;Chrome/微信 3.x 等 DirectX 硬件加速窗口需 `PW_RENDERFULLCONTENT`(值 2,Win8.1/10 1703+)才不黑屏(博客园/CSDN/52pojie 多个案例)。
2. 微信 4.x 专项(两篇 2025~2026 报告**均未确认** PrintWindow 路线):
   - Quicker《微信4.0+ RPA 自动化技术深度研究报告》:首选**前台 BitBlt + ROI 条带**,未提及 PrintWindow;明确 Windows.Graphics.Capture 可截被遮挡窗口;
   - yage.ai《微信自动化跨平台可行性调研》(2026-03):4.0 迁移 Qt Quick/QML、OpenGL 渲染单画布;GDI 路线"只能获取屏幕上可见内容";**该文明确表示未找到 4.0+ 后台截图可行证据**。

**结论:PrintWindow 对微信 4.x(Weixin.exe,Qt Quick/OpenGL 合成)的有效性没有任何已检索到的直接证据,标注【待实机验证】。** 验证代码(实机第一件事跑这个):

```python
# test_printwindow_wechat4.py —— 微信4.x 后台截图验证
import win32gui, win32ui
from ctypes import windll
from PIL import Image
import numpy as np

hwnd = win32gui.FindWindow(None, "微信")   # 类名/标题以 Spy++ 实测为准
win32gui.ShowWindow(hwnd, 4)               # SW_SHOWNOACTIVATE,不可最小化
left, top, right, bottom = win32gui.GetWindowRect(hwnd)
w, h = right - left, bottom - top

hdc = win32gui.GetWindowDC(hwnd)
dc = win32ui.CreateDCFromHandle(hdc)
mem = dc.CreateCompatibleDC()
bmp = win32ui.CreateBitmap()
bmp.CreateCompatibleBitmap(dc, w, h)
mem.SelectObject(bmp)
ok = windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), 2)   # 2 = PW_RENDERFULLCONTENT
arr = np.frombuffer(bmp.GetBitmapBits(True), dtype=np.uint8).reshape(h, w, 4)
Image.fromarray(arr[..., :3][..., ::-1]).save("pw_wechat4.png")  # BGR->RGB
win32gui.DeleteObject(bmp.GetHandle()); mem.DeleteDC(); dc.DeleteDC(); win32gui.ReleaseDC(hwnd, hdc)
print("PrintWindow ret =", ok)   # 0=失败;再看图片是否黑屏/缺内容
```

判定:图像完整不黑 → 可用作后台观察;黑屏或空白 → 放弃后台路线,坚持"前台 mss"(这也与仿真人操作的前台约束自洽)。若必须后台,后备方案是 Windows.Graphics.Capture(Win10 1903+)。

### 2.4 全局注意:DPI 感知(截屏/点击坐标一致的根)

进程必须显式声明 DPI 感知,否则高 DPI 屏上系统会对坐标做虚拟化缩放,导致"截到的图"与"点击坐标"错位。推荐在程序最开头:

```python
import ctypes
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
# 旧系统回退:ctypes.windll.shcore.SetProcessDpiAwareness(2) 或 ctypes.windll.user32.SetProcessDPIAware()
```

官方建议用 manifest `<dpiAwareness>PerMonitorV2</dpiAwareness>`(程序化设置须在任何窗口创建前调用)。Quicker 报告同样强调点击坐标需 `GetDpiForWindow` 换算。

### 2.5 参考来源

- mss(PyPI,10.2.0):https://pypi.org/project/mss/ ;仓库 https://github.com/BoboTiG/python-mss
- mss 速度实测:https://stackoverflow.com/questions/54540307/fastest-screenshot-library-python-improve-performance-of-mss-package
- dxcam 官方基准(240FPS):https://github.com/ra1nty/DXcam ;PyPI 现状(0.3.0/0.4.0.dev2):https://pypi.org/project/dxcam/
- 三方案对比:《Python - Fast Screen Capture》https://kylefu.me/2023/02/18/python-fast-screen-capture.html
- PrintWindow 后台截图示例(含 flag=2):https://www.cnblogs.com/xiaoerjun/p/16737723.html 、 https://blog.wshon.com/2019/11/python-background-screenshot.html
- 硬件加速窗口黑屏讨论:https://blog.csdn.net/weixin_39683241/article/details/116190751 、 https://www.52pojie.cn/thread-1935641-1-1.html
- Quicker《微信4.0+ RPA 深度研究》(BitBlt 前台 ROI / WGC 后台 / 性能数据):https://getquicker.net/KC/Kb/Article/1171
- yage.ai 微信可行性调研(2026-03,后台截图未证实):https://yage.ai/share/wechat-uia-platform-feasibility-survey-20260327.html
- DPI 感知官方文档:https://learn.microsoft.com/en-us/windows/win32/hidpi/setting-the-default-dpi-awareness-for-a-process
- pywin32(PyPI,312):https://pypi.org/project/pywin32/

---

## 3. 模板匹配:OpenCV matchTemplate 多尺度定位图标/按钮

推荐库:**opencv-python 5.0.0.93**(PyPI 现版,Python≥3.6,numpy≥2),`pip install opencv-python`。

### 3.1 基本方法

- 算法:`cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)` → 结果矩阵 `minMaxLoc` 取最大值,`max_val` 即匹配分;
- 阈值经验值:**0.8 起步**(0.75~0.9 区间调),UI 图标静态无光照变化,0.8~0.9 是社区常用命中区;`max_val < 阈值` 判定"未出现";
- 多目标:用 `np.where(res >= threshold)` 取所有超阈值点,再做 NMS(按距离去重)。

### 3.2 灰度 vs 彩色

- **首选灰度**(`cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)`):对微信深色/浅色主题切换、窗口背景变化更鲁棒,且计算量减半(掘金多分辨率指南、OpenCV 中文教程综合建议);
- **彩色补充**:仅当"形状相同、颜色不同"的图标需要区分时(如同位置的红/灰按钮),在灰度命中后加 HSV/RGB 复核;
- **颜色类目标直接走 HSV**:微信未读红点等,Quicker 报告给出的成熟配方:BGR→HSV → 双区间 `InRange`(红 H:0–10 与 170–180,OpenCV 的 H 上限 180)∪,S>120,V 70–255 → 形态学闭运算填充红点数字 → `findContours` → 按外接矩形面积/宽高比(约 10×10~30×30 px)过滤。免打扰灰点、节日皮肤等鲁棒性补救才轮到多尺度模板匹配。

### 3.3 多尺度与 DPI 归一化(实践配方)

1. **根因**:模板截取时的 DPI/缩放与运行时不一致是匹配失败首因。最稳做法是**同机同 DPI 采集模板**;
2. **运行时多尺度扫描**:缩放模板按 0.8~1.2、步长 0.05 循环(先 0.1 粗扫锁定尺度,再 0.02 细扫),取全局最优 `(scale, max_val, max_loc)`(掘金《OpenCV 多分辨率模板匹配与容错实战指南》);
3. **DPI 换算**:读 `GetDpiForWindow(hwnd)`(Quicker 报告),按 `模板DPI / 当前DPI` 预缩放模板再匹配,可把多尺度搜索范围压窄;
4. ROI 优先:先 `GetWindowRect` 裁微信窗口区域再匹配,比全屏匹配快一个量级(Quicker 报告同样强调局部 ROI);
5. 封装"等待出现/消失"原语:`find(tpl, timeout, poll_range)` 内部随机轮询间隔,超时返回 None——这是"感知→定位→行动→验证"循环的基础件。

### 3.4 局限与对策

- 微信小版本更新通常不敏感,大改版需重制模板(yage.ai 调研);模板库按"名称→图像→预期尺寸"集中管理,提供一键自检脚本;
- 旋转/大角度缩放不适用 matchTemplate(本项目场景不会出现);
- 【待实机验证】本机 DPI(100%/125%/150%)下微信图标的实际像素尺寸,建议首日采集一套模板基线。

### 3.5 参考来源

- OpenCV 官方教程 Template Matching(4.13):https://docs.opencv.org/4.13.0/d4/dc6/tutorial_py_template_matching.html
- OpenCV-Python 中文教程《模板匹配》:https://opencv-python-tutorials.readthedocs.io/zh/latest/4.%20OpenCV%E4%B8%AD%E7%9A%84%E5%9B%BE%E5%83%8F%E5%A4%84%E7%90%86/4.12.%20%E6%A8%A1%E6%9D%BF%E5%8C%B9%E9%85%8D
- 《OpenCV 多分辨率模板匹配与容错实战指南》(掘金,2025):https://juejin.cn/post/7481909735870595135
- 《cv2.matchTemplate() 详解》(博客园):https://www.cnblogs.com/IllidanStormrage/articles/16288609.html
- 多目标阈值法:https://codec.wang/docs/opencv/basic/template-matching
- opencv-python(PyPI,5.0.0.93):https://pypi.org/project/opencv-python/
- HSV 红点检测配方:https://getquicker.net/KC/Kb/Article/1171

---

## 4. 仿真人输入:鼠标贝塞尔轨迹 + 随机延迟 + 打字节奏

### 4.1 输入库对比:pyautogui vs pydirectinput vs pynput

| 维度 | pyautogui(0.9.54) | pydirectinput(1.0.4)/ **pydirectinput-rgx(2.1.3.1)** | pynput(1.8.2) |
|---|---|---|---|
| 底层机制 | keybd_event/SendInput 虚拟键码 | SendInput + **DirectInput 扫描码**(rgx 分叉还加了 Unicode 输入函数) | Windows 消息钩子;控制与**监听**双能力 |
| 普通 Win32 应用(微信属此类) | ✅ 完全够用 | ✅(对普通应用无额外收益) | ✅ |
| DirectX 游戏 | ❌ 常被忽略 | ✅ | ❌ |
| 中文/非 ASCII 输入 | ❌ write() 仅 ASCII(→ pyperclip+Ctrl+V) | rgx 支持扫描码/Unicode 函数;原版不支持 | ✅ `Controller.type()` 支持 Unicode |
| 维护状态 | 2023-05 后未更新(仍广泛可用) | 原版 2021 停更;**rgx 分叉活跃,最新 2026-10-11** | 活跃(2026-05-12);LGPL-3.0 |
| 特有能力 | FAILSAFE 安全绳、PAUSE 全局节奏 | 游戏兼容 | 全局监听(急停/占用检测) |

**推荐**:以 **pyautogui 为基础层**(API 全、社区大、FAILSAFE 保险),**pydirectinput-rgx 作为备用**(遇到微信 4.x 吃键或需 Unicode 直输时切换),**pynput 只做监听**(人工热键急停、检测用户是否正在手动操作鼠标以主动避让)。Google project-gameface 的实践也是"PyAutoGUI 管鼠标 + DirectInput 管键盘"的组合模式。

- 安装:`pip install pyautogui pydirectinput-rgx pynput pyperclip`
- 微信是 Qt Quick 渲染的普通 Win32 窗口,虚拟键码点击/滚轮/快捷键正常可达;若实测个别快捷键被吞,再换 rgx 的扫描码函数(此为【待实机验证】项)。

### 4.2 贝塞尔曲线鼠标轨迹(现成库)

**主选:human-mouse 0.1.2**(2025-09-13,MIT,Python≥3.10,108★)

- 原理:贝塞尔曲线 + 样条插值生成平滑轨迹,带随机扰动与自然加减速;
- 依赖 numpy / pyautogui / scipy(与本项目主栈重合);
- API(`MouseController`):`move(x, y, speed_factor=…)`、`move_random()`、`perform_click(x, y)`、`perform_double_click`、`perform_context_click`,构造参数 `always_zigzag`;
- 安装:`pip install human-mouse`
- 仓库:https://github.com/sarperavci/human_mouse

**备选**:
- **pyclick**(patrikoss/pyclick):可定制曲线参数的生成器,适合自己控速分段投递;https://github.com/patrikoss/pyclick
- **bezmouse**(vincentbavitz/bezmouse):极简贝塞尔实现,百行级,适合"抄进自研模块"而非引依赖;https://github.com/vincentbavitz/bezmouse
- DIY 参考:SO《PyAutoGUI mouse movement with bezier curve》给出可调"抖动度"的 pyautogui+贝塞尔实现;https://stackoverflow.com/questions/44467329/pyautogui-mouse-movement-with-bezier-curve

**工程建议**:human-mouse 快速起步;若后续要更精细的"过冲(overshoot)+ 二段修正"行为,参考 GitHub topic `mouse-movements` 里 Playwright 生态的 overshoot 实现,把轨迹点列喂给 `pyautogui.moveTo(duration=0, _pause=False)` 逐点投递(比 mouseTo 内插更可控)。

### 4.3 打字节奏模拟

- **中文消息(主场景)= 剪贴板粘贴**,不是逐键:pyautogui.write() 不支持中文(官方仅 ASCII 键封装),社区标准解为 `pyperclip.copy(text)` + `pyautogui.hotkey('ctrl','v')`(CSDN/SegmentFault 多篇)。仿真节奏体现在:粘贴前光标定位的鼠标轨迹耗时、粘贴前后 0.2~1s 随机小动作、偶发"先删几个字再补"等伪动作;
- **英文/数字/验证码等逐键输入**:逐字符 `pyautogui.write(ch)` + 每键 `time.sleep(random.uniform(0.05, 0.18))`,空格后偶发 0.3s 思考停顿;现成实现:
  - **human-keyboard**(PyPI):人类打字节奏+错误注入;https://pypi.org/project/human-keyboard
  - **typesim**(pc-style/typesim,GitHub):打字错字、回改、思考停顿建模;https://github.com/pc-style/typesim
- 长文本别用一次 `write(long_text)`(SO 案例:长文易丢键),按 5~15 字符分片投递;https://stackoverflow.com/questions/74322676/how-to-simulate-human-keyboard-input-of-a-long-text-using-python

### 4.4 pyautogui 的 PAUSE 与 FAILSAFE 最佳实践(官方文档口径)

- `pyautogui.PAUSE`:每次 pyautogui 调用后强制延迟,**默认 0.1s**;本项目的节奏由自己的随机延迟函数统一控制,建议 **`pyautogui.PAUSE = 0`**(否则两层延迟叠加,动作显得机械呆板),随机性全部自己实现;
- `FAILSAFE`:**默认开启**,鼠标被移到主显示器四角任一角即抛 `pyautogui.FailSafeException`——这是脚本失控时夺回控制权的保险;官方原话 "I HIGHLY RECOMMEND YOU DO NOT DISABLE THE FAILSAFE";
- **本项目守则:绝不关 FAILSAFE**;同时把贝塞尔轨迹终点做 ±3px 抖动,避免轨迹精确擦过角落误触急停;human-mouse 的轨迹应检查路径不穿越四角(可在投递层夹紧坐标)。

### 4.5 参考来源

- PyAutoGUI 官方文档(PAUSE/FAILSAFE):https://pyautogui.readthedocs.io/en/latest/
- PyPI pyautogui(0.9.54):https://pypi.org/project/pyautogui/
- PyDirectInput(PyPI):https://pypi.org/project/PyDirectInput/
- **pydirectinput-rgx(2.1.3.1,2026-10-11)**:https://pypi.org/project/pydirectinput-rgx/
- pynput(PyPI,1.8.2):https://pypi.org/project/pynput/
- human-mouse:https://github.com/sarperavci/human_mouse ;PyPI https://pypi.org/project/human-mouse/
- pyclick:https://github.com/patrikoss/pyclick ;bezmouse:https://github.com/vincentbavitz/bezmouse
- 三库选型讨论:https://stackoverflow.com/questions/70161288/ 、 https://www.reddit.com/r/learnpython/comments/1j49bkv/
- 组合模式实践(Google project-gameface #9):https://github.com/google/project-gameface/issues/9
- pyautogui 中文输入解决:https://blog.csdn.net/lilongsy/article/details/130409505 、 https://segmentfault.com/a/1190000040362403
- human-keyboard:https://pypi.org/project/human-keyboard ;typesim:https://github.com/pc-style/typesim
- mouse-movements 主题页(overshoot 等):https://github.com/topics/mouse-movements

---

## 5. 窗口管理:查找微信窗口、取位置尺寸、置前激活

### 5.1 选型

| 库 | 版本/状态 | 评价 |
|---|---|---|
| **pywin32(直调 user32)** | **312**,requires ≥3.9,维护中 | **主推荐**:FindWindow/EnumWindows/GetWindowRect/SetForegroundWindow/SendMessage 全覆盖,无中间层 |
| pywinauto | 稳定 0.6.9(2025-01-06);1.0.0b1(2026-10-15,β) | 可用其 `Desktop(backend="win32")` 做窗口定位与 set_focus(内部处理了前台切换);**但勿依赖其控件树**——微信 4.x UIA 树为空壳(见 §8.4) |
| pygetwindow | 0.0.9(**2020-10-04 起停更**) | API 简单但年久失修,不建议新项目依赖 |

- 安装:`pip install pywin32`
- 微信 4.x 顶层窗口的**类名/标题**(如旧版 "WeChat"/"微信")【待实机验证】:用 Spy++ 或 `EnumWindows` 遍历 PID(Weixin.exe)列顶层窗口确认,不要沿用 3.x 记忆值。

### 5.2 SetForegroundWindow 的限制与绕法(均有公开来源)

**限制(官方条件)**:调用方进程必须满足"前台进程/被前台进程允许/最近接收过输入"等条件之一,否则调用失败、目标窗口只在任务栏闪烁。MS Learn `SetForegroundWindow` 文档列全了这些条件,并提到前台进程可用 `LockSetForegroundWindow` 主动禁用外来切换。

**常用绕法(按推荐顺序)**:
1. **AttachThreadInput**:把当前线程附着到前台窗口线程(共享输入状态)后再调 `SetForegroundWindow`,经典且最稳;现成 Python ctypes 实现:https://gist.github.com/EBNull/1419093 ;
2. **SPI_SETFOREGROUNDLOCKTIMEOUT=0**:`SystemParametersInfo` 把前台锁定超时清零,恢复旧行为(Bob Moore Win32 技巧);对系统全局有影响,谨慎;
3. **ALT 键技巧**:先 `keybd_event(VK_MENU,...)` 发一个 ALT 按下/抬起,再 SetForegroundWindow;
4. **最小化→恢复**(ShowWindow SW_MINIMIZE→SW_RESTORE)或先向自身窗口模拟一次输入再切换;
5. 或直接用 pywinauto 的 `set_focus()`(封装了类似逻辑)。

本项目为"仿真人"场景,输入前窗口必须前台,置前失败时应**重试+随机退避**而不是硬绕(绕法本身在风控视角也更显眼)。

### 5.3 参考来源

- SetForegroundWindow 官方文档:https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow
- AttachThreadInput Python 实现(Gist):https://gist.github.com/EBNull/1419093
- 经典讨论:https://stackoverflow.com/questions/19136365/win32-setforegroundwindow-not-working-all-the-time 、 https://shlomio.wordpress.com/2012/09/04/solved-setforegroundwindow-win32-api-not-always-works
- SPI_SETFOREGROUNDLOCKTIMEOUT:http://bobmoore.mvps.org/Win32/w32tip33.htm
- pywin32(PyPI,312):https://pypi.org/project/pywin32/
- pywinauto(PyPI,0.6.9/1.0.0b1):https://pypi.org/project/pywinauto/
- pygetwindow(PyPI,0.0.9):https://pypi.org/project/pygetwindow/

---

## 6. GitHub 纯视觉 GUI 智能体参考项目与主循环设计

### 6.1 参考项目一览

| 项目 | 定位 | 对本项目的可借鉴点 | 能否直接用 |
|---|---|---|---|
| **OpenAdapt**(OpenAdaptAI/OpenAdapt) | 桌面 GUI 任务"演示→编译→本地确定性回放",健康运行零模型调用,逐步校验、不匹配即停 | **"行动后校验"机制**:每步操作后截图比对状态,失败进入修复/重试分支;录制器架构 | 设计参考(其 capture 支持 Py3.10~3.12,但整体是重型框架) |
| **OmniParser V2**(microsoft/OmniParser) | 截图→YOLO 检测交互元素+Florence-2 描述→Set-of-Marks(bbox+编号)喂给 LLM | 感知层结构化:界面元素统一为"编号+外接框+语义"的注册表,便于日志与重试 | ❌ 不直接用:依赖 YOLO/VLM,Quicker 实测延迟>500ms 且需 Docker/GPU,违背 CPU 轻量约束 |
| **UI-TARS / UI-TARS-desktop**(bytedance) | 多模态 GUI Agent 模型+桌面端 | 感知→推理→行动闭环与截图历史管理 | ❌ 同上,模型推理重 |

### 6.2 本项目的主循环设计(综合借鉴)

```
┌──────────── 感知 Perception ────────────┐
│ mss 截屏(窗口 ROI)→ 可选 HSV 红点检测   │
│            + RapidOCR(boxes/txts/scores)│
└──────────────┬──────────────────────────┘
               ▼
┌──────────── 定位 Localize ──────────────┐
│ OCR 坐标框命中(文字线索)∪               │
│ matchTemplate 多尺度命中(图标按钮)      │
│ → 输出目标点(外接框中心±抖动)          │
└──────────────┬──────────────────────────┘
               ▼
┌──────────── 行动 Act ───────────────────┐
│ human-mouse 贝塞尔移动(随机速度/过冲)   │
│ → pyautogui 点击(±px 抖动+随机延迟)     │
│ → 中文:pyperclip+Ctrl+V(前后随机小停顿)│
└──────────────┬──────────────────────────┘
               ▼
┌──────────── 验证 Verify(OpenAdapt 思想)┐
│ 随机 0.3~1.5s 后再截图:                 │
│ · 目标元素消失/状态变化?OCR 复读关键区域 │
│ · 失败→退避重试(N 次,随机间隔)→ 人工兜底│
│ 全程:FAILSAFE 开启 + pynput 急停热键    │
└─────────────────────────────────────────┘
```

设计原则(来源依据):OpenAdapt 的"每步校验,不匹配即停";OmniParser 的"元素结构化注册表";Quicker 报告的"高频动作用 OpenCV(<50ms),AI 识图只做复杂逻辑兜底(>500ms)"。

### 6.3 参考来源

- OpenAdapt:https://github.com/OpenAdaptAI/OpenAdapt ;capture(PyPI,Py3.10–3.12):https://pypi.org/project/openadapt-capture
- OmniParser:https://github.com/microsoft/OmniParser ;论文 https://arxiv.org/html/2408.00203v1 ;V2 介绍 https://www.microsoft.com/en-us/research/articles/omniparser-v2-turning-any-llm-into-a-computer-use-agent
- UI-TARS-desktop:https://github.com/bytedance/ui-tars-desktop
- Learn OpenCV OmniParser 教程:https://learnopencv.com/omniparser-vision-based-gui-agent

### 6.4 微信 4.x 专项情报(路线正确性佐证)

- 微信 4.0.5 起 Qt Widgets→Qt Quick/QML,OpenGL 单画布渲染,UIA 只见空壳;wxauto(6832★)2025-10 停更,wxauto4 也停更(yage.ai 调研):https://yage.ai/share/wechat-uia-platform-feasibility-survey-20260327.html
- 《微信4.1 为什么"看不到"UI 树》:https://zhuanlan.zhihu.com/p/1976641454723064191
- Quicker《微信4.0+ RPA 深度研究》(GDI+HSV+SendInput 全流程,OpenCV 路径<50ms):https://getquicker.net/KC/Kb/Article/1171
- 风险意识(yage.ai 风险分级):UI 自动化/OCR 属"低/极低"检测面,Hook 注入与协议模拟风险高一档——纯视觉+仿真输入是各路线中对客户端侵入最小的;但仍应控制频率、保留人工节奏。

---

## 7. 最终推荐技术栈清单(requirements.txt 雏形)

```text
# ==== 感知:截屏 ====
mss==10.2.0                    # 主截屏方案(30~60FPS,纯 ctypes)
Pillow==13.0.0                 # ImageGrab 兜底 + 图像编解码(requires Py>=3.11)

# ==== 感知:OCR(RapidOCR + onnxruntime CPU)====
rapidocr==3.10.0               # PP-OCR 系 ONNX 封装;boxes/txts/scores 原生输出
onnxruntime==1.31.0            # CPU 版即默认 pip 包(requires Py>=3.11)

# ==== 定位:模板匹配 ====
opencv-python==5.0.0.93        # matchTemplate/HSV/findContours
numpy>=2.0                     # opencv-python 5.x 在 Py>=3.9 要求 numpy>=2

# ==== 行动:仿真人输入 ====
pyautogui==0.9.54              # 基础键鼠(虚拟键码);PAUSE=0 + FAILSAFE 保持开启
pydirectinput-rgx==2.1.3.1     # 备用:SendInput 扫描码/Unicode 按键(2026-10 仍维护)
pynput==1.8.2                  # 仅监听:人工急停热键 / 检测用户手动操作(LGPL-3.0 注意合规)
human-mouse==0.1.2             # 贝塞尔+样条鼠标轨迹(依赖 pyautogui/numpy/scipy)
pyperclip==1.11.0              # 中文消息:写剪贴板 + Ctrl+V 粘贴
scipy>=1.11                    # human-mouse 依赖(样条插值)

# ==== 窗口管理 ====
pywin32==312                   # FindWindow/GetWindowRect/SetForegroundWindow/PrintWindow
pywinauto==0.6.9               # 可选:set_focus 封装(勿依赖其 UIA 控件树,微信4.x为空壳)

# ==== 可选降级方案(按需)====
# winrt-Windows.Media.Ocr       # WinRT 系统 OCR 兜底(需系统中文语言包)
# dxcam                         # 高频截屏(0.3.0/0.4.0.dev2)本项目暂不需要
```

版本核对说明:以上版本号均为 2026-10-23 通过 PyPI JSON API 现场核对所得(rapidocr 3.10.0 / onnxruntime 1.31.0 / mss 10.2.0 / opencv-python 5.0.0.93 / pyautogui 0.9.54 / pydirectinput-rgx 2.1.3.1 / pynput 1.8.2 / human-mouse 0.1.2 / pyperclip 1.11.0 / pywin32 312 / pywinauto 0.6.9 / Pillow 13.0.0)。Pillow 13.x 要求 Python≥3.11,满足本项目 3.12;若需兼容 3.10 旧环境,Pillow 回退 11.x【具体版本未核对,标注】。

**明确排除**:
- PaddleOCR——Py3.12 依赖链前科 + CPU 慢 + 体积大,精度需要时再以独立 3.11 环境引入;
- Tesseract——中文识别率低(Quicker 报告不推荐);
- OmniParser / UI-TARS——需 GPU/Docker,违背纯本地 CPU 约束,只借鉴设计;
- pygetwindow——2020 年起停更,能力被 pywin32 直调完全覆盖。

---

## 8. 待实机验证清单(本调研无法远程确认的事项)

1. **PrintWindow+PW_RENDERFULLCONTENT 对微信 4.x(Qt Quick)是否出图**(§2.3 验证代码):有效→可做被遮挡观察;黑屏→坚持前台 mss。
2. **微信 4.x 顶层窗口类名与标题**(Spy++/EnumWindows 确认,勿沿用 3.x 记忆值),以及 `FindWindow(None, "微信")` 是否命中。
3. **本机 DPI(100/125/150%)下微信图标与文字的实际像素尺寸**:确定模板采集基线与多尺度范围。
4. **RapidOCR 对微信 12~13px 小字号、深色主题的实测识别率与耗时**(首次运行含模型下载,注意离线部署时预下载模型)。
5. **pyautogui 虚拟键码在微信 4.x 的可达性**:快捷键(Ctrl+F 搜索、Ctrl+V、Esc)是否被吞;被吞则切 pydirectinput-rgx 扫描码。
6. **SetForegroundWindow 在锁屏/管理员权限差异下**(微信若以管理员运行,普通权限进程无法向其发输入/UIA——两边提权级别需一致)。
7. WinRT OCR 中文语言包在目标机器是否已安装(`AvailableRecognizerLanguages` 含 zh)。

---

## 9. 诚实性声明

- 本文所有性能数字(FPS、ms、准确率)均**引自所列来源**,未在本机复测(按任务约束不安装依赖);实机数字以 §8 清单验证为准。
- 【待实机验证】/【未找到】标注处不存在任何推测填充;其中最重要的未决问题是 §8.1(PrintWindow × 微信 4.x)。
- 微信版本迭代可能使本文"4.x UIA 失效"等结论在数月内变化(yage.ai 调研原话),落地前应以当时实机为准。
