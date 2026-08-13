from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class CardKind(str, Enum):
    SIGNAL = "signal"
    REPORT = "report"
    ALERT = "alert"


@dataclass(frozen=True)
class CardSection:
    content_md: str


@dataclass(frozen=True)
class Card:
    kind: CardKind
    title: str
    body_md: str
    url: str | None = None
    sections: tuple[CardSection, ...] = ()
    image_key: str | None = None
    message_uuid: str | None = None


class Notifier(Protocol):
    def send(self, card: Card) -> bool: ...


def card_to_dict(card: Card) -> dict[str, object]:
    return {
        "kind": card.kind.value,
        "title": card.title,
        "body_md": card.body_md,
        "url": card.url,
        "sections": [section.content_md for section in card.sections],
        "image_key": card.image_key,
        "message_uuid": card.message_uuid,
    }


def card_from_dict(payload: Mapping[str, Any]) -> Card:
    raw_sections = payload.get("sections") or []
    return Card(
        kind=CardKind(str(payload["kind"])),
        title=str(payload["title"]),
        body_md=str(payload["body_md"]),
        url=str(payload["url"]) if payload.get("url") else None,
        sections=tuple(CardSection(str(content)) for content in raw_sections),
        image_key=(
            str(payload["image_key"]) if payload.get("image_key") else None
        ),
        message_uuid=(
            str(payload["message_uuid"]) if payload.get("message_uuid") else None
        ),
    )


class ConsoleNotifier:
    def __init__(self, jsonl_path: Path | None = None) -> None:
        self._jsonl_path = jsonl_path

    def send(self, card: Card) -> bool:
        line = f"\n{'=' * 60}\n[{card.kind.value.upper()}] {card.title}\n{card.body_md}"
        if card.url:
            line += f"\n🔗 {card.url}"
        print(line + f"\n{'=' * 60}")
        if self._jsonl_path is not None:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(),
                **card_to_dict(card),
            }
            with self._jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
