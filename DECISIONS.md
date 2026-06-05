# DECISIONS — antimartingal studio

Running log of design decisions. Read before structural edits.

- **D1 (2026-05-29)** — Scope = abstract antimartingale + real-asset ATR port + options view,
  NOT Black-Scholes LEAPS pricing engine (LEAPS plan `bright-weaving-marshmallow.md` superseded).
- **D2** — Win/loss resolution = **weekly entry + daily intra-week race**, barriers **fixed at
  entry** (B-1), single-day straddle = **loss-first** (B-2). Resolves the A-vs-B ambiguity.
- **D3** — Data: daily from yfinance (stooq fallback), weekly via resample. Daily is the finest
  interval with full free history; intraday windows too short for multi-decade backtest.
- **D4** — Account model = **cash + optional cap** (`cap_mult`), not margin (Q3).
- **D5** — Tab 3 options: **configurable DTE** (slider, default 365), strike chosen by
  **target delta** (default 0.95, deep-ITM), IV = **realized volatility** of the asset.
- **D6** — Sizing: 1·ATR move == `base_bet`. Linear tab: win=+bet, loss=−bet. Options tab:
  `units = bet/ATR`, P&L from BS reprice; premium floor truncates the left tail (the doctrine's
  loss-side lever → +EV vs the linear instrument).
