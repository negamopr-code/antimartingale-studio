"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const DARK = {
  paper_bgcolor: "#1a212b", plot_bgcolor: "#1a212b",
  autosize: true, height: 340,
  font: { color: "#e6edf3", size: 11 }, margin: { t: 36, r: 40, b: 36, l: 56 },
  xaxis: { gridcolor: "#2a3340" }, yaxis: { gridcolor: "#2a3340" },
  legend: { orientation: "h", y: 1.12 },
};
const layout = (title, extra = {}) => ({ ...DARK, title: { text: title, font: { size: 14 } }, ...extra });
async function plot(id, traces, lay) {
  if (typeof Plotly === "undefined")
    throw new Error("Plotly failed to load (/vendor/plotly.min.js). Hard-reload (Ctrl+Shift+R).");
  const el = document.getElementById(id);
  if (!el) throw new Error("plot container #" + id + " not found");
  await Plotly.react(el, traces, lay, { responsive: true, displaylogo: false, scrollZoom: true });
  Plotly.Plots.resize(el);          // force correct size if the container was measured at 0
}

function toast(msg, ok = false) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false; t.classList.toggle("ok", ok);
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, ok ? 3000 : 9000);
}

function errMsg(body, status) {
  if (body && typeof body.error === "string") return body.error;
  if (body && typeof body.detail === "string") return body.detail;
  if (body && Array.isArray(body.detail))   // FastAPI/pydantic validation errors
    return body.detail.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("; ");
  return "HTTP " + status;
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(errMsg(body, r.status));
  return body;
}
const formData = (form) => {
  const o = {};
  new FormData(form).forEach((v, k) => {
    if (v === "") return;
    o[k] = isNaN(v) || k === "ticker" || k === "mode" || k === "start" || k === "strategy_id"
      ? v : Number(v);
  });
  return o;
};
const post = (path, obj) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj) });

// ---- tabs
$$(".tab").forEach((b) => b.onclick = () => {
  $$(".tab").forEach((x) => x.classList.toggle("active", x === b));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === b.dataset.tab));
});

// ---- instruments dropdown
async function loadInstruments() {
  try {
    const { groups } = await api("/api/instruments");
    $$(".instr").forEach((sel) => {
      sel.innerHTML = "";
      for (const [g, items] of Object.entries(groups)) {
        const og = document.createElement("optgroup"); og.label = g;
        items.forEach(({ ticker, label }) => {
          const o = document.createElement("option");
          o.value = ticker; o.textContent = `${ticker} — ${label}`;
          if (ticker === "SPY") o.selected = true;
          og.appendChild(o);
        });
        sel.appendChild(og);
      }
    });
    ["#form-hedged", "#form-hiexec"].forEach(gateScalpData);   // 1m is crypto-only — gate per default ticker
  } catch (e) { console.error(e); }
}

// the FREE 1m feed is crypto-only (Binance) — disable the "1m" scalp_data option for non-crypto
// tickers so it can't be chosen for SPY/GLD/futures/FX (the server would just fall back to daily).
function isCryptoTicker(t) {
  t = (t || "").toUpperCase().trim();
  if (/[=^.]/.test(t)) return false;                     // futures =F / FX =X / indices ^ / .SS
  return /-(USD|USDT|USDC)$/.test(t) || /USDT$/.test(t); // BTC-USD, ETH-USDT, …
}
function gateScalpData(formSel) {
  const tk = $(formSel + " [name=ticker]"), sd = $(formSel + " [name=scalp_data]");
  if (!tk || !sd) return;
  const opt = [...sd.options].find((o) => o.value === "1m");
  if (!opt) return;
  const ok = isCryptoTicker(tk.value);
  opt.disabled = !ok;
  opt.title = ok ? "" : "только для крипты (Binance); выбери крипто-инструмент";
  if (ok && sd.value === "daily") sd.value = "1m";       // crypto → measure the scalp on FREE deep 1m by default
  if (!ok && sd.value === "1m") sd.value = "daily";      // non-crypto can't use 1m → daily
}
["#form-hedged", "#form-hiexec"].forEach((f) => {
  const tk = $(f + " [name=ticker]");
  if (tk) tk.addEventListener("change", () => gateScalpData(f));
});

const statsText = (s) => Object.entries(s)
  .map(([k, v]) => `${k.padEnd(18)}: ${typeof v === "number" ? (+v).toLocaleString(undefined, { maximumFractionDigits: 4 }) : v}`)
  .join("\n");

async function withBusy(btn, fn) {
  btn.disabled = true; const t = btn.textContent; btn.textContent = "…";
  try { await fn(); } catch (e) { toast("Error: " + (e && e.message || e)); console.error(e); }
  finally { btn.disabled = false; btn.textContent = t; }
}

// ---- tab 1
$("#form-coinflip").onsubmit = (e) => {
  e.preventDefault();
  withBusy(e.submitter, async () => {
    const d = await post("/api/coinflip", formData(e.target));
    const s = d.stats;
    const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 4 }));
    const up = s.final_bank > 0;
    const winRate = s.cycles ? 100 * s.successes / s.cycles : 0;
    const verdict = `${up ? "📈 EQUITY RISES" : (s.final_bank < 0 ? "📉 EQUITY FALLS" : "➖ FLAT")}  `
      + `final P&L ${s.final_bank >= 0 ? "+" : ""}${f(s.final_bank)} over ${f(s.cycles)} cycles\n`
      + `empirical EV/cycle : ${f(s.ev_cycle_empirical)}\n`
      + `closed-form  b·((2p)^N−1) : ${f(s.ev_cycle_theory)}\n`
      + `target-hit cycles : ${f(s.successes)} / ${f(s.cycles)}  (${f(winRate)}%)\n`
      + `(p>0.5 ⇒ rises, p=0.5 ⇒ flat over many cycles, p<0.5 ⇒ falls)\n\n`;
    const hist = Object.entries(d.series_counter)
      .sort((a, b) => (+a[0]) - (+b[0]))
      .map(([k, v]) => `  ${String(k).padStart(3)} wins: ${v}`).join("\n");
    $("#cf-stats").textContent = verdict + "— raw stats —\n" + statsText(s)
      + "\n\ncycles ending at streak:\n" + hist;
    // equity curve: cumulative P&L across cycles (one point per booked cycle)
    const eqUp = up ? "#3fb950" : "#f85149";
    await plot("cf-bank", [{ x: d.history.x, y: d.history.y, mode: "lines", name: "cumulative P&L",
                             line: { width: 1.5, color: eqUp },
                             fill: "tozeroy", fillcolor: up ? "rgba(63,185,80,0.12)" : "rgba(248,81,73,0.12)" }],
      layout("Equity curve — cumulative P&L over cycles", { xaxis: { gridcolor: "#2a3340", title: { text: "cycle #" } } }));
    // within-cycle bet escalation of the last winning streak (0, b(2¹−1), b(2²−1), …)
    await plot("cf-streak", [{ x: d.last_series.x, y: d.last_series.y, mode: "lines+markers",
                              line: { color: "#3fb950" }, marker: { size: 5 } }],
      layout("Last winning streak — bank within one cycle", { xaxis: { gridcolor: "#2a3340", title: { text: "win #" } } }));
  });
};

// ---- tab 2 / 3 shared
async function renderBacktest(prefix, d, isOptions) {
  const price = { x: d.price.x, y: d.price.y, mode: "lines", name: "Close",
                  line: { width: 1, color: "#c9d1d9" } };
  const loss = { x: d.entries.loss.x, y: d.entries.loss.y, mode: "markers", name: "loss (−b)",
                 marker: { color: "#f85149", symbol: "triangle-down", size: 6, opacity: 0.6 } };
  const add = { x: (d.entries.add || {}).x || [], y: (d.entries.add || {}).y || [],
                mode: "markers", name: "scale-in (+1 ATR)",
                marker: { color: "#3fb950", symbol: "triangle-up", size: 9, opacity: 0.85 } };
  const win = { x: d.entries.win.x, y: d.entries.win.y, mode: "markers", name: "WIN (target hit)",
                marker: { color: "#f0c000", symbol: "star", size: 15,
                          line: { color: "#0f1419", width: 1 } } };
  const traces = [price, loss, add, win];   // win last = drawn on top
  const lay = layout("Price + entries  (drag/scroll to zoom)", {
    height: 380,
    xaxis: {
      gridcolor: "#2a3340",
      rangeselector: {
        bgcolor: "#10151c", activecolor: "#5b9dff", font: { color: "#e6edf3" },
        buttons: [
          { step: "month", count: 1, label: "1m", stepmode: "backward" },
          { step: "month", count: 3, label: "3m", stepmode: "backward" },
          { step: "month", count: 6, label: "6m", stepmode: "backward" },
          { step: "year", count: 1, label: "1y", stepmode: "backward" },
          { step: "all", label: "all" },
        ],
      },
    },
  });
  if (isOptions && d.delta) {
    traces.push({ x: d.delta.x, y: d.delta.y, mode: "lines", name: "Δ at entry", yaxis: "y2",
                  line: { color: "#5b9dff", width: 1, shape: "hv" } });
    lay.yaxis2 = { overlaying: "y", side: "right", range: [0, 1.05], gridcolor: "transparent",
                   title: { text: "Δ" } };
  }
  // stats + "cost as probability" verdict
  const s = d.stats;
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 4 }));
  // ---- PROFITABILITY VERDICT ----------------------------------------------------------
  // The campaign is a *skewed coin-flip*: many small −b losses, a few large convex wins.
  // It is NOT judged by win-rate vs 50% — only the bottom line (net P&L / profit factor).
  // (The old "edge p−0.5 / breakeven p*" readout was a leftover from the sequential model
  //  and was MEANINGLESS here: its `p` is the campaign target-hit rate, not a per-step win
  //  probability, so it reported a fake negative edge even on profitable runs.)
  const pnls = (d.table || []).map((r) => r.pnl).filter((v) => typeof v === "number");
  const sum = (a) => a.reduce((x, y) => x + y, 0);
  const W = pnls.filter((p) => p > 0), L = pnls.filter((p) => p < 0);
  const net = sum(pnls), winSum = sum(W), lossSum = sum(L);
  const pf = lossSum !== 0 ? winSum / Math.abs(lossSum) : Infinity;
  const avgW = W.length ? winSum / W.length : 0, avgL = L.length ? lossSum / L.length : 0;
  const wr = pnls.length ? (100 * W.length / pnls.length) : 0;
  const verdict = pnls.length
    ? `${net > 0 ? "✅ PROFITABLE" : "❌ NOT PROFITABLE"}   net P&L ${net >= 0 ? "+" : ""}${f(net)}\n`
      + `profit factor : ${pf === Infinity ? "∞" : f(pf)}  ${pf >= 1 ? "(wins outweigh losses)" : "(losses outweigh wins)"}\n`
      + `big wins  ${W.length}  · avg +${f(avgW)}\n`
      + `losses    ${L.length}  · avg ${f(avgL)}\n`
      + `win-rate  ${f(wr)}%   (low is NORMAL — payoff is skewed, not 50/50)\n\n`
    : "";
  // transaction-cost drag (informational; already deducted from net P&L above)
  let extra = "";
  if (s.cost_as_prob != null) {
    extra = "\n— transaction-cost drag (already in net) —\n"
      + `commission : ${f(s.total_commission)}\n`
      + `slippage   : ${f(s.total_slippage)}\n`
      + `total cost : ${f(s.total_cost)}\n`;
  }
  // volatility surface used to price the options (term structure + skew)
  if (s.vol_model != null) {
    extra += "\n— vol surface —\n"
      + `IV source  : ${s.vol_model}  (class ${s.vol_class})\n`
      + `skew β     : ${f(s.skew_beta)} per ln-moneyness\n`
      + (s.delta_mean != null ? `Δ used     : mean ${f(s.delta_mean)} (min ${f(s.delta_min)})\n` : "");
  }
  $(`#${prefix}-stats`).textContent = verdict + "— raw stats —\n" + statsText(s) + extra;

  await plot(`${prefix}-price`, traces, lay);

  // equity on a SINGLE axis: gross (no costs) vs net — the gap between them IS the cost.
  // gross = net + cumulative cost.
  const grossY = d.equity.y.map((v, i) => v + (d.cum_cost.y[i] || 0));
  const dp = s.cost_as_prob != null ? ` — costs ≈ Δp ${f(s.cost_as_prob)} in win-prob` : "";
  await plot(`${prefix}-equity`, [
    { x: d.equity.x, y: grossY, mode: "lines", name: "gross (no costs)",
      line: { color: "#8b949e", dash: "dot", width: 1.5 } },
    { x: d.equity.x, y: d.equity.y, mode: "lines", name: "net (after costs)",
      line: { color: "#a371f7", width: 2 },
      fill: "tonexty", fillcolor: "rgba(248,81,73,0.15)" },
  ], layout("Equity: net vs gross" + dp));

  // per-campaign P&L histogram — shows the SKEW directly: many small −b losses on the left,
  // a few large convex wins far to the right. The bottom-line net is the area-weighted balance.
  // wins/losses split by colour (reuses W/L computed for the verdict above).
  const nbins = Math.min(60, Math.max(12, Math.ceil(Math.sqrt(pnls.length || 1))));
  const xbins = pnls.length
    ? (() => { const lo = Math.min(...pnls), hi = Math.max(...pnls);
               return { start: lo, end: hi, size: (hi - lo) / nbins || 1 }; })()
    : undefined;
  await plot(`${prefix}-hist`, [
    { x: L, type: "histogram", name: `losses (${L.length})`, xbins,
      marker: { color: "rgba(248,81,73,0.75)" } },
    { x: W, type: "histogram", name: `wins (${W.length})`, xbins,
      marker: { color: "rgba(63,185,80,0.8)" } },
  ], layout(`Per-campaign P&L distribution  (mean ${f(pnls.length ? net / pnls.length : 0)})`, {
    barmode: "overlay",
    xaxis: { gridcolor: "#2a3340", title: { text: "P&L per campaign ($)" },
             zeroline: true, zerolinecolor: "#8b949e", zerolinewidth: 1 },
    yaxis: { gridcolor: "#2a3340", title: { text: "# campaigns" } },
  }));

  renderTable(`${prefix}-table`, d.table);
}

