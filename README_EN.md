<p align="center">
  <img src="web/assets/logo_256.png" width="160" alt="WeChatVision logo">
</p>

# WeChatVision — Vision-Based WeChat Automation Assistant

A **pure-vision + human-like keyboard/mouse** automation assistant for the Windows WeChat client: it reads messages via screenshots + OCR, lets a pluggable agent draft replies, and sends them the way a human would — with the mouse and keyboard. Ships with a Chinese Web console; double-click and it runs.

- **Pure vision, no injection**: it only "looks at the screen" and "moves the pointer" — it never touches WeChat's process internals;
- **Safe out of the box**: rehearsal mode by default (everything is logged, nothing is actually sent) + a whitelist (which contains only WeChat's built-in "File Transfer" contact by default);
- **Mailbox protocol**: implements the Muse Mailbox protocol v1, so any external program/agent can fetch messages and post replies over plain HTTP polling.

> [!IMPORTANT]
> ## Compliance & Disclaimer (please read before use)
> - This project is for **personal study and research only**. Use it only with your own WeChat account and in lawful, compliant scenarios;
> - It must **not** be used for harassment, bulk messaging, marketing/spam, or automated friend-adding;
> - Automating a real client carries a real risk of triggering **WeChat's risk control** (feature restrictions or even account suspension). You assume all consequences of using it;
> - By using this project you agree to comply with the WeChat Software License and Service Agreement. The developers are not responsible for any misuse.

---

## 1. How It Works

```
        ┌────────────  Perception (eyes only)  ─────────┐
        │ PrintWindow capture (works even when occluded) │
        │ RapidOCR (on-device): text + box + confidence  │
        └──────────────────┬────────────────────────────┘
                           ▼
        ┌────────────  Decision  ───────────────────────┐
        │ whitelist filter → fingerprint dedup →        │
        │ rate limiting → agent backend                 │
        │ (echo / openai / webhook / mailbox)           │
        └──────────────────┬────────────────────────────┘
                           ▼
        ┌────────────  Action (human-like)  ────────────┐
        │ Bezier mouse paths + randomized pauses;       │
        │ Chinese input via clipboard Ctrl+V;           │
        │ window title and typed content re-checked     │
        │ with OCR before and after every send          │
        └──────────────────────────────────────────────┘
```

What it explicitly does **not** do:

- ❌ No DLL injection, no hooks, no memory reading;
- ❌ No UIAutomation control tree (which WeChat 4.x barely exposes anyway);
- ❌ No modification or replacement of any WeChat file;
- ❌ No chat content is uploaded to any developer-owned server — only the agent backends **you configure yourself** (e.g., an LLM API with your own key) ever receive message text.

---

## 2. Screenshots

| | |
|---|---|
| ![Status](docs/screenshots/01-status.png) | ![Config](docs/screenshots/02-config.png) |
| ![Manual send](docs/screenshots/03-send.png) | ![Help](docs/screenshots/04-help.png) |
| ![Login](docs/screenshots/05-login.png) | |

> If the screenshot files are not present in your copy, just run the app and compare against the real UI. The five images correspond to: ① Status, ④ Config, ③ Manual Send, ⑤ Help, and the access-token login dialog.

---

## 3. Quick Start

### Requirements

- Windows 10 / 11;
- Python 3.11 or newer (developed on 3.12);
- **WeChat 4.x** for Windows, logged in (main window open or minimized both work);
- If importing onnxruntime fails with an MSVCP140.dll error, install the [Microsoft VC++ 2015-2022 Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe) first.

### Step 1 — Install dependencies (once)

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

(Any PyPI mirror works; the Tsinghua mirror is shown because the project is China-based.)

### Step 2 — Launch

With WeChat logged in, either:

- **Double-click `启动微信助手.vbs`** (recommended: no console window; double-clicking again never starts a second copy, it just reopens the console page; logs live under `logs/`);
- or run from a terminal:

```bash
python run.py
```

Common flags:

| Command | Purpose |
|---|---|
| `python run.py --port 8888` | Use a different port (default comes from `web.port` in config.json, i.e. 8765; occupied ports are skipped automatically) |
| `python run.py --no-browser` | Do not auto-open the browser |
| `python run.py --config D:\my.json` | Use a specific config file |

To stop: double-click `停止微信助手.bat` (stops only this project's process; WeChat itself is untouched).

### Step 3 — Open the console

The browser opens `http://127.0.0.1:8765` automatically. **On first launch a random access token (≥12 chars) is generated**, written to `web.password` in config.json, and printed prominently once (inside a star banner) in the launcher window — save it, you need it to log in.

The console has 5 tabs: ① Status / ② Live Log / ③ Manual Send / ④ Config / ⑤ Help.

### Step 4 — First-run self-check (recommended, in order)

1. On the **Status** tab, check three cards: WeChat window "found"; engine "stopped" (expected); agent shows `current backend: echo · healthy`;
2. Keep the defaults (**echo mode + rehearsal mode**), click "Start Engine", and watch the Live Log tab scroll normally;
3. From your phone, send a message to the whitelisted contact (by default the built-in "File Transfer" contact). The log should show: message received → agent invoked → rehearsal intent (nothing actually sent);
4. When everything works and you want the bot to **actually** send: on the Config tab switch "Send mode" to "Allow real sending" (a mutually exclusive choice; switching and saving both ask for confirmation; only whitelisted contacts can ever receive messages).

> To start from the shipped template instead of generated defaults: copy `config.example.json` to `config.json` and edit as needed. Skipping this is also fine — a default config is generated on first launch.

---

## 4. Configuration (config.json)

Everything is editable in the Config tab of the Web console; every save auto-backs up the previous file as `config.json.bak`. Fields map one-to-one onto `config.example.json`. The important ones:

### 4.1 Contact whitelist

Config → "Safety & whitelist", **one contact per line** (names must match what WeChat displays — remark first, otherwise nickname). The bot only reads and sends for whitelisted contacts; by default the list contains only the built-in "File Transfer" contact. Messages from anyone else are ignored outright. Restart the engine after editing the list.

### 4.2 Send mode (either/or, single choice)

| Option | Written on save | Behavior |
|---|---|---|
| Rehearsal (default) | `engine.dry_run=true` + `allow_real_send=false` | Every send is only recorded as an intent; no mouse/keyboard is ever touched |
| Allow real sending | `engine.dry_run=false` + `allow_real_send=true` | Messages may actually be sent as you (whitelist only; window title re-checked via OCR) |

The program guarantees the two low-level switches always stay consistent — you can never end up in a half-real, half-rehearsal state.

### 4.3 Access token

Every API is protected by the access token; see Quick Start for where it comes from. Change it any time on the Config tab (takes effect immediately, other logged-in browsers are kicked back to the login dialog). If config.json gets corrupted at runtime, auth falls back to the last successfully read token (fail-closed) — the service can never turn into a no-auth open door.

### 4.4 Agent backends (4 types) + per-contact routing

Config → "Agent backends": add/edit any number of backends; the "routing" table pins specific contacts to specific backends; everyone else goes to the default backend.

| Type | Purpose | Required fields |
|---|---|---|
| `echo` | Echo test; no config, no network; use it to verify the pipeline | none |
| `openai` | GLM / DeepSeek and other OpenAI-compatible APIs | base_url, api_key, model, system_prompt |
| `webhook` | HTTP callback for your own always-on service (replies within the same request) | webhook_url, webhook_secret (optional) |
| `mailbox` | **Mailbox mode**: messages go to the mailbox for asynchronous processing by an external program; no synchronous reply locally | none (archive retention is configured on the card) |

Equivalent config.json snippet:

```json
"agent": {
  "backends": [
    {"id": "echo-main", "type": "echo"},
    {"id": "glm",       "type": "openai", "base_url": "…", "api_key": "…", "model": "…"},
    {"id": "muse",      "type": "mailbox"}
  ],
  "default_backend": "echo-main",
  "routing": { "some contact": "muse", "another contact": "glm" }
}
```

API keys are displayed masked (`sk-****xxxx`); saving an empty value means "keep the existing key". Contact names follow the same rule as the whitelist: window display name, remark first.

### 4.5 Other frequently used settings

- **Human-like cadence** (`human` section): ±3 px click jitter, randomized pre-click pauses, a 15% chance of an occasional longer hesitation, etc. Defaults are the recommended values;
- **Rate limiting** (`rate_limit`): 3 replies/min per contact, 20/min globally, 5 s per-contact cooldown; excess replies are silently dropped and logged — anti-flooding, anti-ban;
- **Group chats** (`groups`): **fully disabled by default** — the engine does not even open a group chat. If you really need it, follow the in-app group chat guide step by step; real sending into groups additionally requires all "three gates" (groups enabled + group in group whitelist + group real-send switch);
- **Archive retention** (`mailbox.retention_hours`, default 48 h): how long acknowledged mailbox messages are kept after archiving.

---

## 5. Muse Mailbox Integration (for external programs / agents)

If your AI program (called **Muse** in the protocol — any HTTP-capable harness qualifies) is not always online and has no public IP, push won't work. So the project uses a **mailbox (pull) model**: this assistant is the only always-on server; incoming messages land in the mailbox; Muse polls for new mail, processes, replies, and acknowledges.

**Steps (Muse side):**

1. On the Config tab add a **mailbox-type** backend card and route the target contact to it. The mailbox listen scope = private-chat whitelist ∪ group whitelist (follows both automatically, nothing extra to configure);
2. Give Muse a route that reaches this machine:
   - **Same machine**: just use `http://127.0.0.1:8765` (port follows `web.port`);
   - **Across machines**: bring your own public tunnel, e.g. a one-line Cloudflare quick tunnel:
     ```bash
     cloudflared tunnel --url http://127.0.0.1:8765
     ```
     (prints a temporary `trycloudflare.com` URL that dies with the process; for a fixed domain use a named tunnel — see Cloudflare's docs. frp or any reverse proxy works equally well.)
3. **Auth**: every request carries the header `X-Access-Token: <access token>` (the same token used to log into the console; `?token=` query parameter also accepted);
4. Poll per the protocol: `GET /api/mailbox/messages?after=<cursor>` for new mail → `POST /api/mailbox/replies` to reply (echo the original `chat_id` back) → `POST /api/mailbox/ack` to acknowledge and archive.

> The full protocol — 8 endpoints, field tables, dedup/retry/anti-loop rules, polling pseudocode — lives in **[docs/PROTOCOL.md](docs/PROTOCOL.md)**. Required reading before integrating.

Cheat sheet: `msg_id` is the only dedup key; messages with `is_from_me=true` are never processed (loop protection); unacked messages are re-delivered forever; replies to out-of-scope `chat_id`s are rejected with 403; mailbox replies go through **exactly the same safety gates** as manual sends, and when a gate refuses, the delivery ends as `failed` with a Chinese reason — never a fake "delivered".

⚠️ **Never share the token — the tunnel plus the token equals control**: anyone with both can read your mail and make the bot reply as you. Keep tunnels on-demand and short-lived; if you suspect the token leaked, change it on the Config tab (immediate effect).

---

## 6. Security Model

1. **Two independent layers before any real send**: the mutually exclusive "send mode" choice (rehearsal/real) is layer one; the engine-side whitelist plus an **OCR re-check that the current chat window title equals the target contact** — abort on mismatch — is layer two, and neither can be bypassed;
2. **Whitelist everywhere**: private chats only for whitelisted members; groups have zero contact by default, and even when enabled require the group whitelist plus the three gates;
3. **Panic stop**: while the program is moving the mouse or typing, **fling your mouse to the top-left corner of the screen (0,0)** and every automated action aborts immediately (pyautogui FAILSAFE, always on, cannot be — and will not be — disabled). Then press "Stop Engine" in the console;
4. **Local-only binding**: the service binds to `127.0.0.1` only; nobody else on your LAN can reach it. Please do not change the bind address to `0.0.0.0`;
5. **Bounded logs**: rotating file logs under `logs/bot.log` (~50 MB cap); the live log in the browser is an in-memory ring buffer of the last 500 lines; mailbox data lives in `data/` (gitignored — chat content must never be committed).

---

## 7. FAQ

**Q: The status page says "WeChat window not found"?**
Log into WeChat 4.x for Windows and keep the main window available. Being minimized to the tray is fine; the engine restores the window automatically when needed.

**Q: Does a minimized WeChat break anything?**
No. The engine restores and foregrounds the window when it needs to (you may see a flash); minimize it freely otherwise.

**Q: The log shows "blank shell / rendering not restored" or captures come out as solid color?**
WeChat occasionally renders an empty window. The program presses `Ctrl+Alt+W` (WeChat's built-in show/hide hotkey) a few times so WeChat repaints itself; it usually recovers within seconds. Nothing to do.

**Q: Startup fails with MSVCP140.dll / onnxruntime errors?**
Install the Microsoft VC++ 2015-2022 Redistributable (x64): https://aka.ms/vs/17/release/vc_redist.x64.exe , then reopen the terminal. Also note `opencv-python` and `opencv-python-headless` must never be installed together (they clobber each other's cv2 and crash); if pip pulled in both, uninstall one.

**Q: OCR is very slow the first time?**
Normal. The OCR model loads lazily on first use — a few seconds to a few tens of seconds — and is fast afterwards.

**Q: "Port already in use" / the browser does not open?**
Start on another port: `python run.py --port 8888`, then open `http://127.0.0.1:8888` manually.

**Q: Why didn't the bot reply to someone?**
Open the Live Log tab — every skipped message logs its reason: not whitelisted / hit an ignore keyword / duplicate / rate-limited / auto-reply disabled.

**Q: I broke my config. How do I recover?**
Every save auto-backs up to `config.json.bak`; stop the app and rename the `.bak` back to `config.json`. Or simply delete `config.json` — a fresh default is generated on the next launch.

**Q: How do I plug in GLM / DeepSeek or another LLM?**
Config → Agent backends → "Add backend", type `openai`, fill in base_url + API key + model, then pick it in the "Default backend" dropdown; use the "test connection" button. To route a specific contact elsewhere, see §4.4.

---

## 8. Development Self-Tests (fully offline — no WeChat interaction)

```bash
python tests/test_server.py          # server acceptance (last line SERVER_TEST_PASS = ok)
python tests/test_engine.py          # engine acceptance (ENGINE_TEST_PASS)
python tests/test_mailbox.py         # mailbox protocol acceptance (MAILBOX_TEST_PASS)
python tests/test_group.py           # offline group-chat acceptance (GROUP_TEST_PASS)
python tests/test_privacy.py         # privacy scan: no private data staged for commit + .gitignore coverage (PRIVACY_PASS)
python tests/test_launch.py          # launcher self-test (LAUNCH_TEST_PASS)
python tests/test_human.py           # human-like input profile acceptance (HUMAN_TEST_PASS)
python tests/test_auth_failclosed.py # auth fail-closed hardening acceptance (AUTH_FAILCLOSED_PASS)
python tests/test_safe_defaults.py   # factory-default regression: zero-send combination (SAFE_DEFAULTS_OK)
node --check web/app.js              # frontend syntax check (needs Node.js; skip if unavailable)

# Read-only on-machine acceptance (requires a logged-in WeChat; never sends, never touches the input box):
python tests/test_readonly.py        # target contact = env WECHAT_TEST_CONTACT → config whitelist[0] → fallback "File Transfer"
```

Exit code 0 means the suite passed. After code changes, at minimum run server / engine / privacy.

---

## 9. Project Layout

```
WeChatVision/
├── run.py                     # one-shot launcher: starts the service and opens the browser (rehearsal by default)
├── requirements.txt           # dependencies (pip install -r)
├── config.example.json        # config template (copy to config.json and edit)
├── 启动微信助手.bat            # silent launcher entry (delegates to the .vbs)
├── 启动微信助手.vbs            # recommended double-click launcher (no console window)
├── 停止微信助手.bat            # stop the service (WeChat untouched)
├── wxbot/                     # core package
│   ├── config.py              # config load/save/validate/defaults
│   ├── vision/                # window find & capture / OCR / region anchoring
│   ├── actions/               # human-like keyboard+mouse / high-level flows
│   ├── agent/                 # pluggable agent backends (echo/openai/webhook)
│   ├── engine/                # monitor engine / safety gate / dedup & context / group routing
│   ├── mailbox/               # Muse Mailbox protocol v1 (store/queue/media/notify)
│   └── server/                # FastAPI server + WebSocket log stream
├── web/                       # single-page console (plain HTML/JS/CSS, no build step, no CDN)
│   └── assets/                # logo / favicon
├── tests/                     # offline regression + read-only on-machine acceptance (see §8)
└── docs/
    ├── ARCHITECTURE.md        # architecture (stack / module contracts / hard rules)
    ├── PROTOCOL.md            # Muse Mailbox protocol v1 (required reading for integrators)
    ├── research/              # research notes 01-03 (UI flows / vision stack / agent & WebUI)
    └── screenshots/           # UI demo screenshots
```

Generated at runtime (all gitignored, never committed): `config.json` (runtime config incl. the token), `logs/`, `data/` (mailbox data incl. chat content), `tests/_debug/`.

---

## 10. Documentation Index

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Architecture & stack, module contracts, project hard rules |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | Muse Mailbox protocol v1: endpoints, fields, dedup/retry/ack rules |
| [docs/research/01-wechat-ui-flows.md](docs/research/01-wechat-ui-flows.md) | WeChat 4.x UI flow research (search / read / send / unread detection) |
| [docs/research/02-vision-stack.md](docs/research/02-vision-stack.md) | Capture / OCR / template matching / simulated input stack research |
| [docs/research/03-agent-webui.md](docs/research/03-agent-webui.md) | Agent integration & local WebUI design research |
| [README.md](README.md) | 中文说明(Chinese version of this document) |
