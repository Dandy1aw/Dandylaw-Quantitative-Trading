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


class TrendGateSettings(BaseModel):
    enabled: bool = False
    ma_days: int = 200
    mom_days: int = 252
    buffer: float = 0.03
    # 回测(阶段B)显示"仅200线+防御"最优，绝对动量/相对强弱过度保守，默认关闭
    use_mom: bool = False
    benchmark: str = "SPY"
    defensive: list[str] = ["BIL", "TLT", "GLD"]
    # 卡片展示的"参考卖出价"用 ATR 吊灯移动止损（贴近价格），与内部200线闸门分工
    chandelier_lookback: int = 22
    chandelier_atr: int = 14
    chandelier_mult: float = 3.0


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
    momentum_default_group_top_n: dict[str, int] = {}   # 默认组内 ETF/STOCK 各自名额
    asset_type: dict[str, str] = {}   # ticker -> "ETF"|"STOCK"，仅用于默认组内部再分
    international_tickers: dict[str, str] = {}   # ticker -> 币种，固定走 yfinance
    notify: NotifySettings = NotifySettings()
    enrichment: EnrichmentSettings = EnrichmentSettings()
    trend_gate: TrendGateSettings = TrendGateSettings()
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
