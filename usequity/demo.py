"""產生合成示範資料（不需 API 金鑰），用來試用介面與測試。

資料完全是隨機產生的虛構公司，欄位格式與 FMP stable API 回應相同。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .storage import Store

SECTORS = ["Technology", "Healthcare", "Financial Services", "Energy", "Consumer Defensive",
           "Industrials"]


def generate_demo(store: Store, n_symbols: int = 40, start: str = "2013-01-01",
                  end: str = "2026-09-30", seed: int = 7) -> list[str]:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, end)
    quarters = pd.date_range(start, end, freq="QE")
    market = rng.normal(0.0004, 0.011, len(days))

    # 基準指數
    spy = 200 * np.exp(np.cumsum(market))
    store.save_prices("SPY", [{"date": d.strftime("%Y-%m-%d"), "close": p, "adjClose": p, "volume": 1e8}
                              for d, p in zip(days, spy)])

    symbols = []
    for i in range(n_symbols):
        sym = f"DEMO{i:02d}"
        symbols.append(sym)
        quality = rng.uniform(-1, 1)          # 高品質公司：高 ROE、較高長期報酬
        shares = rng.uniform(2e8, 5e9)
        rev = rng.uniform(1e9, 2e10) / 4
        growth_q = 0.01 + 0.015 * quality + rng.normal(0, 0.005)
        margin = 0.12 + 0.08 * quality
        equity = rev * 4 * rng.uniform(0.5, 1.5)
        debt = equity * rng.uniform(0.1, 2.5)

        inc, bal, cf = [], [], []
        for q in quarters:
            rev *= 1 + growth_q + rng.normal(0, 0.03)
            ni = rev * (margin + rng.normal(0, 0.03))
            equity += ni * 0.6
            filing = q + pd.Timedelta(days=int(rng.integers(25, 45)))
            common = {"date": q.strftime("%Y-%m-%d"), "symbol": sym, "reportedCurrency": "USD",
                      "filingDate": filing.strftime("%Y-%m-%d"), "fiscalYear": str(q.year),
                      "period": f"Q{(q.month - 1) // 3 + 1}"}
            inc.append({**common, "revenue": rev, "grossProfit": rev * (0.35 + 0.15 * quality),
                        "operatingIncome": ni * 1.3, "netIncome": ni, "ebitda": ni * 1.6,
                        "epsDiluted": ni / shares, "weightedAverageShsOutDil": shares})
            bal.append({**common, "totalStockholdersEquity": equity, "totalAssets": equity + debt * 1.5,
                        "totalDebt": debt, "cashAndCashEquivalents": rev * 0.5,
                        "totalCurrentAssets": rev * 1.5, "totalCurrentLiabilities": rev * 1.1})
            ocf = ni * rng.uniform(0.9, 1.4)
            capex = -rev * 0.05
            cf.append({**common, "operatingCashFlow": ocf, "capitalExpenditure": capex,
                       "freeCashFlow": ocf + capex})
        store.save_statements(sym, "income", inc)
        store.save_statements(sym, "balance", bal)
        store.save_statements(sym, "cashflow", cf)

        beta = rng.uniform(0.6, 1.5)
        alpha = 0.00015 * quality
        idio = rng.normal(0, 0.016, len(days))
        p0 = rng.uniform(20, 300)
        px = p0 * np.exp(np.cumsum(beta * market + alpha + idio))
        store.save_prices(sym, [{"date": d.strftime("%Y-%m-%d"), "close": p, "adjClose": p, "volume": 1e6}
                                for d, p in zip(days, px)])
        store.save_market_caps(sym, [{"date": d.strftime("%Y-%m-%d"), "marketCap": p * shares}
                                     for d, p in zip(days, px)])
        store.save_profile(sym, {"symbol": sym, "companyName": f"Demo Company {i:02d}",
                                 "sector": SECTORS[i % len(SECTORS)], "industry": "Demo",
                                 "exchange": "DEMO", "country": "US"})
        for ds in ("profile", "statements", "prices", "marketcap"):
            store.log_fetch(sym, ds, "ok", "demo")
    store.log_fetch("SPY", "prices", "ok", "demo")
    return symbols
