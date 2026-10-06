import numpy as np
import pandas as pd
import pytest

from conftest import quarter_rows
from usequity.backtest import BacktestConfig, performance, rebalance_dates, run_backtest
from usequity.factors import FactorEngine

Q = pd.date_range("2018-03-31", periods=24, freq="QE")
DAYS = pd.bdate_range("2018-01-01", "2023-12-29")


def seed(store):
    # GOOD：ROE 高、每日 +0.05%；BAD：ROE 低、每日 −0.05%
    for sym, ni, drift in (("GOOD", 30, 0.0005), ("BAD", 1, -0.0005)):
        store.save_statements(sym, "income", quarter_rows(sym, Q, 30, revenue=100, netIncome=ni,
                                                          weightedAverageShsOutDil=10))
        store.save_statements(sym, "balance", quarter_rows(sym, Q, 30, totalStockholdersEquity=100))
        px = 100 * (1 + drift) ** np.arange(len(DAYS))
        store.save_prices(sym, [{"date": d.strftime("%Y-%m-%d"), "adjClose": p} for d, p in zip(DAYS, px)])
    spy = 100 * 1.0002 ** np.arange(len(DAYS))
    store.save_prices("SPY", [{"date": d.strftime("%Y-%m-%d"), "adjClose": p} for d, p in zip(DAYS, spy)])


def test_rebalance_dates():
    d = rebalance_dates(pd.bdate_range("2020-01-01", "2020-12-31"), "quarterly")
    assert [x.strftime("%m-%d") for x in d] == ["03-31", "06-30", "09-30", "12-31"]


def test_backtest_picks_good_stock(store):
    seed(store)
    eng = FactorEngine(store, benchmark="SPY")
    assert "SPY" in eng.prices.columns
    cfg = BacktestConfig(start="2019-06-01", end="2023-12-29", top_n=1, rank={"roe": 1},
                         cost_bps=0, benchmark="SPY")
    res = run_backtest(eng, cfg)
    assert set(res.holdings["symbol"]) == {"GOOD"}
    # 零成本、全程持有 GOOD：淨值 = GOOD 股價報酬
    px = eng.prices["GOOD"]
    expected = px.loc[res.equity.index[-1]] / px.loc[res.equity.index[0]]
    assert res.equity.iloc[-1] == pytest.approx(expected, rel=1e-9)
    assert res.benchmark is not None and res.metrics["excess_cagr"] > 0
    # 第二期起換手為 0
    assert res.rebalances["turnover"].iloc[1:].abs().max() == pytest.approx(0)


def test_costs_reduce_equity(store):
    seed(store)
    eng = FactorEngine(store, benchmark="SPY")
    base = dict(start="2019-06-01", top_n=2, rank={"roe": 1}, benchmark="SPY")
    r0 = run_backtest(eng, BacktestConfig(cost_bps=0, **base))
    r1 = run_backtest(eng, BacktestConfig(cost_bps=50, **base))
    assert r1.equity.iloc[-1] < r0.equity.iloc[-1]
    # 建倉成本 50bps
    assert r1.rebalances["cost"].iloc[0] == pytest.approx(0.005)


def test_nothing_passes_raises(store):
    seed(store)
    eng = FactorEngine(store, benchmark="SPY")
    with pytest.raises(ValueError):
        run_backtest(eng, BacktestConfig(start="2019-06-01", filters=["roe > 99"], benchmark="SPY"))


def test_leading_periods_without_data_skipped(store):
    seed(store)
    eng = FactorEngine(store, benchmark="SPY")
    # 財報從 2018Q1 開始、TTM 需四季 → 2018 年底前無資料；起始日設更早
    res = run_backtest(eng, BacktestConfig(start="2018-01-01", top_n=1, rank={"roe": 1}, benchmark="SPY"))
    assert res.equity.index[0] > pd.Timestamp("2018-06-01")
    assert res.equity.iloc[0] == pytest.approx(1 - 10 / 1e4)
    assert any("實際起始" in w for w in res.warnings)
    assert res.benchmark.index[0] == res.equity.index[0]


def test_performance_metrics():
    idx = pd.bdate_range("2020-01-01", periods=253)
    eq = pd.Series(np.linspace(1, 1.1, 253), index=idx)
    m = performance(eq)
    assert m["total_return"] == pytest.approx(0.1)
    assert m["max_drawdown"] == 0
