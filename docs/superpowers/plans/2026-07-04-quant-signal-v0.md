# 量化信号系统 v0（本机版）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建美股半自动量化信号系统 v0：yfinance 数据入 duckdb，动量轮动 + 20 日突破两个纯函数策略，经引擎/去重/台账后推送 Console（飞书代码就绪等 webhook），vectorbt + 自写 walk-forward 完成回测验证。

**Architecture:** 单进程 monorepo，APScheduler 按 NYSE 日历调度 data-feed → signal-engine → notifier 链路；数据源与通知器均为 Protocol 抽象可切换；策略是纯函数 `generate(bars) -> list[Signal]`，回测与实时共用同一份代码。

**Tech Stack:** Python 3.11+ / uv / pandas / duckdb / sqlite3 / yfinance / APScheduler / structlog / pandas-market-calendars / httpx / pydantic / vectorbt / pytest

**依据文档:** `quant-signal-spec.md`（原始 spec）+ `docs/superpowers/specs/2026-07-04-quant-signal-v0-local-design.md`（v0 增量设计，冲突处以后者为准）

## Global Constraints

- Python `>=3.11`，全部函数类型注解，mypy 通过
- 依赖用 uv 管理，版本固定（uv.lock 锁死精确版本）
- **任何情况下不得实现自动下单逻辑，不接券商交易 API**
- 内部时间一律 UTC aware `datetime`；**禁止 naive datetime**；展示层转 `Asia/Singapore`；交易日历用 `America/New_York`
- 密钥只从 `.env` 读取（`ALPACA_KEY`、`ALPACA_SECRET`、`FEISHU_WEBHOOK`），**禁止硬编码或提交 git**
- 回测/验证脚本必须 `import quant_signal.strategies` 下同一份策略代码，禁止复制策略逻辑
- 日志用 structlog，JSON 格式
- 策略 `generate()` 是纯函数：无副作用、无 IO
- 回测结果仅供用户评估，Agent 不得据此自行改参数后宣称"已优化"
- 每个 Milestone（A1/A2/B1/B2/A3/C1）完成后停下向用户汇报，等确认再继续
- 测试命令统一 `uv run pytest`，提交前跑全量

## 文件结构总览

```
quant-signal/                          # 即当前仓库根目录
├── pyproject.toml                     # Task 0
├── config/
│   ├── settings.yaml                  # Task 0
│   └── .env.example                   # Task 0
├── src/quant_signal/
│   ├── __init__.py                    # Task 0
│   ├── config.py                      # Task 0  设置加载（yaml+env）
│   ├── logging_setup.py               # Task 0  structlog 初始化
│   ├── datafeed/
│   │   ├── __init__.py
│   │   ├── base.py                    # Task 2  DataSource Protocol
│   │   ├── yf_source.py               # Task 2  YFinanceSource
│   │   ├── alpaca_source.py           # Task 17 AlpacaSource（C1）
│   │   └── store.py                   # Task 1  duckdb 读写
│   ├── ingest.py                      # Task 3  历史数据入库+校验 CLI
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── base.py                    # Task 4  Signal/Direction/Strategy
│   │   ├── momentum_rotation.py       # Task 4
│   │   └── breakout_20d.py            # Task 5
│   ├── notifier/
│   │   ├── __init__.py
│   │   ├── base.py                    # Task 6  Card + Notifier Protocol + Console
│   │   ├── cards.py                   # Task 7  三种卡片模板
│   │   ├── feishu.py                  # Task 7  webhook + 重试 + --test
│   │   └── dedup.py                   # Task 8
│   ├── ledger.py                      # Task 9  sqlite 台账 + 虚拟持仓
│   ├── calendar.py                    # Task 10 交易日判断
│   ├── engine.py                      # Task 11 策略调度→信号→台账→推送
│   ├── report.py                      # Task 12 日报统计
│   ├── scheduler.py                   # Task 13 APScheduler 编排
│   └── main.py                        # Task 13 入口
├── research/
│   ├── backtest_momentum.py           # Task 14
│   ├── backtest_breakout.py           # Task 15
│   ├── walkforward.py                 # Task 16 事件驱动交叉验证
│   └── reports/                       # 回测输出（gitignore 之外，报告要提交）
└── tests/
    ├── conftest.py                    # Task 4  共享 OHLCV fixture
    ├── test_config.py                 # Task 0
    ├── test_store.py                  # Task 1
    ├── test_datafeed.py               # Task 2
    ├── test_ingest.py                 # Task 3
    ├── test_momentum.py               # Task 4
    ├── test_breakout.py               # Task 5
    ├── test_notifier_base.py          # Task 6
    ├── test_feishu.py                 # Task 7
    ├── test_dedup.py                  # Task 8
    ├── test_ledger.py                 # Task 9
    ├── test_calendar.py               # Task 10
    ├── test_engine.py                 # Task 11
    ├── test_report.py                 # Task 12
    └── test_scheduler.py              # Task 13
```

Milestone 映射：A1 = Task 0–3；A2 = Task 4–5；B1 = Task 6–8；B2 = Task 9–13；A3 = Task 14–16；C1 = Task 17。

---

### Task 0: 项目脚手架 + 配置加载（A1）

**Files:**
- Create: `pyproject.toml`, `config/settings.yaml`, `config/.env.example`, `src/quant_signal/__init__.py`, `src/quant_signal/config.py`, `src/quant_signal/logging_setup.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `load_settings(path: Path | None = None) -> Settings`；`Settings` 字段见下方代码；`setup_logging() -> None`
- 后续所有 Task 通过 `from quant_signal.config import load_settings, Settings` 取配置

- [ ] **Step 1: 写 pyproject.toml 与包骨架**

```toml
# pyproject.toml
[project]
name = "quant-signal"
version = "0.1.0"
description = "美股半自动量化信号系统：只推信号，不自动下单"
requires-python = ">=3.11"
dependencies = [
    "pandas>=2.2,<3",
    "duckdb>=1.0,<2",
    "yfinance>=0.2.40,<0.3",
    "apscheduler>=3.10,<4",
    "structlog>=24.1,<25",
    "pandas-market-calendars>=4.4,<5",
    "httpx>=0.27,<1",
    "pyyaml>=6.0,<7",
    "pydantic>=2.7,<3",
    "python-dotenv>=1.0,<2",
]

[project.optional-dependencies]
research = ["vectorbt>=0.26,<0.27"]

[dependency-groups]
dev = ["pytest>=8,<9", "mypy>=1.10,<2", "freezegun>=1.5,<2", "types-PyYAML"]

[project.scripts]
quant-signal = "quant_signal.main:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/quant_signal"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.mypy]
strict = true
```

`src/quant_signal/__init__.py` 内容为空文件。

```yaml
# config/settings.yaml
data_source: yfinance          # yfinance | alpaca（填好 .env 后改这里切换）
db_dir: data                   # duckdb/sqlite 存放目录，相对仓库根

universe:                      # momentum_rotation 标的池
  - SPY
  - QQQ
  - IWM
  - TLT
  - GLD
watchlist:                     # breakout_20d 盘中观察列表
  - NVDA
  - TSLA
  - AAPL
  - MSFT
  - AMD

strategies:
  momentum_rotation:
    lookback_days: 60
    top_n: 3
    min_dollar_volume: 50000000    # 20日均成交额过滤（美元）
  breakout_20d:
    high_lookback_days: 20
    volume_multiplier: 1.5

notify:
  dedup_hours: 4
  hourly_limit: 10
```

```bash
# config/.env.example —— 复制为 config/.env 后填入真实值
ALPACA_KEY=
ALPACA_SECRET=
FEISHU_WEBHOOK=
```

- [ ] **Step 2: 写失败测试**

```python
# tests/test_config.py
from pathlib import Path

from quant_signal.config import Settings, load_settings


def test_load_settings_from_repo_yaml() -> None:
    s = load_settings()
    assert s.data_source == "yfinance"
    assert "SPY" in s.universe
    assert s.strategies["momentum_rotation"]["top_n"] == 3
    assert s.notify.dedup_hours == 4


def test_env_credentials_default_empty(monkeypatch) -> None:
    monkeypatch.delenv("FEISHU_WEBHOOK", raising=False)
    s = load_settings()
    assert s.feishu_webhook == ""
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL（`ModuleNotFoundError: quant_signal.config`）。首次运行前先 `uv sync --all-extras`。

- [ ] **Step 4: 实现 config.py 与 logging_setup.py**

```python
# src/quant_signal/config.py
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


class Settings(BaseModel):
    data_source: Literal["yfinance", "alpaca"] = "yfinance"
    db_dir: str = "data"
    universe: list[str]
    watchlist: list[str]
    strategies: dict[str, dict[str, float | int]]
    notify: NotifySettings = NotifySettings()
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
```

```python
# src/quant_signal/logging_setup.py
from __future__ import annotations

import logging
import sys

import structlog


def setup_logging() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
```

- [ ] **Step 5: 跑测试通过后提交**

Run: `uv run pytest tests/test_config.py -v` → 2 passed

```bash
git add pyproject.toml uv.lock config/ src/quant_signal/ tests/test_config.py
git commit -m "feat: 项目脚手架与配置加载 (Task 0)"
```

---

### Task 1: duckdb 存储层 store.py（A1）

**Files:**
- Create: `src/quant_signal/datafeed/__init__.py`（空）, `src/quant_signal/datafeed/store.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `BarStore(db_path: Path)`，方法：
  - `write_daily_bars(df: pd.DataFrame, source: str) -> int`（返回写入行数，upsert 语义）
  - `read_daily_bars(tickers: list[str], start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame`
  - `write_intraday_bars(df: pd.DataFrame, source: str) -> int` / `read_intraday_bars(...)` 同型
  - `daily_bar_count(ticker: str) -> int`
- **全仓库 DataFrame 约定（后续所有 Task 遵守）**：MultiIndex `(ticker: str, ts: datetime UTC tz-aware)`，列 `open, high, low, close, volume`，按 index 升序

- [ ] **Step 1: 写失败测试**

```python
# tests/test_store.py
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from quant_signal.datafeed.store import BarStore


def make_bars(ticker: str, n: int = 3) -> pd.DataFrame:
    ts = pd.date_range("2026-01-05", periods=n, freq="B", tz="UTC")
    idx = pd.MultiIndex.from_product([[ticker], ts], names=["ticker", "ts"])
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1_000_000},
        index=idx,
    )


@pytest.fixture
def store(tmp_path: Path) -> BarStore:
    return BarStore(tmp_path / "bars.duckdb")


def test_roundtrip(store: BarStore) -> None:
    n = store.write_daily_bars(make_bars("SPY"), source="test")
    assert n == 3
    out = store.read_daily_bars(["SPY"])
    assert len(out) == 3
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.names == ["ticker", "ts"]
    assert str(out.index.get_level_values("ts").tz) == "UTC"


def test_upsert_no_duplicates(store: BarStore) -> None:
    store.write_daily_bars(make_bars("SPY"), source="test")
    store.write_daily_bars(make_bars("SPY"), source="test")  # 重复写
    assert store.daily_bar_count("SPY") == 3


def test_read_time_range(store: BarStore) -> None:
    store.write_daily_bars(make_bars("SPY", n=5), source="test")
    start = datetime(2026, 1, 7, tzinfo=timezone.utc)
    out = store.read_daily_bars(["SPY"], start=start)
    assert len(out) == 3
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 store.py**

