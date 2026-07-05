from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class CardKind(str, Enum):
    SIGNAL = "signal"
    REPORT = "report"
    ALERT = "alert"


@dataclass(frozen=True)
class Card:
    kind: CardKind
    title: str
    body_md: str
    url: str | None = None


class Notifier(Protocol):
    def send(self, card: Card) -> bool: ...


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
                "kind": card.kind.value,
                "title": card.title,
                "body_md": card.body_md,
                "url": card.url,
            }
            with self._jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
