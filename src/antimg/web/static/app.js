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

// IV window matters only for iv_source=realized; IV const only for iv_source=constant. Grey out the
// irrelevant one (disabled inputs aren't submitted → backend just uses its default).
function gateIvInputs(formSel) {
  const src = $(formSel + " [name=iv_source]");
  if (!src) return;
  const v = src.value;
  const set = (name, relevant) => {
    const el = $(formSel + ` [name=${name}]`);
    if (!el) return;
    el.disabled = !relevant;
    const lab = el.closest("label");
    if (lab) lab.style.opacity = relevant ? "1" : "0.4";
  };
  set("iv_window", v === "realized");
  set("iv_const", v === "constant");
}
["#form-straddle", "#form-legs", "#form-amov", "#form-picoin", "#form-hedged", "#form-hiexec"].forEach((f) => {
  const src = $(f + " [name=iv_source]");
  if (src) src.addEventListener("change", () => gateIvInputs(f));
  gateIvInputs(f);                                            // apply on load
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
  else if (s.scalp_model === "capture") {
    const cap = $("#form-hedged [name=scalp_capture]").value;
    verdict += `СКАЛЬП-МОДЕЛЬ: capture (ПРОСТАЯ, из реальных дневных ходов) — ловим ${(cap*100).toFixed(0)}% дневного H−L.\n`
      + `  скальп = ${(cap*100).toFixed(0)}% × (дневной размах) × лоты части, СУММА по истории — ТОЛЬКО ПЛЮСЫ:\n`
      + `  закрываем лишь прибыль (доктрина: ответные заявки книжат возврат); убыточные ноги ПЕРЕНОСЯТСЯ,\n`
      + `  их риск ≤ премии стреддла (= та тета, что мы и так платим, + хедж длинных коллов). Тета и ГАММА —\n`
      + `  точные из реального пути. ⇒ Прибыльность = (скальп-плюсы + гамма) − тета; «catch X%» — твой ввод.`;
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
  } else if (cf && s.scalp_model === "capture") {    // capture: positive-only scalp, coverage from real ranges
    const cov = cf.coverage_ratio, winning = cov >= 1.0, capv = $("#form-hedged [name=scalp_capture]").value;
    verdict +=
      `\n\n════════ ЭКВИВАЛЕНТ МОНЕТКИ (0.6 или 0.45?) — ПРОСТАЯ ОЦЕНКА ════════\n`
      + `Ловим ${(capv*100).toFixed(0)}% дневного хода, только плюсы (~200–250 сделок/мес):\n`
      + `доход скальпа/мес : +${f(cf.scalp_per_month)}$   vs   тета/мес ${f(cf.theta_per_month)}$\n`
      + `── ПОКРЫТИЕ = скальп ÷ |тета| = ${f(cov)}  ${winning ? "≥ 1 ✅" : "< 1 ⚠"}\n`
      + `   ${winning
            ? "0.6-ТИПА: выигрышные скальп-сделки САМИ платят тету — плюсовой флет, тренд = бонус."
            : `0.45-ТИПА: скальп-плюсы покрывают ${f(100 * cov)}% теты — остаток должна добрать гамма (тренд).`}\n`
      + `эмпирический p (доля плюсовых периодов стреддла) : ${f(cf.period_win_rate)}  → ${cf.flip_type}\n`
      + `⇒ Если ловить ${(capv*100).toFixed(0)}% хода, скальп строит ${f(Math.min(100*cov,100))}% от «аренды» (теты); линейно по capture.`;
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
  const capDesc = (a.capture_mode === "preset" && a.capture_range)
    ? `пер-класс ${(a.capture_range[0]*100).toFixed(0)}–${(a.capture_range[1]*100).toFixed(0)}% (рангово: металлы/крипта↑, индексы/вол↓, якорь 20%)`
    : `${(a.capture*100).toFixed(0)}% (одно число для всех)`;
  $("#hi-extrap-stats").textContent =
    `🌐 ЭКСТРАПОЛЯЦИЯ НА ВСЕ ИНСТРУМЕНТЫ — из РЕАЛЬНЫХ дневных ходов истории (без интрадей-фида)\n`
    + `${a.n} инструментов (${a.n_failed} не загрузилось) · ловим ${capDesc} дневного хода (High−Low), только плюсы · DTE ${a.dte_years}г · с ${a.start||"2019"}\n`
    + `\nМОДЕЛЬ (просто): СКАЛЬП = capture × (дневной H−L) × лоты части, СУММА по истории — ТОЛЬКО ПЛЮСЫ\n`
    + `   (закрываем лишь прибыль; убыточные ноги переносятся и ПРИКРЫТЫ длинными коллами, риск ≤ премии).\n`
    + `   Всё в % ОТ ТЕТЫ (аренды) — это compounding-инвариантно (абсурдный крипто-компаундинг сокращается).\n`
    + `\nРЕЖИМЫ: флет-построено (скальп) ${a.n_flat_built} · тренд-построено (гамма) ${a.n_trend_built} · кровит (тета) ${a.n_bleeding}\n`
    + `прибыльных: ${a.n_profitable}/${a.n}   ·   медиана: скальп платит ${a.median_scalp_cover_pct}% теты, ЧИСТЫЙ профит = ${a.median_net_cover_pct}% теты\n`
    + `🪙 МОНЕТКА: медианный p(плюсовой период) = ${a.median_win_rate}   ·   p>0.5 у ${a.n_p_above_half}/${a.n}   ⇒ ${a.median_win_rate > 0.5 ? `«${a.median_win_rate >= 0.6 ? "0.6+" : "~0.5"}-монетка» (есть перевес)` : "«0.45-монетка» (нет перевеса)"} — но платёж АСИММЕТРИЧЕН (убыток ≤ премии, гамма выпукла), поэтому +EV даже при p≈0.5\n`
    + `\n💡 ВЫВОД: «скальп %тета» = на сколько % выигрышные скальп-сделки отбивают аренду стреддла.\n`
    + `   ≥100% ⇒ скальп САМ кормит тету (плюсовой флет — что и обещает доктрина); + гамма сверху = чистый профит.\n`
    + `   Чистый = скальп% + гамма% − 100%. Сортировка по чистому. Линейно по Capture — двинь его и пересчитай.\n`
    + `   ⚠ Дефолт capture 0.20 = грид-калиброванный РЕАЛИСТИЧНЫЙ уровень (0.5 был оптимистичен: те же коллы и\n`
    + `   дают гамму, и прикрывают залипший скальп — двойной зачёт). 1m-грид на ETH: ~76% покрытия при ~64% capture.\n`
    + `   ${a.capture_mode === "preset" ? "Режим PRESET: capture задан ПО КЛАССУ (рангово). " : ""}Это СЦЕНАРИЙ при выбранном edge, НЕ прогноз: edge режимо-зависим и varies ВНУТРИ класса (ETH ranged, BTC trended).`;
  const cols = [["ticker","инстр"],["group","класс"],["capture","capt"],["sigma_R","σR"],["win_rate","p(монетка)"],
    ["scalp_cover_pct","скальп %тета"],["gamma_cover_pct","гамма %тета"],["net_cover_pct","ЧИСТ %тета"],
    ["cagr_pct","CAGR%"],["pct_from_trend","тренд%"],["regime","режим"]];
  const sgn = (v) => (v >= 0 ? "#3fb950" : "#f85149");
  let h = "<table><thead><tr>" + cols.map((c) => `<th>${c[1]}</th>`).join("") + "</tr></thead><tbody>";
  for (const r of rows) {
    h += "<tr>"
      + `<td>${r.ticker}</td><td>${r.group}</td><td>${r.capture != null ? r.capture : "—"}</td><td>${r.sigma_R}</td>`
      + `<td style="color:${r.win_rate > 0.5 ? "#3fb950" : "#f85149"};font-weight:600">${r.win_rate}</td>`
      + `<td style="color:${sgn(r.scalp_cover_pct)};font-weight:600">${f(r.scalp_cover_pct)}%</td>`
      + `<td style="color:${sgn(r.gamma_cover_pct)}">${f(r.gamma_cover_pct)}%</td>`
      + `<td style="color:${sgn(r.net_cover_pct)};font-weight:600">${f(r.net_cover_pct)}%</td>`
      + `<td style="color:${sgn(r.cagr_pct)}">${f(r.cagr_pct)}%</td>`
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

// ---- shared row helpers (per-expiry rows vs coin-flip trial rows) -------------------------------
const rowDate = (r) => r.expiry_date || r.end_date;          // resolution date of a period/trial
const rowPnl = (r) => (r.pnl != null ? r.pnl : r.cum_pnl);   // P&L of a period/trial
const streakStr = (m) => (m && Object.keys(m).length
  ? Object.entries(m).map(([k, v]) => `${k}×${v}`).join(", ") : "—");
function outcomeWalk(id, rows, title) {                      // win/loss ±1 cumulative random walk
  let acc = 0;
  const walk = rows.map((r) => (acc += (r.win ? 1 : -1)));
  const wx = rows.map(rowDate);
  const end = walk.length ? walk[walk.length - 1] : 0;
  return plot(id, [
    { x: wx, y: walk, mode: "lines", name: "побед − убытков",
      line: { color: end >= 0 ? "#3fb950" : "#f85149", width: 2 },
      fill: "tozeroy", fillcolor: end >= 0 ? "rgba(63,185,80,0.10)" : "rgba(248,81,73,0.10)" },
    { x: [wx[0], wx[wx.length - 1]], y: [0, 0], mode: "lines", name: "0",
      line: { color: "#8b949e", dash: "dot", width: 1 } },
  ], layout(`${title} (итог ${end >= 0 ? "+" : ""}${end})`, {
    xaxis: { gridcolor: "#2a3340" },
    yaxis: { gridcolor: "#2a3340", title: { text: "победы − убытки" }, zeroline: true, zerolinecolor: "#8b949e" },
  }));
}
function outcomeStreaks(id, ws, ls, title) {                 // streak-length distribution (grouped)
  ws = ws || {}; ls = ls || {};
  const maxLen = Math.max(0, ...Object.keys(ws).map(Number), ...Object.keys(ls).map(Number));
  const lens = []; for (let i = 1; i <= maxLen; i++) lens.push(i);
  return plot(id, [
    { x: lens, y: lens.map((n) => ws[n] || 0), type: "bar", name: "побед подряд", marker: { color: "rgba(63,185,80,0.85)" } },
    { x: lens, y: lens.map((n) => ls[n] || 0), type: "bar", name: "убытков подряд", marker: { color: "rgba(248,81,73,0.85)" } },
  ], layout(title, {
    barmode: "group",
    xaxis: { gridcolor: "#2a3340", title: { text: "длина серии (подряд)" }, dtick: 1 },
    yaxis: { gridcolor: "#2a3340", title: { text: "сколько раз" } },
  }));
}
function outcomeHist(id, rows, unit, U1, avg) {              // P&L per period/trial histogram
  const pnls = rows.map(rowPnl);
  const W = pnls.filter((v) => v > 0), L = pnls.filter((v) => v <= 0);
  const nbins = Math.min(60, Math.max(12, Math.ceil(Math.sqrt(pnls.length || 1))));
  const xbins = pnls.length
    ? (() => { const lo = Math.min(...pnls), hi = Math.max(...pnls); return { start: lo, end: hi, size: (hi - lo) / nbins || 1 }; })()
    : undefined;
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  return plot(id, [
    { x: L, type: "histogram", name: `losses (${L.length})`, xbins, marker: { color: "rgba(248,81,73,0.75)" } },
    { x: W, type: "histogram", name: `wins (${W.length})`, xbins, marker: { color: "rgba(63,185,80,0.8)" } },
  ], layout(`P&L за ${U1} (среднее ${f(avg)})`, {
    barmode: "overlay",
    xaxis: { gridcolor: "#2a3340", title: { text: `P&L за ${U1} ($)` }, zeroline: true, zerolinecolor: "#8b949e" },
    yaxis: { gridcolor: "#2a3340", title: { text: `# ${unit}` } },
  }));
}

// ---- per-trial picture (coin-flip): how the rolls walked the cumulative P&L to ±R ---------------
async function renderTrialDetail(d, i) {
  const t = d.table[i], rolls = t.rolls || [];
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  $("#str-trial-detail").hidden = false;
  const xs = rolls.map((r) => r.roll);
  const R = t.R;
  // chart 1: per-roll premium deployed (bars) + cumulative P&L path (line, 2nd axis) with ±R rails
  await plot("str-trial-chart", [
    { x: xs, y: rolls.map((r) => r.premium), type: "bar", name: "премия ролла (= R + накопл.)",
      marker: { color: "rgba(88,166,255,0.55)" }, yaxis: "y" },
    { x: xs, y: rolls.map((r) => r.cum), mode: "lines+markers", name: "накопл. P&L → к ±R",
      line: { color: "#e3b341", width: 2 }, marker: { size: 6 }, yaxis: "y2" },
    { x: [xs[0], xs[xs.length - 1]], y: [R, R], mode: "lines", name: "+R (выигрыш)",
      line: { color: "#3fb950", dash: "dash", width: 1 }, yaxis: "y2" },
    { x: [xs[0], xs[xs.length - 1]], y: [-R, -R], mode: "lines", name: "−R (проигрыш)",
      line: { color: "#f85149", dash: "dash", width: 1 }, yaxis: "y2" },
    { x: [xs[0], xs[xs.length - 1]], y: [R, R], mode: "lines", name: "R (отметка премии)",
      line: { color: "#8b949e", dash: "dot", width: 1 }, yaxis: "y" },
  ], layout(`Испытание #${i + 1}: ${t.n_rolls} роллов · ${t.win ? "ВЫИГРЫШ" : "ПРОИГРЫШ"} ${f(t.cum_pnl)} (${t.partial ? (t.n_rolls >= d.params.max_rolls ? "горизонт" : "данные") : "±R"})`, {
    xaxis: { gridcolor: "#2a3340", title: { text: "ролл №" }, dtick: 1 },
    yaxis: { gridcolor: "#2a3340", title: { text: "премия ролла $" }, rangemode: "tozero" },
    yaxis2: { title: { text: "накопл. P&L $" }, overlaying: "y", side: "right", zeroline: true, zerolinecolor: "#8b949e" },
  }));
  // chart 2: the underlying price across the trial's rolls (entry→expiry of each roll)
  const px = []; const py = [];
  rolls.forEach((r) => { px.push(r.entry, r.expiry); py.push(r.spot_in, r.spot_out); });
  await plot("str-trial-price", [
    { x: px, y: py, mode: "lines+markers", name: "цена базового",
      line: { color: "#a371f7", width: 2 }, marker: { size: 4 } },
  ], layout(`Цена ${d.summary.ticker} за испытание (${t.start_date} → ${t.end_date})`, {
    xaxis: { gridcolor: "#2a3340" }, yaxis: { gridcolor: "#2a3340", title: { text: "цена" } },
  }));
  $("#str-trial-stats").textContent =
    `🔬 ИСПЫТАНИЕ #${i + 1}  ·  ${t.start_date} → ${t.end_date}  ·  R = ${f(R)}\n`
    + `${t.n_rolls} роллов · премии Σ ${f(t.premium_total)} · выплат Σ ${f(t.payoff_total)} · ИТОГ ${f(t.cum_pnl)} (${t.win ? "выигрыш" : "проигрыш"})\n`
    + `закрыт: ${t.partial ? (t.n_rolls >= d.params.max_rolls ? "по ГОРИЗОНТУ (не дошёл до ±R)" : "по концу ДАННЫХ (хвост)") : "по ±R"}\n`
    + `\n💡 Премия каждого ролла = R + накопл.P&L: после частичного минуса она НИЖЕ R (рискуем остатком до −R),\n`
    + `   после частичного плюса — ВЫШЕ R (на столе и заработанное). Поэтому «премии Σ» гуляет вокруг ${f(R)}×роллов.`;
  // per-roll ledger
  const cols = [["roll","ролл"],["entry","вход"],["expiry","экспир."],["spot_in","S вход"],["spot_out","S выход"],
    ["iv","IV"],["premium","премия (=R+накопл.)"],["payoff","выплата"],["pnl","P&L ролла"],["cum","накопл. P&L"]];
  let lh = "<div class='tt-scroll'><table><thead><tr>" + cols.map((c) => `<th>${c[1]}</th>`).join("") + "</tr></thead><tbody>";
  for (const r of rolls) {
    lh += "<tr>" + cols.map((c) => {
      let v = r[c[0]];
      if (c[0] === "pnl" || c[0] === "cum") return `<td style="color:${v >= 0 ? "#3fb950" : "#f85149"};font-weight:600">${f(v)}</td>`;
      if (c[0] === "premium") return `<td style="color:${v > R ? "#3fb950" : (v < R ? "#f85149" : "#e6edf3")}">${f(v)}</td>`;
      return `<td>${typeof v === "number" ? (+v).toLocaleString(undefined, { maximumFractionDigits: 4 }) : v}</td>`;
    }).join("") + "</tr>";
  }
  $("#str-trial-ledger").innerHTML = lh + `</tbody></table></div><div class="tt-note">${rolls.length} роллов в испытании #${i + 1}</div>`;
}

// ---- tab 10 · pure straddle (per-expiry) OR coin-flip ±R trials ---------------------------------
async function renderStraddle(d) {
  const s = d.summary, rows = d.table, coin = d.mode === "coinflip";
  const unit = coin ? "испытаний" : "периодов", U1 = coin ? "испытание" : "период";
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  await plot("str-equity", [
    { x: d.equity.map((p) => p.date), y: d.equity.map((p) => p.bank), mode: "lines",
      name: "bank", line: { color: "#a371f7", width: 2 }, fill: "tozeroy", fillcolor: "rgba(163,113,247,0.10)" },
    { x: [d.equity[0].date, d.equity[d.equity.length - 1].date], y: [s.starting_bank, s.starting_bank],
      mode: "lines", name: "start bank", line: { color: "#8b949e", dash: "dot", width: 1 } },
  ], layout(`Банк — ${coin ? "монетка ±R" : "чистый стреддл"} — ${s.ticker} (${s.vol_model})`));
  await outcomeWalk("str-walk", rows, `Серии: +1 победа / −1 убыток, накопит. по ${unit}`);
  await outcomeHist("str-hist", rows, unit, U1, s.avg_pnl != null ? s.avg_pnl : s.net_pnl / (s.n_periods || 1));
  await plot("str-dist", [
    { x: ["в плюсе", "в минусе"], y: [s.n_wins, s.n_losses], type: "bar",
      marker: { color: ["rgba(63,185,80,0.85)", "rgba(248,81,73,0.85)"] },
      text: [`${s.n_wins} (${(s.win_rate * 100).toFixed(1)}%)`, `${s.n_losses} (${((1 - s.win_rate) * 100).toFixed(1)}%)`],
      textposition: "auto" },
  ], layout(`Исходы: в плюсе vs в минусе (${s.n_periods} ${unit})`, {
    yaxis: { gridcolor: "#2a3340", title: { text: `# ${unit}` } },
  }));
  await outcomeStreaks("str-streaks", d.win_streaks, d.loss_streaks, `Серии подряд (макс ${s.max_win_streak}W / ${s.max_loss_streak}L)`);
  const profitable = s.net_pnl > 0;
  if (coin) {
    const R = rows.length ? rows[0].R : 0;
    $("#str-stats").textContent =
      `🪙 КОИН-ФЛИП ±R — ЧИСТЫЙ СТРЕДДЛ — ${s.ticker} (${s.vol_model})\n`
      + `${s.n_trials} испытаний · риск=реворд R=${(d.params.risk_pct * 100).toFixed(2)}% банка (1-е R≈${f(R)} $) · DTE ${d.params.dte_days}д · ${s.years} лет · компаундинг ${d.params.compounding ? "вкл" : "выкл"}\n`
      + `take-profit на +R: ${d.params.take_profit === false ? "ВЫКЛ (даём прибыли течь → выигрыш ≥ +R, выпукло)" : "ВКЛ (фиксируем +R → чистая ±R монетка: выигрыш = +R, проигрыш = −R)"}\n`
      + `\nИТОГ: ${profitable ? "📈 ПЛЮС" : "📉 МИНУС"}  ·  банк ${f(s.starting_bank)} → ${f(s.final_bank)}  (чистый ${f(s.net_pnl)} $)  ·  CAGR ${s.ann_return_pct}%\n`
      + `выигрышей: ${s.n_wins}/${s.n_trials} (${(s.win_rate * 100).toFixed(1)}%)  ·  проигрышей ${s.n_losses}  ·  profit factor ${s.profit_factor == null ? "∞" : s.profit_factor}\n`
      + `средн. выигрыш ${f(s.avg_win)} $ (≥ +R, выпукло)  ·  средн. проигрыш ${f(s.avg_loss)} $ (≈ −R)  ·  роллов/испытание: средн ${s.avg_rolls}, макс ${s.max_rolls} (горизонт ${d.params.max_rolls})\n`
      + `по горизонту (частичные, не дошли до ±R за ${d.params.max_rolls} роллов): ${s.n_partial}/${s.n_trials}\n`
      + `\n🪙 ЯЗЫК МОНЕТКИ: p(выигрыш) = ${s.coin_p}  ·  payoff (выигрыш:проигрыш) b = ${s.payoff_ratio == null ? "—" : s.payoff_ratio}:1 (выигрыши перелетают +R, убытки ≈ −R)\n`
      + `   точка безубытка p* = 1/(1+b) = ${s.breakeven_p == null ? "—" : s.breakeven_p}  ·  перевес p−p* = ${s.edge_p == null ? "—" : (s.edge_p >= 0 ? "+" : "") + s.edge_p}  ${s.edge_p > 0 ? "✅ есть перевес" : "❌ нет перевеса"}\n`
      + `   эквивалент СИММЕТРИЧНОЙ 1:1-монетки (тот же EV на R) ≈ ${s.coin_p_symmetric}  ⇒ ${s.coin_p_symmetric >= 0.5 ? "лучше 0.5" : "хуже 0.5"}\n`
      + `\n🔁 СЕРИИ ПОДРЯД:  макс ${s.max_win_streak} побед / ${s.max_loss_streak} проигрышей подряд\n`
      + `   победы подряд : ${streakStr(d.win_streaks)}\n`
      + `   убытки подряд : ${streakStr(d.loss_streaks)}\n`
      + `\n💡 КАК ЧИТАТЬ: каждое испытание = монетка с фикс. риск/реворд R. Роллим стреддл по экспирациям, пока\n`
      + `   накопл. P&L не дойдёт до +R (победа) или −R (проигрыш). Частичный минус переносится (следующий стреддл\n`
      + `   рискует остатком до −R), частичный плюс тоже (ждём добор до +R). Убыток капнут на −R; выигрыш книжится\n`
      + `   фактический (≥ +R, бывает перелёт на крупном движении = выпуклость лонг-опциона).\n`
      + `\n⚠ Премия — модель Блэка-Шоулза по IV (${s.vol_model}), НЕ котировка реального фида; реальный результат обычно ХУЖЕ.`;
    const cols = [["start_date","начало"],["end_date","конец (резолв)"],["n_rolls","роллов"],["R","R (риск=реворд)"],
      ["spot_start","S старт"],["spot_end","S конец"],["premium_total","премии Σ"],["payoff_total","выплат Σ"],
      ["cum_pnl","P&L испыт."],["partial","как закрыт"]];
    let h = "<div class='tt-scroll'><table><thead><tr>" + cols.map((c) => `<th>${c[1]}</th>`).join("") + "</tr></thead><tbody>";
    rows.forEach((r, i) => {
      h += `<tr class="${r.win ? "w" : "l"}" data-i="${i}" style="cursor:pointer">` + cols.map((c) => {
        let v = r[c[0]];
        if (c[0] === "cum_pnl") return `<td style="color:${v >= 0 ? "#3fb950" : "#f85149"};font-weight:600">${f(v)}</td>`;
        if (c[0] === "partial") return `<td>${r.partial ? (r.n_rolls >= d.params.max_rolls ? "горизонт" : "данные") : "±R"}</td>`;
        return `<td>${typeof v === "number" ? (+v).toLocaleString(undefined, { maximumFractionDigits: 4 }) : v}</td>`;
      }).join("") + "</tr>";
    });
    $("#str-table").innerHTML = h + `</tbody></table></div><div class="tt-note">${rows.length} испытаний · кликни строку для разбора по роллам</div>`;
    $$("#str-table tr[data-i]").forEach((tr) => tr.onclick = () => renderTrialDetail(d, +tr.dataset.i));
    if (rows.length) renderTrialDetail(d, 0);                 // show the first trial by default
    return;
  }
  $("#str-trial-detail").hidden = true;                       // expiry mode: no per-roll picture
  const vrp = s.avg_breakeven_pct - s.avg_move_pct;
  $("#str-stats").textContent =
    `🎯 ЧИСТЫЙ СТРЕДДЛ ДО ЭКСПИРАЦИИ — ${s.ticker}  ·  IV-модель: ${s.vol_model}\n`
    + `${s.n_periods} периодов × DTE ${d.params.dte_days}д · risk ${(d.params.risk_pct*100).toFixed(2)}%/период · ${s.years} лет · компаундинг ${d.params.compounding ? "вкл" : "выкл"}\n`
    + `\nИТОГ: ${profitable ? "📈 ПЛЮС" : "📉 МИНУС"}  ·  банк ${f(s.starting_bank)} → ${f(s.final_bank)}  (чистый ${f(s.net_pnl)} $)  ·  CAGR ${s.ann_return_pct}%\n`
    + `прибыльных периодов: ${s.n_wins}/${s.n_periods} (${(s.win_rate*100).toFixed(1)}%)  ·  убыточных ${s.n_losses}  ·  profit factor ${s.profit_factor == null ? "∞" : s.profit_factor}\n`
    + `средн. выигрыш ${f(s.avg_win)} $  ·  средн. проигрыш ${f(s.avg_loss)} $  ·  средн. P&L ${f(s.avg_pnl)} $\n`
    + `\n🔁 СЕРИИ ПОДРЯД (длина×сколько раз):  макс ${s.max_win_streak} побед / ${s.max_loss_streak} убытков подряд\n`
    + `   победы подряд : ${streakStr(d.win_streaks)}\n`
    + `   убытки подряд : ${streakStr(d.loss_streaks)}\n`
    + `   (читается «3×2» = серия из 3 подряд случилась 2 раза. Стреддл чаще проигрывает → длинные серии убытков, редкие но крупные победы.)\n`
    + `   (risk % = ВЕСЬ стреддл колл+пут вместе; для ATM делится ≈ поровну — см. столбцы «колл $» / «пут $» в таблице.)\n`
    + `\n💸 «АРЕНДА» (стоимость стреддла): заплачено премии Σ ${f(s.total_premium)}  ·  получено на экспирации Σ ${f(s.total_payoff)}\n`
    + `   возврат премии: ${s.premium_recovered_pct}%  ⇒ ${s.premium_recovered_pct >= 100 ? "выплаты ПОКРЫЛИ премию (стреддл окупился)" : "выплаты НЕ покрыли премию (стреддл стоил дороже, чем дал)"}\n`
    + `\n📏 ПОЧЕМУ: чтобы выйти в ноль, нужен ход ≥ премии. Средний нужный ход (breakeven) = ${s.avg_breakeven_pct}%;\n`
    + `   средний РЕАЛЬНЫЙ ход к экспирации = ${s.avg_move_pct}%.  Разница ${vrp >= 0 ? "+" : ""}${vrp.toFixed(2)}пп ${vrp > 0 ? "= IV дороже движения ⇒ премия за риск волатильности съедает стреддл (−EV)" : "= движение перекрыло IV ⇒ стреддл в плюсе"}.\n`
    + `\n⚠ Данные: цена базового — реальная; премия — модель Блэка-Шоулза по IV (${s.vol_model}), НЕ котировка реального опционного фида.\n`
    + `   Качество ответа зависит от IV-модели. Реальные опционы обычно ещё дороже (бид-аск, IV-надбавка) → реальный результат ХУЖЕ.`;
  const cols = [["entry_date","вход"],["expiry_date","экспирация"],["spot_entry","S вход (=K)"],
    ["spot_expiry","S экспир."],["iv","IV"],["prem_per_unit","премия/ед"],["units","единиц"],
    ["call_cost","колл $"],["put_cost","пут $"],["premium_paid","заплачено $ (=колл+пут)"],
    ["payoff","выплата $"],["pnl","P&L $"],["bank_after","банк после"],
    ["breakeven_pct","нужен ход %"],["move_pct","реальн. ход %"]];
  let h = "<div class='tt-scroll'><table><thead><tr>" + cols.map((c) => `<th>${c[1]}</th>`).join("") + "</tr></thead><tbody>";
  for (const r of rows) {
    h += `<tr class="${r.win ? "w" : "l"}">` + cols.map((c) => {
      let v = r[c[0]];
      if (c[0] === "pnl") return `<td style="color:${v >= 0 ? "#3fb950" : "#f85149"};font-weight:600">${f(v)}</td>`;
      return `<td>${typeof v === "number" ? (+v).toLocaleString(undefined, { maximumFractionDigits: 4 }) : v}</td>`;
    }).join("") + "</tr>";
  }
  $("#str-table").innerHTML = h + `</tbody></table></div><div class="tt-note">${rows.length} периодов · прокрути</div>`;
}
$("#form-straddle").onsubmit = (e) => {
  e.preventDefault();
  const o = formData(e.target);
  if (o.risk_pct != null) o.risk_pct = o.risk_pct / 100;   // field is a PERCENT (1 = 1%) → fraction for the API
  withBusy(e.submitter, async () =>
    renderStraddle(await post("/api/pure-straddle", o)));
};

// ---- tab 11 · call vs put, each leg analysed separately (per-expiry OR coin-flip ±R) ------------
async function renderLegs(d) {
  const c = d.call.summary, p = d.put.summary, coin = d.mode === "coinflip";
  const unit = coin ? "испытаний" : "периодов";
  await plot("lg-counts", [
    { x: ["колл", "пут"], y: [c.n_wins, p.n_wins], type: "bar", name: "в плюсе",
      marker: { color: "rgba(63,185,80,0.85)" }, text: [`${c.n_wins}`, `${p.n_wins}`], textposition: "auto" },
    { x: ["колл", "пут"], y: [c.n_losses, p.n_losses], type: "bar", name: "в минусе",
      marker: { color: "rgba(248,81,73,0.85)" }, text: [`${c.n_losses}`, `${p.n_losses}`], textposition: "auto" },
  ], layout(`Исходы по ногам (колл ${c.n_periods} / пут ${p.n_periods} ${unit})`, {
    barmode: "group", yaxis: { gridcolor: "#2a3340", title: { text: `# ${unit}` } },
  }));
  await outcomeWalk("lg-call-walk", d.call.table, `КОЛЛ: +1/−1 накопит. по ${unit}`);
  await outcomeStreaks("lg-call-streaks", d.call.win_streaks, d.call.loss_streaks, `КОЛЛ серии (макс ${c.max_win_streak}W / ${c.max_loss_streak}L)`);
  await outcomeWalk("lg-put-walk", d.put.table, `ПУТ: +1/−1 накопит. по ${unit}`);
  await outcomeStreaks("lg-put-streaks", d.put.win_streaks, d.put.loss_streaks, `ПУТ серии (макс ${p.max_win_streak}W / ${p.max_loss_streak}L)`);
  const tail = coin
    ? (lg) => `  ·  CAGR ${lg.summary.ann_return_pct}%  ·  роллов/исп. средн ${lg.summary.avg_rolls}`
    : (lg) => `  ·  CAGR ${lg.summary.ann_return_pct}%  ·  возврат премии ${lg.summary.premium_recovered_pct}%`;
  const coinLine = (lg) => {
    const s = lg.summary;
    return `   🪙 монетка: p=${s.coin_p} · payoff b=${s.payoff_ratio == null ? "—" : s.payoff_ratio}:1 · безубыток p*=${s.breakeven_p == null ? "—" : s.breakeven_p} · перевес ${s.edge_p == null ? "—" : (s.edge_p >= 0 ? "+" : "") + s.edge_p} · 1:1-эквив. ${s.coin_p_symmetric}\n`;
  };
  const block = (name, lg) => {
    const s = lg.summary;
    return `${name}: в плюсе ${s.n_wins}/${s.n_periods} (${(s.win_rate * 100).toFixed(1)}%)  ·  макс серия ${s.max_win_streak}W / ${s.max_loss_streak}L${tail(lg)}\n`
      + (coin ? coinLine(lg) : "")
      + `   победы подряд : ${streakStr(lg.win_streaks)}\n`
      + `   убытки подряд : ${streakStr(lg.loss_streaks)}\n`;
  };
  $("#lg-stats").textContent =
    `📊 КОЛЛ vs ПУТ — каждая нога отдельно${coin ? " · коин-флип ±R" : ""} — ${d.ticker} (${d.vol_model})\n\n`
    + block("📈 КОЛЛ (выигрывает на росте)", d.call) + "\n"
    + block("📉 ПУТ (выигрывает на падении)", d.put)
    + `\n💡 Серии колла и пута почти ЗЕРКАЛЬНЫ: когда рынок растёт — колл копит победы, пут копит убытки, и наоборот.\n`
    + (coin
        ? `   coin-flip: каждое испытание роллит ногу до +R (победа) или −R (проигрыш); убыток капнут на −R, выигрыш фактический (выпукло).`
        : `   Каждая нога по отдельности обычно −EV (платишь IV-премию за риск волатильности). Колл+пут вместе = стреддл (Tab 10).`);
}
$("#form-legs").onsubmit = (e) => {
  e.preventDefault();
  const o = formData(e.target);
  if (o.risk_pct != null) o.risk_pct = o.risk_pct / 100;   // PERCENT (1 = 1%) → fraction
  withBusy(e.submitter, async () =>
    renderLegs(await post("/api/leg-analysis", o)));
};

// ---- tab 12 · ПИ × antimartingale overlay ------------------------------------------------------
async function renderAntimg(d) {
  const s = d.summary, rows = d.table;
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 0 }));
  // equity: flat (base size) vs antimartingale-scaled, over period end-dates
  await plot("am-equity", [
    { x: d.dates, y: d.flat_equity, mode: "lines", name: "flat (база)",
      line: { color: "#8b949e", width: 2 } },
    { x: d.dates, y: d.am_equity, mode: "lines", name: "антимартингейл",
      line: { color: s.am_total >= s.flat_total ? "#3fb950" : "#f85149", width: 2 },
      fill: "tonexty", fillcolor: "rgba(163,113,247,0.10)" },
  ], layout(`ПИ × антимарт. накопл. P&L — ${s.ticker} (${s.period}, DTE ${s.dte_days}д)`, {
    xaxis: { gridcolor: "#2a3340" }, yaxis: { gridcolor: "#2a3340", title: { text: "накопл. P&L $" }, zeroline: true, zerolinecolor: "#8b949e" },
  }));
  // shuffle test: histogram of AM totals on shuffled order, with the REAL result as a line
  if (d.shuffle_samples && d.shuffle_samples.length) {
    const real = s.am_total;
    await plot("am-shuffle", [
      { x: d.shuffle_samples, type: "histogram", name: `shuffle (${d.shuffle_samples.length})`,
        marker: { color: "rgba(88,166,255,0.6)" } },
    ], layout(`Shuffle-тест: реал ${f(real)} = ${s.real_pctile}-й перцентиль`, {
      xaxis: { gridcolor: "#2a3340", title: { text: "антимарт. итог на перемешанном порядке $" } },
      yaxis: { gridcolor: "#2a3340", title: { text: "# перемешиваний" } },
      shapes: [
        { type: "line", x0: real, x1: real, yref: "paper", y0: 0, y1: 1, line: { color: "#e3b341", width: 2, dash: "dash" } },
        { type: "line", x0: s.flat_total, x1: s.flat_total, yref: "paper", y0: 0, y1: 1, line: { color: "#8b949e", width: 1, dash: "dot" } },
      ],
    }));
  } else {
    await plot("am-shuffle", [], layout("Shuffle-тест выключен (Shuffles = 0)"));
  }
  // verdict
  const amBeatsFlat = s.am_total > s.flat_total;
  const clustering = s.n_shuffles > 0
    ? (s.real_pctile >= 90 ? "✅ ДА — реальный порядок бьёт перемешанный (победы кластеризуются → реальная альфа серий)"
       : s.real_pctile <= 10 ? "❌ НЕТ (хуже перемешанного — победы РАЗРОЗНЕНЫ, пирамидинг вредит)"
       : "➖ НЕТ значимой (реал ≈ медиане shuffle ⇒ порядок не важен, это просто ПЛЕЧО на распределении)")
    : "(shuffle-тест выключен)";
  const p = s.win_rate, N = s.target_streak;
  const evMult = Math.pow(2 * p, N) - 1;                 // EV-identity per-cycle multiple (skill)
  const srcLabel = s.source === "doctrine" ? "ДОКТРИНА 9/3 (синтетика)" : "ПИ backtest";
  $("#am-stats").textContent =
    `🎲 ПИ × АНТИМАРТИНГЕЙЛ — ${s.ticker} (${s.vol_model}) · источник: ${srcLabel} · ${s.n_periods} периодов\n`
    + `правило: после ПЛЮСА ×2 риск, после минуса сброс к базе, фиксация серии на ${N} подряд · p(плюс)=${p} · макс серия ${s.max_win_streak} · макс множитель ×${s.max_mult}\n`
    + `\n🔑 EV-ТОЖДЕСТВО (скилл): за цикл из ${N} побед EV = b·[(2p)^N − 1] = b·[(2·${p})^${N} − 1] = ${evMult >= 0 ? "+" : ""}${evMult.toFixed(2)}·b\n`
    + `   ⇒ при p=${p} ${p > 0.5 ? "✅ (2p)>1 → пирамидинг РАСТЁТ с серией (антимартингейл работает — то, ради чего он и нужен)" : "❌ (2p)≤1 → пирамидинг НЕ помогает (нужен p>0.5; здесь бэктест занижает win-rate, см. ниже)"}\n`
    + `\nИТОГ (база за период = 1×):  flat Σ ${f(s.flat_total)}  →  антимарт Σ ${f(s.am_total)}   (альфа ${s.alpha >= 0 ? "+" : ""}${f(s.alpha)})  ${amBeatsFlat ? "📈" : "📉"}\n`
    + `просадка:  flat ${f(s.flat_max_dd)}  ·  антимарт ${f(s.am_max_dd)}  (плечо увеличивает и просадку)\n`
    + `\n🎯 SHUFFLE-ТЕСТ (отделяет КЛАСТЕРИЗАЦИЮ серий от эффекта p>0.5):\n`
    + `   реальный антимарт = ${f(s.am_total)} = ${s.real_pctile}-й перцентиль перемешанных (медиана ${f(s.shuffle_median_am)}, 5–95% ${f(s.shuffle_p05)}…${f(s.shuffle_p95)})\n`
    + `   ⇒ кластеризация: ${clustering}\n`
    + `\n💡 ДВА РАЗНЫХ ВОПРОСА:\n`
    + `   (1) «бьёт ли AM флэт?» — да при p>0.5 (это эффект (2p)^N−1, ядро антимартингейла);\n`
    + `   (2) «добавляет ли ПОРЯДОК сверх этого?» — shuffle-тест (кластеризуются ли победы во времени).\n`
    + (s.source === "doctrine"
        ? `\n📌 Источник DOCTRINE: периоды СИНТЕТИЧЕСКИЕ при плановом win-rate Коровина (9/3=0.75). Это показывает, что\n   ЕСЛИ ПИ реально даёт 75% плюсовых месяцев — антимартингейл сильно +EV. Победы i.i.d. ⇒ кластеризации нет\n   (shuffle≈реал), весь выигрыш AM — от p>0.5. Реальный ПИ-бэктест (free daily) скальп НЕ видит → занижает p.`
        : `\n📌 Источник BACKTEST: free daily НЕ видит интрадей-скальп ⇒ win-rate занижен (тета доминирует). Доктрина\n   Коровина — 9/3 (p≈0.75): переключи Источник на «doctrine», чтобы оценить антимартингейл на плановом win-rate.`);
  // per-period table
  const cols = [["i","#"],["open","начало"],["close","конец"],["pnl","P&L периода"],["mult","риск ×"],
    ["contribution","вклад (×P&L)"],["flat_cum","flat накопл."],["am_cum","антимарт накопл."],["streak_before","серия до"]];
  let h = "<div class='tt-scroll'><table><thead><tr>" + cols.map((c) => `<th>${c[1]}</th>`).join("") + "</tr></thead><tbody>";
  for (const r of rows) {
    h += `<tr class="${r.win ? "w" : "l"}">` + cols.map((c) => {
      let v = r[c[0]];
      if (c[0] === "pnl" || c[0] === "contribution") return `<td style="color:${v >= 0 ? "#3fb950" : "#f85149"};font-weight:600">${(+v).toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>`;
      if (c[0] === "mult") return `<td style="font-weight:600">×${v}</td>`;
      return `<td>${typeof v === "number" ? (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }) : v}</td>`;
    }).join("") + "</tr>";
  }
  $("#am-table").innerHTML = h + `</tbody></table></div><div class="tt-note">${rows.length} периодов</div>`;
}
$("#form-amov").onsubmit = (e) => {
  e.preventDefault();
  withBusy(e.submitter, async () => {
    toast("Прогон ПИ-бэктеста + антимартингейл + shuffle-тест…", true);
    renderAntimg(await post("/api/hedged-intraday/antimartingale", formData(e.target)));
  });
};

// ---- tab 13 · ПИ coin estimator ----------------------------------------------------------------
async function renderPiCoin(d) {
  const f2 = (v) => (v == null ? "—" : (+v).toFixed(2));
  if (d.scan) {
    await plot("pc-curve", [], layout("Скан каталога — см. таблицу и распределение →"));
    const rows = d.rows, a = d.aggregate;
    await plot("pc-scan", [
      { x: rows.map((r) => r.ticker), y: rows.map((r) => r.p_net), type: "bar",
        marker: { color: rows.map((r) => (r.p_net >= 0.6 ? "#3fb950" : (r.p_net >= 0.55 ? "#d29922" : "#f85149"))) } },
    ], layout(`p_net по каталогу (c=${a.c}, cost ${a.cost_drag}, DTE ${a.dte_days}д)`, {
      xaxis: { gridcolor: "#2a3340", tickangle: -60, automargin: true },
      yaxis: { gridcolor: "#2a3340", title: { text: "p_net" }, range: [0, 1] },
      shapes: [0.5, 0.55, 0.6].map((y) => ({ type: "line", xref: "paper", x0: 0, x1: 1, y0: y, y1: y,
        line: { color: "#8b949e", dash: "dot", width: 1 } })),
    }));
    $("#pc-stats").textContent =
      `🪙 ПИ COIN — СКАН КАТАЛОГА (c=${a.c} gross, cost ${a.cost_drag}, VRP-хейркат ${a.vrp_proxy}, DTE ${a.dte_days}д) · ${a.n} инструментов\n`
      + `≥0.60: ${a.n_above_060}   ·   ≥0.55: ${a.n_above_055}   ·   медиана p_net: ${a.median_p_net}\n`
      + `\n⚠⚠ ДОВЕРЯЙ ТОЛЬКО строкам «IV=реал» (${a.n_real_iv} из ${a.n}): только у них РЕАЛЬНАЯ подразумеваемая воля\n`
      + `   (VIX/VXN/VXD/RVX/GVZ/OVX/EVZ = S&P/Nasdaq/Dow/Russell/золото/нефть/EURUSD). У остальных IV НЕТ —\n`
      + `   подставлена реализованная (+VRP-хейркат ${a.vrp_proxy}); их p_net = ОЦЕНКА, не факт (реальные опционы\n`
      + `   крипты/серебра тоже дорогие — гуру: ETH IV 60-90%). Поставь VRP-хейркат выше, если не доверяешь.\n`
      + `Зелёный p_net≥0.6 · жёлтый 0.55–0.6 · красный <0.55. Среди РЕАЛ-IV: индексы (SPY/Dow) дороги (плохо), золото/нефть мягче.`;
    const cols = [["ticker","инстр"],["group","класс"],["iv_is_real","IV"],["p_net","p_net"],["p_out","p_out (2-я)"],
      ["rv_over_iv","RV/IV"],["variance_ratio","VR(63)"],["wickiness","wick"],
      ["c_star_060","c* для 0.6"],["ev_per_theta","EV/θ"],["payoff_ratio","payoff b"]];
    let h = "<div class='tt-scroll'><table><thead><tr>" + cols.map((c) => `<th>${c[1]}</th>`).join("") + "</tr></thead><tbody>";
    for (const r of rows) {
      h += `<tr class="${r.p_net >= 0.55 ? "w" : ""}">` + cols.map((c) => {
        let v = r[c[0]];
        if (c[0] === "iv_is_real") return `<td>${v ? "✅реал" : "≈прокси"}</td>`;
        if (c[0] === "p_net") return `<td style="color:${v >= 0.6 ? "#3fb950" : (v >= 0.55 ? "#d29922" : "#f85149")};font-weight:700">${f2(v)}${r.iv_is_real ? "" : "*"}</td>`;
        return `<td>${typeof v === "number" ? (+v).toLocaleString(undefined, { maximumFractionDigits: 3 }) : (v == null ? "—" : v)}</td>`;
      }).join("") + "</tr>";
    }
    $("#pc-table").innerHTML = h + `</tbody></table></div><div class="tt-note">${rows.length} инструментов</div>`;
    return;
  }
  const e = d.estimate;
  await plot("pc-scan", [], layout(""));
  const cs = e.curve.map((p) => p.c), ps = e.curve.map((p) => p.p);
  await plot("pc-curve", [
    { x: cs, y: ps, mode: "lines+markers", name: "p_net(c)", line: { color: "#a371f7", width: 2 } },
    { x: [d.params.c, d.params.c], y: [0, 1], mode: "lines", name: `текущее c=${d.params.c}`,
      line: { color: "#e3b341", dash: "dash", width: 1 } },
    { x: [e.c_suggest, e.c_suggest], y: [0, 1], mode: "lines", name: `c~ оценка ${e.c_suggest}`,
      line: { color: "#58a6ff", dash: "dot", width: 1 } },
  ], layout(`p_net vs покрытие c — ${e.ticker} (${e.vol_model})`, {
    xaxis: { gridcolor: "#2a3340", title: { text: "scalp coverage c (gross)" } },
    yaxis: { gridcolor: "#2a3340", title: { text: "p_net" }, range: [0, 1] },
    shapes: [0.5, 0.55, 0.6].map((y) => ({ type: "line", xref: "paper", x0: 0, x1: 1, y0: y, y1: y,
      line: { color: "#8b949e", dash: "dot", width: 1 } })),
  }));
  const worthy = e.p_net >= 0.55;
  const stable = Math.abs(e.p_in - e.p_out) <= 0.15;
  $("#pc-stats").textContent =
    `🪙 ПИ COIN ESTIMATE — ${e.ticker} (${e.vol_model}) · ${e.n_periods} периодов × DTE ${e.dte_days}д\n`
    + `\n⇒ p_net = ${f2(e.p_net)}  (при c=${d.params.c} gross − cost ${d.params.cost_drag} = c_net ${e.c_net})  ${worthy ? "✅ ≥0.55 — монетка с перевесом" : "❌ <0.55 — ~честная монетка"}\n`
    + `АНТИМАРТИНГЕЙЛ: ${e.p_net >= 0.55 ? "ИМЕЕТ смысл (p>0.5 ⇒ (2p)^N−1>0, пирамида растёт с серией)" : "НЕ имеет смысла (p≈0.5 ⇒ (2p)^N−1≈0; edge тут — асимметрия выплат, не серии)"}\n`
    + `\n📊 EV/θ = ${f2(e.ev_per_theta)} за период · средн.выигрыш ${f2(e.avg_win)}θ / проигрыш ${f2(e.avg_loss)}θ · payoff b=${e.payoff_ratio == null ? "∞" : e.payoff_ratio} ⇒ безубыток p*=${f2(e.breakeven_p)}\n`
    + `   (даже если p_net<0.5, при b>1 и p>p* стратегия +EV за счёт ВЫПУКЛОСТИ — но антимартингейлу нужен именно p>0.5.)\n`
    + `\n🔭 ЗАРАНЕЕ-метрики этого инструмента:\n`
    + `   IV-источник: ${e.iv_is_real ? "✅ РЕАЛЬНЫЙ вол-индекс (VIX-семья) — RV/IV и p_net ДОСТОВЕРНЫ" : `≈ ПРОКСИ (реальных опционов нет → IV=реализованная +VRP-хейркат ${e.vrp_applied}). p_net = ОЦЕНКА, не факт (реальные опционы могут быть дороже)`}\n`
    + `   RV/IV (медиана) = ${e.rv_over_iv}  ${e.rv_over_iv >= 1 ? "✅ опционы дёшевы (RV≥IV)" : "⚠ опционы дороги (IV>RV, премия за риск воли)"}\n`
    + `   wickiness = ${e.wickiness} (выше ⇒ больше интрадей-возврата/«соплей» ⇒ выше достижимое c)  ·  VR(63) = ${e.variance_ratio} ${e.variance_ratio < 1 ? "(возврат к среднему — скальп работает)" : "(тренд — скальпу хуже)"}\n`
    + `   оценка достижимого c ≈ ${e.c_suggest} (прокси, не факт)\n`
    + `\n🎯 КРИТИЧЕСКОЕ ПОКРЫТИЕ: чтобы p_net=0.55 нужно c=${e.c_star_055 < 0 ? "недостижимо <1" : e.c_star_055}; для 0.60 — c=${e.c_star_060 < 0 ? "недостижимо <1" : e.c_star_060}.\n`
    + `   ⇒ ${e.c_star_060 >= 0 && e.c_star_060 <= e.c_suggest + 0.1 ? "достижимо при оценке c~ — кандидат на 0.6" : "нужно покрытие выше оценки c~ — маловероятно 0.6"}\n`
    + `\n🧪 СТАБИЛЬНОСТЬ (walk-forward): p_net 1-я половина ${f2(e.p_in)} / 2-я половина ${f2(e.p_out)}  ${stable ? "✅ устойчиво" : "⚠ НЕустойчиво — режим менялся, не экстраполируй"}\n`
    + `\n💡 Это ОЦЕНКА из наблюдаемых данных, не гарантия. Надёжная часть — кривая p_net(c) и c*; достижимость c — вопрос интрадей-возврата (см. wickiness/VR). RV/IV — главный рычаг.`;
  $("#pc-table").innerHTML = "";
}
$("#form-picoin").onsubmit = (e) => {
  e.preventDefault();
  withBusy(e.submitter, async () => {
    const o = formData(e.target);
    if (o.scan === "true") toast("Скан каталога: тяну данные по всем инструментам (1–2 мин)…", true);
    renderPiCoin(await post("/api/pi-coin", o));
  });
};
// one-click: rate & rank the WHOLE catalog (forces scan=true regardless of the dropdown)
$("#picoin-rate-all").onclick = (e) => withBusy(e.target, async () => {
  toast("Рейтинг всех инструментов: тяну данные по каталогу (первый прогон ~1–2 мин)…", true);
  const o = formData($("#form-picoin"));
  o.scan = "true";
  renderPiCoin(await post("/api/pi-coin", o));
});

// ---- Tab 14 periods: one instrument's WHOLE history as a table + the AVERAGE-period payoff
async function renderPiSimPeriods(d) {
  const money = (x) => (x >= 0 ? "+$" : "−$") + Math.abs(Math.round(x)).toLocaleString();
  const a = d.aggregate, rows = d.rows, am = d.am, rr = a.risk_reward;
  const amOn = ($("#form-pisim [name=am_on]") || {}).value === "on";
  $("#sim-periods").hidden = false; $("#sim-scan").hidden = true;
  $("#sim-verdict").textContent = ""; $("#sim-steps").innerHTML = "";
  $("#sim-stats").textContent = ""; $("#sim-grid").innerHTML = "";
  Plotly.purge("sim-chart"); Plotly.purge("sim-payoff");
  const asset = (a.ticker || "").split("-")[0] || a.ticker;

  $("#sim-periods-stats").textContent =
    `📋 ${a.ticker} — ${a.n} периодов по ${a.dte_days} дн (${a.start} → ${a.end}), депозит $${a.deposit.toLocaleString()}, премия $${Math.round(a.premium).toLocaleString()}/период\n`
    + `\n── СРЕДНЕЕ ЗА ПЕРИОД ──\n`
    + `стреддл-ядро : ${money(a.straddle_mean)}/период  (выигрышных ${a.straddle_win_pct}% — длинный стреддл сам по себе ${a.straddle_mean < 0 ? "кровоточит" : "в плюсе"})\n`
    + `скальп       : ${money(a.scalp_mean)}/период  (чоп-модель − залипшие части)\n`
    + `ИТОГО        : ${money(a.total_mean)}/период  (выигрышных ${a.total_win_pct}%)\n`
    + `\n── 🎯 RISK / REWARD (база 10%) ──\n`
    + `средний ВЫИГРЫШ : ${money(rr.avg_win)}   ·   средний ПРОИГРЫШ : ${money(rr.avg_loss)}\n`
    + `макс. ВЫИГРЫШ   : ${money(rr.max_win)}   ·   макс. ПРОИГРЫШ   : ${money(rr.max_loss)}\n`
    + `payoff (W/L) : ${rr.payoff_ratio ?? "—"}×   ·   profit factor : ${rr.profit_factor ?? "—"}   ·   ожидание : ${money(rr.expectancy)}/период\n`
    + `\n── ЗА ВСЮ ИСТОРИЮ ──\n`
    + `сумма ИТОГО  : ${money(a.total_sum)}  (стреддл ${money(a.straddle_sum)} + скальп ${money(a.scalp_sum)})\n`
    + `доходность   : ${a.ann_return_pct >= 0 ? "+" : ""}${a.ann_return_pct}%/год  ·  лучший ${money(a.best)}  ·  худший ${money(a.worst)}  ·  макс.просадка ${money(am.flat_max_dd)}\n`
    + `средний ход  : ${a.avg_move_pct >= 0 ? "+" : ""}${a.avg_move_pct}% (|${a.avg_abs_move_pct}%|)  ·  средний безубыток ±${a.avg_breakeven_pct}%  ·  средняя IV ${(a.avg_iv * 100).toFixed(0)}%`
    + (amOn ? `\n\n── 🎲 АНТИМАРТИНГЕЙЛ (удвоение на просадке, сброс на новом максимуме · cap ×${am.cap_mult}) ──\n`
      + `итог: ${money(am.am_final - a.deposit)} vs база ${money(am.flat_final - a.deposit)}  →  ${am.am_ann_return_pct}%/год vs ${am.flat_ann_return_pct}%/год  (Δ ${(am.am_ann_return_pct - am.flat_ann_return_pct).toFixed(1)} пп)\n`
      + `макс.просадка ${money(am.am_max_dd)} vs ${money(am.flat_max_dd)}  ·  макс.множитель ×${am.max_mult}  ·  макс.проигрыш ${money(am.am_rr.max_loss)} vs ${money(rr.max_loss)}\n`
      + `⚠ антимартингейл НЕ создаёт edge на ~честной последовательности — усиливает дисперсию (больше просадка/худший период). Помогает только если плюсы кластеризуются.` : "");

  // ── AVERAGE-period payoff (in move-% space, price-independent) ──
  const prem = a.premium, beFrac = a.avg_breakeven_pct / 100;
  const beNetFrac = Math.max(0, beFrac * (1 - a.scalp_mean / prem));      // scalp shifts the V up
  const span = Math.max(0.15, 1.5 * beFrac + a.avg_abs_move_pct / 100);
  const N = 81, xs = [], strad = [], withScalp = [];
  for (let i = 0; i < N; i++) {
    const m = -span + 2 * span * i / (N - 1); xs.push(+(m * 100).toFixed(2));
    strad.push(+(prem * (Math.abs(m) / beFrac - 1)).toFixed(1));
    withScalp.push(+(prem * (Math.abs(m) / beFrac - 1) + a.scalp_mean).toFixed(1));
  }
  const ymin = Math.min(...strad), ymax = Math.max(...withScalp);
  const beNetPct = beNetFrac * 100;
  const band = (x0, x1, color) => ({ type: "rect", xref: "x", yref: "paper", x0, x1, y0: 0, y1: 1, fillcolor: color, line: { width: 0 }, layer: "below" });
  const shapes = [
    band(-span * 100, -beNetPct, "rgba(63,185,80,0.10)"), band(beNetPct, span * 100, "rgba(63,185,80,0.10)"),
    band(-beNetPct, beNetPct, "rgba(248,81,73,0.12)"),
    { type: "line", x0: -beNetPct, x1: -beNetPct, y0: ymin, y1: ymax, line: { color: "#3fb950", width: 1, dash: "dash" } },
    { type: "line", x0: beNetPct, x1: beNetPct, y0: ymin, y1: ymax, line: { color: "#3fb950", width: 1, dash: "dash" } },
    { type: "line", x0: a.avg_move_pct, x1: a.avg_move_pct, y0: ymin, y1: ymax, line: { color: "#8b949e", width: 1, dash: "dot" } },
  ];
  const traces = [
    { x: xs, y: xs.map(() => 0), mode: "lines", line: { color: "#3a4452", width: 1, dash: "dot" }, hoverinfo: "skip", showlegend: false },
    { x: xs, y: strad, mode: "lines", name: "среднее ядро (стреддл)", line: { color: "#58a6ff", width: 2.4 } },
    { x: xs, y: withScalp, mode: "lines", name: "+ средний скальп", line: { color: "#d29922", width: 2, dash: "dash" } },
    { x: [a.avg_move_pct], y: [a.total_mean], mode: "markers+text", name: "★ среднее ИТОГО",
      marker: { color: a.total_mean >= 0 ? "#3fb950" : "#f85149", size: 17, symbol: "star", line: { color: "#0f1419", width: 1 } },
      text: [`  среднее ${money(a.total_mean)}/период`], textposition: "middle right", textfont: { size: 12, color: a.total_mean >= 0 ? "#3fb950" : "#f85149" } },
  ];
  await plot("sim-periods-payoff", traces, layout(
    `Средний период (${a.n}×${a.dte_days}дн): 🟢 прибыль / 🔴 убыток · ★ = среднее ИТОГО`,
    { shapes, height: 360, xaxis: { gridcolor: "#2a3340", title: "ход за период, %", zeroline: true }, yaxis: { gridcolor: "#2a3340", title: "P&L, $" } }));

  // equity curve: cumulative deposit + Σ results — flat (always 10%) vs antimartingale
  const eqTraces = [{ x: am.dates, y: am.flat_equity, mode: "lines", name: "база 10% (эквити)", line: { color: "#58a6ff", width: 2 } }];
  if (amOn) eqTraces.push({ x: am.dates, y: am.am_equity, mode: "lines", name: `антимартингейл (cap ×${am.cap_mult})`, line: { color: "#d29922", width: 2 } });
  await plot("sim-periods-equity", eqTraces, layout(
    amOn ? `Эквити: база vs антимартингейл (${am.flat_ann_return_pct}% vs ${am.am_ann_return_pct}%/год)` : `Эквити (база 10%) — ${am.flat_ann_return_pct}%/год`,
    { height: 360, shapes: [{ type: "line", x0: am.dates[0], x1: am.dates[am.dates.length - 1], y0: a.deposit, y1: a.deposit, line: { color: "#8b949e", width: 1, dash: "dot" } }],
      xaxis: { gridcolor: "#2a3340" }, yaxis: { gridcolor: "#2a3340", title: "эквити, $" } }));

  // distribution of per-period TOTAL
  await plot("sim-periods-hist", [{ x: rows.map((r) => amOn ? r.am_total : r.total), type: "histogram", nbinsx: 40, marker: { color: amOn ? "#d29922" : "#58a6ff" } }],
    layout(amOn ? "Распределение ИТОГО по периодам (антимарт.), $" : "Распределение ИТОГО по периодам, $", { height: 360, shapes: [{ type: "line", x0: 0, x1: 0, y0: 0, y1: 1, yref: "paper", line: { color: "#8b949e", width: 1, dash: "dot" } }],
      xaxis: { gridcolor: "#2a3340", title: "ИТОГО за период, $" }, yaxis: { gridcolor: "#2a3340", title: "периодов" } }));

  renderTable("sim-periods-table", rows.map((r) => {
    const row = {
      "#": r.i, "вход": r.open, "экспир": r.close, "S0": r.S0, "S_T": r.S_T, "ход %": r.move_pct,
      "б/у %": r.breakeven_pct, "стреддл $": Math.round(r.straddle), "скальп-осц $": Math.round(r.scalp_osc),
      "залип $": Math.round(r.stuck), "скальп $": Math.round(r.scalp), "ИТОГО $": Math.round(r.total),
    };
    if (amOn) { row["AM ×"] = r.am_mult; row["AM ИТОГО $"] = Math.round(r.am_total); }
    row.outcome = r.outcome;
    return row;
  }));
}

// ---- Tab 14 scan: edge across ALL instruments + the pooled straddle-core distribution
async function renderPiSimScan(d) {
  const money = (x) => (x >= 0 ? "+$" : "−$") + Math.abs(Math.round(x)).toLocaleString();
  const a = d.aggregate;
  $("#sim-scan").hidden = false; $("#sim-periods").hidden = true;
  $("#sim-verdict").textContent = ""; $("#sim-steps").innerHTML = "";
  $("#sim-stats").textContent = ""; $("#sim-grid").innerHTML = "";
  Plotly.purge("sim-chart"); Plotly.purge("sim-payoff");

  // verdict text — lead with the ROBUST median + the real-IV subset
  $("#sim-scan-stats").textContent =
    `🏆 EDGE по каталогу — НЕпересекающиеся месяцы с ${a.scan_start}, депозит $${a.deposit.toLocaleString()}, риск ${(a.risk_pct*100).toFixed(0)}% (тета≈$${(a.risk_pct*a.deposit).toLocaleString()}/мес)\n`
    + `Инструментов надёжных: ${a.n}  (из них с РЕАЛЬНОЙ IV — SPY/QQQ/BTC/ETH: ${a.n_real_iv})  ·  IV-артефактов исключено: ${a.n_artifact}\n`
    + `\n── СТРЕДДЛ-ЯДРО (реально, без допущений: цены + IV) ──\n`
    + `Медиана ядра: ${money(a.median_core_mean)}/мес (по надёжным)  ·  только real-IV: ${money(a.median_core_real_iv)}/мес\n`
    + `Месяцев с RV>IV (edge БЕЗ скальпа): ${a.n_core_edge}/${a.n}  ·  пул месяцев: медиана ${money(a.pooled_core_median)}, среднее ${money(a.pooled_core_mean)}, выигрышных ${a.pooled_core_win_pct}%\n`
    + `  ⇒ ЧИСТО длинный стреддл в МЕДИАНЕ ${a.median_core_mean < 0 ? "КРОВОТОЧИТ (VRP: IV≥RV)" : "в плюсе"}; среднее тянет вверх редкий выпуклый хвост (см. гистограмму).\n`
    + `\n── + СКАЛЬП-ЯКОРЬ ${(a.coverage_anchor*100).toFixed(0)}% теты ──\n`
    + `Медиана total: ${money(a.median_total_mean)}/мес  ·  только real-IV: ${money(a.median_total_real_iv)}/мес\n`
    + `Инструментов +EV при якоре ${(a.coverage_anchor*100).toFixed(0)}%: ${a.n_total_edge}/${a.n}  ·  с edge при РЕАЛИСТИЧНОМ скальпе (c*≤20%): ${a.n_realistic}/${a.n}\n`
    + `\n💡 ВЫВОД: длинный стреддл сам по себе edge НЕ даёт (медиана кровоточит — премия за риск воли). Edge есть там, где (а) RV>IV `
    + `(крипта ETH/BTC) ИЛИ (б) скальп реально кроет тету (c* мал). Скальп честно измерим только на крипте (1-мин) — на остальном это допущение.`;

  // BAR: per-instrument core $/mo (real edge) — reliable only, diverging colours
  const rel = d.rows.filter((r) => r.reliable).slice().sort((x, y) => x.core_mean - y.core_mean);
  await plot("sim-scan-bar", [{
    x: rel.map((r) => r.core_mean), y: rel.map((r) => r.ticker), type: "bar", orientation: "h",
    marker: { color: rel.map((r) => r.core_mean >= 0 ? "#3fb950" : "#f85149") },
    text: rel.map((r) => r.iv_is_real ? "●" : ""), textposition: "outside",
    hovertext: rel.map((r) => `${r.ticker} ${r.label}: ядро ${money(r.core_mean)}/мес, total ${money(r.total_mean)}, c*=${r.c_star}, ${r.verdict}`),
    hoverinfo: "text",
  }], layout("Стреддл-ЯДРО по инструментам, $/мес (РЕАЛЬНЫЙ edge; ● = настоящая IV)", {
    height: Math.max(380, rel.length * 16), margin: { l: 76, t: 36, r: 30, b: 36 },
    yaxis: { gridcolor: "#2a3340", automargin: true, tickfont: { size: 9 } },
    xaxis: { gridcolor: "#2a3340", title: "$/мес (>0 = ядро бьёт тету)", zeroline: true, zerolinecolor: "#8b949e" },
  }));

  // HIST: pooled monthly straddle-core P&L — the "average straddle" shape (asymmetric bleed + convex tail)
  const pc = (d.pooled_core || []).filter((x) => x > -1e6 && x < 1e6);
  await plot("sim-scan-hist", [{
    x: pc, type: "histogram", nbinsx: 60, marker: { color: "#58a6ff" }, name: "месяцы",
  }], layout("Распределение МЕСЯЧНОГО P&L стреддла-ядра (все надёжные месяцы)", {
    height: 380, shapes: [
      { type: "line", x0: 0, x1: 0, y0: 0, y1: 1, yref: "paper", line: { color: "#8b949e", width: 1, dash: "dot" } },
      { type: "line", x0: a.pooled_core_median, x1: a.pooled_core_median, y0: 0, y1: 1, yref: "paper", line: { color: "#d29922", width: 1.5 } },
    ],
    annotations: [{ x: a.pooled_core_median, y: 1, yref: "paper", text: `медиана ${money(a.pooled_core_median)}`, showarrow: false, font: { size: 10, color: "#d29922" }, yanchor: "bottom" }],
    xaxis: { gridcolor: "#2a3340", title: "P&L стреддла за месяц, $ (премия = −потолок убытка)" }, yaxis: { gridcolor: "#2a3340", title: "месяцев" },
  }));

  // TABLE: full ranking
  renderTable("sim-scan-table", d.rows.map((r) => ({
    "инстр": r.ticker, "группа": (r.group || "").slice(0, 16), "мес": r.n_months,
    "ядро $/мес": Math.round(r.core_mean), "ядро win%": Math.round(r.core_win_pct),
    "скальп $/мес": Math.round(r.scalp_mean), "total $/мес": Math.round(r.total_mean),
    "total win%": Math.round(r.total_win_pct), "c*": r.c_star, "RV/IV": r.rv_over_iv,
    "годовых%": Math.round(r.ann_return_pct), "real-IV": r.iv_is_real ? "✓" : "",
    "вердикт": r.reliable ? r.verdict : "⚠ IV-артефакт (исключён)",
  })));
}

// payoff-at-expiry: the clean straddle V vs the scalp-futures-TILTED («перекошенный») V
async function renderSimPayoff(d, asset) {
  const p = d.payoff; if (!p || !p.S) return;
  const zero = p.S.map(() => 0);
  const traces = [
    { x: p.S, y: zero, type: "scatter", mode: "lines", name: "0", line: { color: "#3a4452", width: 1, dash: "dot" }, hoverinfo: "skip" },
    { x: p.S, y: p.straddle, type: "scatter", mode: "lines", name: "чистый стреддл (2C−1F, симметрично)",
      line: { color: "#58a6ff", width: 2.4 } },
  ];
  if (p.mode === "actual") {
    const dir = p.q < 0 ? `нетто ШОРТ ${Math.abs(p.q).toFixed(2)} ${asset}` : (p.q > 0 ? `нетто ЛОНГ ${p.q.toFixed(2)} ${asset}` : "нейтрально");
    traces.push({ x: p.S, y: p.tilted, type: "scatter", mode: "lines",
      name: `+ скальп-фьючи (${dir}) → перекос`, line: { color: "#d29922", width: 2.4 } });
  } else {
    traces.push({ x: p.S, y: p.tilt_short, type: "scatter", mode: "lines", name: "перекос если скальп нетто-ШОРТ (полный лимит)",
      line: { color: "#f85149", width: 1.6, dash: "dash" } });
    traces.push({ x: p.S, y: p.tilt_long, type: "scatter", mode: "lines", name: "перекос если скальп нетто-ЛОНГ (полный лимит)",
      line: { color: "#3fb950", width: 1.6, dash: "dash" } });
  }
  // ★ WHERE WE ENDED UP: the actual result point at (S_T, total P&L = straddle core + scalp)
  const resColor = d.total_net >= 0 ? "#3fb950" : "#f85149";
  traces.push({ x: [d.S_T], y: [d.total_net], type: "scatter", mode: "markers+text",
    name: "★ ИТОГ (где мы оказались)", marker: { color: resColor, size: 17, symbol: "star", line: { color: "#0f1419", width: 1 } },
    text: [`  ИТОГ ${d.total_net >= 0 ? "+$" : "−$"}${Math.abs(Math.round(d.total_net)).toLocaleString()} (${d.total_pct >= 0 ? "+" : ""}${d.total_pct.toFixed(1)}%)`],
    textposition: "middle right", textfont: { size: 12, color: resColor },
    hovertext: [`S_T=$${Math.round(d.S_T).toLocaleString()} · ядро ${d.straddle_net >= 0 ? "+" : "−"}$${Math.abs(Math.round(d.straddle_net))} + скальп ${d.scalp_income >= 0 ? "+" : "−"}$${Math.abs(Math.round(d.scalp_income))} = ${d.total_net >= 0 ? "+" : "−"}$${Math.abs(Math.round(d.total_net))}`], hoverinfo: "text" });
  // a small hollow marker on the straddle curve itself = the gamma-core result (before scalp)
  traces.push({ x: [d.S_T], y: [d.straddle_net], type: "scatter", mode: "markers", name: "стреддл-ядро в S_T",
    marker: { color: "#58a6ff", size: 9, symbol: "circle-open", line: { width: 2 } },
    hovertext: [`ядро (только гамма): ${d.straddle_net >= 0 ? "+" : "−"}$${Math.abs(Math.round(d.straddle_net))}`], hoverinfo: "text" });
  const ymin = Math.min(...p.straddle, ...(p.tilt_short || p.tilted || [0]), d.total_net);
  const ymax = Math.max(...p.straddle, ...(p.tilt_long || p.tilted || [0]), d.total_net);
  const xmin = p.S[0], xmax = p.S[p.S.length - 1];
  // breakevens: the straddle ALONE breaks even at S0 ± premium/M; WITH the scalp income the V shifts up,
  // so the loss zone NARROWS to S0 ± (premium − scalp)/M (scalp ≥ premium ⇒ always green).
  const beStr = d.breakeven_move;                                   // = premium/M (straddle alone)
  const beNet = Math.max(0, (d.premium_budget - d.scalp_income) / d.straddle_units);
  const zoneEdge = beNet;                                           // shade by the FULL position (with scalp)
  const lossLo = p.S0 - zoneEdge, lossHi = p.S0 + zoneEdge;
  const band = (x0, x1, color) => ({ type: "rect", xref: "x", yref: "paper", x0, x1, y0: 0, y1: 1,
    fillcolor: color, line: { width: 0 }, layer: "below" });
  const shapes = [
    band(xmin, lossLo, "rgba(63,185,80,0.10)"),                     // left wing = profit
    band(lossHi, xmax, "rgba(63,185,80,0.10)"),                     // right wing = profit
    band(lossLo, lossHi, "rgba(248,81,73,0.12)"),                   // middle = loss
    { type: "line", x0: lossLo, x1: lossLo, y0: ymin, y1: ymax, line: { color: "#3fb950", width: 1.2, dash: "dash" } },
    { type: "line", x0: lossHi, x1: lossHi, y0: ymin, y1: ymax, line: { color: "#3fb950", width: 1.2, dash: "dash" } },
    { type: "line", x0: p.S0, x1: p.S0, y0: ymin, y1: ymax, line: { color: "#8b949e", width: 1, dash: "dot" } },
    { type: "line", x0: p.S_T, x1: p.S_T, y0: ymin, y1: ymax, line: { color: d.S_T >= d.S0 ? "#3fb950" : "#f85149", width: 1.6 } },
  ];
  // straddle-alone breakevens (lighter) — so you see how much the scalp narrowed the loss zone
  if (Math.abs(beStr - beNet) > 1e-6) {
    shapes.push({ type: "line", x0: p.S0 - beStr, x1: p.S0 - beStr, y0: ymin, y1: ymax, line: { color: "#8b949e", width: 0.8, dash: "dot" } });
    shapes.push({ type: "line", x0: p.S0 + beStr, x1: p.S0 + beStr, y0: ymin, y1: ymax, line: { color: "#8b949e", width: 0.8, dash: "dot" } });
  }
  const beNetPct = (zoneEdge / p.S0 * 100);
  const anns = [
    { x: p.S0, y: ymax, text: "K (вход)", showarrow: false, font: { size: 10, color: "#8b949e" }, yanchor: "bottom" },
    { x: p.S_T, y: ymax, text: `S_T (${d.move_pct >= 0 ? "+" : ""}${d.move_pct.toFixed(1)}%)`, showarrow: false, font: { size: 10, color: d.S_T >= d.S0 ? "#3fb950" : "#f85149" }, yanchor: "bottom" },
    { x: (xmin + lossLo) / 2, y: 0.96, yref: "paper", text: "🟢 ПРИБЫЛЬ", showarrow: false, font: { size: 11, color: "#3fb950" } },
    { x: (lossHi + xmax) / 2, y: 0.96, yref: "paper", text: "🟢 ПРИБЫЛЬ", showarrow: false, font: { size: 11, color: "#3fb950" } },
    { x: p.S0, y: 0.06, yref: "paper", text: `🔴 УБЫТОК (б/у ±${beNetPct.toFixed(1)}% с учётом скальпа)`, showarrow: false, font: { size: 10, color: "#f85149" } },
  ];
  await plot("sim-payoff", traces, layout(
    "Выплата на экспирации: 🟢 прибыль на крыльях / 🔴 убыток в центре (зелёные пунктиры = безубыток с учётом скальпа; серые точки = безубыток стреддла без скальпа)",
    { shapes, annotations: anns, height: 380, xaxis: { gridcolor: "#2a3340", title: `${asset} на экспирации, $` },
      yaxis: { gridcolor: "#2a3340", title: "P&L, $" } }));
}

// ---- Tab 14: «Симуляция в деньгах» — one ПИ construction, one period, every figure in dollars
async function renderPiSim(d) {
  const money = (x) => (x >= 0 ? "+$" : "−$") + Math.abs(Math.round(x)).toLocaleString();
  const asset = (d.ticker || "").split("-")[0] || d.ticker;
  // verdict banner
  $("#sim-verdict").textContent = d.verdict;
  $("#sim-verdict").style.color = d.total_net >= 0 ? "#3fb950" : "#f85149";

  // step cards
  $("#sim-steps").innerHTML = (d.steps || []).map((s) =>
    `<div class="sim-step"><div class="sim-step-h"><span class="sim-step-n">${s.n}</span>${s.title}</div>`
    + `<div class="sim-step-b">${s.body}</div></div>`).join("");

  // price chart with entry/expiry markers + the 5 working-part grid levels
  const tl = d.timeline || { dates: [], close: [] };
  const traces = [{
    x: tl.dates, y: tl.close, type: "scatter", mode: "lines", name: `${asset} цена`,
    line: { color: "#58a6ff", width: 2 },
  }];
  const shapes = [], anns = [];
  const x0 = tl.dates[0], x1 = tl.dates[tl.dates.length - 1];
  shapes.push({ type: "line", x0, x1, y0: d.S0, y1: d.S0, line: { color: "#8b949e", width: 1, dash: "dot" } });
  (d.grid || []).forEach((g) => {
    shapes.push({ type: "line", x0, x1, y0: g.sell, y1: g.sell, line: { color: "#f85149", width: 0.7, dash: "dot" } });
    shapes.push({ type: "line", x0, x1, y0: g.buy, y1: g.buy, line: { color: "#3fb950", width: 0.7, dash: "dot" } });
  });
  traces.push({ x: [x0], y: [d.S0], type: "scatter", mode: "markers", name: "вход", marker: { color: "#e6edf3", size: 9, symbol: "circle" } });
  traces.push({ x: [x1], y: [d.S_T], type: "scatter", mode: "markers", name: "экспирация", marker: { color: d.S_T >= d.S0 ? "#3fb950" : "#f85149", size: 11, symbol: "diamond" } });
  await plot("sim-chart", traces, layout(
    `${d.ticker}: ${d.entry_date} ($${Math.round(d.S0).toLocaleString()}) → ${d.expiry_date} ($${Math.round(d.S_T).toLocaleString()}, ${d.move_pct >= 0 ? "+" : ""}${d.move_pct.toFixed(1)}%) · красным=продажи, зелёным=покупки сетки`,
    { shapes, annotations: anns, height: 380 }));

  // payoff diagram — the scalp-futures TILT on the symmetric straddle V
  await renderSimPayoff(d, asset);

  // money breakdown
  const cov = (d.coverage * 100).toFixed(0);
  let scalpLine;
  const ch = d.chop || {}, dg = d.chop_diag || {};
  const chopLine = `  🌊 АДАПТИВНАЯ ЧОП-МОДЕЛЬ (net рабочих частей):\n`
    + `     осцилляция в чопе: ${money(ch.income_effective)}  (${(ch.trades_per_day || 10)} сд/день × ${((ch.eff || 0.5) * 100).toFixed(0)}% хода, TP $${(ch.tp || 0).toFixed(2)} = ${((ch.flat_frac || 0) * 100).toFixed(0)}% диапазона, ИЗМЕРЕНО чоп ${((dg.chop_frac ?? 0) * 100).toFixed(0)}% дней)\n`
    + `     − залипшие рабочие части: ${money(ch.stuck_used)}  ${(ch.stuck_used >= -1) ? "(flat-гейт сдержал — не фейдим пробой)" : "(стянуты трендом)"}  [фикс-сетка без гейта была бы ${money(ch.stuck_fixed)}]\n`
    + `     = НЕТТО скальп: ${money(ch.net)} = ${(d.coverage * 100).toFixed(0)}% теты`
    + (dg.path_over_range != null ? `;  путь ×${dg.path_over_range} диапазона, нужно $${(ch.path_needed_per_day || 0).toFixed(1)}/день → ${ch.feasible ? `✅ ${(ch.trades_per_day||10)} сд/день достижимо (×${ch.path_headroom})` : (dg.is_daily ? "достижимость без 1-мин не проверить" : "путь 60-мин груб")}` : ``)
    + `\n`;
  if (d.scalp_source === "1m-measured") {
    const bcov = (Math.max(d.scalp_realized, 0) / d.theta_cost * 100).toFixed(0);
    const gap = (ch.income || 0) - d.scalp_income;
    scalpLine = `── ФИКСИРОВАННАЯ сетка (РЕАЛЬНЫЙ 1-мин замер этого месяца) ──\n`
      + `скальп booked 1м   : ${money(d.scalp_realized)}  (= ${bcov}% теты, ${d.scalp_round_trips} закрытых кругов)\n`
      + `скальп залипшие    : ${money(d.scalp_open_mtm)}  (контр-тренд части; на тренде в минус, гамма кроет)\n`
      + `фикс-скальп ИТОГО  : ${money(d.scalp_income)}   ← в общий ИТОГ (что реально сделала «поставил-и-забыл» сетка)\n`
      + `── vs АДАПТИВНО (ре-центрируем по чопу — know-how трейдера) ──\n`
      + chopLine
      + `     ⟶ разница ${money(gap)} = цена ручной подстройки (адаптивный не залипает на тренде)`;
  } else {
    const floorLine = (d.scalp_floor != null)
      ? `  ├ пол (замер 60-мин, недооценка мелкой сетки): ${money(d.scalp_floor)}\n` : "";
    scalpLine = `скальп — ОЦЕНКА ПОЛОСОЙ (нет free 1-мин фида ⇒ не измеряется напрямую):\n`
      + floorLine
      + chopLine.replace("\n", "  ← в ИТОГ\n")
      + `  └ потолок (оптимизм, захват ${(d.scalp_capture * 100).toFixed(0)}% дн.хода на всём лимите): ${money(d.scalp_scenario)}\n`
      + `скальп (вклад в итог): ${money(d.scalp_income)}  = ${cov}% теты`;
  }
  $("#sim-stats").textContent =
    `═══ ВХОД (${d.entry_date}) ═══\n`
    + `${asset} спот        : $${d.S0.toLocaleString(undefined, { maximumFractionDigits: 2 })}   ATM-страйк K = $${d.K.toLocaleString(undefined, { maximumFractionDigits: 2 })}\n`
    + `IV (реальная)      : ${(d.iv * 100).toFixed(1)}%   ·   Колл = $${d.call_price.toFixed(2)}   Пут = $${d.put_price.toFixed(2)}\n`
    + `1 ед. стреддла 2C−1F = $${d.straddle_unit_cost.toFixed(2)}   ·   безубыток движения = ±${d.breakeven_pct.toFixed(1)}%\n`
    + `\n═══ ЧТО КУПИТЬ (бюджет = ${(d.risk_pct * 100).toFixed(0)}% от $${d.deposit.toLocaleString()} = $${d.premium_budget.toLocaleString()}) ═══\n`
    + `КУПИТЬ Коллов      : ${d.n_calls.toFixed(3)} ${asset}\n`
    + `ПРОДАТЬ Фьючерсов  : ${d.n_futures.toFixed(3)} ${asset}  (дельта-нейтральная база)\n`
    + `Премия (= МАКС риск): $${d.premium_budget.toLocaleString()}\n`
    + `\n═══ ТРИ ТРЕТИ → СКАЛЬП ═══\n`
    + `лимит на интрадей  : ${d.intraday_limit_lots.toFixed(3)} ${asset}  (⅓ коллов), ${d.n_parts} частей × ${d.part_lots.toFixed(3)}\n`
    + `первый шаг сетки   : $${d.first_step.toFixed(2)}  (×${d.grid_mult} экспон.)   ·   дневной ATR = $${d.daily_atr.toFixed(2)}\n`
    + `\n═══ ИТОГ ЗА ${d.n_days} ДН ═══\n`
    + `стреддл-ядро       : ${money(d.straddle_net)}  (${d.straddle_units.toFixed(2)} ед × $${Math.round(d.move_abs).toLocaleString()} интринсик − $${d.premium_budget.toLocaleString()} премии)\n`
    + scalpLine + `\n`
    + `───────────────────────────────\n`
    + `ИТОГО              : ${money(d.total_net)}   (${d.total_pct >= 0 ? "+" : ""}${d.total_pct.toFixed(1)}% депозита)\n`
    + (d.scalp_source === "1m-measured"
      ? `\n✓ скальп ИЗМЕРЕН по реальному 1-мин пути Binance (крипта). Залипшие части на тренде — по доктрине (гамма их кроет).`
      : `\n⚠ скальп НЕ измеряется без free 1-мин фида: показан полосой, в итог взят РЕАЛИСТИЧНЫЙ якорь (консервативно). `
        + `Для реального замера возьми крипту (BTC/ETH/SOL). Покрытие — vol-инвариант (INV #7): круги/мес × захват, не воля.`);

  // grid table (real prices)
  renderTable("sim-grid", (d.grid || []).map((g) => ({
    "часть": g.part, "оффсет $": g.offset,
    "ПРОДАТЬ на $": g.sell, "КУПИТЬ на $": g.buy, "лотов": g.lots,
  })));
}
$("#form-pisim").onsubmit = (e) => {
  e.preventDefault();
  withBusy(e.submitter, async () => {
    const o = formData(e.target);
    $("#sim-scan").hidden = true; $("#sim-periods").hidden = true;   // single run → hide scan/periods
    if (o.use_1m === "true" && isCryptoTicker(o.ticker))
      toast("Меряю скальп по реальному 1-мин пути Binance (первый прогон тянет историю)…", true);
    renderPiSim(await post("/api/pi-sim", o));
  });
};
// one instrument's whole history as a table of DTE periods + the average-period payoff
$("#pisim-periods").onclick = (e) => withBusy(e.target, async () => {
  const o = formData($("#form-pisim"));
  toast(`Катаю всю историю ${o.ticker} по периодам ${o.dte_days}дн…`, true);
  renderPiSimPeriods(await post("/api/pi-sim/periods", o));
});
// one-click: edge across the WHOLE catalog (rolling months, real core + anchor scalp)
$("#pisim-scan-all").onclick = (e) => withBusy(e.target, async () => {
  toast("Скан каталога: катаю месяцы по всем инструментам (первый прогон ~1–2 мин)…", true);
  const o = formData($("#form-pisim")); o.scan = "true";
  renderPiSimScan(await post("/api/pi-sim/scan", o));
});

loadInstruments();
window.addEventListener("load", () => {
  if (typeof Plotly === "undefined")
    toast("Charts library (Plotly) did not load from /vendor/plotly.min.js — charts will not render.");
});