- **D7** — Cache via **pickle** (no pyarrow/parquet engine dependency).
- **D8** — `resolve_trials` advances to the next week by **week-start date**, not the Friday
  label (fixed an infinite-loop bug where the current week's label > the in-week exit date).

## Web / deployment / TradingView (2026-05-29)
- **D9** — Web stack = **FastAPI (JSON API) + static Plotly SPA**, not Streamlit/Dash (user choice).
  Math core stays transport-agnostic; web layer only adds transport/validation/serialization.
- **D10** — Deploy = **Docker, maximally portable** (no platform lock-in): `deploy/Dockerfile`
  (lean `requirements-web.txt`, non-root, gunicorn+uvicorn workers, healthcheck) +
  `docker-compose.yml`. Platform configs (render/fly) deferred until a target is chosen.
- **D11** — **Stateless** API → horizontal scale by replicas; `WEB_CONCURRENCY` for workers.
  Shared state behind interfaces: `SignalStore` (SQLite→Postgres), price cache (pickle→Redis).
- **D12** — **TradingView seam**: a strategy is a *signal generator*; we overlay antimartingale
  sizing. `tradingview.parse_alert` → `signals.SignalStore` → `signals_to_trials` → `run_linear`.
  Webhook `/api/webhook/tradingview` authed by `ANTIMG_WEBHOOK_SECRET` (header or body passphrase).
- **D13** — Signal classification: alert `outcome`=win/loss, or sign of `pnl`. Entry/exit pairing
  of separate alerts = future extension (noted in `signals.py`).
- **D14** — Anti-DoS caps in `web/config.py` (max_iterations, max_target_streak, max_points
  downsampling), all env-tunable. Series downsampled so 30y daily history is a light payload.

## Mirror-hedge (rejected, see SKILL.md)
Antimartingale-long + martingale-short on the SAME instrument resizes in lockstep
(`bet_long ≡ bet_short`) ⇒ net exposure 0 ⇒ deterministic zero gross, costs make it negative.
Not implemented; documented as a rejected tactic.

## Cost model + cost-as-probability (2026-05-29)
- **D15** — Transaction costs: BOTH commission and slippage are **% of position notional
  per fill, charged on entry AND exit (×2 round-trip)**; notional=(bet/ATR)*price.
  (Superseded the earlier $/fill commission + `slippage_frac` model — user wanted both in %.)
  Tracked as separate cumulative curves (commission, slippage, total) plotted on the equity
  chart on a SECONDARY axis alongside net vs gross equity.
- **D16** — Cost expressed as a win-probability drag Δp via the breakeven shift:
  no-cost breakeven p=0.5; with avg cost κ/cycle, (2p*)^N = 1+κ/b ⇒ p*=0.5·(1+κ/b)^(1/N).
  Δp=p*−0.5 = "how much win-prob the cost eats"; if edge (p−0.5) < Δp the strategy is net −EV.
  Reported per-component (commission/slippage) and total; UI shows a ✓/✗ verdict vs the edge.

## Options resolution fixed: no stop + per-trial table (2026-05-29)
- **D17** — A LONG CALL has **no −1·ATR stop** (downside = premium). New
  `resolve_trials_long_call`: hold through pullbacks; WIN = price reaches +1·ATR before
  expiry, LOSS = expiry (entry+DTE) without the target. The options tab uses THIS, not the
  linear whipsaw stops. Demonstrated: SPY 2010–2026 linear p≈0.58 vs option p≈0.98 — the
  call captures the up-move far more often (the whole point the user flagged).
- **D18** — Detailed per-trial table under both backtest charts (`res.table`): entry/exit,
  prices, ATR, barriers/target, exit reason, outcome, bet/cost/pnl/bank; options add strike,
  premium in/out, delta in/out, units, option P&L. Static assets versioned (?v=N) + no-cache
  on the app shell so redeploys are always picked up.

## Campaign engine: scale-into-one-position on ATR grid (2026-05-29)
- **D19** — Backtest engine rewritten to `run_campaign`: scale into ONE position on the ATR
  grid. Lot ladder ×2 (1,2,4,8…), weighted avg entry, trailing stop S=avg−h/Q (h=mult·ATR)
  so the whole stack's loss is capped at the initial b → every stop-out ≈ −b, every
  target-N run = big convex win = the coin-flip distribution. `mode`: pyramid (scale-in) |
  scalp (book +b each step). Both backtest tabs use it: instrument='shares' (linear) /
  'calls' (BS-repriced, delta-normalised units=(b/h)/Δ_entry so 1 lot ≈ b/step regardless of
  delta; IV fixed at entry). Verified SPY 2010–: shares median loss = exactly −b, rare huge
  wins; calls fatten the win tail via gamma. Delta slider (default 0.5) + per-campaign table
  (steps/lots_Q/avg/stop/gross/pnl). Old run_linear/run_options/resolve_trials kept (tests).

## Real implied vol (VIX) + option rolling (2026-05-29)
- **D20** — Option IV input: `iv_source` = auto|vix|realized|constant. **auto/vix uses ^VIX**
  (real historical market IMPLIED vol, free) for S&P tickers — realized vol understated
  premiums badly (e.g. 2020 ATM 365d: $8 realized vs $15 VIX). Δ=0.5 strike barely moves,
  but premium/theta become market-real → option P&L is honest (lower). Non-S&P → realized
  vol fallback (no free vol index). Brainstormer "colleague" used intrinsic+rule-of-thumb,
  not real chains — our BS+VIX is strictly more rigorous. Caveat: VIX is 30d ATM, used flat
  (no term structure, no skew).
- **D21** — Auto-roll: when a held call is within `roll_buffer_days` (default 5) of expiry and
  the campaign is still open, roll = crystallise + re-strike to target_delta at current price
  for a fresh DTE, same lot exposure; each roll leg pays commission+slippage. Lets short/weekly
  DTE ride the trend (verified: weekly 7d → ~265 rolls). `_calls_campaign_pnl` is MTM with rolls.

## Pyramid-on-options bugfix + honest profitability (2026-05-30)
- **D22** — `run_campaign` calls path used to force-close each campaign at the option's expiry
  (`d >= expiry_day`). With short DTE that fired in week 1 before price moved +1·ATR, so the
  ladder never built (lots_Q≡1) and D21's rolling was dead code. **Removed the campaign-level
  expiry exit**: a finite option life is handled solely by ROLLING; the campaign exits only on
  stop/target/open (same as shares). Verified lots_Q ∈ {1,3,7,15,31}.
- **D23** — The campaign is NOT a fair coin-flip; its `empirical_p` is the campaign target-hit
  rate (~0.11), NOT a per-step win prob. So the coin-flip `edge = p−0.5` / breakeven-`p*`
  readout (D16) is **meaningless for the campaign** (reported fake −EV on profitable runs).
  UI now shows a plain PROFITABILITY VERDICT: net P&L, profit factor, win/loss counts+averages.
  `cost_as_prob`/`breakeven_p_with_cost` kept in the payload but no longer drive a verdict.
- **D24** — Price chart shows a green triangle-up at every +1·ATR scale-in (`entries.add`);
  target win = gold star, stop loss = red down-triangle.
- Empirical (SPY 2015–26, base $100, target 4, costs on): shares +$22.8k PF2.9; deep-ITM LEAPS
  (DTE365, Δ0.9, real VIX) +$18.3k PF2.2; **weekly DTE7 Δ0.5 calls at real VIX bleed theta to
  −$20.2k** — the data-driven case for the doctrine's deep-ITM low-theta LEAPS.

## Vol surface: term structure + skew + non-S&P indices (2026-05-30)
- **D25** — New `src/antimg/vol.py` `VolModel`: IV is a surface, not a flat number.
  **Term structure** from real CBOE constant-maturity vol indices (S&P: ^VIX9D/^VIX/^VIX3M/
  ^VIX6M) interpolated to the option's tenor in **variance-time** (linear in σ²·T), flat-σ
  extrapolation outside. `use_term_structure=False` → nearest tenor, flat in T.
- **D26** — **Non-S&P vol indices** by asset class: ^VXN (nasdaq), ^RVX (russell), ^VXD (dow),
  ^GVZ (gold), ^OVX (oil), ^EVZ (eurusd); else realized-vol fallback. (Replaces the old
  "VIX for S&P, realized for everything else".) `iv_source` adds `index`; `auto` picks the
  class index then falls back to realized.
- **D27** — **Skew** = additive `σ(m) = σ_atm + β·ln(K/S)`, fixed β per asset class (equity
  smirk β<0: SPY −0.18, QQQ −0.16; gold −0.05; FX −0.03; other −0.10), overridable via UI
  `skew_beta`. With a Δ-target the strike is off-ATM, so the smile shifts the premium:
  deep-ITM (K<S) picks up the smirk. β=0 = pure ATM (prior behaviour). β is a calibration
  (a slider), NOT market data — kept explicit/simple, no full smile fit.
- Wiring: `run_campaign`/`_calls_campaign_pnl` take optional `vol_model`; strike solved at the
  ATM term IV, option priced/repriced at the skew-adjusted IV (entry + every roll). Options
  payload reports `vol_model`/`vol_class`/`skew_beta`. Verified skew monotone (β0 +$24.1k →
  β−0.4 +$20.8k); GLD→index:gold, QQQ→index:nasdaq. assets ?v=10. 43 tests green.

## Coin-flip chart fix (2026-05-30)
- **D28** — Coin-flip UI restyle. The backend (`simcore.Simulation.simulate`) was already
  correct: `history` is the per-TRIAL bank path (multi-point, NOT a single dot — an earlier
  commit message wrongly claimed a "single point" bug; that diagnosis was mistaken and the
  attempted simcore rewrite never applied). The real defect was the UI: a leftover commit
  briefly wired the coin-flip stats block to NON-EXISTENT keys (`empirical_ev_per_cycle`,
  `win_rate`) → stats rendered as "—"/NaN. Corrected to the actual API keys: `final_bank`,
  `cycles`, `successes`, `ev_cycle_empirical`, `ev_cycle_theory`, `trades_per_cycle`. UI now
  shows a verdict line (📈/📉/➖ + final P&L over N cycles, empirical vs closed-form EV/cycle,
  target-hit %), the equity curve filled green/red by sign ("cycle/trial #" axis) and the
  streak chart relabelled. assets ?v=13. No backend/test changes (simcore untouched).
  Also fixed earlier this session (v11): app.js failed to parse — duplicate `const loss`
  (verdict block vs chart trace) broke the whole page; renamed to winSum/lossSum.
  **Lesson: always check the real API payload keys before binding the UI to them, and
  `node --check` app.js after every edit.**

## Black-Scholes speedup (2026-06-01)
- **D29** — Swapped `scipy.stats.norm.cdf` → `scipy.special.ndtr` in `options.py` (`call_delta`,
  `call_price`). `ndtr` IS the standard-normal CDF without the frozen-distribution object
  overhead: `norm.cdf` ≈ 37.7 µs/scalar call vs `ndtr` ≈ 0.1 µs (**377×**). Because every BS
  primitive runs inside 64–80-iteration bisection loops (`strike_for_delta`, `price_for_value`)
  called per round/per bar, this is the dominant cost. **Numerically identical** — max |Δprice|
  and |Δdelta| over a 500-point random grid = exactly 0.0; solver residuals at 1e-15.
  Micro: `call_price` 84→5.7 µs (14.8×), `strike_for_delta` 3.7→0.37 ms (10×),
  `price_for_value` 5.5→0.38 ms (14.5×). End-to-end coin-flip engine on synthetic trend
  (54 cycles): **0.98s → 0.10s, 9.5×, identical final_bank** ⇒ the ~2-min 81-ticker coin-flip
  scan should drop to ~12–15s. 51 tests green, no behaviour change. Motivates the next-step
  p-sweep (which re-runs the engine many times — now affordable).

## Scan honesty: drift-stripped control + breakeven IV markup + fixed verdict (2026-06-01)
- **D30** — Made Tab 5 (Scan) stop flattering the strategy. Three parts:
  1. **Verdict fix (app.js):** the old footnote "mean ≫ median ⇒ NOT sound" printed UNCONDITIONALLY
     (even when mean≈median, contradicting the "✅ BROADLY ROBUST" badge). Now: badge requires BOTH
     ≥50% profitable AND median > 0; the mean≫median and median≤0 caveats are conditional.
  2. **Drift-stripped control (`stress=True`):** `_detrend(daily)` removes the mean daily log-return
     (zero net drift = a true fair coin) keeping vol/intraweek shape, and re-runs the SAME strategy.
     Per-instrument `control_ret_pct` + aggregate `control_median_ret_pct`/`control_profitable_pct`.
     The gap (real − control) is the part that's pure directional drift, not structure. Demo on a
     +10%/yr synthetic: base −6.2% vs control −28.8% ⇒ 22.7pp was drift.
  3. **Breakeven IV markup (coinflip, `stress=True`):** `_breakeven_markup` bisects (net is monotone-
     decreasing in markup) the IV markup at which net=0, in [0.5,3.0], with lo/hi flags. "Options must
     be priced below Nx realized to profit." Demo: 1.22× ⇒ real options (~1.1–1.6×) make it −EV.
  - `ScanReq.stress` opt-in (~3–8× slower); gunicorn `--timeout 120→600` so the stress sweep isn't
    killed. assets ?v=27. 53 tests (added `_detrend` zero-drift invariant + stress-fields test).
  - **WHY:** answering "is 5-in-a-row calls profitable?" — a synthetic zero-drift fair coin reproduced
    the user's ~67%/+28% scan ONLY when +10%/yr drift was injected; at markup 1.25 a fair coin LOSES
    (−33% median). The broad positivity was drift + fill/IV optimism, not a structural edge. These
    controls surface that in the tool itself.

## Scan control fix: drift/trend/floor decomposition via IID shuffle (2026-06-01)
- **D31** — The drift-strip control (D30) was shown to be a BAD test: it removes only the mean
  log-return, leaving the path order (so trends survive) AND over-correcting trending series into a
  back-half reversal (SPY detrend −16k < shuffle +9k — nonsensical). Replaced it with an IID SHUFFLE
  surrogate (`_shuffle_surrogate`: permute per-day bar shapes — logret + hi/lo/open wicks in lockstep
  — destroying serial structure, keeping the exact bar distribution; `keep_drift` toggles zeroing the
  mean). `stress=True` now runs `shuffle_n` (default 8) shuffles in two modes and reports an ADDITIVE
  3-way split per instrument that telescopes to base net:
    floor = E[net | IID, zero drift]            (noise/fill-artifact; doctrine says ≈0)
    drift = E[net | IID, real drift] − floor     (1st-moment directional component)
    trend = base − E[net | IID, real drift]      (serial structure: momentum/trend persistence)
  Naive detrend kept as a labelled reference (over-corrects — don't trust it). Aggregate medians +
  floor-profitable% + be_markup_median in summary. gunicorn timeout 600→900.
  - **Result on real SPY/QQQ/GLD (12 shuffles):** profit is DRIFT-dominated (SPY drift +38k of +70k;
    QQQ +52k of +46k; GLD +70k of +13.5k). Trend/momentum is INCONSISTENT (SPY +20k but QQQ −16k,
    GLD −56k — time-ordering HURTS gold/nasdaq). Floor ≈ 0 within ~1 sd everywhere (GLD +205). ⇒ the
    structure manufactures no edge; the scan's headline is a levered directional long, not structural
    alpha. Confirms the original verdict; the drift-check "not changing" was the control being broken.
  - assets ?v=28. 54 tests (+shuffle surrogate props + additive-identity check). Verdict text now
    splits the median headline into trend/drift/floor with interpretation.

## Make option rolling visible + add rolling model to Tab 7 (2026-06-01)
- **D32** — User: «I don't see option rolling in Tab 7; I only see the loss when it expires.» Correct on
  both counts. (a) Tab 7 Inspect only offered shares + coinflip; the coinflip model has NO within-round
  roll by design (hold to double-or-expiry) so a losing round books its loss only AT expiry — exactly
  what they saw. (b) The auto-roll (re-strike to target-Δ within roll_buffer_days of expiry, keep
  exposure, ride the trend) lives ONLY in the pyramid-calls model (`run_campaign instrument='calls'` →
  `_calls_campaign_pnl`), which Inspect didn't expose; and even where it ran (Tab 3) the roll was never
  emitted as a trace event — only counted in `row['rolls']`, hence invisible.
  - Engine: `_calls_campaign_pnl` now emits a distinct **`opt_roll`** trace event (camp, n, date, spot,
    old→new strike, old→new expiry, prem_close/open, contracts, roll_cost).
  - API: `InspectReq.model` gains **`calls`** (pyramid + auto-roll, realized-vol IV) + `roll_buffer_days`;
    `/api/inspect` runs it (instrument='calls') and returns roll_buffer_days.
  - Frontend: Inspect Strategy dropdown gets «calls — pyramid + auto-roll 🔁» + a Roll-buffer input;
    `_inspCampGrid` passes instrument through so calls campaigns render the options path; roll markers
    (cyan diamonds, old→new strike labels) on both the window overview and the per-campaign chart; a
    «🔁 АВТО-РОЛЛ» narration block + roll rows in the options ledger; roll count in the window summary.
  - Verified real SPY 2020-22 DTE45: 8 opt_roll events = the 8 campaign rolls; rolled strikes re-struck
    to spot, expiry +~6 weeks. assets ?v=29. 55 tests (+calls-inspect roll test, +calls case in inspect test).

## TradingView closed loop: open/close pairing + GET /api/next-bet (2026-06-01)
- **D33** — Implemented the documented TradingView extension (ARCHITECTURE.md «Future extension»):
  the connection was ingest+replay only; now it's a **closed loop**.
  1. **Open/close pairing** (`signals.signals_to_trials`): besides self-contained closed-trade alerts
     (pnl/outcome → one Trial, unchanged), a buy/sell OPEN alert (price, no outcome) is now held and
     PAIRED with the next close/exit/flat alert on the same (strategy_id, ticker) — outcome inferred
     from the price move and side (long: close≥open ⇒ win; short inverted), entry/exit = the two prices.
  2. **Live sizing** (`atr_strategy.pyramid_state` + `GET /api/next-bet`): replays the stored win/loss
     stream through the antimartingale state machine (2× on a win capped at base·cap_mult, reset on a
     loss or a booked target streak) and returns `next_bet` (+ streak/wins/losses/mult/note). A Pine
     alert reads this back — `GET /api/next-bet?strategy_id=&base_bet=&target_streak=&cap_mult=` — to
     size its next order from the running streak. Pure read, no mutation; fresh strategy → base_bet.
  - Verified: 3 wins (cap 8) → next_bet 800 (8×); a loss → reset to base; open+close pair → win/loss.
  - Tab 4 gets a «🎯 Next bet (live)» button + the hint documents pairing & the closed loop. assets ?v=30.
  - 58 tests (+pyramid_state, +next-bet endpoint, +open/close pairing). ARCHITECTURE.md updated (status →
    implemented incl. pairing + next-bet).

## Tab 8 — Hedged Intraday (Прикрытый Интрадей, Korovin) backtest (2026-06-04)
- **D34** — New tab + engine for a DIFFERENT strategy family (not antimartingale): the ПИ method
  (`/hedgedintraday` skill). Built per a live consult of the corpus (`5fada65b`) on backtest modeling.
  - **Position** = long synthetic **straddle (2 ATM calls − 1 future)**, delta-neutral, long gamma,
    max loss = premium. BS mark-to-market daily (IV from `vol.VolModel` term-structure/skew, same as
    Tab 3), rolled to a fresh ATM strike within `roll_buffer_days` of expiry (monthly DTE default).
    Premium budget = `risk_pct`·bank (doctrine 20%), re-sized to the running bank at each roll.
  - **Scalping overlay** = counter-trend exponential grid (three-thirds: intraday limit = `intraday_frac`
    of futures, `n_parts`, first step `grid_atr_frac`·dailyATR, `grid_mult` spacing). Daily-bar model:
    `scalp_day = part_lots·(min(max_rt·g1, eff·reversed_range) − stuck_penalty·max(0,|C−O|−g1))`,
    `reversed_range = (H−L)−|C−O|` (the mean-reverting part the grid harvests; trend portion drags).
  - **Engine**: `src/antimg/hedged_intraday.py::run_hedged_intraday` → separated **straddle / scalp /
    total** P&L streams + a modeled theta path + per-straddle-period table. `POST /api/hedged-intraday`
    (`HedgedIntradayReq`). Tab 8 plots the P&L decomposition + price-with-rolls and an honest verdict
    (CAGR vs the doctrine's 25–40%/yr; % of theta the scalp covered; worst-period vs premium cap).
  - **Honesty (key)**: daily bars see ~1 reversal/day vs the corpus's ~10 RT/day on 1-min → scalp is a
    PESSIMISTIC LOWER BOUND, theta dominates. Default eff=0.5 recovers ~14% of theta on SPY/GLD 2018-26
    — which MATCHES Korovin's own "students offset 10–15% of straddle cost/month" figure (calibration
    check). Monthly ATM straddles bled (~−32% CAGR) under this conservative daily model; lifting
    `scalp_efficiency`/`max_rt_per_day` approximates intraday frequency. Verdict states all this; lesson
    written back to the skill (`references/lessons.md::backtest-daily-bars`).
  - Verified: SPY/GLD real-data smoke (worst period ≥ −premium = the loss cap holds; identity
    total = bank + straddle + scalp). assets ?v=33. 64 tests (+5 engine, +1 web). 8 tabs now.

## Tab 8 bulk scan — ПИ across the whole catalog (2026-06-04)
- **D35** — Added a one-click cross-instrument sweep for the Hedged Intraday strategy (parallels
  Tab 5 «Scan all» but for ПИ). New `HedgedIntradayScanReq` (the ПИ knobs minus ticker/end — too
  different from shares/coinflip to bolt onto `ScanReq`) + `POST /api/hedged-intraday/scan`: runs
  `run_hedged_intraday` on every `instruments.flat_with_group()` ticker with identical params,
  sequential (Yahoo 429), per-ticker failures captured. Per-row summary (`_hi_summary`): net,
  ret%, **CAGR**, straddle/scalp split, scalp-cover%, worst-period, premium cap, **loss_cap_ok**
  (worst period ≥ −premium), maxDD, rolls. Aggregate: profitable%, median/mean CAGR, median
  scalp-cover, loss-cap-ok%, best/worst.
  - Refactored `_build_vol(req, daily, ticker=None)` to take a ticker override (scan has no single
    ticker) and extracted `_run_hi(daily, datr, vm, realized, req)` shared by the single route + scan.
  - Tab 8 frontend: «📊 Bulk» button reusing the same form params (ticker/end ignored server-side),
    its own sortable results table (`renderHiScanTable`, default sort CAGR desc) + horizontal CAGR
    bar + verdict (robust if ≥50% profitable AND median CAGR>0; restates the daily-bar lower-bound
    caveat). assets ?v=34. 65 tests (+scan web test).

## Tab 8 — event-driven daily-cadence scalp grid (user insight) (2026-06-04)
- **D36** — User: «absence of intraday data should not prevent backtest — take 6mo/1yr options, then
  one-day data is representative because the range is much bigger [relative to the grid step]». Correct.
  Reframed the scalp from a lower-bound heuristic to a FAITHFUL daily-cadence simulation.
  - New default `scalp_model='grid'`: event-driven counter-trend grid. Grid step g1 = grid_atr_frac·dailyATR
    (default 1×), exponential offsets from the straddle center. Each daily bar is walked along an OHLC
    path (green O→L→H→C, red O→H→L→C); resting limit orders fill when crossed; a short at a sell-level
    buys back one step lower (long mirror); each working part holds ≤1 leg ⇒ total ≤ intraday limit
    (never naked); genuinely stuck legs are carried + MtM'd, closed at the roll. NO efficiency/RT/penalty
    fudge — removed three knobs from the honest path. `scalp_round_trips` counted + surfaced.
  - Legacy `scalp_model='range'` kept (the old (H−L)−|C−O| heuristic) as the explicit intraday lower bound.
  - Defaults shifted to the slow regime: dte_days 30→180, roll_buffer 5→10, grid_atr_frac 0.5→1.0.
  - **Real-data validation (2018-26): long DTE collapses theta bleed** — GLD monthly −24.7%/yr →
    grid+1yr −1.7%/yr; SPY −35% → −4.3%; SLV +20.7%(range,optimistic) → +0.8%(grid,1yr). Grid books
    70–110 real round-trips; counter-trend scalp ~washes (small trend drag, straddle gamma pays theta)
    ⇒ net ≈ breakeven, not catastrophic bleed. Daily bars ARE representative in this regime.
  - Engine/schema/scan all thread `scalp_model`; Tab 8 + bulk verdicts branch on it (grid = "daily
    representative, read CAGR directly"; range = "lower bound"). assets ?v=36. 67 tests (+2 grid).
  - Lesson → skill `references/lessons.md::daily-bars-representative-with-long-options`.

## Tab 8 — grid-step ATR timeframe (daily/weekly/monthly) (2026-06-04)
- **D37** — User insight: to scalp a wider oscillation that daily bars CAN resolve, base the grid
  STEP on a longer-timeframe ATR (weekly/monthly) so each daily bar is sub-step "intraday-like"
  info within a multi-day swing. New `grid_timeframe` (daily|weekly|monthly, default **weekly**);
  `data.atr_on_timeframe()` computes ATR on the coarse bar, SHIFTS one bar (no look-ahead),
  ffill-reindexes to daily. Execution still walks daily bars. Also added `data.monthly()`.
  - **Real-data (DTE 365): coarser TF turns the quiet doctrine instruments positive** — GLD
    −1.7%(daily)→+4.2%(monthly); SLV +0.8→+7.7; GC −2.0→+4.5; NG −4.5→+3.9. **Honest mechanism:**
    NOT more scalp (scalp stays ~flat/slightly-neg) — the wide grid TRADES FAR LESS (RT/yr ~9→1),
    stops over-churning/fighting trends, and lets the STRADDLE carry (the doctrine's "flatten the
    grid, bigger targets, once-a-day" mode). Straddle remains the engine; the reframe removes the
    daily grid's self-inflicted cost+trend drag. (CAGR shifts also partly via bank-compounding
    coupling: less scalp drag → bigger bank → bigger straddle sizing.)
  - Threaded through single route + bulk scan + both verdicts (show TF + per-year RT). assets ?v=39.
    68 tests (+grid-timeframe widening test).
- **Process:** also persisted a standing memory — ALWAYS consult the governing skill WHILE coding
  (not just at start); the prior-turn wrong verdict came from not doing so. Live corpus consults on
  the ATR-timeframe + instrument-universe questions are QUEUED (NotebookLM rate-limited).

## Tab 8 — straddle breakeven-theta-coverage readout + DTE default 365 (2026-06-04)
- **D38** — User: "SPY should be positive and is not" (range −7%, grid −13%). Diagnosed honestly:
  SPY's straddle gamma+directional is strongly POSITIVE (+4.7–5.3k — it catches the big moves);
  theta (−6.4–8k) only just exceeds it, so the straddle is a hair below breakeven. Net=0 needs the
  scalp to cover only ~17–33% of theta — far below the doctrine's MINIMUM scalp claim (~100%,
  «отбивание теты»). ⇒ under the method's design intent SPY IS positive; the daily backtest shows
  negative only because the grid books ≈0 scalp (can't see SPY's intraday chop) and DTE 180 piled
  on theta. (range model DTE 365 already shows SPY +1.4%.)
  - Engine exposes `gamma_dir_pnl` (straddle − theta) and `breakeven_scalp_cover_pct`
    (= −straddle/|theta|, the % of theta the scalp must cover for net=0). Verdict now leads the
    decomposition with: gamma vs theta split + "straddle is ~breakeven; scalp needs X% of theta;
    doctrine min ≈100% ⇒ instrument positive under design intent." Surfaced in /api/hedged-intraday.
  - Default `dte_days` 180→365 (the user's "even one year"): slower theta, straddle closer to
    breakeven on indices. assets ?v=40. 68 tests.
  - Live consult on SPY/index suitability QUEUED (NotebookLM rate-limited).

## Tab 8 — scalp grid re-centering (frozen-grid bug fix) (2026-06-04)
- **D39** — User spotted the real bug: the scalp grid was anchored at the straddle strike and frozen
  for the whole option life (a year at DTE 365), so once price trended away it stopped scalping the
  current range entirely (→ ~0 round-trips). Fix: `scalp_recenter_days` (default 21) re-centers the
  grid to the CURRENT price every N days (realizing stuck legs), so it follows price and scalps the
  live range. Defaults shifted to the user's "2× daily ATR target": grid_timeframe daily,
  grid_atr_frac 2.0, recenter 21; engine dte_days 180→365.
  - **Honest measured outcome:** re-centering REDUCES the frozen-grid trend-bleed (ETH scalp
    −18.8k→−8.3k, CAGR 24.9%→28%; GLD/SLV/NG scalp losses cut toward ~0) — kept ON by default. BUT
    it does NOT manufacture scalp income: round-trips/yr stay ~5 whether frozen or tracking, coarse
    or fine step. CONFIRMS the hard limit: live ПИ's ~2500 round-trips/yr are TINY intraday wiggles
    (smaller than daily ATR) that an OHLC bar discards; the ≥2·ATR swings a daily bar CAN see are
    rare (~5/yr) and usually don't cleanly reverse (trend). So the daily backtest still measures the
    straddle core, not the scalp — the scalp needs intraday data. (Where the user's "2× daily ATR
    catches all the back-and-forth" overestimates: the profitable scalp is sub-daily, not big swings.)
  - assets ?v=41. 68 tests.

## Tab 8 — many fine sub-parts: count rises, P&L doesn't (2026-06-04)
- **D40** — User: the ⅓ scalp limit can be split into many sub-parts, each deblocked only when price
  travels its (exponential) distance. Confirmed the grid ALREADY does this (cumulative-exponential
  levels, distance-gated fill, re-arm after round-trip). Raised n_parts cap 10→50 so it can be split
  fine. **Measured: more sub-parts raise the round-trip COUNT a lot (SPY 19→239/yr, SLV 15→195/yr at
  40 parts / 0.2×ATR step) — approaching live ПИ frequency — but net scalp P&L does NOT improve**
  (SPY scalp −201→−494, SLV −399→−464). Reason: profit/round-trip ∝ step, so finer parts just slice
  the SAME daily-resolvable mean-reversion into smaller pieces (more trips × smaller size ≈ same
  gross), and trends still drag at every scale. Live ПИ's ~2500 trips/yr profit because they're
  INTRADAY (many reversals WITHIN each day = large intraday path length) — exactly what a daily OHLC
  bar discards. So sub-part count can't recover the scalp edge from daily data; it's bounded by the
  path's mean-reversion content at the daily scale. Same conclusion, new angle.

## Tab 8 — BUG FIX: re-centering destroyed the mean-reversion edge (2026-06-04)
- **D41** — User challenged that ПИ has positive expectation yet the model loses, suspecting a rule
  violation. Investigation found a REAL bug I introduced in D39: `scalp_recenter_days` force-closes
  open scalp legs to market on a timer — which REALIZES the underwater counter-trend legs that were
  about to mean-revert, converting the edge into losses (and violating the doctrine "carry/heal stuck
  parts, never abandon"). Proof: a clean OU mean-reverter flips +933 (carry) → −602 (re-center);
  detrended SPY −329→+77, detrended GLD −176→+80. **Fix: default scalp_recenter_days 21→0** (carry
  stuck legs to the roll — the doctrine-faithful behavior that lets the grid capture mean-reversion);
  re-centering kept as an opt-in but documented as edge-destroying. +OU regression test (69 tests).
  - **Resolved the scale-invariance question honestly:** ПИ is NOT unconditional-positive-EV. The
    scalp = gamma-scalping the straddle; its edge = capturing mean-reversion, which is SCALE- and
    instrument-DEPENDENT. SPY daily returns mean-revert (lag-1 autocorr −0.13) so the edge EXISTS,
    but at the daily scale it's small and competes with DRIFT (stuck-leg losses on the multi-week
    trend). Detrended → scalp positive on SPY/GLD; with drift → ~0/negative. Crypto (ETH/BTC):
    volatile but TRENDING (10x) → counter-trend scalp LOSES (−19k) while the straddle GAMMA WINS
    (+72k) — opposite sides of the trend BY DESIGN (the straddle is the hedge of the scalp's trend
    risk). So "volatile = back-and-forth = scalp profits" conflates volatility with mean-reversion.
  - Live consults on gamma-scalping / trend behavior QUEUED (NotebookLM rate-limited).

## Tab 9 — ПИ Execution viewer + "don't fade a trend" rule (Bollinger gate) (2026-06-04)
- **D42** — User: apply ALL the basic rules (skill references, not live consult), and add a tab to
  WATCH the strategy on a chosen window. Two parts:
  1. **Applied the missing rule** — *don't fade a confirmed trend*: a Bollinger-band FLAT detector
     gates new counter-trend scalp entries (no short above the upper band / long below the lower
     band; exits always allowed) → on a breakout the grid steps aside and lets the straddle run.
     Engine: `use_bbands`(default on)/`bb_window`(20)/`bb_k`(2). Helps modestly (a trailing band
     drifts with the trend, so it only blocks extreme breakouts): SOL scalp −345k→−306k, SLV +43→+391.
  2. **Tab 9 "ПИ Execution"** + `POST /api/hedged-intraday/inspect` (engine `trace=` emits every
     scalp open/close): pick instrument+window (default 3-mo), see price + BB flat-band + ATM strike
     step + each 🔻short/🔺long scalp entry + ○ exit + ◆ roll + the P&L decomposition, with a
     narrative that reads the regime. Verified SOL 2021-H1: scalp opened 10 shorts into the rally,
     7 stuck, scalp −10.5k, but straddle GAMMA +81.9k → TOTAL +71k — the user's thesis on screen
     ("trend like hell ⇒ positive despite the stuck ⅓; scalp & straddle are opposite sides of the
     trend by design"). 70 tests. assets ?v=44.
  - Process note (user): the corpus is for things OUTSIDE the strategy; the strategy rules are in
    the skill refs — APPLY them, don't ping a rate-limited corpus for what's already documented.

## Tab 8/9 — залипшие части rule (profit-gated heal) + regime visualization (2026-06-04)
- **D43** — User: "how do you decide WHEN to drop which working parts? + show your flat/trend logic on
  the Tab 9 chart." Implemented the doctrine's залипшие-части rule properly and made it visible.
  - **Engine:** `heal_with_profit`(on) + `confident_flat_n`(3). When price leaves the WHOLE grid
    (|price−center|>reach) the stuck parts are HEALED — closed & the grid re-centered to current price —
    **only if accumulated round-trip profit (`heal_budget`) covers the realized loss**; otherwise CARRY
    (straddle pays). `clean_streak` counts consecutive clean round-trips → «уверенный флет» at ≥N
    (scaling allowed); reset on heal/stuck. Emits `scalp_heal` + `confident_flat` trace events;
    result gains `scalp_heals`, `confident_flat_days`. This is the answer to "when to drop a part":
    spend accrued profit to unstick, else let the straddle pay — never force-realize (that was the
    D41 bug). OU regression still green.
  - **Tab 9 viz:** trend-regime spans (price OUTSIDE BB) shaded red = grid steps aside; white = flat
    (scalp active); green dotted verticals = «уверенный флет» reached; ✚ = a heal (with the loss it
    spent). Endpoint returns trend_spans/heals/confident_flat + stats. Reverted the scalp to a single
    shared P&L axis on Tab 8 & 9 (user: dual axis was confusing). Verified SOL 2021: 19 trend-spans,
    0 heals (no profit → carried, straddle paid +321k → total +244k). 70 tests. assets ?v=46.
  - Profitability verdict stated plainly to user: NOT broadly profitable as a daily-measurable backtest
    (28% of panel, 33% of even target instruments, negative medians); profit concentrates in strong
    trenders (crypto) via straddle gamma; the scalp that would carry ranging names is unmeasurable on
    daily bars. Conditionally profitable on the right (volatile/trending) instruments, not universally.

## Tab 8/9 — LITERAL three-thirds (no substitution) (2026-06-04)
- **D45** — User: "do it exactly like the strategy, no improvisation" (re the ⚠ three-thirds). I had
  substituted gamma for the trend reserve and ⅓-of-futures for ⅓-of-calls. Replaced with the literal
  doctrine: total calls = 2·n_str split in thirds — base hedge = ⅓ of calls = (2/3)·n_str short
  futures (the 33% floor), ⅓ of calls left UNHEDGED = trend reserve (net-long at rest → trend runs by
  itself), ⅓ of calls = scalp limit. Futures-sold band = exactly 33% (base only) … 67% (full scalp).
  Engine: `base_futs=(2/3)·n_str` used in straddle MtM + all fill notionals; scalp `lim=2·n_str·intraday_frac`.
  - **Following it literally MATTERED** (validates the user): the unhedged trend-reserve third turns
    GLD −1.9%→+4.0%, SLV→+6.2%, SPY −4%→−1%, SOL +130% — the reserve runs with the move as doctrine
    says ("the untouched third drags into profit on a real trend"). Loss cap still holds (worst case =
    flat expiry = −premium; net-long doesn't raise max loss). OU + loss-cap tests green; +band test.
  - Panel: three-thirds ⚠→✅. Remaining ⚠ (literal next): confident-flat LOT SCALING (detect→scale),
    conditional rolling (move≥call-cost + profit, not schedule). assets ?v=49. 71 tests.
  - LESSON: implement doctrine LITERALLY; substituting an "equivalent" mechanism (gamma for the
    unhedged reserve) changed the result and was wrong. Folded into the skill habit.

## Tab 9 — show the ⅓-third split into N working parts + fix the first-step calibration (2026-06-04)
- **D46** — User: "the ⅓ scalp third must then be split into 5 working parts — did you miss that?"
  No — `setup_grid` already splits the intraday third into `n_parts` (default 5) working parts at
  exponential offsets, part_lots = limit/n_parts. BUT the OLD default first step (grid_atr_frac=2.0,
  mult=2) put the 5 parts at 2/6/14/30/62·ATR → parts 3-5 essentially never fired (only ~1-2 of 5
  worked). Fixed the calibration to the doctrine ("ATR sets only the FIRST step", small): default
  grid_atr_frac 2.0→0.5, so parts sit at 0.5/1.5/3.5/7.5/15.5·ATR — part 1 the workhorse, outer ones
  the exponential emergency reserve (rarely hit, by design). Engine emits `grid_setup` trace events;
  Tab 9 now DRAWS the N working-part levels (dotted, labelled ч.1..N) + center, so the split is
  visible and you can see which parts are reachable. 71 tests. assets ?v=50.

## Tab 8/9 — уверенный флет: LITERAL lot scaling (заслуженный риск) (2026-06-04)
- **D48** — User: "we agreed ALL rules" — confident-flat was still ⚠ (detect only). Implemented the
  literal rule: after ≥confident_flat_n clean cycles, the working-part lot SCALES UP, funded by
  ACCRUED PROFIT only (heal_budget): scale = 1 + min(accrued/premium, 1) ∈ [1,2]. Capped ×2 so total
  scalp (n_parts·2·base) ≤ calls−base ⇒ still never naked. Engine `confident_flat_scale`(on) +
  `scalp_scaled_max`. Verified OU flat: scaling ON scalp +12.7k vs OFF +5.2k (same RTs, bigger lots),
  max ×2.00. Visible in the Tab 9 ledger (lot column grows) + panel rule ⚠→✅. 72 tests. assets ?v=52.
  - Panel now: only conditional ROLLING + daily-scalp-data-limit remain ⚠ (rolling = a real mechanic
    to add; data-limit is inherent, not fixable in code).

## Tab 8 parity — doctrine rule-panel + counters on the MAIN tab (2026-06-04)
- **D50** — User: "apply this on the main tab too — you should do it automatically." The rule LOGIC
  already ran identically on Tab 8 (one _run_hi→engine), but the rule-compliance PANEL + counters
  were Tab-9-only. Surfaced scalp_heals / confident_flat_days / scalp_scaled_max / n_parts /
  use_bbands in the `/api/hedged-intraday` stats; refactored `renderHiRules(d,s,id)` to take a target
  container + tolerate Tab-8 aggregate-only stats (scalp_opens/trend_days fall back); rendered the
  same panel under Tab 8. Verified Tab 8 SPY 2015: heals 6, confident-flat 2627 d, lot-scale max
  ×1.61, RT 252 — same engine. Per-trade ledger stays on Tab 9 (windowed; full-history ledger would
  be unusably large). 72 tests. assets ?v=54.
  - HABIT: when adding a doctrine feature, surface it on BOTH the main tab and the inspect tab —
    don't leave parity to a follow-up request.

## Tab 8/9 — INTRADAY scalp feed (hourly) — the long-open data item (2026-06-04)
- **D51** — User: "add an intraday feed for the scalp." `data.fetch_intraday(ticker, "60m", …)`
  (yfinance hourly ~730d history, cached, tz-naive). Engine `run_hedged_intraday(…, intraday=df)`:
  groups intraday bars by day and the scalp grid walks the REAL intraday path (many round-trips)
  instead of one daily OHLC bar; straddle/theta/rolls stay daily. `scalp_data` ('daily'|'hourly') on
  HedgedIntradayReq (Tab 8 + Tab 9; NOT the scan — 80×2y hourly would hammer yfinance). Graceful
  fallback to daily if the fetch fails. `res.intraday_bars` surfaced; rule-panel «Скальп» flips ⚠→✅
  when an intraday feed is used. Synthetic proof: intraday 62 RT/+2916 vs daily 16 RT/+398.
  - Honest scope: hourly ≈2y only, and 60m is still coarser than live 1-min ПИ — so it's a big step
    closer (sees intraday chop) but not full tick fidelity; recent-window only. 72 tests. assets ?v=56.

- **D52** — User: "start with what is available free" (re: getting true low-timeframe data to MEASURE
  the scalp — see the new `/tradinglivedata` skill's verdict: crypto 1-min/tick is FREE & deep via
  Binance, and ETH/BTC is the doctrine's IDEAL instrument). Added `data.fetch_intraday_crypto(ticker,
  interval='1m', …)` — paginates Binance public `/api/v3/klines` (1000 bars/req) over [start,end] via
  **stdlib urllib only** (no ccxt/requests dep), hosts `data-api.binance.vision` → `api.binance.com`
  (both reachable from the container, unlike Yahoo). tz-naive UTC index, cached per (symbol,interval);
  `_to_binance_symbol` maps BTC-USD/ETH-USD/SOL-USD→…USDT and returns None for non-crypto (graceful
  fallback). New `scalp_data='1m'` on HedgedIntradayReq → `_intraday_feed` routes crypto to the 1m feed
  (non-crypto/geo-block → daily). Tab 8 + Tab 9 selects gained the "1m crypto (Binance free)" option.
  Also gave `fetch()` a **Binance daily fallback** for crypto so the whole crypto path is Yahoo-free.
  Verified LIVE: ETH full free path (daily+1m both Binance) walked 23,040 real 1m bars; scalp measured
  (−363 over a trending 20-day window = honest: scalp loses in trend while straddle gamma wins, INV#3).
  4 new tests (symbol map, klines parse, non-crypto reject, @network live smoke). **76 tests green.**
  - Honest scope: free crypto only; SPY/GLD/SLV intraday → Polygon $29/mo, futures → Databento/IQ Feed,
    MOEX (RI/Si) → Finam/ISS (none of the free feeds cover it). 1m over multi-year = slow first pull
    (~1 req/1000 bars) then cached; pick a coarser interval for long windows. assets unchanged.

- **D53** — User: "integrate it in our 9/8 tabs." Made both ПИ tabs **feed-aware** (the dropdown +
  routing from D52 were in, but the UI hard-coded "часовых баров"/hourly). Backend: both
  `/api/hedged-intraday` + `/inspect` now surface `scalp_data` in stats. Frontend (app.js): the shared
  rule-panel «Скальп: внутридневной фид» line + the Tab-8 verdict now branch on `scalp_data` — show
  "1-МИНУТНЫЙ ФИД (Binance, крипта — БЕСПЛАТНО)" + bar count + "ближе всего к живому ПИ (200–250
  круг/мес)" when 1m; the daily-fallback hint now points crypto→«1m crypto», else→«hourly». Also made
  `fetch()`'s Binance **daily** fallback pull FULL history (start=None) so a short first request can't
  poison the daily cache (the 422 I hit). assets v56→v57. **76 tests green.** Verified LIVE through the
  route: ETH 120d scalp_data='1m' → HTTP 200, walked **64,800** real 1m bars, scalp +703 vs straddle
  −668 (net ~0) — first honest read where the scalp actually covered the theta on the doctrine's ideal
  instrument. (Scan tab still daily-only by design — 80×crypto-1m would be a huge pull.)

- **D54** — User: "in tab 9 nothing happens when push run button." Root cause: 1m + a multi-year window
  (they reused Tab 8's ~8-yr range) = thousands of sequential Binance requests → the request hung for
  many minutes (looked dead; could hit the gunicorn 900s timeout). Backend + assets were fine (deployed
  /inspect returns 200). Fix: **clamp the 1m feed to the last `ANTIMG_HI_1M_DAYS` days (default 120)** —
  mirrors the hourly 725d clamp; full-window straddle/theta, recent-window measured scalp. Verified: ETH
  2018→now + 1m on /inspect now 200 in 0.5s (was ~20 min unbounded). Also added an **immediate toast**
  on Tab 8/9 submit when an intraday feed is chosen ("Качаю 1-мин историю…") so it never looks hung, and
  imported `os` in api.py. assets v58. 76 tests.
  - First honest 1m ПИ reads (180d): ETH scalp +223/straddle −490 (cover 68%, CAGR −8.9%); BTC scalp
    −537/straddle −354 (cover −138%, −18.6%). Loss cap held (worst ≥ −premium). Confirms the skill: the
    scalp does NOT reliably pay theta even on crypto 1m — needs a ranging regime; gamma carries trends.

- **D55** — Perf: the 1m cold pull was still ~6 min even clamped to 60d (deployed: 352s). Root cause:
  `urllib.urlopen()` per page = fresh DNS+TLS handshake every request (~4s/req from the container).
  Fix: reuse ONE **keep-alive** `http.client.HTTPSConnection` across all pages (rotate hosts on error,
  fully read each response to reuse the socket). Result (deployed, fresh SOL ticker): **COLD 63s**
  (was 352s, 5.6×), **WARM 0.8s**. Default clamp 120→60d (v59). With the submit toast + spinner, the
  one-time ~60s cold pull is acceptable; cached after. assets v60. 76 tests. Live :8090 rebuilt.