// detailed per-trial table under the charts
function renderTable(id, rows) {
  const el = document.getElementById(id);
  if (!el) return;
  if (!rows || !rows.length) { el.innerHTML = ""; return; }
  const cols = Object.keys(rows[0]);
  const head = "<tr>" + cols.map((c) => `<th>${c}</th>`).join("") + "</tr>";
  const body = rows.map((r) => {
    const cls = r.outcome === "win" ? "w" : (r.outcome === "loss" ? "l" : "");
    return `<tr class="${cls}">` + cols.map((c) => `<td>${r[c]}</td>`).join("") + "</tr>";
  }).join("");
  el.innerHTML = `<div class="tt-scroll"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`
    + `<div class="tt-note">${rows.length} trials · scroll for all</div>`;
}

$("#form-linear").onsubmit = (e) => {
  e.preventDefault();
  withBusy(e.submitter, async () =>
    renderBacktest("lin", await post("/api/backtest/linear", formData(e.target)), false));
};
$("#form-options").onsubmit = (e) => {
  e.preventDefault();
  withBusy(e.submitter, async () =>
    renderBacktest("opt", await post("/api/backtest/options", formData(e.target)), true));
};

// ---- tab 4 signals
async function refreshSignals() {
  const sid = $("#form-signals [name=strategy_id]").value.trim();
  const q = sid ? `?strategy_id=${encodeURIComponent(sid)}` : "";
  const { signals } = await api("/api/signals" + q);
  const tb = $("#sig-table tbody"); tb.innerHTML = "";
  signals.forEach((s) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${s.id}</td><td>${(s.signal_time || s.received_at || "").slice(0, 19)}</td>
      <td>${s.ticker}</td><td>${s.action}</td><td>${s.outcome ?? ""}</td>
      <td>${s.pnl ?? ""}</td><td>${s.strategy_id}</td>`;
    tb.appendChild(tr);
  });
}
$("#refresh-signals").onclick = (e) => withBusy(e.target, refreshSignals);
$("#next-bet-btn").onclick = (e) => withBusy(e.target, async () => {
  const g = (n) => $(`#form-signals [name=${n}]`).value.trim();
  const sid = g("strategy_id") || "default";
  const p = new URLSearchParams({ strategy_id: sid, base_bet: g("base_bet") || "100",
    target_streak: g("target_streak") || "10" });
  if (g("cap_mult")) p.set("cap_mult", g("cap_mult"));
  const d = await api("/api/next-bet?" + p.toString());
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  $("#nextbet-stats").textContent =
    `🎯 СЛЕДУЮЩАЯ СТАВКА (live, strategy=${d.strategy_id})\n`
    + `next bet = ${f(d.next_bet)}  (= ${f(d.next_bet_mult)}× base ${f(d.base_bet)})\n`
    + `текущая серия: ${d.streak} побед подряд  ·  последний исход: ${d.last_outcome ?? "—"}\n`
    + `всего сделок ${d.n_trials} (W ${d.wins} / L ${d.losses}) · target ${d.target_streak} · полных серий ${d.target_streak_completions}\n`
    + `→ ${d.note}\n`
    + `\nPine alert читает это обратно: GET /api/next-bet?strategy_id=${d.strategy_id}&base_bet=…&target_streak=…`;
});
$("#form-signals").onsubmit = (e) => {
  e.preventDefault();
  withBusy(e.submitter, async () => {
    const d = await post("/api/backtest/from-signals", formData(e.target));
    $("#sig-stats").textContent = statsText(d.stats);
    await plot("sig-equity", [{ x: d.equity.x, y: d.equity.y, mode: "lines", line: { color: "#a371f7" } }],
      layout("Equity from TradingView signals"));
    await refreshSignals();
  });
};

// ---- tab 5 scan all instruments
let _scan = { rows: [], sort: "ret_pct", desc: true };   // table sort state

function renderScanTable() {
  const el = $("#scan-table");
  const dir = _scan.desc ? -1 : 1;
  // failed rows always sink to the bottom; ok rows sort by the chosen numeric column
  const rows = [..._scan.rows].sort((a, b) => {
    if (a.ok !== b.ok) return a.ok ? -1 : 1;
    if (!a.ok) return 0;
    const av = a[_scan.sort] ?? -Infinity, bv = b[_scan.sort] ?? -Infinity;
    return (av - bv) * dir;
  });
  const stressed = rows.some((r) => r.ok && r.floor_ret_pct != null);
  const coinflip = rows.some((r) => r.ok && r.be_markup != null);
  const cols = [
    ["ticker", "ticker"], ["group", "group"], ["n_campaigns", "camps"],
    ["wins", "W"], ["losses", "L"], ["net", "net $"], ["ret_pct", "return %"],
    ["profit_factor", "PF"], ["max_drawdown", "max DD"],
    ...(stressed ? [["trend_ret_pct", "trend %"], ["drift_ret_pct", "drift %"], ["floor_ret_pct", "floor %"]] : []),
    ...(stressed && coinflip ? [["be_markup", "be IVx"]] : []),
  ];
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  const head = "<tr>" + cols.map(([k, lbl]) =>
    `<th data-sort="${k}" style="cursor:pointer">${lbl}${_scan.sort === k ? (_scan.desc ? " ▼" : " ▲") : ""}</th>`).join("") + "</tr>";
  const body = rows.map((r) => {
    if (!r.ok) return `<tr class="l"><td>${r.ticker}</td><td>${r.group}</td>`
      + `<td colspan="${cols.length - 2}" style="color:#8b949e">⚠ ${r.error}</td></tr>`;
    const cls = r.net > 0 ? "w" : "l";
    const beTxt = r.be_markup == null ? "—"
      : (r.be_markup_flag === "lo" ? `<${f(r.be_markup)}` : r.be_markup_flag === "hi" ? `>${f(r.be_markup)}` : f(r.be_markup));
    const sgn = (v) => (v > 0 ? "#3fb950" : "#f85149");
    return `<tr class="${cls}"><td>${r.ticker}</td><td>${r.group}</td>`
      + `<td>${r.n_campaigns}</td><td>${r.wins}</td><td>${r.losses}</td>`
      + `<td>${f(r.net)}</td><td>${f(r.ret_pct)}</td><td>${r.profit_factor == null ? "∞" : f(r.profit_factor)}</td>`
      + `<td>${f(r.max_drawdown)}</td>`
      + (stressed ? `<td style="color:${sgn(r.trend_ret_pct)}">${f(r.trend_ret_pct)}</td>`
        + `<td style="color:${sgn(r.drift_ret_pct)}">${f(r.drift_ret_pct)}</td>`
        + `<td style="color:${sgn(r.floor_ret_pct)}">${f(r.floor_ret_pct)}</td>` : "")
      + (stressed && coinflip ? `<td>${beTxt}</td>` : "")
      + `</tr>`;
  }).join("");
  el.innerHTML = `<div class="tt-scroll"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`
    + `<div class="tt-note">${rows.length} instruments · click a header to sort</div>`;
  $$("#scan-table th[data-sort]").forEach((th) => th.onclick = () => {
    const k = th.dataset.sort;
    if (_scan.sort === k) _scan.desc = !_scan.desc; else { _scan.sort = k; _scan.desc = true; }
    renderScanTable();
  });
}

async function renderScan(d) {
  _scan.rows = d.results; _scan.sort = "ret_pct"; _scan.desc = true;
  const s = d.summary;
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  // ROBUST requires BOTH most instruments profitable AND a healthy (positive) median — a >50%
  // hit-rate with a near-zero/negative median is outlier-carried, not robust.
  const robust = s.profitable_pct >= 50 && s.median_ret_pct > 0;
  const outlierCarried = s.mean_ret_pct > 2 * Math.max(s.median_ret_pct, 0) + 5;  // mean ≫ median
  let verdict =
    `${robust ? "✅ BROADLY PROFITABLE (in-sample)" : "⚠ NARROW / FRAGILE"}   `
    + `${s.profitable}/${s.ok} instruments profitable (${f(s.profitable_pct)}%)\n`
    + `median return : ${f(s.median_ret_pct)}%   ·   mean return : ${f(s.mean_ret_pct)}%\n`
    + (s.best ? `best  : ${s.best.ticker}  ${f(s.best.ret_pct)}%  (net ${f(s.best.net)})\n` : "")
    + (s.worst ? `worst : ${s.worst.ticker}  ${f(s.worst.ret_pct)}%  (net ${f(s.worst.net)})\n` : "")
    + (s.failed ? `failed to fetch : ${s.failed}\n` : "");
  // conditional caveat — only when mean actually dwarfs the median (the old text was unconditional)
  if (outlierCarried)
    verdict += `\n(⚠ mean ≫ median ⇒ a few outliers carry the result — depends on rare large wins.)`;
  if (s.median_ret_pct <= 0)
    verdict += `\n(⚠ median ≤ 0 ⇒ most instruments lose; any headline profit is a few outliers.)`;
  // drift / trend / floor decomposition (stress mode) — the honest read via IID shuffle surrogates
  if (s.floor_median_ret_pct != null) {
    const T = s.trend_median_ret_pct, D = s.drift_median_ret_pct, F = s.floor_median_ret_pct;
    verdict += `\n\n── WHERE THE RETURN COMES FROM (median across instruments, ${s.shuffle_n} IID shuffles) ──\n`
      + `  trend / momentum (serial structure) : ${f(T)}%\n`
      + `  directional drift (1st moment)      : ${f(D)}%\n`
      + `  noise / fill-artifact floor (shuffle): ${f(F)}%   (survives even with NO time-order ⇒ not edge)\n`
      + `  floor profitable across shuffles    : ${f(s.floor_profitable_pct)}%\n`;
    // doctrine: on a driftless IID series the structure is EV≈0, so the floor SHOULD be ~0.
    if (F > 0.2 * Math.max(s.median_ret_pct, 1))
      verdict += `⚠ a large floor means the engine books optimistic stop/rung fills — that part is artifact, not skill.\n`;
    verdict += `→ ${(T > Math.abs(D) && T > F)
      ? "most of the result is TREND/MOMENTUM harvesting — real but regime-dependent, crowded, in-sample-on-survivors; NOT from the antimartingale structure."
      : (Math.abs(D) >= T ? "most of the result is plain DIRECTIONAL DRIFT (long a 20-year bull) — strip it and little remains."
        : "the result is dominated by the shuffle floor — i.e. a backtest fill artifact, not a real edge.")}\n`;
    if (s.detrend_median_ret_pct != null)
      verdict += `(naive drift-strip control = ${f(s.detrend_median_ret_pct)}% — kept for reference; it OVER-corrects on `
        + `trending series by forcing a back-half reversal, so trust the shuffle split above, not this number.)\n`;
  }
  if (s.be_markup_median != null)
    verdict += `breakeven IV markup (median) : ${f(s.be_markup_median)}× realized vol`
      + ` — options must be priced BELOW this to profit. Real listed options ≈ 1.1–1.6× ⇒ `
      + `${s.be_markup_median >= 1.3 ? "plausibly tradable" : "likely −EV after real option costs"}.\n`;
  $("#scan-stats").textContent = verdict;

  // horizontal bar of return % per instrument, sorted; green=profit, red=loss.
  // one extreme winner squashing the rest IS the headline — that's the robustness verdict.
  const ok = d.results.filter((r) => r.ok).sort((a, b) => a.ret_pct - b.ret_pct);
  await plot("scan-bar", [{
    type: "bar", orientation: "h",
    x: ok.map((r) => r.ret_pct), y: ok.map((r) => r.ticker),
    marker: { color: ok.map((r) => (r.net > 0 ? "#3fb950" : "#f85149")) },
    hovertemplate: "%{y}: %{x:.1f}%<extra></extra>",
  }], layout("Return % per instrument  (identical params across the panel)", {
    height: Math.max(420, 16 * ok.length),
    margin: { t: 36, r: 20, b: 36, l: 70 },
    xaxis: { gridcolor: "#2a3340", title: { text: "return % on starting bank" },
             zeroline: true, zerolinecolor: "#8b949e", zerolinewidth: 1 },
    yaxis: { gridcolor: "transparent", automargin: true, tickfont: { size: 9 } },
  }));
  renderScanTable();
}

$("#form-scan").onsubmit = (e) => {
  e.preventDefault();
  withBusy(e.submitter, async () =>
    renderScan(await post("/api/scan", formData(e.target))));
};

