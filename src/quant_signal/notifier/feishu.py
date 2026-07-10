from __future__ import annotations

import time

import httpx
import structlog

from quant_signal.config import REPO_ROOT, Settings, load_settings
from quant_signal.notifier.base import Card, CardKind, ConsoleNotifier, Notifier

log = structlog.get_logger()
_BACKOFF: list[float] = [0.5, 1.0, 2.0]
_HEADER_COLOR = {CardKind.SIGNAL: "blue", CardKind.REPORT: "turquoise", CardKind.ALERT: "red"}


def _to_feishu_payload(card: Card) -> dict[str, object]:
    contents = (
        [section.content_md for section in card.sections]
        if card.sections
        else [card.body_md]
    )
    elements: list[dict[str, object]] = []
    for index, content in enumerate(contents):
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": content}}
        )
        if index < len(contents) - 1:
            elements.append({"tag": "hr"})
    if card.url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看图表"},
                        "type": "primary",
                        "url": card.url,
                    }
                ],
            }
        )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": card.title},
                "template": _HEADER_COLOR[card.kind],
            },
            "elements": elements,
        },
    }


class FeishuNotifier:
    def __init__(self, webhook: str) -> None:
        self._webhook = webhook

    def send(self, card: Card) -> bool:
        payload = _to_feishu_payload(card)
        for i, backoff in enumerate(_BACKOFF, start=1):
            try:
                resp = httpx.post(self._webhook, json=payload, timeout=10.0)
                body = resp.json()
                if resp.status_code == 200 and body.get("code") == 0:
                    return True
                log.warning("feishu.reject", attempt=i, status=resp.status_code, body=body)
            except httpx.HTTPError as e:
                log.warning("feishu.error", attempt=i, error=str(e))
            time.sleep(backoff)
        log.error("feishu.giveup", title=card.title)
        return False


def get_notifier(settings: Settings) -> Notifier:
    if settings.feishu_webhook:
        return FeishuNotifier(settings.feishu_webhook)
    return ConsoleNotifier(jsonl_path=REPO_ROOT / "logs" / "signals.jsonl")


def main() -> None:
    """M1 验收：发三种测试卡片。无 webhook 时输出到 Console。"""
    import argparse
    from datetime import datetime, timezone

    from quant_signal.logging_setup import setup_logging
    from quant_signal.notifier.cards import alert_card, report_card, signal_card
    from quant_signal.strategies.base import Direction, Signal

    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", required=True)
    parser.parse_args()

    n = get_notifier(load_settings())
    demo = Signal(
        ticker="SPY",
        direction=Direction.BUY,
        price=520.5,
        reason="测试信号：60日动量 +12.3%，排名第1",
        strategy_id="momentum_rotation",
        ts=datetime.now(timezone.utc),
        suggested_weight=0.33,
    )
    results = [
        n.send(signal_card(demo)),
        n.send(report_card("📋 盘前早报（测试）", "| 标的 | 方向 |\n|---|---|\n| SPY | BUY |")),
        n.send(alert_card("心跳失败（测试）", "连续 2 次心跳失败，请检查进程")),
    ]
    print(f"发送结果: {results}")


if __name__ == "__main__":
    main()
