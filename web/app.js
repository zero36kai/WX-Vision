/* 微信助手控制台前端 —— 原生 JavaScript,零依赖零 CDN。
 * 后端接口(见 wxbot/server/main.py):
 *   GET  /api/status          状态卡数据
 *   GET  /api/config          读配置(api_key / webhook_secret / 访问口令已脱敏)
 *   POST /api/config          存配置(400=校验失败,errors 为中文列表)
 *   POST /api/engine/start|stop
 *   POST /api/send            {contact, text};403=演练模式拦截/白名单外
 *   WS   /ws/logs             {seq, ts, level, module, text},连接即补发历史
 */
"use strict";

const $ = (id) => document.getElementById(id);
const MAX_SEND_CHARS = 2000;
const LOG_DOM_MAX = 500;          // 日志 DOM 上限(更早的节点移除,内存不涨)

/* ---------------- 通用:弹窗与提示 ---------------- */
let _modalResolve = null;
function confirmBox({ title = "确认操作", html = "", okText = "确定", danger = true }) {
  $("modalTitle").textContent = title;
  $("modalBody").innerHTML = html;
  $("modalOk").textContent = okText;
  $("modalOk").className = "btn " + (danger ? "btn-danger" : "btn-primary");
  $("modalMask").classList.remove("hidden");
  return new Promise((resolve) => { _modalResolve = resolve; });
}
function closeModal(v) {
  $("modalMask").classList.add("hidden");
  if (_modalResolve) { _modalResolve(v); _modalResolve = null; }
}
$("modalOk").addEventListener("click", () => closeModal(true));
$("modalCancel").addEventListener("click", () => closeModal(false));
$("modalMask").addEventListener("click", (e) => { if (e.target === $("modalMask")) closeModal(false); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("modalMask").classList.contains("hidden")) closeModal(false);
});

function toast(text, type = "") {
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = text;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 3800);
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------------- 🔑 访问口令 ----------------
 * 服务端(AccessTokenMiddleware)保护 /api/* 与 /ws/logs:
 * HTTP 用请求头 X-Access-Token,WebSocket 用查询参数 ?token=。
 * 口令保存在 localStorage;任何 401 都会清掉本地口令并弹回登录框。 */
const TOKEN_KEY = "wcv_access_token";
let accessToken = localStorage.getItem(TOKEN_KEY) || "";
let authLocked = true;   // true = 登录框显示中(接口调用照发,但不自动弹窗/重连)

function saveToken(t) {
  accessToken = t || "";
  try {
    if (accessToken) localStorage.setItem(TOKEN_KEY, accessToken);
    else localStorage.removeItem(TOKEN_KEY);
  } catch (_) { /* 隐私模式下 localStorage 不可用:仅本次会话内有效 */ }
}
function requireLogin(msg) {
  authLocked = true;
  $("loginError").textContent = msg || "";
  $("loginError").classList.toggle("hidden", !msg);
  $("loginMask").classList.remove("hidden");
  try { ws && ws.close(); } catch (_) { /* 未连接 */ }
  setTimeout(() => { try { $("loginToken").focus(); } catch (_) {} }, 60);
}
function loginOk() {
  authLocked = false;
  $("loginMask").classList.add("hidden");
  $("loginToken").value = "";
  $("loginError").classList.add("hidden");
  loadStatus();
  wsConnect();
}
async function tryLogin() {
  const t = $("loginToken").value.trim();
  if (!t) { requireLogin("请输入访问口令"); return; }
  saveToken(t);
  try {
    const { status } = await api("/api/status");
    if (status === 200) loginOk();
    else { saveToken(""); requireLogin("口令不正确,请重试(口令见启动窗口或 config.json 的 web.password)"); }
  } catch (e) {
    saveToken("");
    requireLogin("连不上服务:请确认 python run.py 窗口还开着(" + e.message + ")");
  }
}
$("loginOk").addEventListener("click", tryLogin);
$("loginToken").addEventListener("keydown", (e) => { if (e.key === "Enter") tryLogin(); });

async function api(path, opts) {
  const o = opts || {};
  const headers = Object.assign({}, o.headers || {});
  if (accessToken) headers["X-Access-Token"] = accessToken;
  const res = await fetch(path, Object.assign({}, o, { headers }));
  let data = {};
  try { data = await res.json(); } catch (_) { /* 非 JSON 响应 */ }
  if (res.status === 401 && !authLocked) {
    requireLogin("登录已失效(口令可能已修改),请重新输入访问口令");
  }
  return { status: res.status, ok: res.ok, data };
}

/* ---------------- 选项卡 ---------------- */
document.querySelectorAll("#tabs .tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tabs .tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll("main .panel").forEach((p) => p.classList.remove("active"));
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "send") loadSendTab();
    if (btn.dataset.tab === "config") loadConfig();
  });
});

/* ---------------- ① 状态 ---------------- */
let allowRealSend = false;
let engineRunning = false;

function setDot(id, cls) { const d = $(id); d.className = "dot" + (cls ? " " + cls : ""); }

