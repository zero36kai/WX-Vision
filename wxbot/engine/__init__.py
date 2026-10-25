# -*- coding: utf-8 -*-
"""监控引擎(wxbot.engine):状态去重(state)/ 安全闸门(safety)/ 监控主循环(monitor)。

红线:引擎默认 dry_run=true,只记录发送意图;真实发送需 engine.dry_run=false
且 config.allow_real_send=true 双开关,且最终必经 flows.send_message 双闸门。
"""
from wxbot.engine.state import (  # noqa: F401
    ContactContextStore,
    FingerprintCache,
    SeenSet,
    fingerprint,
    fingerprint_of,
    is_own,
    normalize_text,
)
from wxbot.engine.safety import (  # noqa: F401
    RateLimiter,
    SafetyGate,
    SafetyRejected,
)
from wxbot.engine.monitor import MonitorEngine  # noqa: F401

__all__ = [
    "ContactContextStore", "FingerprintCache", "SeenSet", "fingerprint",
    "fingerprint_of", "is_own", "normalize_text",
    "RateLimiter", "SafetyGate", "SafetyRejected",
    "MonitorEngine",
]
