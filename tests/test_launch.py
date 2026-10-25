# -*- coding: utf-8 -*-
"""启动器(run.py)自测:端口顺延 + 浏览器最终 URL。纯本地,零微信交互。

运行方式:python tests/test_launch.py(不依赖 pytest,与其他测试同风格)。
末行打印 LAUNCH_TEST_PASS 并 exit 0;任一断言失败打印 [失败] 并 exit 1。

覆盖:
[1] resolve_port:preferred 空闲 → 原样返回;
[2] 占住 preferred → 顺延到下一个空闲端口,且跳过的端口当时确实都被占用;
[3] preferred / preferred+1 连续占用 → 顺延到 preferred+2(确定性构造);
[4] 连续 max_tries 个端口全占用 → RuntimeError(探测耗尽分支);
[5] monkeypatch run._open_page → 真实走 _open_browser_later 的后台线程,
    断言最终打开的 URL 含实际监听端口(而非 config 里的 8765)。
    2026-10-25:弹页改经 _open_page(默认浏览器 --new-window 前台置中,
    webbrowser.open 仅作兜底),测试同步改为捕获 _open_page。

红线:不触碰微信窗口、不发送任何消息、不开真实浏览器(_open_page 被替换为捕获函数)。
"""
from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

import run  # noqa: E402  被测对象:项目根 run.py(导入无副作用,uvicorn 等在 _launch 内才加载)

HOST = "127.0.0.1"


def _listen_free() -> tuple[socket.socket, int]:
    """在 127.0.0.1 上占住一个系统分配的空闲端口,返回 (socket, 端口号)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((HOST, 0))
    s.listen(1)
    return s, s.getsockname()[1]


def _find_consecutive_free(n: int) -> int:
    """在非临时端口区段(20000~40000)找 n 个连续空闲端口的起点,保证测试确定性。"""
    for cand in range(20000, 40000):
        if all(run._port_free(HOST, cand + k) for k in range(n)):
            return cand
    raise AssertionError("20000~40000 内找不到连续空闲端口,测试环境异常")


def main() -> int:
    guards: list[socket.socket] = []
    try:
        # [1] preferred 空闲 → 原样返回
        base = _find_consecutive_free(4)  # base..base+3 此刻全部空闲,后面复用
        got = run.resolve_port(HOST, base)
        assert got == base, f"[1] 空闲端口被改动: {base} -> {got}"
        print(f"[1] PASS 空闲端口 {base} 原样返回")

        # [2] 占住 preferred → 顺延,且顺延到的确实是第一个空闲端口
        s2, p2 = _listen_free()
        guards.append(s2)
        got = run.resolve_port(HOST, p2)
        assert got != p2, f"[2] 被占端口 {p2} 未顺延"
        for busy in range(p2, got):
            assert not run._port_free(HOST, busy), f"[2] 顺延跳过了空闲端口 {busy}"
        print(f"[2] PASS 被占端口 {p2} 顺延到 {got}")

        # [3] preferred 与 preferred+1 连续占用 → 顺延到 preferred+2
        s3a = socket.socket()
        s3a.bind((HOST, base + 1))
        s3a.listen(1)
        s3b = socket.socket()
        s3b.bind((HOST, base + 2))
        s3b.listen(1)
        guards += [s3a, s3b]
        got = run.resolve_port(HOST, base + 1)
        assert got == base + 3, f"[3] 连续占用顺延错误: 期望 {base + 3}, 实际 {got}"
        print(f"[3] PASS 连续占用 {base + 1}/{base + 2} 顺延到 {base + 3}")

        # [4] 连续 max_tries 个端口全占用 → RuntimeError
        s4a = socket.socket()
        s4a.bind((HOST, base + 5))
        s4a.listen(1)
        s4b = socket.socket()
        s4b.bind((HOST, base + 6))
        s4b.listen(1)
        guards += [s4a, s4b]
        try:
            run.resolve_port(HOST, base + 5, max_tries=2)
        except RuntimeError as e:
            print(f"[4] PASS 连续 {base + 5}/{base + 6} 占用且 max_tries=2 → RuntimeError: {e}")
        else:
            raise AssertionError("[4] 探测耗尽未抛 RuntimeError")

        # [5] 最终打开的浏览器 URL 必须含实际监听端口(而非 config 的 8765)
        srv, real_port = _listen_free()  # 模拟服务已在 real_port 就绪
        guards.append(srv)
        captured: list[str] = []
        opened = threading.Event()

        def fake_open_page(url: str) -> None:
            captured.append(url)
            opened.set()

        with mock.patch.object(run, "_open_page", fake_open_page):
            run._open_browser_later(HOST, real_port)  # 真实函数,仅替换 _open_page
            assert opened.wait(20), "[5] 20 秒内未捕获到 _open_page 调用"
        url = captured[0]
        assert f":{real_port}" in url, f"[5] URL 未含实际端口 {real_port}: {url}"
        assert url == f"http://{HOST}:{real_port}/", f"[5] URL 不符合预期: {url}"
        print(f"[5] PASS 浏览器最终 URL = {url}(含实际端口 {real_port},经 _open_page 前台弹窗)")

        print("LAUNCH_TEST_PASS")
        return 0
    finally:
        for g in guards:
            try:
                g.close()
            except OSError:
                pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"[失败] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[失败] 未预期异常 {type(e).__name__}: {e}")
        sys.exit(1)
