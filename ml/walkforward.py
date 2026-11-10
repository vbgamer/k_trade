"""Rolling walk-forward test: the 'keeps learning' setup.

For each test month M:
  * train on everything before (M - CAL_MONTHS)
  * pick the entry threshold on the CAL_MONTHS just before M (data the model has not trained on)
  * trade month M blind
This is exactly how a monthly-retrained live model would have performed.

    ml/.venv/Scripts/python.exe ml/walkforward.py [variant ...]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from features import FEATURE_NAMES  # noqa: E402
from train import DATA, backtest, fit, pick_threshold, summary  # noqa: E402

FIRST_TEST = pd.Period("2024-07", "M")
CAL_MONTHS = 6


def run(df: pd.DataFrame, name: str):
    d = df.copy()
    for side in ("ce", "pe"):
        d[f"ret_{side}"], d[f"held_{side}"] = d[f"ret_{side}_{name}"], d[f"held_{side}_{name}"]
    d = d.dropna(subset=["ret_ce", "ret_pe"])
    d["month"] = pd.to_datetime(d["day"]).dt.to_period("M")
    all_trades = []
    for m in sorted(d["month"].unique()):
        if m < FIRST_TEST:
            continue
        cal_start = m - CAL_MONTHS
        tr = d[d.month < cal_start]
        cal = d[(d.month >= cal_start) & (d.month < m)].copy()
        te = d[d.month == m].copy()
        m_ce, m_pe = fit(tr, cal, "ret_ce"), fit(tr, cal, "ret_pe")
        for part in (cal, te):
            part["pred_ce"] = m_ce.predict(part[FEATURE_NAMES])
            part["pred_pe"] = m_pe.predict(part[FEATURE_NAMES])
        th = pick_threshold(cal)
        t = backtest(te, th)
        if not t.empty:
            all_trades.append(t)
        print(f"  {name} {m}: trades {len(t):3d} pnl {t['pnl_rs'].sum() if not t.empty else 0:9.0f}", flush=True)
    trades = pd.concat(all_trades) if all_trades else pd.DataFrame()
    days = d[d.month >= FIRST_TEST].day.nunique()
    return summary(trades, days)


def main():
    df = pd.read_parquet(DATA)
    df["day"] = pd.to_datetime(df["day"]).dt.date
    variants = sys.argv[1:] or ["t15s8h30", "t30s12h60", "t50s20h120"]
    out = {}
    for v in variants:
        out[v] = run(df, v)
    print("\n== rolling monthly retrain, 2024-07 onwards (all out-of-sample) ==")
    print(pd.DataFrame(out).T.to_string())


if __name__ == "__main__":
    main()
