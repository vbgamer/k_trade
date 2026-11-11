"""Build the admin "AI Lab" training report from the trained models + dataset.

Everything is computed from the saved models in ml/artifacts (the exact models that get deployed), on the same
train / validation / test split train.py used, and written to ml/artifacts/training_report.json.
export_model.py copies it next to the models so the server can show it.

    ml/.venv/Scripts/python.exe ml/report.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime

import lightgbm as lgb
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build_dataset import EXIT_GRID  # noqa: E402
from features import FEATURE_NAMES  # noqa: E402
from train import ART, DATA, MAX_TRADES_PER_DAY, PARAMS, TRAIN_END, VAL_END, backtest, summary  # noqa: E402

CURVE_POINTS = 120
CLIP = (-0.2, 0.3)  # train.fit clips labels to this range before training


def huber(pred: np.ndarray, y: np.ndarray, alpha: float) -> float:
    r = np.abs(pred - y)
    return float(np.mean(np.where(r <= alpha, 0.5 * r * r, alpha * (r - 0.5 * alpha))))


def learning_curve(booster: lgb.Booster, parts: dict, target: str) -> dict:
    """Loss after each boosting round, from the per-tree outputs of the saved model (no retraining)."""
    n = booster.num_trees()
    steps = sorted(set(np.linspace(1, n, min(n, CURVE_POINTS)).astype(int).tolist()))
    out = {"rounds": steps}
    for name, df in parts.items():
        X, y = df[FEATURE_NAMES], df[target].clip(*CLIP).to_numpy()
        cum = np.zeros(len(df))
        losses, done = [], 0
        for s in steps:
            for i in range(done, s):
                cum += booster.predict(X, start_iteration=i, num_iteration=1)
            done = s
            losses.append(round(huber(cum, y, PARAMS["alpha"]), 6))
        out[name] = losses
    return out


def calibration(df: pd.DataFrame, bins: int = 10) -> list:
    """Bucket every (minute, side) by predicted reward and compare with the reward that actually happened."""
    rows = pd.concat([
        pd.DataFrame({"pred": df.pred_ce, "actual": df.ret_ce}),
        pd.DataFrame({"pred": df.pred_pe, "actual": df.ret_pe}),
    ])
    rows["bucket"] = pd.qcut(rows["pred"], bins, labels=False, duplicates="drop")
    g = rows.groupby("bucket")
    return [
        {"pred_lo": round(float(x.pred.min()) * 100, 2), "pred_hi": round(float(x.pred.max()) * 100, 2),
         "pred_avg": round(float(x.pred.mean()) * 100, 2), "actual_avg": round(float(x.actual.mean()) * 100, 2),
         "win_rate": round(float((x.actual > 0).mean()), 3), "count": int(len(x))}
        for _, x in g
    ]


def top_slice(df: pd.DataFrame, threshold: float) -> dict:
    """How the setups the model would actually trade (prediction above the threshold) turned out."""
    best = np.maximum(df.pred_ce, df.pred_pe)
    sel = df[best > threshold]
    actual = np.where(sel.pred_ce >= sel.pred_pe, sel.ret_ce, sel.ret_pe)
    allr = np.concatenate([df.ret_ce.to_numpy(), df.ret_pe.to_numpy()])
    return {
        "signals": int(len(sel)), "minutes": int(len(df)),
        "signal_avg_pct": round(float(np.mean(actual)) * 100, 2) if len(sel) else None,
        "signal_win_rate": round(float(np.mean(actual > 0)), 3) if len(sel) else None,
        "all_avg_pct": round(float(allr.mean()) * 100, 2), "all_win_rate": round(float((allr > 0).mean()), 3),
    }


def equity(trades: pd.DataFrame) -> list:
    if trades.empty:
        return []
    daily = trades.groupby("day")["pnl_rs"].sum().cumsum()
    return [{"day": str(d), "equity": round(float(v), 0)} for d, v in daily.items()]


def trade_rows(trades: pd.DataFrame) -> list:
    return [{"day": str(r.day), "time": f"{(r.minute + 1) // 60:02d}:{(r.minute + 1) % 60:02d}", "side": r.side,
             "pred_pct": round(float(r.pred) * 100, 2), "actual_pct": round(float(r.ret) * 100, 2),
             "pnl_rs": round(float(r.pnl_rs), 0)} for r in trades.itertuples(index=False)]


def first_tree(booster: lgb.Booster, depth: int = 3) -> dict:
    """The first tree, cut to a few levels, as a picture of what one "rule of thumb" in the model looks like."""
    def walk(node, d):
        if "leaf_value" in node:
            return {"leaf": round(node["leaf_value"] * 100, 3), "count": node.get("leaf_count")}
        out = {"feature": FEATURE_NAMES[node["split_feature"]], "threshold": round(float(node["threshold"]), 5),
               "value": round(node["internal_value"] * 100, 3), "count": node.get("internal_count")}
        if d < depth:
            out["left"], out["right"] = walk(node["left_child"], d + 1), walk(node["right_child"], d + 1)
        return out
    return walk(booster.dump_model()["tree_info"][0]["tree_structure"], 0)


def walk_forward_months(variant: str) -> list:
    """Monthly results of the rolling retrain (parsed from walkforward.py's log)."""
    path = os.path.join(HERE, "data", "wf.log")
    if not os.path.exists(path):
        return []
    pat = re.compile(rf"^\s*{re.escape(variant)} (\d{{4}}-\d{{2}}): trades\s+(\d+) pnl\s+(-?\d+)")
    out = []
    for line in open(path, encoding="utf-8", errors="ignore"):
        m = pat.match(line)
        if m:
            out.append({"month": m.group(1), "trades": int(m.group(2)), "pnl_rs": int(m.group(3))})
    return out


def main():
    with open(os.path.join(ART, "model_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    variant, threshold = meta["exit_rule"], float(meta["threshold"])

    df = pd.read_parquet(DATA)
    df["day"] = pd.to_datetime(df["day"]).dt.date
    for side in ("ce", "pe"):
        df[f"ret_{side}"], df[f"held_{side}"] = df[f"ret_{side}_{variant}"], df[f"held_{side}_{variant}"]
    df = df.dropna(subset=["ret_ce", "ret_pe"])
    tr = df[df.day <= TRAIN_END]
    va = df[(df.day > TRAIN_END) & (df.day <= VAL_END)].copy()
    te = df[df.day > VAL_END].copy()

    boosters = {s: lgb.Booster(model_file=os.path.join(ART, f"entry_{s}.txt")) for s in ("ce", "pe")}
    for part in (va, te):
        part["pred_ce"] = boosters["ce"].predict(part[FEATURE_NAMES])
        part["pred_pe"] = boosters["pe"].predict(part[FEATURE_NAMES])

    def split_info(d):
        r = np.concatenate([d.ret_ce.to_numpy(), d.ret_pe.to_numpy()])
        return {"rows": int(len(d)), "days": int(d.day.nunique()), "from": str(d.day.min()), "to": str(d.day.max()),
                "avg_reward_pct": round(float(r.mean()) * 100, 2), "positive_pct": round(float((r > 0).mean()), 3)}

    print("learning curves...", flush=True)
    sample_tr = tr.sample(n=min(len(tr), 60000), random_state=0)
    curves = {s: learning_curve(b, {"train": sample_tr, "validation": va, "test": te}, f"ret_{s}")
              for s, b in boosters.items()}

    importance = {}
    for s, b in boosters.items():
        gain, split = b.feature_importance("gain"), b.feature_importance("split")
        tot = gain.sum() or 1.0
        importance[s] = sorted(
            [{"feature": f, "gain_pct": round(float(g / tot) * 100, 2), "splits": int(c)}
             for f, g, c in zip(FEATURE_NAMES, gain, split)], key=lambda x: -x["gain_pct"])

    tv, tt = backtest(va, threshold), backtest(te, threshold)
    tp, sl, hold = EXIT_GRID[variant]
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": {
            "type": "LightGBM gradient-boosted trees (2 regressors: ATM CE, ATM PE)",
            "target": "net % return of buying the ATM option now and exiting by the exit rule, after costs",
            "trees": {s: b.num_trees() for s, b in boosters.items()},
            "params": PARAMS, "label_clip": list(CLIP), "early_stopping_rounds": 100, "max_rounds": 2000,
            "threshold_pct": round(threshold * 100, 3), "max_trades_per_day": MAX_TRADES_PER_DAY,
            "exit_rule": {"name": variant, "target_pct": tp * 100, "stop_pct": sl * 100,
                          "max_hold_min": hold},
            "features": FEATURE_NAMES,
        },
        "data": {"train": split_info(tr), "validation": split_info(va), "test": split_info(te),
                 "total_rows": int(len(df)), "total_days": int(df.day.nunique())},
        "learning_curves": curves,
        "importance": importance,
        "calibration": {"validation": calibration(va), "test": calibration(te)},
        "signal_quality": {"validation": top_slice(va, threshold), "test": top_slice(te, threshold)},
        "equity": {"validation": equity(tv), "test": equity(tt)},
        "trades": {"test": trade_rows(tt)},
        "results": {"validation": summary(tv, va.day.nunique()), "test": summary(tt, te.day.nunique())},
        "variants": meta.get("all_variants", {}),
        "walk_forward": {**(meta.get("walk_forward") or {}), "months": walk_forward_months(variant)},
        "first_tree": first_tree(boosters["ce"]),
    }
    out = os.path.join(ART, "training_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, separators=(",", ":"))
    print(f"wrote {out} ({os.path.getsize(out) / 1e3:.0f} KB)")
    print("results test:", report["results"]["test"])


if __name__ == "__main__":
    main()
