from usequity.cli import main


def test_demo_screen_backtest(tmp_path, capsys):
    db = str(tmp_path / "demo.db")
    assert main(["--db", db, "demo"]) == 0
    assert main(["--db", db, "screen", "--top", "3"]) == 0
    assert "DEMO" in capsys.readouterr().out
    assert main(["--db", db, "backtest", "--start", "2020-01-01", "--output", str(tmp_path / "bt")]) == 0
    out = capsys.readouterr().out
    assert "年化報酬" in out and "基準" in out
    assert (tmp_path / "bt" / "equity.csv").exists()


def test_probe_classifies_symbols(monkeypatch, capsys):
    from usequity import cli
    from usequity.fmp import FMPError

    class C:
        calls = 0

        def income_statement(self, sym, period, limit):
            self.calls += 1
            if sym == "BAD":
                raise FMPError("HTTP 402: This value set for 'symbol' is not available", 402)
            return [{"date": "2025-12-31"}]

    monkeypatch.setattr(cli, "_client", lambda cfg: C())
    assert main(["probe", "--symbols", "aapl,BAD,msft"]) == 0
    out = capsys.readouterr().out
    assert "可用 2 檔：AAPL,MSFT" in out and "不開放 1 檔：BAD" in out