function renderStatus(s) {
  allowRealSend = !!s.allow_real_send;
  engineRunning = !!s.engine_running;

  // 顶部模式徽标
  const badge = $("modeBadge");
  if (allowRealSend) { badge.textContent = "真实发送已开启"; badge.className = "badge badge-live"; }
  else { badge.textContent = "演练模式(不真发)"; badge.className = "badge badge-safe"; }

  // 微信窗口
  const w = s.wechat_window || {};
  if (w.found) {
    $("windowMain").textContent = "✅ 已找到微信窗口";
    const bits = [];
    if (w.minimized) bits.push("最小化(引擎会自动恢复)");
    else bits.push(w.foreground ? "当前在前台" : "在后台运行");
    $("windowSub").textContent = bits.join(" · ");
    setDot("dotWindow", "ok");
    $("cardWindow").className = "card card-ok";
  } else {
    $("windowMain").textContent = "❌ 未找到微信窗口";
    $("windowSub").textContent = w.error || "请登录电脑版微信后重试";
    setDot("dotWindow", "bad");
    $("cardWindow").className = "card card-bad";
  }

  // 引擎
  const e = s.engine || {}, eg = e.engine || {};
  if (engineRunning) {
    $("engineMain").textContent = "🟢 运行中";
    setDot("dotEngine", "ok");
  } else {
    $("engineMain").textContent = "⚪ 已停止";
    setDot("dotEngine", "");
  }
  let engSub;
  if (e.error) engSub = e.error;
  else if (engineRunning) {
    const bits = [`每 ${eg.poll_interval_sec ?? "?"} 秒检查一次`];
    if (eg.dry_run !== undefined) bits.push(eg.dry_run ? "演练模式(不真发)" : "⚠️ 真实发送中");
    if (typeof e.uptime_sec === "number" && e.uptime_sec > 0) {
      const m = Math.floor(e.uptime_sec / 60), sec = Math.round(e.uptime_sec % 60);
      bits.push(`已运行 ${m}分${sec}秒`);
    }
    engSub = bits.join(" · ") + ";详情见「② 实时日志」";
  } else engSub = "点下方「启动引擎」开始监控";
  $("engineSub").textContent = engSub;
  $("btnStart").disabled = engineRunning;
  $("btnStop").disabled = !engineRunning;

  // 智能体
  const a = s.agent || {};
  $("agentMain").textContent = `当前后端:${a.mode || "?"}` +
    (a.healthy === true ? " · 健康" : a.healthy === false ? " · 异常" : " · 健康未知");
  setDot("dotAgent", a.healthy === true ? "ok" : a.healthy === false ? "bad" : "warn");
  $("agentSub").textContent = a.detail || "";
}

async function loadStatus() {
  try {
    const { status, ok, data } = await api("/api/status");
    if (ok) renderStatus(data);
    else if (status !== 401) toast("读取状态失败", "err");   // 401 由 api() 统一弹登录框
  } catch (e) { $("engineSub").textContent = "连不上服务:请确认 python run.py 窗口还开着(" + e.message + ")"; }
}

$("btnRefresh").addEventListener("click", loadStatus);
$("btnStart").addEventListener("click", async () => {
  const yes = await confirmBox({
    title: "启动引擎?",
    html: allowRealSend
      ? "⚠️ <b>真实发送已开启</b>:引擎将自动检测白名单联系人的新消息,并可能以你的身份真实回复!\n\n仍要启动吗?"
      : "引擎将开始监控微信(仅白名单联系人)。\n当前为<b>演练模式</b>:只会记录将要发送的内容,不会真的发消息。",
    okText: "启动",
    danger: allowRealSend,
  });
  if (!yes) return;
  const { ok, data } = await api("/api/engine/start", { method: "POST" });
  if (ok) toast("引擎已启动", "ok"); else toast(data.error || "启动失败", "err");
  loadStatus();
});
$("btnStop").addEventListener("click", async () => {
  const yes = await confirmBox({ title: "停止引擎?", html: "停止后将不再检测/回复任何消息。", okText: "停止", danger: false });
  if (!yes) return;
  const { ok, data } = await api("/api/engine/stop", { method: "POST" });
  if (ok) toast("引擎已停止", "ok"); else toast(data.error || "停止失败", "err");
  loadStatus();
});

/* ---------------- ② 实时日志 ---------------- */
let ws = null, wsSeq = 0, autoscroll = true, reconnectTimer = null;
const logBox = $("logBox");

function levelEnabled(lv) {
  if (lv === "WARNING") return $("fWarn").checked;
  if (lv === "ERROR") return $("fErr").checked;
  if (lv === "DEBUG" || lv === "INFO") return $("fInfo").checked;
  return true;
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}
function appendLog(m) {
  if (typeof m.seq === "number") {
    if (m.seq <= wsSeq) return;      // 重连补历史时按序号去重
    wsSeq = m.seq;
  }
  const line = document.createElement("div");
  line.className = "logline lv-" + (m.level || "INFO");
  const show = levelEnabled(m.level);
  line.innerHTML = `<span class="t">${esc(fmtTime(m.ts))}</span>  ` +
    `<span class="lv">${esc((m.level || "INFO").padEnd(7))}</span>` +
    `<span class="mod">[${esc(m.module || "")}]</span> ${esc(m.text || "")}`;
  line.style.display = show ? "" : "none";
  line.dataset.level = m.level || "INFO";
  logBox.appendChild(line);
  while (logBox.childElementCount > LOG_DOM_MAX) logBox.firstElementChild.remove();
  const stick = autoscroll && (logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 30);
  if (stick) logBox.scrollTop = logBox.scrollHeight;
  const total = logBox.childElementCount;
  $("logCount").textContent = `共 ${total} 条`;
}
["fInfo", "fWarn", "fErr"].forEach((id) => $(id).addEventListener("change", () => {
  logBox.querySelectorAll(".logline").forEach((el) => {
    el.style.display = levelEnabled(el.dataset.level) ? "" : "none";
  });
}));
$("btnPause").addEventListener("click", () => {
  autoscroll = !autoscroll;
  $("btnPause").textContent = autoscroll ? "⏸ 暂停滚动" : "▶ 恢复滚动";
});
$("btnClear").addEventListener("click", () => { logBox.innerHTML = ""; $("logCount").textContent = "共 0 条"; });