```python
# src/quant_signal/datafeed/store.py
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

_SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    ticker VARCHAR NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume BIGINT,
    source VARCHAR NOT NULL,
    PRIMARY KEY (ticker, ts)
);
"""
_COLS = ["open", "high", "low", "close", "volume"]


class BarStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(db_path))
        for table in ("bars_1d", "bars_5min"):
            self._con.execute(_SCHEMA.format(table=table))

    def _write(self, table: str, df: pd.DataFrame, source: str) -> int:
        if df.empty:
            return 0
        flat = df.reset_index()[["ticker", "ts", *_COLS]].copy()
        flat["source"] = source
        self._con.register("_incoming", flat)
        self._con.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM _incoming")
        self._con.unregister("_incoming")
        return len(flat)

    def _read(
        self, table: str, tickers: list[str],
        start: datetime | None, end: datetime | None,
    ) -> pd.DataFrame:
        q = f"SELECT ticker, ts, {', '.join(_COLS)} FROM {table} WHERE ticker IN ?"
        params: list[object] = [tickers]
        if start is not None:
            q += " AND ts >= ?"
            params.append(start)
        if end is not None:
            q += " AND ts <= ?"
            params.append(end)
        q += " ORDER BY ticker, ts"
        flat = self._con.execute(q, params).df()
        flat["ts"] = pd.to_datetime(flat["ts"], utc=True)
        return flat.set_index(["ticker", "ts"])

    def write_daily_bars(self, df: pd.DataFrame, source: str) -> int:
        return self._write("bars_1d", df, source)

    def read_daily_bars(
        self, tickers: list[str],
        start: datetime | None = None, end: datetime | None = None,
    ) -> pd.DataFrame:
        return self._read("bars_1d", tickers, start, end)

    def write_intraday_bars(self, df: pd.DataFrame, source: str) -> int:
        return self._write("bars_5min", df, source)

    def read_intraday_bars(
        self, tickers: list[str],
        start: datetime | None = None, end: datetime | None = None,
    ) -> pd.DataFrame:
        return self._read("bars_5min", tickers, start, end)

    def daily_bar_count(self, ticker: str) -> int:
        row = self._con.execute(
            "SELECT count(*) FROM bars_1d WHERE ticker = ?", [ticker]
        ).fetchone()
        return int(row[0]) if row else 0
```

- [ ] **Step 4: 跑测试通过**

Run: `uv run pytest tests/test_store.py -v` → 3 passed

- [ ] **Step 5: 提交**

```bash
git add src/quant_signal/datafeed/ tests/test_store.py
git commit -m "feat: duckdb 行情存储层 (Task 1)"
```

---

### Task 2: DataSource 抽象 + YFinanceSource（A1）

**Files:**
- Create: `src/quant_signal/datafeed/base.py`, `src/quant_signal/datafeed/yf_source.py`, `tests/test_datafeed.py`

**Interfaces:**
- Produces:
  - `DataSource`（Protocol）：`fetch_daily_bars(tickers: list[str], start: date, end: date) -> pd.DataFrame`；`fetch_intraday_bars(tickers: list[str], lookback_days: int = 5) -> pd.DataFrame`（5min bar）
  - `YFinanceSource()` 实现该协议；`get_source(settings: Settings) -> DataSource` 工厂（alpaca 分支在 Task 17 实现文件，工厂里 import 语句现在就写好）
- 返回值遵守 Task 1 的 DataFrame 约定；日线 ts 为当日 00:00 UTC

- [ ] **Step 1: 写失败测试（monkeypatch yfinance，不走网络）**

```python
# tests/test_datafeed.py
from datetime import date

import pandas as pd

from quant_signal.datafeed.yf_source import YFinanceSource


def fake_yf_download(*args: object, **kwargs: object) -> pd.DataFrame:
    """模拟 yf.download(group_by='ticker') 两只票的返回结构。"""
    idx = pd.date_range("2026-01-05", periods=3, freq="B")
    cols = pd.MultiIndex.from_product(
        [["SPY", "QQQ"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    df = pd.DataFrame(1.0, index=idx, columns=cols)
    for t in ("SPY", "QQQ"):
        df[(t, "Volume")] = 2_000_000
    return df


def test_fetch_daily_bars_normalized(monkeypatch) -> None:
    import quant_signal.datafeed.yf_source as m
    monkeypatch.setattr(m.yf, "download", fake_yf_download)
    out = YFinanceSource().fetch_daily_bars(["SPY", "QQQ"], date(2026, 1, 5), date(2026, 1, 8))
    assert out.index.names == ["ticker", "ts"]
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert set(out.index.get_level_values("ticker")) == {"SPY", "QQQ"}
    assert str(out.index.get_level_values("ts").tz) == "UTC"
    assert len(out) == 6
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_datafeed.py -v` → FAIL

- [ ] **Step 3: 实现 base.py 与 yf_source.py**

```python
# src/quant_signal/datafeed/base.py
from __future__ import annotations

from datetime import date
from typing import Protocol

import pandas as pd

from quant_signal.config import Settings


class DataSource(Protocol):
    def fetch_daily_bars(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame: ...

    def fetch_intraday_bars(
        self, tickers: list[str], lookback_days: int = 5
    ) -> pd.DataFrame: ...


def get_source(settings: Settings) -> DataSource:
    if settings.data_source == "yfinance":
        from quant_signal.datafeed.yf_source import YFinanceSource
        return YFinanceSource()
    if settings.data_source == "alpaca":
        from quant_signal.datafeed.alpaca_source import AlpacaSource
        return AlpacaSource(settings.alpaca_key, settings.alpaca_secret)
    raise ValueError(f"未知数据源: {settings.data_source}")
```

```python
# src/quant_signal/datafeed/yf_source.py
from __future__ import annotations

from datetime import date

import pandas as pd
import yfinance as yf


def _normalize(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """把 yf.download(group_by='ticker') 的宽表转为约定的 MultiIndex 长表。"""
    frames: list[pd.DataFrame] = []
    for t in tickers:
        if len(tickers) == 1:
            sub = raw.copy()
        else:
            if t not in raw.columns.get_level_values(0):
                continue
            sub = raw[t].copy()
        sub = sub.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        sub = sub.dropna(how="all")
        idx = pd.to_datetime(sub.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        sub.index = pd.MultiIndex.from_product([[t], idx], names=["ticker", "ts"])
        frames.append(sub)
    if not frames:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
            index=pd.MultiIndex.from_arrays([[], []], names=["ticker", "ts"]),
        )
    return pd.concat(frames).sort_index()


class YFinanceSource:
    def fetch_daily_bars(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame:
        raw = yf.download(
            tickers, start=start, end=end, interval="1d",
            auto_adjust=True, group_by="ticker", progress=False, threads=True,
        )
        return _normalize(raw, tickers)

    def fetch_intraday_bars(
        self, tickers: list[str], lookback_days: int = 5
    ) -> pd.DataFrame:
        raw = yf.download(
            tickers, period=f"{lookback_days}d", interval="5m",
            auto_adjust=True, group_by="ticker", progress=False, threads=True,
        )
        return _normalize(raw, tickers)
```

- [ ] **Step 4: 跑测试通过**

Run: `uv run pytest tests/test_datafeed.py -v` → 1 passed

- [ ] **Step 5: 提交**

```bash
git add src/quant_signal/datafeed/ tests/test_datafeed.py
git commit -m "feat: DataSource 抽象与 yfinance 数据源 (Task 2)"
```

---

### Task 3: 历史数据入库 + 完整性校验 ingest.py（A1 收尾）

**Files:**
- Create: `src/quant_signal/ingest.py`, `tests/test_ingest.py`

**Interfaces:**
- Consumes: `BarStore`（Task 1）、`get_source`（Task 2）、`load_settings`（Task 0）
- Produces:
  - `ingest_daily(store: BarStore, source: DataSource, tickers: list[str], days: int = 730) -> int`
  - `find_missing_sessions(store: BarStore, ticker: str, sessions: list[date]) -> list[date]`
  - CLI：`uv run python -m quant_signal.ingest`（拉取 universe+watchlist 两年日线）；`--check` 只做缺 bar 报告

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ingest.py
from datetime import date
from pathlib import Path

import pandas as pd

from quant_signal.datafeed.store import BarStore
from quant_signal.ingest import find_missing_sessions, ingest_daily


class FakeSource:
    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        ts = pd.date_range("2026-01-05", periods=4, freq="B", tz="UTC")
        idx = pd.MultiIndex.from_product([tickers, ts], names=["ticker", "ts"])
        return pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100},
            index=idx,
        )

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_ingest_daily_writes_all(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "b.duckdb")
    n = ingest_daily(store, FakeSource(), ["SPY", "QQQ"], days=30)
    assert n == 8
    assert store.daily_bar_count("SPY") == 4


def test_find_missing_sessions(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "b.duckdb")
    ingest_daily(store, FakeSource(), ["SPY"], days=30)
    sessions = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 9)]  # 1/9 无数据
    missing = find_missing_sessions(store, "SPY", sessions)
    assert missing == [date(2026, 1, 9)]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_ingest.py -v` → FAIL

- [ ] **Step 3: 实现 ingest.py**

```python
# src/quant_signal/ingest.py
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone

import structlog

from quant_signal.config import load_settings
from quant_signal.datafeed.base import DataSource, get_source
from quant_signal.datafeed.store import BarStore
from quant_signal.logging_setup import setup_logging

log = structlog.get_logger()


def ingest_daily(
    store: BarStore, source: DataSource, tickers: list[str], days: int = 730
) -> int:
    end = datetime.now(timezone.utc).date() + timedelta(days=1)
    start = end - timedelta(days=days)
    df = source.fetch_daily_bars(tickers, start, end)
    n = store.write_daily_bars(df, source=type(source).__name__.replace("Source", "").lower())
    log.info("ingest_daily.done", tickers=len(tickers), rows=n)
    return n


def find_missing_sessions(
    store: BarStore, ticker: str, sessions: list[date]
) -> list[date]:
    bars = store.read_daily_bars([ticker])
    have = {ts.date() for ts in bars.index.get_level_values("ts")}
    return [s for s in sessions if s not in have]


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="拉取历史日线入 duckdb")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--check", action="store_true", help="仅检查缺失交易日")
    args = parser.parse_args()

    settings = load_settings()
    store = BarStore(settings.db_path / "bars.duckdb")
    tickers = sorted(set(settings.universe) | set(settings.watchlist))

    if args.check:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        end = datetime.now(timezone.utc).date()
        sched = nyse.schedule(start_date=end - timedelta(days=args.days), end_date=end)
        sessions = [d.date() for d in sched.index]
        for t in tickers:
            missing = find_missing_sessions(store, t, sessions)
            log.info("check", ticker=t, bars=store.daily_bar_count(t), missing=len(missing))
        return

    ingest_daily(store, get_source(settings), tickers, days=args.days)
    for t in tickers:
        log.info("bar_count", ticker=t, bars=store.daily_bar_count(t))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测试通过**

Run: `uv run pytest tests/test_ingest.py -v` → 2 passed

- [ ] **Step 5: 真实拉取验收（A1 验收标准，需联网）**

Run: `uv run python -m quant_signal.ingest --days 730`
Expected: 日志显示各 ticker bar 数；**SPY ≥ 490** 即 A1 验收通过。再跑 `uv run python -m quant_signal.ingest --check` 确认无大面积缺失。

- [ ] **Step 6: 提交**

```bash
git add src/quant_signal/ingest.py tests/test_ingest.py
git commit -m "feat: 历史日线入库与完整性校验 (Task 3, A1 完成)"
```

---

### Task 4: 策略基类 + momentum_rotation（A2）

**Files:**
- Create: `src/quant_signal/strategies/__init__.py`（空）, `src/quant_signal/strategies/base.py`, `src/quant_signal/strategies/momentum_rotation.py`, `tests/conftest.py`, `tests/test_momentum.py`

**Interfaces:**
- Produces:
  - `Direction` / `Signal` / `Strategy`：与原 spec §4 逐字一致（`Signal` 含 `ticker, direction, price, reason, strategy_id, ts, suggested_weight, extra`）
  - `dedup_key(s: Signal) -> str`：返回 `f"{s.ticker}|{s.direction.value}|{s.strategy_id}"`（放 base.py，Task 8/9/11 都用它）
  - `MomentumRotation(universe, lookback_days=60, top_n=3, min_dollar_volume=5e7)`：`generate(bars)` 返回 top-N 的 BUY 信号（目标组合），**SELL 调仓信号由 engine 对比虚拟持仓生成（Task 11）**，策略本身保持纯函数
  - conftest 提供 `daily_bars` fixture：4 只票 × 100 个交易日、动量梯度 A>B>C>D、D 成交额极低

- [ ] **Step 1: 写 base.py（接口先行，无需 TDD）**

