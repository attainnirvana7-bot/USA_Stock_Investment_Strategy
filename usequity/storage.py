"""SQLite 本機資料庫：財報、股價、公司資料與抓取紀錄。

財報以 JSON 原樣保存（FMP 欄位會隨版本增減），讀取時再展開成 DataFrame。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

import pandas as pd

STATEMENT_KINDS = ("income", "balance", "cashflow")

SCHEMA = """
CREATE TABLE IF NOT EXISTS statements (
    symbol       TEXT NOT NULL,
    kind         TEXT NOT NULL,      -- income / balance / cashflow
    period       TEXT NOT NULL,      -- FY / Q1..Q4
    fiscal_date  TEXT NOT NULL,      -- 報告期結束日
    filing_date  TEXT,               -- 向 SEC 申報日（點時回測用）
    data         TEXT NOT NULL,
    PRIMARY KEY (symbol, kind, fiscal_date, period)
);
CREATE TABLE IF NOT EXISTS prices (
    symbol    TEXT NOT NULL,
    date      TEXT NOT NULL,
    close     REAL,
    adj_close REAL,
    volume    REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS market_caps (
    symbol     TEXT NOT NULL,
    date       TEXT NOT NULL,
    market_cap REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS profiles (
    symbol TEXT PRIMARY KEY,
    data   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fetch_log (
    symbol     TEXT NOT NULL,
    dataset    TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    status     TEXT NOT NULL,
    message    TEXT,
    PRIMARY KEY (symbol, dataset)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ 寫入
    def save_statements(self, symbol: str, kind: str, rows: Iterable[dict]) -> int:
        assert kind in STATEMENT_KINDS
        recs = []
        for r in rows:
            fiscal = r.get("date")
            if not fiscal:
                continue
            filing = r.get("filingDate") or r.get("fillingDate") or r.get("acceptedDate")
            recs.append((
                symbol, kind, r.get("period") or "FY", fiscal[:10],
                filing[:10] if filing else None, json.dumps(r),
            ))
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO statements VALUES (?,?,?,?,?,?)", recs)
        return len(recs)

    def save_prices(self, symbol: str, rows: Iterable[dict]) -> int:
        recs = []
        for r in rows:
            d = r.get("date")
            if not d:
                continue
            close = r.get("close", r.get("adjClose"))
            adj = r.get("adjClose", close)
            recs.append((symbol, d[:10], close, adj, r.get("volume")))
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?)", recs)
        return len(recs)

    def save_market_caps(self, symbol: str, rows: Iterable[dict]) -> int:
        recs = [(symbol, r["date"][:10], r.get("marketCap")) for r in rows if r.get("date")]
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO market_caps VALUES (?,?,?)", recs)
        return len(recs)

    def save_profile(self, symbol: str, data: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO profiles VALUES (?,?)", (symbol, json.dumps(data)))

    def log_fetch(self, symbol: str, dataset: str, status: str, message: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)",
                (symbol, dataset, _utcnow().isoformat(), status, message[:500]),
            )

    # ------------------------------------------------------------------ 讀取
    def is_fresh(self, symbol: str, dataset: str, ttl_hours: float) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT fetched_at, status FROM fetch_log WHERE symbol=? AND dataset=?",
                (symbol, dataset),
            ).fetchone()
        if not row or row[1] != "ok":
            return False
        return _utcnow() - datetime.fromisoformat(row[0]) < timedelta(hours=ttl_hours)

    def mark_unsupported(self, symbol: str, message: str) -> None:
        """記錄方案不開放的代號，並清掉它先前的錯誤紀錄（原因已明確，不必重複列出）。"""
        with self._conn() as c:
            c.execute("DELETE FROM fetch_log WHERE symbol=? AND status='error'", (symbol,))
        self.log_fetch(symbol, "_symbol", "unsupported", message)

    def is_unsupported(self, symbol: str, days: float = 30) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT fetched_at FROM fetch_log WHERE symbol=? AND dataset='_symbol' AND status='unsupported'",
                (symbol,),
            ).fetchone()
        return bool(row) and _utcnow() - datetime.fromisoformat(row[0]) < timedelta(days=days)

    def last_price_date(self, symbol: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT MAX(date) FROM prices WHERE symbol=?", (symbol,)).fetchone()
        return row[0] if row else None

    def symbols(self) -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT symbol FROM statements UNION SELECT DISTINCT symbol FROM profiles"
            ).fetchall()
        return sorted(r[0] for r in rows)

    def statements(self, kind: str, symbols: list[str] | None = None) -> pd.DataFrame:
        """回傳展開後的財報表，附 symbol / fiscal_date / filing_date / period 欄。"""
        sql = "SELECT symbol, period, fiscal_date, filing_date, data FROM statements WHERE kind=?"
        params: list = [kind]
        if symbols:
            sql += f" AND symbol IN ({','.join('?' * len(symbols))})"
            params += list(symbols)
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        if not rows:
            return pd.DataFrame(columns=["symbol", "period", "fiscal_date", "filing_date"])
        recs = []
        for sym, period, fiscal, filing, data in rows:
            d = json.loads(data)
            d.update(symbol=sym, period=period, fiscal_date=fiscal, filing_date=filing)
            recs.append(d)
        df = pd.DataFrame.from_records(recs)
        df["fiscal_date"] = pd.to_datetime(df["fiscal_date"])
        df["filing_date"] = pd.to_datetime(df["filing_date"])
        return df.sort_values(["symbol", "fiscal_date"]).reset_index(drop=True)

    def prices(self, symbols: list[str] | None = None, field: str = "adj_close") -> pd.DataFrame:
        """寬表：index=日期，columns=代號。"""
        assert field in ("close", "adj_close", "volume")
        sql = f"SELECT symbol, date, {field} FROM prices"
        params: list = []
        if symbols:
            sql += f" WHERE symbol IN ({','.join('?' * len(symbols))})"
            params = list(symbols)
        with self._conn() as c:
            df = pd.read_sql_query(sql, c, params=params)
        if df.empty:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        return df.pivot(index="date", columns="symbol", values=field).sort_index()

    def market_caps(self, symbols: list[str] | None = None) -> pd.DataFrame:
        sql = "SELECT symbol, date, market_cap FROM market_caps"
        params: list = []
        if symbols:
            sql += f" WHERE symbol IN ({','.join('?' * len(symbols))})"
            params = list(symbols)
        with self._conn() as c:
            df = pd.read_sql_query(sql, c, params=params)
        if df.empty:
            return pd.DataFrame()
        df["date"] = pd.to_datetime(df["date"])
        return df.pivot(index="date", columns="symbol", values="market_cap").sort_index()

    def profiles(self) -> pd.DataFrame:
        with self._conn() as c:
            rows = c.execute("SELECT symbol, data FROM profiles").fetchall()
        if not rows:
            return pd.DataFrame(columns=["symbol"])
        return pd.DataFrame.from_records([{**json.loads(d), "symbol": s} for s, d in rows])

    def fetch_log(self) -> pd.DataFrame:
        with self._conn() as c:
            return pd.read_sql_query("SELECT * FROM fetch_log ORDER BY fetched_at DESC", c)

    def summary(self) -> dict:
        with self._conn() as c:
            return {
                "symbols": len(self.symbols()),
                # 同時有財報與股價、可用於選股的代號（不含只留下公司資料或「不開放」紀錄的代號、基準指數）
                "usable": c.execute(
                    "SELECT COUNT(DISTINCT symbol) FROM statements "
                    "WHERE symbol IN (SELECT DISTINCT symbol FROM prices)").fetchone()[0],
                "statement_rows": c.execute("SELECT COUNT(*) FROM statements").fetchone()[0],
                "price_rows": c.execute("SELECT COUNT(*) FROM prices").fetchone()[0],
                "price_range": c.execute("SELECT MIN(date), MAX(date) FROM prices").fetchone(),
                "errors": c.execute("SELECT COUNT(*) FROM fetch_log WHERE status='error'").fetchone()[0],
                "unsupported": c.execute(
                    "SELECT COUNT(*) FROM fetch_log WHERE status='unsupported'").fetchone()[0],
            }