function wsConnect() {
  if (authLocked) return;                 // 未通过口令验证前不连日志通道
  clearTimeout(reconnectTimer);
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  $("wsState").textContent = "日志连接中…";
  $("wsDot").className = "ws-dot";
  const q = accessToken ? "?token=" + encodeURIComponent(accessToken) : "";
  try { ws = new WebSocket(proto + location.host + "/ws/logs" + q); }
  catch (e) { wsRetry(); return; }
  ws.onopen = () => { $("wsDot").className = "ws-dot ok"; $("wsState").textContent = "已连接"; };
  ws.onmessage = (ev) => { try { appendLog(JSON.parse(ev.data)); } catch (_) { /* 忽略坏帧 */ } };
  ws.onclose = async (ev) => {
    if (authLocked) return;
    if (ev.code === 4401) { requireLogin("访问口令已更改,请重新输入"); return; }
    // 异常断开时探一次状态:若服务端已认为口令失效(401)就弹登录,否则照常重连
    try {
      const { status } = await api("/api/status");
      if (status === 401) return;         // api() 内部已弹登录框
    } catch (_) { /* 服务不可达,按普通断线处理 */ }
    wsRetry();
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}
function wsRetry() {
  $("wsDot").className = "ws-dot bad";
  $("wsState").textContent = "连接断开,2 秒后重连…";
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(wsConnect, 2000);
}
window.addEventListener("beforeunload", () => { try { ws && ws.close(); } catch (_) {} });

/* ---------------- ③ 手动发送 ---------------- */
async function loadSendTab() {
  try {
    const { status, ok, data } = await api("/api/config");
    if (!ok) { if (status !== 401) toast("读取白名单失败", "err"); return; }
    allowRealSend = !!data.allow_real_send;
    const banner = $("sendBanner"), btn = $("btnSend");
    if (allowRealSend) {
      banner.className = "banner banner-live";
      banner.innerHTML = "⚠️ <b>真实发送已开启</b>:点「发送」后消息会真实发给所选联系人(发送前仍会自动核对聊天窗口名字)。";
      btn.textContent = "✉️ 发送(真实)";
    } else {
      banner.className = "banner banner-warn";
      banner.innerHTML = "当前处于<b>演练模式</b>:点「演练发送」只会把\"要发什么\"记进日志,不会真的发出消息。";
      btn.textContent = "🧪 演练发送";
    }
    const wl = (data.config && data.config.safety && data.config.safety.whitelist) || [];
    const sel = $("sendTarget");
    sel.innerHTML = "";
    if (!wl.length) {
      sel.innerHTML = `<option value="">(白名单为空,请先到「④ 配置」填写)</option>`;
      btn.disabled = true;
    } else {
      wl.forEach((n) => {
        const o = document.createElement("option");
        o.value = n; o.textContent = n;
        sel.appendChild(o);
      });
      btn.disabled = false;
    }
  } catch (e) { toast("读取白名单失败:" + e.message, "err"); }
}
$("btnReloadTargets").addEventListener("click", loadSendTab);
$("sendText").addEventListener("input", () => { $("sendCount").textContent = String($("sendText").value.length); });
$("btnSend").addEventListener("click", async () => {
  const contact = $("sendTarget").value;
  const text = $("sendText").value;
  if (!contact) { toast("请先选择发送目标", "err"); return; }
  if (!text.trim()) { toast("消息内容不能为空", "err"); return; }
  const yes = await confirmBox({
    title: allowRealSend ? "确认真实发送?" : "确认演练发送?",
    html: allowRealSend
      ? `将以<b>你的身份</b>把下面这条消息真实发给 <b>${esc(contact)}</b>(白名单成员):\n<div class="quote">${esc(text)}</div>发送后无法撤回,确认吗?`
      : `演练模式:目标 <b>${esc(contact)}</b>(白名单成员),内容如下。\n<div class="quote">${esc(text)}</div>引擎只会记录发送意图,不会真的发出。`,
    okText: allowRealSend ? "真实发送" : "演练发送",
  });
  if (!yes) return;
  $("btnSend").disabled = true;
  try {
    const { ok, data } = await api("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contact, text }),
    });
    const box = $("sendResult");
    box.classList.remove("hidden");
    if (ok) {
      box.className = "result ok";
      box.textContent = (allowRealSend ? "✅ 已交给引擎真实发送" : "🧪 演练完成(未真实发送)") +
        "\n引擎执行记录(排查问题时才需要看,可直接复制反馈):" + JSON.stringify(data.engine_result, null, 2);
      $("sendText").value = ""; $("sendCount").textContent = "0";
    } else {
      box.className = "result err";
      box.textContent = "❌ " + (data.error || "请求失败,请稍后重试;仍不行请到「② 实时日志」找原因");
    }
  } catch (e) {
    $("sendResult").className = "result err";
    $("sendResult").classList.remove("hidden");
    $("sendResult").textContent = "❌ 网络错误:" + e.message;
  } finally {
    $("btnSend").disabled = false;
  }
});