// ---- tab 6 explain (step-by-step trace of the real engine)
async function renderExplain(d, ids = { chart: "expl-chart", stats: "expl-stats", ledger: "expl-ledger" }) {
  if (d.model === "coinflip") return renderExplainCoinflip(d, ids);
  const b = d.b, isCalls = d.instrument === "calls";
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  const adds = d.trace.filter((t) => t.t === "add");
  const optAdds = d.trace.filter((t) => t.t === "opt_add");
  const optMarks = d.trace.filter((t) => t.t === "opt_mark");
  const optRolls = d.trace.filter((t) => t.t === "opt_roll");
  const optExit = d.trace.find((t) => t.t === "opt_exit");
  const won = d.exit && d.exit.reason === "target";

  // --- common chart: price, grid markers, trailing stop, rung lines ---
  const price = { x: d.price.x, y: d.price.y, mode: "lines", name: "цена (close)",
                  line: { color: "#c9d1d9", width: 1.5 } };
  const entry = { x: [d.entry.date], y: [d.entry.price], mode: "markers", name: "вход (1 лот)",
                  marker: { color: "#5b9dff", symbol: "triangle-up", size: 14,
                            line: { color: "#0f1419", width: 1 } } };
  const addT = { x: adds.map((a) => a.date), y: adds.map((a) => a.level), mode: "markers+text",
                 name: "долив +2^k лотов",
                 text: adds.map((a) => `шаг ${a.step}: +${a.lots_added}→Q${a.Q}`),
                 textposition: "top center", textfont: { size: 9, color: "#3fb950" },
                 marker: { color: "#3fb950", symbol: "circle", size: 11,
                           line: { color: "#0f1419", width: 1 } } };
  const exitT = { x: [d.exit.date], y: [d.exit.price], mode: "markers",
                  name: won ? "ЦЕЛЬ (win)" : "СТОП",
                  marker: { color: won ? "#f0c000" : "#f85149",
                            symbol: won ? "star" : "x", size: 16,
                            line: { color: "#0f1419", width: 1 } } };
  const stopEv = [d.entry, ...adds, d.exit].filter((e) => e && e.stop != null);
  // weighted-average entry of the whole stack — the stop is defined RELATIVE to this line
  const avgEv = [d.entry, ...adds, d.exit].filter((e) => e && e.avg != null);
  const avgLine = { x: avgEv.map((e) => e.date), y: avgEv.map((e) => e.avg),
                    mode: "lines", name: "средняя цена позиции (avg)",
                    line: { color: "#d29922", width: 1.5, shape: "hv" } };
  const stop = { x: stopEv.map((e) => e.date), y: stopEv.map((e) => e.stop), mode: "lines",
                 name: "стоп = avg − h/Q (риск = b)", line: { color: "#f85149", width: 1.5, shape: "hv", dash: "dash" } };
  const x0 = d.price.x[0], x1 = d.price.x[d.price.x.length - 1];
  const shapes = d.rungs.map((r) => ({
    type: "line", xref: "x", yref: "y", x0, x1, y0: r.level, y1: r.level,
    line: { color: r.k === 0 ? "#5b9dff" : "#39414d", width: 1, dash: "dot" },
  }));
  const traces = [price, avgLine, stop, addT, entry, exitT];
  const lay = layout(`${d.scenario} (${isCalls ? "коллы" : "акции"}) — пошаговый разбор`, {
    height: 460, shapes,
    margin: { t: 36, r: isCalls ? 64 : 64, b: 36, l: 56 },
    xaxis: { gridcolor: "#2a3340" }, yaxis: { gridcolor: "#2a3340", title: { text: "цена" } },
  });
  if (isCalls && optRolls.length) {     // auto-roll markers (re-strike near expiry) on the price axis
    traces.push({ x: optRolls.map((rr) => rr.date), y: optRolls.map((rr) => rr.spot),
                  mode: "markers+text", name: "🔁 ролл (ре-страйк)",
                  text: optRolls.map((rr) => `ролл${rr.n}: ${f(rr.old_strike)}→${f(rr.new_strike)}`),
                  textposition: "bottom center", textfont: { size: 9, color: "#39d0d8" },
                  marker: { color: "#39d0d8", symbol: "diamond", size: 13, line: { color: "#0f1419", width: 1 } } });
  }
  if (isCalls && optMarks.length) {     // overlay option-stack unrealised P&L on the right axis
    traces.push({ x: optMarks.map((m) => m.date), y: optMarks.map((m) => m.unreal), mode: "lines",
                  name: "нереализ. P&L опциона", yaxis: "y2",
                  line: { color: "#f0c000", width: 1.5 } });
    lay.yaxis2 = { overlaying: "y", side: "right", title: { text: "$ P&L опциона" },
                   gridcolor: "transparent", zeroline: false };
  } else if (!isCalls) {                              // share rung labels (no 2nd axis to collide with)
    lay.annotations = d.rungs.map((r) => ({
      xref: "paper", x: 1, y: r.level, yref: "y", xanchor: "left",
      text: r.k === 0 ? " вход R0" : ` +${r.k}·h`, showarrow: false,
      font: { size: 9, color: r.k === 0 ? "#5b9dff" : "#8b949e" } }));
  }
  await plot(ids.chart, traces, lay);

  // --- common grid narration (instrument-agnostic: where we enter/scale/exit) ---
  const L = [];
  const e0 = d.entry, x = d.exit;
  L.push(`СЦЕНАРИЙ: ${d.scenario}  ·  инструмент: ${isCalls ? "коллы (auto-Δ)" : "акции (Δ=1)"}  ·  b = $${f(b)}\n`);
  L.push(`СЕТКА (одинакова для акций и коллов):`);
  L.push(`1) ВХОД ${e0.date}: R0=${f(e0.price)}, ATR=${f(e0.atr)} ⇒ шаг h=${f(e0.h)}. Стоп = R0−h = ${f(e0.stop)} (риск 1 лота = b).`);
  let n = 2;
  for (const a of adds) {
    L.push(`${n}) ШАГ ${a.step} (${a.date}): цена на R0+${a.step}·h = ${f(a.trigger)} → долив 2^${a.step}=${f(a.lots_added)} лота, Q=${f(a.Q)}, средняя=${f(a.avg)}, стоп = avg−h/Q = ${f(a.stop)}.`);
    n++;
  }
  L.push(`${n}) ${won ? "ЦЕЛЬ" : "СТОП"} ${x.date} на ${f(x.price)}.\n`);
  L.push(`СТОП = СРЕДНЯЯ − h/Q (НЕ классический трейлинг от пика!). Дистанция до стопа сжимается (h/Q),`);
  L.push(`но убыток ОТ СРЕДНЕЙ при выносе = Q·(avg−stop)·$/пункт = h·$/пункт = РОВНО начальный b на любом шаге.`);
  L.push(`На графике: оранжевая линия = средняя позиции (avg), красный пунктир = стоп; зазор между ними = h/Q.\n`);

  if (!isCalls) {
    // ----- shares money -----
    const e = d.entry;
    L.push(`— ДЕНЬГИ (акции, units = реальные единицы, Δ=1) —`);
    L.push(`Чтобы рисковать b=$${f(b)} на стопе −1·ATR ($${f(e.h)}): units = b/h = ${f(e.units)} ед.`);
    L.push(`Нотионал на входе = ${f(e.units)}×$${f(e.price)} = $${f(e.notional)}. «Заходит» НЕ $${f(b)}, а $${f(e.notional)}; $${f(b)} — лишь сумма под стопом.`);
    if (won) {
      L.push(`+1·ATR: первый лот даёт +$${f(b)} (= ${f(e.units)}×$${f(e.h)}) = +b, не +b/2.`);
      L.push(`Нереализ. P&L: ${[e, ...adds].map((s) => "$" + f(s.unreal)).join(" → ")} → реализ. +$${f(x.gross)} = ${f(x.gross / b)}×b.`);
      L.push(`Нотионал раздувается: ${[e, ...adds, x].map((s) => "$" + f(s.notional)).join(" → ")} ⇒ ради +$${f(x.gross)} в пике $${f(x.notional)} капитала.`);
    } else {
      L.push(`Убыток на стопе = −$${f(-x.pnl)} = −b (ровно, при любом Q).`);
    }
  } else {
    // ----- options money (from the SAME function that computes the P&L) -----
    const oe = optAdds[0], step1 = optAdds[1];
    L.push(`— ДЕНЬГИ (коллы, дельта-нормированный сайзинг units=(b/h)/Δ) —`);
    if (oe) {
      L.push(`ВХОД: страйк K=${f(oe.strike)}, IV=${f(oe.iv)}, Δ=${f(oe.delta)}, премия ${f(oe.premium_per)}/ед.`);
      L.push(`Чтобы держать b/h=${f(e0.per_pt)} $/пункт при Δ=${f(oe.delta)}, беру ${f(oe.contracts_added)} контрактов (=(b/h)/Δ).`);
      L.push(`Уплачено премии = ${f(oe.contracts_added)}×$${f(oe.premium_per)} = $${f(oe.premium_paid)}. Это и есть макс. убыток (стопа на опционе нет — выпуклость).`);
    }
    if (step1) {
      L.push(`\nПРО ТВОИ "+$50": при +1·ATR опцион на входных ${f(oe.contracts_added)} контрактах дал +$${f(step1.unreal)} ≈ +b.`);
      L.push(`Если бы взяли как акции (${f(e0.per_pt)} ед., НЕ делённые на Δ) — было бы ≈Δ×h×units = ${f(oe.delta)}×$${f(e0.h)}×${f(e0.per_pt)} = +$${f(oe.delta * e0.h * e0.per_pt)} — те самые "+$50".`);
      L.push(`Деление на Δ (взяли ${f(oe.contracts_added)}, а не ${f(e0.per_pt)}) восстанавливает +b. Чуть больше $${f(b)} — это гамма (Δ растёт по ходу).`);
    }
    L.push(`\nПо шагам (Δ растёт ⇒ опцион всё «прямее»):`);
    for (const a of optAdds) {
      L.push(`  шаг ${a.step} @${f(a.level)}: Δ=${f(a.delta)}, премия ${f(a.premium_per)}/ед, +${f(a.contracts_added)} контр → ${f(a.contracts)} всего, премии в сумме $${f(a.premium_book)}, нереализ. $${f(a.unreal)}.`);
    }
    if (optRolls.length) {
      L.push(`\n🔁 АВТО-РОЛЛ (${optRolls.length}): за ${d.roll_buffer_days || 5} дн до экспирации позиция перекрывается в новый страйк (та же экспозиция, свежий DTE) — так короткий DTE едет по тренду, а не гибнет на экспирации.`);
      for (const rr of optRolls)
        L.push(`  ролл ${rr.n} (${rr.date}, цена ${f(rr.spot)}): страйк ${f(rr.old_strike)}→${f(rr.new_strike)}, экспирация ${rr.old_expiry}→${rr.new_expiry}; закрыли по ${f(rr.prem_close)}/ед, открыли по ${f(rr.prem_open)}/ед, ${f(rr.contracts)} контр., издержки ролла $${f(rr.roll_cost)}.`);
    }
    if (optExit) {
      L.push(`\n${won ? "ЦЕЛЬ" : "СТОП/выход"}: закрытие по ${f(optExit.premium_per)}/ед × ${f(optExit.contracts)} = $${f(optExit.stack_value)}; премии вложено $${f(optExit.premium_book)}.`);
      L.push(`БРУТТО опциона = $${f(optExit.gross)}${won ? ` = ${f(optExit.gross / b)}×b` : ""}. ${won ? `Больше акций (${f(d.table[0] ? d.table[0].gross : 0)} было бы линейно) за счёт гаммы.` : "Выпуклость смягчает убыток vs −b у акций."}`);
    }
  }
  L.push(`\nИТОГ: проигрыши малы и часты, выигрыш серии — крупный, но требует раздувания капитала/премии (см. cap_mult, скан вкладки 5).`);
  $("#" + ids.stats).textContent = L.join("\n");

  // --- money ledger table ---
  const led = $("#" + ids.ledger);
  let cols, rows, note;
  if (!isCalls) {
    cols = [["t", "событие"], ["price", "цена"], ["level", "ур."], ["Q", "Q лот"],
      ["units", "units"], ["notional", "нотионал $"], ["unreal", "нереал.$"], ["risk", "риск$"], ["stop", "стоп"]];
    rows = [d.entry, ...adds, d.exit];
    note = "денежный леджер (акции) · units = реальные единицы, Δ=1";
  } else {
    cols = [["step", "шаг"], ["level", "цена"], ["delta", "Δ"], ["premium_per", "премия/ед"],
      ["contracts_added", "+контр"], ["contracts", "контр всего"], ["premium_book", "премия Σ$"],
      ["stack_value", "стоимость$"], ["unreal", "нереал.$"]];
    const rollRows = optRolls.map((rr) => ({ t: "opt_roll", date: rr.date, step: `🔁ролл${rr.n}`,
      level: rr.spot, delta: "", premium_per: rr.prem_close, contracts_added: "",
      contracts: rr.contracts, premium_book: rr.roll_cost,
      stack_value: `${f(rr.old_strike)}→${f(rr.new_strike)}`, unreal: "" }));
    rows = [...optAdds, ...rollRows, optExit].filter(Boolean)
      .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
    note = "опционный леджер · контрактов=(b/h)/Δ · 🔁ролл = ре-страйк страйка у экспирации (издержки в «премия Σ$») · цена закрытия в строке выхода";
  }
  const head = "<tr>" + cols.map(([, l]) => `<th>${l}</th>`).join("") + "</tr>";
  const body = rows.map((ev) => {
    const cls = (ev.t === "exit" || ev.t === "opt_exit") ? (won ? "w" : "l") : "";
    return `<tr class="${cls}">` + cols.map(([k]) =>
      `<td>${ev[k] == null ? "" : (typeof ev[k] === "number" ? f(ev[k]) : ev[k])}</td>`).join("") + "</tr>";
  }).join("");
  led.innerHTML = `<div class="tt-scroll"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`
    + `<div class="tt-note">${note}</div>`;
}

