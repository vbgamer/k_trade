import asyncio
import json
import logging
import math
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
import redis.asyncio as aioredis
from sqlalchemy.future import select
from app.core.config import settings
from app.broker.mock_live_adapter import market_state, MockLiveAdapter
from app.broker.upstox import (
    NIFTY_INDEX_KEY, UpstoxAuthError, UpstoxClient, build_chain_rows, nearest_expiry
)
from app.core.memory_bus import (
    memory_bus, in_memory_ltps, in_memory_candles, in_memory_quotes, market_snapshot, tracked_keys,
    index_daily, MINUTE_BAR_KEY
)
from app.engine import minute_bars as mb
from app.services import upstox_session

logger = logging.getLogger("market_data")

SIM_INDEX_KEY = "NSE_INDEX|Nifty 50"
SIM_LOT_SIZE = 65  # simulator only; live mode uses the lot size Upstox reports


class MarketDataService:
    """
    Market Data Service.
    Sources prices from Upstox (live, once a session is connected) or the built-in simulator,
    aggregates them into candles, and broadcasts events over both Redis (production) and the
    in-memory event bus (local fallback). Quote snapshots go out on the "market:quotes" channel.
    """

    def __init__(self, candle_duration_sec: int = 5):
        self.sim_candle_duration = candle_duration_sec
        self.candle_duration = candle_duration_sec
        self.redis_client = None
        self.broker = MockLiveAdapter()
        self.active_subscriptions = set()
        self.running = False
        self.aggregators = {}  # instrument_key -> {"ticks": [], "start_time": float}
        self.source = "simulator"
        self._contracts: List[Dict[str, Any]] = []
        self._contracts_day: Optional[date] = None
        self._last_chain_at = 0.0
        self._history_day: Optional[date] = None
        self._history_tried_at = 0.0

    async def connect(self):
        try:
            self.redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            await self.redis_client.ping()
            logger.info("MarketDataService connected to Redis Event Bus.")
        except Exception:
            logger.warning("Redis not available on connect. Operating in Local Memory-Bus mode.")
            self.redis_client = None

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
            try:
                await self.redis_client.close()
            except Exception:
                pass
        logger.info("MarketDataService stream feed loop stopped.")

    # --- Source selection -----------------------------------------------------

    def _switch_source(self, source: str):
        """Drop cached prices and candles when switching so fake and real data never mix."""
        if source == self.source:
            return
        logger.info(f"Market data source: {self.source} -> {source}")
        self.source = source
        self.aggregators.clear()
        in_memory_candles.clear()
        in_memory_ltps.clear()
        in_memory_quotes.clear()
        market_snapshot.clear()
        market_snapshot.update(source=source, rows=[])
        mb._state["day"] = None  # forces minute bars / chain history to reset
        index_daily.clear()
        self._history_day = None
        self.candle_duration = settings.LIVE_CANDLE_SECONDS if source == "upstox" else self.sim_candle_duration
        self._last_chain_at = 0.0

    async def _run_feed_loop(self):
        while self.running:
            delay = 0.5
            try:
                if settings.upstox_configured and upstox_session.is_connected():
                    self._switch_source("upstox")
                    await self._live_step()
                    delay = settings.LIVE_QUOTE_INTERVAL_SEC
                else:
                    self._switch_source("simulator")
                    await self._simulator_step()
            except asyncio.CancelledError:
                break
            except UpstoxAuthError as e:
                logger.warning(f"Upstox session rejected, falling back to simulator: {e}")
                from app.core.database import async_session_maker
                async with async_session_maker() as db:
                    await upstox_session.clear(db, reason=str(e))
            except Exception as e:
                logger.error(f"Error in market data feed loop: {str(e)}")
                if self.source == "upstox":
                    delay = 5.0  # back off on network / API errors
            await asyncio.sleep(delay)

    # --- Live (Upstox) --------------------------------------------------------

    async def _refresh_chain(self, client: UpstoxClient):
        today = datetime.now(upstox_session.IST).date()  # exchange date, not the server's UTC date
        if self._contracts_day != today or not self._contracts:
            self._contracts = await client.get_option_contracts(NIFTY_INDEX_KEY)
            self._contracts_day = today
        expiry = nearest_expiry(self._contracts, today)
        if not expiry:
            logger.warning("Upstox returned no upcoming NIFTY expiries.")
            return
        chain = await client.get_option_chain(expiry, NIFTY_INDEX_KEY)
        contracts = [c for c in self._contracts if c.get("expiry") == expiry]
        snap = build_chain_rows(chain, contracts, settings.OPTION_CHAIN_STRIKES_EACH_SIDE)
        market_snapshot.update(source="upstox", expiry=expiry, updated_at=time.time(), **snap)

        # Keep quoting open positions even if they drift out of the ATM window
        from app.core.database import async_session_maker
        from app.db.models import Position
        async with async_session_maker() as db:
            res = await db.execute(select(Position.instrument_key).where(Position.quantity != 0))
            tracked_keys.update(k for k in res.scalars().all() if k.startswith("NSE_FO|"))

    def _chain_keys(self) -> List[str]:
        keys = []
        for row in market_snapshot.get("rows", []):
            for leg in (row.get("ce"), row.get("pe")):
                if leg:
                    keys.append(leg["instrument_key"])
        return keys

    async def _live_step(self):
        client = UpstoxClient(upstox_session.session["token"])
        now = time.time()
        if now - self._last_chain_at >= settings.LIVE_CHAIN_INTERVAL_SEC or not market_snapshot.get("rows"):
            await self._refresh_chain(client)
            self._last_chain_at = now

        keys = list(dict.fromkeys([NIFTY_INDEX_KEY] + self._chain_keys() + sorted(tracked_keys)))
        quotes = await client.get_quotes(keys)
        now = time.time()
        for key, q in quotes.items():
            if not q["ltp"]:
                continue
            in_memory_quotes[key] = {**q, "ts": now, "live": True}
            await self._ingest(key, q["ltp"], now)

        await self._ensure_history(client, now)

        index_q = quotes.get(NIFTY_INDEX_KEY)
        if index_q:
            market_snapshot["spot"] = index_q["ltp"]
            market_snapshot["spot_prev_close"] = index_q.get("prev_close")
        for row in market_snapshot.get("rows", []):
            for leg in (row.get("ce"), row.get("pe")):
                if leg and leg["instrument_key"] in quotes:
                    q = quotes[leg["instrument_key"]]
                    leg.update(ltp=q["ltp"], bid=q["bid"], ask=q["ask"], oi=q["oi"] or leg.get("oi"))
        await self._update_minute_bars(NIFTY_INDEX_KEY, quotes, now)
        await self._broadcast_quotes(quotes, now)

    async def _ensure_history(self, client: UpstoxClient, now: float):
        """Once a day: backfill today's NIFTY minute bars (restart-safe) and the previous sessions' closes."""
        today = datetime.now(upstox_session.IST).date()
        if self._history_day == today or now - self._history_tried_at < 60:
            return
        self._history_tried_at = now
        try:
            daily = [c for c in await client.get_daily_candles(NIFTY_INDEX_KEY, today, today - timedelta(days=12))
                     if c["date"] < today]
            if len(daily) >= 2:
                index_daily.update(day=today, prev_close=daily[-1]["close"],
                                   prev_day_ret=math.log(daily[-1]["close"] / daily[-2]["close"]))
            intraday = [c for c in await client.get_intraday_candles(NIFTY_INDEX_KEY) if c["date"] == today]
            added = mb.backfill(NIFTY_INDEX_KEY, intraday, now)
            self._history_day = today
            logger.info(f"NIFTY history ready: prev_close={index_daily.get('prev_close')} backfilled {added} minute bars")
        except Exception as e:
            logger.warning(f"Could not load NIFTY history yet (will retry): {e}")

    # --- Simulator --------------------------------------------------------------

    async def _simulator_step(self):
        spot = market_state.step()
        options = await self.broker.get_option_chain(SIM_INDEX_KEY)
        now = time.time()

        quotes: Dict[str, Dict[str, Any]] = {SIM_INDEX_KEY: {"ltp": spot, "bid": spot, "ask": spot}}
        for opt in options:
            ltp = opt["ltp"]
            quotes[opt["instrument_key"]] = {"ltp": ltp, "bid": round(ltp - 0.05, 2), "ask": round(ltp + 0.05, 2)}

        for key, q in quotes.items():
            in_memory_quotes[key] = {**q, "ts": now, "live": False}
            await self._ingest(key, q["ltp"], now)

        rows: Dict[int, Dict[str, Any]] = {}
        for opt in options:
            row = rows.setdefault(opt["strike"], {"strike": opt["strike"], "ce": None, "pe": None})
            q = quotes[opt["instrument_key"]]
            row[opt["side"].lower()] = {
                "instrument_key": opt["instrument_key"], "trading_symbol": opt["trading_symbol"],
                "ltp": q["ltp"], "bid": q["bid"], "ask": q["ask"],
                "oi": None, "volume": None, "prev_close": None, "iv": None, "delta": None, "theta": None,
            }
        ordered = [rows[s] for s in sorted(rows)]
        atm = min(ordered, key=lambda r: abs(r["strike"] - spot)) if ordered else None
        market_snapshot.update(
            source="simulator", spot=spot, spot_prev_close=None, expiry=None, lot_size=SIM_LOT_SIZE,
            atm_strike=atm["strike"] if atm else None,
            atm_ce_key=(atm["ce"] or {}).get("instrument_key") if atm else None,
            atm_pe_key=(atm["pe"] or {}).get("instrument_key") if atm else None,
            rows=ordered, updated_at=now,
        )
        await self._update_minute_bars(SIM_INDEX_KEY, quotes, now)
        await self._broadcast_quotes(quotes, now)

    # --- Shared -----------------------------------------------------------------

    async def _update_minute_bars(self, index_key: str, quotes: Dict[str, Dict[str, Any]], now: float):
        """Fold quotes into IST minute bars; when the NIFTY minute closes, snapshot premiums and publish it."""
        closed_index = None
        for key, q in quotes.items():
            if q.get("ltp"):
                closed = mb.update(key, q["ltp"], now)
                if key == index_key and closed:
                    closed_index = closed
        if closed_index:
            mb.record_chain(closed_index["minute"], market_snapshot.get("rows", []))
            today, _ = mb.ist_minute(now)
            m = closed_index["minute"]
            candle = {**closed_index, "instrument_key": MINUTE_BAR_KEY,
                      "time": f"{today.isoformat()} {m // 60:02d}:{m % 60:02d}"}
            await memory_bus.publish(f"market:candles:{MINUTE_BAR_KEY}", json.dumps(candle))
            try:  # the AI Lab's minute-by-minute score chart (runs whether or not anyone runs the AI agent)
                from app.ml import live as ml_live
                ml_live.record_minute(datetime.fromtimestamp(now, upstox_session.IST))
            except Exception as e:
                logger.debug(f"AI score log skipped: {e}")

    async def _broadcast_quotes(self, quotes: Dict[str, Dict[str, Any]], now: float):
        payload = {
            "source": self.source,
            "spot": market_snapshot.get("spot"),
            "ts": now,
            "quotes": {k: {"ltp": q["ltp"], "bid": q["bid"], "ask": q["ask"]} for k, q in quotes.items()},
        }
        await memory_bus.publish("market:quotes", json.dumps(payload))

    async def _ingest(self, key: str, price: float, now_epoch: float):
        # Update local fallback tick cache
        in_memory_ltps[key] = price

        # Cache to Redis if available
        tick_payload = {"ltp": price, "timestamp": now_epoch}
        if self.redis_client:
            try:
                await self.redis_client.set(f"ltp:{key}", json.dumps(tick_payload))
            except Exception:
                self.redis_client = None  # Force local fallback on connection drop

        # Aggregate ticks into candles
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

            # Cache candle in local fallback
            if key not in in_memory_candles:
                in_memory_candles[key] = []
            in_memory_candles[key].insert(0, candle)
            in_memory_candles[key] = in_memory_candles[key][:200]

            # Broadcast over local in-memory event bus
            await memory_bus.publish(f"market:candles:{key}", json.dumps(candle))

            # Publish to Redis if available
            if self.redis_client:
                try:
                    channel = f"market:candles:{key}"
                    await self.redis_client.publish(channel, json.dumps(candle))
                    cache_key = f"candles:{key}"
                    await self.redis_client.lpush(cache_key, json.dumps(candle))
                    await self.redis_client.ltrim(cache_key, 0, 199)
                except Exception:
                    self.redis_client = None

            # Reset aggregator
            self.aggregators[key] = {"ticks": [], "start_time": now_epoch}