/* ---------------- ④ 配置 ---------------- */
let cfgRealOrig = false;    // 进入页面时是否处于「允许真实发送」,用于检测用户改动

function setV(id, v) { $(id).value = v === undefined || v === null ? "" : v; }
function numV(id, dflt) {
  const v = parseFloat($(id).value);
  return Number.isFinite(v) ? v : dflt;
}

/* ---- 发送模式(单选二选一):演练 = dry_run:true + allow_real_send:false;
 *      真实 = dry_run:false + allow_real_send:true。保存时联动写入,
 *      页首徽标即时联动;底层两开关永远一致,杜绝「又演练又真实」。---- */
function setModeBadge(real) {
  const b = $("modeBadge");
  if (real) { b.textContent = "真实发送已开启"; b.className = "badge badge-live"; }
  else { b.textContent = "演练模式(不真发)"; b.className = "badge badge-safe"; }
}
function setSendMode(mode) {
  $("modeDry").checked = mode !== "real";
  $("modeReal").checked = mode === "real";
  setModeBadge(mode === "real");
}
document.querySelectorAll('input[name="sendMode"]').forEach((r) => {
  r.addEventListener("change", async () => {
    if (!$("modeReal").checked) { setModeBadge(false); return; }   // 切回演练=变安全,无需确认
    const yes = await confirmBox({
      title: "⚠️ 切换到「允许真实发送」?",
      html: "保存后,手动发送与自动回复都可能<b>以你的身份真实发出消息</b>\n"
        + "(仍仅限白名单联系人,发送前会自动核对窗口名字)。\n"
        + "请确认这是你本人的微信账号,并且你知道:以此身份发出去的消息,责任在你。",
      okText: "我已知晓,允许真实发送",
    });
    if (!yes) $("modeDry").checked = true;      // 取消 → 回到演练
    setModeBadge($("modeReal").checked);
  });
});

/* ---- 智能体后端(多后端管理)与按联系人路由 ---- */
const BACKEND_TYPES = [
  ["echo", "echo - 回声测试(不用配置,拿来试跑)"],
  ["openai", "openai - OpenAI 兼容接口(GLM/DeepSeek 等)"],
  ["webhook", "webhook - HTTP 回调(接常驻服务)"],
  ["mailbox", "mailbox - 信箱(接 Muse,异步回复)"],
];
let backendRows = [];       // [{id,type,base_url,api_key,model,system_prompt,webhook_url,webhook_secret,timeout_sec?,max_reply_chars?}]
let routingRows = [];       // [{contact, backend}]
let defaultBackendId = "";
let defaultPersona = "";    // personas.default:新建 openai 后端时预填人设,延续旧行为
// 信箱全局参数:mailbox 服务是全局单例,retention_hours 是全局配置(不随多个
// mailbox 后端各一份)。所有 mailbox 类型卡片同源展示同一个值,编辑任一张卡即
// 全局生效(保存时写入 mailbox.retention_hours;以最后编辑的为准)。
let mailboxRetention = 48;        // GET /api/config 后填充的当前生效值
let mailboxRetentionDraft = null; // 最后一次编辑的未保存值(字符串;null = 未编辑)

function backendFieldsHTML(b) {
  if (b.type === "openai") {
    return `
      <div class="grid2">
        <div><label class="fld-label">接口地址 base_url</label>
          <input class="input be-f" data-f="base_url" value="${esc(b.base_url)}" spellcheck="false"
            placeholder="例:https://api.deepseek.com"></div>
        <div><label class="fld-label">模型名 model(如 deepseek-chat / glm-4-flash)</label>
          <input class="input be-f" data-f="model" value="${esc(b.model)}" spellcheck="false"></div>
      </div>
      <label class="fld-label">API Key(已打码;留空保存 = 不修改)</label>
      <input class="input be-f" data-f="api_key" type="password" autocomplete="off" value="${esc(b.api_key)}">
      <label class="fld-label">人设(该后端专用;留空 = 不带人设)</label>
      <textarea class="input be-f" data-f="system_prompt" rows="2">${esc(b.system_prompt)}</textarea>`;
  }
  if (b.type === "webhook") {
    return `
      <label class="fld-label">回调地址(webhook_url)</label>
      <input class="input be-f" data-f="webhook_url" value="${esc(b.webhook_url)}" spellcheck="false">
      <p class="hint">你的服务会收到 POST 请求,内容含 contact、history、incoming 三个字段;在响应 JSON 里返回 reply 字段即作为回复。</p>
      <label class="fld-label">回调密钥(可选;已打码;留空保存 = 不修改)</label>
      <input class="input be-f" data-f="webhook_secret" type="password" autocomplete="off" value="${esc(b.webhook_secret)}">
      <p class="hint">webhook 后端要求对方服务<b>一直在线</b>,在同一次 HTTP 请求内立刻返回回复;
对方服务不常驻(如 Muse)请改用 mailbox 信箱后端。</p>`;
  }
  if (b.type === "mailbox") {
    const ret = mailboxRetentionDraft !== null ? mailboxRetentionDraft : mailboxRetention;
    return `
      <div class="be-note">📮 信箱后端<b>没有同步参数</b>:该联系人的消息进入信箱,由 Muse
异步处理并经信箱协议回信(回复自动发回原会话,无需填任何地址)。</div>
      <label class="fld-label">归档保留时长 retention_hours(小时;已确认归档的消息保留多久供查询)</label>
      <input class="input be-ret" type="number" step="1" min="1" value="${esc(ret)}" spellcheck="false">
      <p class="hint">信箱服务全局唯一,<b>所有 mailbox 后端共享此值</b>(编辑任一张 mailbox
卡片即全局生效,保存时写入 mailbox.retention_hours)。信箱监听范围 =
<b>私聊白名单 ∪ 群白名单</b>(自动跟随上方两个名单,无需单独配置),Muse 的回信也只允许
投递到该范围内,范围外一律拒收。</p>
      <p class="hint"><b>🔌 接入指引(Muse 侧怎么连到本信箱)</b><br>
① 本机直连:<code>http://127.0.0.1:8765</code>(端口跟随 web.port 配置);<br>
② 远程访问:需自备公网通道 —— Cloudflare Tunnel / frp / 任意反向代理,把公网地址
转发到本机 WebUI 端口。临时隧道一条命令:<code>cloudflared tunnel --url http://127.0.0.1:8765</code>
(输出临时地址,重启即换;固定域名用 named tunnel 方式,见 Cloudflare 官方文档);<br>
③ 鉴权:每个请求带请求头 <code>X-Access-Token: &lt;访问口令&gt;</code>(即本控制台的登录口令,
「🔑 访问口令」区可查改);<br>
④ 协议文档:项目内 <code>docs/PROTOCOL.md</code>(拉取 / 回信 / ack 端点与字段全定义,
Muse 接入前必读)。</p>`;
  }
  return `<p class="hint">echo 回声后端:不用配置也不联网,无论收到什么都回一句固定的测试语,用来把「读消息 → 回复 → 发送」整条流程先跑通。没有要填的参数。</p>`;
}