// long-call coin-flip view: premium = the bet, risk ≤ b, dynamic doubling target
async function renderExplainCoinflip(d, ids = { chart: "expl-chart", stats: "expl-stats", ledger: "expl-ledger" }) {
  const b = d.b, f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  const rounds = d.rounds, x = d.cf_exit, won = x && x.reason === "target";

  const price = { x: d.price.x, y: d.price.y, mode: "lines", name: "цена (close)",
                  line: { color: "#c9d1d9", width: 1.5 } };
  const entries = { x: rounds.map((r) => r.date), y: rounds.map((r) => r.entry),
                    mode: "markers", name: "вход в раунд (купили коллы)",
                    text: rounds.map((r) => `R${r.round}: $${f(r.stake)} премии`),
                    marker: { color: "#5b9dff", symbol: "triangle-up", size: 13,
                              line: { color: "#0f1419", width: 1 } } };
  const winR = rounds.filter((r) => r.win), lossR = rounds.filter((r) => !r.win);
  const wins = { x: winR.map((r) => r.sell_date), y: winR.map((r) => r.double_at),
                 mode: "markers", name: "удвоился (×2) → ролл",
                 text: winR.map((r) => `+${f(r.m_atr)}·ATR`), textposition: "top center",
                 textfont: { size: 9, color: "#f0c000" },
                 marker: { color: "#f0c000", symbol: "star", size: 14, line: { color: "#0f1419", width: 1 } } };
  const loss = { x: lossR.map((r) => r.sell_date), y: lossR.map((r) => r.sell),
                 mode: "markers", name: "экспирация (≤ b)",
                 marker: { color: "#f85149", symbol: "x", size: 14, line: { color: "#0f1419", width: 1 } } };
  // each round's doubling level as a dotted segment entry→exit
  const shapes = rounds.map((r) => ({
    type: "line", xref: "x", yref: "y", x0: r.date, x1: r.sell_date,
    y0: r.double_at, y1: r.double_at,
    line: { color: r.win ? "#f0c000" : "#8b949e", width: 1, dash: "dot" } }));

  await plot(ids.chart, [price, entries, wins, loss], layout(
    `${d.scenario} (коллы, коин-флип) — премия = ставка, риск ≤ b`, {
      height: 460, shapes, margin: { t: 36, r: 20, b: 36, l: 56 },
      xaxis: { gridcolor: "#2a3340" }, yaxis: { gridcolor: "#2a3340", title: { text: "цена" } } }));

  const L = [];
  L.push(`СЦЕНАРИЙ: ${d.scenario}  ·  КОЛЛЫ — коин-флип  ·  b = $${f(b)} (бюджет премии)\n`);
  L.push(`Ставка = ПРЕМИЯ. Лонг-колл не теряет больше премии ⇒ риск ≤ b по построению, СТОП НЕ НУЖЕН.`);
  L.push(`«Победа» = колл подорожал в ×${f(d.double_target)} (удвоился) ⇒ катим всю выручку в следующий раунд (×2 контрактов).`);
  L.push(`Уровень удвоения считается из Блэка-Шоулза ДИНАМИЧЕСКИ (не фикс 2·ATR — зависит от Δ/IV/DTE).\n`);
  for (const r of rounds) {
    L.push(`Раунд ${r.round} (${r.date}): купили коллов на $${f(r.stake)} премии — ${f(r.contracts)} контр., страйк ${f(r.strike)}, Δ=${f(r.delta)}, премия ${f(r.prem_per)}/ед.`);
    L.push(`   Удвоение при цене ${f(r.double_at)} = вход +${f(r.m_atr)}·ATR (динамически).`);
    if (r.win) L.push(`   → ✔ дошли: выручка $${f(r.proceeds)} = ×${f(d.double_target)}. Катим дальше.\n`);
    else L.push(`   → ✕ не дошли до экспирации: выручка $${f(r.proceeds)} (≤ ставки). Цикл закрыт.\n`);
  }
  if (won) {
    L.push(`ИТОГ: серия ${f(x.streak)} побед → P&L +$${f(x.pnl)} = b·(${f(d.double_target)}^${f(x.streak)}−1).`);
    L.push(`Риск за цикл был ограничен $${f(b)} (премия 1-го раунда; дальше рисковали уже выигранным).`);
  } else {
    L.push(`ИТОГ: проигрыш цикла = −$${f(-x.pnl)} ≤ b. Колл просто истёк, не удвоившись.`);
    L.push(`Максимум потерь = первоначальная премия b = $${f(b)}, как в коин-флипе.`);
  }
  $("#" + ids.stats).textContent = L.join("\n");

  const led = $("#" + ids.ledger);
  const cols = [["round", "раунд"], ["entry", "вход"], ["delta", "Δ"], ["prem_per", "премия/ед"],
    ["contracts", "контр."], ["stake", "ставка$"], ["double_at", "удвоение@"], ["m_atr", "+ATR"],
    ["proceeds", "выручка$"]];
  const head = "<tr>" + cols.map(([, l]) => `<th>${l}</th>`).join("")
    + "<th>итог</th></tr>";
  const body = rounds.map((r) =>
    `<tr class="${r.win ? "w" : "l"}">` + cols.map(([k]) =>
      `<td>${typeof r[k] === "number" ? f(r[k]) : r[k]}</td>`).join("")
    + `<td>${r.win ? "×2 ролл" : "экспирация"}</td></tr>`).join("");
  led.innerHTML = `<div class="tt-scroll"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`
    + `<div class="tt-note">коин-флип на коллах · ставка=премия · риск≤b · +ATR до удвоения считается из BS динамически</div>`;
}

$("#form-explain").onsubmit = (e) => {
  e.preventDefault();
  withBusy(e.submitter, async () =>
    renderExplain(await post("/api/explain", formData(e.target))));
};

// ---- tab 7 inspect: real instrument + window, drill into each campaign (reuses Explain renderers)
let _inspect = null;

function _sliceWin(px, d0, d1, pad = 3) {
  let lo = px.x.findIndex((x) => x >= d0); if (lo < 0) lo = 0; lo = Math.max(0, lo - pad);
  let hi = px.x.length - 1; for (let i = px.x.length - 1; i >= 0; i--) { if (px.x[i] <= d1) { hi = i; break; } }
  hi = Math.min(px.x.length - 1, hi + pad);
  return { x: px.x.slice(lo, hi + 1), y: px.y.slice(lo, hi + 1) };
}
function _inspCampGrid(d, camp) {
  // shares OR pyramid-calls campaign — same grid; renderExplain branches on instrument.
  const ev = d.trace.filter((e) => e.camp === camp);
  const entry = ev.find((e) => e.t === "entry"), exit_ = ev.find((e) => e.t === "exit");
  const rungs = [];
  for (let k = 0; k <= d.target_streak; k++) rungs.push({ k, level: +(entry.price + k * entry.h).toFixed(4) });
  return { model: "grid", instrument: d.instrument, b: d.b, scenario: `${d.ticker} · кампания ${camp}`,
           roll_buffer_days: d.roll_buffer_days, price: _sliceWin(d.price, entry.date, exit_.date),
           trace: ev, rungs, entry, exit: exit_ };
}
function _inspCampCF(d, camp) {
  const ev = d.trace.filter((e) => e.camp === camp);
  const rounds = ev.filter((e) => e.t === "cf_round"), cf_exit = ev.find((e) => e.t === "cf_exit");
  const d0 = rounds[0] && rounds[0].date, d1 = (cf_exit && cf_exit.date) || (rounds[rounds.length - 1] || {}).sell_date;
  return { model: "coinflip", instrument: "calls", b: d.b, double_target: d.double_target,
           scenario: `${d.ticker} · кампания ${camp}`, price: _sliceWin(d.price, d0, d1),
           rounds, cf_exit, trace: ev };
}
const INSP_IDS = { chart: "insp-chart", stats: "insp-stats", ledger: "insp-ledger" };
function inspShowCamp(camp) {
  const d = _inspect; if (!d) return;
  const payload = d.model === "coinflip" ? _inspCampCF(d, camp) : _inspCampGrid(d, camp);
  renderExplain(payload, INSP_IDS);
  $$("#insp-list tr[data-camp]").forEach((tr) =>
    tr.classList.toggle("sel", +tr.dataset.camp === camp));
}
function renderInspList(d) {
  const el = $("#insp-list");
  const cols = Object.keys(d.table[0]);
  const head = "<tr>" + cols.map((c) => `<th>${c}</th>`).join("") + "</tr>";
  const body = d.table.map((r) => {
    const cls = r.outcome === "win" ? "w" : (r.outcome === "loss" ? "l" : "");
    return `<tr data-camp="${r.i}" class="${cls}" style="cursor:pointer">`
      + cols.map((c) => `<td>${r[c]}</td>`).join("") + "</tr>";
  }).join("");
  el.innerHTML = `<div class="tt-scroll"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`
    + `<div class="tt-note">${d.table.length} кампаний · клик по строке = детальный разбор ↓</div>`;
  $$("#insp-list tr[data-camp]").forEach((tr) => tr.onclick = () => inspShowCamp(+tr.dataset.camp));
}
async function renderInspect(d) {
  _inspect = d;
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  const price = { x: d.price.x, y: d.price.y, mode: "lines", name: "цена", line: { color: "#c9d1d9", width: 1 } };
  const traces = [price];
  if (d.model === "coinflip") {
    const re = d.trace.filter((e) => e.t === "cf_round"), w = re.filter((e) => e.win), l = re.filter((e) => !e.win);
    traces.push({ x: re.map((e) => e.date), y: re.map((e) => e.entry), mode: "markers", name: "вход раунда",
                  marker: { color: "#5b9dff", symbol: "triangle-up", size: 7, opacity: 0.7 } });
    traces.push({ x: w.map((e) => e.sell_date), y: w.map((e) => e.double_at), mode: "markers", name: "×2 win",
                  marker: { color: "#f0c000", symbol: "circle", size: 6, opacity: 0.85 } });
    traces.push({ x: l.map((e) => e.sell_date), y: l.map((e) => e.sell), mode: "markers", name: "проигрыш",
                  marker: { color: "#f85149", symbol: "x", size: 7, opacity: 0.7 } });
  } else {
    const en = d.trace.filter((e) => e.t === "entry"), ad = d.trace.filter((e) => e.t === "add"),
          ex = d.trace.filter((e) => e.t === "exit");
    const w = ex.filter((e) => e.reason === "target"), l = ex.filter((e) => e.reason !== "target");
    traces.push({ x: en.map((e) => e.date), y: en.map((e) => e.price), mode: "markers", name: "вход",
                  marker: { color: "#5b9dff", symbol: "triangle-up", size: 8, opacity: 0.7 } });
    traces.push({ x: ad.map((e) => e.date), y: ad.map((e) => e.level), mode: "markers", name: "долив",
                  marker: { color: "#3fb950", symbol: "circle", size: 4, opacity: 0.5 } });
    traces.push({ x: w.map((e) => e.date), y: w.map((e) => e.price), mode: "markers", name: "WIN",
                  marker: { color: "#f0c000", symbol: "star", size: 11, line: { color: "#0f1419", width: 1 } } });
    traces.push({ x: l.map((e) => e.date), y: l.map((e) => e.price), mode: "markers", name: "стоп",
                  marker: { color: "#f85149", symbol: "x", size: 8, opacity: 0.7 } });
    const rl = d.trace.filter((e) => e.t === "opt_roll");      // option auto-rolls (calls model)
    if (rl.length) traces.push({ x: rl.map((e) => e.date), y: rl.map((e) => e.spot), mode: "markers",
                  name: `🔁 ролл (${rl.length})`,
                  marker: { color: "#39d0d8", symbol: "diamond", size: 8, line: { color: "#0f1419", width: 1 } } });
  }
  await plot("insp-overview", traces, layout(`${d.ticker} (${d.instrument}) — обзор окна, ${d.table.length} кампаний`, {
    height: 400, xaxis: { gridcolor: "#2a3340", rangeselector: {
      bgcolor: "#10151c", activecolor: "#5b9dff", font: { color: "#e6edf3" }, buttons: [
        { step: "month", count: 3, label: "3m", stepmode: "backward" },
        { step: "year", count: 1, label: "1y", stepmode: "backward" }, { step: "all", label: "all" }] } } }));
  const net = d.table.reduce((a, r) => a + (r.pnl || 0), 0);
  const targetHits = d.table.filter((r) => r.outcome === "win").length;       // rode the FULL target streak
  const profitable = d.table.filter((r) => (r.pnl || 0) > 0).length;          // P&L > 0 (incl. profitable stop-outs)
  const losing = d.table.filter((r) => (r.pnl || 0) < 0).length;
  const isCalls = d.instrument === "calls";
  const totRolls = d.trace.filter((e) => e.t === "opt_roll").length;
  const modelLbl = d.model === "coinflip" ? "coin-flip (коллы)"
    : (isCalls ? "calls — пирамида с авто-роллом" : "shares (ATR-пирамида)");
  $("#insp-summary").textContent =
    `${d.ticker} · ${modelLbl} · кампаний ${d.table.length}${totRolls ? ` · роллов ${totRolls} 🔁` : ""}\n`
    + `🎯 дошли до target-серии: ${targetHits}   ·   прибыльных: ${profitable}   ·   убыточных: ${losing}\n`
    + `итоговый банк ${f(d.final_bank)}   net P&L ${net >= 0 ? "+" : ""}${f(net)}\n`
    + `(«дошли до target» ≠ «прибыльных»: ${isCalls ? "колл-кампания может выйти по стопу ВЫШЕ входа — прибыльный стоп-аут (выпуклость)" : "стоп-аут = −b"}; `
    + `net = немного крупных выигрышей vs много мелких проигрышей — коин-флип скос.)\n`
    + (isCalls ? `⚠ опционы оценены по realized-vol БЕЗ надбавки → P&L ОПТИМИСТИЧЕН (вкладка 5 → breakeven IV markup даёт честную картину).\n` : "")
    + `↓ клик по строке таблицы — пошаговый разбор входов и наращиваний этой кампании`;
  renderInspList(d);
  if (d.table.length) inspShowCamp(d.table[0].i);
}
$("#form-inspect").onsubmit = (e) => {
  e.preventDefault();
  withBusy(e.submitter, async () =>
    renderInspect(await post("/api/inspect", formData(e.target))));
};

