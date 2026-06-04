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


def test_hedged_intraday(client):
    r = client.post("/api/hedged-intraday", json={"ticker": "SPY", "atr_period": 10,
                                                  "start": "2015-01-01", "dte_days": 30})
    assert r.status_code == 200, r.text
    d = r.json()
    s = d["stats"]
    assert s["n_days"] > 0 and d["table"]
    assert s["total_theta"] <= 0.0                       # long straddle pays theta
    assert {"straddle_pnl", "scalp_pnl", "scalp_covers_theta_pct", "ann_return_pct"} <= set(s)
    assert {"equity_total", "equity_straddle", "equity_scalp", "theta_path"} <= set(d)


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


def test_backtest_options_coinflip(client):
    # the long-call coin-flip model is selectable on the options backtest endpoint
    r = client.post("/api/backtest/options", json={"ticker": "SPY", "atr_period": 5,
                                                   "opt_model": "coinflip", "double_target": 2.0,
                                                   "target_streak": 4, "base_bet": 100})
    assert r.status_code == 200
    d = r.json()
    assert d["stats"]["model"].startswith("long-call coin-flip")
    assert len(d["table"]) > 0 and "pnl" in d["table"][0]
    # risk-capped by construction even WITH costs (netted into the roll): loss ≤ b·(1+fee)
    assert all(row["pnl"] >= -101 for row in d["table"])


def test_backtest_modes(client):
    for mode in ("pyramid", "scalp"):
        r = client.post("/api/backtest/linear", json={"ticker": "SPY", "atr_period": 5,
                                                      "mode": mode})
        assert r.status_code == 200, mode
        assert len(r.json()["table"]) > 0


def test_scan_all(client):
    r = client.post("/api/scan", json={"atr_period": 5, "base_bet": 100,
                                       "target_streak": 10, "starting_bank": 10000})
    assert r.status_code == 200
    d = r.json()
    from antimg import instruments
    # one row per catalog instrument (^VIX gets flat data → ATR 0 → legitimately no trials)
    assert len(d["results"]) == len(instruments.flat_with_group())
    assert d["summary"]["ok"] >= d["summary"]["total"] - 1
    row = next(x for x in d["results"] if x["ticker"] == "SPY")
    assert row["ok"] and row["n_campaigns"] > 0
    assert "ret_pct" in row and "profit_factor" in row and "group" in row
    # every instrument that resolved trades a synthetic uptrend → all profitable
    assert d["summary"]["profitable"] == d["summary"]["ok"]
    assert d["summary"]["best"]["ticker"] and d["summary"]["worst"]["ticker"]


def test_explain_scenarios(client):
    # uptrend → target win with scale-ins; flat/down → −b stop. Trace invariant: risk ≡ b.
    expect = {"uptrend": "target", "flat": "stop", "downtrend": "stop"}
    for sc, reason in expect.items():
        r = client.post("/api/explain", json={"scenario": sc, "target_streak": 4, "base_bet": 100})
        assert r.status_code == 200, sc
        d = r.json()
        assert d["exit"]["reason"] == reason, sc
        # every traced step keeps whole-stack risk at exactly the base bet b
        for ev in d["trace"]:
            if "risk" in ev:
                assert abs(ev["risk"] - 100) < 1e-6, (sc, ev)
        if sc == "uptrend":
            assert d["exit"]["pnl"] > 0 and any(e["t"] == "add" for e in d["trace"])
        else:
            assert abs(d["exit"]["pnl"] + 100) < 1e-6   # exactly −b


def test_explain_calls_coinflip(client):
    # calls = long-call coin-flip: premium is the bet, risk ≤ b by construction, exact b(2^N−1)
    d = client.post("/api/explain", json={"scenario": "uptrend", "instrument": "calls",
                                          "target_delta": 0.5, "base_bet": 100,
                                          "target_streak": 4, "double_target": 2.0}).json()
    assert d["model"] == "coinflip"
    rounds = d["rounds"]
    assert len(rounds) == 4 and all(r["win"] for r in rounds)
    # stakes roll by the double_target each win: 100 → 200 → 400 → 800
    assert [round(r["stake"]) for r in rounds] == [100, 200, 400, 800]
    # each round reports a DYNAMICALLY-solved doubling level in ATR (not a fixed 2.0)
    assert all(r["m_atr"] > 0 for r in rounds)
    # win streak of N pays exactly b(2^N − 1)
    assert abs(d["cf_exit"]["pnl"] - 100 * (2 ** 4 - 1)) < 1e-6
    # losing scenarios: risk is capped at the premium b (loss ≥ −b)
    for sc in ("downtrend", "flat"):
        dd = client.post("/api/explain", json={"scenario": sc, "instrument": "calls",
                                               "base_bet": 100}).json()
        assert -100 <= dd["cf_exit"]["pnl"] < 0


