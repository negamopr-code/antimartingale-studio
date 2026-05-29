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
  await Plotly.react(el, traces, lay, { responsive: true, displaylogo: false });
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
    const hist = Object.entries(d.series_counter).map(([k, v]) => `  ${k.padStart(3)} wins: ${v}`).join("\n");
    $("#cf-stats").textContent = statsText(d.stats) + "\n\ncycles ending at streak:\n" + hist;
    await plot("cf-bank", [{ x: d.history.x, y: d.history.y, mode: "lines", line: { width: 1, color: "#5b9dff" } }],
      layout("Cumulative bank"));
    await plot("cf-streak", [{ x: d.last_series.x, y: d.last_series.y, mode: "lines", line: { color: "#3fb950" } }],
      layout("Last winning streak"));
  });
};

// ---- tab 2 / 3 shared
async function renderBacktest(prefix, d, isOptions) {
  const price = { x: d.price.x, y: d.price.y, mode: "lines", name: "Close",
                  line: { width: 1, color: "#c9d1d9" } };
  const win = { x: d.entries.win.x, y: d.entries.win.y, mode: "markers", name: "win",
                marker: { color: "#3fb950", symbol: "triangle-up", size: 7 } };
  const loss = { x: d.entries.loss.x, y: d.entries.loss.y, mode: "markers", name: "loss",
                 marker: { color: "#f85149", symbol: "triangle-down", size: 7 } };
  const traces = [price, win, loss];
  const lay = layout("Price + entries", {
    height: 460,
    xaxis: {
      gridcolor: "#2a3340",
      rangeslider: { visible: true, thickness: 0.09, bgcolor: "#10151c" },
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
  let extra = "";
  if (s.cost_as_prob != null) {
    const edge = s.empirical_p - 0.5;
    const ok = edge >= s.cost_as_prob;
    extra = "\n— cost as win-prob drag (Δp) —\n"
      + `commission Δp : ${f(s.commission_as_prob)}\n`
      + `slippage   Δp : ${f(s.slippage_as_prob)}\n`
      + `TOTAL      Δp : ${f(s.cost_as_prob)}\n`
      + `breakeven p*  : ${f(s.breakeven_p_with_cost)}\n`
      + `your edge p-.5: ${f(edge)}\n`
      + (ok ? "✓ edge still covers costs → net +EV"
            : "✗ costs exceed edge → net −EV");
  }
  $(`#${prefix}-stats`).textContent = statsText(s) + extra;

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

loadInstruments();
window.addEventListener("load", () => {
  if (typeof Plotly === "undefined")
    toast("Charts library (Plotly) did not load from /vendor/plotly.min.js — charts will not render.");
});
