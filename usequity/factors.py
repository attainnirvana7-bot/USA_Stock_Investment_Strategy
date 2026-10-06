"""點時（point-in-time）因子計算。

回測的核心風險是前視偏差：在 t 日只能用 t 日「已公開」的財報。
每筆財報以 filing_date（向 SEC 申報日）作為可用日；缺值時以
報告期結束日 + filing_lag_days 估計。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .storage import Store

# 財報欄位對照：內部名稱 -> FMP 欄位候選（stable 與舊版 v3 命名不同時依序嘗試）
INCOME_FIELDS = {
    "revenue": ["revenue"],
    "gross_profit": ["grossProfit"],
    "operating_income": ["operatingIncome"],
    "net_income": ["netIncome"],
    "ebitda": ["ebitda"],
    "eps": ["epsDiluted", "epsdiluted", "eps"],
    "shares": ["weightedAverageShsOutDil", "weightedAverageShsOut"],
}
BALANCE_FIELDS = {
    "equity": ["totalStockholdersEquity", "totalEquity"],
    "total_assets": ["totalAssets"],
    "total_debt": ["totalDebt"],
    "cash": ["cashAndCashEquivalents", "cashAndShortTermInvestments"],
    "current_assets": ["totalCurrentAssets"],
    "current_liabilities": ["totalCurrentLiabilities"],
}
CASHFLOW_FIELDS = {
    "operating_cash_flow": ["operatingCashFlow", "netCashProvidedByOperatingActivities"],
    "capex": ["capitalExpenditure"],
    "free_cash_flow": ["freeCashFlow"],
}
# 損益與現金流量為「期間」數字，季資料需加總近四季成 TTM；資產負債為「時點」數字
FLOW_ITEMS = ["revenue", "gross_profit", "operating_income", "net_income", "ebitda", "eps",
              "operating_cash_flow", "capex", "free_cash_flow"]

# 供介面與說明文件使用
FACTOR_DESCRIPTIONS = {
    "market_cap": "市值（美元）",
    "price": "收盤價",
    "pe": "本益比 = 市值 / 近四季淨利",
    "earnings_yield": "盈餘殖利率 = 近四季淨利 / 市值（本益比倒數，虧損時為負）",
    "pb": "股價淨值比 = 市值 / 股東權益",
    "ps": "股價營收比 = 市值 / 近四季營收",
    "ev_ebitda": "EV/EBITDA，EV = 市值 + 總負債 − 現金",
    "fcf_yield": "自由現金流殖利率 = 近四季 FCF / 市值",
    "roe": "股東權益報酬率 = 近四季淨利 / 股東權益",
    "roa": "資產報酬率 = 近四季淨利 / 總資產",
    "gross_margin": "毛利率",
    "operating_margin": "營業利益率",
    "net_margin": "淨利率",
    "debt_to_equity": "負債權益比 = 總負債 / 股東權益",
    "current_ratio": "流動比率",
    "revenue_growth": "營收年增率（近四季 vs 前四季）",
    "eps_growth": "EPS 年增率（近四季 vs 前四季）",
    "momentum_12_1": "12-1 個月動能（排除最近一個月）",
    "return_1m": "近一個月報酬",
    "volatility_1y": "近一年年化波動度",
    "fundamental_age_days": "所用財報距今天數（資料新鮮度）",
}


def _pick(df: pd.DataFrame, mapping: dict[str, list[str]]) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    for name, candidates in mapping.items():
        col = next((c for c in candidates if c in df.columns), None)
        out[name] = pd.to_numeric(df[col], errors="coerce") if col else np.nan
    return out


def _available_date(df: pd.DataFrame, lag_days: int) -> pd.Series:
    est = df["fiscal_date"] + pd.Timedelta(days=lag_days)
    filing = df["filing_date"]
    # filing 早於 fiscal（資料錯誤）時不採信
    ok = filing.notna() & (filing >= df["fiscal_date"])
    return filing.where(ok, est)


def build_fundamentals(store: Store, symbols: list[str] | None = None,
                       filing_lag_days: int = 45) -> pd.DataFrame:
    """將三大報表合併為每檔每期一列的快照，季資料已轉 TTM。

    回傳欄位：symbol, fiscal_date, available_date, is_quarterly, 各項財報數字，
    以及 revenue_prev / eps_prev（前一年同口徑，供成長率計算）。
    """
    parts = []
    for kind, mapping in (("income", INCOME_FIELDS), ("balance", BALANCE_FIELDS),
                          ("cashflow", CASHFLOW_FIELDS)):
        raw = store.statements(kind, symbols)
        if raw.empty:
            continue
        vals = _pick(raw, mapping)
        vals[["symbol", "period", "fiscal_date"]] = raw[["symbol", "period", "fiscal_date"]]
        vals[f"avail_{kind}"] = _available_date(raw, filing_lag_days)
        parts.append(vals.set_index(["symbol", "period", "fiscal_date"]))
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, axis=1).reset_index()

    # 同一檔若同時有季與年資料，優先使用季資料（可算 TTM 且更新較即時）
    df["is_quarterly"] = df["period"].astype(str).str.upper().str.startswith("Q")
    has_q = df.groupby("symbol")["is_quarterly"].transform("any")
    df = df[df["is_quarterly"] == has_q].copy()

    avail_cols = [c for c in df.columns if c.startswith("avail_")]
    # 三張表都公開後才算可用
    df["available_date"] = df[avail_cols].max(axis=1)
    df = df.drop(columns=avail_cols)
    for col in FLOW_ITEMS + list(BALANCE_FIELDS) + ["shares"]:
        if col not in df:
            df[col] = np.nan
    df = df.sort_values(["symbol", "fiscal_date"]).reset_index(drop=True)

    out = []
    for _, g in df.groupby("symbol", sort=False):
        g = g.copy()
        if g["is_quarterly"].iloc[0]:
            # 近四季加總；四季橫跨超過 ~13 個月代表中間缺季，視為無效
            span = g["fiscal_date"] - g["fiscal_date"].shift(3)
            valid = span <= pd.Timedelta(days=300)
            for col in FLOW_ITEMS:
                g[col] = g[col].rolling(4, min_periods=4).sum().where(valid)
            lag = 4
        else:
            lag = 1
        prev_span = g["fiscal_date"] - g["fiscal_date"].shift(lag)
        prev_ok = prev_span.between(pd.Timedelta(days=330), pd.Timedelta(days=400))
        g["revenue_prev"] = g["revenue"].shift(lag).where(prev_ok)
        g["eps_prev"] = g["eps"].shift(lag).where(prev_ok)
        out.append(g)
    res = pd.concat(out, ignore_index=True)
    return res.dropna(subset=["revenue", "net_income"], how="all")


def _safe_div(a, b):
    a = pd.to_numeric(a, errors="coerce")
    b = pd.to_numeric(b, errors="coerce")
    with np.errstate(divide="ignore", invalid="ignore"):
        r = a / b
    return r.where(b.notna() & (b != 0))


def _growth(cur, prev):
    # 基期為負時成長率無意義
    return _safe_div(cur - prev, prev.abs()).where(prev > 0)


def compute_factors(
    fundamentals: pd.DataFrame,
    prices: pd.DataFrame,
    asof: pd.Timestamp | str,
    market_caps: pd.DataFrame | None = None,
    profiles: pd.DataFrame | None = None,
    max_staleness_days: int = 400,
) -> pd.DataFrame:
    """計算 asof 當日（收盤後）可得的因子截面，index 為代號。

    prices：還原股價寬表（index=日期、columns=代號）。
    market_caps：歷史市值寬表；缺值時以 收盤價 × 稀釋股數 估算。
    """
    asof = pd.Timestamp(asof)
    if fundamentals.empty:
        return pd.DataFrame()

    avail = fundamentals[fundamentals["available_date"] <= asof]
    avail = avail[avail["fiscal_date"] >= asof - pd.Timedelta(days=max_staleness_days)]
    if avail.empty:
        return pd.DataFrame()
    snap = avail.sort_values("fiscal_date").groupby("symbol").tail(1).set_index("symbol")

    px = prices.loc[:asof]
    syms = [s for s in snap.index if s in px.columns]
    snap = snap.loc[syms]
    if px.empty or not syms:
        return pd.DataFrame()
    px = px[syms]
    last_px = px.ffill().iloc[-1]
    # 若最後有效價格太舊（下市、停牌），視為無報價
    last_valid = px.apply(lambda s: s.last_valid_index())
    stale = (asof - pd.to_datetime(last_valid)) > pd.Timedelta(days=10)
    last_px = last_px.where(~stale)

    f = pd.DataFrame(index=snap.index)
    f["price"] = last_px

    mcap = pd.Series(np.nan, index=snap.index)
    if market_caps is not None and not market_caps.empty:
        mc = market_caps.loc[:asof].reindex(columns=snap.index)
        if not mc.empty:
            recent = mc.loc[asof - pd.Timedelta(days=10):]
            mcap = recent.ffill().iloc[-1] if not recent.empty else mcap
    est = last_px * snap["shares"]
    f["market_cap"] = mcap.fillna(est)

    mc_ = f["market_cap"]
    ev = mc_ + snap["total_debt"].fillna(0) - snap["cash"].fillna(0)
    f["pe"] = _safe_div(mc_, snap["net_income"]).where(snap["net_income"] > 0)
    f["earnings_yield"] = _safe_div(snap["net_income"], mc_)
    f["pb"] = _safe_div(mc_, snap["equity"]).where(snap["equity"] > 0)
    f["ps"] = _safe_div(mc_, snap["revenue"]).where(snap["revenue"] > 0)
    f["ev_ebitda"] = _safe_div(ev, snap["ebitda"]).where(snap["ebitda"] > 0)
    fcf = snap["free_cash_flow"].fillna(snap["operating_cash_flow"] + snap["capex"])
    f["fcf_yield"] = _safe_div(fcf, mc_)
    f["roe"] = _safe_div(snap["net_income"], snap["equity"]).where(snap["equity"] > 0)
    f["roa"] = _safe_div(snap["net_income"], snap["total_assets"])
    f["gross_margin"] = _safe_div(snap["gross_profit"], snap["revenue"])
    f["operating_margin"] = _safe_div(snap["operating_income"], snap["revenue"])
    f["net_margin"] = _safe_div(snap["net_income"], snap["revenue"])
    f["debt_to_equity"] = _safe_div(snap["total_debt"], snap["equity"]).where(snap["equity"] > 0)
    f["current_ratio"] = _safe_div(snap["current_assets"], snap["current_liabilities"])
    f["revenue_growth"] = _growth(snap["revenue"], snap["revenue_prev"])
    f["eps_growth"] = _growth(snap["eps"], snap["eps_prev"])

    # 價格因子（以交易日數計）
    def ret_between(lag_far: int, lag_near: int) -> pd.Series:
        if len(px) <= lag_far:
            return pd.Series(np.nan, index=px.columns)
        filled = px.ffill()
        return filled.iloc[-1 - lag_near] / filled.iloc[-1 - lag_far] - 1

    f["momentum_12_1"] = ret_between(252, 21)
    f["return_1m"] = ret_between(21, 0)
    daily = px.iloc[-253:].pct_change(fill_method=None)
    f["volatility_1y"] = daily.std() * np.sqrt(252)
    f.loc[daily.count() < 200, "volatility_1y"] = np.nan
    f["fundamental_age_days"] = (asof - snap["fiscal_date"]).dt.days
    f["fiscal_date"] = snap["fiscal_date"]

    if profiles is not None and not profiles.empty:
        prof = profiles.set_index("symbol")
        for col, src in (("name", "companyName"), ("sector", "sector"), ("industry", "industry")):
            if src in prof.columns:
                f[col] = prof[src].reindex(f.index)

    f = f.replace([np.inf, -np.inf], np.nan)
    return f[f["price"].notna()]


class FactorEngine:
    """從資料庫載入一次資料，之後可快速計算任意日期的截面（回測重複呼叫用）。"""

    def __init__(self, store: Store, symbols: list[str] | None = None, filing_lag_days: int = 45,
                 benchmark: str | None = None):
        self.fundamentals = build_fundamentals(store, symbols, filing_lag_days)
        if symbols is None and not self.fundamentals.empty:
            symbols = sorted(self.fundamentals["symbol"].unique())
        self.symbols = symbols or []
        self.benchmark = benchmark
        # 基準只載入股價，不參與選股
        price_syms = self.symbols + ([benchmark] if benchmark and benchmark not in self.symbols else [])
        self.prices = store.prices(price_syms) if self.symbols else pd.DataFrame()
        self.market_caps = store.market_caps(self.symbols) if self.symbols else pd.DataFrame()
        self.profiles = store.profiles()

    def at(self, asof) -> pd.DataFrame:
        if self.prices.empty:
            return pd.DataFrame()
        fac = compute_factors(self.fundamentals, self.prices, asof, self.market_caps, self.profiles)
        return fac[fac.index != self.benchmark] if self.benchmark else fac

    def latest(self) -> pd.DataFrame:
        if self.prices.empty:
            return pd.DataFrame()
        return self.at(self.prices.index.max())
