# Muse 信箱协议 v1 —— 微信助手(WeChatVision) ↔ Muse.ai 双向通信规范

> WeChatVision 与 Muse.ai 之间的双向通信契约。实现必须逐条遵循;修改需与两端实现方同步。
> v1.1 修订(2026-10-24,响应 Muse 反馈):新增 §2.1 可选唤醒通知;旧 webhook 报文自 v1.1 起携带 msg_id/chat_id;明确 WebSocket 仅为可选观察通道。协议头版本仍为 1。
> 设计原理:Muse **不是常驻在线可推送**的程序——它平时休眠,被定时任务或用户对话唤醒,且其机器无公网 IP 不能被直连。因此一切走"信箱"拉模式:电脑端微信助手是唯一常驻服务端,消息入信箱;Muse 经隧道定时轮询取信、处理、回信、确认。

## 1. 传输与鉴权

- 通道:Muse 经公网通道访问 `https://your-domain.example`(例如 Cloudflare 隧道地址;或同机直连 `http://127.0.0.1:<web.port>`,默认 8765)
- 鉴权:**双方共用一个 token**(即 WebUI 访问口令 `web.password`)。每个请求带请求头 `X-Access-Token: <token>`(兼容 `?token=` 查询参数);无/错 token → 401
- 协议版本:所有响应带 `X-Mailbox-Protocol: 1`;Muse 发现版本不匹配应停止并提示
- 编码:UTF-8 JSON;时间一律 ISO 8601 **带时区**(如 `2026-10-24T10:30:00+08:00`)

## 2. 电脑端提供的接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/mailbox/status` | 健康检查:当前最大 msg_id、已确认游标、待取条数、各会话投递队列 |
| GET | `/api/mailbox/messages?after={N}&limit={M}` | 拉取 msg_id **大于 N** 的消息,按 msg_id 升序;M≤50 默认 20;响应含 `messages[] / next_cursor / has_more` |
| GET | `/api/mailbox/history?chat_id={X}&limit={M}` | 某会话的历史消息(含已归档),供合并上下文;默认 20 条 |
| POST | `/api/mailbox/replies` | 提交回复,异步投递;响应 `202 {delivery_id}` |
| GET | `/api/mailbox/deliveries/{delivery_id}` | 查询投递状态:`queued → delivering → delivered / failed(附 reason)` |
| POST | `/api/mailbox/ack` | 确认归档:`{cursor}`;响应 `{archived_until}` |
| GET | `/api/mailbox/config` | 只读:监听会话范围、执行白名单、机器人身份(供 Muse 读取对齐) |
| GET | `/api/media/{media_id}` | 非文本媒体下载(带 token) |

错误约定:4xx 带 `{"error": "<中文原因>"}`;5xx 重试安全(见 §8)。

### 2.1 可选:入信唤醒通知(notify)

配置 `mailbox.notify_url` 后:每有新消息入信箱,PC 会**尽力而为**地向该 URL 发一个短超时(3 秒)的 POST,body 即该消息的完整字段(含 msg_id);失败/超时**完全忽略、绝不阻塞入信**。它只是"该来轮询了"的敲门砖,**不是可靠信号——Muse 的定时轮询仍是唯一可靠机制**,未收到 notify 也必须照常轮询。不持长连接或无接收端的场景(如 Muse)可留空不用,零影响。

## 3. 每条消息的字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `msg_id` | int | **全局唯一、单调递增**(允许跳号,PC 持久化,重启不回退)。Muse 只能假设"更大=更新" |
| `chat_id` | string | 会话标识=聊天窗口显示名(私聊=联系人显示名;群聊=群名)。**回复路由键,原样带回** |
| `chat_type` | "private" \| "group" | |
| `sender` | string | 显示名:私聊=联系人名;群聊=成员昵称;`is_from_me=true` 时固定 `"self"` |
| `is_from_me` | bool | **助手(本机微信)自己发出的消息**(手动发送、任何后端的自动回复都会入信箱并置 true)。Muse **一律忽略、不触发处理**,仅可作上下文——防死循环 |
| `msg_type` | "text" \| "image" \| "voice" \| "file" \| "system" | |
| `text` | string | 文本内容;非文本消息为空串;`system` 为系统提示(如撤回/时间戳) |
| `media_url` | string \| null | 非文本消息给**可下载链接**(完整带 token 的 URL),**禁止 base64** |
| `ts` | string | 发送时间,ISO 8601 带时区 |

