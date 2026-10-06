import pytest

from usequity.storage import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def quarter_rows(symbol, quarters, filing_lag=30, **values):
    """產生季報列；values 的值可為常數或 callable(i)。"""
    import pandas as pd
    rows = []
    for i, q in enumerate(quarters):
        q = pd.Timestamp(q)
        r = {"date": q.strftime("%Y-%m-%d"), "symbol": symbol, "period": f"Q{(q.month - 1) // 3 + 1}",
             "filingDate": (q + pd.Timedelta(days=filing_lag)).strftime("%Y-%m-%d") if filing_lag is not None else None}
        for k, v in values.items():
            r[k] = v(i) if callable(v) else v
        rows.append(r)
    return rows