def test_call_coinflip_risk_capped_direct():
    # the engine itself: no cycle ever loses more than the base bet b
    from antimg import scenarios, data as datamod, atr_strategy as strat
    for sc in ("uptrend", "downtrend", "flat"):
        daily = scenarios.scenario(sc, atr_period=4, target_streak=4)
        weekly = datamod.weekly(daily)
        watr = datamod.atr(weekly, 4)
        res = strat.run_call_coinflip(daily, weekly, watr, base_bet=100, target_streak=4,
                                      mult=1.0, double_target=2.0, target_delta=0.5,
                                      dte_days=45, iv=0.20)
        for row in res.table:
            assert row["pnl"] >= -100 - 1e-6, (sc, row)


def test_explain_trace_invariant_direct():
    # the engine's own trace, not the API: risk stays = b through a full pyramid
    from antimg import scenarios, data as datamod, atr_strategy as strat
    daily = scenarios.scenario("uptrend", atr_period=4, target_streak=4)
    weekly = datamod.weekly(daily)
    watr = datamod.atr(weekly, 4)
    trace = []
    strat.run_campaign(daily, weekly, watr, base_bet=100, target_streak=4, mult=1.0,
                       instrument="shares", mode="pyramid", trace=trace)
    adds = [e for e in trace if e["t"] == "add" and e["camp"] == 1]
    assert [a["lots_added"] for a in adds] == [2, 4, 8, 16]    # doubling ladder
    assert all(abs(a["risk"] - 100) < 1e-6 for a in adds)
    # molecular money: entry deploys b/h units = $2000 notional to risk $100; notional grows
    entry = next(e for e in trace if e["t"] == "entry" and e["camp"] == 1)
    assert abs(entry["units"] - 20) < 1e-6 and abs(entry["notional"] - 2000) < 1e-6
    assert [a["notional"] for a in adds] == sorted(a["notional"] for a in adds)  # monotone up
    assert [a["unreal"] for a in adds] == [100, 400, 1100, 2600]


def test_scan_coinflip(client, monkeypatch):
    # the scan can sweep the long-call coin-flip model too. Use a tiny catalog + short series
    # (the synthetic monotone uptrend would otherwise spawn hundreds of winning cycles).
    from antimg import data, instruments
    monkeypatch.setattr(instruments, "CATALOG", {"t": [("AAA", "x"), ("BBB", "y")]})
    dates = pd.bdate_range("2018-01-01", periods=90)
    price = pd.Series(np.linspace(100, 140, len(dates)), index=dates)
    df = pd.DataFrame({"Open": price, "High": price * 1.01, "Low": price * 0.995,
                       "Close": price, "Volume": 0}, index=dates)
    monkeypatch.setattr(data, "fetch", lambda *a, **k: df)
    r = client.post("/api/scan", json={"model": "coinflip", "atr_period": 4, "target_streak": 2,
                                       "dte_days": 30, "double_target": 2.0, "iv_markup": 1.25,
                                       "base_bet": 100, "starting_bank": 10000})
    assert r.status_code == 200
    d = r.json()
    assert d["params"]["model"] == "coinflip"
    ok = [x for x in d["results"] if x["ok"]]
    assert ok and all("ret_pct" in x and "profit_factor" in x for x in ok)


def test_detrend_zeroes_drift():
    # _detrend must remove the net drift (mean log-return ~ 0) while keeping the series length
    # and starting price; this is the control's whole correctness premise.
    from antimg.web.api import _detrend
    dates = pd.bdate_range("2018-01-01", periods=300)
    price = pd.Series(100 * np.exp(np.linspace(0, 0.8, len(dates))), index=dates)  # strong uptrend
    df = pd.DataFrame({"Open": price, "High": price * 1.01, "Low": price * 0.99,
                       "Close": price, "Volume": 1.0}, index=dates)
    out = _detrend(df)
    assert len(out) == len(df) and abs(out["Close"].iloc[0] - df["Close"].iloc[0]) < 1e-6
    mean_logret = np.diff(np.log(out["Close"].to_numpy(float))).mean()
    assert abs(mean_logret) < 1e-9                    # drift stripped
    # intraday range preserved (High/Low scaled by the same factor as Close)
    assert np.allclose(out["High"] / out["Close"], df["High"] / df["Close"])


