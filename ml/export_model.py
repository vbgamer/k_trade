"""Export the trained LightGBM models + metadata into backend/app/ml/models for the live agent,
and verify the pure-Python evaluator reproduces LightGBM's predictions.

    ml/.venv/Scripts/python.exe ml/export_model.py
"""
import json
import os
import shutil
import sys

import lightgbm as lgb
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))
from app.ml.gbm import GBMModel  # noqa: E402

ART = os.path.join(HERE, "artifacts")
DEST = os.path.join(ROOT, "backend", "app", "ml", "models")


KEEP = ("split_feature", "threshold", "default_left", "leaf_value", "decision_type", "missing_type",
        "internal_value")  # internal_value powers the per-prediction explanation (GBMModel.explain)


def _slim(node):
    """Keep only what the evaluator and explainer need (drops gains, counts)."""
    out = {k: node[k] for k in KEEP if k in node}
    for child in ("left_child", "right_child"):
        if child in node:
            out[child] = _slim(node[child])
    return out


def main():
    os.makedirs(DEST, exist_ok=True)
    sample = pd.read_parquet(os.path.join(HERE, "data", "dataset.parquet")).sample(500, random_state=0)
    for side in ("ce", "pe"):
        booster = lgb.Booster(model_file=os.path.join(ART, f"entry_{side}.txt"))
        dump = booster.dump_model()
        out = os.path.join(DEST, f"entry_{side}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"feature_names": dump["feature_names"],
                       "tree_info": [{"tree_structure": _slim(t["tree_structure"])} for t in dump["tree_info"]]},
                      f, separators=(",", ":"))
        py = GBMModel.load(out)
        ref = booster.predict(sample[dump["feature_names"]])
        mine = [py.predict(r) for r in sample[dump["feature_names"]].to_dict("records")]
        err = max(abs(a - b) for a, b in zip(ref, mine))
        print(f"{side}: {booster.num_trees()} trees, {os.path.getsize(out)/1e6:.1f} MB, max abs diff {err:.2e}")
        assert err < 1e-9, "pure-python evaluator disagrees with lightgbm"
        rows = sample[dump["feature_names"]].head(50).to_dict("records")
        ex_err = max(abs(e["base"] + sum(e["contributions"].values()) - p)
                     for e, p in zip((py.explain(r) for r in rows), mine))
        print(f"{side}: explanation max abs diff {ex_err:.2e}")
        assert ex_err < 1e-9, "explanations do not add up to the prediction"
    shutil.copy(os.path.join(ART, "model_meta.json"), os.path.join(DEST, "model_meta.json"))
    report = os.path.join(ART, "training_report.json")  # built by ml/report.py, shown in the admin AI Lab
    if os.path.exists(report):
        shutil.copy(report, os.path.join(DEST, "training_report.json"))
    else:
        print("note: no training_report.json yet; run ml/report.py then export again for the AI Lab charts")
    print(f"exported to {DEST}")


if __name__ == "__main__":
    main()