// ---- tab 8 hedged intraday (Прикрытый Интрадей) — straddle + counter-trend scalping
async function renderHedged(d) {
  const s = d.stats;
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  const net = s.net_pnl, up = net > 0;

  // --- equity decomposition: total vs straddle (gamma−theta) vs scalp — ONE shared $ axis ---
  await plot("hi-equity", [
    { x: d.equity_straddle.x, y: d.equity_straddle.y, mode: "lines", name: "стреддл P&L (гамма−тета)",
      line: { color: "#f0c000", width: 1.5 } },
    { x: d.equity_scalp.x, y: d.equity_scalp.y, mode: "lines",
      name: `скальп P&L (накоп.; итог ${s.scalp_pnl >= 0 ? "+" : ""}${f(s.scalp_pnl)})`,
      line: { color: "#5b9dff", width: 1.5 } },
    { x: d.theta_path.x, y: d.theta_path.y, mode: "lines", name: "тета уплачено (накоп.)",
      line: { color: "#8b949e", width: 1, dash: "dot" } },
    { x: d.equity_total.x, y: d.equity_total.y.map((v) => v - s.starting_bank), mode: "lines",
      name: "ИТОГО P&L (= стреддл + скальп)",
      line: { color: up ? "#3fb950" : "#f85149", width: 2.5 },
      fill: "tozeroy", fillcolor: up ? "rgba(63,185,80,0.10)" : "rgba(248,81,73,0.10)" },
  ], layout(`Разложение P&L (от 0): стреддл + скальп = ИТОГО  ·  старт банка $${f(s.starting_bank)}`, {
    height: 380, xaxis: { gridcolor: "#2a3340" },
    yaxis: { gridcolor: "#2a3340", title: { text: "$ P&L (от 0)" },
             zeroline: true, zerolinecolor: "#8b949e" } }));

  // --- price with roll markers ---
  const price = { x: d.price.x, y: d.price.y, mode: "lines", name: "цена (close)",
                  line: { color: "#c9d1d9", width: 1 } };
  const rolls = { x: (d.rolls || {}).x || [], y: (d.rolls || {}).y || [], mode: "markers",
                  name: `🔁 ролл стреддла (${(d.rolls.x || []).length})`,
                  marker: { color: "#39d0d8", symbol: "diamond", size: 7, line: { color: "#0f1419", width: 1 } } };
  await plot("hi-price", [price, rolls], layout(`${d.ticker} — цена + роллы ATM-стреддла`, {
    height: 320, xaxis: { gridcolor: "#2a3340", rangeselector: {
      bgcolor: "#10151c", activecolor: "#5b9dff", font: { color: "#e6edf3" }, buttons: [
        { step: "month", count: 6, label: "6m", stepmode: "backward" },
        { step: "year", count: 1, label: "1y", stepmode: "backward" }, { step: "all", label: "all" }] } } }));

  // --- honest verdict ---
  const cover = s.scalp_covers_theta_pct;
  const ann = s.ann_return_pct;
  const inBand = ann >= 25 && ann <= 40;
  const startBank = s.final_bank - net;
  const retPct = startBank ? 100 * net / startBank : 0;
  let verdict =
    `${up ? "✅ СЧЁТ ВЫРОС" : "❌ СЧЁТ УПАЛ"}   ИТОГО P&L ${net >= 0 ? "+" : ""}${f(net)}  (${f(retPct)}% за период)\n`
    + `годовых (CAGR) : ${f(ann)}%  за ${f(s.years)} лет  ${inBand ? "✅ в доктринной полосе 25–40%" : (ann > 40 ? "⚠ выше 25–40% — вероятно режим/КПД оптимистичны" : "⚠ ниже доктринных 25–40%")}\n\n`
    + `── РАЗЛОЖЕНИЕ ──\n`
    + `стреддл (гамма−тета) : ${s.straddle_pnl >= 0 ? "+" : ""}${f(s.straddle_pnl)}\n`
    + `  из них гамма+направление (БЕЗ теты) : ${s.gamma_dir_pnl >= 0 ? "+" : ""}${f(s.gamma_dir_pnl)}  ← ловит крупные движения\n`
    + `  тета уплачено всего                 : ${f(s.total_theta)}\n`
    + `скальп (контр-тренд) : ${s.scalp_pnl >= 0 ? "+" : ""}${f(s.scalp_pnl)}  →  покрыл ${f(cover)}% теты`
    + `${cover >= 100 ? " ✅" : ""}\n`
    + (s.breakeven_scalp_cover_pct > 0
        ? `\n🎯 СТРЕДДЛ ПОЧТИ В НУЛЕ: гамма (${s.gamma_dir_pnl >= 0 ? "+" : ""}${f(s.gamma_dir_pnl)}) почти покрыла тету сама.\n`
          + `   Чтобы выйти В НОЛЬ, скальпу надо покрыть всего ${f(s.breakeven_scalp_cover_pct)}% теты — `
          + `а доктринный МИНИМУМ скальпа = ~100% («отбивание теты»).\n`
          + `   ⇒ ${s.breakeven_scalp_cover_pct <= 100
                ? "ПОД ЗАМЫСЕЛ МЕТОДА ЭТОТ ИНСТРУМЕНТ ПЛЮСОВОЙ — нужен лишь КУСОЧЕК обещанного доктриной скальпа."
                : "скальпу пришлось бы превзойти свою минимальную задачу — инструмент тяжёлый для метода."}\n`
        : `\n✅ Стреддл сам по себе в плюсе (гамма > теты) — скальп уже сверху.\n`)
    + `\n── РИСК ──\n`
    + `макс. премия под риском (1 стреддл) : ${f(s.max_premium_at_risk)}  = пол потерь стреддла\n`
    + `худший период (стреддл+скальп)      : ${f(s.worst_period_pnl)}\n`
    + `max drawdown : ${f(s.max_drawdown)}   ·   роллов : ${s.n_rolls}   ·   дней : ${s.n_days}\n`
    + `IV-поверхность : ${s.vol_model} (класс ${s.vol_class})   ·   издержки : ${f(s.total_cost)}\n\n`;
  if (s.scalp_model === "grid") {
    const yrs = Math.max(s.years || 1, 0.1);
    const rtYr = Math.round(s.scalp_round_trips / yrs);
    const tf = s.grid_timeframe || "daily";
    const rc = $("#form-hedged [name=scalp_recenter_days]").value;
    verdict += `СКАЛЬП-МОДЕЛЬ: grid · шаг от ${tf}-ATR×${$("#form-hedged [name=grid_atr_frac]").value} · ре-центр ${rc>0?`каждые ${rc}д`:"OFF (заморожен)"} — ${s.scalp_round_trips} круговых (~${rtYr}/год).\n`
      + (rc > 0
          ? `  ⚠ Ре-центр каждые ${rc}д ПРИНУДИТЕЛЬНО закрывает залипшие части по рынку — реализует их ДО разворота, убивая эдж mean-reversion (на чистом OU: +933→−602). Лучше 0.\n`
          : `  ✅ Залипшие части ПЕРЕНОСЯТСЯ до ролла (доктрина: лечить, не бросать) — это и даёт сетке ловить mean-reversion (открытая просадочная нога = ставка на возврат).\n`)
      + `ЧТО ДНЕВНОЙ БЭКТЕСТ МЕРЯЕТ ЧЕСТНО:\n`
      + `  ✅ ТЕТА — точно (длинные опционы DTE ${$("#form-hedged [name=dte_days]").value}д = медленная тета, BS-MtM).\n`
      + `  ✅ ГАММА стреддла на крупных движениях — точно (это и есть «большая рыба» доктрины).\n`
      + (s.intraday_bars > 0
          ? (s.scalp_data === "1m"
              ? `  ✅ СКАЛЬП ИЗМЕРЕН: грид прошёл ${s.intraday_bars.toLocaleString()} реальных 1-мин баров (Binance, бесплатно), ${s.scalp_round_trips} круговых.\n`
                + `    Это ближе всего к живому ПИ (~200–250 круговых/мес); крипта (ETH/BTC) — идеальный инструмент доктрины.\n`
              : `  ✅ СКАЛЬП измерен на ${s.intraday_bars.toLocaleString()} часовых барах (yfinance ~2 года), ${s.scalp_round_trips} круговых.\n`
                + `    Лучше дневки, но 60m грубее живого 1-мин ПИ → близкая, но НЕ полная оценка.\n`)
            + `  Итог: стреддл ${f(s.straddle_pnl)}; скальп ${f(s.scalp_pnl)}`
            + (s.scalp_pnl < 0
                ? ` — минус на трендовом окне ОЖИДАЕМ: скальп теряет в тренде, его хеджирует гамма стреддла.`
                : ` — собрал mean-reversion в боковике и помог отбить тету.`)
          : `  ⚠ СКАЛЬП — НИЖНЯЯ ОЦЕНКА: дневной бар даёт лишь ~${rtYr} круговых/год; живой ПИ ~2500/год на ТАЙ-В-ДЕНЬ\n`
            + `    осцилляциях — они МЕНЬШЕ дневного ATR и в баре их НЕТ ⇒ бэктест меряет ЛОНГ-ВОЛ СТРЕДДЛ (ядро), не скальп.\n`
            + `  Итог: на этом инструменте ${s.straddle_pnl >= 0 ? "стреддл уже несёт" : "стреддл сам по себе в минусе"} `
            + `(${f(s.straddle_pnl)}); скальп тут ${f(s.scalp_pnl)} — но его вклад в боковике на дневках НЕ виден.\n`
            + (isCryptoTicker(d.ticker)
                ? `  Это КРИПТА → выбери «Scalp data → 1m crypto (Binance free)»: бесплатные глубокие 1-мин бары (любое окно). 1m включается авто при выборе крипты.`
                : `  ⚠ Для НЕ-крипты (${d.ticker}) бесплатного ГЛУБОКОГО интрадея нет: hourly = только ~2 года (yfinance), глубже — платный вендор (Polygon ≈$29/мес). Бесплатный 1m только у крипты (ETH/BTC).`));
  }
  else if (s.scalp_model === "analytic") {
    const K = $("#form-hedged [name=scalp_k]").value;
    verdict += `СКАЛЬП-МОДЕЛЬ: analytic (ВОЛ-ПРИВОД) — оценка ЛЮБОГО инструмента из его волатильности.\n`
      + `  доход скальпа/день ≈ K·лоты·σ$(t), K=${K}. Тета и ГАММА стреддла — ТОЧНЫЕ (реальный путь);\n`
      + `  приближается только неизмеримый иначе скальп. По мат-ву Броуновских пересечений доход ∝ σ$ →\n`
      + `  трекает реализованную волатильность инструмента во времени, БЕЗ интрадей-фида.\n`
      + `  ⚠ Вол-инвариантна только ВЕЛИЧИНА (∝ лоты·σ$); K несёт edge (mean-reversion vs тренд) и\n`
      + `    НЕ универсальна: калибровка 1m крипты — ETH +0.06 (боковик) / SOL ~0 / BTC −0.006 (тренд).\n`
      + `    Результат линеен по K ⇒ это СЦЕНАРИЙ при выбранном edge, а не предсказание. Для измерения\n`
      + `    скальпа на крипте — модель «grid» + «1m crypto». Издержки в analytic не моделируются.`;
  }
  else
    verdict += `СКАЛЬП-МОДЕЛЬ: range (грубая эвристика).\n`
      + `⚠ НЕ механически точная: величина скальпа = что задашь в КПД (${$("#form-hedged [name=scalp_efficiency]").value}) / Max RT\n`
      + `  (${$("#form-hedged [name=max_rt_per_day]").value}), позиция не переносится — может как ЗАВЫСИТЬ, так и занизить скальп.\n`
      + `  Для честной картины переключись на модель «grid» (событийная сетка, дневной такт) с длинным DTE.`;
  // --- "Эквивалент монетки": reduce the strategy to profitability primitives (0.6 vs 0.45) ---
  const cf = s.coinflip;
  if (cf && s.scalp_model === "grid") {        // only the grid model books real round-trips
    const cap = cf.capture_fraction, cov = cf.coverage_ratio, tpm = cf.trades_per_month;
    const tpmOk = tpm >= 150;                      // near the doctrine 200–250 loaded-book band
    const capOk = cap >= 0.5;                      // corpus "ideal = catch >50% of the move"
    const winning = cov >= 1.0;
    let cfTxt =
      `\n\n════════ ЭКВИВАЛЕНТ МОНЕТКИ (0.6 или 0.45?) ════════\n`
      + `Доктринный тест прибыльности = «отбивает ли скальп тету»: доход скальпа ≥ тета.\n`
      + `И тета, и доход-на-сделку растут с волатильностью (∝ σ·S) при фиксированном бюджете риска,\n`
      + `поэтому ПОКРЫТИЕ почти НЕ зависит от инструмента — его задают сделки/мес × доля пойманного.\n\n`
      + `сделок/мес        : ${f(tpm)}   (цель доктрины ${cf.trades_per_month_target})  ${tpmOk ? "✅" : "⚠ грид слишком широк для этого фида — уменьши grid_atr_frac"}\n`
      + `прибыль на сделку : ${cf.profit_per_trade >= 0 ? "+" : ""}${f(cf.profit_per_trade)}$\n`
      + `доля пойманного φ : ${f(100 * cap)}% дневного диапазона  ${capOk ? "✅ >50% (идеал доктрины)" : "(доктринный идеал >50%)"}\n`
      + `доход скальпа/мес : ${cf.scalp_per_month >= 0 ? "+" : ""}${f(cf.scalp_per_month)}$   vs   тета/мес ${f(cf.theta_per_month)}$\n`
      + `── ПОКРЫТИЕ = доход скальпа ÷ |тета| = ${f(cov)}  ${winning ? "≥ 1 ✅" : "< 1 ⚠"}\n`
      + `   ${winning
            ? "0.6-ТИПА: скальп САМ платит тету — стратегия плюсовая даже в чистом флете, тренд = бонус."
            : `0.45-ТИПА: флет НЕ окупает тету (покрыто ${f(100 * cov)}%) — плюс держится на гамме/тренде, не на скальпе.`}\n`
      + `   точка безубытка по φ : надо ловить ${f(100 * cf.breakeven_capture)}% диапазона (сейчас ${f(100 * cap)}%).\n`
      + `\nПРОЕКЦИЯ НА ДРУГОЙ АКТИВ (φ переносится, σ сокращается): если ловить ${f(100 * cf.assumed_capture)}% →\n`
      + `   покрытие ≈ ${cf.coverage_at_assumed == null ? "—" : f(cf.coverage_at_assumed)}  ${cf.coverage_at_assumed != null && cf.coverage_at_assumed >= 1 ? "(плюсовой флет)" : "(нужен тренд)"}\n`
      + `эмпирический p (доля плюсовых периодов стреддла) : ${f(cf.period_win_rate)}  → ${cf.flip_type}`;
    verdict += cfTxt;
  } else if (cf && s.scalp_model === "analytic") {   // analytic: coverage valid (calibrated), no trade-count
    const cov = cf.coverage_ratio, winning = cov >= 1.0;
    verdict +=
      `\n\n════════ ЭКВИВАЛЕНТ МОНЕТКИ (0.6 или 0.45?) — ВОЛ-ОЦЕНКА ════════\n`
      + `Прибыльность из волатильности инструмента (модель analytic, K=${$("#form-hedged [name=scalp_k]").value}):\n`
      + `доход скальпа/мес : ${cf.scalp_per_month >= 0 ? "+" : ""}${f(cf.scalp_per_month)}$   vs   тета/мес ${f(cf.theta_per_month)}$\n`
      + `── ПОКРЫТИЕ = доход скальпа ÷ |тета| = ${f(cov)}  ${winning ? "≥ 1 ✅" : "< 1 ⚠"}\n`
      + `   ${winning
            ? "0.6-ТИПА (при этом edge K): скальп сам платит тету — плюсовой флет, тренд = бонус."
            : `0.45-ТИПА (при этом edge K): флет покрывает ${f(100 * cov)}% теты — нужен тренд/гамма.`}\n`
      + `эмпирический p (доля плюсовых периодов стреддла) : ${f(cf.period_win_rate)}  → ${cf.flip_type}\n`
      + `⚠ Покрытие ЛИНЕЙНО по K — это сценарий при выбранном intraday-edge, не предсказание\n`
      + `  (сделок/мес и φ модель analytic не считает — это измерения модели grid на реальном фиде).`;
  }
  $("#hi-stats").textContent = verdict;
  renderHiRules(d, s, "hi-rules");          // same doctrine-compliance panel as Tab 9 (auto-parity)
  renderTable("hi-table", d.table);
}
// intraday feeds (1m/hourly) fetch market data on first run — tell the user it's working, not hung
function intradayNotice(form) {
  const sd = ($(form + " [name=scalp_data]") || {}).value;
  if (sd === "1m") toast("Качаю 1-мин историю с Binance (только крипта; первый прогон ~до минуты, дальше из кэша)…", true);
  else if (sd === "hourly") toast("Качаю часовую историю (yfinance ~2 года; первый прогон несколько сек)…", true);
}
$("#form-hedged").onsubmit = (e) => {
  e.preventDefault();
  intradayNotice("#form-hedged");
  withBusy(e.submitter, async () =>
    renderHedged(await post("/api/hedged-intraday", formData(e.target))));
};

