"""AI Options Agent: ML-scored NIFTY ATM option buying with fixed exit rules.

Every closed NIFTY minute (09:45-14:30) it rebuilds the exact training features from live data, asks the two
LightGBM models (ATM CE / ATM PE) for the expected net reward of buying now, and
  * mode "suggest": only publishes the suggestion (Trade screen card)
  * mode "auto":    buys the better side when its predicted reward clears the trained threshold.
Open trades exit on the rules the model was trained with: stop, target, time limit, 15:15 square-off.
Every entry carries a journal (features + predictions) that is written to the audit log, so the model can
be retrained on its own trades (the reward feedback loop).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from app.core.memory_bus import (
    MINUTE_BAR_KEY, ai_status, chain_history, in_memory_quotes, index_daily, market_snapshot, minute_bars,
)
from app.engine import minute_bars as mb
from app.engine.registry import BaseStrategy, StrategyRegistry
from app.ml import agent
from app.services.upstox_session import IST

logger = logging.getLogger("strategy_ai_agent")
INDEX_KEY = "NSE_INDEX|Nifty 50"


class AIOptionsAgent(BaseStrategy):
    def __init__(self, parameters: Dict[str, Any]):
        super().__init__(parameters)
        self.qty = int(parameters.get("quantity") or 65)
        self.product_type = parameters.get("product_type", "MIS")
        self.mode = parameters.get("ai_mode", "auto")
        self.max_trades = int(parameters.get("max_trades_per_day") or 3)
        self.models = agent.load_models()
        meta = (self.models or {}).get("meta", {})
        self.sim = meta.get("sim", {"target_pct": 0.15, "stop_pct": 0.08, "max_hold_min": 30})
        self.threshold = float(parameters.get("ai_threshold", meta.get("threshold", 0.0)))
        self.day: Optional[date] = None
        self.trades_today = 0
        self.entry: Optional[Dict[str, Any]] = None  # {"key", "minute", "price"}
        self.clock = lambda: datetime.now(IST)  # injectable for tests

    def _publish(self, **state):
        ai_status[self.parameters.get("user_id") or "unknown"] = {"updated_at": time.time(), "mode": self.mode, "threshold": self.threshold,
                                 "trades_today": self.trades_today, "max_trades": self.max_trades, **state}

    async def on_candle(self, candle_data: Dict[str, Any], open_positions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if candle_data.get("instrument_key") != MINUTE_BAR_KEY:
            return None
        now = self.clock()
        if self.day != now.date():
            self.day, self.trades_today, self.entry = now.date(), 0, None
        bar_minute = int(candle_data["minute"])
        minute_now = now.hour * 60 + now.minute

        if open_positions:
            return self._manage_exit(open_positions[0], minute_now)
        self.entry = None

        if not self.models:
            self._publish(status="no_model", message="No trained model deployed")
            return None
        if not (agent.FIRST_DECISION <= bar_minute <= agent.LAST_DECISION):
            self._publish(status="idle", message="Outside entry window (09:45-14:30)")
            return None

        expiry_s = market_snapshot.get("expiry")
        if not expiry_s:
            self._publish(status="waiting", message="Waiting for live option chain (connect Upstox)")
            return None
        bars = mb.closed_bars(INDEX_KEY, now.timestamp())
        features, row, why = agent.build_live_features(
            bars=bars, rows=market_snapshot.get("rows", []), chain_hist=chain_history,
            today=now.date(), expiry=date.fromisoformat(expiry_s),
            prev_close=index_daily.get("prev_close") or market_snapshot.get("spot_prev_close"),
            prev_day_ret=float(index_daily.get("prev_day_ret") or 0.0),
        )
        if features is None:
            self._publish(status="waiting", message=why)
            return None

        preds = agent.score(self.models, features)
        side = "ce" if preds["ce"] >= preds["pe"] else "pe"
        best = preds[side]
        leg = row[side]
        confident = best > self.threshold
        state = dict(status="watching", strike=row["strike"], pred_ce=round(preds["ce"], 4),
                     pred_pe=round(preds["pe"], 4), best_side=side.upper(), best_symbol=leg.get("trading_symbol"),
                     signal=confident, bar_time=candle_data.get("time"))

        if not confident:
            self._publish(**state, message=f"No edge: best {side.upper()} {best * 100:+.2f}% "
                                            f"vs threshold {self.threshold * 100:+.2f}%")
            return None
        if self.trades_today >= self.max_trades:
            self._publish(**state, message="Signal, but daily trade limit reached")
            return None
        if self.mode != "auto":
            self._publish(**state, message=f"SUGGESTION: BUY {leg.get('trading_symbol')} (suggest-only mode)")
            return None

        price = float(leg.get("ask") or leg["ltp"])
        self.trades_today += 1
        self.entry = {"key": leg["instrument_key"], "minute": minute_now, "price": price}
        self._publish(**state, message=f"BUY {leg.get('trading_symbol')} @ ~{price:.2f}")
        logger.info(f"AI entry {leg.get('trading_symbol')} pred={best:.4f} thr={self.threshold:.4f}")
        return {
            "action": "BUY",
            "instrument_key": leg["instrument_key"],
            "trading_symbol": leg.get("trading_symbol"),
            "quantity": self.qty,
            "product_type": self.product_type,
            "entry_price": price,
            "stop_loss": round(price * (1 - self.sim["stop_pct"]), 2),
            "target": round(price * (1 + self.sim["target_pct"]), 2),
            "journal": {"features": features, "pred_ce": preds["ce"], "pred_pe": preds["pe"],
                        "side": side.upper(), "threshold": self.threshold,
                        "exit_rule": (self.models["meta"] or {}).get("exit_rule")},
        }

    def _manage_exit(self, pos: Dict[str, Any], minute_now: int) -> Optional[Dict[str, Any]]:
        key = pos["instrument_key"]
        if not self.entry or self.entry["key"] != key:
            # restarted mid-trade: rebuild entry state from the position (hold clock restarts now)
            self.entry = {"key": key, "minute": minute_now, "price": float(pos["average_price"])}
        q = in_memory_quotes.get(key) or {}
        ltp = q.get("bid") or q.get("ltp")
        bars = minute_bars.get(key) or []
        last_bar = bars[-2] if len(bars) >= 2 else None  # last fully closed minute of the held option
        reason = agent.exit_reason(entry_price=self.entry["price"], bar=last_bar, ltp=ltp,
                                   held_minutes=minute_now - self.entry["minute"], minute_now=minute_now,
                                   sim=self.sim)
        pnl_pct = (ltp / self.entry["price"] - 1) * 100 if ltp else None
        self._publish(status="in_trade", message=f"Holding {pos['trading_symbol']}"
                      + (f" ({pnl_pct:+.1f}%)" if pnl_pct is not None else ""),
                      symbol=pos["trading_symbol"], entry_price=self.entry["price"],
                      stop=round(self.entry["price"] * (1 - self.sim["stop_pct"]), 2),
                      target=round(self.entry["price"] * (1 + self.sim["target_pct"]), 2))
        if not reason or pos["quantity"] <= 0:
            return None
        logger.info(f"AI exit {pos['trading_symbol']} reason={reason}")
        return {
            "action": "SELL",
            "instrument_key": key,
            "trading_symbol": pos["trading_symbol"],
            "quantity": abs(pos["quantity"]),
            "product_type": self.product_type,
            "journal": {"exit_reason": reason, "entry_price": self.entry["price"], "exit_ltp": ltp},
        }


StrategyRegistry.register("aiOptionsAgent", AIOptionsAgent)
