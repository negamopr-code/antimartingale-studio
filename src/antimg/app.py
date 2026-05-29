"""Tkinter GUI — three tabs (ttk.Notebook):

  Tab 1  Coin-flip      : the user's original pyramid simulator, improved.
  Tab 2  ATR backtest   : weekly-entry/daily-resolution antimartingale on a real asset,
                          ATR auto-computed, commissions & slippage, equity + entries.
  Tab 3  Options        : same strategy via a modeled deep-ITM call, Black-Scholes delta
                          auto-computed and plotted, configurable DTE.

Headless note: tkinter is not installed in the dev container — run this on a host with
python3-tk. The math modules (simcore/data/atr_strategy/options) are testable headless.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

from . import atr_strategy as strat
from . import data as datamod
from . import instruments
from .simcore import Simulation, expected_trades_per_cycle

FONT = ("Arial", 12)
MONO = ("Courier New", 11)


def _labeled_entry(parent, text, default, width=8, row=0, col=0):
    ttk.Label(parent, text=text).grid(row=row, column=col, sticky="e", padx=4, pady=3)
    e = ttk.Entry(parent, width=width)
    e.insert(0, default)
    e.grid(row=row, column=col + 1, sticky="w", padx=4, pady=3)
    return e


# --------------------------------------------------------------------------- Tab 1
class CoinFlipTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.sim = Simulation()
        ctrl = ttk.Frame(self)
        ctrl.pack(fill=tk.X, padx=10, pady=8)

        self.e_iter = _labeled_entry(ctrl, "Iterations", "100000", row=0, col=0)
        self.e_target = _labeled_entry(ctrl, "Target streak", "10", row=0, col=2)
        self.e_bet = _labeled_entry(ctrl, "Base bet", "1", row=0, col=4)
        self.e_p = _labeled_entry(ctrl, "Win prob", "0.5", row=0, col=6)
        self.e_seed = _labeled_entry(ctrl, "Seed (blank=rnd)", "", row=0, col=8)

        self.mode = tk.StringVar(value="separate")
        ttk.Radiobutton(ctrl, text="Separate", variable=self.mode, value="separate").grid(row=1, column=0, columnspan=2)
        ttk.Radiobutton(ctrl, text="Continuous", variable=self.mode, value="continuous").grid(row=1, column=2, columnspan=2)
        ttk.Button(ctrl, text="Run", command=self.run).grid(row=1, column=6, padx=6)
        ttk.Button(ctrl, text="Reset", command=self.reset).grid(row=1, column=8, padx=6)

        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True)
        self.fig, self.axs = plt.subplots(2, 1, figsize=(9, 6))
        self.fig.tight_layout(pad=3)
        self.canvas = FigureCanvasTkAgg(self.fig, master=body)
        self.canvas.get_tk_widget().pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.stats = tk.Text(body, width=42, font=MONO)
        self.stats.pack(side=tk.RIGHT, fill=tk.Y)

    def reset(self):
        self.sim.reset_all()
        for ax in self.axs:
            ax.clear()
        self.canvas.draw()
        self.stats.delete("1.0", tk.END)

    def run(self):
        try:
            it = int(self.e_iter.get()); N = int(self.e_target.get())
            b = float(self.e_bet.get()); p = float(self.e_p.get())
            seed = int(self.e_seed.get()) if self.e_seed.get().strip() else None
        except ValueError:
            messagebox.showerror("Input", "Enter valid numbers.")
            return
        res = self.sim.simulate(it, N, b, p, self.mode.get(), seed=seed)
        self.axs[0].clear(); self.axs[1].clear()
        self.axs[0].plot(res.history, lw=0.7)
        self.axs[0].set_title("Cumulative bank")
        self.axs[0].axhline(0, color="grey", lw=0.5)
        if res.last_series:
            self.axs[1].plot(res.last_series, color="green")
        self.axs[1].set_title("Last winning streak")
        self.fig.tight_layout(pad=3); self.canvas.draw()

        et = expected_trades_per_cycle(N, p)
        lines = [
            f"trials       : {res.total_iterations}",
            f"cycles       : {res.cycles}",
            f"successes(N) : {res.successes}",
            f"final bank   : {res.cumulative_bank:.2f}",
            "",
            f"E[cycle] theory : {res.closed_form_ev_cycle:+.4f}",
            f"E[cycle] empiri : {res.empirical_ev_cycle:+.4f}",
            f"E[trials/cycle] : {et:.3f}",
            f"E[trade] theory : {res.closed_form_ev_cycle/et:+.5f}" if et else "",
            "",
            "cycles ending at streak:",
        ]
        for k in sorted(res.series_counter):
            lines.append(f"  {k:>3} wins : {res.series_counter[k]}")
        self.stats.delete("1.0", tk.END)
        self.stats.insert(tk.END, "\n".join(lines))


# ---------------------------------------------------------------- shared asset controls
class AssetControls(ttk.Frame):
    def __init__(self, master, options_mode: bool):
        super().__init__(master)
        self.options_mode = options_mode
        self.columnconfigure(20, weight=1)

        ttk.Label(self, text="Instrument").grid(row=0, column=0, sticky="e", padx=4)
        self.cb = ttk.Combobox(self, width=42, values=[lbl for _, lbl in instruments.flat()])
        self.cb.set("SPY — S&P 500 ETF  [US equity index / ETF]")
        self.cb.grid(row=0, column=1, columnspan=4, sticky="w", padx=4, pady=3)

        self.e_start = _labeled_entry(self, "Start", "2005-01-01", row=0, col=6)
        self.e_atr = _labeled_entry(self, "ATR period", "14", row=1, col=0)
        self.e_mult = _labeled_entry(self, "ATR mult", "1.0", row=1, col=2)
        self.e_bet = _labeled_entry(self, "Base bet $", "100", row=1, col=4)
        self.e_target = _labeled_entry(self, "Target streak", "10", row=1, col=6)
        self.e_comm = _labeled_entry(self, "Commission $", "0", row=2, col=0)
        self.e_slip = _labeled_entry(self, "Slippage frac", "0.0", row=2, col=2)
        self.e_bank = _labeled_entry(self, "Start bank $", "10000", row=2, col=4)
        self.e_cap = _labeled_entry(self, "Cap mult (blank=none)", "", row=2, col=6)

        if options_mode:
            self.e_dte = _labeled_entry(self, "DTE days", "365", row=3, col=0)
            self.e_delta = _labeled_entry(self, "Target Δ", "0.95", row=3, col=2)
            self.e_r = _labeled_entry(self, "Risk-free r", "0.045", row=3, col=4)
            self.e_ivwin = _labeled_entry(self, "IV window (d)", "20", row=3, col=6)

    def ticker(self) -> str:
        return self.cb.get().split(" — ")[0].strip()

    def common(self) -> dict:
        cap = self.e_cap.get().strip()
        return dict(
            atr_period=int(self.e_atr.get()), mult=float(self.e_mult.get()),
            base_bet=float(self.e_bet.get()), target_streak=int(self.e_target.get()),
            commission=float(self.e_comm.get()), slippage_frac=float(self.e_slip.get()),
            starting_bank=float(self.e_bank.get()),
            cap_mult=float(cap) if cap else None,
            start=self.e_start.get().strip(),
        )


# --------------------------------------------------------------------------- Tab 2
class AtrTab(ttk.Frame):
    def __init__(self, master, options_mode=False):
        super().__init__(master)
        self.options_mode = options_mode
        self.ctrls = AssetControls(self, options_mode)
        self.ctrls.pack(fill=tk.X, padx=10, pady=6)
        bar = ttk.Frame(self); bar.pack(fill=tk.X, padx=10)
        ttk.Button(bar, text="Run backtest", command=self.run).pack(side=tk.LEFT)
        self.status = ttk.Label(bar, text="")
        self.status.pack(side=tk.LEFT, padx=10)

        body = ttk.Frame(self); body.pack(fill=tk.BOTH, expand=True)
        self.fig, self.axs = plt.subplots(2, 1, figsize=(10, 6))
        self.fig.tight_layout(pad=3)
        self.canvas = FigureCanvasTkAgg(self.fig, master=body)
        self.canvas.get_tk_widget().pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        NavigationToolbar2Tk(self.canvas, body, pack_toolbar=False)
        self.stats = tk.Text(body, width=40, font=MONO)
        self.stats.pack(side=tk.RIGHT, fill=tk.Y)

    def run(self):
        try:
            c = self.ctrls.common()
            tk_ = self.ctrls.ticker()
        except ValueError:
            messagebox.showerror("Input", "Enter valid numbers.")
            return
        self.status.config(text=f"loading {tk_} ..."); self.update_idletasks()
        try:
            daily = datamod.fetch(tk_, start=c["start"])
            weekly = datamod.weekly(daily)
            watr = datamod.atr(weekly, c["atr_period"])
        except Exception as ex:
            messagebox.showerror("Data", str(ex)); self.status.config(text="failed"); return

        trials = strat.resolve_trials(daily, weekly, watr, c["mult"])
        if not trials:
            messagebox.showwarning("Backtest", "No trials resolved."); return

        if self.options_mode:
            rv = datamod.realized_vol(daily["Close"], int(self.ctrls.e_ivwin.get()))
            res = strat.run_options(
                trials, daily, rv, c["base_bet"], c["target_streak"],
                r=float(self.ctrls.e_r.get()), dte_days=int(self.ctrls.e_dte.get()),
                target_delta=float(self.ctrls.e_delta.get()),
                commission=c["commission"], slippage_frac=c["slippage_frac"],
                starting_bank=c["starting_bank"], cap_mult=c["cap_mult"])
        else:
            res = strat.run_linear(
                trials, c["base_bet"], c["target_streak"],
                commission=c["commission"], slippage_frac=c["slippage_frac"],
                starting_bank=c["starting_bank"], cap_mult=c["cap_mult"])

        self._plot(daily, res)
        self._stats(tk_, res)
        self.status.config(text="done")

    def _plot(self, daily, res):
        ax0, ax1 = self.axs
        ax0.clear(); ax1.clear()
        ax0.plot(daily.index, daily["Close"], color="black", lw=0.6, label="Close")
        wins = [t for t in res.trials if t.outcome == "win"]
        loss = [t for t in res.trials if t.outcome == "loss"]
        ax0.scatter([t.entry_date for t in wins], [t.entry_price for t in wins],
                    marker="^", color="green", s=24, label="win entry")
        ax0.scatter([t.entry_date for t in loss], [t.entry_price for t in loss],
                    marker="v", color="red", s=24, label="loss entry")
        ax0.set_title("Price + entries"); ax0.legend(fontsize=8)

        if self.options_mode and res.delta_path:
            axd = ax0.twinx()
            axd.plot(res.delta_dates, res.delta_path, color="blue", lw=0.7, alpha=0.6)
            axd.set_ylabel("call Δ", color="blue"); axd.set_ylim(0, 1.05)

        ax1.plot(res.equity_dates, res.equity, color="purple")
        ax1.axhline(res.equity[0] if res.equity else 0, color="grey", lw=0.5)
        ax1.set_title("Equity curve")
        self.fig.tight_layout(pad=3); self.canvas.draw()

    def _stats(self, ticker, res):
        lines = [
            f"ticker        : {ticker}",
            f"trials        : {res.n_trials}",
            f"wins          : {res.wins}",
            f"empirical p   : {res.empirical_p:.4f}",
            f"final bank    : {res.final_bank:,.2f}",
            f"max drawdown  : {res.max_drawdown:,.2f}",
            "",
            f"E[cycle] @p   : {res.closed_form_ev_cycle:+.4f}",
            "  (b*((2p)^N-1); >0 only if p>0.5)",
        ]
        if self.options_mode and res.delta_path:
            dp = res.delta_path
            lines += ["", f"Δ mean        : {sum(dp)/len(dp):.3f}",
                      f"Δ min / max   : {min(dp):.3f} / {max(dp):.3f}"]
        self.stats.delete("1.0", tk.END)
        self.stats.insert(tk.END, "\n".join(lines))


def main():
    root = tk.Tk()
    root.title("Antimartingale studio")
    root.geometry("1500x900")
    nb = ttk.Notebook(root)
    nb.add(CoinFlipTab(nb), text="1 · Coin-flip")
    nb.add(AtrTab(nb, options_mode=False), text="2 · ATR backtest")
    nb.add(AtrTab(nb, options_mode=True), text="3 · Options (auto Δ)")
    nb.pack(fill=tk.BOTH, expand=True)
    root.mainloop()


if __name__ == "__main__":
    main()
