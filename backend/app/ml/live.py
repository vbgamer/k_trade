"""What the deployed model thinks right now, for the admin AI Lab.

Scores the latest closed NIFTY minute exactly as the AI agent would (same features, same models), explains the
prediction feature by feature, and keeps a log of today's minute-by-minute scores.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from app.core.memory_bus import ai_score_log, chain_history, index_daily, market_snapshot
from app.engine import minute_bars as mb
from app.ml import agent

INDEX_KEY = "NSE_INDEX|Nifty 50"
TOP_REASONS = 8

_report: Dict[str, Any] = {}


def load_report() -> Optional[Dict[str, Any]]:
    """Training report written by ml/report.py and copied by ml/export_model.py (None if not exported yet)."""
    if "report" not in _report:
        try:
            with open(os.path.join(agent.MODEL_DIR, "training_report.json"), encoding="utf-8") as f:
                _report["report"] = json.load(f)
        except FileNotFoundError:
            _report["report"] = None
    return _report["report"]


def _reasons(model, features: Dict[str, float]) -> Optional[Dict[str, Any]]:
    if not model.explainable:
        return None
    ex = model.explain(features)
    top = sorted(ex["contributions"].items(), key=lambda kv: -abs(kv[1]))[:TOP_REASONS]
    return {
        "base_pct": round(ex["base"] * 100, 3),
        "reasons": [{"feature": f, "value": features.get(f), "effect_pct": round(c * 100, 3)} for f, c in top],
        "other_pct": round(sum(c for f, c in ex["contributions"].items()
                               if f not in dict(top)) * 100, 3),
    }


def decide(now: datetime, explain: bool = True) -> Dict[str, Any]:
    """Score the last closed minute. Returns {"status": "ok", ...} or {"status": <why not>, "reason": ...}."""
    models = agent.load_models()
    if not models:
        return {"status": "no_model", "reason": "No trained model deployed"}
    expiry_s = market_snapshot.get("expiry")
    if not expiry_s:
        return {"status": "waiting", "reason": "Waiting for the live option chain (connect Upstox)"}
    bars = mb.closed_bars(INDEX_KEY, now.timestamp())
    features, row, why = agent.build_live_features(
        bars=bars, rows=market_snapshot.get("rows", []), chain_hist=chain_history,
        today=now.date(), expiry=date.fromisoformat(expiry_s),
        prev_close=index_daily.get("prev_close") or market_snapshot.get("spot_prev_close"),
        prev_day_ret=float(index_daily.get("prev_day_ret") or 0.0),
    )
    if features is None:
        return {"status": "waiting", "reason": why}
    preds = agent.score(models, features)
    threshold = float(models["meta"]["threshold"])
    side = "CE" if preds["ce"] >= preds["pe"] else "PE"
    best = max(preds["ce"], preds["pe"])
    minute = bars[-1]["minute"]
    in_window = agent.FIRST_DECISION <= minute <= agent.LAST_DECISION
    out = {
        "status": "ok",
        "minute": minute,
        "time": f"{(minute + 1) // 60:02d}:{(minute + 1) % 60:02d}",
        "spot": bars[-1]["close"],
        "strike": int(row["strike"]),
        "ce_ltp": (row.get("ce") or {}).get("ltp"),
        "pe_ltp": (row.get("pe") or {}).get("ltp"),
        "pred_ce_pct": round(preds["ce"] * 100, 3),
        "pred_pe_pct": round(preds["pe"] * 100, 3),
        "threshold_pct": round(threshold * 100, 3),
        "best_side": side,
        "signal": best > threshold,
        "in_window": in_window,
        "decision": (f"BUY {side}" if best > threshold and in_window
                     else "WAIT (outside 09:45-14:30 entry window)" if not in_window
                     else "WAIT (no edge above threshold)"),
    }
    if explain:
        out["features"] = {k: round(v, 6) if isinstance(v, float) else v for k, v in features.items()}
        out["explain"] = {"ce": _reasons(models["ce"], features), "pe": _reasons(models["pe"], features)}
    return out


def record_minute(now: datetime) -> Optional[Dict[str, Any]]:
    """Called at every NIFTY minute close: append today's score point (keeps one point per minute)."""
    if ai_score_log.get("day") != now.date():
        ai_score_log["day"], ai_score_log["points"] = now.date(), []
    d = decide(now, explain=False)
    if d["status"] != "ok":
        return None
    points: List[Dict[str, Any]] = ai_score_log["points"]
    if points and points[-1]["minute"] >= d["minute"]:
        return None
    point = {k: d[k] for k in ("minute", "time", "spot", "pred_ce_pct", "pred_pe_pct", "signal", "in_window")}
    points.append(point)
    return point


def pair_journal(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Turn AI_TRADE_JOURNAL audit rows (oldest first) into round trips: what the model predicted vs what happened."""
    open_by: Dict[tuple, Dict[str, Any]] = {}
    trades: List[Dict[str, Any]] = []
    for r in rows:
        m = r.get("metadata") or {}
        res = m.get("result") or {}
        key = (r.get("user_id"), m.get("instrument_key"))
        if m.get("action") == "BUY":
            if res.get("status") != "FILLED":
                trades.append({"status": "entry rejected", "symbol": r.get("symbol"), "opened_at": r.get("at"),
                               "user": r.get("user"), "side": m.get("side"),
                               "pred_pct": _pct(m.get("pred_ce") if m.get("side") == "CE" else m.get("pred_pe")),
                               "note": res.get("rejection_reason")})
                continue
            t = {"status": "open", "symbol": r.get("symbol"), "user": r.get("user"), "opened_at": r.get("at"),
                 "side": m.get("side"), "entry": res.get("fill_price"),
                 "pred_pct": _pct(m.get("pred_ce") if m.get("side") == "CE" else m.get("pred_pe")),
                 "threshold_pct": _pct(m.get("threshold"))}
            open_by[key] = t
            trades.append(t)
        elif m.get("action") == "SELL" and key in open_by:
            t = open_by.pop(key)
            exit_px = res.get("fill_price") or m.get("exit_ltp")
            t.update(status="closed", closed_at=r.get("at"), exit=exit_px, exit_reason=m.get("exit_reason"))
            if t.get("entry") and exit_px:
                t["actual_pct"] = round((float(exit_px) / float(t["entry"]) - 1) * 100, 2)
                t["model_was_right"] = (t["actual_pct"] > 0) == ((t.get("pred_pct") or 0) > 0)
    return list(reversed(trades))


def _pct(v) -> Optional[float]:
    return round(float(v) * 100, 3) if v is not None else None