def test_scan_stress_fields(client, monkeypatch):
    # stress=True adds the drift-stripped control (+ breakeven markup for coinflip) per row
    # and aggregate control stats in the summary.
    from antimg import data, instruments
    monkeypatch.setattr(instruments, "CATALOG", {"t": [("AAA", "x"), ("BBB", "y")]})
    dates = pd.bdate_range("2016-01-01", periods=160)
    rng = np.random.default_rng(3)
    price = pd.Series(100 * np.exp(np.cumsum(0.0008 + 0.012 * rng.standard_normal(len(dates)))),
                      index=dates)
    df = pd.DataFrame({"Open": price, "High": price * 1.012, "Low": price * 0.988,
                       "Close": price, "Volume": 1.0}, index=dates)
    monkeypatch.setattr(data, "fetch", lambda *a, **k: df)
    r = client.post("/api/scan", json={"model": "coinflip", "atr_period": 4, "target_streak": 2,
                                       "dte_days": 30, "iv_markup": 1.25, "base_bet": 100,
                                       "starting_bank": 10000, "stress": True, "shuffle_n": 3})
    assert r.status_code == 200
    d = r.json()
    ok = [x for x in d["results"] if x["ok"]]
    # 3-way decomposition fields + breakeven markup per row; aggregate medians in summary
    assert ok and all(k in x for x in ok for k in
                      ("trend_ret_pct", "drift_ret_pct", "floor_ret_pct", "be_markup", "detrend_ret_pct"))
    for x in ok:                                          # additive identity: net ≈ trend+drift+floor
        recon = x["trend_ret_pct"] + x["drift_ret_pct"] + x["floor_ret_pct"]
        assert abs(recon - x["ret_pct"]) < 0.5
    assert "trend_median_ret_pct" in d["summary"] and "floor_median_ret_pct" in d["summary"]
    assert "be_markup_median" in d["summary"]


def test_shuffle_surrogate_props():
    # the IID surrogate must preserve length/start price and (zero-drift mode) strip the drift,
    # while the sorted return distribution is preserved (only the ORDER changes).
    from antimg.web.api import _shuffle_surrogate
    dates = pd.bdate_range("2015-01-01", periods=400)
    price = pd.Series(100 * np.exp(np.linspace(0, 0.9, len(dates))), index=dates)  # uptrend
    df = pd.DataFrame({"Open": price, "High": price * 1.01, "Low": price * 0.99,
                       "Close": price, "Volume": 1.0}, index=dates)
    z = _shuffle_surrogate(df, 0, keep_drift=False)
    assert len(z) == len(df) and abs(z["Close"].iloc[0] - df["Close"].iloc[0]) < 1e-6
    assert abs(np.diff(np.log(z["Close"].to_numpy(float))).mean()) < 1e-9       # drift stripped
    k = _shuffle_surrogate(df, 0, keep_drift=True)
    g_real = np.sort(np.diff(np.log(df["Close"].to_numpy(float))))
    g_keep = np.sort(np.diff(np.log(k["Close"].to_numpy(float))))
    assert np.allclose(g_real, g_keep)                  # same return distribution, only reordered


def test_inspect_window(client):
    # Inspect runs the real engine over a window and returns ALL campaigns' trace + table
    for model, ev in (("shares", "entry"), ("coinflip", "cf_round"), ("calls", "opt_add")):
        r = client.post("/api/inspect", json={"ticker": "SPY", "start": "2015-01-02",
                                              "model": model, "atr_period": 5, "target_streak": 4})
        assert r.status_code == 200, model
        d = r.json()
        assert d["model"] == ("coinflip" if model == "coinflip" else "grid")
        assert d["instrument"] == ("calls" if model in ("coinflip", "calls") else "shares")
        assert len(d["table"]) > 0 and len(d["trace"]) > 0
        # trace covers multiple campaigns, keyed to table rows by camp == i
        camps = {e["camp"] for e in d["trace"]}
        assert {row["i"] for row in d["table"]} <= camps
        assert any(e["t"] == ev for e in d["trace"])


