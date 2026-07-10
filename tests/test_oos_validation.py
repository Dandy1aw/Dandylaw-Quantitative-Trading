import pandas as pd

from quant_signal.oos_validation import buffered_selection, rolling_splits


def test_rolling_splits_are_strictly_forward_and_non_overlapping() -> None:
    idx = pd.bdate_range("2020-01-01", periods=30, tz="UTC")

    splits = rolling_splits(idx, train_size=12, test_size=6, step_size=6)

    assert len(splits) == 3
    assert all(split.train_end < split.test_start for split in splits)
    assert all(split.train_start <= split.train_end for split in splits)
    assert all(split.test_start <= split.test_end for split in splits)
    assert splits[1].test_start > splits[0].test_start


def test_buffered_selection_retains_incumbent_inside_buffer() -> None:
    ranking = [("A", 0.30), ("B", 0.20), ("C", 0.19), ("D", 0.10)]

    selected = buffered_selection(ranking, current={"C"}, top_n=2, rank_buffer=1)

    assert selected == ["A", "C"]


def test_buffered_selection_applies_absolute_momentum_hurdle() -> None:
    ranking = [("A", 0.03), ("B", -0.01), ("C", -0.05)]

    selected = buffered_selection(
        ranking, current={"B"}, top_n=2, rank_buffer=2, min_momentum=0.0
    )

    assert selected == ["A"]
