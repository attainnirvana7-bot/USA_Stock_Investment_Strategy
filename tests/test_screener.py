import numpy as np
import pandas as pd
import pytest

from usequity.screener import FilterError, apply_filters, composite_score, parse_filter, screen


@pytest.fixture
def df():
    return pd.DataFrame({
        "roe": [0.2, 0.05, np.nan, 0.3],
        "pe": [10, 30, 15, 50],
        "sector": ["Technology", "Energy", "Technology", "Utilities"],
    }, index=["A", "B", "C", "D"])


def test_parse_filter():
    assert parse_filter("market_cap > 10e9") == ("market_cap", ">", 1e10)
    assert parse_filter('sector == "Technology"') == ("sector", "==", "Technology")
    assert parse_filter("sector != 'Energy|Utilities'") == ("sector", "!=", ["Energy", "Utilities"])
    for bad in ["roe >", "__import__('os')", "roe > abc", 'roe > "x"']:
        with pytest.raises(FilterError):
            parse_filter(bad)


def test_apply_filters(df):
    assert list(apply_filters(df, ["roe > 0.1"]).index) == ["A", "D"]
    # 缺值不通過，連 != 也一樣
    assert "C" not in apply_filters(df, ["roe != 0.2"]).index
    assert list(apply_filters(df, ['sector != "Energy|Utilities"']).index) == ["A", "C"]
    with pytest.raises(FilterError):
        apply_filters(df, ["nope > 1"])


def test_composite_score_direction(df):
    # pe 權重為負：越小分數越高
    s = composite_score(df, {"pe": -1})
    assert s.idxmax() == "A" and s.idxmin() == "D"
    s = composite_score(df, {"roe": 1, "pe": -1})
    # C 沒有 roe，只用 pe 計分，仍有分數
    assert s.notna().all()


def test_screen_top_n(df):
    res = screen(df, ["pe < 40"], {"roe": 1}, top_n=1)
    assert list(res.selected.index) == ["A"]
    assert res.universe_size == 4
    assert list(res.candidates["rank"]) == list(range(1, len(res.candidates) + 1))