def test_inspect_calls_emits_rolls(client):
    # the pyramid-calls model must auto-roll near expiry on a long-held campaign, and the roll
    # must now be a VISIBLE, well-formed trace event (the user couldn't see rolls before).
    r = client.post("/api/inspect", json={"ticker": "SPY", "start": "2015-01-02", "model": "calls",
                                          "atr_period": 5, "target_streak": 8, "dte_days": 20,
                                          "roll_buffer_days": 5})
    assert r.status_code == 200
    d = r.json()
    rolls = [e for e in d["trace"] if e["t"] == "opt_roll"]
    assert rolls, "expected at least one opt_roll on a long-held short-DTE campaign"
    rr = rolls[0]
    for k in ("camp", "n", "date", "spot", "old_strike", "new_strike", "old_expiry",
              "new_expiry", "contracts", "roll_cost"):
        assert k in rr
    assert rr["new_expiry"] > rr["old_expiry"]          # rolled to a later expiry
    # the campaign row's rolls count matches the number of opt_roll events for that campaign
    by_camp = {}
    for e in rolls:
        by_camp[e["camp"]] = by_camp.get(e["camp"], 0) + 1
    for row in d["table"]:
        if "rolls" in row:
            assert row["rolls"] == by_camp.get(row["i"], 0)


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


def test_pyramid_state():
    # the live antimartingale state machine: 2x on a win, reset on a loss / booked streak.
    from antimg.atr_strategy import pyramid_state
    assert pyramid_state([], 100, 10)["next_bet"] == 100               # fresh -> base
    s = pyramid_state(["win", "win"], 100, 10)                         # 2 wins -> 4x
    assert s["next_bet"] == 400 and s["streak"] == 2 and s["wins"] == 2
    assert pyramid_state(["win", "win", "loss"], 100, 10)["next_bet"] == 100   # loss resets
    booked = pyramid_state(["win", "win", "win"], 100, 3)              # target=3 booked -> reset
    assert booked["next_bet"] == 100 and booked["streak"] == 0 and booked["target_streak_completions"] == 1
    capped = pyramid_state(["win", "win", "win", "win"], 100, 10, 4)   # cap_mult 4 -> bet<=400
    assert capped["next_bet"] == 400


def test_next_bet_endpoint(client):
    # closed loop: stream closed trades, then read /api/next-bet back
    base = client.get("/api/next-bet?strategy_id=nb").json()
    assert base["next_bet"] == 100 and base["streak"] == 0            # nothing yet -> base
    for pnl in (12, 7):                                               # two wins
        client.post("/api/webhook/tradingview", json={"passphrase": "testsecret", "ticker": "ES",
                                                      "action": "close", "pnl": pnl, "strategy": "nb"})
    d = client.get("/api/next-bet?strategy_id=nb&base_bet=100&target_streak=10").json()
    assert d["streak"] == 2 and d["next_bet"] == 400 and d["next_bet_mult"] == 4.0
    client.post("/api/webhook/tradingview", json={"passphrase": "testsecret", "ticker": "ES",
                                                  "action": "close", "pnl": -3, "strategy": "nb"})
    assert client.get("/api/next-bet?strategy_id=nb").json()["next_bet"] == 100   # loss -> reset


def test_signals_open_close_pairing():
    # separate open + close alerts (no pnl) are paired into a win/loss by the price move
    from antimg.signals import Signal, signals_to_trials
    sigs = [
        Signal(source="tv", ticker="ES", action="buy", price=100.0, strategy_id="p"),    # open long
        Signal(source="tv", ticker="ES", action="close", price=105.0, strategy_id="p"),  # +5 -> win
        Signal(source="tv", ticker="ES", action="sell", price=200.0, strategy_id="p"),   # open short
        Signal(source="tv", ticker="ES", action="close", price=210.0, strategy_id="p"),  # +10 -> loss (short)
    ]
    trials = signals_to_trials(sigs)
    assert [t.outcome for t in trials] == ["win", "loss"]
    assert trials[0].entry_price == 100.0 and trials[0].exit_price == 105.0
