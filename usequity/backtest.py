"""定期再平衡的多因子選股回測。

流程：每個再平衡日收盤後，以當日已公開資料計算因子 → 選股 → 以當日收盤價換股，
持有至下個再平衡日。期間內權重隨股價漂移（買進持有），換股時扣交易成本。

已知限制（解讀結果時請留意）：
- 股票池是「現在」的成分股或清單，存在倖存者偏差，歷史績效會偏樂觀。
- 以收盤價成交、無滑價與流動性限制。
- 公司基本資料（產業別）只有現況，不是點時資料。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from .factors import FactorEngine
from .screener import screen

FREQ = {"monthly": "ME", "quarterly": "QE", "annual": "YE"}


@dataclass
class BacktestConfig:
    start: str = "2016-01-01"
    end: str | None = None
    rebalance: str = "quarterly"
    weighting: str = "equal"
    cost_bps: float = 10.0
    top_n: int = 10
    filters: list[str] = field(default_factory=list)
    rank: dict[str, float] = field(default_factory=dict)
    benchmark: str | None = "SPY"

    @classmethod
    def from_config(cls, cfg: dict, **overrides) -> "BacktestConfig":
        b, s = cfg["backtest"], cfg["screen"]
        kw = dict(
            start=b.get("start"), end=b.get("end"), rebalance=b.get("rebalance", "quarterly"),
            weighting=b.get("weighting", "equal"), cost_bps=b.get("cost_bps", 10),
            top_n=s.get("top_n", 10), filters=list(s.get("filters") or []),
            rank=dict(s.get("rank") or {}), benchmark=cfg["crawl"].get("benchmark"),
        )
        kw.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kw)


@dataclass
class BacktestResult:
    equity: pd.Series                 # 策略淨值（起始 1.0）
    benchmark: pd.Series | None       # 基準淨值
    holdings: pd.DataFrame            # 每期持股：date, symbol, weight, score
    rebalances: pd.DataFrame          # 每期：date, n_holdings, turnover, cost
    metrics: dict[str, float]
    benchmark_metrics: dict[str, float] | None
    warnings: list[str] = field(default_factory=list)


def rebalance_dates(index: pd.DatetimeIndex, freq: str) -> list[pd.Timestamp]:
    """每期最後一個交易日。"""
    if freq not in FREQ:
        raise ValueError(f"rebalance 必須是 {list(FREQ)} 之一")
    s = pd.Series(index, index=index)
    return list(s.groupby(pd.Grouper(freq=FREQ[freq])).last().dropna())


def performance(equity: pd.Series, rf: float = 0.0) -> dict[str, float]:
    equity = equity.dropna()
    if len(equity) < 2:
        return {}
    rets = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    vol = rets.std() * np.sqrt(252)
    downside = rets[rets < 0].std() * np.sqrt(252)
    dd = equity / equity.cummax() - 1
    mdd = dd.min()
    excess = rets - rf / 252
    return {
        "total_return": total,
        "cagr": cagr,
        "volatility": vol,
        "sharpe": excess.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else np.nan,
        "sortino": excess.mean() * 252 / downside if downside > 0 else np.nan,
        "max_drawdown": mdd,
        "calmar": cagr / abs(mdd) if mdd < 0 else np.nan,
        "best_day": rets.max(),
        "worst_day": rets.min(),
    }


def run_backtest(engine: FactorEngine, cfg: BacktestConfig,
                 progress: Callable[[int, int, pd.Timestamp], None] | None = None) -> BacktestResult:
    prices = engine.prices
    if prices.empty:
        raise ValueError("資料庫沒有股價資料，請先執行爬蟲")
    bench_sym = cfg.benchmark if cfg.benchmark in prices.columns else None
    tradable = prices.drop(columns=[bench_sym]) if bench_sym else prices

    start = pd.Timestamp(cfg.start) if cfg.start else tradable.index.min()
    end = pd.Timestamp(cfg.end) if cfg.end else tradable.index.max()
    window = tradable.loc[start:end]
    if len(window) < 2:
        raise ValueError(f"{start.date()} ~ {end.date()} 之間沒有足夠的股價資料")
    dates = [d for d in rebalance_dates(window.index, cfg.rebalance) if d < window.index[-1]]
    # 起始日若不是期末，也在第一個交易日建倉，避免浪費第一期
    if not dates or dates[0] > window.index[0]:
        dates.insert(0, window.index[0])
    warnings: list[str] = []

    # 期間內以前值補缺（停牌、下市後視為以最後價格持有現金）
    filled = window.ffill()
    daily_ret = filled.pct_change(fill_method=None).fillna(0.0)

    equity = pd.Series(np.nan, index=window.index)
    holdings_rows, reb_rows = [], []
    value = 1.0
    cur_w = pd.Series(dtype=float)   # 目前（漂移後）權重
    empty_periods = 0
    started = False       # 尚未有任何持股前的期間（財報資料還沒開始）直接略過，不計入績效
    skipped_leading = 0

    for i, d in enumerate(dates):
        if progress:
            progress(i + 1, len(dates), d)
        nxt = dates[i + 1] if i + 1 < len(dates) else window.index[-1]
        fac = engine.at(d)
        fac = fac[fac.index != bench_sym] if bench_sym else fac
        target = pd.Series(dtype=float)
        if not fac.empty:
            res = screen(fac, cfg.filters, cfg.rank, cfg.top_n)
            sel = res.selected
            if not sel.empty:
                if cfg.weighting == "rank" and sel["score"].notna().all() and sel["score"].sum() > 0:
                    target = sel["score"] / sel["score"].sum()
                else:
                    target = pd.Series(1.0 / len(sel), index=sel.index)
                for sym, w in target.items():
                    holdings_rows.append({"date": d, "symbol": sym, "weight": w,
                                          "score": sel.at[sym, "score"]})
        if target.empty and not started:
            skipped_leading += 1
            continue
        if target.empty:
            empty_periods += 1

        all_syms = target.index.union(cur_w.index)
        turnover = (target.reindex(all_syms, fill_value=0) - cur_w.reindex(all_syms, fill_value=0)).abs().sum()
        cost = turnover * cfg.cost_bps / 1e4
        value *= 1 - cost
        reb_rows.append({"date": d, "n_holdings": len(target), "turnover": turnover, "cost": cost})

        # 持有期：(d, nxt]
        period = daily_ret.loc[d:nxt].iloc[1:]
        if not started:
            started = True
            equity.loc[d] = value
        if target.empty:
            # 空手：持有現金（報酬 0）
            equity.loc[period.index] = value
            cur_w = pd.Series(dtype=float)
            continue
        r = period.reindex(columns=target.index).fillna(0.0)
        growth = (1 + r).cumprod()
        path = growth.mul(target, axis=1)
        port = path.sum(axis=1)
        equity.loc[period.index] = value * port.values
        if len(port):
            value *= port.iloc[-1]
            cur_w = path.iloc[-1] / port.iloc[-1]
        else:
            cur_w = target

    if not started:
        raise ValueError("整段期間沒有任何股票通過條件（或尚無可用財報），請放寬條件或確認資料")
    equity = equity.ffill().dropna()
    if skipped_leading:
        warnings.append(f"前 {skipped_leading} 期尚無可用財報或無股票通過條件，"
                        f"回測實際起始於 {equity.index[0].date()}")
    if empty_periods:
        warnings.append(f"{empty_periods}/{len(dates)} 期沒有任何股票通過條件，該期持有現金")

    bench = None
    bench_metrics = None
    if bench_sym:
        b = prices[bench_sym].loc[equity.index[0]:equity.index[-1]].ffill().dropna()
        if len(b) > 1:
            bench = b / b.iloc[0]
            bench_metrics = performance(bench)
    elif cfg.benchmark:
        warnings.append(f"資料庫沒有基準 {cfg.benchmark} 的股價，未比較基準")

    metrics = performance(equity)
    reb = pd.DataFrame(reb_rows)
    if not reb.empty:
        # 第一期建倉的 100% 換手不計入平均
        metrics["avg_turnover"] = reb["turnover"].iloc[1:].mean() if len(reb) > 1 else np.nan
        metrics["total_cost"] = reb["cost"].sum()
        metrics["avg_holdings"] = reb["n_holdings"].mean()
    if bench is not None:
        aligned = pd.concat([equity, bench], axis=1, keys=["s", "b"]).dropna()
        rs, rb = aligned["s"].pct_change().dropna(), aligned["b"].pct_change().dropna()
        if rb.var() > 0:
            metrics["beta"] = rs.cov(rb) / rb.var()
        te = (rs - rb).std() * np.sqrt(252)
        metrics["excess_cagr"] = metrics.get("cagr", np.nan) - bench_metrics.get("cagr", np.nan)
        metrics["information_ratio"] = (rs - rb).mean() * 252 / te if te > 0 else np.nan

    return BacktestResult(
        equity=equity, benchmark=bench,
        holdings=pd.DataFrame(holdings_rows, columns=["date", "symbol", "weight", "score"]),
        rebalances=reb, metrics=metrics, benchmark_metrics=bench_metrics, warnings=warnings,
    )


METRIC_LABELS = {
    "total_return": ("總報酬", "pct"),
    "cagr": ("年化報酬 CAGR", "pct"),
    "volatility": ("年化波動度", "pct"),
    "sharpe": ("Sharpe", "num"),
    "sortino": ("Sortino", "num"),
    "max_drawdown": ("最大回撤", "pct"),
    "calmar": ("Calmar", "num"),
    "best_day": ("最佳單日", "pct"),
    "worst_day": ("最差單日", "pct"),
    "avg_turnover": ("平均每期換手率", "pct"),
    "total_cost": ("累計交易成本", "pct"),
    "avg_holdings": ("平均持股數", "num"),
    "beta": ("Beta", "num"),
    "excess_cagr": ("超額年化報酬", "pct"),
    "information_ratio": ("資訊比率", "num"),
}


def format_metric(key: str, value: float) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "—"
    kind = METRIC_LABELS.get(key, (key, "num"))[1]
    return f"{value:.2%}" if kind == "pct" else f"{value:.2f}"
