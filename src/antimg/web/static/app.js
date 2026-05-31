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
  } catch (e) { console.error(e); }
}

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
  const cols = [
    ["ticker", "ticker"], ["group", "group"], ["n_campaigns", "camps"],
    ["wins", "W"], ["losses", "L"], ["net", "net $"], ["ret_pct", "return %"],
    ["profit_factor", "PF"], ["max_drawdown", "max DD"],
  ];
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  const head = "<tr>" + cols.map(([k, lbl]) =>
    `<th data-sort="${k}" style="cursor:pointer">${lbl}${_scan.sort === k ? (_scan.desc ? " ▼" : " ▲") : ""}</th>`).join("") + "</tr>";
  const body = rows.map((r) => {
    if (!r.ok) return `<tr class="l"><td>${r.ticker}</td><td>${r.group}</td>`
      + `<td colspan="7" style="color:#8b949e">⚠ ${r.error}</td></tr>`;
    const cls = r.net > 0 ? "w" : "l";
    return `<tr class="${cls}"><td>${r.ticker}</td><td>${r.group}</td>`
      + `<td>${r.n_campaigns}</td><td>${r.wins}</td><td>${r.losses}</td>`
      + `<td>${f(r.net)}</td><td>${f(r.ret_pct)}</td><td>${r.profit_factor == null ? "∞" : f(r.profit_factor)}</td>`
      + `<td>${f(r.max_drawdown)}</td></tr>`;
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
  const verdict =
    `${s.profitable_pct >= 50 ? "✅ BROADLY ROBUST" : "⚠ NARROW / FRAGILE"}   `
    + `${s.profitable}/${s.ok} instruments profitable (${f(s.profitable_pct)}%)\n`
    + `median return : ${f(s.median_ret_pct)}%   ·   mean return : ${f(s.mean_ret_pct)}%\n`
    + (s.best ? `best  : ${s.best.ticker}  ${f(s.best.ret_pct)}%  (net ${f(s.best.net)})\n` : "")
    + (s.worst ? `worst : ${s.worst.ticker}  ${f(s.worst.ret_pct)}%  (net ${f(s.worst.net)})\n` : "")
    + (s.failed ? `failed to fetch : ${s.failed}\n` : "")
    + `\n(mean ≫ median ⇒ a few outliers carry the result — the strategy is NOT broadly sound,\n`
    + ` it depends on rare large wins. Median near/below 0 confirms most instruments lose.)`;
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
async function renderExplain(d) {
  const b = d.b, isCalls = d.instrument === "calls";
  const f = (v) => (v == null ? "—" : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  const adds = d.trace.filter((t) => t.t === "add");
  const optAdds = d.trace.filter((t) => t.t === "opt_add");
  const optMarks = d.trace.filter((t) => t.t === "opt_mark");
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
  if (isCalls && optMarks.length) {     // overlay option-stack unrealised P&L on the right axis
    traces.push({ x: optMarks.map((m) => m.date), y: optMarks.map((m) => m.unreal), mode: "lines",
                  name: "нереализ. P&L опциона", yaxis: "y2",
                  line: { color: "#f0c000", width: 1.5 } });
    lay.yaxis2 = { overlaying: "y", side: "right", title: { text: "$ P&L опциона" },
                   gridcolor: "transparent", zeroline: false };
  } else {                              // share rung labels (no 2nd axis to collide with)
    lay.annotations = d.rungs.map((r) => ({
      xref: "paper", x: 1, y: r.level, yref: "y", xanchor: "left",
      text: r.k === 0 ? " вход R0" : ` +${r.k}·h`, showarrow: false,
      font: { size: 9, color: r.k === 0 ? "#5b9dff" : "#8b949e" } }));
  }
  await plot("expl-chart", traces, lay);

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
    if (optExit) {
      L.push(`\n${won ? "ЦЕЛЬ" : "СТОП/выход"}: закрытие по ${f(optExit.premium_per)}/ед × ${f(optExit.contracts)} = $${f(optExit.stack_value)}; премии вложено $${f(optExit.premium_book)}.`);
      L.push(`БРУТТО опциона = $${f(optExit.gross)}${won ? ` = ${f(optExit.gross / b)}×b` : ""}. ${won ? `Больше акций (${f(d.table[0] ? d.table[0].gross : 0)} было бы линейно) за счёт гаммы.` : "Выпуклость смягчает убыток vs −b у акций."}`);
    }
  }
  L.push(`\nИТОГ: проигрыши малы и часты, выигрыш серии — крупный, но требует раздувания капитала/премии (см. cap_mult, скан вкладки 5).`);
  $("#expl-stats").textContent = L.join("\n");

  // --- money ledger table ---
  const led = $("#expl-ledger");
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
    rows = [...optAdds, optExit].filter(Boolean);
    note = "опционный леджер · контрактов=(b/h)/Δ на каждом доливе · цена закрытия в строке выхода";
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

$("#form-explain").onsubmit = (e) => {
  e.preventDefault();
  withBusy(e.submitter, async () =>
    renderExplain(await post("/api/explain", formData(e.target))));
};

loadInstruments();
window.addEventListener("load", () => {
  if (typeof Plotly === "undefined")
    toast("Charts library (Plotly) did not load from /vendor/plotly.min.js — charts will not render.");
});
