"""Build the training table: one row per (day, decision minute) with features + simulated trade outcomes.

For every decision minute we simulate buying the ATM CE and, separately, the ATM PE of the nearest weekly
expiry at the NEXT minute's open (we decide when a bar closes, so we can only fill on the next bar),
then exit at target / stop-loss / time-limit / 15:15 square-off, charging slippage, brokerage and taxes.
The net % return of each simulated trade is the "reward" the model learns from.

    ml/.venv/Scripts/python.exe ml/build_dataset.py
"""
from __future__ import annotations

import glob
import os
import sys
import time
from datetime import date

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from features import FEATURE_NAMES, STRIKE_STEP, atm_strike, compute_features  # noqa: E402

RAW = os.path.join(HERE, "data", "raw")
OUT = os.path.join(HERE, "data", "dataset.parquet")

OPEN_MIN = 9 * 60 + 15          # 09:15 bar
N_BARS = 375                    # 09:15 .. 15:29
FIRST_DECISION = 9 * 60 + 45    # need 30 min of history for features
LAST_DECISION = 14 * 60 + 30    # no new entries after 14:30
SQUARE_OFF = 15 * 60 + 15       # forced exit bar

# Trade simulation (also used as the live exit rules)
SIM = {
    "target_pct": 0.15,     # +15% on premium
    "stop_pct": 0.08,       # -8% on premium
    "max_hold_min": 30,
    "slippage_pct": 0.0025, # per side, against us
    "taxes_pct": 0.0012,    # STT on sell + exchange + GST + stamp, round trip, on premium
    "brokerage_rs": 40.0,   # Rs 20 per order x 2
    "lot_size": 65,
}
# Exit-rule variants labelled in the same pass, so train.py can compare them: name -> (target, stop, max_hold_min)
EXIT_GRID = {
    "t15s8h30": (0.15, 0.08, 30),
    "t10s10h15": (0.10, 0.10, 15),
    "t30s12h60": (0.30, 0.12, 60),
    "t50s20h120": (0.50, 0.20, 120),
    "t25s25h90": (0.25, 0.25, 90),
    "t100s30eod": (1.00, 0.30, 400),
}
NEAR_STRIKES = 3            # strikes each side of ATM used for PCR


def load_spot() -> pd.DataFrame:
    frames = []
    for p in sorted(glob.glob(os.path.join(RAW, "spot", "*.csv"))):
        d = pd.read_csv(p, usecols=["Timestamp", "Open", "High", "Low", "Close"])
        frames.append(d)
    s = pd.concat(frames, ignore_index=True)
    s["ts"] = pd.to_datetime(s["Timestamp"])
    s = s.drop(columns="Timestamp").rename(columns=str.lower).drop_duplicates("ts").sort_values("ts")
    s["day"] = s["ts"].dt.date
    s["slot"] = s["ts"].dt.hour * 60 + s["ts"].dt.minute - OPEN_MIN
    return s[(s["slot"] >= 0) & (s["slot"] < N_BARS)]


def day_matrix(df: pd.DataFrame, cols) -> dict:
    """Pivot one day's option rows into {(strike, type): {col: np.array[N_BARS]}} with gaps filled."""
    out = {}
    for (k, t), g in df.groupby(["strike", "option_type"], sort=False):
        idx = g["slot"].to_numpy()
        arrs = {}
        for col in cols:
            a = np.full(N_BARS, np.nan)
            a[idx] = g[col].to_numpy(dtype=float)
            arrs[col] = a
        close = pd.Series(arrs["close"]).ffill().to_numpy()
        for col in ("open", "high", "low"):
            arrs[col] = np.where(np.isnan(arrs[col]), close, arrs[col])
        arrs["close"] = close
        arrs["open_interest"] = pd.Series(arrs["open_interest"]).ffill().fillna(0).to_numpy()
        out[(int(k), t)] = arrs
    return out


def simulate(c: dict, entry_slot: int, cfg=SIM):
    """Simulate a long option trade entering at the open of entry_slot. Returns (net_ret, exit_reason, bars_held)."""
    raw_entry = c["open"][entry_slot]
    if not np.isfinite(raw_entry) or raw_entry < 5:
        return np.nan, "nodata", 0
    entry = raw_entry * (1 + cfg["slippage_pct"])
    tp, sl = entry * (1 + cfg["target_pct"]), entry * (1 - cfg["stop_pct"])
    last = min(entry_slot + cfg["max_hold_min"] - 1, SQUARE_OFF - OPEN_MIN)
    exit_px, reason, held = None, "time", 0
    for i in range(entry_slot, last + 1):
        held = i - entry_slot + 1
        lo, hi, op = c["low"][i], c["high"][i], c["open"][i]
        if lo <= sl:  # stop first when both touched in the same bar (conservative)
            exit_px, reason = min(sl, op) if i > entry_slot else sl, "stop"
            break
        if hi >= tp:
            exit_px, reason = max(tp, op) if i > entry_slot else tp, "target"
            break
    if exit_px is None:
        exit_px = c["close"][last]
        reason = "squareoff" if last == SQUARE_OFF - OPEN_MIN else "time"
    exit_px *= (1 - cfg["slippage_pct"])
    cost = cfg["taxes_pct"] + cfg["brokerage_rs"] / (entry * cfg["lot_size"])
    return exit_px / entry - 1 - cost, reason, held


