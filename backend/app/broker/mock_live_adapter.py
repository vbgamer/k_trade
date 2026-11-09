import random
import asyncio
import time
from typing import Dict, Any, List, Optional
from app.broker.base_adapter import BaseBrokerAdapter
from app.core.memory_bus import in_memory_quotes

# In-memory shared state for simulation purposes
class GlobalMarketState:
    def __init__(self):
        self.nifty_spot = 24300.0
        self.tick_count = 0
        
    def step(self):
        # Random walk for Nifty Spot
        self.tick_count += 1
        change = random.normalvariate(0.0, 1.5)  # mean 0, std dev 1.5
        self.nifty_spot = round(self.nifty_spot + change, 2)
        return self.nifty_spot

market_state = GlobalMarketState()

LIVE_QUOTE_MAX_AGE_SEC = 15.0
PAPER_BROKERAGE_PER_ORDER = 20.0  # INR, typical discount-broker F&O fee


def is_live_instrument(instrument_key: str) -> bool:
    """Upstox instrument keys (NSE_FO|<token>) are priced from the live feed, never the random model."""
    return instrument_key.startswith("NSE_FO|")


def paper_fill_from_quote(
    broker_order_id: str, side: str, instrument_key: str, order_type: str, limit_price: Optional[float]
) -> Dict[str, Any]:
    quote = in_memory_quotes.get(instrument_key)
    if not quote or not quote.get("live") or time.time() - quote["ts"] > LIVE_QUOTE_MAX_AGE_SEC:
        return {"status": "REJECTED", "rejection_reason": "No fresh live price for this contract. Is Upstox connected?"}

    ltp = quote["ltp"]
    # Fall back to LTP when the book side is empty (e.g. pre-open or illiquid strike)
    best = (quote.get("ask") if side == "BUY" else quote.get("bid")) or ltp
    if order_type == "LIMIT":
        if limit_price is None:
            return {"status": "REJECTED", "rejection_reason": "Limit price required"}
        marketable = limit_price >= best if side == "BUY" else limit_price <= best
        if not marketable:
            return {
                "status": "REJECTED",
                "rejection_reason": f"Limit {limit_price} not marketable (best {'ask' if side == 'BUY' else 'bid'} {best}). "
                                    "Paper engine fills only immediately executable limit orders.",
            }

    return {
        "status": "FILLED",
        "broker_order_id": broker_order_id,
        "fill_price": round(best, 2),
        "slippage": round(best - ltp, 2),
        "brokerage_fees": PAPER_BROKERAGE_PER_ORDER,
    }

class MockLiveAdapter(BaseBrokerAdapter):
    """
    High-fidelity Broker Adapter Mock for local execution.
    Connects to the GlobalMarketState to fetch spot prices and generates option contracts on the fly.
    """
    
    def __init__(self):
        self.logged_in = False

    async def login(self, credentials: Dict[str, Any]) -> bool:
        await asyncio.sleep(0.1)  # Simulate API latency
        if not credentials.get("client_id") or not credentials.get("api_key"):
            return False
        self.logged_in = True
        return True

    def _calculate_option_price(self, spot: float, strike: int, side: str) -> float:
        # Simple intrinsic + extrinsic option pricing model
        time_value = 120.0 - (int(time.time()) % 1200) / 10.0  # Decaying time value
        time_value = max(time_value, 20.0)
        
        if side == "CE":
            intrinsic = max(spot - strike, 0.0)
        else:
            intrinsic = max(strike - spot, 0.0)
            
        price = intrinsic + time_value
        return round(price, 2)

    async def get_ltp(self, instrument_key: str) -> float:
        await asyncio.sleep(0.02)
        quote = in_memory_quotes.get(instrument_key)
        if quote and quote.get("live"):
            return quote["ltp"]
        spot = market_state.nifty_spot
        
        # Format: e.g. "NSE_INDEX|Nifty 50" or "NSE_OPTION|NIFTY26JUL24300CE"
        if "Nifty 50" in instrument_key or "INDEX" in instrument_key:
            return spot
            
        if "OPTION" in instrument_key or "NIFTY" in instrument_key:
            try:
                # Extract strike and side
                parts = instrument_key.split("|")[-1]  # NIFTY26JUL24300CE
                # find side (CE or PE)
                side = "CE" if "CE" in parts else "PE"
                # get strike number
                strike_str = "".join([c for c in parts if c.isdigit()])
                strike = int(strike_str)
                return self._calculate_option_price(spot, strike, side)
            except Exception:
                return 150.0  # Default fallback option price

        return spot

    async def get_option_chain(self, underlying_key: str) -> List[Dict[str, Any]]:
        await asyncio.sleep(0.05)
        spot = market_state.nifty_spot
        atm_strike = int(round(spot / 50.0) * 50)
        
        # Generate 5 strikes around ATM
        strikes = range(atm_strike - 150, atm_strike + 200, 50)
        contracts = []
        
        for strike in strikes:
            for side in ["CE", "PE"]:
                symbol = f"NIFTY26JUL{strike}{side}"
                key = f"NSE_OPTION|{symbol}"
                ltp = self._calculate_option_price(spot, strike, side)
                contracts.append({
                    "instrument_key": key,
                    "trading_symbol": symbol,
                    "strike": strike,
                    "side": side,
                    "ltp": ltp
                })
        return contracts

    async def place_order(
        self,
        client_order_id: str,
        side: str,
        instrument_key: str,
        quantity: int,
        order_type: str = "MARKET",
        limit_price: Optional[float] = None,
        product_type: str = "MIS"
    ) -> Dict[str, Any]:
        await asyncio.sleep(0.05)  # Execution network latency

        broker_order_id = f"bo_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"

        # Live-data paper fills: cross the real spread (buy at ask, sell at bid)
        if is_live_instrument(instrument_key):
            return paper_fill_from_quote(broker_order_id, side, instrument_key, order_type, limit_price)

        # Enforce rate limits check simulation
        if random.random() < 0.005:  # 0.5% chance of connection drop
            return {
                "status": "REJECTED",
                "rejection_reason": "INSUFFICIENT_FUNDS_OR_LIMIT_REJECTION"
            }

        ltp = await self.get_ltp(instrument_key)

        # Simulate slippage on market orders
        slippage = 0.0
        if order_type == "MARKET":
            # Slippage is positive for buys, negative for sells (bad execution fill)
            direction = 1.0 if side == "BUY" else -1.0
            slippage = round(random.uniform(0.05, 0.25) * direction, 2)
            fill_price = round(ltp + slippage, 2)
        else:
            fill_price = limit_price or ltp

        return {
            "status": "FILLED",
            "broker_order_id": broker_order_id,
            "fill_price": fill_price,
            "slippage": slippage,
            "brokerage_fees": 20.0,  # Standard discount broker option fee per order (INR)
        }

    async def cancel_order(self, broker_order_id: str) -> bool:
        await asyncio.sleep(0.02)
        return True
