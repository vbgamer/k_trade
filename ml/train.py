"""Train the entry model and backtest it walk-forward on data it has never seen.

Model: two LightGBM regressors (ATM CE, ATM PE) that predict the *net reward* (% return after costs) of
buying that option now and managing it with the target / stop / time exits from build_dataset.SIM.
Profitable setups in history pull predictions up, losing ones pull them down.

Split (by date, never shuffled):
    train      .. TRAIN_END
    validation .. VAL_END     -> picks the entry threshold
    test       after VAL_END  -> final, untouched exam

    ml/.venv/Scripts/python.exe ml/train.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

import lightgbm as lgb
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build_dataset import EXIT_GRID, SIM  # noqa: E402
from features import FEATURE_NAMES  # noqa: E402

DATA = os.path.join(HERE, "data", "dataset.parquet")
ART = os.path.join(HERE, "artifacts")

TRAIN_END = date(2024, 6, 30)
VAL_END = date(2025, 6, 30)
MAX_TRADES_PER_DAY = 3
MIN_VAL_TRADES = 60
PARAMS = dict(
    objective="huber", alpha=0.05, learning_rate=0.03, num_leaves=31, min_child_samples=400,
    feature_fraction=0.7, bagging_fraction=0.7, bagging_freq=1, lambda_l2=5.0, verbose=-1,
)


def fit(train: pd.DataFrame, val: pd.DataFrame, target: str) -> lgb.Booster:
    dtr = lgb.Dataset(train[FEATURE_NAMES], train[target].clip(-0.2, 0.3))
    dva = lgb.Dataset(val[FEATURE_NAMES], val[target].clip(-0.2, 0.3))
    return lgb.train(PARAMS, dtr, num_boost_round=2000, valid_sets=[dva],
                     callbacks=[lgb.early_stopping(100, verbose=False)])


def backtest(df: pd.DataFrame, threshold: float, max_per_day: int = MAX_TRADES_PER_DAY) -> pd.DataFrame:
    """Replay minute by minute: one position at a time, enter the better side if its predicted reward > threshold."""
    trades = []
    for d, g in df.sort_values(["day", "minute"]).groupby("day"):
        busy_until, n = -1, 0
        for r in g.itertuples(index=False):
            if n >= max_per_day or r.minute <= busy_until:
                continue
            side = "CE" if r.pred_ce >= r.pred_pe else "PE"
            pred = max(r.pred_ce, r.pred_pe)
            if pred <= threshold:
                continue
            ret = r.ret_ce if side == "CE" else r.ret_pe
            held = r.held_ce if side == "CE" else r.held_pe
            entry = r.ce_entry if side == "CE" else r.pe_entry
            trades.append(dict(day=d, minute=r.minute, side=side, pred=pred, ret=ret,
                               pnl_rs=ret * entry * (1 + SIM["slippage_pct"]) * SIM["lot_size"]))
            busy_until = r.minute + held
            n += 1
    return pd.DataFrame(trades)


def summary(t: pd.DataFrame, n_days: int) -> dict:
    if t.empty:
        return {"trades": 0}
    daily = t.groupby("day")["pnl_rs"].sum()
    equity = daily.cumsum()
    return {
        "trades": int(len(t)),
        "trading_days": int(n_days),
        "win_rate": round(float((t["ret"] > 0).mean()), 3),
        "avg_ret_pct": round(float(t["ret"].mean() * 100), 2),
        "total_pnl_rs_1lot": round(float(t["pnl_rs"].sum()), 0),
        "max_drawdown_rs": round(float((equity.cummax() - equity).max()), 0),
        "green_days_pct": round(float((daily > 0).mean()), 3),
        "worst_day_rs": round(float(daily.min()), 0),
        "best_day_rs": round(float(daily.max()), 0),
    }


def pick_threshold(va: pd.DataFrame) -> float:
    best = None
    for q in np.linspace(0.90, 0.999, 25):
        th = float(np.quantile(np.maximum(va.pred_ce, va.pred_pe), q))
        t = backtest(va, th)
        if len(t) < MIN_VAL_TRADES:
            continue
        score = t["pnl_rs"].sum()
        if best is None or score > best[1]:
            best = (th, score)
    return best[0] if best else 0.0


def run_variant(df, name):
    """Train + backtest one exit-rule variant. Columns ret_ce_<name> etc. become the active labels."""
    d = df.copy()
    for side in ("ce", "pe"):
        d[f"ret_{side}"], d[f"held_{side}"] = d[f"ret_{side}_{name}"], d[f"held_{side}_{name}"]
    d = d.dropna(subset=["ret_ce", "ret_pe"])
    tr, va, te = d[d.day <= TRAIN_END], d[(d.day > TRAIN_END) & (d.day <= VAL_END)].copy(), d[d.day > VAL_END].copy()
    m_ce, m_pe = fit(tr, va, "ret_ce"), fit(tr, va, "ret_pe")
    for part in (va, te):
        part["pred_ce"] = m_ce.predict(part[FEATURE_NAMES])
        part["pred_pe"] = m_pe.predict(part[FEATURE_NAMES])
    th = pick_threshold(va)
    tv, tt = backtest(va, th), backtest(te, th)
    rnd = te.sample(n=max(len(tt), 1), random_state=1)
    side = np.random.default_rng(0).integers(0, 2, len(rnd))
    rr = np.where(side == 0, rnd.ret_ce, rnd.ret_pe)
    return dict(name=name, threshold=th, models=(m_ce, m_pe), val=summary(tv, va.day.nunique()),
                test=summary(tt, te.day.nunique()), test_trades=tt, random_avg=float(rr.mean()),
                base_avg=float((d.ret_ce.mean() + d.ret_pe.mean()) / 2))


def main():
    df = pd.read_parquet(DATA)
    df["day"] = pd.to_datetime(df["day"]).dt.date
    names = sorted({c[len("ret_ce_"):] for c in df.columns if c.startswith("ret_ce_")})
    results = []
    for n in names:
        r = run_variant(df, n)
        results.append(r)
        v, t = r["val"], r["test"]
        print(f"{n:12s} base {r['base_avg']*100:6.2f}% | VAL trades {v.get('trades',0):4d} pnl {v.get('total_pnl_rs_1lot',0):8.0f} "
              f"| TEST trades {t.get('trades',0):4d} win {t.get('win_rate',0):.2f} avg {t.get('avg_ret_pct',0):6.2f}% "
              f"pnl {t.get('total_pnl_rs_1lot',0):8.0f} dd {t.get('max_drawdown_rs',0):7.0f} | random avg {r['random_avg']*100:6.2f}%",
              flush=True)

    best = max(results, key=lambda r: r["val"].get("total_pnl_rs_1lot", -1e18))  # chosen on validation only
    print(f"\nchosen on validation: {best['name']}")
    for k, v in best["test"].items():
        print(f"  {k:20s} {v}")
    tt = best["test_trades"]
    if not tt.empty:
        tt["month"] = pd.to_datetime(tt["day"]).dt.to_period("M")
        print(tt.groupby("month").agg(trades=("ret", "size"), win_rate=("ret", lambda x: (x > 0).mean()),
                                      pnl_rs=("pnl_rs", "sum")).round(2).to_string())
    m_ce, m_pe = best["models"]
    imp = pd.Series(m_ce.feature_importance("gain"), index=FEATURE_NAMES).sort_values(ascending=False)
    print("top CE features:", ", ".join(imp.index[:8]))

    tp, sl, hold = EXIT_GRID[best["name"]]
    os.makedirs(ART, exist_ok=True)
    m_ce.save_model(os.path.join(ART, "entry_ce.txt"))
    m_pe.save_model(os.path.join(ART, "entry_pe.txt"))
    meta = {"exit_rule": best["name"], "threshold": best["threshold"], "features": FEATURE_NAMES,
            "sim": dict(SIM, target_pct=tp, stop_pct=sl, max_hold_min=hold),
            "max_trades_per_day": MAX_TRADES_PER_DAY, "train_end": str(TRAIN_END), "val_end": str(VAL_END),
            "results": {"validation": best["val"], "test": best["test"]},
            "all_variants": {r["name"]: {"val": r["val"], "test": r["test"]} for r in results}}
    with open(os.path.join(ART, "model_meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print(f"Saved models + meta to {ART}")


if __name__ == "__main__":
    main()
