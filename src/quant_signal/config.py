from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]


class NotifySettings(BaseModel):
    dedup_hours: int = 4
    hourly_limit: int = 10


class EnrichmentSettings(BaseModel):
    enabled: bool = False
    uzi_run_py: str = ""
    python_exe: str = "python"
    depth: str = "lite"
    timeout_seconds: int = 120
    max_tickers: int = 8


class Settings(BaseModel):
    data_source: Literal["yfinance", "alpaca"] = "yfinance"
    db_dir: str = "data"
    universe: list[str]
    watchlist: list[str]
    strategies: dict[str, dict[str, float | int]]
    momentum_group_top_n: dict[str, int] = {}   # 币种 -> 独立名额，如 {"HKD": 1, "KRW": 1}
    international_tickers: dict[str, str] = {}   # ticker -> 币种，固定走 yfinance
    notify: NotifySettings = NotifySettings()
    enrichment: EnrichmentSettings = EnrichmentSettings()
    # 凭证来自 .env，不出现在 yaml
    alpaca_key: str = ""
    alpaca_secret: str = ""
    feishu_webhook: str = ""

    @property
    def db_path(self) -> Path:
        p = REPO_ROOT / self.db_dir
        p.mkdir(parents=True, exist_ok=True)
        return p


def load_settings(path: Path | None = None) -> Settings:
    cfg_path = path or REPO_ROOT / "config" / "settings.yaml"
    load_dotenv(REPO_ROOT / "config" / ".env")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    raw["alpaca_key"] = os.environ.get("ALPACA_KEY", "")
    raw["alpaca_secret"] = os.environ.get("ALPACA_SECRET", "")
    raw["feishu_webhook"] = os.environ.get("FEISHU_WEBHOOK", "")
    return Settings(**raw)