def build():
    t0 = time.time()
    spot = load_spot()
    by_day = {d: g for d, g in spot.groupby("day")}
    days = sorted(by_day)
    expiries = sorted(date.fromisoformat(os.path.basename(p)[:10])
                      for p in glob.glob(os.path.join(RAW, "options", "*.parquet")))

    # day -> nearest expiry on/after it (only days covered by option data)
    day_exp = {}
    for d in days:
        nxt = [e for e in expiries if e >= d]
        if nxt and d >= date(2021, 5, 24) and (nxt[0] - d).days <= 8:
            day_exp[d] = nxt[0]

    prev_close, prev_ret = {}, {}
    for i, d in enumerate(days):
        if i:
            pc = by_day[days[i - 1]]["close"].iloc[-1]
            prev_close[d] = pc
            ppc = by_day[days[i - 2]]["close"].iloc[-1] if i > 1 else None
            prev_ret[d] = float(np.log(pc / ppc)) if ppc else 0.0

    rows = []
    for exp in sorted(set(day_exp.values())):
        exp_days = [d for d in day_exp if day_exp[d] == exp]
        opt = pd.read_parquet(os.path.join(RAW, "options", f"{exp.isoformat()}.parquet"),
                              columns=["timestamp", "open", "high", "low", "close", "open_interest",
                                       "trading_day", "strike", "option_type"])
        opt["trading_day"] = pd.to_datetime(opt["trading_day"]).dt.date
        opt = opt[opt["trading_day"].isin(exp_days)]
        ts = opt["timestamp"].dt.tz_localize(None) if opt["timestamp"].dt.tz is not None else opt["timestamp"]
        opt["slot"] = ts.dt.hour * 60 + ts.dt.minute - OPEN_MIN
        opt = opt[(opt["slot"] >= 0) & (opt["slot"] < N_BARS)]

        for d, od in opt.groupby("trading_day"):
            sd = by_day[d]
            if len(sd) < 300:
                continue  # half / broken day
            sp = {c: np.full(N_BARS, np.nan) for c in ("open", "high", "low", "close")}
            for c in sp:
                sp[c][sd["slot"].to_numpy()] = sd[c].to_numpy()
            closes = pd.Series(sp["close"]).ffill().bfill().to_numpy()
            for c in ("open", "high", "low"):
                sp[c] = np.where(np.isnan(sp[c]), closes, sp[c])
            sp["close"] = closes

            lo_k = atm_strike(np.nanmin(closes)) - (NEAR_STRIKES + 2) * STRIKE_STEP
            hi_k = atm_strike(np.nanmax(closes)) + (NEAR_STRIKES + 2) * STRIKE_STEP
            od = od[(od["strike"] >= lo_k) & (od["strike"] <= hi_k)]
            mat = day_matrix(od, ["open", "high", "low", "close", "open_interest"])

            for m in range(FIRST_DECISION, LAST_DECISION + 1):
                s = m - OPEN_MIN          # decision bar (just closed)
                spot_now = closes[s]
                k = atm_strike(spot_now)
                ce, pe = mat.get((k, "CE")), mat.get((k, "PE"))
                if ce is None or pe is None or not (np.isfinite(ce["close"][s]) and np.isfinite(pe["close"][s])):
                    continue
                oi_ce = oi_pe = 0.0
                for j in range(-NEAR_STRIKES, NEAR_STRIKES + 1):
                    kk = k + j * STRIKE_STEP
                    if (kk, "CE") in mat:
                        oi_ce += mat[(kk, "CE")]["open_interest"][s]
                    if (kk, "PE") in mat:
                        oi_pe += mat[(kk, "PE")]["open_interest"][s]

                def _oi_chg(cc):
                    first = next((v for v in cc["open_interest"][: s + 1] if v > 0), 0)
                    return cc["open_interest"][s] / first - 1 if first else 0.0

                ce_hist = ce["close"][: s + 1]
                pe_hist = pe["close"][: s + 1]
                ce_hist = np.where(np.isnan(ce_hist), ce["close"][s], ce_hist)
                pe_hist = np.where(np.isnan(pe_hist), pe["close"][s], pe_hist)
                f = compute_features(
                    opens=sp["open"][: s + 1], highs=sp["high"][: s + 1], lows=sp["low"][: s + 1],
                    closes=closes[: s + 1], minute_of_day=m + 1, today=d, expiry=exp,
                    prev_close=prev_close.get(d), prev_day_ret=prev_ret.get(d, 0.0),
                    ce_prem_hist=ce_hist, pe_prem_hist=pe_hist,
                    pcr_near=oi_pe / oi_ce if oi_ce > 0 else 1.0,
                    ce_oi_chg=_oi_chg(ce), pe_oi_chg=_oi_chg(pe),
                )
                f.update(day=d, minute=m, strike=k, expiry=exp,
                         ce_entry=ce["open"][s + 1], pe_entry=pe["open"][s + 1])
                for name, (tp_, sl_, h_) in EXIT_GRID.items():
                    cfg = dict(SIM, target_pct=tp_, stop_pct=sl_, max_hold_min=h_)
                    for side, cc in (("ce", ce), ("pe", pe)):
                        r, why, held = simulate(cc, s + 1, cfg)
                        f[f"ret_{side}_{name}"], f[f"held_{side}_{name}"] = r, held
                # default columns = the live SIM rules
                for side in ("ce", "pe"):
                    f[f"ret_{side}"], f[f"held_{side}"] = f[f"ret_{side}_t15s8h30"], f[f"held_{side}_t15s8h30"]
                rows.append(f)
        print(f"  expiry {exp}: {len(rows):,} rows so far ({time.time() - t0:.0f}s)", flush=True)

    df = pd.DataFrame(rows)
    df = df.dropna(subset=["ret_ce", "ret_pe"])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    df.to_parquet(OUT, index=False)
    print(f"Saved {len(df):,} rows, {df['day'].nunique()} days -> {OUT} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    build()
