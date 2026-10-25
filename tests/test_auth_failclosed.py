# -*- coding: utf-8 -*-
"""口令 fail-closed 加固验收(2026-10-24 第二轮审查修复)。

背景:审查发现——服务运行期 config.json 缺失或损坏(非法 JSON)时,口令被
"读成空",认证中间件误判为"未启用认证",放行全部 /api/* 与 /ws/logs
(fail-open;配合常开隧道即公网裸奔且无告警)。修复前实测:坏 JSON 下无口令
GET /api/status=200、错口令=200。

修复(wxbot/server/main.py):create_app 装配时快照口令,之后现读配置每得到
有效口令即更新快照("最近一次成功读取的口令");任何读不到有效口令的状态
(文件缺失 / 非法 JSON / 口令被清空)一律回退该快照并按异常期去重告警
(fail-closed);启动时即无口令(明确未启用认证)才维持原免认证行为
(test_server 等既有测试契约不变)。

六项验收(进程内 TestClient,零微信交互、零发送):
 ① 对照组(正常配置):无口令/错口令 401(中文),对口令 200,静态页匿名 200;
 ② 运行期坏 JSON:无口令/错口令仍 401、最近口令仍 200;WS 无口令被拒
    (4401,收不到任何日志帧);
 ③ 告警与去重:损坏期总线恰好一条 ERROR 安全告警(连发多请求不重复),
    恢复后出现解除提示,再次损坏会重新告警;
 ④ 运行期改口令后文件被删:按最近口令校验(新口令 200、旧口令 401);
 ⑤ 运行期口令被清空(文件仍可读):按最近口令校验,WARNING 告警;
 ⑥ 启动即无口令(既有契约):/api/status 匿名 200 不变。

运行:python tests/test_auth_failclosed.py
末行打印 AUTH_FAILCLOSED_PASS 并 exit 0 = 通过;任一必检项失败 exit 1。
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass
warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from wxbot.config import AppConfig  # noqa: E402
from wxbot.server.events import EventBus  # noqa: E402
from wxbot.server.main import create_app  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
PWD1, PWD2, PWD3 = "correct-pass-123", "rotated-pass-456", "final-pass-789"

# 告警文案的稳定子串(与 main.py _resolve_password 保持一致)
CORRUPT_ALERT = "config.json 缺失或损坏"
RECOVERED_NOTE = "已恢复可读"
EMPTIED_ALERT = "web.password 为空"


class SkipTest(Exception):
    pass


def check(idx: str, desc: str, fn):
    print(f"\n[{idx}] {desc}")
    try:
        detail = fn() or ""
        print(f"    => PASS {detail}")
        RESULTS.append((idx, True, detail))
        return True
    except SkipTest as e:
        print(f"    => SKIP: {e}")
        RESULTS.append((idx, True, f"SKIP: {e}"))
        return True
    except Exception as e:
        print(f"    => FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()
        RESULTS.append((idx, False, f"{type(e).__name__}: {e}"))
        return False


class StubEngine:
    """最小引擎桩:create_app 只需 start/stop/status/send_now 存在。"""

    def start(self):
        pass

    def stop(self):
        pass

    def status(self):
        return {"running": False}

    def send_now(self, contact: str, text: str):
        return {"ok": True, "dry_run": True}


def write_config(path: Path, password: str) -> None:
    """写一份合法 config.json(默认配置 + 指定口令;空串=清空口令)。"""
    raw = AppConfig().to_dict()
    raw["web"]["password"] = password
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


class Harness:
    """一个带口令配置 + 独立总线的进程内应用;可随时破坏/恢复 config.json。"""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wxbot_auth_fc_")
        self.cfg = Path(self.tmp.name) / "config.json"
        write_config(self.cfg, PWD1)
        self.bus = EventBus(buffer_size=500)
        self.app = create_app(engine=StubEngine(), config_path=self.cfg,
                              window_finder=lambda: None, bus=self.bus,
                              enable_logging_bridge=False)
        self.client = TestClient(self.app)

    def __enter__(self):
        self.client.__enter__()
        return self

    def __exit__(self, *exc):
        return self.client.__exit__(*exc)

    def corrupt(self):        # 模拟手改写坏:非法 JSON
        self.cfg.write_text('{"web": {"password": ', encoding="utf-8")

    def delete(self):
        self.cfg.unlink()

    def restore(self, password: str):
        write_config(self.cfg, password)

    def alerts(self, needle: str) -> list[dict]:
        return [e for e in self.bus.recent() if needle in str(e.get("text", ""))]

    def codes(self, token: str | None) -> int:
        headers = {"X-Access-Token": token} if token is not None else {}
        return self.client.get("/api/status", headers=headers).status_code


def test1_baseline():
    """对照组:配置正常时无/错口令 401(中文)、对口令 200、静态页匿名。"""
    with Harness() as h:
        assert h.codes(None) == 401, f"无口令应 401,实际 {h.codes(None)}"
        r = h.client.get("/api/status")
        body = r.json()
        assert body.get("ok") is False and "口令" in str(body.get("error", "")), \
            f"401 应带中文口令错误: {r.text[:160]}"
        assert h.codes("wrong-token") == 401, "错口令应 401"
        assert h.codes(PWD1) == 200, "对口令应 200"
        assert h.client.get("/").status_code == 200, "静态页应匿名可访问"
        assert h.alerts(CORRUPT_ALERT) == [], "正常配置不应有损坏告警"
    return f"无/错口令401(中文)·对口令200·静态页匿名200(口令 {PWD1})"


def test2_corrupt_json_failclosed():
    """运行期坏 JSON:无/错口令仍 401,最近口令仍 200;WS 无口令 4401 无日志帧。"""
    with Harness() as h:
        assert h.codes(PWD1) == 200
        h.corrupt()
        assert h.codes(None) == 401, \
            f"坏 JSON 下无口令应 401(fail-open!),实际 {h.codes(None)}"
        assert h.codes("wrong-token") == 401, "坏 JSON 下错口令应 401"
        assert h.codes(PWD1) == 200, "坏 JSON 下正确口令应仍 200(按快照校验)"
        # WS:无口令必须被拒(4401),且收不到任何日志帧
        try:
            with h.client.websocket_connect("/ws/logs") as ws:
                msg = ws.receive()
        except WebSocketDisconnect as e:  # 某些 starlette 版本在 receive 抛出
            msg = {"type": "websocket.disconnect", "code": e.code}
        mtype = msg.get("type")
        if mtype == "websocket.send":
            raise AssertionError(f"坏 JSON 下无口令 WS 竟收到日志帧: {str(msg)[:120]}")
        code = msg.get("code")
        assert code == 4401, f"WS 应以 4401 关闭,实际 type={mtype} code={code}"
    return "坏 JSON:无/错口令401·最近口令200·WS 无口令 4401 零日志帧"


def test3_alert_once_and_recovery():
    """告警按异常期去重:损坏期恰 1 条 ERROR;恢复出解除提示;再损坏再告警。"""
    with Harness() as h:
        h.corrupt()
        assert h.codes(None) == 401
        for _ in range(5):                 # 连续请求不得重复告警
            h.codes(None)
        first = h.alerts(CORRUPT_ALERT)
        assert len(first) == 1, f"损坏期应恰好 1 条告警,实际 {len(first)}"
        assert str(first[0].get("level", "")).upper() == "ERROR", \
            f"损坏告警应为 ERROR: {first[0]}"
        # 恢复(口令换成 PWD2)→ 解除提示;旧口令失效、新口令生效
        h.restore(PWD2)
        assert h.codes(PWD2) == 200 and h.codes(PWD1) == 401, "恢复后应按新口令校验"
        assert len(h.alerts(RECOVERED_NOTE)) == 1, "恢复应出现一次解除提示"
        # 再次损坏 → 重新告警(去重按异常期,不是全生命周期一次)
        h.corrupt()
        assert h.codes(None) == 401
        assert len(h.alerts(CORRUPT_ALERT)) == 2, "再次损坏应重新告警"
    return "损坏期告警x1(去重)·恢复解除提示·再损坏再告警"


def test4_deleted_after_rotation():
    """运行期改口令后文件被删:按「最近一次成功读取的口令」校验。"""
    with Harness() as h:
        h.restore(PWD2)                    # 正常改口令 PWD1 → PWD2(现读生效)
        assert h.codes(PWD2) == 200
        h.delete()
        assert h.codes(None) == 401, "文件被删后无口令应 401"
        assert h.codes(PWD1) == 401, "文件被删后旧口令应 401"
        assert h.codes(PWD2) == 200, "文件被删后最近口令应仍 200"
    return "删除 config.json:无口令401·旧口令401·最近口令200"


def test5_emptied_password():
    """运行期口令被清空(文件仍可读):按最近口令校验 + WARNING 告警。"""
    with Harness() as h:
        write_config(h.cfg, "")            # 可读但 web.password 为空
        assert h.codes(None) == 401, \
            f"口令被清空后无口令应 401(fail-open!),实际 {h.codes(None)}"
        assert h.codes(PWD1) == 200, "口令被清空后最近口令应仍 200"
        got = h.alerts(EMPTIED_ALERT)
        assert len(got) == 1, f"应有一条口令被清空告警,实际 {len(got)}"
        assert str(got[0].get("level", "")).upper() == "WARNING", \
            f"清空告警应为 WARNING: {got[0]}"
        # 重新配置合法口令 → 回到 normal(新口令生效)
        h.restore(PWD3)
        assert h.codes(PWD3) == 200 and h.codes(PWD1) == 401
    return "清空口令:无口令401·最近口令200·WARNING告警·恢复后新口令生效"


def test6_no_password_contract():
    """既有契约:启动即无口令(测试用临时配置)时保持匿名可用,不受加固影响。"""
    with tempfile.TemporaryDirectory(prefix="wxbot_auth_fc_np_") as d:
        bus = EventBus(buffer_size=100)
        app = create_app(engine=StubEngine(), config_path=Path(d) / "config.json",
                         window_finder=lambda: None, bus=bus,
                         enable_logging_bridge=False)
        with TestClient(app) as c:
            assert c.get("/api/status").status_code == 200, \
                "启动即无口令的既有测试场景应保持匿名 200"
            assert c.get("/api/config").status_code == 200
            assert bus.recent() == [] or not [
                e for e in bus.recent() if CORRUPT_ALERT in str(e.get("text", ""))
            ], "无口令场景不应触发损坏告警"
    return "启动即无口令:/api/* 匿名 200(test_server 既有契约不变)"


def main() -> int:
    check("1", "对照组:正常配置 无/错口令401(中文)·对口令200·静态页匿名", test1_baseline)
    check("2", "运行期坏 JSON:无/错口令401·最近口令200·WS 4401 零日志帧", test2_corrupt_json_failclosed)
    check("3", "告警去重与恢复:损坏期x1 ERROR·解除提示·再损坏再告警", test3_alert_once_and_recovery)
    check("4", "改口令后文件被删:按最近口令校验(新200/旧401)", test4_deleted_after_rotation)
    check("5", "口令被清空(文件可读):按最近口令校验·WARNING告警", test5_emptied_password)
    check("6", "启动即无口令:既有匿名契约不变(无告警)", test6_no_password_contract)

    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "-" * 62)
    for idx, ok, detail in RESULTS:
        print(f"  [{idx}] {'PASS' if ok else 'FAIL'}  {detail}")
    print("-" * 62)
    if failed:
        print(f"AUTH_FAILCLOSED_TEST_FAIL(失败 {len(failed)} 项)")
        return 1
    print("AUTH_FAILCLOSED_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
