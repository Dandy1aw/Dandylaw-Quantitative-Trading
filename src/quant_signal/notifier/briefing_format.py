"""Chinese display formatting shared by briefing cards."""

from __future__ import annotations

from decimal import Decimal

SECTOR_NAMES_ZH = {
    "Technology": "科技",
    "Semiconductors": "半导体",
    "Information Technology": "信息技术",
    "Communication Services": "通信服务",
    "Consumer Discretionary": "非必需消费",
    "Consumer Staples": "必需消费",
    "Energy": "能源",
    "Financials": "金融",
    "Health Care": "医疗保健",
    "Industrials": "工业",
    "Materials": "原材料",
    "Real Estate": "房地产",
    "Utilities": "公用事业",
}

INDUSTRY_NAMES_ZH = {
    "Computer Hardware": "计算机硬件",
    "Communication Equipment": "通信设备",
    "Advertising Agencies": "广告代理",
    "Semiconductors": "半导体",
    "Semiconductor Equipment & Materials": "半导体设备与材料",
    "Software - Infrastructure": "基础软件",
    "Software - Application": "应用软件",
}

BLOCK_REASON_NAMES_ZH = {
    "STOP_TOO_WIDE": "止损距离过大",
    "STOP_TOO_TIGHT": "止损距离过小",
    "NO_ACCOUNT": "账户数据不足",
    "INVALID_PRICES": "价格结构无效",
    "INSUFFICIENT_BUYING_POWER": "购买力不足",
    "POSITION_ALREADY_OPEN": "已有持仓",
    "ZERO_QTY": "可用资金不足，建议股数为0",
    "UNSUPPORTED_MARKET": "当前市场暂不支持",
    "STALE_QUOTE": "行情数据已过期",
    "STALE_ACCOUNT": "账户数据已过期",
    "POSITION_QTY_UNKNOWN": "持仓股数不完整",
    "MAX_NEW_POSITIONS": "当日新开仓数量已达上限",
    "DAILY_RISK_EXHAUSTED": "当日新增风险额度已用完",
    "PORTFOLIO_BUDGET_EXHAUSTED": "组合可用资金已用完",
    "CLUSTER_WEIGHT_EXCEEDED": "相关板块仓位已达上限",
}


def briefing_float(value: object) -> float | None:
    if not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def briefing_number(value: object, digits: int = 2) -> str:
    number = briefing_float(value)
    return "-" if number is None else f"{number:.{digits}f}"


def briefing_percent(value: object) -> str:
    number = briefing_float(value)
    return "-" if number is None else f"{number * 100:.0f}%"


def sector_name_zh(value: object) -> str:
    raw = str(value or "未知行业")
    return SECTOR_NAMES_ZH.get(raw, raw)


def industry_name_zh(value: object) -> str:
    raw = str(value or "细分行业未知")
    return INDUSTRY_NAMES_ZH.get(raw, raw)


def usd_yi(value: object, *, digits: int = 2) -> str:
    number = briefing_float(value)
    return "-" if number is None else f"{number / 100_000_000:.{digits}f}亿美元"


def market_cap_zh(value: object) -> str:
    number = briefing_float(value)
    if number is None:
        return "-"
    if number >= 1_000_000_000_000:
        return f"{number / 1_000_000_000_000:.2f}万亿美元"
    return f"{number / 100_000_000:.0f}亿美元"
