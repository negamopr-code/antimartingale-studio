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