function backendIdOptions(selected) {
  return backendRows.map((b) => {
    const label = b.id + (b.type ? `(${b.type})` : "");
    return `<option value="${esc(b.id)}"${b.id === selected ? " selected" : ""}>${esc(label)}</option>`;
  }).join("");
}

function refreshBackendIdOptions() {
  // 默认后端下拉:当前默认被删/改名时回落到第一个后端
  if (!backendRows.some((b) => b.id === defaultBackendId)) {
    defaultBackendId = backendRows.length ? backendRows[0].id : "";
  }
  $("cfgDefaultBackend").innerHTML = backendIdOptions(defaultBackendId) ||
    `<option value="">(还没有后端,请先点「添加后端」)</option>`;
  // 路由行的后端下拉(保留当前选择;指向已删后端的行显示警示项,保存时拦截)
  document.querySelectorAll("#routingList .rt-backend").forEach((s) => {
    const row = routingRows[+s.closest(".rt-row").dataset.idx];
    if (!row) return;
    const exists = backendRows.some((b) => b.id === row.backend);
    s.innerHTML = (backendIdOptions(row.backend) || `<option value="">(无后端可选)</option>`)
      + (exists ? "" : `<option value="${esc(row.backend)}" selected>⚠ 后端「${esc(row.backend)}」不存在</option>`);
  });
}

function renderBackends() {
  $("backendList").innerHTML = backendRows.map((b, i) => `
    <div class="backend-card" data-idx="${i}">
      <div class="backend-head">
        <span class="backend-num">${i + 1}</span>
        <input class="input be-id" value="${esc(b.id)}" spellcheck="false"
          placeholder="后端 ID(路由用,如 echo-main / glm / muse)">
        <select class="input be-type">${BACKEND_TYPES.map(([v, t]) =>
          `<option value="${v}"${b.type === v ? " selected" : ""}>${t}</option>`).join("")}</select>
        <button type="button" class="btn btn-danger-outline btn-sm be-del" title="删除此后端">🗑 删除</button>
      </div>
      <div class="be-fields">${backendFieldsHTML(b)}</div>
    </div>`).join("");
  refreshBackendIdOptions();
}

function renderRouting() {
  $("routingList").innerHTML = routingRows.map((r, i) => `
    <div class="rt-row" data-idx="${i}">
      <input class="input rt-contact" value="${esc(r.contact)}" spellcheck="false"
        placeholder="联系人显示名(备注优先,无备注填昵称)">
      <span class="rt-arrow">→</span>
      <select class="input rt-backend"></select>
      <button type="button" class="btn btn-ghost btn-sm rt-del" title="删除这条路由">✖</button>
    </div>`).join("");
  $("routingEmpty").classList.toggle("hidden", routingRows.length > 0);
  refreshBackendIdOptions();
}

