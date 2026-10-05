"""命令列介面。

  python -m usequity.cli check-api                 驗證 FMP 金鑰與各端點權限
  python -m usequity.cli crawl [--symbols AAPL,MSFT] [--force]
  python -m usequity.cli status                    資料庫概況
  python -m usequity.cli screen [--top 10] [--date 2024-06-30]
  python -m usequity.cli backtest [--start 2018-01-01] [--rebalance monthly]
  python -m usequity.cli demo                      產生合成示範資料（免金鑰）
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from .backtest import METRIC_LABELS, BacktestConfig, format_metric, run_backtest
from .config import api_key, load_config
from .crawler import DATASETS, Crawler, resolve_universe
from .factors import FactorEngine
from .fmp import FMPClient, FMPError
from .screener import screen
from .storage import Store

log = logging.getLogger("usequity")

SCREEN_COLUMNS = ["rank", "name", "sector", "price", "market_cap", "pe", "pb", "roe",
                  "gross_margin", "revenue_growth", "fcf_yield", "momentum_12_1", "score"]


def _client(cfg) -> FMPClient:
    key = api_key()
    if not key:
        sys.exit("請先設定環境變數 FMP_API_KEY（https://site.financialmodelingprep.com/developer/docs）")
    return FMPClient.from_config(cfg, key)


def cmd_check_api(cfg, args) -> int:
    client = _client(cfg)
    sym = args.symbol
    lim = int(cfg["crawl"]["statement_limit"])
    start = cfg["crawl"]["price_start"]
    checks = [
        ("profile", lambda: client.profile(sym)),
        ("income-statement (annual)", lambda: client.income_statement(sym, "annual", 1)),
        ("income-statement (quarter)", lambda: client.income_statement(sym, "quarter", 1)),
        ("balance-sheet-statement", lambda: client.balance_sheet(sym, "annual", 1)),
        ("cash-flow-statement", lambda: client.cash_flow(sym, "annual", 1)),
        ("historical-price-eod/dividend-adjusted", lambda: client.historical_prices(sym, start=start)),
        ("historical-market-capitalization", lambda: client.historical_market_cap(sym, limit=5)),
        ("sp500-constituent", lambda: client.constituents("sp500")),
    ]
    # 以爬蟲實際使用的參數測試；受限時 crawl 會自動調整，這裡只是讓你知道方案上限
    limited = [
        (f"財報 limit={lim}", lambda: client.income_statement(sym, "annual", lim)),
        (f"歷史市值 from={start}", lambda: client.historical_market_cap(sym, start=start)),
    ]
    ok = True
    for name, fn in checks:
        try:
            data = fn()
            n = len(data) if isinstance(data, list) else (1 if data else 0)
            print(f"  ✅ {name:42s} {n} 筆")
        except FMPError as e:
            ok = False
            print(f"  ❌ {name:42s} {e}")
    print("\n方案參數上限（受限時 crawl 會自動調降，不影響執行）：")
    for name, fn in limited:
        try:
            data = fn()
            print(f"  ✅ {name:42s} {len(data)} 筆")
        except FMPError as e:
            print(f"  ⚠ {name:42s} {str(e)[:160]}")
    print("\n❌ 端點代表目前方案不支援或金鑰有誤。" if not ok else "\n端點全部可用。")
    return 0 if ok else 1


def cmd_probe(cfg, args) -> int:
    """每個代號只花 1 次請求（年報 limit=1），判斷目前方案是否開放。"""
    client = _client(cfg)
    syms = resolve_universe(None, {"universe": "list", "symbols": args.symbols.split(",")})
    ok, no, other = [], [], []
    for sym in syms:
        try:
            rows = client.income_statement(sym, "annual", 1)
            (ok if rows else other).append(sym)
            print(f"  ✅ {sym}" if rows else f"  ❔ {sym}：無資料")
        except FMPError as e:
            if e.daily_limit:
                print(f"  ⛔ 已達每日請求上限，停在 {sym}")
                break
            if e.symbol_unsupported:
                no.append(sym)
                print(f"  ❌ {sym}：方案不開放")
            else:
                other.append(sym)
                print(f"  ❔ {sym}：{str(e)[:120]}")
    print(f"\n可用 {len(ok)} 檔：{','.join(ok)}")
    print(f"不開放 {len(no)} 檔：{','.join(no)}")
    if other:
        print(f"無法判斷 {len(other)} 檔：{','.join(other)}")
    print(f"API 請求 {client.calls} 次")
    return 0


def cmd_crawl(cfg, args) -> int:
    client = _client(cfg)
    store = Store(cfg["storage"]["db_path"])
    crawl_cfg = dict(cfg["crawl"])
    if args.symbols:
        crawl_cfg["universe"], crawl_cfg["symbols"] = "list", args.symbols.split(",")
    if args.period:
        crawl_cfg["period"] = args.period
    symbols = resolve_universe(client, crawl_cfg)
    if args.limit:
        symbols = symbols[: args.limit]
    datasets = tuple(args.datasets.split(",")) if args.datasets else DATASETS
    print(f"股票池 {len(symbols)} 檔，資料集：{', '.join(datasets)}")

    def progress(i, n, sym):
        # 終端機覆寫同一行；GitHub Actions 等非終端環境逐行輸出，log 才讀得懂
        if sys.stdout.isatty():
            print(f"\r[{i}/{n}] {sym:8s}", end="", flush=True)
        else:
            print(f"[{i}/{n}] {sym}", flush=True)

    rep = Crawler(client, store, crawl_cfg).run(symbols, datasets, force=args.force, progress=progress)
    print(f"\n完成：寫入 {rep.fetched}，略過（快取有效）{rep.skipped}，API 請求 {rep.api_calls} 次")
    for n in rep.notes:
        print(f"  ℹ {n}")
    if rep.unsupported:
        print(f"  ℹ 方案不開放、已略過 {len(rep.unsupported)} 檔：{', '.join(rep.unsupported)}")
    if rep.aborted:
        print(f"  ⛔ 中途停止：{rep.aborted}。已抓的資料已存檔，下次執行會接續未完成的部分")
    for e in rep.errors[:20]:
        print(f"  ⚠ {e}")
    if len(rep.errors) > 20:
        print(f"  …共 {len(rep.errors)} 筆錯誤，詳見 status")
    return 0 if not rep.errors else 2


def cmd_status(cfg, args) -> int:
    store = Store(cfg["storage"]["db_path"])
    s = store.summary()
    print(f"資料庫：{store.path}")
    print(f"  可選股代號 {s['usable']}（資料庫共 {s['symbols']} 個代號），財報列數 {s['statement_rows']}，股價列數 {s['price_rows']}")
    print(f"  股價區間 {s['price_range'][0]} ~ {s['price_range'][1]}，抓取錯誤 {s['errors']} 筆，"
          f"方案不開放代號 {s['unsupported']} 檔")
    errs = store.fetch_log()
    errs = errs[errs["status"] == "error"]
    if not errs.empty:
        print(errs.head(20).to_string(index=False))
    return 0


def _engine(cfg) -> FactorEngine:
    store = Store(cfg["storage"]["db_path"])
    eng = FactorEngine(store, filing_lag_days=int(cfg["backtest"]["filing_lag_days"]),
                       benchmark=cfg["crawl"].get("benchmark"))
    if eng.fundamentals.empty:
        sys.exit("資料庫沒有財報資料，請先執行 crawl（或 demo）")
    return eng


def cmd_screen(cfg, args) -> int:
    eng = _engine(cfg)
    fac = eng.at(args.date) if args.date else eng.latest()
    res = screen(fac, cfg["screen"]["filters"], cfg["screen"]["rank"], args.top or cfg["screen"]["top_n"])
    asof = args.date or eng.prices.index.max().date()
    print(f"基準日 {asof}：股票池 {res.universe_size} 檔，通過條件 {len(res.candidates)} 檔")
    cols = [c for c in SCREEN_COLUMNS if c in res.selected.columns]
    with pd.option_context("display.width", 200, "display.max_columns", 30,
                           "display.float_format", "{:,.3f}".format):
        out = res.selected[cols].copy()
        if "market_cap" in out:
            out["market_cap"] = (out["market_cap"] / 1e9).round(1)
            out = out.rename(columns={"market_cap": "mcap_B"})
        print(out.to_string())
    if args.output:
        res.candidates.to_csv(args.output)
        print(f"已輸出 {args.output}")
    return 0


def cmd_backtest(cfg, args) -> int:
    eng = _engine(cfg)
    bt = BacktestConfig.from_config(cfg, start=args.start, end=args.end, rebalance=args.rebalance,
                                    top_n=args.top, weighting=args.weighting, cost_bps=args.cost_bps)
    tty = sys.stdout.isatty()
    res = run_backtest(eng, bt, progress=(lambda i, n, d: print(f"\r再平衡 {i}/{n} {d.date()}", end="", flush=True))
                       if tty else None)
    if tty:
        print()
    for w in res.warnings:
        print(f"⚠ {w}")
    print(f"{'指標':16s}{'策略':>12s}{'基準':>12s}")
    for k, (label, _) in METRIC_LABELS.items():
        if k not in res.metrics:
            continue
        b = format_metric(k, res.benchmark_metrics.get(k)) if res.benchmark_metrics and k in res.benchmark_metrics else ""
        print(f"{label:16s}{format_metric(k, res.metrics[k]):>12s}{b:>12s}")
    if args.output:
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        pd.concat([res.equity.rename("strategy"),
                   res.benchmark.rename("benchmark") if res.benchmark is not None else None],
                  axis=1).to_csv(out / "equity.csv")
        res.holdings.to_csv(out / "holdings.csv", index=False)
        res.rebalances.to_csv(out / "rebalances.csv", index=False)
        print(f"已輸出至 {out}/")
    return 0


def cmd_demo(cfg, args) -> int:
    from .demo import generate_demo
    path = Path(args.db or cfg["storage"]["db_path"])
    if path.exists() and not args.overwrite:
        sys.exit(f"{path} 已存在；加 --overwrite 覆蓋，或用 --db 指定其他路徑")
    if path.exists():
        path.unlink()
    syms = generate_demo(Store(path))
    print(f"已產生 {len(syms)} 檔合成公司 + SPY 至 {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="usequity", description="美股財報爬蟲 / 選股 / 回測")
    p.add_argument("-c", "--config", help="設定檔路徑（預設 config.yaml）")
    p.add_argument("--db", help="覆寫資料庫路徑")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("check-api", help="驗證 API 金鑰與端點權限")
    s.add_argument("--symbol", default="AAPL")

    s = sub.add_parser("probe", help="測試代號是否在目前方案內（每檔 1 次請求）")
    s.add_argument("--symbols", required=True, help="逗號分隔")

    s = sub.add_parser("crawl", help="抓取財報與股價")
    s.add_argument("--symbols", help="逗號分隔，覆寫設定檔股票池")
    s.add_argument("--period", choices=["annual", "quarter"])
    s.add_argument("--datasets", help=f"逗號分隔：{','.join(DATASETS)}")
    s.add_argument("--limit", type=int, help="只抓前 N 檔（測試用）")
    s.add_argument("--force", action="store_true", help="忽略快取有效期")

    sub.add_parser("status", help="資料庫概況")

    s = sub.add_parser("screen", help="依設定檔條件選股")
    s.add_argument("--date", help="基準日（預設最新）")
    s.add_argument("--top", type=int)
    s.add_argument("--output", help="候選清單輸出 CSV")

    s = sub.add_parser("backtest", help="回測")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--rebalance", choices=["monthly", "quarterly", "annual"])
    s.add_argument("--weighting", choices=["equal", "rank"])
    s.add_argument("--top", type=int)
    s.add_argument("--cost-bps", type=float)
    s.add_argument("--output", help="結果輸出目錄")

    s = sub.add_parser("demo", help="產生合成示範資料")
    s.add_argument("--overwrite", action="store_true")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.db:
        cfg["storage"]["db_path"] = args.db
    handlers = {"check-api": cmd_check_api, "probe": cmd_probe, "crawl": cmd_crawl, "status": cmd_status,
                "screen": cmd_screen, "backtest": cmd_backtest, "demo": cmd_demo}
    return handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
