import pandas as pd
import pytest

from conftest import quarter_rows
from usequity.factors import build_fundamentals, compute_factors

Q = pd.date_range("2020-03-31", periods=8, freq="QE")


def seed(store, filing_lag=30):
    store.save_statements("AAA", "income", quarter_rows(
        "AAA", Q, filing_lag, revenue=lambda i: 100 + 10 * i, netIncome=10, grossProfit=40,
        operatingIncome=15, ebitda=20, epsDiluted=1.0, weightedAverageShsOutDil=100))
    store.save_statements("AAA", "balance", quarter_rows(
        "AAA", Q, filing_lag, totalStockholdersEquity=200, totalAssets=500, totalDebt=100,
        cashAndCashEquivalents=50, totalCurrentAssets=150, totalCurrentLiabilities=100))
    store.save_statements("AAA", "cashflow", quarter_rows(
        "AAA", Q, filing_lag, operatingCashFlow=12, capitalExpenditure=-2, freeCashFlow=10))
    days = pd.bdate_range("2019-01-01", "2022-06-30")
    store.save_prices("AAA", [{"date": d.strftime("%Y-%m-%d"), "close": 10.0, "adjClose": 10.0} for d in days])


def test_ttm_and_growth(store):
    seed(store)
    f = build_fundamentals(store)
    last = f.iloc[-1]
    # 近四季營收：170+160+150+140
    assert last["revenue"] == 620
    assert last["net_income"] == 40
    assert last["revenue_prev"] == 100 + 110 + 120 + 130
    assert f["revenue"].isna().sum() == 0  # 不足四季的列已被丟棄
    assert len(f) == 5


def test_no_lookahead(store):
    seed(store, filing_lag=30)
    f = build_fundamentals(store)
    prices = store.prices()
    q_end = Q[-1]
    # 季末後 10 天：最新一季尚未申報，應使用前一季
    fac = compute_factors(f, prices, q_end + pd.Timedelta(days=10))
    assert fac.loc["AAA", "fiscal_date"] == Q[-2]
    # 申報日後：才看得到
    fac = compute_factors(f, prices, q_end + pd.Timedelta(days=31))
    assert fac.loc["AAA", "fiscal_date"] == Q[-1]


def test_missing_filing_date_uses_lag(store):
    seed(store, filing_lag=None)
    f = build_fundamentals(store, filing_lag_days=45)
    prices = store.prices()
    fac = compute_factors(f, prices, Q[-1] + pd.Timedelta(days=44))
    assert fac.loc["AAA", "fiscal_date"] == Q[-2]
    fac = compute_factors(f, prices, Q[-1] + pd.Timedelta(days=46))
    assert fac.loc["AAA", "fiscal_date"] == Q[-1]


def test_valuation_ratios(store):
    seed(store)
    f = build_fundamentals(store)
    fac = compute_factors(f, store.prices(), "2022-06-30").loc["AAA"]
    mcap = 10.0 * 100  # 無歷史市值時以 價格 × 股數 估算
    assert fac["market_cap"] == mcap
    assert fac["pe"] == pytest.approx(mcap / 40)
    assert fac["roe"] == pytest.approx(40 / 200)
    assert fac["debt_to_equity"] == pytest.approx(0.5)
    assert fac["gross_margin"] == pytest.approx(160 / 620)
    assert fac["ev_ebitda"] == pytest.approx((mcap + 100 - 50) / 80)
    assert fac["fcf_yield"] == pytest.approx(40 / mcap)
    assert fac["momentum_12_1"] == pytest.approx(0.0)


def test_market_cap_table_preferred(store):
    seed(store)
    store.save_market_caps("AAA", [{"date": "2022-06-30", "marketCap": 5000.0}])
    f = build_fundamentals(store)
    fac = compute_factors(f, store.prices(), "2022-06-30", store.market_caps())
    assert fac.loc["AAA", "market_cap"] == 5000.0


def test_stale_fundamentals_dropped(store):
    seed(store)
    f = build_fundamentals(store)
    # 最後一季 2021-12-31，超過 400 天後不再使用
    assert compute_factors(f, store.prices(), "2023-03-01").empty