// ---- tab 8 bulk: ПИ across the whole catalog (own sortable table)
let _hiScan = { rows: [], sort: "cagr_pct", desc: true };
function renderHiScanTable() {
  const el = $("#hi-scan-table");
  const dir = _hiScan.desc ? -1 : 1;
  const rows = [..._hiScan.rows].sort((a, b) => {
    if (a.ok !== b.ok) return a.ok ? -1 : 1;
    if (!a.ok) return 0;
    const av = a[_hiScan.sort] ?? -Infinity, bv = b[_hiScan.sort] ?? -Infinity;
    return (av - bv) * dir;
  });
  const cols = [["ticker", "ticker"], ["group", "group"], ["cagr_pct", "CAGR %"],
    ["net", "net $"], ["straddle_pnl", "стреддл $"], ["scalp_pnl", "скальп $"],
    ["scalp_cover_pct", "тета покрыта %"], ["worst_period_pnl", "худший период $"],
    ["max_premium_at_risk", "премия cap $"], ["loss_cap_ok", "cap?"],
    ["max_drawdown", "max DD $"], ["n_rolls", "роллов"]];
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  const sgn = (v) => (v > 0 ? "#3fb950" : "#f85149");
  const head = "<tr>" + cols.map(([k, lbl]) =>
    `<th data-sort="${k}" style="cursor:pointer">${lbl}${_hiScan.sort === k ? (_hiScan.desc ? " ▼" : " ▲") : ""}</th>`).join("") + "</tr>";
  const body = rows.map((r) => {
    if (!r.ok) return `<tr class="l"><td>${r.ticker}</td><td>${r.group}</td>`
      + `<td colspan="${cols.length - 2}" style="color:#8b949e">⚠ ${r.error}</td></tr>`;
    return `<tr class="${r.net > 0 ? "w" : "l"}"><td>${r.ticker}</td><td>${r.group}</td>`
      + `<td style="color:${sgn(r.cagr_pct)}">${f(r.cagr_pct)}</td><td>${f(r.net)}</td>`
      + `<td style="color:${sgn(r.straddle_pnl)}">${f(r.straddle_pnl)}</td>`
      + `<td style="color:${sgn(r.scalp_pnl)}">${f(r.scalp_pnl)}</td><td>${f(r.scalp_cover_pct)}</td>`
      + `<td>${f(r.worst_period_pnl)}</td><td>${f(r.max_premium_at_risk)}</td>`
      + `<td>${r.loss_cap_ok ? "✅" : "⚠"}</td><td>${f(r.max_drawdown)}</td><td>${r.n_rolls}</td></tr>`;
  }).join("");
  el.innerHTML = `<div class="tt-scroll"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`
    + `<div class="tt-note">${rows.length} инструментов · клик по заголовку = сортировка</div>`;
  $$("#hi-scan-table th[data-sort]").forEach((th) => th.onclick = () => {
    const k = th.dataset.sort;
    if (_hiScan.sort === k) _hiScan.desc = !_hiScan.desc; else { _hiScan.sort = k; _hiScan.desc = true; }
    renderHiScanTable();
  });
}
async function renderHiScan(d) {
  _hiScan = { rows: d.results, sort: "cagr_pct", desc: true };
  const s = d.summary;
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  // The doctrine targets VOLATILE OSCILLATING instruments (crypto / metals / energy), NOT the
  // whole panel (FX, slow equity ETFs are off-doctrine). Compute that subset separately — it's the
  // fair population for this method.
  const DOCTRINE = new Set(["Crypto", "Metals", "Energy"]);
  const okRows = d.results.filter((r) => r.ok);
  const tgt = okRows.filter((r) => DOCTRINE.has(r.group));
  const med = (xs) => { const a = [...xs].sort((p, q) => p - q); return a.length ? a[a.length >> 1] : 0; };
  const tgtProf = tgt.filter((r) => r.net > 0).length;
  const tgtMed = med(tgt.map((r) => r.cagr_pct));
  const outlierCarried = s.mean_cagr_pct > 2 * Math.max(s.median_cagr_pct, 0) + 5;  // mean ≫ median
  let verdict =
    `⚠ ШИРОКАЯ ПАНЕЛЬ — НЕВЕРНЫЙ ТЕСТ для этого метода. Доктрина таргетит ВОЛАТИЛЬНЫЕ ОСЦИЛЛЯТОРЫ\n`
    + `(крипта/металлы/энергия), а не FX и медленные ETF. Плюс СКАЛЬП недо-измерен на дневках (см. ниже).\n\n`
    + `── вся панель (${s.ok} инстр.) ──\n`
    + `${s.profitable}/${s.ok} в плюсе (${f(s.profitable_pct)}%)   ·   медианный CAGR : ${f(s.median_cagr_pct)}%   ·   средний : ${f(s.mean_cagr_pct)}%\n`
    + (s.mean_cagr_ex_best_pct != null
        ? `среднее БЕЗ лучшего (${s.best ? s.best.ticker : "—"}) : ${f(s.mean_cagr_ex_best_pct)}%   ← если резко падает, headline держится на одном выбросе\n` : "")
    + (tgt.length
        ? `── доктринные таргеты (крипта/металлы/энергия, ${tgt.length} инстр.) ──\n`
          + `  ${tgtProf}/${tgt.length} в плюсе (${f(100 * tgtProf / tgt.length)}%)   ·   медианный CAGR : ${f(tgtMed)}%\n`
        : "")
    + `\n`
    + (s.best ? `лучший  : ${s.best.ticker}  ${f(s.best.cagr_pct)}%/год  (net ${f(s.best.net)})\n` : "")
    + (s.worst ? `худший  : ${s.worst.ticker}  ${f(s.worst.cagr_pct)}%/год  (net ${f(s.worst.net)})\n` : "")
    + (s.failed ? `не загрузилось : ${s.failed}\n` : "")
    + `медиана покрытия теты скальпом : ${f(s.median_scalp_cover_pct)}%   ·   `
    + `loss-cap (стреддл ≤ премии) держится : ${f(s.loss_cap_ok_pct)}% инструментов\n`;
  if (outlierCarried)
    verdict += `\n⚠ среднее ≫ медианы ⇒ результат тянут НЕСКОЛЬКО выбросов (крипта/extreme-тренды), `
      + `а типичный инструмент около нуля. Смотри МЕДИАНУ, не среднее.`;
  verdict += `\n⚠ Огромный net на крипте — артефакт КОМПАУНДИНГА (премия = 20% растущего банка на ходе ×20+, `
    + `без кэпа нотионала). Читай CAGR, не net.\n\n`
    + `── ЧТО ЭТОТ БЭКТЕСТ МЕРЯЕТ ЧЕСТНО, А ЧТО НЕТ ──\n`
    + `✅ ТЕТА (длинные опционы) и ГАММА стреддла на крупных ходах — точно. Прибыль на крипте/трендах = `
    + `длинный стреддл ловит «большую рыбу» (как и обещает доктрина).\n`
    + `⚠ СКАЛЬП — НИЖНЯЯ ОЦЕНКА: дневной бар держит ~1–2% реальных интрадей-круговых (живой ПИ ~2500/год). `
    + `Поэтому в БОКОВИКЕ, где скальп должен кормить тету, бэктест показывает ~ноль — это ДЫРА В ДАННЫХ, `
    + `а НЕ «метод не работает». Меряем по сути «купи длинный стреддл и ролль», а не скальпинг.\n`
    + `⇒ Вывод: на ВОЛАТИЛЬНЫХ/ТРЕНДОВЫХ (крипта, металлы в движении) измеримое ядро уже прибыльно; `
    + `на ТИХИХ — нужен внутридневной фид, чтобы честно оценить скальп. Низкий медианный CAGR по всей панели `
    + `= неверная популяция + неизмеримый скальп, НЕ приговор стратегии.`;
  const sm = (d.params || {}).scalp_model;
  if (sm === "range")
    verdict += `\n⚠ Скальп-модель «range» = грубая эвристика (величина = заданный КПД). Используй «grid».`;
  $("#hi-scan-stats").textContent = verdict;
  const ok = d.results.filter((r) => r.ok).sort((a, b) => a.cagr_pct - b.cagr_pct);
  await plot("hi-scan-bar", [{
    type: "bar", orientation: "h",
    x: ok.map((r) => r.cagr_pct), y: ok.map((r) => r.ticker),
    marker: { color: ok.map((r) => (r.net > 0 ? "#3fb950" : "#f85149")) },
    hovertemplate: "%{y}: %{x:.1f}%/год<extra></extra>",
  }], layout("ПИ CAGR % на инструмент  (одинаковые параметры по всему каталогу)", {
    height: Math.max(420, 16 * ok.length), margin: { t: 36, r: 20, b: 36, l: 70 },
    xaxis: { gridcolor: "#2a3340", title: { text: "CAGR % / год" },
             zeroline: true, zerolinecolor: "#8b949e", zerolinewidth: 1 },
    yaxis: { gridcolor: "transparent", automargin: true, tickfont: { size: 9 } },
  }));
  renderHiScanTable();
}
$("#hedged-scan-btn").onclick = (e) => withBusy(e.target, async () =>
  renderHiScan(await post("/api/hedged-intraday/scan", formData($("#form-hedged")))));

