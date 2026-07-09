import asyncio
import json
import logging
from typing import Dict, List, Callable, Any

logger = logging.getLogger("memory_bus")

# Global in-memory cache fallbacks
in_memory_ltps: Dict[str, float] = {}
in_memory_candles: Dict[str, List[Dict[str, Any]]] = {}

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
