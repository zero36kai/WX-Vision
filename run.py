# -*- coding: utf-8 -*-
"""一键启动:python run.py [--port 8765] [--host 127.0.0.1] [--no-browser]

流程:读/生成 config.json → 确保 web.password(为空则自动生成≥12位随机口令,
写回 config.json 并在启动日志醒目打印一次)→ resolve_port 端口探测(被占用
自动 +1 顺延)→ 装配监控引擎(不启动)→ create_app → 等端口就绪后自动打开
浏览器(用最终端口)→ uvicorn 阻塞运行(仅监听本机回环)。

双击友好:任何启动异常都会打印中文可读错误,并保持窗口 30 秒再退出,
避免控制台一闪而过、看不到失败原因(2026-10-24 修复:8765 被无关进程
占用导致 uvicorn 绑定失败 → 闪退 → 浏览器打不开)。

红线保持:默认演练模式(dry_run / allow_real_send=false),不会真实发送;
pyautogui FAILSAFE 始终开启;服务只监听 127.0.0.1,不暴露局域网。
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

DEFAULT_PORT = 8765


def _port_free(host: str, port: int) -> bool:
    """探测 host:port 当前是否可绑定(bind+listen 双重确认,贴近 uvicorn 的实际占用判定)。

    Windows 上不设 SO_REUSEADDR:该选项在 Windows 语义不同,会导致误判已占用端口为空闲。
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.bind((host, port))
            s.listen(1)
        return True
    except OSError:
        return False


def resolve_port(host: str, preferred: int, max_tries: int = 20) -> int:
    """从 preferred 起逐个 +1 探测,返回第一个可绑定的空闲端口。

    preferred 本身空闲时原样返回;连续 max_tries 个端口全部被占用则抛
    RuntimeError(由调用方转成中文可读提示)。纯探测、不做任何打印,
    便于离线单测(tests/test_launch.py)。
    """
    for offset in range(max_tries):
        port = preferred + offset
        if _port_free(host, port):
            return port
    raise RuntimeError(
        f"端口 {preferred}~{preferred + max_tries - 1} 全部被占用;"
        "请用 --port 换一个端口,或关闭占用端口的程序后重试。"
    )


