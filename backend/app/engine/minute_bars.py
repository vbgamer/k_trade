"""IST-minute-aligned bars built from polled quotes (the ML agent needs bars identical to its training data:
09:15, 09:16, ... each covering exactly one exchange minute)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from app.core.memory_bus import chain_history, minute_bars
from app.services.upstox_session import IST

MAX_BARS = 400  # a full session is 375 one-minute bars

_state: Dict[str, Any] = {"day": None}


def ist_minute(now_epoch: float) -> tuple:
    t = datetime.fromtimestamp(now_epoch, IST)
    return t.date(), t.hour * 60 + t.minute


def reset_if_new_day(today: date) -> bool:
    if _state["day"] != today:
        _state["day"] = today
        minute_bars.clear()
        chain_history.clear()
        return True
    return False


def update(key: str, price: float, now_epoch: float) -> Optional[Dict[str, Any]]:
    """Fold a price into the key's current minute bar. Returns the previous bar when a new minute starts."""
    today, minute = ist_minute(now_epoch)
    reset_if_new_day(today)
    bars = minute_bars.setdefault(key, [])
    if bars and bars[-1]["minute"] == minute:
        b = bars[-1]
        b["high"] = max(b["high"], price)
        b["low"] = min(b["low"], price)
        b["close"] = price
        return None
    closed = bars[-1] if bars and bars[-1]["minute"] < minute else None
    bars.append({"minute": minute, "open": price, "high": price, "low": price, "close": price})
    if len(bars) > MAX_BARS:
        del bars[: len(bars) - MAX_BARS]
    return closed


def closed_bars(key: str, now_epoch: float) -> List[Dict[str, Any]]:
    """Completed bars for today (excludes the minute still forming)."""
    _, minute = ist_minute(now_epoch)
    return [b for b in minute_bars.get(key, []) if b["minute"] < minute]


def backfill(key: str, candles: List[Dict[str, Any]], now_epoch: float) -> int:
    """Merge historical 1-minute candles ({"minute", open, high, low, close}) for today ahead of live bars."""
    today, minute = ist_minute(now_epoch)
    reset_if_new_day(today)
    have = {b["minute"] for b in minute_bars.get(key, [])}
    # keep only bar fields: API candles also carry datetime objects, which must never reach JSON publishing
    extra = [{k: c[k] for k in ("minute", "open", "high", "low", "close")}
             for c in candles if c["minute"] < minute and c["minute"] not in have]
    if extra:
        minute_bars[key] = sorted(minute_bars.get(key, []) + extra, key=lambda b: b["minute"])[-MAX_BARS:]
    return len(extra)


def record_chain(minute: int, rows: List[Dict[str, Any]]) -> None:
    """Remember each strike's CE/PE premium at a minute close (used for straddle-change features)."""
    for row in rows:
        ce, pe = row.get("ce") or {}, row.get("pe") or {}
        if ce.get("ltp") and pe.get("ltp"):
            chain_history.setdefault(int(row["strike"]), {})[minute] = (float(ce["ltp"]), float(pe["ltp"]))
