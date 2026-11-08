# server/strategies/kaushalcustomWMASMACall1/logic.py
from __future__ import annotations

from typing import Dict, Any, Optional, List, Tuple
import os
import time
import importlib.util
from datetime import datetime, timedelta, timezone

# =========================================================
# SAFE IMPORTS
# =========================================================
try:
    from ohlc_data import get_last_n_candles
except Exception:
    from server.ohlc_data import get_last_n_candles  # type: ignore

try:
    from option_chain import get_atm_option_with_greeks_for_index
except Exception:
    from server.option_chain import get_atm_option_with_greeks_for_index  # type: ignore

# Feed snapshot (Redis/SNAP)
try:
    from engine_api import ensure_feed_for, wait_for_snapshot
except Exception:
    from server.engine_api import ensure_feed_for, wait_for_snapshot  # type: ignore

# MTM
try:
    from engine_mtm import compute_strategy_mtm_for_instrument, compute_strategy_mtm_for_today
except Exception:
    try:
        from server.engine_mtm import compute_strategy_mtm_for_instrument, compute_strategy_mtm_for_today  # type: ignore
    except Exception:
        compute_strategy_mtm_for_instrument = None  # type: ignore
        compute_strategy_mtm_for_today = None  # type: ignore

# Models
try:
    from models import db, StrategySubscription
except Exception:
    from server.models import db, StrategySubscription  # type: ignore

# Broker resolution helpers
try:
    from models import BrokerOrder, get_active_broker_for_user
except Exception:
    try:
        from server.models import BrokerOrder, get_active_broker_for_user  # type: ignore
    except Exception:
        BrokerOrder = None  # type: ignore
        get_active_broker_for_user = None  # type: ignore

try:
    from server.strategies._common.common_runtime import (
        get_start_end_times,
        flatten_today_positions,
    )
except Exception:
    try:
        from strategies._common.common_runtime import (  # type: ignore
            get_start_end_times,
            flatten_today_positions,
        )
    except Exception:
        get_start_end_times = None  # type: ignore
        flatten_today_positions = None  # type: ignore


# =========================================================
# Load order helpers from order.py in same folder
# Must match your working contract (broker, atm_option, inputs, ctx)
# ========================================================
_order_path = os.path.join(os.path.dirname(__file__), "order.py")
_spec = importlib.util.spec_from_file_location("kaushalcustomWMASMACall1_order", _order_path)
_order = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader is not None
_spec.loader.exec_module(_order)  # type: ignore[arg-type]

_build_order_intent = _order._build_order_intent
_execute_order_if_possible = _order._execute_order_if_possible
_exit_all_positions_for_user = _order.exit_all_positions_for_user


# =========================================================
# CONFIG
# =========================================================
SLUG = "kaushalcustomWMASMACall1"

UNDERLYING_KEY = "NSE_INDEX|Nifty 50"
UNDERLYING_NAME = "NIFTY"

TIMEFRAME_MIN = 1
DEFAULT_TIMEFRAME_MIN = TIMEFRAME_MIN
ALLOWED_TIMEFRAME_MINS = {1, 3, 5, 15, 60}
WMA_CLOSE_PERIOD = 5
SMA_OPEN_PERIOD = 1
WMA_OFFSET = -1
SMA_OFFSET = -1

STRIKE_STEP = 50

CANDLES_N = 200
IDLE_SLEEP_SEC = 0.5

DEFAULT_QTY = 50
DEFAULT_PRODUCT = "NRML"
DEFAULT_TRADE_MODE = "LIVE"

IST = timezone(timedelta(hours=5, minutes=30))
LOG_PATH = os.path.join(os.path.dirname(__file__), "nifty_atm_wma5_sma1_open_offsetm1_call.log")

# Entry/exit cutoff (IST)
ENTRY_CUTOFF_HH = 15
ENTRY_CUTOFF_MM = 30
EXIT_HH = 15
EXIT_MM = 30


# =========================================================
# TIME HELPERS
# =========================================================
def _now_ist() -> datetime:
    return datetime.now(IST)


def _now_ist_str() -> str:
    return _now_ist().strftime("%Y-%m-%d %H:%M")


