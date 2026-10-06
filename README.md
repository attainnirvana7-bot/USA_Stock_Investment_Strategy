# USA_EQUITY_INVESTMENT_STRATEGY

美股財報資料爬蟲、多因子選股與回測工具，資料來源為
[Financial Modeling Prep（FMP）stable API](https://site.financialmodelingprep.com/developer/docs)。

- **爬蟲**：三大財報（季／年）、還原股價、歷史市值、公司資料 → 本機 SQLite，有快取有效期、節流、重試
- **選股**：點時因子（估值、獲利、體質、成長、動能）+ 硬性篩選 + 加權百分位排名
- **回測**：月／季／年再平衡、等權或分數加權、交易成本、對比 SPY 基準
- **介面**：Streamlit 網頁（`app.py`）與命令列（`python -m usequity.cli`）

## 快速開始

```bash
pip install -r requirements.txt
export FMP_API_KEY=你的金鑰          # https://site.financialmodelingprep.com/developer/docs

python -m usequity.cli check-api     # 先確認方案支援哪些端點
python -m usequity.cli probe --symbols AAPL,PG   # 測試代號是否在方案內（每檔 1 次請求）
python -m usequity.cli crawl         # 依 config.yaml 抓取
python -m usequity.cli screen        # 最新選股結果
python -m usequity.cli backtest      # 回測

streamlit run app.py                 # 網頁介面
```

沒有金鑰也可以先試用：`python -m usequity.cli --db data/demo.db demo` 產生合成資料
（40 檔虛構公司 + SPY），或在網頁側欄按「產生並切換」。

## 在 GitHub Actions 上執行

1. **Settings → Secrets and variables → Actions → New repository secret**，
   新增 `FMP_API_KEY`（FMP 金鑰）與 `DB_KEY`（自訂的長密語，用來加密發布到 streamlit 分支的資料庫；
   未設定時只發布程式碼、不含資料）
2. **Actions → FMP 選股回測 → Run workflow**，可指定代號、財報期別、資料集
3. 執行完在該次 run 的 **Summary** 看 API 權限、選股與回測結果；
   頁面底部 **Artifacts** 可下載 CSV 與資料庫

資料庫以 Actions 快取跨次累積，快取有效期內的資料不會重抓。

**排程**：每週三、六 08:17（台北時間）自動執行，結果 commit 到 `reports/YYYY/YYYY-MM-DD.md`
（附選股 CSV），累積成歷史紀錄。手動執行時勾選 `save_report` 也會存。

主要觸發是 cron-job.org 呼叫 `workflow_dispatch`，body 為 `{"ref":"main","inputs":{"scheduled":"true"}}`
（設定見 ops-hub 的 `CRON_JOBS.md`）；`scheduled=true` 的行為與排程完全相同（略過 API 權限檢查、一定回測、一定存報告）。
GitHub 內建排程（週三、六 09:27、11:47）只當備援：`guard` job 發現 12 小時內已有成功的排程執行（run 標題帶「· 排程」）就略過，
不會重複耗用 FMP 額度。手動執行（`scheduled` 預設 false）不受影響。

**額度**：免費方案每日約 250 次請求。預設 40 檔在穩定後每次排程約 80–200 次
（週六重抓財報）。方案不開放的代號會自動記錄、30 天內不再嘗試；
達每日上限時爬蟲會停止，已抓的資料都在快取中，下次執行會接續。
`workflow_dispatch` 只在 workflow 檔位於預設分支（main）時才會出現在 Actions 頁面。

## 部署到 Streamlit Community Cloud

GitHub Actions 每次排程執行後，會把程式碼 + **加密後**的資料庫（`data/usequity.db.enc`）
發布到 `streamlit` 分支（單一 commit 強制覆寫，不會在歷史中累積）。repo 是公開的，
明文資料庫不會進入 git，也不會放進 Actions artifact；Streamlit 啟動時以 `DB_KEY` 解密。
main 的程式碼更新時，
「發布 Streamlit」workflow 也會以快取中的資料庫重新發布。Streamlit 部署這個分支，
分支一更新就會自動重新部署。

1. 到 https://share.streamlit.io 用 GitHub 登入，授權存取私人 repo
2. **Create app → Deploy a public app from GitHub**（repo 為私人時 app 預設也是私人）
   - Repository：`attainnirvana7-bot/USA_EQUITY_INVESTMENT_STRATEGY`
   - Branch：`streamlit`
   - Main file path：`app.py`
3. 展開 **Advanced settings → Secrets**，填入以下兩行，再按 **Deploy**：
   ```toml
   APP_PASSWORD = "介面登入密碼"
   DB_KEY = "資料庫解密密語（至少 16 字元，需與 GitHub Secrets 的 DB_KEY 相同）"
   ```
4. 之後在 app 的 **Settings → Sharing** 設定誰可以檢視

Community Cloud 每個 workspace 只能有一個私人 app。額度已被占用時，可把這個 app 設為公開：
repo 本身仍是私人的，而設定了 `APP_PASSWORD` 後，沒有密碼就看不到任何內容。
沒設定 `APP_PASSWORD` 時不需登入（本機使用）。

介面上的「資料爬取」頁在雲端也能用，但寫入的資料在 app 重啟後就會消失，
而且會消耗同一份 API 額度，所以不建議在 Streamlit 設定 `FMP_API_KEY`；
資料更新交給 GitHub Actions 排程即可。

## 使用的 FMP 端點

| 用途 | 端點（皆在 `/stable/` 下） |
|---|---|
| 損益表 | `income-statement?symbol=&period=quarter\|annual&limit=` |
| 資產負債表 | `balance-sheet-statement` |
| 現金流量表 | `cash-flow-statement` |
| 還原股價 | `historical-price-eod/dividend-adjusted?symbol=&from=` |
| 歷史市值 | `historical-market-capitalization?symbol=&from=` |
| 公司資料（產業別） | `profile?symbol=` |
| 成分股 | `sp500-constituent` / `nasdaq-constituent` / `dowjones-constituent` |

免費方案實測限制（2026-09）：每日約 250 次請求；財報每次最多 5 期；歷史市值不接受 `from`；
成分股清單不開放。爬蟲偵測到前兩項會自動調整（財報降到 5 期、市值只抓近期），
因此免費方案建議 `period: annual`（5 年年報，回測約可從 2021 年起）。`check-api` 會逐一列出。
一檔股票完整抓取約 6 次請求（公司資料 1、財報 3、股價 1、市值 1），S&P 500 全抓約 3,000 次。
沒權限的資料集可用 `--datasets profile,statements,prices` 跳過，市值會改以「價格 × 稀釋股數」估算。

## 可用因子

| 欄位 | 說明 |
|---|---|
| `market_cap`, `price` | 市值、收盤價 |
| `pe`, `earnings_yield`, `pb`, `ps`, `ev_ebitda`, `fcf_yield` | 估值（以近四季 TTM 計） |
| `roe`, `roa`, `gross_margin`, `operating_margin`, `net_margin` | 獲利能力 |
| `debt_to_equity`, `current_ratio` | 財務體質 |
| `revenue_growth`, `eps_growth` | 近四季 vs 前四季年增率 |
| `momentum_12_1`, `return_1m`, `volatility_1y` | 價格因子 |
| `sector`, `industry`, `name` | 文字欄位，可用 `==` / `!=`，以 `\|` 分隔多值 |
| `fundamental_age_days` | 所用財報距基準日天數 |

篩選條件格式為「欄位 運算子 值」，例如：

```yaml
filters:
  - "market_cap > 10e9"
  - "roe > 0.15"
  - 'sector != "Energy|Utilities"'
rank:                 # 正值越大越好、負值越小越好
  earnings_yield: 1
  momentum_12_1: 1
  volatility_1y: -0.5
```

條件是自行解析的，不經 `eval`，網頁輸入不會被當成程式碼執行。

## 重要設計

**點時資料，避免前視偏差。** 每筆財報以 `filingDate`（SEC 申報日）作為可用日，
缺值時以報告期結束日 + `filing_lag_days`（預設 45 天）估計；三張報表都公開後才採用。
超過 400 天沒有新財報的公司會被排除。

**季資料轉 TTM。** 損益與現金流量加總近四季（四季跨度超過約 10 個月即視為缺季，不計算），
資產負債取最新一季。同一檔同時有季與年資料時優先用季資料。

**股價每次抓完整區間。** 還原股價在每次配息後整段歷史都會被修正，增量接上會在接縫處產生假報酬。

**永久性錯誤不重試。** 400/401/402/403/404 直接記錄；401（金鑰錯誤）會中止整批抓取。
FMP 有時以 HTTP 200 回傳 `{"Error Message": ...}`，同樣視為錯誤。單一代號、單一資料集失敗
不影響其他項目，下次執行只會重抓失敗或過期的部分。

## 回測的已知限制

- **倖存者偏差**：股票池是現在的成分股，已下市或被剔除的公司不在其中，歷史績效會偏樂觀。
- 以再平衡日收盤價成交，未計滑價與流動性。
- 產業別是現況，不是點時資料。
- 結果僅供研究參考，不構成投資建議。

## 結構

```
config.yaml            股票池、選股條件、回測參數
.github/workflows/     GitHub Actions：選股回測（排程 / 手動）、發布 Streamlit
scripts/               發布 streamlit 分支的腳本
app.py                 Streamlit 介面
usequity/
  fmp/client.py        FMP API 用戶端（節流、重試、錯誤處理）
  storage.py           SQLite 存取
  crawler.py           爬蟲（快取有效期、錯誤隔離）
  factors.py           點時因子計算
  screener.py          篩選與排名
  backtest.py          回測引擎與績效指標
  cli.py               命令列
  demo.py              合成示範資料
tests/                 pytest（不需網路與金鑰）
```

```bash
python -m pytest -q
```
