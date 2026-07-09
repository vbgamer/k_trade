import asyncio
import json
import logging
import time
from datetime import datetime, timezone
import redis.asyncio as aioredis
from app.core.config import settings
from app.broker.mock_live_adapter import market_state, MockLiveAdapter

logger = logging.getLogger("market_data")

class MarketDataService:
    """
    Market Data Service.
    Consumes live ticks (from broker/exchange), aggregates them into candles, 
    and broadcasts events over the Redis Pub/Sub Event Bus.
    """
    
    def __init__(self, candle_duration_sec: int = 5):
        # Default to 5 seconds for manual testing convenience (mocking a 1-minute candle speed)
        self.candle_duration = candle_duration_sec
        self.redis_client = None
        self.broker = MockLiveAdapter()
        self.active_subscriptions = set()
        self.running = False
        self.aggregators = {}  # instrument_key -> {"ticks": [], "start_time": float}

    async def connect(self):
        self.redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        logger.info("MarketDataService connected to Redis Event Bus.")

    def subscribe(self, instrument_key: str):
        self.active_subscriptions.add(instrument_key)
        logger.info(f"Subscribed market data feed for: {instrument_key}")

    def unsubscribe(self, instrument_key: str):
        self.active_subscriptions.discard(instrument_key)
        logger.info(f"Unsubscribed market data feed for: {instrument_key}")

    async def start_stream(self):
        if self.running:
            return
        self.running = True
        self.stream_task = asyncio.create_task(self._run_feed_loop())
        logger.info("MarketDataService stream feed loop started.")

    async def stop_stream(self):
        self.running = False
        if hasattr(self, "stream_task"):
            self.stream_task.cancel()
        if self.redis_client:
            await self.redis_client.close()
        logger.info("MarketDataService stream feed loop stopped.")

    async def _run_feed_loop(self):
        while self.running:
            try:
                # 1. Update/Step the global simulated spot index
                spot = market_state.step()
                
                # Fetch option chain for Nifty ATM
                options = await self.broker.get_option_chain("NSE_INDEX|Nifty 50")
                
                # Create a list of active instrument keys we want ticks for
                targets = ["NSE_INDEX|Nifty 50"] + [opt["instrument_key"] for opt in options]
                
                now_epoch = time.time()
                now_str = datetime.now(timezone.utc).isoformat()
                
                for key in targets:
                    # Get tick price
                    price = spot if "INDEX" in key else next(
                        (opt["ltp"] for opt in options if opt["instrument_key"] == key), 
                        150.0
                    )
                    
                    # Publish raw tick to Redis Cache (for fast LTP fetches)
                    tick_payload = {"ltp": price, "timestamp": now_epoch}
                    if self.redis_client:
                        await self.redis_client.set(f"ltp:{key}", json.dumps(tick_payload))
                        
                    # 2. Aggregate ticks into candles
                    if key not in self.aggregators:
                        self.aggregators[key] = {"ticks": [], "start_time": now_epoch}
                        
                    agg = self.aggregators[key]
                    agg["ticks"].append(price)
                    
                    # Check if candle duration has elapsed
                    if now_epoch - agg["start_time"] >= self.candle_duration:
                        ticks = agg["ticks"]
                        candle = {
                            "instrument_key": key,
                            "time": datetime.fromtimestamp(agg["start_time"]).strftime("%Y-%m-%d %H:%M:%S"),
                            "open": ticks[0],
                            "high": max(ticks),
                            "low": min(ticks),
                            "close": ticks[-1],
                            "volume": len(ticks) * 100
                        }
                        
                        # Publish closed candle event to Event Bus channel
                        channel = f"market:candles:{key}"
                        if self.redis_client:
                            await self.redis_client.publish(channel, json.dumps(candle))
                            # Cache last n closed candles in a redis list (max size 200)
                            cache_key = f"candles:{key}"
                            await self.redis_client.lpush(cache_key, json.dumps(candle))
                            await self.redis_client.ltrim(cache_key, 0, 199)
                            
                        # Reset aggregator
                        self.aggregators[key] = {"ticks": [], "start_time": now_epoch}
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in market data feed loop: {str(e)}")
                
            await asyncio.sleep(0.5)  # Market ticks every 0.5s
