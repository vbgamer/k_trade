"""Train/live parity check: replay a historical day through the LIVE feature path (backend/app/ml/agent.py)
and compare with the training rows from build_dataset.py. Any mismatch means live trading would see
different inputs than the model was trained on.

    ml/.venv/Scripts/python.exe ml/check_parity.py [YYYY-MM-DD]
"""
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "backend"))
from app.ml.agent import build_live_features  # noqa: E402
from build_dataset import OPEN_MIN, RAW, N_BARS, day_matrix, load_spot  # noqa: E402
from features import FEATURE_NAMES  # noqa: E402


def main():
    ds = pd.read_parquet(os.path.join(HERE, "data", "dataset.parquet"))
    ds["day"] = pd.to_datetime(ds["day"]).dt.date
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else sorted(ds.day.unique())[-20]
    rows_ds = ds[ds.day == day].set_index("minute")
    expiry = pd.to_datetime(rows_ds["expiry"].iloc[0]).date()

    spot = load_spot()
    days = sorted(spot["day"].unique())
    i = days.index(day)
    pc, ppc = (spot[spot.day == days[i - 1]]["close"].iloc[-1], spot[spot.day == days[i - 2]]["close"].iloc[-1])
    sd = spot[spot.day == day]
    closes = pd.Series(np.nan, index=range(N_BARS))
    for c in ("open", "high", "low", "close"):
        pass
    bars_all = []
    full = {c: np.full(N_BARS, np.nan) for c in ("open", "high", "low", "close")}
    for c in full:
        full[c][sd["slot"].to_numpy()] = sd[c].to_numpy()
    cl = pd.Series(full["close"]).ffill().bfill().to_numpy()
    for c in ("open", "high", "low"):
        full[c] = np.where(np.isnan(full[c]), cl, full[c])
    for s in range(N_BARS):
        bars_all.append({"minute": OPEN_MIN + s, "open": full["open"][s], "high": full["high"][s],
                         "low": full["low"][s], "close": cl[s]})

    opt = pd.read_parquet(os.path.join(RAW, "options", f"{expiry.isoformat()}.parquet"))
    opt["trading_day"] = pd.to_datetime(opt["trading_day"]).dt.date
    opt = opt[opt.trading_day == day]
    ts = opt["timestamp"].dt.tz_localize(None)
    opt["slot"] = ts.dt.hour * 60 + ts.dt.minute - OPEN_MIN
    opt = opt[(opt.slot >= 0) & (opt.slot < N_BARS)]
    mat = day_matrix(opt, ["open", "high", "low", "close", "open_interest"])

    worst = {}
    checked = 0
    for m, ref in rows_ds.iterrows():
        s = m - OPEN_MIN
        bars = bars_all[: s + 1]
        chain_hist = {}
        for (k, t), arr in mat.items():
            if (k, "CE") in mat and (k, "PE") in mat and t == "CE":
                ce_c, pe_c = mat[(k, "CE")]["close"], mat[(k, "PE")]["close"]
                chain_hist[k] = {OPEN_MIN + j: (ce_c[j], pe_c[j]) for j in range(s + 1)
                                 if np.isfinite(ce_c[j]) and np.isfinite(pe_c[j])}
        live_rows = []
        for k in sorted({k for k, _ in mat}):
            def leg(t):
                a = mat.get((k, t))
                if a is None or not np.isfinite(a["close"][s]):
                    return None
                first = next((v for v in a["open_interest"][: s + 1] if v > 0), 0)
                return {"ltp": a["close"][s], "oi": a["open_interest"][s], "prev_oi": first or None,
                        "instrument_key": f"{k}{t}", "trading_symbol": f"{k}{t}"}
            live_rows.append({"strike": k, "ce": leg("CE"), "pe": leg("PE")})
        f, _, why = build_live_features(bars=bars, rows=live_rows, chain_hist=chain_hist, today=day, expiry=expiry,
                                        prev_close=pc, prev_day_ret=float(np.log(pc / ppc)))
        if f is None:
            print(f"minute {m}: live path not ready: {why}")
            continue
        checked += 1
        for n in FEATURE_NAMES:
            d = abs(f[n] - ref[n])
            if d > worst.get(n, (0,))[0]:
                worst[n] = (d, m, f[n], ref[n])
    print(f"{day}: compared {checked} minutes")
    bad = {n: w for n, w in worst.items() if w[0] > 1e-9}
    for n, (d, m, a, b) in sorted(bad.items(), key=lambda x: -x[1][0]):
        print(f"  MISMATCH {n:16s} max diff {d:.3g} at minute {m}: live {a:.6g} vs train {b:.6g}")
    print("PARITY OK" if not bad else f"{len(bad)} features differ")


if __name__ == "__main__":
    main()
