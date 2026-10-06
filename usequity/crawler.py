"""財報與股價爬蟲：依股票池逐檔抓取，支援快取有效期與增量更新。"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable

from .fmp import FMPClient, FMPError
from .storage import Store

log = logging.getLogger(__name__)

DATASETS = ("profile", "statements", "prices", "marketcap")
ProgressFn = Callable[[int, int, str], None]


@dataclass
class CrawlReport:
    symbols: int = 0
    fetched: dict[str, int] = field(default_factory=dict)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # 依方案限制自動調整的紀錄
    unsupported: list[str] = field(default_factory=list)  # 方案不開放的代號
    aborted: str = ""                                 # 中途停止的原因（金鑰錯誤、達每日上限）
    api_calls: int = 0


def resolve_universe(client: FMPClient | None, crawl_cfg: dict) -> list[str]:
    universe = crawl_cfg.get("universe", "list")
    if universe == "list":
        syms = crawl_cfg.get("symbols") or []
    else:
        if client is None:
            raise FMPError("需要 API 金鑰才能取得成分股清單")
        syms = [r["symbol"] for r in client.constituents(universe) if r.get("symbol")]
    # FMP 以 '-' 表示股票類別（BRK-B），統一大寫並去重、保序
    seen, out = set(), []
    for s in syms:
        s = str(s).strip().upper().replace(".", "-")
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


class Crawler:
    def __init__(self, client: FMPClient, store: Store, crawl_cfg: dict):
        self.client = client
        self.store = store
        self.cfg = crawl_cfg
        self.ttl = crawl_cfg.get("ttl_hours", {})
        # 方案限制在同一次執行中不會變，偵測到一次就套用到後續所有代號，避免每檔都撞一次 402
        self.limit_cap: int | None = None
        self.mcap_without_from = False
        self._notes: list[str] = []

    def run(
        self,
        symbols: list[str],
        datasets: tuple[str, ...] = DATASETS,
        force: bool = False,
        progress: ProgressFn | None = None,
    ) -> CrawlReport:
        rep = CrawlReport(symbols=len(symbols))
        calls_before = self.client.calls
        bench = self.cfg.get("benchmark")
        todo = list(symbols)
        if bench and "prices" in datasets and bench not in todo:
            todo.append(bench)

        for i, sym in enumerate(todo, 1):
            if progress:
                progress(i, len(todo), sym)
            # 先前已確認方案不開放的代號：30 天內不再嘗試，省下額度
            if not force and self.store.is_unsupported(sym):
                rep.unsupported.append(sym)
                continue
            is_bench_only = sym == bench and sym not in symbols
            for ds in datasets:
                if is_bench_only and ds != "prices":
                    continue
                # 每個資料集獨立 try/except：單一失敗不影響其他
                try:
                    n = self._fetch(sym, ds, force)
                except FMPError as e:
                    if e.symbol_unsupported:
                        log.warning("%s：目前方案不開放此代號，略過", sym)
                        self.store.mark_unsupported(sym, str(e))
                        rep.unsupported.append(sym)
                        break
                    msg = f"{sym}/{ds}: {e}"
                    log.warning(msg)
                    rep.errors.append(msg)
                    self.store.log_fetch(sym, ds, "error", str(e))
                    if e.status == 401 or e.daily_limit:
                        # 金鑰無效或已達每日上限：後續請求必然失敗，直接中止。
                        # 已抓到的資料都有快取，下次執行會從未完成的部分接續。
                        rep.aborted = "API 金鑰無效" if e.status == 401 else "已達方案每日請求上限"
                        rep.api_calls = self.client.calls - calls_before
                        rep.notes = list(self._notes)
                        return rep
                    continue
                if n is None:
                    rep.skipped += 1
                else:
                    rep.fetched[ds] = rep.fetched.get(ds, 0) + n
                    self.store.log_fetch(sym, ds, "ok", f"{n} rows")
        rep.api_calls = self.client.calls - calls_before
        rep.notes = list(self._notes)
        return rep

    def _statements(self, fn, sym: str, period: str, limit: int) -> list[dict]:
        if self.limit_cap is not None:
            limit = min(limit, self.limit_cap)
        try:
            return fn(sym, period, limit)
        except FMPError as e:
            # 免費方案：「The values for 'limit' must be between 0 and 5」→ 降到上限重試
            m = re.search(r"limit'? must be between 0 and (\d+)", str(e))
            if e.status != 402 or not m or int(m.group(1)) >= limit:
                raise
            self.limit_cap = int(m.group(1))
            self._notes.append(f"方案限制財報每次最多 {self.limit_cap} 期，已自動調降 limit")
            log.info(self._notes[-1])
            return fn(sym, period, self.limit_cap)

    def _fetch(self, sym: str, ds: str, force: bool) -> int | None:
        ttl_key = "prices" if ds == "marketcap" else ds
        if not force and self.store.is_fresh(sym, ds, self.ttl.get(ttl_key, 24)):
            return None
        if ds == "profile":
            p = self.client.profile(sym)
            if p:
                self.store.save_profile(sym, p)
            return 1 if p else 0
        if ds == "statements":
            period = self.cfg.get("period", "quarter")
            limit = int(self.cfg.get("statement_limit", 40))
            n = 0
            for kind, fn in (("income", self.client.income_statement),
                             ("balance", self.client.balance_sheet),
                             ("cashflow", self.client.cash_flow)):
                n += self.store.save_statements(sym, kind, self._statements(fn, sym, period, limit))
            return n
        if ds == "prices":
            # 每次抓完整區間而非增量：還原股價在每次配息後整段歷史都會被修正，
            # 增量接上會在接縫處產生假報酬。反正一檔一次請求，額度相同。
            start = self.cfg.get("price_start", "2014-01-01")
            return self.store.save_prices(sym, self.client.historical_prices(sym, start=start))
        if ds == "marketcap":
            start = None if self.mcap_without_from else self.cfg.get("price_start", "2014-01-01")
            try:
                rows = self.client.historical_market_cap(sym, start=start)
            except FMPError as e:
                # 免費方案不接受 from 參數：改抓預設的近期區間，更早的市值由 價格 × 股數 估算
                if e.status != 402 or start is None or "'from'" not in str(e):
                    raise
                self.mcap_without_from = True
                self._notes.append("方案不支援歷史市值的起始日參數，只抓近期市值；更早期間以 價格 × 股數 估算")
                log.info(self._notes[-1])
                rows = self.client.historical_market_cap(sym)
            return self.store.save_market_caps(sym, rows)
        raise ValueError(f"未知資料集：{ds}")
