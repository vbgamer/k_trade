"""Live side of the ML trading agent: builds the training features from live data and scores them.

Pure functions over the in-memory market state so they can be unit-tested without a market feed.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from app.ml.features import STRIKE_STEP, atm_strike, compute_features
from app.ml.gbm import GBMModel

logger = logging.getLogger("ml_agent")

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
OPEN_MINUTE = 9 * 60 + 15
FIRST_DECISION = 9 * 60 + 45   # bar start minute; mirrors ml/build_dataset.py
LAST_DECISION = 14 * 60 + 30
SQUARE_OFF = 15 * 60 + 15
NEAR_STRIKES = 3

_cache: Dict[str, Any] = {}


def load_models() -> Optional[Dict[str, Any]]:
    """Load the exported models once per process. Returns None if no model has been exported yet."""
    if "models" in _cache:
        return _cache["models"]
    try:
        with open(os.path.join(MODEL_DIR, "model_meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        models = {
            "meta": meta,
            "ce": GBMModel.load(os.path.join(MODEL_DIR, "entry_ce.json")),
            "pe": GBMModel.load(os.path.join(MODEL_DIR, "entry_pe.json")),
        }
        logger.info(f"Loaded ML entry models (exit rule {meta.get('exit_rule')}, threshold {meta['threshold']:.4f})")
    except FileNotFoundError:
        logger.warning("No exported ML model found in app/ml/models; the AI agent will stay idle.")
        models = None
    _cache["models"] = models
    return models


def _row_for(rows: List[Dict[str, Any]], strike: int) -> Optional[Dict[str, Any]]:
    return next((r for r in rows if int(r["strike"]) == strike), None)


def _oi_change(leg: Dict[str, Any]) -> float:
    oi, prev = leg.get("oi"), leg.get("prev_oi")
    return (float(oi) / float(prev) - 1.0) if oi and prev else 0.0


def build_live_features(
    *,
    bars: List[Dict[str, Any]],
    rows: List[Dict[str, Any]],
    chain_hist: Dict[int, Dict[int, Tuple[float, float]]],
    today: date,
    expiry: date,
    prev_close: Optional[float],
    prev_day_ret: float,
) -> Tuple[Optional[Dict[str, float]], Optional[Dict[str, Any]], str]:
    """Return (features, atm_row, reason). features is None (with a reason) when the data is not ready."""
    if not bars or bars[0]["minute"] != OPEN_MINUTE:
        return None, None, "waiting for a full day of NIFTY bars from 09:15"
    if len(bars) < 31:
        return None, None, "warming up (needs 30 minutes of bars)"
    spot = bars[-1]["close"]
    k = atm_strike(spot)
    row = _row_for(rows, k)
    ce, pe = (row or {}).get("ce") or {}, (row or {}).get("pe") or {}
    if not ce.get("ltp") or not pe.get("ltp"):
        return None, None, f"no live premium for ATM strike {k}"

    # premium history of this strike, oldest first, filling gaps with the current premium
    hist = chain_hist.get(k, {})
    minutes = [b["minute"] for b in bars]
    ce_hist = [hist.get(m, (None, None))[0] or float(ce["ltp"]) for m in minutes]
    pe_hist = [hist.get(m, (None, None))[1] or float(pe["ltp"]) for m in minutes]
    ce_hist[-1], pe_hist[-1] = float(ce["ltp"]), float(pe["ltp"])

    oi_ce = oi_pe = 0.0
    for j in range(-NEAR_STRIKES, NEAR_STRIKES + 1):
        r = _row_for(rows, k + j * STRIKE_STEP) or {}
        oi_ce += float((r.get("ce") or {}).get("oi") or 0)
        oi_pe += float((r.get("pe") or {}).get("oi") or 0)

    f = compute_features(
        opens=[b["open"] for b in bars], highs=[b["high"] for b in bars],
        lows=[b["low"] for b in bars], closes=[b["close"] for b in bars],
        minute_of_day=bars[-1]["minute"] + 1, today=today, expiry=expiry,
        prev_close=prev_close, prev_day_ret=prev_day_ret,
        ce_prem_hist=ce_hist, pe_prem_hist=pe_hist,
        pcr_near=oi_pe / oi_ce if oi_ce > 0 else 1.0,
        ce_oi_chg=_oi_change(ce), pe_oi_chg=_oi_change(pe),
    )
    return f, row, "ok"


def score(models: Dict[str, Any], features: Dict[str, float]) -> Dict[str, float]:
    return {"ce": models["ce"].predict(features), "pe": models["pe"].predict(features)}


def exit_reason(
    *, entry_price: float, bar: Optional[Dict[str, Any]], ltp: Optional[float],
    held_minutes: int, minute_now: int, sim: Dict[str, Any],
) -> Optional[str]:
    """Same exit rules the model was trained with: stop first, then target, time limit, 15:15 square-off."""
    if minute_now >= SQUARE_OFF:
        return "squareoff"
    lows = [v for v in ((bar or {}).get("low"), ltp) if v is not None]
    highs = [v for v in ((bar or {}).get("high"), ltp) if v is not None]
    lo, hi = (min(lows) if lows else None), (max(highs) if highs else None)
    if lo is not None and lo <= entry_price * (1 - sim["stop_pct"]):
        return "stop"
    if hi is not None and hi >= entry_price * (1 + sim["target_pct"]):
        return "target"
    if held_minutes >= sim["max_hold_min"]:
        return "time"
    return None
