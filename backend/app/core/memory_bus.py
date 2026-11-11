import asyncio
import json
import logging
from typing import Dict, List, Callable, Any, Set

logger = logging.getLogger("memory_bus")

# Global in-memory cache fallbacks
in_memory_ltps: Dict[str, float] = {}
in_memory_candles: Dict[str, List[Dict[str, Any]]] = {}

# Latest quote per instrument: {"ltp", "bid", "ask", "ts", "live"}. Paper fills read bid/ask from here.
in_memory_quotes: Dict[str, Dict[str, Any]] = {}

# Option chain snapshot served to the Trade screen and used by strategies to find the ATM contract.
# Keys: source ("upstox" | "simulator"), spot, expiry, lot_size, atm_strike, atm_ce_key, atm_pe_key, rows, updated_at
market_snapshot: Dict[str, Any] = {"source": "simulator", "rows": []}

# Extra instruments to keep quoting (e.g. open positions that drifted out of the chain window)
tracked_keys: Set[str] = set()

# Today's IST-minute-aligned bars per instrument, oldest first: {"minute": mins since midnight, open, high, low, close}
minute_bars: Dict[str, List[Dict[str, Any]]] = {}

# ATM-area option premiums recorded at each minute close: strike -> {minute: (ce_ltp, pe_ltp)}
chain_history: Dict[int, Dict[int, Any]] = {}

# Previous sessions' NIFTY closes for features: {"prev_close", "prev_day_ret", "day"}
index_daily: Dict[str, Any] = {}

# Latest AI agent status per user id (suggestion card on the Trade screen)
ai_status: Dict[str, Dict[str, Any]] = {}

# The model's score at every closed NIFTY minute today, for the admin AI Lab: {"day", "points": [...]}
ai_score_log: Dict[str, Any] = {"day": None, "points": []}

# Channel/key on which a closed NIFTY minute bar is published (drives the ML agent)
MINUTE_BAR_KEY = "NIFTY_1M"

class MemoryPubSub:
    """
    In-memory Pub/Sub event bus to bypass Redis dependency during local development.
    """
    def __init__(self):
        self.subscribers: List[Callable[[str, str], Any]] = []

    def subscribe(self, callback: Callable[[str, str], Any]):
        if callback not in self.subscribers:
            self.subscribers.append(callback)
            logger.info("New subscriber bound to in-memory Event Bus.")

    def unsubscribe(self, callback: Callable[[str, str], Any]):
        if callback in self.subscribers:
            self.subscribers.remove(callback)
            logger.info("Subscriber unbound from in-memory Event Bus.")

    async def publish(self, channel: str, message: str):
        for sub in self.subscribers:
            try:
                if asyncio.iscoroutinefunction(sub):
                    await sub(channel, message)
                else:
                    sub(channel, message)
            except Exception as e:
                logger.error(f"Error distributing in-memory event: {str(e)}")

memory_bus = MemoryPubSub()


async def publish_user_event(user_id: str, event_type: str, data: Dict[str, Any]) -> None:
    """Push an event to every open device of one user (fills, rejections, AI status)."""
    await memory_bus.publish("user:events", json.dumps(
        {"user_id": user_id, "event_type": event_type, "data": data}, default=str))
