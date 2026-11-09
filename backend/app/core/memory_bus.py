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