def _wait_port_open(host: str, port: int, timeout: float = 15.0) -> bool:
    """轮询端口直到可连接(再开浏览器,避免浏览器先于服务就绪而报错)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _open_page(url: str) -> None:
    """打开控制台页面并尽量把浏览器窗口带到前台。

    webbrowser.open(os.startfile)在浏览器已在运行时通常只开一个后台
    标签页、不弹出新窗口(2026-10-25 实测:用户双击时看着像"没反应")。
    这里优先按注册表 https 协议处理器定位默认浏览器,用 --new-window
    (Chromium 系:Edge/Chrome 等)/ -new-window(Firefox)强制开新窗口
    置前;任一步失败都退回 webbrowser.open,绝不抛错。
    """
    try:
        import re
        import subprocess
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, r"https\shell\open\command"
        ) as k:
            tmpl = (winreg.QueryValue(k, None) or "").strip()
        # 去掉 %1 占位符与处理器自带的定位参数,换成显式新窗口参数
        for tok in ('"%1"', "%1", '"%l"', "%l",
                    "--single-argument", "-osint", "-url"):
            tmpl = tmpl.replace(tok, " ")
        tmpl = re.sub(r"\s+", " ", tmpl).strip()
        if tmpl:
            new_win = "-new-window" if "firefox" in tmpl.lower() else "--new-window"
            subprocess.Popen(
                f'{tmpl} {new_win} "{url}"',
                shell=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
    except Exception:
        pass
    webbrowser.open(url)


def _clean_old_launcher_logs(max_age_days: int = 3) -> None:
    """清理过期的启动器日志(logs/webui_*.log,启动器每次双击生成一个独立文件)。

    独立文件是为避免"服务实例长持日志文件时,再次双击的重定向因文件被
    占用而失败"的坑(2026-10-25 修复);3 天前的旧启动日志在此统一清走。
    """
    cutoff = time.time() - max_age_days * 86400
    log_dir = ROOT / "logs"
    if not log_dir.is_dir():
        return
    for p in log_dir.glob("webui_*.log"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def _open_browser_later(host: str, port: int) -> None:
    """后台等端口就绪后打开浏览器;port 必须是 resolve_port 返回的最终端口。"""
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"

    def _open():
        if _wait_port_open(host, port):
            print(f"[启动] 打开浏览器: {url}")
            try:
                _open_page(url)
            except Exception as e:
                print(f"[提示] 浏览器自动打开失败({e}),请手动访问: {url}")
        else:
            print(f"[提示] 服务迟迟未就绪,请手动访问: {url}")

    threading.Thread(target=_open, name="opener", daemon=True).start()


def _hold_console(seconds: int = 30) -> None:
    """双击场景:出错后保持控制台窗口一段时间,让用户看清中文错误提示再退出。"""
    print(f"\n[提示] 窗口将在 {seconds} 秒后自动关闭,请先截图或记录上方错误信息……")
    sys.stdout.flush()
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        print("[退出] 已跳过等待。")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run.py", description="微信助手控制台一键启动(默认演练模式,不会真实发送)")
    parser.add_argument("--port", type=int, default=None,
                        help="服务端口(默认取 config.json 的 web.port,即 8765;"
                             "若被占用会自动顺延到下一个空闲端口)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址,仅限本机(默认 127.0.0.1,请勿改为 0.0.0.0)")
    parser.add_argument("--no-browser", action="store_true",
                        help="不自动打开浏览器")
    parser.add_argument("--config", default=str(ROOT / "config.json"),
                        help="config.json 路径(默认项目根目录)")
    return parser.parse_args()


def _launch(args: argparse.Namespace) -> int:
    """装配并阻塞运行服务;返回退出码(0=正常)。启动类失败一律返回非 0。"""
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("[警告] 出于安全考虑,服务只应监听本机回环地址;你选择了"
              f" {args.host},WebUI 将暴露给局域网/外网,风险自负!")

    print("=" * 62)
    print("  微信助手(WeChatVision)控制台")
    print("  默认演练模式:引擎只记录发送意图,不会真实发送任何消息")
    print("  急停:把鼠标快速甩到屏幕左上角,一切模拟动作立即中止")
    print("=" * 62)

    # ---- 配置(不存在则生成默认值) ----
    from wxbot.config import load_config
    from wxbot.server.events import EventBus
    try:
        cfg = load_config(args.config, create_default=True)
        problems = cfg.validate()
        if problems:
            print("[警告] config.json 存在配置问题(服务仍会启动):")
            for p in problems:
                print("        -", p)
    except Exception as e:
        print(f"[错误] 配置加载失败: {e}")
        print("       可删除 config.json 后重试(将重新生成默认配置)。")
        return 1

    # ---- 访问口令:web.password 为空则自动生成(≥12位)并写回 config.json ----
    # 远程访问(公网隧道)与本机浏览器首次登录的唯一门禁;口令每次请求现读,
    # 配置页修改后立即生效,无需重启。
    from wxbot.server.main import ensure_web_password
    try:
        password, generated = ensure_web_password(args.config)
    except Exception as e:
        print(f"[错误] 访问口令生成失败: {e}")
        return 1
    if generated:
        print()
        print("*" * 66)
        print("  【重要】已自动生成访问口令(随机 ≥12 位),并写入 config.json")
        print("  的 web.password 字段。通过公网隧道远程访问 WebUI 时需要输入:")
        print()
        print(f"      🔑 访问口令: {password}")
        print()
        print("  本机浏览器首次打开控制台时也需输入一次(之后记在浏览器里)。")
        print("  可在 WebUI「④ 配置」页修改;请勿把口令泄露给不信任的人。")
        print("*" * 66)
        print()
        sys.stdout.flush()  # 重定向/管道场景下也立即落盘,确保"醒目打印一次"成立

    preferred = args.port if args.port is not None else int(cfg.web.port)

    # ---- 单实例保护:该端口已有本服务在运行(200/401 均算)则不重复启动 ----
    # 只打开浏览器指向既有实例后退出;多次双击 vbs/bat 不会产生第二个进程。
    def _already_running(h: str, p: int) -> bool:
        try:
            import httpx
            host = "127.0.0.1" if h in ("0.0.0.0", "::") else h
            # trust_env=False:不受系统代理环境变量干扰,回环直连更可靠
            r = httpx.get(f"http://{host}:{p}/api/status",
                          timeout=1.5, trust_env=False)
            # 401 = 口令拦截,同样证明是本服务在线
            return r.status_code in (200, 401)
        except Exception:
            return False

    if _already_running(args.host, preferred):
        host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        url = f"http://{host}:{preferred}/"
        if args.no_browser:
            print(f"[提示] 微信助手已在运行:{url}(本次按 --no-browser 未打开浏览器)。")
        else:
            print(f"[提示] 微信助手已在运行:{url} —— 不重复启动,已为你打开控制台页面。")
            try:
                _open_page(url)
            except Exception:
                pass
        return 0

    # ---- 端口探测:被占用自动 +1 顺延;之后的日志/浏览器/uvicorn 全部用最终端口 ----
    try:
        port = resolve_port(args.host, preferred)
    except RuntimeError as e:
        print(f"[错误] {e}")
        return 1
    if port != preferred:
        print(f"[提示] 端口 {preferred} 已被其他程序占用,自动改用空闲端口 {port}。")
    _clean_old_launcher_logs()

    # ---- 事件总线:引擎事件(on_event 回调)与 WebUI 共用一条总线 ----
    bus = EventBus(buffer_size=max(100, int(cfg.logging.ws_buffer)))

    # ---- 装配引擎(MonitorEngine;只构造不启动) ----
    try:
        from wxbot.server.main import _build_default_engine, create_app
        engine = _build_default_engine(Path(args.config), bus=bus)
    except RuntimeError as e:
        print(f"[错误] {e}")
        return 1
    except Exception as e:
        print(f"[错误] 引擎装配失败: {type(e).__name__}: {e}")
        return 1

    app = create_app(engine=engine, config_path=args.config, bus=bus)

    if not args.no_browser:
        _open_browser_later(args.host, port)

    print(f"[启动] WebUI: http://{args.host}:{port}/   (Ctrl+C 退出)")
    try:
        import uvicorn
        uvicorn.run(app, host=args.host, port=port, log_level="info")
    except OSError as e:
        # 理论上 resolve_port 已避开占用;此处兜底极端竞态(探测后瞬间被抢)。
        print(f"[错误] 服务启动失败: {e}")
        print(f"       端口 {port} 可能刚被其他程序抢占,可重试本命令或换端口:"
              f"python run.py --port {port + 1}")
        return 1
    return 0


def main() -> int:
    args = _parse_args()  # argparse 自身的 SystemExit(--help / 参数错)直接透传
    try:
        code = _launch(args)
    except KeyboardInterrupt:
        print("\n[退出] 已手动停止。")
        return 0
    except SystemExit as e:
        # uvicorn 0.54 绑定失败等场景在内部 sys.exit(STARTUP_FAILURE),不走 Exception 分支
        code = e.code if isinstance(e.code, int) else 1
        if code:
            print(f"[错误] 服务异常退出(退出码 {code}),常见原因是端口被占用或权限不足。")
    except Exception as e:
        print(f"[错误] 启动失败:{type(e).__name__}: {e}")
        print("       如反复出现,请把本窗口完整截图反馈。")
        code = 1
    if code:
        _hold_console(30)  # 双击场景:保持窗口,看清错误再退出
    return code


if __name__ == "__main__":
    sys.exit(main())