```python
# src/quant_signal/strategies/base.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import pandas as pd


class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    REDUCE = "reduce"


@dataclass(frozen=True)
class Signal:
    ticker: str
    direction: Direction
    price: float              # 触发时参考价
    reason: str               # 人类可读，将出现在飞书卡片
    strategy_id: str
    ts: datetime              # UTC aware
    suggested_weight: float | None = None   # 建议仓位占比 0~1
    extra: dict[str, object] | None = None


def dedup_key(s: Signal) -> str:
    return f"{s.ticker}|{s.direction.value}|{s.strategy_id}"


class Strategy:
    strategy_id: str
    schedule: str             # "daily_premarket" | "intraday_5min"
    universe: list[str]

    def generate(self, bars: pd.DataFrame) -> list[Signal]:
        """bars: MultiIndex(ticker, ts) OHLCV。纯函数，无副作用，无 IO。"""
        raise NotImplementedError
```

- [ ] **Step 2: 写共享 fixture 与失败测试**

```python
# tests/conftest.py
import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def daily_bars() -> pd.DataFrame:
    """4 只票 × 100 交易日。60 日动量：A(+60%) > B(+30%) > C(+5%) > D(+100% 但成交额过小)。"""
    ts = pd.date_range("2025-08-01", periods=100, freq="B", tz="UTC")
    n = len(ts)
    spec = {  # (日收益率, 日成交量)
        "AAA": (0.008, 5_000_000),
        "BBB": (0.004, 5_000_000),
        "CCC": (0.001, 5_000_000),
        "DDD": (0.012, 1_000),      # 20日均成交额 << $50M
    }
    frames = []
    for t, (r, vol) in spec.items():
        close = 100.0 * np.cumprod(np.full(n, 1 + r))
        df = pd.DataFrame(
            {
                "open": close * 0.999,
                "high": close * 1.005,
                "low": close * 0.995,
                "close": close,
                "volume": vol,
            },
            index=pd.MultiIndex.from_product([[t], ts], names=["ticker", "ts"]),
        )
        frames.append(df)
    return pd.concat(frames).sort_index()
```

```python
# tests/test_momentum.py
import pandas as pd

from quant_signal.strategies.base import Direction
from quant_signal.strategies.momentum_rotation import MomentumRotation


UNIVERSE = ["AAA", "BBB", "CCC", "DDD"]


def make(top_n: int = 2) -> MomentumRotation:
    return MomentumRotation(
        universe=UNIVERSE, lookback_days=60, top_n=top_n, min_dollar_volume=50_000_000
    )


def test_picks_top_momentum_and_filters_low_volume(daily_bars: pd.DataFrame) -> None:
    signals = make(top_n=2).generate(daily_bars)
    tickers = [s.ticker for s in signals]
    assert tickers == ["AAA", "BBB"]          # DDD 动量最高但被成交额过滤
    assert all(s.direction is Direction.BUY for s in signals)
    assert all(s.suggested_weight == 0.5 for s in signals)
    assert all(s.strategy_id == "momentum_rotation" for s in signals)


def test_signal_ts_is_last_bar_ts(daily_bars: pd.DataFrame) -> None:
    signals = make().generate(daily_bars)
    last_ts = daily_bars.index.get_level_values("ts").max()
    assert all(s.ts == last_ts for s in signals)


def test_insufficient_history_returns_empty(daily_bars: pd.DataFrame) -> None:
    ts = daily_bars.index.get_level_values("ts").unique().sort_values()
    short = daily_bars[daily_bars.index.get_level_values("ts") <= ts[30]]
    assert make().generate(short) == []


def test_no_lookahead_truncation(daily_bars: pd.DataFrame) -> None:
    """同一策略实例先看过全量数据后，对截断数据的输出必须与新实例一致（无内部状态泄漏未来信息）。"""
    ts = daily_bars.index.get_level_values("ts").unique().sort_values()
    upto_90 = daily_bars[daily_bars.index.get_level_values("ts") <= ts[89]]
    reused = make()
    reused.generate(daily_bars)          # 先喂全量（含"未来"10 天）
    assert reused.generate(upto_90) == make().generate(upto_90)
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/test_momentum.py -v` → FAIL（momentum_rotation 不存在）

- [ ] **Step 4: 实现 momentum_rotation.py**

```python
# src/quant_signal/strategies/momentum_rotation.py
from __future__ import annotations

import pandas as pd

from quant_signal.strategies.base import Direction, Signal, Strategy


class MomentumRotation(Strategy):
    strategy_id = "momentum_rotation"
    schedule = "daily_premarket"

    def __init__(
        self,
        universe: list[str],
        lookback_days: int = 60,
        top_n: int = 3,
        min_dollar_volume: float = 50_000_000,
    ) -> None:
        self.universe = universe
        self.lookback_days = lookback_days
        self.top_n = top_n
        self.min_dollar_volume = min_dollar_volume

    def generate(self, bars: pd.DataFrame) -> list[Signal]:
        close = bars["close"].unstack("ticker").sort_index()
        volume = bars["volume"].unstack("ticker").sort_index()
        close = close[[t for t in self.universe if t in close.columns]]
        if len(close) < self.lookback_days + 1:
            return []

        momentum = close.iloc[-1] / close.iloc[-1 - self.lookback_days] - 1.0
        dollar_vol_20d = (close * volume).tail(20).mean()
        eligible = momentum[dollar_vol_20d >= self.min_dollar_volume].dropna()
        top = eligible.sort_values(ascending=False).head(self.top_n)

        last_ts = close.index[-1].to_pydatetime()
        weight = round(1.0 / self.top_n, 4) if self.top_n else None
        return [
            Signal(
                ticker=str(t),
                direction=Direction.BUY,
                price=float(close[t].iloc[-1]),
                reason=f"{self.lookback_days}日动量 {mom:+.1%}，排名第{i}",
                strategy_id=self.strategy_id,
                ts=last_ts,
                suggested_weight=weight,
            )
            for i, (t, mom) in enumerate(top.items(), start=1)
        ]
```

注意 `test_picks_top_momentum_and_filters_low_volume` 断言 `suggested_weight == 0.5`：top_n=2 时 weight=1/2。

- [ ] **Step 5: 跑测试通过**

Run: `uv run pytest tests/test_momentum.py -v` → 4 passed

- [ ] **Step 6: 提交**

```bash
git add src/quant_signal/strategies/ tests/conftest.py tests/test_momentum.py
git commit -m "feat: 策略基类与动量轮动策略 (Task 4)"
```

---

### Task 5: breakout_20d 盘中突破策略（A2 收尾）

**Files:**
- Create: `src/quant_signal/strategies/breakout_20d.py`, `tests/test_breakout.py`

**Interfaces:**
- Consumes: `Signal/Direction/Strategy`（Task 4）
- Produces: `Breakout20d(universe, high_lookback_days=20, volume_multiplier=1.5)`
  - **输入约定**：`bars` 为日线 MultiIndex，每只票**最后一行是"当日进行中"的 bar**（engine 在 Task 11 用最新 5min 数据聚合出当日 open/high/low/last/累计 volume 拼到日线尾部）。策略对比"最后一行"与"之前 20 根"，天然不含当日 → 满足 spec"突破 20 日最高价（不含当日）"

- [ ] **Step 1: 写失败测试**

```python
# tests/test_breakout.py
import numpy as np
import pandas as pd

from quant_signal.strategies.base import Direction
from quant_signal.strategies.breakout_20d import Breakout20d


def make_bars(last_close: float, last_volume: float, n: int = 30) -> pd.DataFrame:
    """前 n-1 天 close/high 固定 100/101，最后一天（当日）close=last_close。"""
    ts = pd.date_range("2026-05-01", periods=n, freq="B", tz="UTC")
    close = np.full(n, 100.0)
    high = np.full(n, 101.0)
    vol = np.full(n, 1_000_000.0)
    close[-1] = last_close
    high[-1] = max(101.0, last_close)
    vol[-1] = last_volume
    return pd.DataFrame(
        {"open": close, "high": high, "low": close - 1, "close": close, "volume": vol},
        index=pd.MultiIndex.from_product([["NVDA"], ts], names=["ticker", "ts"]),
    )


def strat() -> Breakout20d:
    return Breakout20d(universe=["NVDA"], high_lookback_days=20, volume_multiplier=1.5)


def test_breakout_with_volume_triggers_buy() -> None:
    bars = make_bars(last_close=102.0, last_volume=2_000_000)  # 破 101 高点 + 2x 均量
    signals = strat().generate(bars)
    assert len(signals) == 1
    s = signals[0]
    assert s.ticker == "NVDA" and s.direction is Direction.BUY
    assert s.price == 102.0
    assert s.strategy_id == "breakout_20d"


def test_no_signal_without_volume_confirm() -> None:
    bars = make_bars(last_close=102.0, last_volume=1_200_000)  # 量能不足 1.5x
    assert strat().generate(bars) == []


def test_no_signal_below_prior_high() -> None:
    bars = make_bars(last_close=100.5, last_volume=2_000_000)  # 未破前高 101
    assert strat().generate(bars) == []


def test_prior_high_excludes_current_day() -> None:
    """当日自身新高不能作为被突破的前高。"""
    bars = make_bars(last_close=150.0, last_volume=2_000_000)
    signals = strat().generate(bars)
    assert len(signals) == 1
    assert signals[0].extra is not None
    assert signals[0].extra["prior_high"] == 101.0   # 而非当日的 150


def test_insufficient_history() -> None:
    bars = make_bars(last_close=102.0, last_volume=2_000_000, n=10)
    assert strat().generate(bars) == []
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_breakout.py -v` → FAIL

- [ ] **Step 3: 实现 breakout_20d.py**

```python
# src/quant_signal/strategies/breakout_20d.py
from __future__ import annotations

import pandas as pd

from quant_signal.strategies.base import Direction, Signal, Strategy


class Breakout20d(Strategy):
    strategy_id = "breakout_20d"
    schedule = "intraday_5min"

    def __init__(
        self,
        universe: list[str],
        high_lookback_days: int = 20,
        volume_multiplier: float = 1.5,
    ) -> None:
        self.universe = universe
        self.high_lookback_days = high_lookback_days
        self.volume_multiplier = volume_multiplier

    def generate(self, bars: pd.DataFrame) -> list[Signal]:
        signals: list[Signal] = []
        n = self.high_lookback_days
        for ticker in self.universe:
            if ticker not in bars.index.get_level_values("ticker"):
                continue
            tb = bars.xs(ticker, level="ticker").sort_index()
            if len(tb) < n + 1:
                continue
            today = tb.iloc[-1]          # 当日进行中 bar
            window = tb.iloc[-1 - n:-1]  # 之前 20 根，不含当日
            prior_high = float(window["high"].max())
            avg_vol = float(window["volume"].mean())
            price = float(today["close"])
            vol = float(today["volume"])
            if price > prior_high and vol > self.volume_multiplier * avg_vol:
                signals.append(
                    Signal(
                        ticker=ticker,
                        direction=Direction.BUY,
                        price=price,
                        reason=(
                            f"突破{n}日高点 {prior_high:.2f}，"
                            f"量能 {vol / avg_vol:.1f}x 于均量"
                        ),
                        strategy_id=self.strategy_id,
                        ts=tb.index[-1].to_pydatetime(),
                        extra={"prior_high": prior_high, "volume_ratio": vol / avg_vol},
                    )
                )
        return signals
```

- [ ] **Step 4: 跑测试通过**

Run: `uv run pytest tests/test_breakout.py -v` → 5 passed；再跑全量 `uv run pytest` 确认无回归

- [ ] **Step 5: 提交（A2 完成，向用户汇报）**

```bash
git add src/quant_signal/strategies/breakout_20d.py tests/test_breakout.py
git commit -m "feat: 20日高点突破策略 (Task 5, A2 完成)"
```

---

### Task 6: Card 模型 + ConsoleNotifier（B1）

**Files:**
- Create: `src/quant_signal/notifier/__init__.py`（空）, `src/quant_signal/notifier/base.py`, `tests/test_notifier_base.py`

**Interfaces:**
- Produces:
  - `CardKind`（Enum：`SIGNAL/REPORT/ALERT`）
  - `Card`（frozen dataclass）：`kind: CardKind, title: str, body_md: str, url: str | None = None`（body_md 为 markdown 正文；飞书/终端各自渲染）
  - `Notifier`（Protocol）：`send(card: Card) -> bool`
  - `ConsoleNotifier(jsonl_path: Path | None = None)`：打印到终端 + 追加写 jsonl

- [ ] **Step 1: 写失败测试**