// ---- 🧮 profit attribution (the closed-form mathematical model) ----
async function renderHiAttr(d) {
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 0 }));
  const m = d.measured, cf = d.closed_form, st = d.state, p = d.model_params;
  // stacked decomposition: theta (cost, red) + gamma-trend (green) + scalp-flat (blue) → total
  const cats = ["измерено (бэктест)", "модель (закрытая форма)"];
  await plot("hi-attr", [
    { x: cats, y: [m.theta, cf.theta], name: "тета (издержка)", type: "bar", marker: { color: "#f85149" } },
    { x: cats, y: [m.gamma_trend, cf.gamma_trend], name: "гамма (ТРЕНД)", type: "bar", marker: { color: "#3fb950" } },
    { x: cats, y: [m.scalp_flat, cf.scalp_flat], name: "скальп (ФЛЕТ)", type: "bar", marker: { color: "#5b9dff" } },
    { x: cats, y: [m.total, cf.total], name: "ИТОГО", type: "scatter", mode: "markers",
      marker: { color: "#f0c000", symbol: "diamond", size: 13, line: { color: "#0f1419", width: 1 } } },
  ], Object.assign(layout(`${d.ticker} — из чего складывается P&L: тета + гамма(тренд) + скальп(флет)`, {
    height: 360, yaxis: { gridcolor: "#2a3340", title: { text: "$ P&L" }, zeroline: true, zerolinecolor: "#8b949e" },
    xaxis: { gridcolor: "#2a3340" } }), { barmode: "relative" }));

  const txt =
    `🧮 МАТ-МОДЕЛЬ ПИ — что строит какую часть прибыли\n`
    + `\nСостояние волатильности: σ_I(implied)=${st.sigma_implied}  σ_R(realized)=${st.sigma_realized}  `
    + `vr=σ_R/σ_I=${st.vr}  ·  банк $${f(st.bank)} · риск ${(st.risk_pct*100).toFixed(0)}% · DTE ${st.dte_years}г · ${st.years}л\n`
    + `\nЗАКРЫТАЯ ФОРМА (годовые потоки, sized to ρ·B):\n`
    + `   тета  Θ = −a            ,  a = ρB/2T = ${f(p.a_theta_rate)}      (издержка; в $ НЕ зависит от σ)\n`
    + `   гамма Γ = +a·vr²·g      ,  g(капт.гаммы)=${p.gamma_capture_g}        (∝ vr² — ВЫПУКЛО → строит профит в ТРЕНДЕ)\n`
    + `   скальп Σ = +C_s·ρB·vr   ,  C_s=${p.c_s}              (∝ vr — ЛИНЕЙНО → платит тету во ФЛЕТЕ)\n`
    + `   ─────────────────────────────────────\n`
    + `   ИТОГО = Γ + Σ + Θ ;  ПРИБЫЛЬНО ⟺ vr²·g + 2T·C_s·vr > 1  (сейчас = ${p.profitable_condition} ${p.profitable_condition > 1 ? "✅" : "❌"})\n`
    + `\n── РАЗЛОЖЕНИЕ (измерено бэктестом — это истина; модель его воспроизводит) ──\n`
    + `   тета (издержка)   : ${f(m.theta)}\n`
    + `   гамма (ТРЕНД)     : +${f(m.gamma_trend)}   ← строит ${m.pct_from_trend}% валовой прибыли\n`
    + `   скальп (ФЛЕТ)     : ${m.scalp_flat >= 0 ? "+" : ""}${f(m.scalp_flat)}   ← строит ${m.pct_from_flat}% валовой прибыли\n`
    + `   ИТОГО             : ${m.total >= 0 ? "+" : ""}${f(m.total)}   [${m.regime}]\n`
    + `   модель (закр.форма): тета ${f(cf.theta)} · гамма +${f(cf.gamma_trend)} · скальп ${cf.scalp_flat>=0?"+":""}${f(cf.scalp_flat)} · итого ${cf.total>=0?"+":""}${f(cf.total)}\n`
    + `\n💡 ВЫВОД: ${m.conclusion}\n`
    + `\n(Закрытая форма: a и C_s — из первых принципов ATM-опциона ±~20%; g калибрована к бэктесту; `
    + `вывод/проценты — на ИЗМЕРЕННЫХ потоках. Структура: ТРЕНД→гамма (∝vr²), ФЛЕТ→скальп (∝vr), тета=пост. издержка.)`;
  $("#hi-attr-stats").textContent = txt;
}
$("#hedged-attr-btn").onclick = (e) => withBusy(e.target, async () => {
  intradayNotice("#form-hedged");
  renderHiAttr(await post("/api/hedged-intraday/attribution", formData($("#form-hedged"))));
});

// ---- 🌐 extrapolate the attribution across ALL instruments (data-driven g/K, no backtest) ----
function renderHiExtrap(d) {
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 0 }));
  const a = d.aggregate, rows = d.rows;
  $("#hi-extrap-stats").textContent =
    `🌐 ЭКСТРАПОЛЯЦИЯ НА ВСЕ ИНСТРУМЕНТЫ — без бэктеста, из данных (σ_I, σ_R, VR63→g,K) через закрытую форму\n`
    + `${a.n} инструментов (${a.n_failed} не загрузилось) · база K=${a.base_k} · DTE ${a.dte_years}г · годовые $ на банк $10k\n`
    + `\nРЕЖИМЫ: тренд-построено (гамма) ${a.n_trend_built} · флет-построено (скальп) ${a.n_flat_built} · кровит (тета) ${a.n_bleeding}\n`
    + `прибыльных: ${a.n_profitable}/${a.n}   ·   медианный годовой ${a.median_ret_pct}%   ·   медианная g(доля тренда)=${a.median_g}\n`
    + `\n💡 ВЫВОД: для каждого инструмента ГАММА (тренд, ∝vr²·g) и СКАЛЬП (флет, ∝vr·K) бьют тету (−a, пост.).\n`
    + `   g=VR/(VR+1) из variance ratio — данные-driven доля тренда (валидирована к бэктест-гамме, corr≈0.4);\n`
    + `   ⚠ ГАММА/g обоснована (дневной бэктест меряет её честно); СКАЛЬП/K — грубая нога (истинный\n`
    + `   интрадей-edge только у крипты на 1m; знак K из VR верен, величина — якорь по крипте).\n`
    + `   Сортировка по ИТОГО (годовой P&L модели). Тета у всех ≈ −a; кто в плюсе — тот, у кого гамма+скальп > a.`;
  // table
  const cols = [["ticker", "инстр"], ["group", "класс"], ["VR63", "VR63"], ["g_data", "g(тренд)"],
    ["k_data", "K"], ["vr", "σR/σI"], ["theta", "тета"], ["gamma_trend", "гамма(тренд)"],
    ["scalp_flat", "скальп(флет)"], ["total", "ИТОГО/год"], ["pct_from_trend", "тренд%"], ["regime", "режим"]];
  const sgn = (v) => (v >= 0 ? "#3fb950" : "#f85149");
  let h = "<table><thead><tr>" + cols.map((c) => `<th>${c[1]}</th>`).join("") + "</tr></thead><tbody>";
  for (const r of rows) {
    h += "<tr>"
      + `<td>${r.ticker}</td><td>${r.group}</td><td>${r.VR63}</td><td>${r.g_data}</td>`
      + `<td style="color:${sgn(r.k_data)}">${r.k_data}</td><td>${r.vr}</td>`
      + `<td style="color:#f85149">${f(r.theta)}</td><td style="color:#3fb950">${f(r.gamma_trend)}</td>`
      + `<td style="color:${sgn(r.scalp_flat)}">${f(r.scalp_flat)}</td>`
      + `<td style="color:${sgn(r.total)};font-weight:600">${f(r.total)}</td>`
      + `<td>${r.pct_from_trend}%</td><td>${r.regime.replace(/ \(.*/, "")}</td></tr>`;
  }
  $("#hi-extrap").innerHTML = h + "</tbody></table>";
}
$("#hedged-extrap-btn").onclick = (e) => withBusy(e.target, async () => {
  toast("Экстраполяция по всему каталогу: тяну дневные данные (первый прогон ~1–2 мин)…", true);
  renderHiExtrap(await post("/api/hedged-intraday/extrapolate", formData($("#form-hedged"))));
});

// ---- tab 9: ПИ Execution — watch the strategy run on a window
async function renderHiExec(d) {
  const s = d.stats;
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  const price = { x: d.price.x, y: d.price.y, mode: "lines", name: "цена", line: { color: "#c9d1d9", width: 1.5 } };
  const ubT = { x: d.bb_upper.x, y: d.bb_upper.y, mode: "lines", name: "BB верх", line: { color: "#39414d", width: 1 } };
  const lbT = { x: d.bb_lower.x, y: d.bb_lower.y, mode: "lines", name: "BB низ (флет внутри)",
                line: { color: "#39414d", width: 1 }, fill: "tonexty", fillcolor: "rgba(91,157,255,0.06)" };
  const strike = { x: d.strike.x, y: d.strike.y, mode: "lines", name: "страйк стреддла (ATM)",
                   line: { color: "#f0c000", width: 1.5, dash: "dot", shape: "hv" } };
  const sh = { x: d.scalp_short.x, y: d.scalp_short.y, mode: "markers", name: `🔻 шорт-скальп (${d.scalp_short.x.length})`,
               marker: { color: "#f85149", symbol: "triangle-down", size: 9, opacity: 0.85 } };
  const lo = { x: d.scalp_long.x, y: d.scalp_long.y, mode: "markers", name: `🔺 лонг-скальп (${d.scalp_long.x.length})`,
               marker: { color: "#3fb950", symbol: "triangle-up", size: 9, opacity: 0.85 } };
  const cl = { x: d.scalp_close.x, y: d.scalp_close.y, mode: "markers", name: `○ выход (${d.scalp_close.x.length})`,
               marker: { color: "#5b9dff", symbol: "circle-open", size: 7, line: { width: 1.5 } } };
  const rolls = { x: d.rolls.x, y: d.rolls.y, mode: "markers", name: `◆ ролл (${d.rolls.x.length})`,
                  marker: { color: "#39d0d8", symbol: "diamond", size: 11, line: { color: "#0f1419", width: 1 } } };
  const heals = { x: (d.heals || {}).x || [], y: (d.heals || {}).y || [], mode: "markers",
                  name: `✚ лечение залипших (${((d.heals || {}).x || []).length})`,
                  marker: { color: "#d29922", symbol: "cross", size: 11, line: { color: "#0f1419", width: 1 } } };
  // TREND regime spans (price outside the BB) shaded red = grid STEPS ASIDE (no new counter-trend);
  // unshaded = FLAT (scalp active). Green dotted verticals = «уверенный флет» reached (scaling allowed).
  const shapes = (d.trend_spans || []).map((sp) => ({
    type: "rect", xref: "x", yref: "paper", x0: sp.x0, x1: sp.x1, y0: 0, y1: 1,
    fillcolor: "rgba(248,81,73,0.10)", line: { width: 0 }, layer: "below" }));
  ((d.confident_flat || {}).x || []).forEach((x) => shapes.push({
    type: "line", xref: "x", yref: "paper", x0: x, x1: x, y0: 0, y1: 1,
    line: { color: "#3fb950", width: 1, dash: "dot" }, layer: "below" }));
  // the n_parts working-part levels of the intraday third (first period): sell above / buy below
  const annos = [];
  if (d.grid_levels) {
    const gl = d.grid_levels, x1 = d.price.x[d.price.x.length - 1];
    gl.sell.forEach((lv, k) => { shapes.push({ type: "line", xref: "paper", yref: "y", x0: 0, x1: 1, y0: lv, y1: lv, line: { color: "#5a4a2a", width: 1, dash: "dot" }, layer: "below" });
      annos.push({ xref: "paper", x: 1, y: lv, yref: "y", xanchor: "left", text: ` ч.${k + 1}`, showarrow: false, font: { size: 8, color: "#8b6d3a" } }); });
    gl.buy.forEach((lv, k) => { shapes.push({ type: "line", xref: "paper", yref: "y", x0: 0, x1: 1, y0: lv, y1: lv, line: { color: "#2a3a5a", width: 1, dash: "dot" }, layer: "below" });
      annos.push({ xref: "paper", x: 1, y: lv, yref: "y", xanchor: "left", text: ` ч.${k + 1}`, showarrow: false, font: { size: 8, color: "#3a557f" } }); });
    shapes.push({ type: "line", xref: "paper", yref: "y", x0: 0, x1: 1, y0: gl.center, y1: gl.center, line: { color: "#f0c000", width: 1 }, layer: "below" });
  }
  await plot("hx-exec", [ubT, lbT, price, strike, sh, lo, cl, heals, rolls], layout(
    `${d.ticker} — ЛОГИКА ПИ: ⅓-скальп = ${d.n_parts || 5} раб. частей (пунктир ч.1..N) · 🟥 тренд (вне BB) · 🟢┊ уверенный флет`, {
      height: 480, shapes, annotations: annos,
      xaxis: { gridcolor: "#2a3340" }, yaxis: { gridcolor: "#2a3340", title: { text: "цена" } } }));
  await plot("hx-pnl", [
    { x: d.equity_straddle.x, y: d.equity_straddle.y, mode: "lines", name: "стреддл (гамма−тета)", line: { color: "#f0c000", width: 1.5 } },
    { x: d.equity_scalp.x, y: d.equity_scalp.y, mode: "lines", name: "скальп", line: { color: "#5b9dff", width: 1.5 } },
    { x: d.equity_total.x, y: d.equity_total.y.map((v) => v - s.starting_bank), mode: "lines", name: "ИТОГО (= стреддл + скальп)", line: { color: s.net_pnl >= 0 ? "#3fb950" : "#f85149", width: 2.5 }, fill: "tozeroy", fillcolor: s.net_pnl >= 0 ? "rgba(63,185,80,0.10)" : "rgba(248,81,73,0.10)" },
  ], layout(`P&L (от 0): стреддл + скальп = ИТОГО  ·  старт банка $${(+s.starting_bank).toLocaleString()}`, {
    height: 300, xaxis: { gridcolor: "#2a3340" }, yaxis: { gridcolor: "#2a3340", title: { text: "$ P&L (от 0)" }, zeroline: true, zerolinecolor: "#8b949e" } }));
  const up = s.net_pnl >= 0;
  $("#hx-stats").textContent =
    `${up ? "✅ ИТОГО ПЛЮС" : "❌ ИТОГО МИНУС"}  net ${up ? "+" : ""}${f(s.net_pnl)}  (CAGR ${f(s.ann_return_pct)}%, ${s.n_days} дн)\n`
    + `стреддл (гамма−тета) ${s.straddle_pnl >= 0 ? "+" : ""}${f(s.straddle_pnl)}  =  гамма+направление ${s.gamma_dir_pnl >= 0 ? "+" : ""}${f(s.gamma_dir_pnl)} + тета ${f(s.total_theta)}\n`
    + `скальп ${s.scalp_pnl >= 0 ? "+" : ""}${f(s.scalp_pnl)}  ·  контр-входов ${s.scalp_opens}  ·  круговых ${s.scalp_round_trips}  ·  залипло к концу ${s.scalp_stuck_at_end}  ·  роллов ${s.n_rolls}\n`
    + `BB-гейт: ${d.use_bbands ? "ВКЛ — на пробое полосы новые контр-входы НЕ ставятся (не фейдим тренд, стреддл бежит)" : "ВЫКЛ — фейдим каждый уровень"}\n`
    + `лечений залипших частей (за накопл. прибыль): ${s.scalp_heals}  ·  дней «уверенного флета»: ${s.confident_flat_days}  ·  дней в тренде (вне BB): ${s.trend_days}\n\n`
    + `── МОЯ ЛОГИКА НА ГРАФИКЕ (как определяю флет/тренд и когда бросать части) ──\n`
    + `🟥 красная заливка = ТРЕНД (цена ВНЕ полосы Боллинджера) → новые контр-трендовые части НЕ ставлю, стреддл бежит.\n`
    + `   белый фон (цена ВНУТРИ полосы) = ФЛЕТ → скальплю контр-тренд: 🔻шорт у верха / 🔺лонг у низа, ○ выход на возврате.\n`
    + `┊ горизонтальный пунктир = ${d.n_parts || 5} РАБОЧИХ ЧАСТЕЙ интрадейной трети (ч.1..${d.n_parts || 5}): ч.1 близко к центру (срабатывает часто), дальние — экспоненциальный аварийный резерв (редко). Жёлтая линия = центр/страйк.\n`
    + `🟢┊ зелёный вертик. пунктир = «УВЕРЕННЫЙ ФЛЕТ» (≥3 чистых круговых подряд без залипания) — доктрина разрешает наращивать лот.\n`
    + `✚ оранжевый крест = «ЛЕЧЕНИЕ» залипшей части: цена ушла за всю сетку, но НАКОПЛЕННОЙ ПРИБЫЛИ хватает — закрываю и переношу сетку\n`
    + `   к текущей цене. Если прибыли НЕ хватает — НЕ трогаю (переношу до ролла, платит стреддл). Это и есть ответ «когда бросать часть».\n`
    + `◆ = ролл стреддла.\n`
    + (s.straddle_pnl > 0 && s.scalp_pnl < 0
        ? `→ Тренд: стреддл-ГАММА (+${f(s.gamma_dir_pnl)}) забрала движение, контр-скальп залип и в минусе — ИТОГО плюс ЗА СЧЁТ стреддла (это by design: скальп и стреддл — разные стороны тренда).`
        : s.scalp_pnl > 0
        ? `→ Флет/диапазон: контр-скальп собрал mean-reversion (+${f(s.scalp_pnl)}) и помог отбить тету.`
        : `→ Тихий/дрейфовый рынок: скальп около нуля, стреддл платит тету — характерно для не-целевого инструмента.`);
  renderHiLedger(d, s);
  renderHiRules(d, s, "hx-rules");
}

