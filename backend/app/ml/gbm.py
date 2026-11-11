"""Pure-Python evaluator for LightGBM models exported with ml/export_model.py.

The server has 1 GB RAM, so instead of installing lightgbm + numpy + scipy we walk the exported trees
directly. One prediction is a few hundred dictionary lookups (well under a millisecond).
"""
from __future__ import annotations

import json
from typing import Dict, List, Sequence


class GBMModel:
    def __init__(self, dump: Dict):
        self.feature_names: List[str] = dump["feature_names"]
        self.trees = [t["tree_structure"] for t in dump["tree_info"]]

    @classmethod
    def load(cls, path: str) -> "GBMModel":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    @staticmethod
    def _walk(node: Dict, x: Sequence[float]) -> float:
        while "leaf_value" not in node:
            v = x[node["split_feature"]]
            missing = node.get("missing_type", "None")
            is_nan = v != v
            if missing == "NaN" and is_nan or missing == "Zero" and (is_nan or v == 0.0):
                go_left = node.get("default_left", True)
            else:
                if is_nan:  # missing_type None: LightGBM treats NaN as 0
                    v = 0.0
                go_left = v <= node["threshold"]
            node = node["left_child"] if go_left else node["right_child"]
        return node["leaf_value"]

    def predict(self, features: Dict[str, float]) -> float:
        x = [float(features.get(n, float("nan"))) for n in self.feature_names]
        return sum(self._walk(t, x) for t in self.trees)

    @property
    def explainable(self) -> bool:
        return bool(self.trees) and "internal_value" in self.trees[0]

    def explain(self, features: Dict[str, float]) -> Dict:
        """Why the model predicted what it did: walk each tree and credit every split's change in expected value to
        the feature it split on (Saabas path attribution). base + sum(contributions) == predict(features)."""
        x = [float(features.get(n, float("nan"))) for n in self.feature_names]
        contrib = [0.0] * len(self.feature_names)
        base = 0.0
        for node in self.trees:
            if "leaf_value" in node:
                base += node["leaf_value"]
                continue
            base += node["internal_value"]
            while "leaf_value" not in node:
                v = x[node["split_feature"]]
                missing = node.get("missing_type", "None")
                is_nan = v != v
                if missing == "NaN" and is_nan or missing == "Zero" and (is_nan or v == 0.0):
                    go_left = node.get("default_left", True)
                else:
                    go_left = (0.0 if is_nan else v) <= node["threshold"]
                child = node["left_child"] if go_left else node["right_child"]
                child_val = child["leaf_value"] if "leaf_value" in child else child["internal_value"]
                contrib[node["split_feature"]] += child_val - node["internal_value"]
                node = child
        return {"base": base, "contributions": dict(zip(self.feature_names, contrib))}