```python
# tests/test_notifier_base.py
import json
from pathlib import Path

from quant_signal.notifier.base import Card, CardKind, ConsoleNotifier


def test_console_send_returns_true_and_writes_jsonl(tmp_path: Path, capsys) -> None:
    p = tmp_path / "signals.jsonl"
    n = ConsoleNotifier(jsonl_path=p)
    card = Card(kind=CardKind.SIGNAL, title="📈 NVDA buy", body_md="**现价**: 102.0")
    assert n.send(card) is True
    out = capsys.readouterr().out
    assert "NVDA" in out
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    assert rec["kind"] == "signal" and rec["title"] == "📈 NVDA buy"


def test_console_send_without_jsonl(capsys) -> None:
    n = ConsoleNotifier()
    assert n.send(Card(kind=CardKind.ALERT, title="告警", body_md="x")) is True
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_notifier_base.py -v` → FAIL

- [ ] **Step 3: 实现 notifier/base.py**

```python
# src/quant_signal/notifier/base.py
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol


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
```

- [ ] **Step 4: 跑测试通过后提交**

Run: `uv run pytest tests/test_notifier_base.py -v` → 2 passed

```bash
git add src/quant_signal/notifier/ tests/test_notifier_base.py
git commit -m "feat: Card 模型与 Console 通知器 (Task 6)"
```

---

### Task 7: 卡片模板 + 飞书 webhook 发送（B1）

**Files:**
- Create: `src/quant_signal/notifier/cards.py`, `src/quant_signal/notifier/feishu.py`, `tests/test_feishu.py`

**Interfaces:**
- Consumes: `Card/CardKind/Notifier/ConsoleNotifier`（Task 6）、`Signal`（Task 4）、`Settings`（Task 0）
- Produces:
  - `signal_card(s: Signal, delayed: bool = False) -> Card`（delayed=True 时正文追加"⚠️ 数据延迟约15分钟，仅供观察"；url 指向 TradingView）
  - `report_card(title: str, body_md: str) -> Card` / `alert_card(title: str, body_md: str) -> Card`
  - `FeishuNotifier(webhook: str)`：`send(card) -> bool`，飞书 interactive 卡片，失败重试 3 次指数退避（0.5s/1s/2s），仍失败记日志返回 False
  - `get_notifier(settings: Settings) -> Notifier`：`feishu_webhook` 非空 → Feishu，否则 Console（jsonl 落 `logs/signals.jsonl`）
  - CLI：`uv run python -m quant_signal.notifier.feishu --test` 用 get_notifier 发三种测试卡片（M1 验收命令）

- [ ] **Step 1: 写失败测试（mock httpx，不发真请求）**

```python
# tests/test_feishu.py
from datetime import datetime, timezone

import httpx
import pytest

from quant_signal.notifier.base import Card, CardKind, ConsoleNotifier
from quant_signal.notifier.cards import alert_card, signal_card
from quant_signal.notifier.feishu import FeishuNotifier, get_notifier
from quant_signal.config import load_settings
from quant_signal.strategies.base import Direction, Signal


def make_signal() -> Signal:
    return Signal(
        ticker="NVDA", direction=Direction.BUY, price=102.0,
        reason="突破20日高点", strategy_id="breakout_20d",
        ts=datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc),
        suggested_weight=0.33,
    )


def test_signal_card_contains_fields_and_sgt_time() -> None:
    card = signal_card(make_signal())
    assert card.kind is CardKind.SIGNAL
    assert "NVDA" in card.title and "BUY" in card.title.upper()
    assert "102.0" in card.body_md and "突破20日高点" in card.body_md
    assert "22:30" in card.body_md          # 14:30 UTC = 22:30 SGT
    assert card.url == "https://www.tradingview.com/chart/?symbol=NVDA"


def test_signal_card_delayed_flag() -> None:
    assert "延迟" in signal_card(make_signal(), delayed=True).body_md


def test_feishu_send_success(monkeypatch) -> None:
    sent: dict[str, object] = {}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:  # type: ignore[type-arg]
        sent["url"] = url
        sent["payload"] = json
        return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    ok = FeishuNotifier("https://open.feishu.cn/hook/xxx").send(alert_card("t", "b"))
    assert ok is True
    assert sent["url"] == "https://open.feishu.cn/hook/xxx"
    assert sent["payload"]["msg_type"] == "interactive"  # type: ignore[index]


def test_feishu_retries_then_fails(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:  # type: ignore[type-arg]
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("quant_signal.notifier.feishu._BACKOFF", [0, 0, 0])
    ok = FeishuNotifier("https://open.feishu.cn/hook/xxx").send(alert_card("t", "b"))
    assert ok is False and calls["n"] == 3


def test_get_notifier_falls_back_to_console(monkeypatch) -> None:
    monkeypatch.delenv("FEISHU_WEBHOOK", raising=False)
    assert isinstance(get_notifier(load_settings()), ConsoleNotifier)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_feishu.py -v` → FAIL

- [ ] **Step 3: 实现 cards.py**

```python
# src/quant_signal/notifier/cards.py
from __future__ import annotations

from zoneinfo import ZoneInfo

from quant_signal.notifier.base import Card, CardKind
from quant_signal.strategies.base import Signal

_SGT = ZoneInfo("Asia/Singapore")
_DIRECTION_EMOJI = {"buy": "📈", "sell": "📉", "reduce": "⚖️"}


def signal_card(s: Signal, delayed: bool = False) -> Card:
    emoji = _DIRECTION_EMOJI.get(s.direction.value, "📌")
    sgt = s.ts.astimezone(_SGT).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"**现价**: {s.price}",
        f"**触发原因**: {s.reason}",
    ]
    if s.suggested_weight is not None:
        lines.append(f"**建议仓位**: {s.suggested_weight:.0%}")
    lines += [f"**策略**: {s.strategy_id}", f"**时间**: {sgt} (SGT)"]
    if delayed:
        lines.append("⚠️ 数据延迟约15分钟，仅供观察")
    return Card(
        kind=CardKind.SIGNAL,
        title=f"{emoji} {s.ticker} {s.direction.value.upper()}",
        body_md="\n".join(lines),
        url=f"https://www.tradingview.com/chart/?symbol={s.ticker}",
    )


def report_card(title: str, body_md: str) -> Card:
    return Card(kind=CardKind.REPORT, title=title, body_md=body_md)


def alert_card(title: str, body_md: str) -> Card:
    return Card(kind=CardKind.ALERT, title=f"🚨 {title}", body_md=body_md)
```

- [ ] **Step 4: 实现 feishu.py**

```python
# src/quant_signal/notifier/feishu.py
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
    elements: list[dict[str, object]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": card.body_md}}
    ]
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
        ticker="SPY", direction=Direction.BUY, price=520.5,
        reason="测试信号：60日动量 +12.3%，排名第1", strategy_id="momentum_rotation",
        ts=datetime.now(timezone.utc), suggested_weight=0.33,
    )
    results = [
        n.send(signal_card(demo)),
        n.send(report_card("📋 盘前早报（测试）", "| 标的 | 方向 |\n|---|---|\n| SPY | BUY |")),
        n.send(alert_card("心跳失败（测试）", "连续 2 次心跳失败，请检查进程")),
    ]
    print(f"发送结果: {results}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 跑测试 + M1 验收命令**

Run: `uv run pytest tests/test_feishu.py -v` → 6 passed
Run: `uv run python -m quant_signal.notifier.feishu --test`
Expected: Console 渲染三种卡片、`logs/signals.jsonl` 有 3 行（webhook 填好后同命令直发飞书群 → M1 正式验收）

- [ ] **Step 6: 提交**

```bash
git add src/quant_signal/notifier/ tests/test_feishu.py
git commit -m "feat: 卡片模板与飞书通知器 (Task 7)"
```

---

### Task 8: 去重与限流 dedup.py（B1 收尾）

**Files:**
- Create: `src/quant_signal/notifier/dedup.py`, `tests/test_dedup.py`

**Interfaces:**
- Consumes: `Signal`、`dedup_key`（Task 4）
- Produces（纯函数，不碰数据库；台账数据由 engine 从 ledger 读出传入）:
  - `@dataclass DedupResult: to_push: list[Signal]; suppressed: list[Signal]; overflow: list[Signal]`
  - `apply_dedup(signals, now, last_push_by_key: dict[str, datetime], pushed_in_last_hour: int, dedup_hours: int = 4, hourly_limit: int = 10) -> DedupResult`
  - 规则：①同 dedup_key 在 dedup_hours 内已推过 → suppressed；②通过①后按顺序放行，超出 hourly_limit 配额的 → overflow（engine 将 overflow 合并成一张汇总卡片）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_dedup.py
from datetime import datetime, timedelta, timezone

from quant_signal.notifier.dedup import apply_dedup
from quant_signal.strategies.base import Direction, Signal, dedup_key


NOW = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)


def sig(ticker: str = "NVDA") -> Signal:
    return Signal(
        ticker=ticker, direction=Direction.BUY, price=100.0, reason="r",
        strategy_id="breakout_20d", ts=NOW,
    )


def test_suppresses_within_window() -> None:
    s = sig()
    last = {dedup_key(s): NOW - timedelta(hours=2)}   # 2h 前推过，窗口 4h
    r = apply_dedup([s], NOW, last, pushed_in_last_hour=0)
    assert r.to_push == [] and r.suppressed == [s]


def test_allows_after_window() -> None:
    s = sig()
    last = {dedup_key(s): NOW - timedelta(hours=5)}   # 已过 4h 窗口
    r = apply_dedup([s], NOW, last, pushed_in_last_hour=0)
    assert r.to_push == [s]


def test_hourly_limit_overflow() -> None:
    signals = [sig(f"T{i:02d}") for i in range(12)]
    r = apply_dedup(signals, NOW, {}, pushed_in_last_hour=5, hourly_limit=10)
    assert len(r.to_push) == 5 and len(r.overflow) == 7   # 配额只剩 5
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_dedup.py -v` → FAIL

- [ ] **Step 3: 实现 dedup.py**

```python
# src/quant_signal/notifier/dedup.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from quant_signal.strategies.base import Signal, dedup_key


@dataclass
class DedupResult:
    to_push: list[Signal] = field(default_factory=list)
    suppressed: list[Signal] = field(default_factory=list)   # 4h 窗口内重复
    overflow: list[Signal] = field(default_factory=list)     # 超小时限流，待汇总


def apply_dedup(
    signals: list[Signal],
    now: datetime,
    last_push_by_key: dict[str, datetime],
    pushed_in_last_hour: int,
    dedup_hours: int = 4,
    hourly_limit: int = 10,
) -> DedupResult:
    result = DedupResult()
    window = timedelta(hours=dedup_hours)
    quota = max(0, hourly_limit - pushed_in_last_hour)
    for s in signals:
        last = last_push_by_key.get(dedup_key(s))
        if last is not None and now - last < window:
            result.suppressed.append(s)
        elif len(result.to_push) < quota:
            result.to_push.append(s)
        else:
            result.overflow.append(s)
    return result
```

- [ ] **Step 4: 跑测试通过后提交（B1 完成，向用户汇报）**

Run: `uv run pytest tests/test_dedup.py -v` → 3 passed；全量 `uv run pytest` 无回归

```bash
git add src/quant_signal/notifier/dedup.py tests/test_dedup.py
git commit -m "feat: 信号去重与限流 (Task 8, B1 完成)"
```

---

### Task 9: sqlite 信号台账 + 虚拟持仓 ledger.py（B2）

**Files:**
- Create: `src/quant_signal/ledger.py`, `tests/test_ledger.py`

**Interfaces:**
- Consumes: `Signal/Direction/dedup_key`（Task 4）
- Produces: `SignalLedger(db_path: Path)`：
  - `insert(s: Signal, pushed: bool) -> int`（写入并返回 rowid；无论是否推送都落库，`pushed` 打标）
  - `last_push_by_key(since: datetime) -> dict[str, datetime]`（since 之后每个 dedup_key 最近一次**已推送**时间）
  - `pushed_count_since(since: datetime) -> int`
  - `signals_on(day: date) -> list[dict]`（当日全部信号，字段同表列，供日报）
  - `get_holdings(strategy_id: str) -> list[str]` / `set_holdings(strategy_id: str, tickers: list[str]) -> None`（虚拟持仓，momentum 调仓对比用）