def _floor_min(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _next_close_time(now: datetime, tf_min: int) -> datetime:
    """
    Next candle close boundary (tf_min).
    Add +1s so we always fetch a CLOSED candle.
    """
    base = _floor_min(now)
    m = base.minute
    nxt = ((m // tf_min) + 1) * tf_min
    add_h = 0
    if nxt >= 60:
        nxt -= 60
        add_h = 1
    return base.replace(minute=nxt) + timedelta(hours=add_h, seconds=1)


def _coerce_timeframe_min(value: Any, default: int = DEFAULT_TIMEFRAME_MIN) -> int:
    try:
        tf = int(value)
    except Exception:
        return default
    return tf if tf in ALLOWED_TIMEFRAME_MINS else default


def _resolve_timeframe_min(user_id: Optional[int] = None, ctx: Optional[Dict[str, Any]] = None) -> int:
    if isinstance(ctx, dict):
        for key in ("timeframeMin", "timeframe_min", "timeframe"):
            if ctx.get(key) is not None:
                return _coerce_timeframe_min(ctx.get(key))
    if user_id:
        try:
            sub = (
                StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
                .order_by(StrategySubscription.id.desc())
                .first()
            )
            if sub and getattr(sub, "timeframe_min", None) is not None:
                return _coerce_timeframe_min(sub.timeframe_min)
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
    return DEFAULT_TIMEFRAME_MIN


# =========================================================
# LOGGING
# =========================================================
def _log(*args: Any) -> None:
    msg = " ".join(str(a) for a in args)
    try:
        print(msg)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


# =========================================================
# INDICATORS
# =========================================================
def _sma(values: List[float], period: int) -> List[Optional[float]]:
    if not values:
        return []
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0:
        return out
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        out[i] = sum(window) / float(period)
    return out


def _wma(values: List[float], period: int) -> List[Optional[float]]:
    if not values:
        return []
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0:
        return out
    denom = period * (period + 1) / 2.0
    weights = list(range(1, period + 1))
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        wsum = 0.0
        for w, v in zip(weights, window):
            wsum += float(w) * float(v)
        out[i] = wsum / denom
    return out


# =========================================================
# FETCH OHLC (ONLY ON CLOSE)
# =========================================================
def _fetch_candles(instrument_key: str) -> Optional[List[list]]:
    r = get_last_n_candles(
        instrument_key=instrument_key,
        timeframe_minutes=TIMEFRAME_MIN,
        n=CANDLES_N,
        now_ist=_now_ist_str(),
        respect_now_cutoff=True,
    )
    if not (isinstance(r, dict) and r.get("ok")):
        return None
    candles = r.get("candles") or []
    if len(candles) < max(WMA_CLOSE_PERIOD, SMA_OPEN_PERIOD) + 5 + max(abs(WMA_OFFSET), abs(SMA_OFFSET)):
        return None
    return candles  # latest->older


def _parse_candles_chrono(candles_latest_first: List[list]) -> Tuple[List[str], List[float], List[float], List[float], List[float]]:
    c = candles_latest_first[::-1]  # oldest->latest
    ts = [str(x[0]) for x in c]
    o = [float(x[1]) for x in c]
    h = [float(x[2]) for x in c]
    l = [float(x[3]) for x in c]
    cl = [float(x[4]) for x in c]
    return ts, o, h, l, cl


def _compute_indicators(candles_latest_first: List[list]) -> Dict[str, Any]:
    ts, o, h, l, cl = _parse_candles_chrono(candles_latest_first)
    def _compute_heikin_ashi(o_list: List[float], h_list: List[float], l_list: List[float], c_list: List[float]):
        """Return ha_o, ha_h, ha_l, ha_c lists (chrono order: oldest->latest).

        HA rules:
          ha_close = (open + high + low + close) / 4
          ha_open = (prev_ha_open + prev_ha_close) / 2  (first ha_open = (open0 + close0)/2)
          ha_high = max(high, ha_open, ha_close)
          ha_low = min(low, ha_open, ha_close)
        """
        n = len(c_list)
        ha_o: List[float] = [0.0] * n
        ha_h: List[float] = [0.0] * n
        ha_l: List[float] = [0.0] * n
        ha_c: List[float] = [0.0] * n
        if n == 0:
            return ha_o, ha_h, ha_l, ha_c

        ha_c[0] = (o_list[0] + h_list[0] + l_list[0] + c_list[0]) / 4.0
        ha_o[0] = (o_list[0] + c_list[0]) / 2.0
        ha_h[0] = max(h_list[0], ha_o[0], ha_c[0])
        ha_l[0] = min(l_list[0], ha_o[0], ha_c[0])

        for i in range(1, n):
            ha_c[i] = (o_list[i] + h_list[i] + l_list[i] + c_list[i]) / 4.0
            ha_o[i] = (ha_o[i - 1] + ha_c[i - 1]) / 2.0
            ha_h[i] = max(h_list[i], ha_o[i], ha_c[i])
            ha_l[i] = min(l_list[i], ha_o[i], ha_c[i])

        return ha_o, ha_h, ha_l, ha_c

    ha_o, ha_h, ha_l, ha_c = _compute_heikin_ashi(o, h, l, cl)

    # Use Heikin-Ashi close for WMA and Heikin-Ashi open for SMA.
    # Both indicators are read with offset -1, so the current decision uses the prior closed signal values.
    wma_close = _wma(ha_c, WMA_CLOSE_PERIOD)
    sma_open = _sma(ha_o, SMA_OPEN_PERIOD)
    idx = len(cl) - 1
    prev_idx = idx - 1
    wma_idx = idx + WMA_OFFSET
    wma_prev_idx = prev_idx + WMA_OFFSET
    sma_idx = idx + SMA_OFFSET
    sma_prev_idx = prev_idx + SMA_OFFSET
    return {
        "ts_now": ts[idx],
        "open_now": o[idx],
        "high_now": h[idx],
        "low_now": l[idx],
        "close_now": cl[idx],
        "ha_open_now": ha_o[idx],
        "ha_close_now": ha_c[idx],
        "ha_open_prev": ha_o[prev_idx] if prev_idx >= 0 else None,
        "ha_close_prev": ha_c[prev_idx] if prev_idx >= 0 else None,
        "wma_close_now": wma_close[wma_idx] if 0 <= wma_idx < len(wma_close) else None,
        "sma_open_now": sma_open[sma_idx] if 0 <= sma_idx < len(sma_open) else None,
        "ts_prev": ts[prev_idx] if prev_idx >= 0 else None,
        "open_prev": o[prev_idx] if prev_idx >= 0 else None,
        "high_prev": h[prev_idx] if prev_idx >= 0 else None,
        "low_prev": l[prev_idx] if prev_idx >= 0 else None,
        "close_prev": cl[prev_idx] if prev_idx >= 0 else None,
        "wma_close_prev": wma_close[wma_prev_idx] if 0 <= wma_prev_idx < len(wma_close) else None,
        "sma_open_prev": sma_open[sma_prev_idx] if 0 <= sma_prev_idx < len(sma_open) else None,
    }


# =========================================================
# SNAP LTP
# =========================================================
def _get_ltp(instr_key: str) -> Optional[float]:
    try:
        snap = wait_for_snapshot(instr_key, field="ltp", timeout=1.0) or {}
        v = snap.get("ltp")
        return float(v) if v is not None else None
    except Exception:
        return None


# =========================================================
# BROKER RESOLUTION
# =========================================================
def _resolve_broker_for_user(user_id: int, prefs: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Resolve broker for this user in a safe order:
      1) prefs["broker"] if provided and non-empty
      2) StrategySubscription.config_json["broker"] or sub.broker if available
      3) get_active_broker_for_user(user_id)
      4) last BrokerOrder.broker for this strategy (fallback)
    Returns lowercased broker or None.
    """
    # 1) prefs
    try:
        if isinstance(prefs, dict):
            b = (prefs.get("broker") or "").strip()
            if b:
                return str(b).lower()
    except Exception:
        pass

    # 2) subscription stored broker/config_json broker
    try:
        sub = (
            StrategySubscription.query
            .filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .first()
        )
        if sub:
            cfg = getattr(sub, "config_json", None)
            if isinstance(cfg, dict):
                b2 = (cfg.get("broker") or "").strip()
                if b2:
                    return str(b2).lower()

            if getattr(sub, "broker", None):
                b3 = str(sub.broker).strip()
                if b3:
                    return b3.lower()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass

    # 3) active broker resolver
    try:
        active = get_active_broker_for_user(user_id)
        if isinstance(active, (list, tuple)) and active:
            return str(active[0]).lower()
        return str(active).lower() if active else None
    except Exception:
        pass

    # 4) last broker from BrokerOrder (strategy-specific)
    try:
        if BrokerOrder is not None:
            last = (
                BrokerOrder.query
                .filter_by(user_id=user_id, strategy_name=SLUG)
                .order_by(BrokerOrder.id.desc())  # type: ignore[attr-defined]
                .first()
            )
            if last and getattr(last, "broker", None):
                return str(last.broker).lower()
    except Exception:
        pass

    return None


# =========================================================
# PREFS (merge subscription + ctx overrides)
# =========================================================
def _prefs(user_id: int, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "qty": DEFAULT_QTY,
        "trade_mode": DEFAULT_TRADE_MODE,
        "product": DEFAULT_PRODUCT,
        "broker": None,
    }

    # ctx overrides first
    try:
        if isinstance(ctx, dict):
            if ctx.get("qty") is not None:
                out["qty"] = int(ctx["qty"])
            if ctx.get("trade_mode"):
                out["trade_mode"] = str(ctx["trade_mode"]).upper()
            if ctx.get("product"):
                out["product"] = str(ctx["product"]).upper()
            if ctx.get("broker"):
                out["broker"] = str(ctx["broker"]).lower()
    except Exception:
        pass

    # subscription overrides if present
    try:
        sub = (
            StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .first()
        )
        if not sub:
            return out

        if getattr(sub, "qty", None):
            out["qty"] = int(sub.qty)

        cfg = getattr(sub, "config_json", None)
        if isinstance(cfg, dict):
            if cfg.get("qty") is not None:
                out["qty"] = int(cfg["qty"])
            if cfg.get("trade_mode"):
                out["trade_mode"] = str(cfg["trade_mode"]).upper()
            if cfg.get("product"):
                out["product"] = str(cfg["product"]).upper()
            if cfg.get("broker"):
                out["broker"] = str(cfg["broker"]).lower()

        if getattr(sub, "broker", None):
            out["broker"] = str(sub.broker).lower()

        return out

    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return out


def _persist_current_trade_in_sub(user_id: int, opt_key: str, opt_sym: str) -> None:
    try:
        sub = (
            StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .first()
        )
        if not sub:
            return
        sub.instrument_key = opt_key
        sub.instrument = opt_sym
        db.session.add(sub)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _reset_subscription_runtime(user_id: int) -> None:
    try:
        sub = (
            StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .first()
        )
        if not sub:
            return
        sub.mtm = 0
        sub.orders = 0
        sub.pnl = 0
        sub.status = "running"
        try:
            mj = getattr(sub, "meta_json", None) or {}
            if isinstance(mj, dict):
                mj.pop("pending_entry_claim_ts", None)
                sub.meta_json = mj
        except Exception:
            pass
        db.session.add(sub)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _read_persisted_last_exit_ts(user_id: int) -> Optional[str]:
    """Read a persisted last_exit_ts from StrategySubscription.status_reason if present.
    Stored as: "last_exit_ts=<ts>". Return None if not present or on error.
    """
    try:
        sub = (
            StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .first()
        )
        if not sub:
            return None
        sr = getattr(sub, "status_reason", None)
        if not sr or not isinstance(sr, str):
            return None
        if sr.startswith("last_exit_ts="):
            return sr.split("=", 1)[1]
        return None
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def _write_persisted_last_exit_ts(user_id: int, ts: str) -> None:
    """Persist last_exit_ts into StrategySubscription.status_reason as "last_exit_ts=<ts>".
    Swallows DB errors and rolls back on failure.
    """
    try:
        sub = (
            StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .first()
        )
        if not sub:
            return
        sub.status_reason = f"last_exit_ts={ts}"
        db.session.add(sub)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _read_persisted_last_entry_ts(user_id: int) -> Optional[str]:
    """Read a persisted last_entry_ts from StrategySubscription.meta_json if present."""
    try:
        sub = (
            StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .first()
        )
        if not sub:
            return None
        mj = getattr(sub, "meta_json", None) or {}
        if not isinstance(mj, dict):
            return None
        ts = mj.get("last_entry_ts")
        return str(ts) if ts else None
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def _write_persisted_last_entry_ts(user_id: int, ts: str) -> None:
    """Persist last_entry_ts into StrategySubscription.meta_json."""
    try:
        sub = (
            StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .first()
        )
        if not sub:
            return
        mj = getattr(sub, "meta_json", None) or {}
        if not isinstance(mj, dict):
            mj = {}
        mj["last_entry_ts"] = ts
        sub.meta_json = mj
        db.session.add(sub)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _read_pending_entry_claim_ts(user_id: int) -> Optional[str]:
    """Read a persisted in-flight entry claim timestamp from StrategySubscription.meta_json."""
    try:
        sub = (
            StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .first()
        )
        if not sub:
            return None
        mj = getattr(sub, "meta_json", None) or {}
        if not isinstance(mj, dict):
            return None
        ts = mj.get("pending_entry_claim_ts")
        return str(ts) if ts else None
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def _claim_entry_candle(user_id: int, ts: str) -> bool:
    """
    Atomically claim an entry candle before placing an order.
    Returns True only for the runner that successfully reserves the candle.
    """
    try:
        sub = (
            StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .with_for_update()
            .first()
        )
        if not sub:
            return False
        mj = getattr(sub, "meta_json", None) or {}
        if not isinstance(mj, dict):
            mj = {}

        if str(mj.get("last_entry_ts") or "") == ts:
            return False
        if str(mj.get("pending_entry_claim_ts") or "") == ts:
            return False

        mj["pending_entry_claim_ts"] = ts
        sub.meta_json = mj
        db.session.add(sub)
        db.session.commit()
        return True
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return False


def _clear_entry_candle_claim(user_id: int, ts: str) -> None:
    """Clear the in-flight entry claim if it still belongs to the same candle."""
    try:
        sub = (
            StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .first()
        )
        if not sub:
            return
        mj = getattr(sub, "meta_json", None) or {}
        if not isinstance(mj, dict):
            return
        if str(mj.get("pending_entry_claim_ts") or "") != ts:
            return
        mj.pop("pending_entry_claim_ts", None)
        sub.meta_json = mj
        db.session.add(sub)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _finalize_entry_candle_claim(user_id: int, ts: str) -> None:
    """Convert a successful in-flight claim into a completed entry marker."""
    try:
        sub = (
            StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .first()
        )
        if not sub:
            return
        mj = getattr(sub, "meta_json", None) or {}
        if not isinstance(mj, dict):
            mj = {}
        if str(mj.get("pending_entry_claim_ts") or "") == ts:
            mj.pop("pending_entry_claim_ts", None)
        mj["last_entry_ts"] = ts
        sub.meta_json = mj
        db.session.add(sub)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


# =========================================================
# STOP CONTROL
# =========================================================
def _is_strategy_stopped(user_id: int) -> bool:
    try:
        sub = (
            StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .first()
        )
        if not sub:
            return False
        st = str(getattr(sub, "status", "") or "").lower()
        return st in ("stopped", "exited")
    except Exception:
        return False


def _mark_strategy_stopped(user_id: int, reason: str = "") -> None:
    try:
        sub = (
            StrategySubscription.query.filter_by(user_id=user_id, slug=SLUG)
            .order_by(StrategySubscription.id.desc())
            .first()
        )
        if not sub:
            return
        sub.status = "stopped"
        if reason:
            try:
                mj = getattr(sub, "meta_json", None) or {}
                if isinstance(mj, dict):
                    mj["stopped_reason"] = reason
                    sub.meta_json = mj
            except Exception:
                pass
        db.session.add(sub)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


# =========================================================
# ORDER HELPERS
# =========================================================
def _place_order(
    user_id: int,
    broker: str,
    side: str,  # "BUY" or "SELL"
    option_key: str,
    option_symbol: str,
    qty: int,
    product: str,
    trade_mode: str,
    meta: Dict[str, Any],
    option_ltp: Optional[float] = None,
) -> Dict[str, Any]:
    atm_option = {
        "instrument_key": option_key,
        "trading_symbol": option_symbol or option_key,
        "ltp": option_ltp,
    }

    inputs = {
        "order": {
            "side": side,
            "qty": int(qty),
            "product": str(product).upper(),
            "order_type": "MARKET",
            "validity": "DAY",
        }
    }

    ctx = {
        "user_id": int(user_id),
        "broker": str(broker).lower(),
        "trade_mode": str(trade_mode).upper(),
        "strategy_name": SLUG,
        "strategy_slug": SLUG,
        "meta": meta,
        "paper_fill_price": option_ltp,
    }

    intent = _build_order_intent(
        broker=str(broker).lower(),
        atm_option=atm_option,
        inputs=inputs,
        ctx=ctx,
    )
    return _execute_order_if_possible(intent, ctx=ctx)


def _enter_buy_option(
    user_id: int,
    broker: str,
    prefs: Dict[str, Any],
    meta: Dict[str, Any],
    preset_opt: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    opt = preset_opt or get_atm_option_with_greeks_for_index(
        index_instrument_key=UNDERLYING_KEY,
        label=UNDERLYING_NAME,
        side="CE",
        strike_step=STRIKE_STEP,
    )
    if not opt:
        return {"ok": False, "error": "option_select_failed"}

    option_key = opt.get("instrument_key") or opt.get("instrumentKey")
    option_sym = opt.get("trading_symbol") or opt.get("tradingSymbol") or opt.get("symbol") or option_key
    if not option_key:
        return {"ok": False, "error": "missing_option_instrument_key"}

    _persist_current_trade_in_sub(user_id, option_key, option_sym or option_key)

    try:
        ensure_feed_for(option_key, option_sym or option_key, mode="full")
    except Exception:
        pass

    qty = int(prefs.get("qty") or DEFAULT_QTY)
    product = str(prefs.get("product") or DEFAULT_PRODUCT).upper()
    trade_mode = str(prefs.get("trade_mode") or DEFAULT_TRADE_MODE).upper()

    opt_ltp = _get_ltp(option_key)

    res = _place_order(
        user_id=user_id,
        broker=broker,
        side="BUY",
        option_key=option_key,
        option_symbol=option_sym or option_key,
        qty=qty,
        product=product,
        trade_mode=trade_mode,
        meta=meta,
        option_ltp=opt_ltp,
    )

    return {
        "ok": bool(res.get("executed")),
        "option_key": option_key,
        "option_sym": option_sym or option_key,
        "raw": res,
    }


def _exit_sell(
    user_id: int,
    broker: str,
    current_option_key: str,
    current_option_sym: str,
    prefs: Dict[str, Any],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    qty = int(prefs.get("qty") or DEFAULT_QTY)
    product = str(prefs.get("product") or DEFAULT_PRODUCT).upper()
    trade_mode = str(prefs.get("trade_mode") or DEFAULT_TRADE_MODE).upper()

    opt_ltp = _get_ltp(current_option_key)

    res = _place_order(
        user_id=user_id,
        broker=broker,
        side="SELL",
        option_key=current_option_key,
        option_symbol=current_option_sym or current_option_key,
        qty=qty,
        product=product,
        trade_mode=trade_mode,
        meta=meta,
        option_ltp=opt_ltp,
    )
    return {"ok": bool(res.get("executed")), "raw": res}


# =========================================================
# REQUIRED entrypoint
# =========================================================
def run(ctx: Dict[str, Any]) -> Dict[str, Any]:
    user_id = int(ctx.get("user_id") or 0) if isinstance(ctx, dict) else 0
    tf_min = _resolve_timeframe_min(user_id=user_id, ctx=ctx)
    return {
        "ok": True,
        "armed": True,
        "tf_min": tf_min,
        "logic": (
            "NIFTY ATM option OHLC using Heikin Ashi candles; WMA(5) on HA close with offset -1 and SMA(1) on HA open with offset -1. "
            "Signal compares WMA(5-HA-close, offset -1) vs SMA(1-HA-open, offset -1) with strict crossover logic: "
            "If WMA(5-HA-close, offset -1) crosses above SMA(1-HA-open, offset -1) -> BUY (if not in trade). "
            "If WMA(5-HA-close, offset -1) crosses below SMA(1-HA-open, offset -1) -> EXIT (if in trade). "
            "No new entries after 15:30 IST; any open position exits at 15:30 IST."
        ),
    }


# =========================================================
# IMPORTANT: must match strategies_routes.py which calls:
#   logic_module.stream_forever(user_id=user_id, ctx=...)
# =========================================================
def stream_forever(user_id: int, ctx: Optional[Dict[str, Any]] = None) -> None:
    global TIMEFRAME_MIN
    TIMEFRAME_MIN = _resolve_timeframe_min(user_id=user_id, ctx=ctx)
    prefs = _prefs(user_id, ctx=ctx)
    _reset_subscription_runtime(user_id)

    # broker preference: ctx/subscription, else active broker resolver
    broker = (prefs.get("broker") or "").strip().lower() if isinstance(prefs.get("broker"), str) else ""
    if not broker:
        broker = _resolve_broker_for_user(user_id, prefs=prefs) or ""

    _log(f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] stream_forever started uid={user_id} broker={broker} tf={TIMEFRAME_MIN}m")

    # State
    in_trade: bool = False
    position_side: Optional[str] = None  # "LONG" or "SHORT"
    current_option_key: Optional[str] = None
    current_option_sym: Optional[str] = None
    # last_exit_ts: store the ts of the candle where we last exited a position.
    # This prevents immediate re-entry in the same candle across restarts/duplicate runners.
    # initialize from persisted subscription state (if any)
    last_exit_ts: Optional[str] = _read_persisted_last_exit_ts(user_id)
    last_entry_ts: Optional[str] = _read_persisted_last_entry_ts(user_id)
    last_processed_candle_ts: Optional[str] = None

    next_ohlc_at = _next_close_time(_now_ist(), TIMEFRAME_MIN)

    while True:
        try:
            # HARD STOP
            if _is_strategy_stopped(user_id):
                _log(f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] Strategy STOPPED/EXITED for user={user_id}. Exiting stream_forever.")
                break

            # keep broker fresh if user changed broker state
            if not broker:
                broker = _resolve_broker_for_user(user_id, prefs=prefs) or ""
                if broker:
                    _log(f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] broker resolved dynamically: {broker}")

            now = _now_ist()
            if get_start_end_times:
                start_time, end_time = get_start_end_times(user_id, SLUG, StrategySubscription, IST)
            else:
                start_time, end_time = None, None

            if end_time and now >= end_time:
                _log(
                    f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] End time reached ({end_time.strftime('%Y-%m-%d %H:%M %z')}). "
                    "Auto-exit + stop."
                )
                today_only = str(prefs.get("strategy_style") or "intraday").lower() != "carry"
                if not broker:
                    broker = _resolve_broker_for_user(user_id, prefs=prefs) or ""
                if not broker:
                    _log("[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] broker not resolved; cannot AUTO-EXIT order at end time")
                else:
                    if in_trade and current_option_key and current_option_sym:
                        ex = _exit_sell(
                            user_id=user_id,
                            broker=broker,
                            current_option_key=current_option_key,
                            current_option_sym=current_option_sym,
                            prefs=prefs,
                            meta={"exit_reason": "time_end", "tf_min": TIMEFRAME_MIN},
                        )
                        _log("[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] END-TIME EXIT res=", ex)
                    if flatten_today_positions:
                        flatten_today_positions(
                            user_id=user_id,
                            broker=broker,
                            prefs=prefs,
                            slug=SLUG,
                            timeframe_min=TIMEFRAME_MIN,
                            BrokerOrder=BrokerOrder,
                            db=db,
                            exit_all_positions_for_user=_exit_all_positions_for_user,
                            now_ist_func=_now_ist,
                            today_only=today_only,
                            log_prefix="[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] ",
                        )
                _mark_strategy_stopped(user_id, reason="time_end")
                _log("[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] Strategy STOPPED after end time. Exiting stream_forever.")
                break

            if start_time and now < start_time:
                time.sleep(IDLE_SLEEP_SEC)
                continue

            entry_cutoff_dt = datetime.combine(now.date(), datetime.min.time()).replace(
                hour=ENTRY_CUTOFF_HH, minute=ENTRY_CUTOFF_MM, tzinfo=IST
            )
            exit_cutoff_dt = datetime.combine(now.date(), datetime.min.time()).replace(
                hour=EXIT_HH, minute=EXIT_MM, tzinfo=IST
            )

            # ------------------------------------------------
            # Time cutoff: no new trades after 15:30 IST,
            # and exit any open position at/after 15:30 IST.
            # ------------------------------------------------
            if now >= exit_cutoff_dt:
                if in_trade and current_option_key and current_option_sym:
                    try:
                        _log(
                            f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL][EXIT] time_cutoff ts={now.isoformat()} "
                            f"option={current_option_sym or current_option_key}"
                        )
                    except Exception:
                        pass
                    if not broker:
                        _log("[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] broker not resolved; cannot EXIT on cutoff")
                    else:
                        ex = _exit_sell(
                            user_id=user_id,
                            broker=broker,
                            current_option_key=current_option_key,
                            current_option_sym=current_option_sym,
                            prefs=prefs,
                            meta={
                                "signal": "time_cutoff_exit",
                                "ts": now.isoformat(),
                                "tf_min": TIMEFRAME_MIN,
                            },
                        )
                        _log("[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] EXIT (cutoff) res=", ex)
                        if ex.get("ok"):
                            in_trade = False
                            position_side = None
                            current_option_key = None
                            current_option_sym = None
                            try:
                                last_exit_ts = now.isoformat()
                                _write_persisted_last_exit_ts(user_id, last_exit_ts)
                            except Exception:
                                pass

                time.sleep(IDLE_SLEEP_SEC)
                continue

            if now >= next_ohlc_at:
                next_ohlc_at = _next_close_time(_now_ist(), TIMEFRAME_MIN)

                # Determine which option to use for OHLC
                opt_key = current_option_key
                opt_sym = current_option_sym
                if not opt_key:
                    atm = get_atm_option_with_greeks_for_index(
                        index_instrument_key=UNDERLYING_KEY,
                        label=UNDERLYING_NAME,
                        side="CE",
                        strike_step=STRIKE_STEP,
                    )
                    if not atm:
                        time.sleep(0.25)
                        continue
                    opt_key = atm.get("instrument_key") or atm.get("instrumentKey")
                    opt_sym = atm.get("trading_symbol") or atm.get("tradingSymbol") or atm.get("symbol") or opt_key

                if not opt_key:
                    time.sleep(0.25)
                    continue

                candles = _fetch_candles(opt_key)
                if not candles:
                    time.sleep(0.25)
                    continue
                ind = _compute_indicators(candles)
                wma_close_now = ind.get("wma_close_now")
                wma_close_prev = ind.get("wma_close_prev")
                sma_open_now = ind.get("sma_open_now")
                sma_open_prev = ind.get("sma_open_prev")
                high_now = float(ind["high_now"])
                close_now = float(ind["close_now"])
                ts_now = str(ind["ts_now"])

                if last_processed_candle_ts == ts_now:
                    try:
                        _log(f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] skipping duplicate candle ts={ts_now}")
                    except Exception:
                        pass
                    time.sleep(IDLE_SLEEP_SEC)
                    continue
                last_processed_candle_ts = ts_now

                try:
                    _log(
                        f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL][OPTION_OHLC] "
                        f"tf={TIMEFRAME_MIN}m "
                        f"option={opt_sym or opt_key} "
                        f"ts={ts_now} "
                        f"O={ind['open_now']:.2f} "
                        f"H={ind['high_now']:.2f} "
                        f"L={ind['low_now']:.2f} "
                        f"C={close_now:.2f}"
                    )
                    _log(
                        f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL][CLOSE] ts={ts_now} "
                        f"option={opt_sym or opt_key} key={opt_key} "
                        f"O={ind['open_now']:.2f} H={ind['high_now']:.2f} L={ind['low_now']:.2f} C={close_now:.2f} "
                        f"wma5_ha_close_offset_m1={wma_close_now if wma_close_now is not None else 'NA'} "
                        f"sma1_ha_open_offset_m1={sma_open_now if sma_open_now is not None else 'NA'} "
                        f"in_trade={in_trade} pos={position_side}"
                    )
                except Exception:
                    pass

                if (
                    wma_close_now is not None
                    and sma_open_now is not None
                    and wma_close_prev is not None
                    and sma_open_prev is not None
                ):
                    wma_close_val = float(wma_close_now)
                    sma_open_val = float(sma_open_now)
                    wma_close_prev_val = float(wma_close_prev)
                    sma_open_prev_val = float(sma_open_prev)
                    bullish_cross = (wma_close_prev_val <= sma_open_prev_val) and (wma_close_val > sma_open_val)
                    bearish_cross = (wma_close_prev_val >= sma_open_prev_val) and (wma_close_val < sma_open_val)
                else:
                    bullish_cross = False
                    bearish_cross = False
                try:
                    _log(
                        f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL][CROSSCHK] ts={ts_now} "
                        f"prev(wma5_ha_close_offset_m1={wma_close_prev} sma1_ha_open_offset_m1={sma_open_prev}) "
                        f"now(wma5_ha_close_offset_m1={wma_close_now} sma1_ha_open_offset_m1={sma_open_now}) "
                        f"bullish={bullish_cross} bearish={bearish_cross}"
                    )
                except Exception:
                    pass

                exited_this_candle = False

                # Exit on opposite crossover
                if in_trade and current_option_key and current_option_sym:
                    if position_side == "LONG" and bearish_cross:
                        try:
                            persisted_exit = _read_persisted_last_exit_ts(user_id)
                            if persisted_exit:
                                last_exit_ts = persisted_exit
                            if last_exit_ts is not None and last_exit_ts == ts_now:
                                _log(f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] skipping duplicate exit ts={ts_now} because last_exit_ts={last_exit_ts}")
                                time.sleep(IDLE_SLEEP_SEC)
                                continue
                        except Exception:
                            pass
                        try:
                            _log(
                                f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL][EXIT] bearish_cross ts={ts_now} "
                                f"prev(wma5_ha_close_offset_m1={wma_close_prev} sma1_ha_open_offset_m1={sma_open_prev}) "
                                f"now(wma5_ha_close_offset_m1={wma_close_now} sma1_ha_open_offset_m1={sma_open_now})"
                            )
                        except Exception:
                            pass
                        if not broker:
                            _log("[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] broker not resolved; cannot EXIT long")
                        else:
                            ex = _exit_sell(
                                user_id=user_id,
                                broker=broker,
                                current_option_key=current_option_key,
                                current_option_sym=current_option_sym,
                                prefs=prefs,
                                meta={
                                    "signal": "bearish_crossover_exit",
                                    "ts": ts_now,
                                    "tf_min": TIMEFRAME_MIN,
                                },
                            )
                            _log("[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] EXIT LONG res=", ex)
                            if ex.get("ok"):
                                in_trade = False
                                position_side = None
                                current_option_key = None
                                current_option_sym = None
                                # record last exit timestamp to prevent same-candle re-entry
                                try:
                                    last_exit_ts = ts_now
                                    # persist across processes
                                    _write_persisted_last_exit_ts(user_id, last_exit_ts)
                                    _log(f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] last_exit_ts set={last_exit_ts} after EXIT LONG (persisted)")
                                    # enhanced exit diagnostics
                                    try:
                                        raw = ex.get("raw") or {}
                                        fill_price = None
                                        if isinstance(raw, dict):
                                            fill_price = raw.get("fill_price")
                                            order_blob = raw.get("order") or {}
                                            fill_price = fill_price or order_blob.get("filled_price") or order_blob.get("fill_price")
                                            resp = raw.get("response") or {}
                                            if isinstance(resp, dict):
                                                fill_price = fill_price or resp.get("fill_price") or resp.get("filled_price")

                                        order_id = None
                                        if isinstance(raw, dict):
                                            order_id = raw.get("paper_order_id") or raw.get("paper_order_id")
                                            if not order_id:
                                                resp = raw.get("response") or {}
                                                if isinstance(resp, dict):
                                                    order_id = resp.get("order_id") or resp.get("orderId") or resp.get("id")

                                        snapshot_ltp = None
                                        try:
                                            snapshot_ltp = _get_ltp(current_option_key) if current_option_key else None
                                        except Exception:
                                            snapshot_ltp = None

                                        wall_ts = _now_ist().isoformat()
                                        _log(
                                            f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL][FILL][EXIT] candle_ts={ts_now} wall_ts={wall_ts} "
                                            f"option={current_option_sym or current_option_key} fill_price={fill_price} "
                                            f"order_id={order_id} snapshot_ltp={snapshot_ltp} raw={str(raw)[:400]}"
                                        )
                                    except Exception:
                                        pass
                                except Exception:
                                    pass
                                exited_this_candle = True

                # If in_trade or just exited, do not enter on the same candle
                if in_trade or exited_this_candle:
                    time.sleep(IDLE_SLEEP_SEC)
                    continue

                # Enter on bullish crossover (no breakout logic)
                if bullish_cross:
                    # guard: if we exited on this same candle (or last_exit_ts == ts_now), skip re-entry
                    try:
                        # refresh persisted last_exit_ts so other processes' exits are respected
                        persisted = _read_persisted_last_exit_ts(user_id)
                        if persisted:
                            last_exit_ts = persisted
                        if last_exit_ts is not None and last_exit_ts == ts_now:
                            _log(f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] skipping bullish entry ts={ts_now} because last_exit_ts={last_exit_ts}")
                            time.sleep(IDLE_SLEEP_SEC)
                            continue
                        persisted_entry = _read_persisted_last_entry_ts(user_id)
                        if persisted_entry:
                            last_entry_ts = persisted_entry
                        if last_entry_ts is not None and last_entry_ts == ts_now:
                            _log(f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] skipping duplicate bullish entry ts={ts_now} because last_entry_ts={last_entry_ts}")
                            time.sleep(IDLE_SLEEP_SEC)
                            continue
                        pending_claim_ts = _read_pending_entry_claim_ts(user_id)
                        if pending_claim_ts is not None and pending_claim_ts == ts_now:
                            _log(f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] skipping bullish entry ts={ts_now} because pending_entry_claim_ts={pending_claim_ts}")
                            time.sleep(IDLE_SLEEP_SEC)
                            continue
                    except Exception:
                        pass
                    if now >= entry_cutoff_dt:
                        _log(
                            f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] entry cutoff reached {entry_cutoff_dt.strftime('%H:%M')} IST; skipping entry."
                        )
                        time.sleep(IDLE_SLEEP_SEC)
                        continue
                    try:
                        ensure_feed_for(opt_key, opt_sym or opt_key, mode="full")
                    except Exception:
                        pass
                    if not _claim_entry_candle(user_id, ts_now):
                        _log(f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] entry claim rejected for ts={ts_now}; another runner likely owns it")
                        time.sleep(IDLE_SLEEP_SEC)
                        continue
                    _log(f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] attempting ENTRY LONG ts={ts_now} last_exit_ts={last_exit_ts}")
                    _log(
                        f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL][ENTRY] bullish_cross ts={ts_now} "
                        f"prev(wma5_ha_close_offset_m1={wma_close_prev} sma1_ha_open_offset_m1={sma_open_prev}) "
                        f"now(wma5_ha_close_offset_m1={wma_close_now} sma1_ha_open_offset_m1={sma_open_now}) "
                        f"option={opt_sym or opt_key}"
                    )
                    if not broker:
                        _log("[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] broker not resolved; cannot ENTER long")
                        _clear_entry_candle_claim(user_id, ts_now)
                    else:
                        preset = {
                            "instrument_key": opt_key,
                            "trading_symbol": opt_sym or opt_key,
                        }
                        ent = _enter_buy_option(
                            user_id=user_id,
                            broker=broker,
                            prefs=prefs,
                            meta={
                                "signal": "bullish_crossover_entry",
                                "ts": ts_now,
                                "tf_min": TIMEFRAME_MIN,
                            },
                            preset_opt=preset,
                        )
                        _log("[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] ENTRY LONG res=", ent)
                        if ent.get("ok") and ent.get("option_key"):
                            in_trade = True
                            position_side = "LONG"
                            current_option_key = ent.get("option_key")
                            current_option_sym = ent.get("option_sym") or ent.get("option_key")
                            try:
                                last_entry_ts = ts_now
                                _finalize_entry_candle_claim(user_id, last_entry_ts)
                            except Exception:
                                pass
                            # enhanced fill diagnostics
                            try:
                                raw = ent.get("raw") or {}
                                # Try common locations for fill price and order id
                                fill_price = raw.get("fill_price") if isinstance(raw, dict) else None
                                if fill_price is None and isinstance(raw, dict):
                                    order_blob = raw.get("order") or {}
                                    fill_price = order_blob.get("filled_price") or order_blob.get("fill_price")
                                if fill_price is None and isinstance(raw, dict):
                                    resp = raw.get("response") or {}
                                    if isinstance(resp, dict):
                                        fill_price = resp.get("fill_price") or resp.get("filled_price")

                                order_id = None
                                if isinstance(raw, dict):
                                    order_id = raw.get("paper_order_id") or raw.get("paper_order_id")
                                    if not order_id:
                                        resp = raw.get("response") or {}
                                        if isinstance(resp, dict):
                                            order_id = resp.get("order_id") or resp.get("orderId") or resp.get("id")

                                snapshot_ltp = None
                                try:
                                    snapshot_ltp = _get_ltp(current_option_key) if current_option_key else None
                                except Exception:
                                    snapshot_ltp = None

                                wall_ts = _now_ist().isoformat()
                                _log(
                                    f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL][FILL][ENTRY] candle_ts={ts_now} wall_ts={wall_ts} "
                                    f"option={current_option_sym or current_option_key} fill_price={fill_price} "
                                    f"order_id={order_id} snapshot_ltp={snapshot_ltp} raw={str(raw)[:400]}"
                                )
                            except Exception:
                                pass
                        else:
                            _clear_entry_candle_claim(user_id, ts_now)

            # ------------------------------------------------
            # MTM update while in trade
            # ------------------------------------------------
            if in_trade and current_option_key:
                ltp_opt = _get_ltp(current_option_key)
                if ltp_opt is not None and compute_strategy_mtm_for_instrument:
                    try:
                        try:
                            _log(
                                f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL][MTM] ts={_now_ist_str()} "
                                f"option={current_option_sym or current_option_key} ltp={ltp_opt}"
                            )
                        except Exception:
                            pass
                        compute_strategy_mtm_for_instrument(
                            strategy_slug=SLUG,
                            instrument_key=current_option_key,
                            ltp=float(ltp_opt),
                            user_id=user_id,
                            today_only=True,
                            update_subscription=True,
                        )
                    except Exception:
                        pass

                # MTM-based auto-exit (max_profit / max_loss) like TrendFusion
                if ltp_opt is not None and compute_strategy_mtm_for_today:
                    try:
                        mtm = compute_strategy_mtm_for_today(
                            strategy_slug=SLUG,
                            user_id=int(user_id),
                            ltps={current_option_key: float(ltp_opt)},
                            today_only=True,
                            update_subscription=True,
                        )

                        sub = (
                            StrategySubscription.query
                            .filter_by(user_id=int(user_id), slug=SLUG)
                            .order_by(StrategySubscription.id.desc())
                            .first()
                        )

                        max_profit = float((getattr(sub, "max_profit", None) or 0.0) if sub else 0.0)
                        max_loss = float((getattr(sub, "max_loss", None) or 0.0) if sub else 0.0)

                        trade_mode = str(prefs.get("trade_mode") or "LIVE").upper()
                        live_mtm = float((mtm or {}).get("live_mtm") or 0.0)
                        paper_mtm = float((mtm or {}).get("paper_mtm") or 0.0)
                        mtm_check = live_mtm if trade_mode == "LIVE" else paper_mtm

                        hit_profit = (max_profit > 0) and (mtm_check >= max_profit)
                        hit_loss = (max_loss > 0) and (mtm_check <= -abs(max_loss))

                        if hit_profit or hit_loss:
                            reason = "profit" if hit_profit else "loss"
                            _log(
                                f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] AUTO-EXIT {reason.upper()} "
                                f"mode={trade_mode} mtm={mtm_check:.2f} maxP={max_profit:.2f} maxL={max_loss:.2f}"
                            )

                            if current_option_key and current_option_sym:
                                if not broker:
                                    _log("[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] broker not resolved; cannot AUTO-EXIT order")
                                else:
                                    ex = _exit_sell(
                                        user_id=user_id,
                                        broker=broker,
                                        current_option_key=current_option_key,
                                        current_option_sym=current_option_sym,
                                        prefs=prefs,
                                        meta={
                                            "exit_reason": f"auto_{reason}",
                                            "tf_min": TIMEFRAME_MIN,
                                            "trade_mode": trade_mode,
                                        },
                                    )
                                    _log("[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] AUTO-EXIT res=", ex)

                            _mark_strategy_stopped(user_id, reason=f"auto_{reason}")
                            _log("[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] Strategy STOPPED after auto-exit. Exiting stream_forever.")
                            break

                    except Exception:
                        try:
                            db.session.rollback()
                        except Exception:
                            pass

            time.sleep(IDLE_SLEEP_SEC)

        except Exception as e:
            try:
                _log(f"[NIFTY_ATM_WMA5_SMA1_OPEN_OFFSETM1_CALL] loop error: {e}")
            except Exception:
                pass
            time.sleep(1.0)
