from unittest.mock import MagicMock

import pytest

from usequity.crawler import Crawler, resolve_universe
from usequity.fmp import client as client_mod
from usequity.fmp import FMPClient, FMPError


def resp(status, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.headers = {}
    r.text = text
    r.json.return_value = json_data
    return r


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def make_client(*responses):
    session = MagicMock()
    session.get.side_effect = list(responses)
    return FMPClient("KEY", session=session, max_retries=2), session


def test_passes_apikey_and_params():
    c, s = make_client(resp(200, [{"symbol": "AAPL"}]))
    assert c.profile("AAPL") == {"symbol": "AAPL"}
    url, kw = s.get.call_args[0][0], s.get.call_args[1]
    assert url.endswith("/stable/profile")
    assert kw["params"] == {"symbol": "AAPL", "apikey": "KEY"}


def test_error_message_in_200_body():
    c, _ = make_client(resp(200, {"Error Message": "Invalid API KEY"}))
    with pytest.raises(FMPError, match="Invalid API KEY"):
        c.profile("AAPL")


def test_permanent_error_not_retried():
    c, s = make_client(resp(402, {"Error Message": "Premium"}), resp(200, []))
    with pytest.raises(FMPError) as e:
        c.income_statement("AAPL", "quarter")
    assert e.value.status == 402 and s.get.call_count == 1


def test_retry_on_429():
    c, s = make_client(resp(429), resp(200, [{"date": "2024-01-02"}]))
    assert c.historical_prices("AAPL") == [{"date": "2024-01-02"}]
    assert s.get.call_count == 2


def test_empty_key_rejected():
    with pytest.raises(FMPError):
        FMPClient("")


class FakeClient:
    def __init__(self, fail=None):
        self.calls = 0
        self.fail = fail or {}

    def _hit(self, name, sym):
        self.calls += 1
        if (name, sym) in self.fail:
            raise FMPError("boom", self.fail[(name, sym)])

    def profile(self, sym):
        self._hit("profile", sym)
        return {"symbol": sym, "sector": "Tech"}

    def income_statement(self, sym, period, limit):
        self._hit("statements", sym)
        return [{"date": "2024-03-31", "period": "Q1", "filingDate": "2024-05-01", "revenue": 1}]

    balance_sheet = cash_flow = income_statement

    def historical_prices(self, sym, start=None):
        self._hit("prices", sym)
        return [{"date": "2024-01-02", "close": 1, "adjClose": 1}]

    def historical_market_cap(self, sym, start=None):
        self._hit("marketcap", sym)
        return [{"date": "2024-01-02", "marketCap": 100}]


def test_crawler_ttl_and_isolation(store):
    fc = FakeClient(fail={("statements", "BBB"): 500})
    cr = Crawler(fc, store, {"benchmark": "SPY", "ttl_hours": {"statements": 24, "prices": 24, "profile": 24}})
    rep = cr.run(["AAA", "BBB"])
    # BBB 財報失敗不影響其股價與 AAA
    assert len(rep.errors) == 1 and "BBB/statements" in rep.errors[0]
    assert store.last_price_date("BBB") == "2024-01-02"
    assert store.last_price_date("SPY") == "2024-01-02"
    assert set(store.statements("income")["symbol"]) == {"AAA"}
    # 第二次：快取有效者略過，只重試失敗的
    calls = fc.calls
    rep2 = cr.run(["AAA", "BBB"])
    assert fc.calls - calls == 1
    assert rep2.skipped == 9 - 1 + 0  # 2 檔 × 4 資料集 + SPY 股價 = 9，其中 1 個重抓


def test_crawler_aborts_on_401(store):
    fc = FakeClient(fail={("profile", "AAA"): 401})
    rep = Crawler(fc, store, {}).run(["AAA", "BBB"])
    assert fc.calls == 1 and rep.errors


def test_resolve_universe_normalizes():
    assert resolve_universe(None, {"universe": "list", "symbols": ["aapl", "BRK.B", "AAPL"]}) == ["AAPL", "BRK-B"]


class LimitedPlanClient(FakeClient):
    """模擬免費方案：財報 limit 上限 5、市值不接受 from。"""

    def income_statement(self, sym, period, limit):
        self.calls += 1
        if limit > 5:
            raise FMPError("income-statement HTTP 402: Premium Query Parameter: 'Special Parameters : "
                           "The values for 'limit' must be between 0 and 5 based on your current subscription.", 402)
        return [{"date": f"{2025 - i}-12-31", "period": "FY", "filingDate": f"{2026 - i}-02-01", "revenue": 1}
                for i in range(limit)]

    balance_sheet = cash_flow = income_statement

    def historical_market_cap(self, sym, start=None):
        self.calls += 1
        if start:
            raise FMPError("HTTP 402: Premium Query Parameter: 'Special Endpoint : This value set for 'from' "
                           "is not available under your current subscription", 402)
        return [{"date": "2026-09-29", "marketCap": 100}]


def test_crawler_adapts_to_plan_limits(store):
    fc = LimitedPlanClient()
    rep = Crawler(fc, store, {"statement_limit": 40, "period": "annual"}).run(["AAA", "BBB"])
    assert rep.errors == []
    assert len(store.statements("income", ["BBB"])) == 5
    assert store.market_caps().shape == (1, 2)
    assert len(rep.notes) == 2
    # 偵測到限制後，第二檔不再撞 402：AAA 財報 1 次失敗 + 3 次成功、市值 2 次；BBB 財報 3 次、市值 1 次
    assert fc.calls == 2 + 4 + 2 + 2 + 3 + 1


def test_daily_limit_not_retried():
    r = resp(429, {"Error Message": "Limit Reach . Please upgrade your plan"})
    r.text = '{"Error Message": "Limit Reach . Please upgrade your plan"}'
    c, s = make_client(r, resp(200, []))
    with pytest.raises(FMPError) as e:
        c.profile("AAPL")
    assert e.value.daily_limit and s.get.call_count == 1


SYMBOL_402 = ("HTTP 402: Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' "
              "is not available under your current subscription")


def test_unsupported_symbol_skipped_next_time(store):
    fc = FakeClient(fail={("statements", "PG"): 402})

    def hit(name, sym):
        fc.calls += 1
        if (name, sym) in fc.fail:
            raise FMPError(SYMBOL_402, 402)
    fc._hit = hit
    cr = Crawler(fc, store, {})
    rep = cr.run(["AAA", "PG"])
    # PG：profile 成功、財報 402 → 標記不開放並跳過其餘資料集，不算錯誤
    assert rep.unsupported == ["PG"] and rep.errors == []
    assert store.last_price_date("PG") is None
    assert store.summary()["unsupported"] == 1
    calls = fc.calls
    rep2 = Crawler(fc, store, {}).run(["AAA", "PG"], force=False)
    assert rep2.unsupported == ["PG"]
    assert fc.calls == calls  # AAA 快取有效、PG 直接略過：0 次請求


def test_daily_limit_aborts_crawl(store):
    fc = FakeClient(fail={("prices", "AAA"): 429})

    def hit(name, sym):
        fc.calls += 1
        if (name, sym) in fc.fail:
            raise FMPError("HTTP 429: Limit Reach . Please upgrade your plan", 429)
    fc._hit = hit
    rep = Crawler(fc, store, {}).run(["AAA", "BBB"])
    assert rep.aborted and "BBB" not in store.symbols()


def test_summary_usable_excludes_unsupported(store):
    fc = FakeClient()

    def hit(name, sym):
        fc.calls += 1
        if sym == "PG" and name == "statements":
            raise FMPError(SYMBOL_402, 402)
    fc._hit = hit
    Crawler(fc, store, {"benchmark": "SPY"}).run(["AAA", "PG"])
    s = store.summary()
    # PG 只留下公司資料、SPY 只有股價：都不算可選股
    assert s["usable"] == 1 and s["symbols"] == 2
