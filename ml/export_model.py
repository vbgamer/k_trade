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


KEEP = ("split_feature", "threshold", "default_left", "leaf_value", "decision_type", "missing_type")


def _slim(node):
    """Keep only what the evaluator needs (drops gains, counts, internal values)."""
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
    shutil.copy(os.path.join(ART, "model_meta.json"), os.path.join(DEST, "model_meta.json"))
    print(f"exported to {DEST}")


if __name__ == "__main__":
    main()
