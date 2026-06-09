"""Tab 14 — «Симуляция в деньгах»: a single-period, fully-transparent worked example of the
Прикрытый Интрадей (ПИ) method on a real instrument over a real past window.

Unlike the multi-roll backtest (Tab 8), this is ONE construction held for ONE period, with every
number exposed: what you buy, what the synthetic straddle costs, the exponential scalp grid in real
prices, and — for crypto — the scalp income MEASURED by walking the real 1-minute path (the doctrine's
only honestly-measurable instrument). It answers, in dollars: «что конкретно купить, сколько стоит
стреддл, сколько принесёт скальпинг» for e.g. $10k on ETH.

Construction (Korovin, synthetic straddle):
    1 straddle unit = 2 ATM calls − 1 future  →  pays |S_T − S0| at expiry, costs 2·call_premium.
    M straddle units  ⇒  buy 2M calls, short M futures.  Premium budget = risk_pct · deposit = 2·c0·M.
    Three-thirds: the M short futures are the delta-neutral BASE; the intraday scalp LIMIT =
    intraday_frac · (calls count) = intraday_frac · 2M, split into n_parts working parts spaced
    EXPONENTIALLY (step·grid_mult^k) around S0.

Honesty (skill INVARIANTS):
  • Loss is capped at the premium paid (#2): straddle_net ≥ −premium.
  • The scalp can only be MEASURED on an intraday feed (#5); on daily bars it is a labelled SCENARIO
    (capture × Σ daily-range × scalp-lots). For crypto we walk the real 1m path and report the
    measured number alongside, carrying stuck parts (never force-closing — #1).
Not financial advice — educational reproduction of a third-party method.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import options as opt

TRADING_DAYS = 252


@dataclass
class PiSimResult:
    # --- request echo ------------------------------------------------------------------
    ticker: str = ""
    vol_model: str = ""
    deposit: float = 0.0
    risk_pct: float = 0.0
    dte_days: int = 0
    # --- 1. entry snapshot -------------------------------------------------------------
    entry_date: str = ""
    expiry_date: str = ""
    S0: float = 0.0
    K: float = 0.0
    iv: float = 0.0
    r: float = 0.0
    call_price: float = 0.0
    put_price: float = 0.0
    straddle_unit_cost: float = 0.0      # 2·call (cost of one 2C−1F straddle unit)
    premium_budget: float = 0.0          # risk_pct · deposit (what you actually spend)
    straddle_units: float = 0.0          # M
    n_calls: float = 0.0                 # 2M
    n_futures: float = 0.0               # M (short, the delta-neutral base)
    breakeven_move: float = 0.0          # 2·c0 — move needed for the straddle to pay off ($)
    breakeven_pct: float = 0.0
    # --- 2. three-thirds + grid --------------------------------------------------------
    intraday_frac: float = 0.0
    intraday_limit_lots: float = 0.0     # ETH of futures dedicated to scalping (⅓ of calls)
    n_parts: int = 0
    part_lots: float = 0.0               # ETH per working part
    daily_atr: float = 0.0
    first_step: float = 0.0
    grid_mult: float = 0.0
    grid: list[dict] = field(default_factory=list)   # [{part, offset, sell, buy, lots}]
    # --- 3. period outcome -------------------------------------------------------------
    S_T: float = 0.0
    move_abs: float = 0.0
    move_pct: float = 0.0
    n_days: int = 0
    straddle_gross: float = 0.0          # M·|S_T−S0| (intrinsic at expiry)
    theta_cost: float = 0.0              # = premium_budget (the rent, max loss)
    straddle_net: float = 0.0            # gross − premium
    # scalp
    scalp_capture: float = 0.0
    sum_daily_range: float = 0.0
    scalp_scenario: float = 0.0          # capture × Σ range × limit (daily, labelled scenario)
    scalp_realized: float | None = None  # closed round-trips on the real 1m path (the "flat income")
    scalp_open_mtm: float | None = None  # stuck counter-trend legs marked at S_T (≤0 in a trend)
    scalp_round_trips: int = 0
    scalp_source: str = "scenario"       # "1m-measured" | "scenario"
    scalp_income: float = 0.0            # TRUE scalp contribution to the account (realized + stuck mtm)
    coverage: float = 0.0                # scalp_realized / theta_cost  (≥1 = flat self-pays the rent)
    # --- 4. verdict + presentation -----------------------------------------------------
    total_net: float = 0.0               # straddle_net + scalp_income
    total_pct: float = 0.0               # of deposit
    verdict: str = ""
    steps: list[dict] = field(default_factory=list)   # narrated walkthrough
    timeline: dict = field(default_factory=dict)      # {dates, close} for the chart


def _build_grid(center: float, first_step: float, grid_mult: float, n_parts: int,
                part_lots: float) -> list[dict]:
    """Exponential grid: cumulative offsets step·(1+m+m²+…), symmetric sell/buy levels."""
    rows, acc = [], 0.0
    for k in range(n_parts):
        acc += first_step * (grid_mult ** k)
        rows.append({"part": k + 1, "offset": round(acc, 4),
                     "sell": round(center + acc, 4), "buy": round(center - acc, 4),
                     "lots": round(part_lots, 4)})
    return rows


def measure_scalp_1m(intraday: pd.DataFrame, center: float, grid: list[dict], n_parts: int,
                     *, fee: float = 0.0, bb_window: int = 120, bb_k: float = 2.0) -> tuple[float, float, int]:
    """Walk the real 1-minute path with the counter-trend exponential grid (mirrors the Tab-8 engine).

    Response orders: short at each sell-level crossed up, target = the next inner level; long at each
    buy-level crossed down, target = next inner level. Stuck parts are CARRIED (never force-closed) —
    they're marked-to-market at the final price (INVARIANT #1). A Bollinger FLAT-GATE (doctrine: «don't
    fade a galloping market») suspends NEW counter-trend entries on a breakout — no new short above the
    upper band / no new long below the lower band; EXITS are always allowed. Set bb_k=0 to disable.
    Returns (realized, open_mtm, round_trips).
    """
    if intraday is None or intraday.empty:
        return 0.0, 0.0, 0
    sell_lv = [g["sell"] for g in grid]
    buy_lv = [g["buy"] for g in grid]
    inner_up = [center] + sell_lv[:-1]   # buy-back target for a short opened at sell_lv[k]
    inner_dn = [center] + buy_lv[:-1]    # sell target for a long opened at buy_lv[k]
    lots = [g["lots"] for g in grid]
    sarm = [True] * n_parts              # short-arm rearmed?
    barm = [True] * n_parts              # long(buy)-arm rearmed?
    legs: list[dict] = []
    realized = 0.0
    rts = 0
    O = intraday["Open"].to_numpy(float); H = intraday["High"].to_numpy(float)
    L = intraday["Low"].to_numpy(float);  C = intraday["Close"].to_numpy(float)
    # FLAT-GATE bands: rolling mean ± k·std of the 1m close (don't open counter-trend INTO a breakout)
    if bb_k > 0 and len(C) >= 5:
        cs = pd.Series(C)
        mid = cs.rolling(bb_window, min_periods=5).mean()
        sd = cs.rolling(bb_window, min_periods=5).std(ddof=0).fillna(0.0)
        ub = (mid + bb_k * sd).to_numpy(); lb = (mid - bb_k * sd).to_numpy()
    else:
        ub = np.full(len(C), np.inf); lb = np.full(len(C), -np.inf)
    for i in range(len(C)):
        o, h, l, c = O[i], H[i], L[i], C[i]
        up_i, lo_i = ub[i], lb[i]
        path = [o, l, h, c] if c >= o else [o, h, l, c]   # intrabar traversal guess
        for a, b in zip(path, path[1:]):
            if b > a:                                      # rising segment
                for k in range(n_parts):
                    lv = sell_lv[k]
                    if sarm[k] and a < lv <= b and not (np.isfinite(up_i) and lv > up_i):
                        legs.append({"side": "S", "k": k, "entry": lv, "target": inner_up[k],
                                     "lots": lots[k]})
                        sarm[k] = False
                for leg in legs[:]:
                    if leg["side"] == "L" and a < leg["target"] <= b:
                        realized += (leg["target"] - leg["entry"]) * leg["lots"]
                        realized -= fee * leg["lots"] * leg["target"]
                        rts += 1; barm[leg["k"]] = True; legs.remove(leg)
            elif b < a:                                    # falling segment
                for k in range(n_parts):
                    lv = buy_lv[k]
                    if barm[k] and b <= lv < a and not (np.isfinite(lo_i) and lv < lo_i):
                        legs.append({"side": "L", "k": k, "entry": lv, "target": inner_dn[k],
                                     "lots": lots[k]})
                        barm[k] = False
                for leg in legs[:]:
                    if leg["side"] == "S" and b <= leg["target"] < a:
                        realized += (leg["entry"] - leg["target"]) * leg["lots"]
                        realized -= fee * leg["lots"] * leg["target"]
                        rts += 1; sarm[leg["k"]] = True; legs.remove(leg)
    S_T = float(C[-1])
    open_mtm = sum(((S_T - leg["entry"]) if leg["side"] == "L" else (leg["entry"] - S_T)) * leg["lots"]
                   for leg in legs)
    return realized, open_mtm, rts


def simulate(daily: pd.DataFrame, vol_model, *, ticker: str, deposit: float, start: str,
             dte_days: int, risk_pct: float, n_parts: int = 5, grid_atr_frac: float = 0.5,
             grid_mult: float = 2.0, intraday_frac: float = 1.0 / 3.0, capture: float = 0.20,
             r: float = 0.045, atr_period: int = 14, intraday: pd.DataFrame | None = None,
             vol_label: str = "") -> PiSimResult:
    """Run ONE ПИ period: entry → grid → outcome, every figure exposed. `intraday` (optional 1m OHLC)
    enables the MEASURED scalp; otherwise the scalp is the daily-range capture SCENARIO."""
    res = PiSimResult(ticker=ticker, vol_model=vol_label, deposit=deposit, risk_pct=risk_pct,
                      dte_days=dte_days, intraday_frac=intraday_frac, n_parts=n_parts,
                      grid_mult=grid_mult, scalp_capture=capture, r=r)
    df = daily.loc[daily.index >= pd.Timestamp(start)]
    if df.empty or len(df) < 2:
        raise ValueError("no data at/after the start date")
    entry = df.index[0]
    expiry_ts = entry + pd.Timedelta(days=dte_days)
    period = df.loc[df.index <= expiry_ts]
    if len(period) < 2:
        raise ValueError("not enough bars inside the period (shorter DTE or earlier start)")
    S0 = float(period["Close"].iloc[0]); K = S0
    S_T = float(period["Close"].iloc[-1])
    T = dte_days / 365.0
    iv = float(vol_model.sigma(entry, T, K, S0)) if vol_model is not None else 0.20

    # --- 1. entry snapshot: synthetic straddle sizing -----------------------------------
    c0 = float(opt.call_price(S0, K, T, r, iv))
    p0 = float(opt.put_price(S0, K, T, r, iv))
    unit_cost = 2.0 * c0                                   # one 2C−1F straddle unit
    budget = risk_pct * deposit
    M = budget / unit_cost if unit_cost > 0 else 0.0       # straddle units
    res.entry_date = entry.date().isoformat(); res.expiry_date = period.index[-1].date().isoformat()
    res.S0, res.K, res.iv, res.call_price, res.put_price = S0, K, iv, c0, p0
    res.straddle_unit_cost = unit_cost; res.premium_budget = budget
    res.straddle_units = M; res.n_calls = 2.0 * M; res.n_futures = M
    res.breakeven_move = unit_cost; res.breakeven_pct = 100.0 * unit_cost / S0 if S0 else 0.0

    # --- 2. three-thirds + exponential grid --------------------------------------------
    atr = _atr(daily, atr_period, entry)
    res.daily_atr = atr
    first_step = grid_atr_frac * atr if atr > 0 else S0 * 0.01
    limit = max(2.0 * M * intraday_frac, 0.0)              # ⅓ of CALLS = the scalp limit (in ETH)
    part_lots = limit / max(1, n_parts)
    res.intraday_limit_lots = limit; res.part_lots = part_lots; res.first_step = first_step
    res.grid = _build_grid(S0, first_step, grid_mult, n_parts, part_lots)

    # --- 3a. straddle core outcome (real price path) -----------------------------------
    move = abs(S_T - S0)
    res.S_T, res.move_abs, res.move_pct = S_T, move, (100.0 * (S_T - S0) / S0 if S0 else 0.0)
    res.n_days = len(period)
    res.straddle_gross = M * move                          # M units × intrinsic |S_T−S0|
    res.theta_cost = budget                                # premium paid = the rent = max loss
    res.straddle_net = res.straddle_gross - budget

    # --- 3b. scalp: scenario (daily) + measured (1m, crypto) ---------------------------
    rng = (period["High"] - period["Low"]).clip(lower=0).sum()
    res.sum_daily_range = float(rng)
    res.scalp_scenario = float(capture * rng * limit)      # capture × Σ range × scalp lots
    if intraday is not None and not intraday.empty:
        islice = intraday.loc[(intraday.index >= entry) & (intraday.index <= period.index[-1] + pd.Timedelta(days=1))]
        realized, open_mtm, rts = measure_scalp_1m(islice, S0, res.grid, n_parts)
        res.scalp_realized = round(realized, 2); res.scalp_open_mtm = round(open_mtm, 2)
        res.scalp_round_trips = rts; res.scalp_source = "1m-measured"
        # TRUE scalp contribution = booked round-trips + the mark of stuck counter-trend legs (≤0 in a
        # trend; INVARIANT #3 — the scalp bleeds in a trend while the straddle gamma wins). Coverage —
        # "does the FLAT income pay the rent" — uses the booked round-trips only.
        res.scalp_income = realized + open_mtm
    else:
        res.scalp_source = "scenario"
        res.scalp_realized = round(res.scalp_scenario, 2)
        res.scalp_income = res.scalp_scenario
    res.coverage = (max(res.scalp_realized or 0.0, 0.0) / res.theta_cost) if res.theta_cost > 0 else 0.0

    # --- 4. totals + verdict ------------------------------------------------------------
    res.total_net = res.straddle_net + res.scalp_income
    res.total_pct = 100.0 * res.total_net / deposit if deposit else 0.0
    res.verdict = _verdict(res)
    res.steps = _narrate(res)
    cl = period["Close"]
    res.timeline = {"dates": [d.date().isoformat() for d in cl.index], "close": [round(float(x), 2) for x in cl.values]}
    return res


def _atr(daily: pd.DataFrame, period: int, asof) -> float:
    """Plain Wilder-ish ATR (mean true range) over `period` bars ending at/just before `asof`."""
    d = daily.loc[daily.index <= asof]
    if len(d) < 2:
        d = daily
    h, l, c = d["High"].to_numpy(float), d["Low"].to_numpy(float), d["Close"].to_numpy(float)
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    n = min(period, len(tr))
    return float(np.mean(tr[-n:])) if n > 0 else 0.0


def _verdict(r: PiSimResult) -> str:
    cov = r.coverage
    asset = r.ticker.split("-")[0]
    head = (f"ПЛЮС +${r.total_net:,.0f}" if r.total_net >= 0 else f"МИНУС ${r.total_net:,.0f}") + \
           f" ({r.total_pct:+.1f}% депозита за {r.n_days} дн)."
    move_txt = (f"{asset} прошёл {r.move_pct:+.1f}% (нужно ≥±{r.breakeven_pct:.1f}%, чтобы стреддл сам вышел в плюс) → "
                f"гамма-ядро {'+' if r.straddle_net>=0 else ''}${r.straddle_net:,.0f}.")
    if r.scalp_source == "1m-measured":
        stuck = r.scalp_open_mtm or 0.0
        cov_txt = (f"Скальп по реальному 1-мин пути: {r.scalp_round_trips} закрытых кругов +${r.scalp_realized:,.0f} "
                   f"= {cov*100:.0f}% теты (${r.theta_cost:,.0f})")
        if stuck < -1.0:
            cov_txt += (f", НО залипшие контр-трендовые части −${abs(stuck):,.0f}: на тренде скальп истекает, "
                        f"а зарабатывает гамма стреддла (это по доктрине, не баг). Итог скальпа ${r.scalp_income:,.0f}.")
        else:
            cov_txt += (". Флет — скальп почти без залипания: "
                        + ("сам окупает тету (≥100%)." if cov >= 1.0 else "часть теты добивает гамма."))
    else:
        cov_txt = (f"Скальп — СЦЕНАРИЙ захвата {r.scalp_capture*100:.0f}% дневного хода (дневные бары НЕ содержат "
                   f"интрадей-кругов): ${r.scalp_scenario:,.0f} ≈ {cov*100:.0f}% теты. Для измерения нужен 1-мин фид (крипта).")
    return f"{head} {move_txt} {cov_txt} Макс. риск всё время ограничен премией ${r.theta_cost:,.0f}."


def _narrate(r: PiSimResult) -> list[dict]:
    """The step-by-step «в деньгах» walkthrough the frontend renders as numbered cards."""
    return [
        {"n": 1, "title": "Депозит и бюджет на премию",
         "body": (f"Депозит ${r.deposit:,.0f}. По доктрине на риск за период выделяем {r.risk_pct*100:.0f}% → "
                  f"бюджет на опционы ${r.premium_budget:,.0f}. Это же — наш МАКСИМАЛЬНЫЙ убыток за период.")},
        {"n": 2, "title": "Что покупаем (синтетический стреддл 2C−1F)",
         "body": (f"{r.ticker} на {r.entry_date} = ${r.S0:,.0f}. Берём ATM-страйк ${r.K:,.0f}. "
                  f"Месячный центральный Колл (IV {r.iv*100:.0f}%) стоит ${r.call_price:,.2f}. "
                  f"1 ед. стреддла = 2 Колла − 1 Фьюч = ${r.straddle_unit_cost:,.2f}. "
                  f"На ${r.premium_budget:,.0f} берём {r.straddle_units:.2f} ед. → "
                  f"КУПИТЬ {r.n_calls:.2f} Колла, ПРОДАТЬ {r.n_futures:.2f} Фьюча (дельта-нейтраль).")},
        {"n": 3, "title": "Правило трёх третей → лимит на скальп",
         "body": (f"Из {r.n_calls:.2f} коллов под интрадей выделяем {r.intraday_frac*100:.0f}% = "
                  f"{r.intraday_limit_lots:.2f} {r.ticker.split('-')[0]} лимита, дробим на {r.n_parts} рабочих части по "
                  f"{r.part_lots:.3f} каждая. Остальное — дельта-нейтральная база (тянет в плюс на тренде).")},
        {"n": 4, "title": "Экспоненциальная сетка (реальные цены)",
         "body": ("Расставляем части от центра ${:,.0f}, шаг ×{:g} (первый ≈ {:.1f}% от цены): ".format(
                      r.S0, r.grid_mult, 100.0 * r.first_step / r.S0 if r.S0 else 0.0)
                  + "; ".join(f"ч.{g['part']}: прод ${g['sell']:,.0f} / покуп ${g['buy']:,.0f}" for g in r.grid))},
        {"n": 5, "title": f"Прогон периода ({r.entry_date} → {r.expiry_date})",
         "body": (f"{r.ticker} ушёл с ${r.S0:,.0f} до ${r.S_T:,.0f} ({r.move_pct:+.1f}%). "
                  f"Стреддл-ядро на экспирации: {r.straddle_units:.2f}×${r.move_abs:,.0f} интринсика "
                  f"− ${r.theta_cost:,.0f} премии = {'+' if r.straddle_net>=0 else ''}${r.straddle_net:,.0f}.")},
        {"n": 6, "title": ("Скальпинг — ИЗМЕРЕНО по реальному 1-мин пути" if r.scalp_source == "1m-measured"
                           else "Скальпинг — СЦЕНАРИЙ (дневные бары не видят интрадей)"),
         "body": (
             (f"Прогнали сетку по реальному 1-минутному пути ({r.scalp_round_trips} закрытых кругов): "
              f"booked +${r.scalp_realized:,.0f} = {r.coverage*100:.0f}% теты. "
              + (f"Залипшие контр-трендовые части по рынку на экспирации: ${r.scalp_open_mtm:,.0f} "
                 f"(на тренде скальп в минусе — отрабатывает гамма; по доктрине части НЕ закрывают силой). "
                 if (r.scalp_open_mtm or 0.0) < -1.0 else
                 f"Залипания почти нет (${r.scalp_open_mtm:,.0f}) — это флет. ")
              + f"Итоговый вклад скальпа: ${r.scalp_income:,.0f}.")
             if r.scalp_source == "1m-measured" else
             (f"Захват {r.scalp_capture*100:.0f}% дневного хода (Σ диапазонов ${r.sum_daily_range:,.0f}) на "
              f"{r.intraday_limit_lots:.2f} лотах = +${r.scalp_scenario:,.0f} ≈ {r.coverage*100:.0f}% теты. "
              f"⚠ это СЦЕНАРИЙ при заданном захвате, а НЕ измерение — дневные бары не содержат интрадей-кругов. "
              f"Реальное измерение скальпа возможно только на 1-мин фиде (крипта). Это верхняя планка для флета."))},
        {"n": 7, "title": "Итог в деньгах",
         "body": (f"Ядро {'+' if r.straddle_net>=0 else ''}${r.straddle_net:,.0f} + скальп "
                  f"{'+' if r.scalp_income>=0 else ''}${r.scalp_income:,.0f} = "
                  f"ИТОГО {'+' if r.total_net>=0 else ''}${r.total_net:,.0f} ({r.total_pct:+.1f}% депозита) за {r.n_days} дн. "
                  f"Риск всё это время был ограничен премией ${r.theta_cost:,.0f} (это и есть «безрисковость» — "
                  f"маркетинг: реальный потолок убытка = вся премия).")},
    ]
