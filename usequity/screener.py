"""選股：硬性條件篩選 + 多因子加權百分位排名。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

_FILTER_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(<=|>=|==|!=|<|>)\s*(.+?)\s*$")
_OPS = {
    "<": lambda s, v: s < v,
    "<=": lambda s, v: s <= v,
    ">": lambda s, v: s > v,
    ">=": lambda s, v: s >= v,
    "==": lambda s, v: s == v,
    "!=": lambda s, v: s != v,
}


class FilterError(ValueError):
    pass


def parse_filter(expr: str) -> tuple[str, str, float | str | list[str]]:
    """解析「欄位 運算子 值」；值可為數字、字串（加引號）或以 | 分隔的多個字串（僅 == / !=）。

    刻意不用 DataFrame.query / eval，避免設定檔或介面輸入被當成程式碼執行。
    """
    m = _FILTER_RE.match(expr)
    if not m:
        raise FilterError(f"無法解析條件：{expr!r}（格式：欄位 運算子 值，例如 roe > 0.15）")
    col, op, raw = m.groups()
    raw = raw.strip()
    if raw[:1] in "\"'" and raw[-1:] == raw[:1]:
        text = raw[1:-1]
        if op not in ("==", "!="):
            raise FilterError(f"字串只能用 == 或 !=：{expr!r}")
        vals = [t.strip() for t in text.split("|")]
        return col, op, vals if len(vals) > 1 else text
    try:
        return col, op, float(raw)
    except ValueError:
        raise FilterError(f"值必須是數字或加引號的字串：{expr!r}")


def apply_filters(df: pd.DataFrame, filters: list[str]) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    for expr in filters:
        col, op, val = parse_filter(expr)
        if col not in df.columns:
            raise FilterError(f"未知欄位：{col}")
        s = df[col]
        if isinstance(val, list):
            hit = s.isin(val)
            cond = hit if op == "==" else ~hit
        else:
            cond = _OPS[op](s, val)
        # 缺值一律不通過（NaN 比較本來就是 False，但 != 會是 True，需排除）
        mask &= cond.fillna(False).astype(bool) & s.notna()
    return df[mask]


def composite_score(df: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """各因子在候選池內的百分位（0–1），依權重加權平均。

    權重為負代表越小越好。某檔缺某因子時，僅以其有值的因子重新分配權重。
    """
    if df.empty or not weights:
        return pd.Series(np.nan, index=df.index, dtype=float)
    num = pd.Series(0.0, index=df.index)
    den = pd.Series(0.0, index=df.index)
    for col, w in weights.items():
        if not w:
            continue
        if col not in df.columns:
            raise FilterError(f"未知排名因子：{col}")
        r = pd.to_numeric(df[col], errors="coerce").rank(pct=True, ascending=w > 0)
        has = r.notna()
        num[has] += r[has] * abs(w)
        den[has] += abs(w)
    return (num / den).where(den > 0)


@dataclass
class ScreenResult:
    selected: pd.DataFrame
    candidates: pd.DataFrame
    universe_size: int
    filters: list[str] = field(default_factory=list)


def screen(factors: pd.DataFrame, filters: list[str], weights: dict[str, float],
           top_n: int | None = 10) -> ScreenResult:
    cand = apply_filters(factors, filters or []).copy()
    cand["score"] = composite_score(cand, weights or {})
    if weights:
        cand = cand.sort_values("score", ascending=False, na_position="last")
        cand = cand[cand["score"].notna()]
    cand.insert(0, "rank", range(1, len(cand) + 1))
    sel = cand.head(top_n) if top_n else cand
    return ScreenResult(sel, cand, len(factors), list(filters or []))
