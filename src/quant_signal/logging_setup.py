from __future__ import annotations

import logging
import re
import sys
from typing import Any, MutableMapping

import structlog

# 只遮 token 部分, 保留 host 便于排障
_WEBHOOK_RE = re.compile(r"(https://open\.feishu\.cn/open-apis/bot/v\d+/hook/)[\w-]+")
_APCA_RE = re.compile(r"(APCA-API-(?:KEY-ID|SECRET-KEY)\s*[:=]\s*)\S+")


def _redact_text(text: str) -> str:
    text = _WEBHOOK_RE.sub(r"\1***", text)
    return _APCA_RE.sub(r"\1***", text)


def redact_secrets(
    logger: object, method_name: str | None, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: 日志中的 webhook token 与 Alpaca 凭据一律脱敏。"""
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = _redact_text(value)
    return event_dict


def setup_logging() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    # httpx 在 INFO 级会打印完整请求 URL(含飞书 webhook token), 必须压到 WARNING
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            redact_secrets,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
