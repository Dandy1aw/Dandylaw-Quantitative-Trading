from quant_signal.notifier.base import CardKind
from quant_signal.notifier.cards import build_enrichment_card


def result(ticker: str, score: float, verdict: str, risks: list[str]) -> dict:
    return {
        "ticker": ticker, "name": f"{ticker} Inc.", "overall_score": score,
        "verdict_label": verdict, "panel_consensus": score - 10, "risks": risks,
    }


def test_card_lists_all_results() -> None:
    results = [
        result("MU", 49.8, "谨慎 · 1派看多/6派看空", ["ROE 极低", "行业景气度承压"]),
        result("SPY", 72.0, "积极 · 5派看多/1派看空", ["估值偏高"]),
    ]
    card = build_enrichment_card(results, held={"MU", "SPY"})
    assert card.kind is CardKind.REPORT
    assert "MU" in card.body_md and "SPY" in card.body_md
    assert "49.8" in card.body_md and "72.0" in card.body_md


def test_divergence_flagged_when_bearish_and_held() -> None:
    """held(=我们系统看多) 但深度分析给出看空/谨慎结论时，要有醒目分歧标注。"""
    results = [result("MU", 49.8, "谨慎 · 1派看多/6派看空", ["ROE 极低"])]
    card = build_enrichment_card(results, held={"MU"})
    assert "⚠️" in card.body_md
    assert "分歧" in card.body_md


def test_no_divergence_flag_when_bullish() -> None:
    results = [result("SPY", 72.0, "积极 · 5派看多/1派看空", ["估值偏高"])]
    card = build_enrichment_card(results, held={"SPY"})
    lines = [line for line in card.body_md.split("\n") if "SPY" in line]
    assert all("⚠️" not in line for line in lines)


def test_no_divergence_flag_when_not_held() -> None:
    """不在 held 里（只是候选，非当前持仓/买入目标）不做分歧提示。"""
    results = [result("MU", 49.8, "谨慎 · 1派看多/6派看空", ["ROE 极低"])]
    card = build_enrichment_card(results, held=set())
    lines = [line for line in card.body_md.split("\n") if "MU" in line]
    assert all("⚠️" not in line for line in lines)