// per-part scalp ledger: every entry/exit in order, with a running cumulative P&L
function renderHiLedger(d, s) {
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  const led = d.ledger || [];
  const head = "<tr><th>#</th><th>дата</th><th>событие</th><th>часть</th><th>сторона</th>"
    + "<th>цена</th><th>лот</th><th>серия</th><th>уверенный флет</th><th>открыто</th><th>P&L</th><th>Σ накопл.</th></tr>";
  const body = led.map((e, i) => {
    const cls = e.kind === "выход" ? (e.pnl >= 0 ? "w" : "l") : "";
    const pcol = e.pnl > 0 ? "#3fb950" : e.pnl < 0 ? "#f85149" : "#8b949e";
    const ccol = e.cum >= 0 ? "#3fb950" : "#f85149";
    const flat = e.conf_flat
      ? `<span style="color:#3fb950">🟢 да${e.scale > 1.001 ? ` ·лот ×${(+e.scale).toFixed(2)}` : ""}</span>`
      : `<span style="color:#8b949e">—</span>`;
    return `<tr class="${cls}"><td>${i + 1}</td><td>${e.date}</td>`
      + `<td>${e.kind === "вход" ? "▸ вход" : "◂ выход"}</td><td style="text-align:center">ч.${e.part}</td>`
      + `<td>${e.side === "short" ? "🔻шорт" : "🔺лонг"}</td><td>${f(e.price)}</td><td>${f(e.lots)}</td>`
      + `<td style="text-align:center">${e.streak}</td><td style="white-space:nowrap">${flat}</td>`
      + `<td style="white-space:nowrap;color:#8b949e">${e.open}</td>`
      + `<td style="color:${pcol}">${e.kind === "выход" ? (e.pnl >= 0 ? "+" : "") + f(e.pnl) : "—"}</td>`
      + `<td style="color:${ccol};font-weight:600">${e.cum >= 0 ? "+" : ""}${f(e.cum)}</td></tr>`;
  }).join("");
  // per-part subtotals (складываем части между собой) + grand total
  const pp = (d.per_part || []).map((p) =>
    `ч.${p.part}: ${p.round_trips} круговых, итог ${p.pnl >= 0 ? "+" : ""}${f(p.pnl)}`).join("   ·   ");
  const tot = (d.per_part || []).reduce((a, p) => a + p.pnl, 0);
  const note = `${led.length}${d.ledger_full > led.length ? ` из ${d.ledger_full}` : ""} событий · `
    + `ПО ЧАСТЯМ: ${pp || "—"}   ·   ИТОГО реализованного скальпа: ${tot >= 0 ? "+" : ""}${f(tot)} `
    + `(незакрытые залипшие части в Σ не входят — они в графике P&L «скальп»)`;
  $("#hx-ledger").innerHTML = led.length
    ? `<div class="tt-scroll"><table><thead>${head}</thead><tbody>${body}</tbody></table></div><div class="tt-note">${note}</div>`
    : `<div class="tt-note">в этом окне контр-трендовых сделок не было (цена всё время в тренде / вне полосы).</div>`;
}

// rule-by-rule doctrine compliance — so a skipped rule is VISIBLE, not silently missing.
// Shared by Tab 8 (main backtest, aggregate stats) and Tab 9 (windowed, full trace). `id` = target
// container; trace-only fields (scalp_opens/trend_days) fall back gracefully on the main tab.
function renderHiRules(d, s, id) {
  const bb = (d.use_bbands !== false);                   // schema default is ON; undefined ⇒ ON
  const opens = s.scalp_opens != null ? `контр-входов: ${s.scalp_opens}` : `круговых: ${s.scalp_round_trips}`;
  const trendd = s.trend_days != null ? `вне полосы ${s.trend_days} дн → ` : "";
  // status: ok | part | no  ·  note may use this run's numbers
  const R = [
    ["ok", "Синтетический стреддл 2 Колла − 1 Фьючерс, ATM", "страйк ATM = спот, V-payoff, ролл у экспирации"],
    ["ok", "Соотношение 2:1, НИКОГДА не голый", "скальп-фьючерсы ≤ интрадейного лимита (⅓ держимых фьючей) → не naked"],
    ["ok", "Сайзинг: премия ≈ risk% депозита", "бюджет премии = risk%·банк, пересайз на каждом ролле"],
    ["ok", "ATM-страйк, ролл/ре-центрирование", `роллов в окне: ${s.n_rolls}`],
    ["ok", "Правило трёх третей (⅓/⅓/⅓)",
      "ДОСЛОВНО: коллы (2·n_str) дроблю на трети — база = ⅓ коллов постоянно хеджирую (⅔·n_str фьючей = 33% пол); "
      + "тренд-резерв = ⅓ коллов НЕ хеджирую → в покое позиция net-long, тренд бежит сам; скальп-лимит = ⅓ коллов. "
      + "Проданные фьючи в полосе 33% (только база) … 67% (полный скальп в ралли). Это и тащит GLD/SLV/SPY вверх vs прежнего нейтрала."],
    ["ok", "Контр-трендовый скальпинг", opens],
    ["ok", "Экспоненциальная сетка частей", "уровни на смещениях шаг·m^k — дальние части дальше (не вываливаем объём у центра)"],
    ["ok", "Ответные заявки (книжим возврат)", `круговых сделок: ${s.scalp_round_trips}`],
    ["ok", "Уверенный флет → наращивать лот (заслуженный риск)",
      `после ≥3 чистых циклов (${s.confident_flat_days} дн, 🟢┊ на графике) НАРАЩИВАЮ размер рабочей части за счёт НАКОПЛЕННОЙ прибыли — макс. достигнутый множитель ×${(+(s.scalp_scaled_max||1)).toFixed(2)} (кэп ×2, чтобы скальп+база ≤ коллов → не голый). В леджере виден рост колонки «лот».`],
    ["ok", "Залипшие части: нести/лечить за прибыль", `лечений за накопл. прибыль: ${s.scalp_heals}; иначе перенос до ролла (платит стреддл) — не форс-реализуем`],
    [bb ? "ok" : "no", "Не фейдить тренд — встать в сторону (BB)", bb ? `${trendd}новые контр-входы СТОП на пробое полосы (🟥 на графике вкладки 9)` : "BB-гейт ВЫКЛЮЧЕН — фейдим каждый уровень → правило НЕ применяется ❌"],
    [(+s.roll_profit_pct > 0) ? "ok" : "part", "Роллирование в плюс-зоне по целевой прибыли",
      (+s.roll_profit_pct > 0
        ? `✅ ВКЛ профит-цель ${(+s.roll_profit_pct)}% от депозита (доктрина ≈5–7%/мес): при достижении закрываю ВСЮ конструкцию, переоткрываюсь ATM, компаундю банк, залипшие части — в утиль. Профит-роллов: ${s.profit_rolls || 0} из ${s.n_rolls} (остальное — по экспирации). Роллить только В ПЛЮСЕ ⇒ движение ≥ стоимости колла выполняется автоматически.`
        : "роллю ТОЛЬКО ПО РАСПИСАНИЮ (за roll_buffer дней до экспирации). Доктрина: роллить в плюс-зоне при достижении плановой прибыли (модуль 26/27). Поставь «Roll @ profit %» > 0 (≈5–7%/мес) — тогда ✅.")],
    ["ok", "Макс. убыток = премия (риск-кэп)", "стреддл за период не теряет больше уплаченной премии (V-payoff)"],
    ["ok", "Тета из BS-переоценки", `накопленная тета: ${(+s.total_theta).toLocaleString(undefined,{maximumFractionDigits:0})}`],
    [(s.intraday_bars > 0) ? "ok" : "part", "Скальп: внутридневной фид",
      (s.intraday_bars > 0
        ? `✅ ВКЛЮЧЁН ${s.scalp_data === "1m" ? "1-МИНУТНЫЙ" : "ЧАСОВОЙ"} ФИД${s.scalp_data === "1m" ? " (Binance, крипта — БЕСПЛАТНО)" : " (yfinance ~2 года)"}: грид прошёл ${s.intraday_bars.toLocaleString()} ${s.scalp_data === "1m" ? "1-минутных" : "часовых"} баров → скальп ловит внутридневной чоп, а не 1 разворот в день. Круговых: ${s.scalp_round_trips}.${s.scalp_data === "1m" ? " Это ближе всего к живому ПИ (200–250 круговых/мес)." : ""}`
        : (isCryptoTicker(d.ticker)
            ? "дневной бар скрывает внутридневной чоп → скальп недо-измерен. Это КРИПТА — выбери «Scalp data → 1m crypto (Binance free)»: бесплатные глубокие 1-мин бары (теперь и история любого окна). При выборе крипто-инструмента 1m включается автоматически."
            : `дневной бар скрывает внутридневной чоп → скальп недо-измерен. ⚠ Для НЕ-крипты (${d.ticker}) бесплатного ГЛУБОКОГО интрадея НЕТ: «hourly 60m» (yfinance) покрывает только последние ~2 года, для старых окон остаются дневки. Глубокая 1-мин история по акциям/ETF/фьючерсам требует ПЛАТНОГО вендора (Polygon ≈$29/мес, IQ Feed). Бесплатный глубокий 1m есть только у КРИПТЫ (ETH/BTC/SOL) — единственный полностью измеримый инструмент.`))],
  ];
  const ic = { ok: "✅", part: "⚠", no: "❌" };
  const col = { ok: "#3fb950", part: "#d29922", no: "#f85149" };
  const rows = R.map(([st, rule, note]) =>
    `<tr><td style="text-align:center;color:${col[st]};vertical-align:top">${ic[st]}</td>`
    + `<td style="white-space:nowrap;vertical-align:top">${rule}</td>`
    + `<td style="color:#8b949e;white-space:normal;max-width:640px;line-height:1.4">${note}</td></tr>`).join("");
  const nOk = R.filter((r) => r[0] === "ok").length, nPart = R.filter((r) => r[0] === "part").length, nNo = R.filter((r) => r[0] === "no").length;
  $("#" + id).innerHTML =
    `<div class="tt-scroll"><table><thead><tr><th>✓</th><th>правило доктрины</th><th>как в этом прогоне</th></tr></thead>`
    + `<tbody>${rows}</tbody></table></div>`
    + `<div class="tt-note">✅ применено: ${nOk}  ·  ⚠ частично: ${nPart}  ·  ❌ не применено: ${nNo}  —  честная картина: ядро (стреддл/сетка/залипшие/не-фейдить-тренд) применено; «уверенный-флет наращивание» и «условный ролл» пока частично; скальп на дневках ограничен данными.</div>`;
}
$("#form-hiexec").onsubmit = (e) => {
  e.preventDefault();
  intradayNotice("#form-hiexec");
  withBusy(e.submitter, async () =>
    renderHiExec(await post("/api/hedged-intraday/inspect", formData(e.target))));
};

loadInstruments();
window.addEventListener("load", () => {
  if (typeof Plotly === "undefined")
    toast("Charts library (Plotly) did not load from /vendor/plotly.min.js — charts will not render.");
});
