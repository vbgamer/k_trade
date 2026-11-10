"""Feature definitions shared by training (historical data) and live inference (the backend).

Everything here is computed from *today's* 1-minute NIFTY candles plus the ATM option quotes and the
option-chain OI at the decision minute, so the exact same numbers can be produced live from Upstox data.
Keep this file free of pandas so the backend can import it without the training stack.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Dict, List, Optional, Sequence

STRIKE_STEP = 50
FEATURE_NAMES: List[str] = [
    "ret_1", "ret_3", "ret_5", "ret_10", "ret_15", "ret_30",
    "vol_15", "vol_30", "range_15",
    "rsi_14", "ema9_dist", "ema21_dist", "ema9_21",
    "ret_from_open", "gap", "prev_day_ret", "day_range", "pos_in_range",
    "mins_since_open", "weekday", "days_to_expiry", "is_expiry_day",
    "ce_prem", "pe_prem", "straddle", "ce_pe_skew", "straddle_chg_5", "straddle_chg_15",
]
# pcr_near / ce_oi_chg / pe_oi_chg are still computed but NOT model inputs: the historical option data is
# missing open interest for ~65% of 2025-26 rows, so a model using them learns from 2021-24 OI and then
# sees garbage in recent data.


def atm_strike(spot: float, step: int = STRIKE_STEP) -> int:
    return int(round(spot / step) * step)


def _ema(values: Sequence[float], period: int) -> float:
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(closes: Sequence[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def _ret(c: Sequence[float], n: int) -> float:
    if len(c) <= n or c[-1 - n] <= 0:
        return 0.0
    return math.log(c[-1] / c[-1 - n])


def _std(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def compute_features(
    *,
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    minute_of_day: int,          # minutes since midnight IST of the decision bar's close
    today: date,
    expiry: date,
    prev_close: Optional[float],
    prev_day_ret: float,
    ce_prem_hist: Sequence[float],   # ATM CE close per minute today (same strike as now), oldest first
    pe_prem_hist: Sequence[float],
    pcr_near: float,
    ce_oi_chg: float,
    pe_oi_chg: float,
) -> Dict[str, float]:
    """Return the feature dict for the bar that just closed (the last element of the sequences)."""
    c = list(closes)
    spot = c[-1]
    rets = [math.log(c[i] / c[i - 1]) for i in range(max(1, len(c) - 30), len(c)) if c[i - 1] > 0]
    day_hi, day_lo = max(highs), min(lows)
    ema9, ema21 = _ema(c, 9), _ema(c, 21)
    hi15, lo15 = max(highs[-15:]), min(lows[-15:])

    ce, pe = ce_prem_hist[-1], pe_prem_hist[-1]
    straddle = ce + pe

    def _chg(hist_ce, hist_pe, n):
        if len(hist_ce) <= n:
            return 0.0
        old = hist_ce[-1 - n] + hist_pe[-1 - n]
        return (straddle / old - 1.0) if old > 0 else 0.0

    f = {
        "ret_1": _ret(c, 1), "ret_3": _ret(c, 3), "ret_5": _ret(c, 5),
        "ret_10": _ret(c, 10), "ret_15": _ret(c, 15), "ret_30": _ret(c, 30),
        "vol_15": _std(rets[-15:]), "vol_30": _std(rets),
        "range_15": (hi15 - lo15) / spot,
        "rsi_14": _rsi(c[-60:]),
        "ema9_dist": spot / ema9 - 1.0, "ema21_dist": spot / ema21 - 1.0, "ema9_21": ema9 / ema21 - 1.0,
        "ret_from_open": math.log(spot / opens[0]),
        "gap": (opens[0] / prev_close - 1.0) if prev_close else 0.0,
        "prev_day_ret": prev_day_ret,
        "day_range": (day_hi - day_lo) / spot,
        "pos_in_range": (spot - day_lo) / (day_hi - day_lo) if day_hi > day_lo else 0.5,
        "mins_since_open": float(minute_of_day - (9 * 60 + 15)),
        "weekday": float(today.weekday()),
        "days_to_expiry": float((expiry - today).days),
        "is_expiry_day": float(expiry == today),
        "ce_prem": ce / spot, "pe_prem": pe / spot, "straddle": straddle / spot,
        "ce_pe_skew": (ce - pe) / straddle if straddle > 0 else 0.0,
        "straddle_chg_5": _chg(ce_prem_hist, pe_prem_hist, 5),
        "straddle_chg_15": _chg(ce_prem_hist, pe_prem_hist, 15),
        "pcr_near": pcr_near, "ce_oi_chg": ce_oi_chg, "pe_oi_chg": pe_oi_chg,
    }
    return f
