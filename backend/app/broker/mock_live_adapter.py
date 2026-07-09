import random
import asyncio
import time
from typing import Dict, Any, List, Optional
from app.broker.base_adapter import BaseBrokerAdapter

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

        broker_order_id = f"bo_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
        
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