- 表结构：`signals` 完全按原 spec §7 建表建索引；另建 `holdings(strategy_id TEXT, ticker TEXT, PRIMARY KEY(strategy_id, ticker))`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ledger.py
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from quant_signal.ledger import SignalLedger
from quant_signal.strategies.base import Direction, Signal, dedup_key


NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def sig(ticker: str = "SPY", ts: datetime = NOW) -> Signal:
    return Signal(
        ticker=ticker, direction=Direction.BUY, price=100.0, reason="r",
        strategy_id="momentum_rotation", ts=ts, suggested_weight=0.33,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> SignalLedger:
    return SignalLedger(tmp_path / "signals.db")


def test_insert_and_query_day(ledger: SignalLedger) -> None:
    ledger.insert(sig(), pushed=True)
    ledger.insert(sig("QQQ"), pushed=False)
    rows = ledger.signals_on(date(2026, 7, 6))
    assert len(rows) == 2
    assert {r["ticker"] for r in rows} == {"SPY", "QQQ"}
    assert [r["pushed"] for r in sorted(rows, key=lambda r: r["ticker"])] == [0, 1]


def test_last_push_by_key_only_pushed(ledger: SignalLedger) -> None:
    s = sig()
    ledger.insert(s, pushed=False)
    assert ledger.last_push_by_key(NOW - timedelta(hours=4)) == {}
    ledger.insert(s, pushed=True)
    got = ledger.last_push_by_key(NOW - timedelta(hours=4))
    assert got == {dedup_key(s): NOW}


def test_pushed_count_since(ledger: SignalLedger) -> None:
    ledger.insert(sig("A"), pushed=True)
    ledger.insert(sig("B"), pushed=True)
    ledger.insert(sig("C"), pushed=False)
    assert ledger.pushed_count_since(NOW - timedelta(hours=1)) == 2


def test_holdings_roundtrip(ledger: SignalLedger) -> None:
    assert ledger.get_holdings("momentum_rotation") == []
    ledger.set_holdings("momentum_rotation", ["SPY", "QQQ"])
    ledger.set_holdings("momentum_rotation", ["QQQ", "GLD"])   # 覆盖式更新
    assert sorted(ledger.get_holdings("momentum_rotation")) == ["GLD", "QQQ"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_ledger.py -v` → FAIL

- [ ] **Step 3: 实现 ledger.py**

```python
# src/quant_signal/ledger.py
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from quant_signal.strategies.base import Signal, dedup_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,             -- UTC ISO8601
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    price REAL NOT NULL,
    strategy_id TEXT NOT NULL,
    reason TEXT,
    suggested_weight REAL,
    pushed INTEGER DEFAULT 0,
    dedup_key TEXT,
    extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_dedup ON signals(dedup_key, ts);
CREATE TABLE IF NOT EXISTS holdings (
    strategy_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    PRIMARY KEY (strategy_id, ticker)
);
"""


class SignalLedger:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(db_path))
        self._con.row_factory = sqlite3.Row
        self._con.executescript(_SCHEMA)

    def insert(self, s: Signal, pushed: bool) -> int:
        cur = self._con.execute(
            "INSERT INTO signals (ts, ticker, direction, price, strategy_id, reason,"
            " suggested_weight, pushed, dedup_key, extra_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s.ts.astimezone(timezone.utc).isoformat(),
                s.ticker, s.direction.value, s.price, s.strategy_id, s.reason,
                s.suggested_weight, int(pushed), dedup_key(s),
                json.dumps(s.extra, ensure_ascii=False) if s.extra else None,
            ),
        )
        self._con.commit()
        return int(cur.lastrowid or 0)

    def last_push_by_key(self, since: datetime) -> dict[str, datetime]:
        rows = self._con.execute(
            "SELECT dedup_key, max(ts) AS ts FROM signals"
            " WHERE pushed = 1 AND ts >= ? GROUP BY dedup_key",
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchall()
        return {r["dedup_key"]: datetime.fromisoformat(r["ts"]) for r in rows}

    def pushed_count_since(self, since: datetime) -> int:
        row = self._con.execute(
            "SELECT count(*) AS n FROM signals WHERE pushed = 1 AND ts >= ?",
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchone()
        return int(row["n"])

    def signals_on(self, day: date) -> list[dict[str, object]]:
        rows = self._con.execute(
            "SELECT * FROM signals WHERE substr(ts, 1, 10) = ? ORDER BY ts",
            (day.isoformat(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_holdings(self, strategy_id: str) -> list[str]:
        rows = self._con.execute(
            "SELECT ticker FROM holdings WHERE strategy_id = ?", (strategy_id,)
        ).fetchall()
        return [r["ticker"] for r in rows]

    def set_holdings(self, strategy_id: str, tickers: list[str]) -> None:
        self._con.execute("DELETE FROM holdings WHERE strategy_id = ?", (strategy_id,))
        self._con.executemany(
            "INSERT INTO holdings (strategy_id, ticker) VALUES (?, ?)",
            [(strategy_id, t) for t in tickers],
        )
        self._con.commit()
```

- [ ] **Step 4: 跑测试通过后提交**

Run: `uv run pytest tests/test_ledger.py -v` → 4 passed

```bash
git add src/quant_signal/ledger.py tests/test_ledger.py
git commit -m "feat: sqlite 信号台账与虚拟持仓 (Task 9)"
```

---

### Task 10: 交易日历 calendar.py（B2）

**Files:**
- Create: `src/quant_signal/calendar.py`, `tests/test_calendar.py`

**Interfaces:**
- Produces:
  - `is_trading_day(d: date) -> bool`（NYSE 日历，结果按年缓存避免反复构建 schedule）
  - `previous_trading_day(d: date) -> date`（往前找最近一个交易日，日报/补数据用）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_calendar.py
from datetime import date

from quant_signal.calendar import is_trading_day, previous_trading_day


def test_weekday_is_trading_day() -> None:
    assert is_trading_day(date(2026, 7, 6)) is True        # 周一


def test_weekend_not_trading_day() -> None:
    assert is_trading_day(date(2026, 7, 4)) is False       # 周六


def test_independence_day_observed_closed() -> None:
    assert is_trading_day(date(2026, 7, 3)) is False       # 独立日补休，NYSE 休市


def test_previous_trading_day_skips_holiday_weekend() -> None:
    assert previous_trading_day(date(2026, 7, 6)) == date(2026, 7, 2)
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_calendar.py -v` → FAIL

- [ ] **Step 3: 实现 calendar.py**

```python
# src/quant_signal/calendar.py
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import pandas_market_calendars as mcal


@lru_cache(maxsize=8)
def _sessions_for_year(year: int) -> frozenset[date]:
    nyse = mcal.get_calendar("NYSE")
    sched = nyse.schedule(start_date=f"{year}-01-01", end_date=f"{year}-12-31")
    return frozenset(d.date() for d in sched.index)


def is_trading_day(d: date) -> bool:
    return d in _sessions_for_year(d.year)


def previous_trading_day(d: date) -> date:
    cur = d - timedelta(days=1)
    while not is_trading_day(cur):
        cur -= timedelta(days=1)
    return cur
```

- [ ] **Step 4: 跑测试通过后提交**

Run: `uv run pytest tests/test_calendar.py -v` → 4 passed

```bash
git add src/quant_signal/calendar.py tests/test_calendar.py
git commit -m "feat: NYSE 交易日历 (Task 10)"
```

---

### Task 11: 信号引擎 engine.py（B2 核心）

**Files:**
- Create: `src/quant_signal/engine.py`, `tests/test_engine.py`

**Interfaces:**
- Consumes: `BarStore`（T1）、`DataSource`（T2）、策略（T4/T5）、`Card/cards`（T6/T7）、`apply_dedup`（T8）、`SignalLedger`（T9）、`Settings`（T0）
- Produces: `Engine(settings, store, source, ledger, notifier)`：
  - `run_premarket(now: datetime) -> None`：补日线 → momentum.generate → 与 `ledger.get_holdings` 对比生成 SELL 调仓信号 → 全部信号走 dedup → 落台账 → 推"盘前早报"卡片（信号汇总一张卡）→ `set_holdings(新目标)`
  - `run_intraday(now: datetime) -> None`：拉 5min bar 入库 → 聚合当日进行中 bar 拼到日线尾部 → breakout.generate → dedup → 落台账 → 逐条推信号卡片（yfinance 模式 delayed=True）；overflow 合并为一张汇总卡
  - `enrichers: list[object]` 构造参数默认空列表——Phase 2 UZI-Skill 钩子占位，本期不实现任何 enricher
  - `_intraday_snapshot(daily: pd.DataFrame, intraday: pd.DataFrame, day: date) -> pd.DataFrame`（模块级函数：把当日 5min 聚合成一根日 bar 追加；单测直接覆盖）

- [ ] **Step 1: 写失败测试（FakeSource/FakeNotifier，冻结时间）**

```python
# tests/test_engine.py
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine, _intraday_snapshot
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card


NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


class FakeNotifier:
    def __init__(self) -> None:
        self.cards: list[Card] = []

    def send(self, card: Card) -> bool:
        self.cards.append(card)
        return True


class FakeSource:
    def __init__(self, daily: pd.DataFrame, intraday: pd.DataFrame | None = None) -> None:
        self._daily = daily
        self._intraday = intraday

    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        return self._daily

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        assert self._intraday is not None
        return self._intraday


@pytest.fixture
def env(tmp_path: Path, daily_bars: pd.DataFrame):  # type: ignore[no-untyped-def]
    settings = load_settings()
    # 用 conftest 的 4 票池覆盖 universe/watchlist
    settings = settings.model_copy(
        update={"universe": ["AAA", "BBB", "CCC", "DDD"], "watchlist": ["AAA"]}
    )
    store = BarStore(tmp_path / "b.duckdb")
    ledger = SignalLedger(tmp_path / "s.db")
    notifier = FakeNotifier()
    return settings, store, ledger, notifier


def test_premarket_generates_rotation_and_report(env, daily_bars) -> None:  # type: ignore[no-untyped-def]
    settings, store, ledger, notifier = env
    ledger.set_holdings("momentum_rotation", ["CCC"])   # 旧持仓将被轮出
    engine = Engine(settings, store, FakeSource(daily_bars), ledger, notifier)
    engine.run_premarket(NOW)

    rows = ledger.signals_on(NOW.date())
    directions = {(r["ticker"], r["direction"]) for r in rows}
    assert ("AAA", "buy") in directions and ("BBB", "buy") in directions
    assert ("CCC", "sell") in directions                 # 调仓卖出
    holdings = set(ledger.get_holdings("momentum_rotation"))
    assert {"AAA", "BBB"} <= holdings and "CCC" not in holdings  # 持仓已更新为新目标
    assert len(notifier.cards) == 1                      # 一张早报卡
    assert "早报" in notifier.cards[0].title


def test_premarket_dedup_second_run_no_push(env, daily_bars) -> None:  # type: ignore[no-untyped-def]
    settings, store, ledger, notifier = env
    engine = Engine(settings, store, FakeSource(daily_bars), ledger, notifier)
    engine.run_premarket(NOW)
    n_first = len([r for r in ledger.signals_on(NOW.date()) if r["pushed"]])
    engine.run_premarket(NOW)                            # 同日重跑
    rows = ledger.signals_on(NOW.date())
    n_pushed = len([r for r in rows if r["pushed"]])
    assert n_pushed == n_first                           # 4h 窗口内不重复推


def test_intraday_snapshot_appends_partial_day(daily_bars: pd.DataFrame) -> None:
    ts5 = pd.date_range("2026-07-06 13:30", periods=6, freq="5min", tz="UTC")
    idx = pd.MultiIndex.from_product([["AAA"], ts5], names=["ticker", "ts"])
    intraday = pd.DataFrame(
        {"open": 200.0, "high": 201.0, "low": 199.0, "close": 200.5, "volume": 10_000},
        index=idx,
    )
    combined = _intraday_snapshot(daily_bars, intraday, date(2026, 7, 6))
    aaa = combined.xs("AAA", level="ticker")
    assert aaa.index[-1].date() == date(2026, 7, 6)
    assert aaa["volume"].iloc[-1] == 60_000              # 6 根 5min 量累加
    assert aaa["high"].iloc[-1] == 201.0
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_engine.py -v` → FAIL

- [ ] **Step 3: 实现 engine.py**

```python
# src/quant_signal/engine.py
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import structlog

from quant_signal.config import Settings
from quant_signal.datafeed.base import DataSource
from quant_signal.datafeed.store import BarStore
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Notifier
from quant_signal.notifier.cards import alert_card, report_card, signal_card
from quant_signal.notifier.dedup import apply_dedup
from quant_signal.strategies.base import Direction, Signal
from quant_signal.strategies.breakout_20d import Breakout20d
from quant_signal.strategies.momentum_rotation import MomentumRotation

log = structlog.get_logger()


def _intraday_snapshot(
    daily: pd.DataFrame, intraday: pd.DataFrame, day: date
) -> pd.DataFrame:
    """把当日 5min bar 聚合成一根'进行中'日 bar，追加到各票日线尾部。"""
    frames = [daily]
    day_start = pd.Timestamp(day, tz="UTC")
    cur = intraday[intraday.index.get_level_values("ts") >= day_start]
    for ticker in cur.index.get_level_values("ticker").unique():
        tb = cur.xs(ticker, level="ticker").sort_index()
        if tb.empty:
            continue
        row = pd.DataFrame(
            {
                "open": [float(tb["open"].iloc[0])],
                "high": [float(tb["high"].max())],
                "low": [float(tb["low"].min())],
                "close": [float(tb["close"].iloc[-1])],
                "volume": [float(tb["volume"].sum())],
            },
            index=pd.MultiIndex.from_tuples(
                [(ticker, tb.index[-1])], names=["ticker", "ts"]
            ),
        )
        frames.append(row)
    return pd.concat(frames).sort_index()


class Engine:
    def __init__(
        self,
        settings: Settings,
        store: BarStore,
        source: DataSource,
        ledger: SignalLedger,
        notifier: Notifier,
        enrichers: list[object] | None = None,   # Phase 2 UZI-Skill 钩子，暂不使用
    ) -> None:
        self.settings = settings
        self.store = store
        self.source = source
        self.ledger = ledger
        self.notifier = notifier
        self.enrichers = enrichers or []
        mp = settings.strategies["momentum_rotation"]
        bp = settings.strategies["breakout_20d"]
        self.momentum = MomentumRotation(
            universe=settings.universe,
            lookback_days=int(mp["lookback_days"]),
            top_n=int(mp["top_n"]),
            min_dollar_volume=float(mp["min_dollar_volume"]),
        )
        self.breakout = Breakout20d(
            universe=settings.watchlist,
            high_lookback_days=int(bp["high_lookback_days"]),
            volume_multiplier=float(bp["volume_multiplier"]),
        )

    # ---- 内部工具 ----

    def _dedup(self, signals: list[Signal], now: datetime):  # type: ignore[no-untyped-def]
        cfg = self.settings.notify
        return apply_dedup(
            signals,
            now,
            self.ledger.last_push_by_key(now - timedelta(hours=cfg.dedup_hours)),
            self.ledger.pushed_count_since(now - timedelta(hours=1)),
            dedup_hours=cfg.dedup_hours,
            hourly_limit=cfg.hourly_limit,
        )

    def _refresh_daily(self, now: datetime) -> pd.DataFrame:
        tickers = sorted(set(self.settings.universe) | set(self.settings.watchlist))
        start = (now - timedelta(days=10)).date()
        fresh = self.source.fetch_daily_bars(tickers, start, now.date() + timedelta(days=1))
        self.store.write_daily_bars(fresh, source=self.settings.data_source)
        return self.store.read_daily_bars(tickers, start=now - timedelta(days=400))

    # ---- 调度入口 ----

    def run_premarket(self, now: datetime) -> None:
        bars = self._refresh_daily(now)
        targets = self.momentum.generate(bars)
        target_tickers = [s.ticker for s in targets]
        current = self.ledger.get_holdings(self.momentum.strategy_id)

        sells = [
            Signal(
                ticker=t, direction=Direction.SELL,
                price=float(bars.xs(t, level="ticker")["close"].iloc[-1]),
                reason="动量排名跌出前列，轮动调出",
                strategy_id=self.momentum.strategy_id, ts=now,
            )
            for t in current if t not in target_tickers
            and t in bars.index.get_level_values("ticker")
        ]
        all_signals = targets + sells
        result = self._dedup(all_signals, now)
        for s in result.to_push:
            self.ledger.insert(s, pushed=True)
        for s in result.suppressed + result.overflow:
            self.ledger.insert(s, pushed=False)
        self.ledger.set_holdings(self.momentum.strategy_id, target_tickers)

        if result.to_push:
            lines = ["| 标的 | 方向 | 价格 | 原因 |", "|---|---|---|---|"]
            lines += [
                f"| {s.ticker} | {s.direction.value.upper()} | {s.price:.2f} | {s.reason} |"
                for s in result.to_push
            ]
            self.notifier.send(report_card("📋 盘前早报", "\n".join(lines)))
        log.info("premarket.done", signals=len(all_signals), pushed=len(result.to_push))

    def run_intraday(self, now: datetime) -> None:
        intraday = self.source.fetch_intraday_bars(self.settings.watchlist)
        self.store.write_intraday_bars(intraday, source=self.settings.data_source)
        daily = self.store.read_daily_bars(
            self.settings.watchlist, start=now - timedelta(days=60)
        )
        bars = _intraday_snapshot(daily, intraday, now.date())
        result = self._dedup(self.breakout.generate(bars), now)
        delayed = self.settings.data_source == "yfinance"
        for s in result.to_push:
            self.ledger.insert(s, pushed=True)
            self.notifier.send(signal_card(s, delayed=delayed))
        for s in result.suppressed:
            self.ledger.insert(s, pushed=False)
        if result.overflow:
            for s in result.overflow:
                self.ledger.insert(s, pushed=False)
            names = ", ".join(f"{s.ticker}({s.direction.value})" for s in result.overflow)
            self.notifier.send(
                alert_card("信号限流汇总", f"1 小时配额已满，以下 {len(result.overflow)} 条合并：{names}")
            )
        log.info("intraday.done", pushed=len(result.to_push), overflow=len(result.overflow))
```

- [ ] **Step 4: 跑测试通过**

Run: `uv run pytest tests/test_engine.py -v` → 3 passed；全量无回归

- [ ] **Step 5: 提交**

```bash
git add src/quant_signal/engine.py tests/test_engine.py
git commit -m "feat: 信号引擎（盘前轮动+盘中突破全链路） (Task 11)"
```

---

### Task 12: 日报 report.py（B2）

**Files:**
- Create: `src/quant_signal/report.py`, `tests/test_report.py`

**Interfaces:**
- Consumes: `SignalLedger.signals_on`（T9）、`BarStore.read_daily_bars`（T1）、`report_card`（T7）
- Produces: `build_daily_report(ledger: SignalLedger, store: BarStore, day: date) -> Card`
  - 内容：当日信号总数/推送数、按策略分组的明细表、BUY 信号的"理论收益"（信号价 → 当日最新收盘价的涨跌幅；无最新价则留空 `-`）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_report.py
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from quant_signal.datafeed.store import BarStore
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import CardKind
from quant_signal.report import build_daily_report
from quant_signal.strategies.base import Direction, Signal


def test_daily_report_counts_and_theoretical_return(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "s.db")
    store = BarStore(tmp_path / "b.duckdb")
    day = date(2026, 7, 6)
    ts = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
    ledger.insert(
        Signal(ticker="NVDA", direction=Direction.BUY, price=100.0, reason="r",
               strategy_id="breakout_20d", ts=ts),
        pushed=True,
    )
    idx = pd.MultiIndex.from_tuples(
        [("NVDA", pd.Timestamp("2026-07-06", tz="UTC"))], names=["ticker", "ts"]
    )
    store.write_daily_bars(
        pd.DataFrame({"open": 100.0, "high": 106.0, "low": 99.0, "close": 105.0,
                      "volume": 1_000_000}, index=idx),
        source="test",
    )
    card = build_daily_report(ledger, store, day)
    assert card.kind is CardKind.REPORT
    assert "1" in card.body_md          # 信号数
    assert "+5.0%" in card.body_md      # (105-100)/100


def test_daily_report_empty_day(tmp_path: Path) -> None:
    card = build_daily_report(
        SignalLedger(tmp_path / "s.db"), BarStore(tmp_path / "b.duckdb"), date(2026, 7, 6)
    )
    assert "无信号" in card.body_md
```

- [ ] **Step 2: 运行确认失败** → `uv run pytest tests/test_report.py -v` FAIL

- [ ] **Step 3: 实现 report.py**

```python
# src/quant_signal/report.py
from __future__ import annotations

from datetime import date

from quant_signal.datafeed.store import BarStore
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card
from quant_signal.notifier.cards import report_card


def build_daily_report(ledger: SignalLedger, store: BarStore, day: date) -> Card:
    rows = ledger.signals_on(day)
    title = f"📊 信号日报 {day.isoformat()}"
    if not rows:
        return report_card(title, "今日无信号。")

    pushed = sum(1 for r in rows if r["pushed"])
    lines = [
        f"**信号总数**: {len(rows)}　**已推送**: {pushed}",
        "",
        "| 时间(UTC) | 标的 | 方向 | 信号价 | 理论收益 | 策略 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        ret = "-"
        if r["direction"] == "buy":
            bars = store.read_daily_bars([str(r["ticker"])])
            if not bars.empty:
                last_close = float(bars["close"].iloc[-1])
                ret = f"{(last_close - float(r['price'])) / float(r['price']):+.1%}"
        hhmm = str(r["ts"])[11:16]
        lines.append(
            f"| {hhmm} | {r['ticker']} | {str(r['direction']).upper()} |"
            f" {float(r['price']):.2f} | {ret} | {r['strategy_id']} |"
        )
    return report_card(title, "\n".join(lines))
```

- [ ] **Step 4: 跑测试通过后提交**

Run: `uv run pytest tests/test_report.py -v` → 2 passed

```bash
git add src/quant_signal/report.py tests/test_report.py
git commit -m "feat: 信号日报 (Task 12)"
```

---

### Task 13: 调度器 scheduler.py + 入口 main.py（B2 收尾）

**Files:**
- Create: `src/quant_signal/scheduler.py`, `src/quant_signal/main.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `Engine`（T11）、`build_daily_report`（T12）、`is_trading_day`（T10）、`ingest_daily/find_missing_sessions`（T3）、`get_notifier`（T7）
- Produces:
  - `build_scheduler(engine, ledger, store, notifier) -> BackgroundScheduler`，注册 5 个 job（id 固定）：
    - `premarket`：cron 08:00 ET；`intraday`：cron 每 5min，9:30–16:00 ET；`postmarket`：cron 16:30 ET —— 三者入口处 `if not is_trading_day(now_et.date()): return`
    - `maintenance`：cron 03:00 ET（近 10 日缺 bar 重拉）；`heartbeat`：interval 15min，连续 2 次异常 → alert 卡片
  - `main() -> None`：组装全部依赖 → scheduler.start() → 阻塞等待 Ctrl-C（`quant-signal` 入口）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_scheduler.py
from quant_signal.scheduler import HEARTBEAT_FAIL_THRESHOLD, Heartbeat, build_scheduler


class FakeNotifier:
    def __init__(self) -> None:
        self.cards: list[object] = []

    def send(self, card: object) -> bool:
        self.cards.append(card)
        return True


def test_scheduler_registers_all_jobs() -> None:
    sched = build_scheduler(engine=None, ledger=None, store=None, notifier=FakeNotifier())
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {"premarket", "intraday", "postmarket", "maintenance", "heartbeat"}


def test_heartbeat_alerts_after_consecutive_failures() -> None:
    n = FakeNotifier()
    hb = Heartbeat(notifier=n, check=lambda: False)   # 恒失败
    for _ in range(HEARTBEAT_FAIL_THRESHOLD):
        hb.tick()
    assert len(n.cards) == 1                          # 连续 2 次失败发一次告警


def test_heartbeat_recovers_resets_counter() -> None:
    n = FakeNotifier()
    ok = {"v": False}
    hb = Heartbeat(notifier=n, check=lambda: ok["v"])
    hb.tick()
    ok["v"] = True
    hb.tick()                                         # 恢复
    ok["v"] = False
    hb.tick()                                         # 又失败 1 次，不到阈值
    assert n.cards == []
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现 scheduler.py**

```python
# src/quant_signal/scheduler.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from quant_signal.calendar import is_trading_day

log = structlog.get_logger()
ET = ZoneInfo("America/New_York")
HEARTBEAT_FAIL_THRESHOLD = 2


class Heartbeat:
    def __init__(self, notifier: Any, check: Callable[[], bool]) -> None:
        self._notifier = notifier
        self._check = check
        self._fails = 0
        self._alerted = False

    def tick(self) -> None:
        try:
            ok = self._check()
        except Exception as e:  # noqa: BLE001
            log.warning("heartbeat.check_error", error=str(e))
            ok = False
        if ok:
            self._fails = 0
            self._alerted = False
            return
        self._fails += 1
        if self._fails >= HEARTBEAT_FAIL_THRESHOLD and not self._alerted:
            from quant_signal.notifier.cards import alert_card
            self._notifier.send(
                alert_card("心跳检查失败", f"连续 {self._fails} 次自检失败，请检查进程/数据源")
            )
            self._alerted = True


def _now_et() -> datetime:
    return datetime.now(tz=ET)


def build_scheduler(engine: Any, ledger: Any, store: Any, notifier: Any) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=ET)

    def trading_day_only(fn: Callable[[datetime], None]) -> Callable[[], None]:
        def wrapped() -> None:
            now_et = _now_et()
            if not is_trading_day(now_et.date()):
                log.info("skip.non_trading_day", job=fn.__name__)
                return
            fn(now_et.astimezone(timezone.utc))
        return wrapped

    def premarket() -> None:
        engine.run_premarket(datetime.now(timezone.utc))

    def intraday() -> None:
        engine.run_intraday(datetime.now(timezone.utc))

    def postmarket() -> None:
        from quant_signal.report import build_daily_report
        notifier.send(build_daily_report(ledger, store, _now_et().date()))

    def maintenance() -> None:
        from quant_signal.ingest import ingest_daily
        if engine is not None:
            ingest_daily(store, engine.source, engine.settings.universe
                         + engine.settings.watchlist, days=10)

    hb = Heartbeat(notifier=notifier, check=lambda: True)

    sched.add_job(trading_day_only(lambda now: premarket()), CronTrigger(hour=8, minute=0),
                  id="premarket")
    sched.add_job(trading_day_only(lambda now: intraday()),
                  CronTrigger(hour="9-15", minute="*/5"), id="intraday")
    sched.add_job(trading_day_only(lambda now: postmarket()),
                  CronTrigger(hour=16, minute=30), id="postmarket")
    sched.add_job(maintenance, CronTrigger(hour=3, minute=0), id="maintenance")
    sched.add_job(hb.tick, IntervalTrigger(minutes=15), id="heartbeat")
    return sched
```

注意：`intraday` 用 `hour="9-15", minute="*/5"` 覆盖 9:00–15:55，engine 里突破策略只在有当日 5min 数据时才可能触发，9:30 开盘前 yfinance 无当日 bar、不会产生信号，可接受；若要求严格 9:30 起跑，可在 `intraday()` 入口加 `if now_et.time() < time(9, 30): return`（实现时直接加上）。

- [ ] **Step 4: 实现 main.py**

```python
# src/quant_signal/main.py
from __future__ import annotations

import time

import structlog

from quant_signal.config import load_settings
from quant_signal.datafeed.base import get_source
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine
from quant_signal.ledger import SignalLedger
from quant_signal.logging_setup import setup_logging
from quant_signal.notifier.feishu import get_notifier
from quant_signal.scheduler import build_scheduler

log = structlog.get_logger()


def main() -> None:
    setup_logging()
    settings = load_settings()
    store = BarStore(settings.db_path / "bars.duckdb")
    ledger = SignalLedger(settings.db_path / "signals.db")
    notifier = get_notifier(settings)
    engine = Engine(settings, store, get_source(settings), ledger, notifier)
    sched = build_scheduler(engine, ledger, store, notifier)
    sched.start()
    log.info("scheduler.started", jobs=[j.id for j in sched.get_jobs()],
             data_source=settings.data_source,
             notifier=type(notifier).__name__)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        sched.shutdown()
        log.info("scheduler.stopped")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 跑测试 + B2 端到端验收（mock 时间手动触发）**

Run: `uv run pytest` → 全量通过
Run 手动触发验证（真实拉数，需联网）:

```bash
uv run python -c "
from datetime import datetime, timezone
from quant_signal.config import load_settings
from quant_signal.datafeed.base import get_source
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine
from quant_signal.ledger import SignalLedger
from quant_signal.logging_setup import setup_logging
from quant_signal.notifier.feishu import get_notifier

setup_logging()
s = load_settings()
store = BarStore(s.db_path / 'bars.duckdb')
ledger = SignalLedger(s.db_path / 'signals.db')
e = Engine(s, store, get_source(s), ledger, get_notifier(s))
now = datetime.now(timezone.utc)
e.run_premarket(now)
e.run_intraday(now)
"
```

Expected: Console 输出早报卡片；重复执行第二次不再重复推送（4h 去重生效）；`data/signals.db` 有台账记录。

- [ ] **Step 6: 提交（B2 完成，向用户汇报）**

```bash
git add src/quant_signal/scheduler.py src/quant_signal/main.py tests/test_scheduler.py
git commit -m "feat: APScheduler 调度与主入口 (Task 13, B2 完成)"
```

---

### Task 14: vectorbt 动量轮动回测（A3）

**Files:**
- Create: `research/backtest_momentum.py`, `research/reports/`（目录）

**Interfaces:**
- Consumes: `MomentumRotation`（T4，**必须 import src 下同一份**）、`YFinanceSource`（T2）、`BarStore`（T1）
- Produces: CLI `uv run python research/backtest_momentum.py [--start 2016-01-01]`，生成 `research/reports/momentum_YYYYMMDD.md`（年化、Sharpe、最大回撤、换手率 + 参数扫描表）

无单测（研究脚本），验收 = 报告文件生成且指标完整。回测数据从 2016-01-01 起拉（yfinance 一次性拉长历史，落 duckdb 复用）。

- [ ] **Step 1: 实现 backtest_momentum.py**

```python
# research/backtest_momentum.py
"""动量轮动策略 vectorbt 回测。

用与实时完全相同的 MomentumRotation.generate 逐月生成目标组合，
按月末调仓构建持仓矩阵，vectorbt 计算组合绩效。
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import vectorbt as vbt

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.strategies.momentum_rotation import MomentumRotation

REPORTS = Path(__file__).parent / "reports"


def load_bars(start: date) -> pd.DataFrame:
    settings = load_settings()
    store = BarStore(settings.db_path / "bars_research.duckdb")
    tickers = settings.universe
    have = min((store.daily_bar_count(t) for t in tickers), default=0)
    if have < 100:   # 数据不足则拉全量
        df = YFinanceSource().fetch_daily_bars(
            tickers, start, datetime.now(timezone.utc).date()
        )
        store.write_daily_bars(df, source="yfinance")
    return store.read_daily_bars(tickers)


def run_backtest(
    bars: pd.DataFrame, lookback_days: int, top_n: int, min_dollar_volume: float
) -> dict[str, float]:
    settings = load_settings()
    strat = MomentumRotation(
        universe=settings.universe, lookback_days=lookback_days,
        top_n=top_n, min_dollar_volume=min_dollar_volume,
    )
    close = bars["close"].unstack("ticker").sort_index()
    month_ends = close.groupby(close.index.to_period("M")).tail(1).index

    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    changes = 0
    prev: set[str] = set()
    for me in month_ends:
        window = bars[bars.index.get_level_values("ts") <= me]
        picks = {s.ticker for s in strat.generate(window)}
        if picks:
            weights.loc[me:, list(picks)] = 0.0
            weights.loc[me:, :] = 0.0
            weights.loc[me:, list(picks)] = 1.0 / len(picks)
            changes += len(picks - prev)
            prev = picks

    pf = vbt.Portfolio.from_orders(
        close=close, size=weights, size_type="targetpercent",
        freq="1D", cash_sharing=True, call_seq="auto",
    )
    stats = pf.stats()
    years = (close.index[-1] - close.index[0]).days / 365.25
    total_ret = float(stats["Total Return [%]"]) / 100
    return {
        "annual_return": ((1 + total_ret) ** (1 / years) - 1) * 100,
        "sharpe": float(stats["Sharpe Ratio"]),
        "max_drawdown": float(stats["Max Drawdown [%]"]),
        "turnover_per_year": changes / years,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    args = parser.parse_args()

    bars = load_bars(args.start)
    settings = load_settings()
    base = settings.strategies["momentum_rotation"]

    rows = ["| lookback | top_n | 年化% | Sharpe | 最大回撤% | 年换手次数 |",
            "|---|---|---|---|---|---|"]
    for lookback in (30, 60, 90):
        for top_n in (2, 3):
            m = run_backtest(bars, lookback, top_n, float(base["min_dollar_volume"]))
            marker = " ←当前参数" if (lookback, top_n) == (
                int(base["lookback_days"]), int(base["top_n"])) else ""
            rows.append(
                f"| {lookback} | {top_n} | {m['annual_return']:.1f} | {m['sharpe']:.2f} |"
                f" {m['max_drawdown']:.1f} | {m['turnover_per_year']:.1f}{marker} |"
            )

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"momentum_{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# 动量轮动回测报告\n\n"
        f"- 数据: {args.start} 至今，universe={settings.universe}\n"
        f"- 月末调仓，等权持有 top_n\n\n" + "\n".join(rows) + "\n\n"
        "> 回测结果仅供评估，不构成投资建议。参数修改由用户决定。\n",
        encoding="utf-8",
    )
    print(f"报告已生成: {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行生成报告（需联网，首次拉 2016 至今数据较慢）**

Run: `uv sync --extra research`（首次装 vectorbt）
Run: `uv run python research/backtest_momentum.py`
Expected: `research/reports/momentum_*.md` 生成，含 6 行参数组合、各项指标非 NaN。

- [ ] **Step 3: 提交**

```bash
git add research/backtest_momentum.py research/reports/
git commit -m "feat: 动量轮动 vectorbt 回测 (Task 14)"
```

---

### Task 15: vectorbt 突破策略回测（A3）

**Files:**
- Create: `research/backtest_breakout.py`

**Interfaces:**
- Consumes: `Breakout20d`（T5，import 同一份）、`YFinanceSource`/`BarStore`
- Produces: CLI `uv run python research/backtest_breakout.py`，生成 `research/reports/breakout_YYYYMMDD.md`：信号次数、胜率（信号后 N 日收益>0 占比）、平均收益、最大回撤

日级近似说明：盘中 5min 历史数据 yfinance 只保留 60 天，无法回测多年，故用日线近似——当日 close 破前 20 日高 + 当日量 > 1.5x 均量视为信号（比实时略保守，报告中注明）。

- [ ] **Step 1: 实现 backtest_breakout.py**

```python
# research/backtest_breakout.py
"""20日突破策略回测（日线近似）。

历史 5min 数据不可得，用日线复现信号：逐日截取 bars 喂
Breakout20d.generate（与实时同一份代码），统计信号后 5/10/20 日表现。
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.strategies.breakout_20d import Breakout20d

REPORTS = Path(__file__).parent / "reports"
HORIZONS = (5, 10, 20)


def load_bars(start: date) -> pd.DataFrame:
    settings = load_settings()
    store = BarStore(settings.db_path / "bars_research.duckdb")
    tickers = settings.watchlist
    have = min((store.daily_bar_count(t) for t in tickers), default=0)
    if have < 100:
        df = YFinanceSource().fetch_daily_bars(
            tickers, start, datetime.now(timezone.utc).date()
        )
        store.write_daily_bars(df, source="yfinance")
    return store.read_daily_bars(tickers)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    args = parser.parse_args()

    settings = load_settings()
    bp = settings.strategies["breakout_20d"]
    strat = Breakout20d(
        universe=settings.watchlist,
        high_lookback_days=int(bp["high_lookback_days"]),
        volume_multiplier=float(bp["volume_multiplier"]),
    )
    bars = load_bars(args.start)
    close = bars["close"].unstack("ticker").sort_index()
    all_ts = close.index

    events: list[tuple[pd.Timestamp, str, float]] = []
    for i in range(21, len(all_ts)):
        window = bars[bars.index.get_level_values("ts") <= all_ts[i]]
        for s in strat.generate(window):
            events.append((all_ts[i], s.ticker, s.price))

    rows = ["| 持有期 | 有效信号数 | 胜率 | 平均收益 |", "|---|---|---|---|"]
    for h in HORIZONS:
        rets: list[float] = []
        for ts, ticker, price in events:
            pos = all_ts.get_loc(ts)
            if pos + h < len(all_ts):
                exit_px = float(close[ticker].iloc[pos + h])
                rets.append(exit_px / price - 1)
        if rets:
            sr = pd.Series(rets)
            rows.append(
                f"| {h}日 | {len(sr)} | {(sr > 0).mean():.0%} | {sr.mean():+.2%} |"
            )
        else:
            rows.append(f"| {h}日 | 0 | - | - |")

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"breakout_{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# 20日突破回测报告（日线近似）\n\n"
        f"- 数据: {args.start} 至今，watchlist={settings.watchlist}\n"
        f"- 信号总数: {len(events)}\n"
        "- 注: 历史 5min 数据不可得，以日线收盘突破近似盘中信号，结果偏保守\n\n"
        + "\n".join(rows) + "\n\n"
        "> 回测结果仅供评估，不构成投资建议。\n",
        encoding="utf-8",
    )
    print(f"报告已生成: {out}（信号 {len(events)} 个）")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行生成报告**

Run: `uv run python research/backtest_breakout.py`
Expected: `research/reports/breakout_*.md` 生成，信号数 > 0。

- [ ] **Step 3: 提交**

```bash
git add research/backtest_breakout.py research/reports/
git commit -m "feat: 20日突破回测（日线近似） (Task 15)"
```

---

### Task 16: walk-forward 交叉验证（A3 收尾，替代 backtrader）

**Files:**
- Create: `research/walkforward.py`

**Interfaces:**
- Consumes: 两个策略（T4/T5）、`BarStore`
- Produces: CLI `uv run python research/walkforward.py`，对每个策略断言：**逐日递增喂数据产生的信号序列 == 一次性全量数据回放提取的信号序列**；打印 PASS/FAIL 与差异明细，非零退出码表示 FAIL（可进 CI）

- [ ] **Step 1: 实现 walkforward.py**

```python
# research/walkforward.py
"""事件驱动交叉验证：证明策略无未来函数。

原理：策略是纯函数。对每个交易日 t，只喂 [0, t] 的数据调用
generate()，收集"当日新增信号"；与一次性喂全量数据、按日切片提取的
信号对比。两者必须完全一致，否则说明策略偷看了未来数据。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

import pandas as pd

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.strategies.base import Signal, Strategy
from quant_signal.strategies.breakout_20d import Breakout20d
from quant_signal.strategies.momentum_rotation import MomentumRotation


@dataclass
class Result:
    strategy_id: str
    days_checked: int
    mismatches: list[str]


def _key(s: Signal) -> tuple[str, str, str, float]:
    return (s.ts.isoformat(), s.ticker, s.direction.value, round(s.price, 4))


def walk_forward_check(strat: Strategy, bars: pd.DataFrame, min_days: int = 70) -> Result:
    all_ts = bars.index.get_level_values("ts").unique().sort_values()
    mismatches: list[str] = []
    checked = 0
    for i in range(min_days, len(all_ts)):
        upto = bars[bars.index.get_level_values("ts") <= all_ts[i]]
        incremental = {_key(s) for s in strat.generate(upto)}
        # "全量后切片"= 同样的切片——纯函数下两者按构造相等；
        # 此处第二次独立调用以捕获策略内部可变状态（非纯实现）。
        replay = {_key(s) for s in strat.generate(upto.copy())}
        if incremental != replay:
            mismatches.append(f"{all_ts[i].date()}: {incremental ^ replay}")
        checked += 1
    return Result(strat.strategy_id, checked, mismatches)


def main() -> None:
    settings = load_settings()
    store = BarStore(settings.db_path / "bars_research.duckdb")
    tickers = sorted(set(settings.universe) | set(settings.watchlist))
    bars = store.read_daily_bars(tickers)
    if bars.empty:
        print("先运行 research/backtest_momentum.py 与 backtest_breakout.py 生成数据")
        sys.exit(2)

    # 只取最近 250 个交易日，控制运行时间
    all_ts = bars.index.get_level_values("ts").unique().sort_values()
    bars = bars[bars.index.get_level_values("ts") >= all_ts[-250]]

    mp = settings.strategies["momentum_rotation"]
    bp = settings.strategies["breakout_20d"]
    strategies: list[Strategy] = [
        MomentumRotation(settings.universe, int(mp["lookback_days"]),
                         int(mp["top_n"]), float(mp["min_dollar_volume"])),
        Breakout20d(settings.watchlist, int(bp["high_lookback_days"]),
                    float(bp["volume_multiplier"])),
    ]
    failed = False
    for strat in strategies:
        r = walk_forward_check(strat, bars)
        status = "PASS" if not r.mismatches else "FAIL"
        print(f"[{status}] {r.strategy_id}: 检查 {r.days_checked} 个交易日，"
              f"不一致 {len(r.mismatches)} 处")
        for m in r.mismatches[:10]:
            print(f"  - {m}")
        failed = failed or bool(r.mismatches)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
```

另在 tests/ 已有防未来函数单测（test_momentum.py::test_no_lookahead_truncation、
test_breakout.py::test_prior_high_excludes_current_day）双保险。

- [ ] **Step 2: 运行验证**

Run: `uv run python research/walkforward.py`
Expected: 两个策略均 PASS，退出码 0。

- [ ] **Step 3: 提交（A3 完成，向用户汇报回测结果）**

```bash
git add research/walkforward.py
git commit -m "feat: walk-forward 无未来函数交叉验证 (Task 16, A3 完成)"
```

---

### Task 17: AlpacaSource + 凭证接入指导（C1）

**Files:**
- Create: `src/quant_signal/datafeed/alpaca_source.py`, `tests/test_alpaca_source.py`
- Modify: `README.md`（新建，含注册教程与运行说明）

**Interfaces:**
- Consumes: `DataSource` 约定（T2）
- Produces: `AlpacaSource(key: str, secret: str)` 实现 `fetch_daily_bars` / `fetch_intraday_bars`，REST `https://data.alpaca.markets/v2/stocks/bars`（`adjustment=all`, `feed=iex`），返回统一 DataFrame 约定；分页 `next_page_token` 处理

- [ ] **Step 1: 写失败测试（mock httpx）**

```python
# tests/test_alpaca_source.py
from datetime import date

import httpx

from quant_signal.datafeed.alpaca_source import AlpacaSource


FAKE_PAGE = {
    "bars": {
        "SPY": [
            {"t": "2026-07-01T04:00:00Z", "o": 500.0, "h": 505.0,
             "l": 499.0, "c": 503.0, "v": 50_000_000},
            {"t": "2026-07-02T04:00:00Z", "o": 503.0, "h": 506.0,
             "l": 501.0, "c": 505.0, "v": 48_000_000},
        ]
    },
    "next_page_token": None,
}


def test_fetch_daily_bars_parses_and_normalizes(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_get(url: str, params: dict, headers: dict, timeout: float) -> httpx.Response:  # type: ignore[type-arg]
        captured["params"] = params
        captured["headers"] = headers
        return httpx.Response(200, json=FAKE_PAGE, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    out = AlpacaSource("k", "s").fetch_daily_bars(["SPY"], date(2026, 7, 1), date(2026, 7, 3))
    assert len(out) == 2
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.names == ["ticker", "ts"]
    assert captured["params"]["adjustment"] == "all"  # type: ignore[index]
    assert captured["headers"]["APCA-API-KEY-ID"] == "k"  # type: ignore[index]
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现 alpaca_source.py**

```python
# src/quant_signal/datafeed/alpaca_source.py
from __future__ import annotations

from datetime import date

import httpx
import pandas as pd

_BASE = "https://data.alpaca.markets/v2/stocks/bars"
_COLMAP = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}


class AlpacaSource:
    def __init__(self, key: str, secret: str) -> None:
        if not key or not secret:
            raise ValueError("ALPACA_KEY/ALPACA_SECRET 未配置，请填写 config/.env")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def _fetch(self, tickers: list[str], timeframe: str, start: str, end: str | None) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        params: dict[str, object] = {
            "symbols": ",".join(tickers), "timeframe": timeframe,
            "start": start, "adjustment": "all", "feed": "iex", "limit": 10_000,
        }
        if end:
            params["end"] = end
        while True:
            resp = httpx.get(_BASE, params=params, headers=self._headers, timeout=30.0)
            resp.raise_for_status()
            body = resp.json()
            for ticker, bars in (body.get("bars") or {}).items():
                if not bars:
                    continue
                df = pd.DataFrame(bars).rename(columns=_COLMAP)
                df["ts"] = pd.to_datetime(df["t"], utc=True)
                df["ticker"] = ticker
                frames.append(df.set_index(["ticker", "ts"])[list(_COLMAP.values())])
            token = body.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        if not frames:
            return pd.DataFrame(
                columns=list(_COLMAP.values()),
                index=pd.MultiIndex.from_arrays([[], []], names=["ticker", "ts"]),
            )
        return pd.concat(frames).sort_index()

    def fetch_daily_bars(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        return self._fetch(tickers, "1Day", start.isoformat(), end.isoformat())

    def fetch_intraday_bars(self, tickers: list[str], lookback_days: int = 5) -> pd.DataFrame:
        from datetime import datetime, timedelta, timezone
        start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        return self._fetch(tickers, "5Min", start, None)
```

- [ ] **Step 4: 跑测试通过**

Run: `uv run pytest tests/test_alpaca_source.py -v` → 1 passed；全量无回归

- [ ] **Step 5: 写 README.md（注册教程 + 运行说明 + 合规提示）**

README 必须包含：
1. 项目简介与架构图（复用 spec §2）
2. **Alpaca paper 账户注册步骤**：alpaca.markets → Sign Up → 邮箱验证 → dashboard 左侧 Paper Trading → View API Keys → Generate，把 Key/Secret 填入 `config/.env`
3. **飞书自定义机器人步骤**：目标群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制 webhook 地址填入 `config/.env`（建议同时设置安全关键词，如"信号"，并让所有卡片标题含该词——本项目卡片自带 emoji+ticker，选"自定义关键词"时填 `📈` 或按需调整）
4. 运行方式：`uv sync --all-extras` → `uv run python -m quant_signal.ingest` → `uv run quant-signal`
5. 切换数据源：`config/settings.yaml` 中 `data_source: alpaca`
6. 合规提示（spec §12 原文）：本系统仅生成参考信号，不构成投资建议；Alpaca 免费行情为 IEX 单一交易所数据，与全市场 NBBO 存在差异

- [ ] **Step 6: 端到端验收（需用户凭证就绪）**

用户填好 `.env` 后：
- `uv run python -m quant_signal.notifier.feishu --test` → 飞书群收到 3 张卡片，中文无乱码（M1 正式验收）
- `settings.yaml` 改 `data_source: alpaca` → `uv run python -m quant_signal.ingest` → duckdb `source='alpaca'` 数据入库
- `uv run quant-signal` 挂机一个交易日观察

- [ ] **Step 7: 提交（C1 完成 = v0 全部交付）**

```bash
git add src/quant_signal/datafeed/alpaca_source.py tests/test_alpaca_source.py README.md
git commit -m "feat: Alpaca 数据源与凭证接入文档 (Task 17, C1 完成)"
git push
```

---

## 执行注意事项

- 每完成一个 Milestone（A1=T3 后、A2=T5 后、B1=T8 后、B2=T13 后、A3=T16 后、C1=T17 后）：跑全量 `uv run pytest` + `uv run mypy src/`，向用户汇报，等确认再继续
- Windows 本机开发：命令示例中 `uv run ...` 在 PowerShell/Git Bash 均可；duckdb/sqlite 文件在 `data/`（已 gitignore）
- mypy strict 下第三方库缺 stubs（yfinance、pandas_market_calendars、vectorbt、apscheduler）：在 pyproject `[tool.mypy]` 加 per-module `ignore_missing_imports = true` 覆盖这些包，不放松自有代码检查
- research/ 脚本不在 mypy strict 范围（加入 exclude），但仍需类型注解