$("backendList").addEventListener("input", (e) => {
  const card = e.target.closest(".backend-card");
  const b = card && backendRows[+card.dataset.idx];
  if (!b) return;
  if (e.target.classList.contains("be-id")) {
    b.id = e.target.value;
    refreshBackendIdOptions();
  } else if (e.target.classList.contains("be-f")) {
    b[e.target.dataset.f] = e.target.value;
  } else if (e.target.classList.contains("be-ret")) {
    // 归档保留时长是全局单值:以最后编辑的为准,实时同步到其余 mailbox 卡片
    mailboxRetentionDraft = e.target.value;
    document.querySelectorAll("#backendList .be-ret").forEach((inp) => {
      if (inp !== e.target) inp.value = e.target.value;
    });
  }
});
$("backendList").addEventListener("change", (e) => {
  const card = e.target.closest(".backend-card");
  const b = card && backendRows[+card.dataset.idx];
  if (!b) return;
  if (e.target.classList.contains("be-type")) {
    b.type = e.target.value;
    card.querySelector(".be-fields").innerHTML = backendFieldsHTML(b);
  }
});
$("backendList").addEventListener("click", (e) => {
  const del = e.target.closest(".be-del");
  if (!del) return;
  const i = +del.closest(".backend-card").dataset.idx;
  const removed = backendRows.splice(i, 1)[0];
  if (removed && removed.id === defaultBackendId) defaultBackendId = "";
  renderBackends(); renderRouting();
  toast(`已移除后端「${removed ? removed.id : i + 1}」(未保存,可点「放弃修改」找回)`);
});
$("btnAddBackend").addEventListener("click", () => {
  let n = backendRows.length + 1;
  while (backendRows.some((b) => b.id === "backend-" + n)) n += 1;
  backendRows.push({
    id: "backend-" + n, type: "echo",
    base_url: "", api_key: "", model: "", system_prompt: defaultPersona,
    webhook_url: "", webhook_secret: "",
  });
  renderBackends();
  const cards = $("backendList").querySelectorAll(".backend-card");
  const last = cards[cards.length - 1];
  if (last) { last.scrollIntoView({ block: "nearest" }); last.querySelector(".be-id").focus(); }
});
$("cfgDefaultBackend").addEventListener("change", () => {
  defaultBackendId = $("cfgDefaultBackend").value;
});
$("routingList").addEventListener("input", (e) => {
  const row = e.target.closest(".rt-row");
  const r = row && routingRows[+row.dataset.idx];
  if (r && e.target.classList.contains("rt-contact")) r.contact = e.target.value;
});
$("routingList").addEventListener("change", (e) => {
  const row = e.target.closest(".rt-row");
  const r = row && routingRows[+row.dataset.idx];
  if (r && e.target.classList.contains("rt-backend")) r.backend = e.target.value;
});
$("routingList").addEventListener("click", (e) => {
  const del = e.target.closest(".rt-del");
  if (!del) return;
  routingRows.splice(+del.closest(".rt-row").dataset.idx, 1);
  renderRouting();
});
$("btnAddRouting").addEventListener("click", () => {
  routingRows.push({
    contact: "",
    backend: defaultBackendId || (backendRows[0] && backendRows[0].id) || "",
  });
  renderRouting();
  const rows = $("routingList").querySelectorAll(".rt-row");
  const last = rows[rows.length - 1];
  if (last) { last.scrollIntoView({ block: "nearest" }); last.querySelector(".rt-contact").focus(); }
});

/* 信箱监听范围说明已并入 mailbox 后端卡片(私聊白名单 ∪ 群白名单,自动跟随,
 * 无需单独配置);原先的「📬 信箱(Muse 接入)」独立板块已按 Muse 反馈移除
 * (notify_url 对无接收端的 Muse 不生效),监听范围不再单独展示。 */

function fillForm(c) {
  const s = (c.safety || {}), a = (c.agent || {}), cap = (c.capture || {}),
        rl = (c.rate_limit || {}), lg = (c.logging || {}), en = (c.engine || {}),
        gr = (c.groups || {}), mb = (c.mailbox || {});
  // 信箱全局参数:先于 renderBackends() 赋值,让所有 mailbox 卡片同源显示。
  // notify_url 不再进 UI(协议仍支持,config.json 既有值由服务端按键合并保留)。
  mailboxRetention = (mb.retention_hours !== undefined && mb.retention_hours !== null)
    ? mb.retention_hours : 48;
  mailboxRetentionDraft = null;             // 重新读取配置 = 丢弃未保存的手改值
  setV("cfgWhitelist", (s.whitelist || []).join("\n"));
  $("cfgAutoReply").checked = !!en.auto_reply_enabled;
  $("cfgKnowRisk").checked = !!s.require_know_risk;
  setV("cfgIgnore", (s.ignore_keywords || []).join("\n"));
  $("cfgGroupsEnabled").checked = !!gr.enabled;       // 群聊默认全关
  setV("cfgGroupMode", gr.reply_mode || "off");
  setV("cfgGroupWhitelist", (gr.whitelist || []).join("\n"));
  setV("cfgGroupBotNames", (gr.bot_names || []).join("\n"));

  // 后端列表:旧配置只有 mode(无 backends)时,按服务端同款迁移规则合成等价单后端
  const rawBackends = Array.isArray(a.backends) ? a.backends : [];
  const list = rawBackends.length ? rawBackends : [{
    id: a.mode || "echo", type: a.mode || "echo",
    base_url: a.base_url || "", api_key: a.api_key || "",
    model: a.model || "", system_prompt: a.system_prompt || "",
    webhook_url: a.webhook_url || "", webhook_secret: a.webhook_secret || "",
    timeout_sec: a.timeout_sec, max_reply_chars: a.max_reply_chars,
  }];
  backendRows = list.map((b) => {
    const row = {
      id: String(b.id || ""), type: String(b.type || "echo"),
      base_url: b.base_url || "", api_key: b.api_key || "",
      model: b.model || "", system_prompt: b.system_prompt || "",
      webhook_url: b.webhook_url || "", webhook_secret: b.webhook_secret || "",
    };
    // 透传不在 UI 编辑的数值字段,避免一次保存把它们重置回默认
    if (typeof b.timeout_sec === "number") row.timeout_sec = b.timeout_sec;
    if (typeof b.max_reply_chars === "number") row.max_reply_chars = b.max_reply_chars;
    return row;
  });
  defaultBackendId = String(a.default_backend || "");
  if (!backendRows.some((b) => b.id === defaultBackendId)) {
    defaultBackendId = backendRows.length ? backendRows[0].id : "";
  }
  routingRows = Object.entries(a.routing || {}).map(
    ([k, v]) => ({ contact: String(k), backend: String(v || "") }));
  renderBackends(); renderRouting();
  defaultPersona = (typeof c.personas === "object" && c.personas) ? (c.personas.default || "") : "";

  // 发送模式(单选二选一):allow_real_send && !dry_run 才算「真实」;
  // 旧配置若存在「允许发送但仍在演练」的糊涂组合,按实际生效的演练展示
  cfgRealOrig = !!c.allow_real_send && en.dry_run === false;
  setSendMode(cfgRealOrig ? "real" : "dry");

  setV("cfgPoll", en.poll_interval_sec !== undefined ? en.poll_interval_sec : 5);
  setV("cfgQpm", en.max_replies_per_contact_per_min !== undefined
    ? en.max_replies_per_contact_per_min : (rl.per_contact_qpm !== undefined ? rl.per_contact_qpm : 3));
  const dr = Array.isArray(rl.reply_delay_range_sec) ? rl.reply_delay_range_sec : [1, 3];
  setV("cfgDelayLo", dr[0]); setV("cfgDelayHi", dr[1]);
  setV("cfgLogLevel", lg.level || "INFO");
  setV("cfgOcrConf", cap.ocr_min_conf !== undefined ? cap.ocr_min_conf : 0.5);
}

