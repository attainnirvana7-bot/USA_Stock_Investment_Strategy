"""Financial Modeling Prep stable API 用戶端。

文件：https://site.financialmodelingprep.com/developer/docs
所有端點皆位於 https://financialmodelingprep.com/stable/ 之下，以 apikey 參數認證。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

import requests

log = logging.getLogger(__name__)

# 永久性錯誤不重試：重試只會浪費額度
NON_RETRY_STATUSES = {400, 401, 402, 403, 404, 410, 422}
RETRY_STATUSES = {429, 500, 502, 503, 504}


class FMPError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, endpoint: str = ""):
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint

    @property
    def daily_limit(self) -> bool:
        """已達方案每日請求上限（FMP 回「Limit Reach」）：當天再試都沒用。"""
        return "limit reach" in str(self).lower()

    @property
    def symbol_unsupported(self) -> bool:
        """方案不開放這個代號（「This value set for 'symbol' is not available」）。"""
        return self.status == 402 and "'symbol'" in str(self)


class RateLimiter:
    """滑動視窗節流：60 秒內最多 N 次請求。"""

    def __init__(self, per_minute: int):
        self.per_minute = max(1, int(per_minute))
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= 60:
                self._calls.popleft()
            if len(self._calls) >= self.per_minute:
                sleep_for = 60 - (now - self._calls[0]) + 0.05
                log.debug("節流：等待 %.1f 秒", sleep_for)
                time.sleep(sleep_for)
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 60:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


class FMPClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://financialmodelingprep.com/stable",
        requests_per_minute: int = 250,
        timeout: float = 30,
        max_retries: int = 3,
        session: requests.Session | None = None,
    ):
        if not api_key:
            raise FMPError("未設定 FMP_API_KEY")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.limiter = RateLimiter(requests_per_minute)
        self.calls = 0

    @classmethod
    def from_config(cls, cfg: dict, api_key: str) -> "FMPClient":
        f = cfg["fmp"]
        return cls(
            api_key,
            base_url=f["base_url"],
            requests_per_minute=f["requests_per_minute"],
            timeout=f["timeout"],
            max_retries=f["max_retries"],
        )

    # ------------------------------------------------------------------ 基礎
    def get(self, endpoint: str, **params: Any) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        query = {k: v for k, v in params.items() if v is not None}
        query["apikey"] = self.api_key
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            self.calls += 1
            try:
                resp = self.session.get(url, params=query, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = e
                log.warning("%s 連線失敗（第 %d 次）：%s", endpoint, attempt + 1, e)
                self._backoff(attempt)
                continue

            if resp.status_code in NON_RETRY_STATUSES:
                raise FMPError(
                    f"{endpoint} HTTP {resp.status_code}: {_error_text(resp)}",
                    resp.status_code,
                    endpoint,
                )
            if resp.status_code == 429 and "limit reach" in resp.text.lower():
                raise FMPError(f"{endpoint} HTTP 429: {_error_text(resp)}", 429, endpoint)
            if resp.status_code in RETRY_STATUSES:
                last_err = FMPError(f"HTTP {resp.status_code}", resp.status_code, endpoint)
                retry_after = resp.headers.get("Retry-After")
                log.warning("%s HTTP %d（第 %d 次）", endpoint, resp.status_code, attempt + 1)
                self._backoff(attempt, retry_after)
                continue
            if resp.status_code != 200:
                raise FMPError(
                    f"{endpoint} HTTP {resp.status_code}: {_error_text(resp)}",
                    resp.status_code,
                    endpoint,
                )

            try:
                data = resp.json()
            except ValueError:
                raise FMPError(f"{endpoint} 回應非 JSON：{resp.text[:200]}", 200, endpoint)
            # FMP 在某些錯誤（方案不支援、金鑰錯誤）時仍回 200 並附 "Error Message"
            if isinstance(data, dict) and ("Error Message" in data or "error" in data):
                msg = data.get("Error Message") or data.get("error")
                raise FMPError(f"{endpoint}: {msg}", resp.status_code, endpoint)
            return data
        raise FMPError(f"{endpoint} 重試 {self.max_retries} 次後仍失敗：{last_err}", None, endpoint)

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None = None) -> None:
        delay = 2 ** attempt
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(min(delay, 60))

    # ------------------------------------------------------------------ 端點
    def profile(self, symbol: str) -> dict | None:
        data = self.get("profile", symbol=symbol)
        return data[0] if isinstance(data, list) and data else None

    def income_statement(self, symbol: str, period: str = "annual", limit: int = 40) -> list[dict]:
        return self.get("income-statement", symbol=symbol, period=period, limit=limit) or []

    def balance_sheet(self, symbol: str, period: str = "annual", limit: int = 40) -> list[dict]:
        return self.get("balance-sheet-statement", symbol=symbol, period=period, limit=limit) or []

    def cash_flow(self, symbol: str, period: str = "annual", limit: int = 40) -> list[dict]:
        return self.get("cash-flow-statement", symbol=symbol, period=period, limit=limit) or []

    def key_metrics_ttm(self, symbol: str) -> dict | None:
        data = self.get("key-metrics-ttm", symbol=symbol)
        return data[0] if isinstance(data, list) and data else None

    def ratios_ttm(self, symbol: str) -> dict | None:
        data = self.get("ratios-ttm", symbol=symbol)
        return data[0] if isinstance(data, list) and data else None

    def historical_prices(self, symbol: str, start: str | None = None, end: str | None = None) -> list[dict]:
        """還原股利的日線（adjClose），回測用。"""
        data = self.get("historical-price-eod/dividend-adjusted", symbol=symbol, **{"from": start, "to": end})
        if isinstance(data, dict):  # 舊格式 {"symbol":..., "historical":[...]}
            data = data.get("historical", [])
        return data or []

    def historical_market_cap(self, symbol: str, start: str | None = None, end: str | None = None,
                              limit: int = 5000) -> list[dict]:
        """每日歷史市值；比「股價 × 財報股數」更不受分割、庫藏股影響。"""
        return self.get("historical-market-capitalization", symbol=symbol, limit=limit,
                        **{"from": start, "to": end}) or []

    def constituents(self, index: str) -> list[dict]:
        endpoint = {
            "sp500": "sp500-constituent",
            "nasdaq100": "nasdaq-constituent",
            "dowjones": "dowjones-constituent",
        }[index]
        return self.get(endpoint) or []

    def company_screener(self, **filters: Any) -> list[dict]:
        """FMP 伺服器端篩選，例如 marketCapMoreThan=1e10, sector="Technology", exchange="NASDAQ"。"""
        return self.get("company-screener", **filters) or []


def _error_text(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return str(data.get("Error Message") or data.get("message") or data)[:300]
    except ValueError:
        pass
    return resp.text[:300]
