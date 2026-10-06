"""Streamlit 網頁介面：資料爬取 / 選股 / 回測。

啟動：streamlit run app.py
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from usequity.backtest import METRIC_LABELS, BacktestConfig, format_metric, run_backtest
from usequity.config import api_key, load_config
from usequity.crawler import DATASETS, Crawler, resolve_universe
from usequity.factors import FACTOR_DESCRIPTIONS, FactorEngine
from usequity.fmp import FMPClient, FMPError
from usequity.screener import FilterError, screen
from usequity.storage import Store

st.set_page_config(page_title="美股財報選股回測", page_icon="📈", layout="wide")



def _secret(name: str) -> str | None:
    # Streamlit Cloud 的根層級 secrets 也會出現在環境變數；本機沒有 secrets.toml 時 st.secrets 會拋例外
    val = os.environ.get(name)
    if not val:
        try:
            val = st.secrets.get(name)
        except Exception:
            val = None
    return val or None


def _app_password() -> str | None:
    return _secret("APP_PASSWORD")


def require_password() -> None:
    """設定了 APP_PASSWORD 時，需先輸入密碼才顯示內容（公開部署時保護資料）。"""
    expected = _app_password()
    if not expected or st.session_state.get("authed"):
        return
    st.title("📈 美股選股回測")
    with st.form("login"):
        pw = st.text_input("密碼", type="password")
        ok = st.form_submit_button("進入")
    if ok and hmac.compare_digest(pw.encode(), expected.encode()):
        st.session_state["authed"] = True
        st.rerun()
    if ok:
        st.error("密碼錯誤")
    st.stop()


require_password()

CFG = load_config(os.environ.get("USEQUITY_CONFIG"))


def ensure_decrypted_db(path: str) -> str | None:
    """公開分支上只有加密檔 usequity.db.enc；尚未解密過這個版本時以 DB_KEY 解密。回傳錯誤訊息或 None。

    以旁邊的 .stamp 記錄已解密的加密檔版本（大小 + 修改時間），不能只比明文檔的修改時間：
    未設定金鑰時介面仍會建立空的資料庫檔，之後設好金鑰也會被誤判為「已是最新」。
    """
    db = Path(path)
    enc = db.with_name(db.name + ".enc")
    if not enc.exists():
        return None
    st_ = enc.stat()
    stamp = db.with_name(db.name + ".stamp")
    sig = f"{st_.st_size}:{st_.st_mtime_ns}"
    if db.exists() and stamp.exists() and stamp.read_text() == sig:
        return None
    key = _secret("DB_KEY")
    if not key:
        return "資料庫已加密，請在 Streamlit 的 Settings → Secrets 設定 DB_KEY（與 GitHub Secrets 相同）"
    from usequity.crypto import KeyError_, decrypt_file
    try:
        decrypt_file(enc, db, key)
    except KeyError_ as e:
        return str(e)
    stamp.write_text(sig)
    return None


_db_error = ensure_decrypted_db(CFG["storage"]["db_path"])
if _db_error:
    st.error(_db_error)
st.session_state.setdefault("db_path", CFG["storage"]["db_path"])


# ---------------------------------------------------------------- 共用
def db_path() -> str:
    return st.session_state["db_path"]


@st.cache_resource(show_spinner="載入資料與計算因子…")
def get_engine(path: str, lag: int, benchmark: str | None, _mtime: float) -> FactorEngine:
    # _mtime 讓資料庫更新後自動失效快取
    return FactorEngine(Store(path), filing_lag_days=lag, benchmark=benchmark)


def engine() -> FactorEngine | None:
    p = Path(db_path())
    if not p.exists():
        return None
    eng = get_engine(str(p), int(CFG["backtest"]["filing_lag_days"]), CFG["crawl"].get("benchmark"),
                     p.stat().st_mtime)
    return None if eng.fundamentals.empty else eng


def parse_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def parse_weights(text: str) -> dict[str, float]:
    out = {}
    for ln in parse_lines(text):
        k, _, v = ln.partition(":")
        if not v:
            raise FilterError(f"權重格式應為「因子: 權重」：{ln!r}")
        out[k.strip()] = float(v)
    return out


def strategy_inputs(prefix: str):
    s = CFG["screen"]
    c1, c2 = st.columns(2)
    with c1:
        filters = st.text_area(
            "篩選條件（每行一條，全部需成立）",
            value="\n".join(s.get("filters") or []), height=150, key=f"{prefix}_filters",
            help='格式：欄位 運算子 值。例如 roe > 0.15、sector == "Technology"、'
                 'sector != "Energy|Utilities"',
        )
    with c2:
        weights = st.text_area(
            "排名權重（因子: 權重；負值代表越小越好）",
            value="\n".join(f"{k}: {v}" for k, v in (s.get("rank") or {}).items()), height=150,
            key=f"{prefix}_weights",
        )
    return parse_lines(filters), parse_weights(weights)


def fmt_table(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "market_cap" in df:
        df["market_cap"] = df["market_cap"] / 1e9
    return df.rename(columns={"market_cap": "市值(十億)"})


PCT_COLS = ["earnings_yield", "fcf_yield", "roe", "roa", "gross_margin", "operating_margin",
            "net_margin", "revenue_growth", "eps_growth", "momentum_12_1", "return_1m", "volatility_1y"]


def column_config(df: pd.DataFrame) -> dict:
    cfg = {}
    for c in df.columns:
        if c in PCT_COLS:
            cfg[c] = st.column_config.NumberColumn(c, format="percent", help=FACTOR_DESCRIPTIONS.get(c))
        elif c in FACTOR_DESCRIPTIONS or c in ("score", "市值(十億)"):
            cfg[c] = st.column_config.NumberColumn(c, format="%.2f", help=FACTOR_DESCRIPTIONS.get(c))
    return cfg


# ---------------------------------------------------------------- 頁面
def page_data():
    st.header("📥 資料爬取")
    store = Store(db_path())
    s = store.summary()
    c = st.columns(4)
    c[0].metric("可選股代號", s["usable"], help=f"同時有財報與股價的代號；資料庫共記錄 {s['symbols']} 個代號")
    c[1].metric("財報列數", f"{s['statement_rows']:,}")
    c[2].metric("股價列數", f"{s['price_rows']:,}")
    c[3].metric("抓取錯誤", s["errors"])
    if s["price_range"][0]:
        st.caption(f"股價區間：{s['price_range'][0]} ~ {s['price_range'][1]}　資料庫：`{store.path}`")

    st.subheader("從 Financial Modeling Prep 抓取")
    key = api_key()
    key_in = st.text_input("FMP API 金鑰", value="", type="password",
                           help="建議改設環境變數 FMP_API_KEY；此欄只在本次工作階段使用，不會存檔",
                           placeholder="已從環境變數讀取" if key else "尚未設定")
    key = key_in or key
    crawl = CFG["crawl"]
    c1, c2, c3 = st.columns([1, 1, 2])
    universe = c1.selectbox("股票池", ["list", "sp500", "nasdaq100", "dowjones"],
                            index=["list", "sp500", "nasdaq100", "dowjones"].index(crawl.get("universe", "list")))
    period = c2.selectbox("財報期別", ["quarter", "annual"],
                          index=0 if crawl.get("period") == "quarter" else 1,
                          help="季報可算 TTM、較即時；免費方案可能只能抓年報")
    datasets = c3.multiselect("資料集", list(DATASETS), default=list(DATASETS))
    symbols_text = st.text_area("代號清單（股票池選 list 時使用，逗號或換行分隔）",
                                value=", ".join(crawl.get("symbols") or []), disabled=universe != "list")
    c1, c2 = st.columns(2)
    limit = c1.number_input("只抓前 N 檔（0 = 全部）", min_value=0, value=0)
    force = c2.checkbox("忽略快取，全部重抓")

    if st.button("開始抓取", type="primary", disabled=not key):
        cc = dict(crawl, universe=universe, period=period,
                  symbols=[x for x in symbols_text.replace("\n", ",").split(",") if x.strip()])
        try:
            client = FMPClient.from_config(CFG, key)
            syms = resolve_universe(client, cc)
        except FMPError as e:
            st.error(f"無法取得股票池：{e}")
            return
        if limit:
            syms = syms[: int(limit)]
        bar = st.progress(0.0, text="準備中…")
        rep = Crawler(client, store, cc).run(
            syms, tuple(datasets), force=force,
            progress=lambda i, n, sym: bar.progress(i / n, text=f"[{i}/{n}] {sym}"))
        bar.empty()
        st.success(f"完成：寫入 {rep.fetched}，快取略過 {rep.skipped}，API 請求 {rep.api_calls} 次")
        for n in rep.notes:
            st.info(n)
        if rep.errors:
            with st.expander(f"⚠ {len(rep.errors)} 筆錯誤"):
                st.code("\n".join(rep.errors))
        st.cache_resource.clear()
    if not key:
        st.info("請輸入金鑰或設定 FMP_API_KEY。免費金鑰：https://site.financialmodelingprep.com/developer/docs")

    with st.expander("抓取紀錄"):
        st.dataframe(store.fetch_log(), width="stretch", hide_index=True)


def page_screen():
    st.header("🔎 選股")
    eng = engine()
    if eng is None:
        st.warning("資料庫沒有財報資料，請先到「資料爬取」頁抓取，或在側欄載入示範資料。")
        return
    last = eng.prices.index.max().date()
    c1, c2 = st.columns([1, 1])
    asof = c1.date_input("基準日（以當日已公開財報計算）", value=last,
                         min_value=eng.prices.index.min().date(), max_value=last)
    top_n = c2.number_input("選出檔數", 1, 200, int(CFG["screen"]["top_n"]))
    try:
        filters, weights = strategy_inputs("scr")
        fac = eng.at(pd.Timestamp(asof))
        res = screen(fac, filters, weights, int(top_n))
    except (FilterError, ValueError) as e:
        st.error(str(e))
        return
    st.caption(f"股票池 {res.universe_size} 檔 → 通過條件 {len(res.candidates)} 檔 → 選出 {len(res.selected)} 檔")

    front = ["rank", "name", "sector", "score", "price", "market_cap"]
    cols = [c for c in front if c in res.candidates] + \
           [c for c in res.candidates.columns if c not in front and c not in ("industry", "fiscal_date")]
    t1, t2, t3 = st.tabs(["選出結果", "全部候選", "因子說明"])
    with t1:
        df = fmt_table(res.selected[cols])
        st.dataframe(df, width="stretch", column_config=column_config(df))
    with t2:
        df = fmt_table(res.candidates[cols])
        st.dataframe(df, width="stretch", column_config=column_config(df))
        st.download_button("下載 CSV", res.candidates.to_csv().encode("utf-8-sig"),
                           f"screen_{asof}.csv", "text/csv")
    with t3:
        st.table(pd.DataFrame({"說明": FACTOR_DESCRIPTIONS}).rename_axis("欄位"))
        st.markdown("另有文字欄位 `name`、`sector`、`industry` 可用於 `==` / `!=` 條件（產業別為現況，非點時資料）。")


def page_backtest():
    st.header("📊 回測")
    eng = engine()
    if eng is None:
        st.warning("資料庫沒有財報資料，請先抓取或載入示範資料。")
        return
    b = CFG["backtest"]
    lo, hi = eng.prices.index.min().date(), eng.prices.index.max().date()
    c = st.columns(6)
    start = c[0].date_input("起始日", value=max(lo, pd.Timestamp(b["start"]).date()), min_value=lo, max_value=hi)
    end = c[1].date_input("結束日", value=hi, min_value=lo, max_value=hi)
    reb = c[2].selectbox("再平衡", ["monthly", "quarterly", "annual"],
                         index=["monthly", "quarterly", "annual"].index(b["rebalance"]))
    weighting = c[3].selectbox("權重", ["equal", "rank"], index=0 if b["weighting"] == "equal" else 1,
                               help="equal：等權；rank：依綜合分數加權")
    top_n = c[4].number_input("持股數", 1, 200, int(CFG["screen"]["top_n"]))
    cost = c[5].number_input("單邊成本 (bps)", 0.0, 200.0, float(b["cost_bps"]))
    try:
        filters, weights = strategy_inputs("bt")
    except (FilterError, ValueError) as e:
        st.error(str(e))
        return

    cfg = BacktestConfig(start=str(start), end=str(end), rebalance=reb, weighting=weighting,
                         cost_bps=cost, top_n=int(top_n), filters=filters, rank=weights,
                         benchmark=CFG["crawl"].get("benchmark"))
    if st.button("執行回測", type="primary"):
        bar = st.progress(0.0)
        try:
            st.session_state["bt_result"] = (
                cfg, run_backtest(eng, cfg, progress=lambda i, n, d: bar.progress(i / n, text=f"再平衡 {d.date()}")))
        except (FilterError, ValueError) as e:
            st.error(str(e))
            return
        finally:
            bar.empty()
    if "bt_result" not in st.session_state:
        st.info("設定完成後按「執行回測」。")
        return
    # 結果存在 session_state，按下載鈕等造成重繪時不會消失
    shown_cfg, res = st.session_state["bt_result"]
    if shown_cfg != cfg:
        st.caption("⚠ 參數已變更，以下仍是上次的回測結果，請重新執行。")
    cfg = shown_cfg
    for w in res.warnings:
        st.warning(w)

    keys = ["cagr", "volatility", "sharpe", "max_drawdown", "excess_cagr", "avg_turnover"]
    cols = st.columns(len(keys))
    for col, k in zip(cols, keys):
        if k in res.metrics:
            col.metric(METRIC_LABELS[k][0], format_metric(k, res.metrics[k]))
            if res.benchmark_metrics and k in res.benchmark_metrics:
                col.caption(f"基準 {format_metric(k, res.benchmark_metrics[k])}")

    fig = go.Figure()
    fig.add_scatter(x=res.equity.index, y=res.equity, name="策略")
    if res.benchmark is not None:
        fig.add_scatter(x=res.benchmark.index, y=res.benchmark, name=f"基準 {cfg.benchmark}")
    fig.update_layout(title="淨值曲線（起始 = 1）", yaxis_type="log", height=420,
                      margin=dict(l=10, r=10, t=40, b=10), legend=dict(orientation="h"))
    st.plotly_chart(fig, width="stretch")

    dd = res.equity / res.equity.cummax() - 1
    fig2 = go.Figure(go.Scatter(x=dd.index, y=dd, fill="tozeroy", name="回撤"))
    fig2.update_layout(title="回撤", yaxis_tickformat=".0%", height=250, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig2, width="stretch")

    t1, t2, t3, t4 = st.tabs(["績效指標", "年度報酬", "每期持股", "再平衡紀錄"])
    with t1:
        rows = []
        for k, (label, _) in METRIC_LABELS.items():
            if k in res.metrics:
                bm = res.benchmark_metrics.get(k) if res.benchmark_metrics else None
                rows.append({"指標": label, "策略": format_metric(k, res.metrics[k]),
                             "基準": format_metric(k, bm) if bm is not None else ""})
        st.table(pd.DataFrame(rows).set_index("指標"))
    with t2:
        yr = res.equity.resample("YE").last().pct_change()
        yr.iloc[0] = res.equity.resample("YE").last().iloc[0] / res.equity.iloc[0] - 1
        ytab = pd.DataFrame({"策略": yr})
        if res.benchmark is not None:
            by = res.benchmark.resample("YE").last()
            byr = by.pct_change()
            byr.iloc[0] = by.iloc[0] / res.benchmark.iloc[0] - 1
            ytab["基準"] = byr
            ytab["超額"] = ytab["策略"] - ytab["基準"]
        ytab.index = ytab.index.year
        st.dataframe(ytab.style.format("{:.2%}"), width="stretch")
    with t3:
        h = res.holdings.copy()
        h["date"] = h["date"].dt.date
        st.dataframe(h, width="stretch", hide_index=True,
                     column_config={"weight": st.column_config.NumberColumn(format="percent")})
        st.download_button("下載持股 CSV", res.holdings.to_csv(index=False).encode("utf-8-sig"),
                           "holdings.csv", "text/csv")
    with t4:
        r = res.rebalances.copy()
        r["date"] = r["date"].dt.date
        st.dataframe(r, width="stretch", hide_index=True,
                     column_config={"turnover": st.column_config.NumberColumn(format="percent"),
                                    "cost": st.column_config.NumberColumn(format="percent")})
    st.caption("⚠ 股票池為現今成分股，有倖存者偏差；以收盤價成交、未計滑價。結果僅供研究參考。")


# ---------------------------------------------------------------- 主程式
with st.sidebar:
    st.title("📈 美股選股回測")
    page = st.radio("功能", ["資料爬取", "選股", "回測"], label_visibility="collapsed")
    st.divider()
    st.text_input("資料庫路徑", key="db_path")

    def load_demo():
        # 以 callback 執行：在元件重繪前改寫 db_path 才不會觸發 Streamlit 的例外
        from usequity.demo import generate_demo
        demo = Path(CFG["storage"]["db_path"]).with_name("demo.db")
        if demo.exists():
            demo.unlink()
        generate_demo(Store(demo))
        st.session_state["db_path"] = str(demo)
        st.cache_resource.clear()

    with st.expander("示範資料（免金鑰）"):
        st.caption("產生 40 檔虛構公司 + SPY 的合成資料，寫入 data/demo.db 並切換過去。")
        st.button("產生並切換", on_click=load_demo)
    if Path(db_path()).exists():
        s = Store(db_path()).summary()
        if s["price_range"][1]:
            st.caption(f"資料至 {s['price_range'][1]}　可選股 {s['usable']} 檔")
    st.caption("資料來源：[Financial Modeling Prep](https://site.financialmodelingprep.com/developer/docs)")

{"資料爬取": page_data, "選股": page_screen, "回測": page_backtest}[page]()