async function loadConfig() {
  try {
    const { status, ok, data } = await api("/api/config");
    if (ok) fillForm(data.config || {});
    else if (status !== 401) toast("读取配置失败", "err");
  } catch (e) { toast("读取配置失败:" + e.message, "err"); }
}

$("btnReloadCfg").addEventListener("click", loadConfig);
$("btnHealthcheck").addEventListener("click", async () => {
  $("btnHealthcheck").disabled = true;
  $("btnHealthcheck").textContent = "⏳ 测试中…";
  try {
    const { ok, data } = await api("/api/agent/healthcheck", { method: "POST" });
    if (ok && data.healthy === true) toast("✅ 连接成功,智能体可用", "ok");
    else if (ok && data.healthy === false) toast("❌ 连接失败:请检查网络、API Key 和模型名是否填对", "err");
    else toast(data.detail || (data.error || "暂时测不了,请稍后再试"), "");
  } catch (e) { toast("测试请求失败:" + e.message, "err"); }
  finally {
    $("btnHealthcheck").disabled = false;
    $("btnHealthcheck").textContent = "🔌 测试连接";
  }
});
$("btnSaveCfg").addEventListener("click", async () => {
  const qpm = Math.max(1, Math.round(numV("cfgQpm", 3)));
  // 访问口令:留空 = 不修改;两遍输入需一致;改动立即生效(中间件每次请求现读)
  const newPwd = $("cfgNewPwd").value, newPwd2 = $("cfgNewPwd2").value;
  if (newPwd || newPwd2) {
    if (newPwd !== newPwd2) { toast("两次输入的新口令不一致", "err"); return; }
    if (newPwd.length < 6) { toast("新口令太短:至少 6 位(建议 12 位以上随机字符)", "err"); return; }
  }
  // —— 前端防呆校验:后端/路由问题一律阻止保存(不到服务端就拦下)——
  const errs = [];
  const ids = backendRows.map((b) => b.id.trim());
  if (!backendRows.length) errs.push("至少保留一个智能体后端(全部删除后机器人无法回复)");
  backendRows.forEach((b, i) => {
    if (!ids[i]) errs.push(`第 ${i + 1} 个后端还没填 ID(下方路由表靠 ID 找到它,必须填)`);
  });
  const dups = [...new Set(ids.filter((x, i) => x && ids.indexOf(x) !== i))];
  if (dups.length) errs.push(`后端 ID 重复:${dups.join("、")}(每个后端的 ID 必须唯一)`);
  const seenContact = new Set();
  routingRows.forEach((r, i) => {
    const c = r.contact.trim();
    if (!c) { errs.push(`路由表第 ${i + 1} 行没填联系人名字(不需要这条就点该行的 ✖ 删除)`); return; }
    if (seenContact.has(c)) errs.push(`路由表里联系人「${c}」出现了两次(同一联系人只填一行)`);
    seenContact.add(c);
    if (!r.backend || !ids.includes(r.backend)) {
      errs.push(`路由表里「${c}」指向的后端「${r.backend || "(空)"}」不存在,请改选或先添加该后端`);
    }
  });
  // 归档保留时长:mailbox 卡片里的全局单值(多卡同源,取最后编辑值;空/非数字 → 拦下)
  const retRaw = mailboxRetentionDraft !== null ? mailboxRetentionDraft : String(mailboxRetention);
  const retNum = parseFloat(retRaw);
  const retention = Math.round(retNum);
  if (!Number.isFinite(retNum) || retention < 1) {
    errs.push("归档保留时长(retention_hours)至少为 1 小时(在 mailbox 后端卡片内填写)");
  }
  if (errs.length) {
    const box = $("cfgErrors");
    box.classList.remove("hidden");
    box.className = "result err";
    box.textContent = "❌ 请先修正后再保存:\n" + errs.map((x) => "· " + x).join("\n");
    toast(`有 ${errs.length} 处配置问题未解决`, "err");
    return;
  }
  // 发送模式:演练 = dry_run:true + allow_real_send:false;真实 = 两者取反(联动互斥)
  const real = $("modeReal").checked;
  const routingObj = {};
  routingRows.forEach((r) => { routingObj[r.contact.trim()] = r.backend; });
  const payload = {
    safety: {
      whitelist: $("cfgWhitelist").value,
      ignore_keywords: $("cfgIgnore").value,
      auto_reply_enabled: $("cfgAutoReply").checked,   // 兼容字段,与 engine 组保持同值
      require_know_risk: $("cfgKnowRisk").checked,
    },
    engine: {
      dry_run: !real,                                  // 与 allow_real_send 联动,二选一
      auto_reply_enabled: $("cfgAutoReply").checked,
      poll_interval_sec: numV("cfgPoll", 5),
      max_replies_per_contact_per_min: qpm,
    },
    rate_limit: {
      per_contact_qpm: qpm,                            // 与 engine 组同值,避免两处口径不一
      reply_delay_range_sec: [numV("cfgDelayLo", 1), numV("cfgDelayHi", 3)],
    },
    agent: {
      backends: backendRows.map((b) => {
        const out = {
          id: b.id.trim(), type: b.type,
          base_url: b.base_url.trim(),
          api_key: b.api_key.trim(),        // 空/脱敏值 = 服务端视为不修改
          model: b.model.trim(),
          system_prompt: b.system_prompt,
          webhook_url: b.webhook_url.trim(),
          webhook_secret: b.webhook_secret.trim(),
        };
        if (typeof b.timeout_sec === "number") out.timeout_sec = b.timeout_sec;
        if (typeof b.max_reply_chars === "number") out.max_reply_chars = b.max_reply_chars;
        return out;
      }),
      default_backend: defaultBackendId,
      routing: routingObj,                  // 服务端校验:指向未知后端 → 400 中文报错
    },
    // 信箱段:只提交 retention_hours。notify_url 已从界面移除(Muse 无接收端,
    // 敲门通知不生效),但协议层仍支持:服务端按键合并,config.json 里既有的
    // mailbox.notify_url 不经过本页保存,保留不动、不清空、不覆盖。
    mailbox: { retention_hours: retention },
    logging: { level: $("cfgLogLevel").value },
    capture: { ocr_min_conf: numV("cfgOcrConf", 0.5) },
    groups: {
      enabled: $("cfgGroupsEnabled").checked,
      reply_mode: $("cfgGroupMode").value || "off",
      whitelist: $("cfgGroupWhitelist").value,     // 服务端归一(每行/逗号一个)
      bot_names: $("cfgGroupBotNames").value,
    },
    allow_real_send: real,                // 与 engine.dry_run 联动(演练 false / 真实 true)
  };
  if (newPwd) payload.web = { password: newPwd };
  const yes = await confirmBox({
    title: "保存配置?",
    html: "将校验并写入 config.json(旧文件自动备份为 config.json.bak)。\n"
      + (newPwd ? "⚠️ 本次将同时<b>修改访问口令</b>,保存后立即生效(本页自动换用新口令)!\n" : "")
      + (real
        ? "⚠️ 发送模式为<b>「允许真实发送」</b>:保存后立即生效,可能以你的身份真实发出消息!"
        : "发送模式为「演练模式」:一切发送只记录意图,不会真的发出。"),
    okText: "保存",
    danger: real || !!newPwd,
  });
  if (!yes) return;
  try {
    const { ok, data } = await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const box = $("cfgErrors");
    box.classList.remove("hidden");
    if (ok) {
      box.className = "result ok";
      box.textContent = "✅ 配置已保存(旧文件已备份)。\n" + (data.restart_hint || "");
      if (newPwd) {
        saveToken(newPwd);                    // 本页立即换用新口令
        $("cfgNewPwd").value = ""; $("cfgNewPwd2").value = "";
        box.textContent += "\n🔑 访问口令已更新:其他已登录的浏览器将被要求重新登录。";
      }
      loadConfig(); loadStatus();
    } else {
      box.className = "result err";
      box.textContent = "❌ 保存失败:\n" + (data.errors || [data.error || "未知错误"]).map((x) => "· " + x).join("\n");
    }
  } catch (e) { toast("保存请求失败:" + e.message, "err"); }
});

/* ---------------- 启动 ---------------- */
setInterval(() => { if (!document.hidden && !authLocked) loadStatus(); }, 5000);
(async function boot() {
  if (!accessToken) { requireLogin(); return; }
  // 已存口令:先验一次,通过即进入控制台;失效则清掉并弹登录框
  try {
    const { status } = await api("/api/status");
    if (status === 200) loginOk();
    else { saveToken(""); requireLogin("保存的口令已失效,请重新输入"); }
  } catch (e) {
    requireLogin("连不上服务:请确认 python run.py 窗口还开着(" + e.message + ")");
  }
})();