**纯视觉能力边界(诚实声明)**:`image` = 消息气泡可见区截图裁剪 PNG;`voice`/`file` = 仅事件通知(text 中说明"收到语音/文件"),纯视觉无法提取其内容。

## 4. 编号与游标怎么配合

- PC 端:入信箱时从持久化计数器分配 msg_id;只增不减;事务失败允许跳号
- Muse 端:本地保存游标 cursor(已处理到的 msg_id);每次 `GET /messages?after=cursor` **只取新消息**,绝不重读全部
- Muse 不得假设编号连续;去重唯一依据是 msg_id
- PC 重启后计数器与已确认游标从磁盘恢复,不回退、不重发已确认段

## 5. 回复如何送回正确的会话

Muse 提交回复时**必须原样带回**:
```json
POST /api/mailbox/replies
{
  "chat_id": "会话显示名",          // ← 来自消息的 chat_id,原样带回
  "reply_to_msg_id": 108,           // ← 所回复批次的最后一条 msg_id
  "text": "合并后的连贯回复……",
  "client_ref": "muse-本地去重键-可选"  // 幂等键:相同 client_ref 的重复提交 PC 只投递一次
}
```
- PC 校验:`chat_id` 必须在信箱监听范围内(默认=私聊白名单 ∪ 群白名单),否则 403 拒收——**信箱回复永远不可能发到范围外的会话**
- 投递:PC 按 `chat_id` 分队列**串行投递**(每队列一把锁),复用既有发送链(白名单/标题 OCR 双闸门+拟人节奏)
- 投递重试:最多 3 次(指数退避);最终 `failed` 附原因,Muse 可读状态后重新提交

## 6. 确认(ack)与删除/归档的时机

- **ack 语义**:`POST /ack {cursor}` 表示"≤cursor 的全部消息已处理完,且回复已成功送达(delivered)"
- Muse 侧取值:各会话并行时,ack 值取 **min(各会话已处理的最大 msg_id)**(全局安全下界);全串行实现则就是最后处理完的编号
- **PC 只在收到 ack 后才归档**(标记 archived,不再投递);未 ack 的消息**永久保留**,每次轮询都会重投
- 归档消息默认保留 48 小时(可配 `mailbox.retention_hours`)供审计/历史查询,之后物理清理;`/history` 仍可查到归档段
- ack 幂等;cursor ≤ 已确认值 → no-op 并返回当前值;跳号安全(以编号比较为准)

## 7. 同会话串行、加锁与合并规则

- **Muse 端**(协议要求,由 Muse 自行实现):每个 `chat_id` 一把本地锁;一个会话同一时刻只有一个处理者
  - "批"= 某次轮询取回的该会话全部未处理消息
  - 锁定期内该会话新到的消息**不得另起处理者抢答**;等上一批完成后,下一轮把积压消息作为**同一批**
  - 批处理 = 整批消息 + 该会话独立保存的历史记录 → 理解 → **合并成一封连贯回复**(reply_to_msg_id=批内最大 msg_id)
  - 不同会话互不影响、历史分开保存
- **PC 端**:每会话投递队列串行(天然满足"不抢答");`/status` 暴露各会话队列深度供排查

## 8. 异常、重试与去重

| 场景 | 行为 |
|---|---|
| 消息未 ack | 每次轮询重投(无限期);Muse 按 msg_id 去重,不重复回复 |
| 回复投递失败 | PC 重试 3 次 → `failed`;Muse 查到后可重新 POST(client_ref 相同则 PC 幂等去重) |
| Muse 轮询超时/断线 | 退避重试;轮询无副作用(只读),重试安全 |
| PC 重启 | 计数器/游标/队列从磁盘恢复;未 ack 消息继续可取 |
| 重复 ack | 幂等,no-op |
| 死循环防护 | `is_from_me=true` 一律忽略(仅上下文);PC 侧回复入信箱带该标记 |

