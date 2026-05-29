import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    from antimg import data
    from antimg.web import api
    from antimg.web.config import settings

    # synthetic uptrend so the backtest resolves trials without network
    dates = pd.bdate_range("2015-01-01", periods=400)
    price = pd.Series(np.linspace(100, 300, len(dates)), index=dates)
    df = pd.DataFrame({"Open": price, "High": price * 1.01,
                       "Low": price * 0.995, "Close": price,
                       "Volume": 0}, index=dates)
    vix = pd.DataFrame({"Open": 18.0, "High": 18.0, "Low": 18.0,
                        "Close": 18.0, "Volume": 0}, index=dates)  # ~18% IV

    def fake_fetch(ticker="SPY", *a, **k):
        return vix if str(ticker).upper().startswith("^VIX") else df
    monkeypatch.setattr(data, "fetch", fake_fetch)

    # isolate signal store + enable webhook
    api.STORE = api.signals.InMemorySignalStore()
    settings.webhook_secret = "testsecret"
    return TestClient(api.app)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_instruments(client):
    r = client.get("/api/instruments")
    assert "SPY" in [i["ticker"] for g in r.json()["groups"].values() for i in g]


def test_coinflip(client):
    r = client.post("/api/coinflip", json={"iterations": 5000, "target_streak": 8,
                                           "base_bet": 1, "win_prob": 1.0, "seed": 1})
    assert r.status_code == 200
    s = r.json()["stats"]
    assert s["successes"] >= 1


def test_coinflip_validation_caps(client):
    r = client.post("/api/coinflip", json={"iterations": 10**12})
    assert r.status_code == 422  # exceeds max_iterations


def test_backtest_linear(client):
    r = client.post("/api/backtest/linear", json={"ticker": "SPY", "atr_period": 5,
                                                  "base_bet": 100, "target_streak": 10})
    assert r.status_code == 200
    d = r.json()
    assert d["stats"]["n_trials"] > 0
    assert len(d["price"]["x"]) > 0


def test_backtest_options_campaign(client):
    r = client.post("/api/backtest/options", json={"ticker": "SPY", "atr_period": 5,
                                                   "dte_days": 365, "target_delta": 0.5})
    assert r.status_code == 200
    d = r.json()
    assert d["stats"]["n_trials"] > 0 and len(d["table"]) > 0
    # campaign rows carry the entry delta + strike
    assert "delta_entry" in d["table"][0] and "strike" in d["table"][0]


def test_backtest_modes(client):
    for mode in ("pyramid", "scalp"):
        r = client.post("/api/backtest/linear", json={"ticker": "SPY", "atr_period": 5,
                                                      "mode": mode})
        assert r.status_code == 200, mode
        assert len(r.json()["table"]) > 0


def test_webhook_and_from_signals(client):
    # bad secret rejected
    assert client.post("/api/webhook/tradingview",
                       json={"passphrase": "wrong", "ticker": "SPY", "pnl": 5}).status_code == 401
    # good secret, three closed trades
    for pnl in (10, -5, 8):
        r = client.post("/api/webhook/tradingview",
                        json={"passphrase": "testsecret", "ticker": "SPY",
                              "action": "close", "pnl": pnl, "strategy": "s1"})
        assert r.status_code == 200
    assert client.get("/api/signals").json()["count"] == 3
    r = client.post("/api/backtest/from-signals", json={"strategy_id": "s1", "base_bet": 100})
    assert r.status_code == 200
    assert r.json()["stats"]["n_trials"] == 3


def test_webhook_disabled_when_no_secret(client):
    from antimg.web.config import settings
    settings.webhook_secret = ""
    assert client.post("/api/webhook/tradingview", json={"ticker": "X"}).status_code == 503
    settings.webhook_secret = "testsecret"
