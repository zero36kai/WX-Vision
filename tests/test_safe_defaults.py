# -*- coding: utf-8 -*-
"""安全默认回归:默认生成的 config 必须是"零发送"出厂状态。

exit 0 = 通过(末行打印 SAFE_DEFAULTS_OK);任一断言失败 exit 1。

断言对象 = wxbot.config.AppConfig() 的默认值 + load_config 在无配置文件时
自动生成的默认 config.json(仓库不携带任何真实 config.json,见 .gitignore):
1. effective allow_real_send == False(等效语义与 server/main.py 的
   _allow_real_send 一致:顶层 → safety 组 → send 组,任一为 true 即 true);
2. engine.dry_run == True(演练模式:只记录发送意图,绝不执行键鼠);
3. engine.auto_reply_enabled == False(自动回复总开关默认关);
4. agent.mode == "echo"(零配置回声模式);
5. safety.whitelist == ["文件传输助手"](微信内置、人人都有,不含任何
   真实用户信息);
6. web.password == ""(留空 = 首次启动由 run.py 自动生成随机口令并写回);
7. 末行打印 SAFE_DEFAULTS_OK 并 exit 0。

加载走 wxbot.config.load_config 的正式逻辑(dataclass 校验,校验失败会抛
ValueError → 本脚本按失败处理)。

红线:纯本地配置断言,不触碰微信窗口、不发送任何消息。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from wxbot.config import AppConfig, load_config  # 正式加载逻辑(ARCHITECTURE §2)

# 与 wxbot/server/main.py::_ALLOW_REAL_SEND_PATHS 相同的读取顺序:
# 顶层 → safety 组 → send 组(旧版兼容写法)。任一显式 true 即视为 true。
_EFFECTIVE_ALLOW_SEND_PATHS = (
    ("allow_real_send",),
    ("safety", "allow_real_send"),
    ("send", "allow_real_send"),
)


def effective_allow_real_send(raw: dict) -> tuple[bool, str]:
    """按 server 的等效语义读取'允许真实发送'总开关,返回(值, 来源描述)。"""
    for path in _EFFECTIVE_ALLOW_SEND_PATHS:
        node = raw
        try:
            for key in path:
                node = node[key]
        except (KeyError, TypeError):
            continue
        if isinstance(node, bool):
            if node:  # 显式 true:总开关开着,不安全
                return True, ".".join(path)
    return False, "全部读取路径均为 false/缺省"


def main() -> int:
    print("断言对象 1:AppConfig() 出厂默认值(wxbot.config)")
    failures: list[str] = []
    checks: list[tuple[str, bool, str]] = []
    cfg = AppConfig()

    checks.append(("默认 allow_real_send == False", cfg.allow_real_send is False,
                   f"实际={cfg.allow_real_send}"))
    checks.append(("默认 engine.dry_run == True", cfg.engine.dry_run is True,
                   f"实际={cfg.engine.dry_run}"))
    checks.append(("默认 engine.auto_reply_enabled == False",
                   cfg.engine.auto_reply_enabled is False,
                   f"实际={cfg.engine.auto_reply_enabled}"))
    checks.append(("默认 agent.mode == 'echo'", cfg.agent.mode == "echo",
                   f"实际={cfg.agent.mode!r}"))
    checks.append(("默认 safety.whitelist == ['文件传输助手']",
                   cfg.safety.whitelist == ["文件传输助手"],
                   f"实际={cfg.safety.whitelist}"))
    checks.append(("默认 web.password == ''(留空=首次启动自动生成)",
                   cfg.web.password == "", f"实际={cfg.web.password!r}"))
    checks.append(("默认配置通过 validate()", cfg.validate() == [],
                   f"实际={cfg.validate()}"))

    print("断言对象 2:无配置文件时 load_config 自动生成的默认 config.json")
    with tempfile.TemporaryDirectory(prefix="wxbot_safe_defaults_") as td:
        p = Path(td) / "config.json"
        try:
            gen = load_config(p, create_default=True)
        except Exception as e:
            print(f"FAIL: 默认 config.json 生成/校验失败: {type(e).__name__}: {e}")
            return 1
        raw = json.loads(p.read_text(encoding="utf-8"))
        eff, source = effective_allow_real_send(raw)
        checks.append(("生成的 allow_real_send == False", eff is False,
                       f"实际={eff}(来源: {source})"))
        checks.append(("生成的 engine.dry_run == True", gen.engine.dry_run is True,
                       f"实际={gen.engine.dry_run}"))
        checks.append(("生成的 engine.auto_reply_enabled == False",
                       gen.engine.auto_reply_enabled is False,
                       f"实际={gen.engine.auto_reply_enabled}"))
        checks.append(("生成的 agent.mode == 'echo'", gen.agent.mode == "echo",
                       f"实际={gen.agent.mode!r}"))
        checks.append(("生成的 safety.whitelist == ['文件传输助手']",
                       gen.safety.whitelist == ["文件传输助手"],
                       f"实际={gen.safety.whitelist}"))
        checks.append(("生成的 web.password == ''",
                       gen.web.password == "" and raw.get("web", {}).get("password") == "",
                       f"实际={gen.web.password!r}"))

    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}({detail})")
        if not ok:
            failures.append(name)

    if failures:
        print(f"\nSAFE_DEFAULTS_FAIL: {len(failures)} 项不安全: {failures}")
        print("!! 出厂默认被改动,发布/使用前必须先恢复零发送默认组合。")
        return 1
    print("\n全部检查通过:默认生成的配置 = echo + 演练 + 白名单(文件传输助手) + 零发送。")
    print("SAFE_DEFAULTS_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