## 9. 鉴权与会话/执行范围设置

`GET /api/mailbox/config` 返回(全部在 PC 端 WebUI 配置,Muse 只读对齐):
```json
{
  "protocol": 1,
  "listen_chats": ["…白名单成员…"],     // 信箱监听的会话(默认=私聊白名单 ∪ 群白名单)
  "exec_senders": ["…"],               // **只有这些发送者的消息才可被 Muse 当指令执行**(默认=私聊白名单)
  "bot_identity": "self",              // 助手自己消息的 sender 标识
  "redelivery": true,
  "retention_hours": 48
}
```
- token 鉴权覆盖全部信箱与媒体接口
- Muse 不得对 `exec_senders` 之外的消息执行命令性操作(协议约束+PC 侧投递闸门双保险)

## 10. Muse 端最小实现(轮询伪代码)

```
cursor = 本地持久化游标
每 N 秒:
  r = GET /api/mailbox/messages?after=cursor&limit=20
  若空: continue
  按 chat_id 分组;is_from_me=true 的只记入上下文、跳过处理
  对每个 chat_id(各持本地锁,锁内串行):
      batch = 该会话全部未处理消息
      history = GET /api/mailbox/history?chat_id=…            # 或用本地已存历史
      reply = 理解(batch + history) → 合并为一封连贯回复
      d = POST /api/mailbox/replies {chat_id, reply_to_msg_id: batch末条.msg_id, text: reply, client_ref: …}
      轮询 GET /api/mailbox/deliveries/{d.id} 直到 delivered   # failed→告警/重发
      本会话游标推进到 batch末条.msg_id
  ack = min(各会话已处理最大 msg_id)
  POST /api/mailbox/ack {cursor: ack}
```

## 11. 与双模路由的关系

PC 端智能体路由(见 ARCHITECTURE §5):每联系人可指定后端——`openai` 兼容 API 模式(同步回复)或 `mailbox` 信箱模式(入信箱,Muse 异步处理);两者**同时启用、按联系人路由**,默认后端可配。信箱后端产出的回复经 `/api/mailbox/replies` 回流,走与手动发送完全相同的安全闸门。

**对非值守型 harness(如 Muse)的明确约定**:不要使用同步 webhook 后端(它假设对端是常驻服务、同一次 HTTP 内立刻回复)。Muse 一律使用 mailbox 后端:引擎把消息入信箱(视同步回复为 null),回复经 `/api/mailbox/replies` 异步回流,目标由 chat_id 决定。旧 webhook 后端(供常驻型 harness 使用)的报文自 v1.1 起在顶层与 history/incoming 每条中同样携带 `msg_id` 与 `chat_id`(与信箱同源编号器,全局唯一)。WebSocket 实时流(/ws/logs)仅供浏览器 WebUI 观察用,**协议不依赖它**,Muse 无需任何长连接。

## 12. 示例

取信:
```json
GET /api/mailbox/messages?after=105
→ {"messages":[
   {"msg_id":106,"chat_id":"示例联系人","chat_type":"private","sender":"示例联系人",
    "is_from_me":false,"msg_type":"text","text":"你好,帮我看看这个","media_url":null,
    "ts":"2026-10-24T10:31:02+08:00"},
   {"msg_id":107,"chat_id":"示例联系人","chat_type":"private","sender":"self",
    "is_from_me":true,"msg_type":"text","text":"(助手先前的回复,忽略)","media_url":null,
    "ts":"2026-10-24T10:31:40+08:00"}],
  "next_cursor":107,"has_more":false}
```
回信+确认:
```json
POST /api/mailbox/replies {"chat_id":"示例联系人","reply_to_msg_id":106,"text":"好的,已处理…"}
→ 202 {"delivery_id":"d-000123"}
GET  /api/mailbox/deliveries/d-000123 → {"status":"delivered"}
POST /api/mailbox/ack {"cursor":107} → {"archived_until":107}
```
