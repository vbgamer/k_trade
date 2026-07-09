import json
import logging
import datetime
import pytz
import redis.asyncio as aioredis
from typing import Dict, Any, List, Optional
from app.engine.registry import BaseStrategy, StrategyRegistry
from app.core.config import settings
from app.core.memory_bus import in_memory_candles

logger = logging.getLogger("strategy_kaushal")

class KaushalCustomWMASMACall1(BaseStrategy):
    """
    Nifty ATM Call Crossover Strategy.
    Calculates Heikin-Ashi candles from raw prices.
    Generates signals comparing WMA(5) on HA close vs SMA(1) on HA open, offset -1.
    """
    
    def __init__(self, parameters: Dict[str, Any]):
        super().__init__(parameters)
        self.timeframe_min = parameters.get("timeframeMin", 1)
        self.qty = parameters.get("quantity", 50)
        self.product_type = parameters.get("product_type", "MIS")
        
        # Indicator Periods
        self.wma_close_period = 5
        self.sma_open_period = 1
        
        # Timezone timezone
        self.ist = pytz.timezone("Asia/Kolkata")

    def _sma(self, values: List[float], period: int) -> List[Optional[float]]:
        if not values:
            return []
        out = [None] * len(values)
        if period <= 0:
            return out
        for i in range(period - 1, len(values)):
            window = values[i - period + 1 : i + 1]
            out[i] = sum(window) / float(period)
        return out

    def _wma(self, values: List[float], period: int) -> List[Optional[float]]:
        if not values:
            return []
        out = [None] * len(values)
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

    def _compute_heikin_ashi(
        self, 
        o_list: List[float], 
        h_list: List[float], 
        l_list: List[float], 
        c_list: List[float]
    ):
        n = len(c_list)
        ha_o = [0.0] * n
        ha_h = [0.0] * n
        ha_l = [0.0] * n
        ha_c = [0.0] * n
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

    async def on_candle(
        self,
        candle_data: Dict[str, Any],
        open_positions: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        instrument_key = candle_data["instrument_key"]
        
        # 1. Load historical closed candles (try Redis, fallback to Memory Bus)
        candles = []
        use_fallback = False
        try:
            redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            cache_key = f"candles:{instrument_key}"
            raw_candles = await redis_client.lrange(cache_key, 0, 199)
            await redis_client.close()
            if raw_candles:
                candles = [json.loads(c) for c in raw_candles]
            else:
                use_fallback = True
        except Exception:
            use_fallback = True
            
        if use_fallback:
            candles = in_memory_candles.get(instrument_key, [])
            
        if len(candles) < max(self.wma_close_period, self.sma_open_period) + 5:
            return None
            
        chrono_candles = candles[::-1]  # Chronological order
        
        o = [float(x["open"]) for x in chrono_candles]
        h = [float(x["high"]) for x in chrono_candles]
        l = [float(x["low"]) for x in chrono_candles]
        cl = [float(x["close"]) for x in chrono_candles]
        
        # 2. Heikin-Ashi Indicator Transformation
        ha_o, ha_h, ha_l, ha_c = self._compute_heikin_ashi(o, h, l, cl)
        
        # 3. Indicator calculations
        wma_close = self._wma(ha_c, self.wma_close_period)
        sma_open = self._sma(ha_o, self.sma_open_period)
        
        idx = len(cl) - 1
        prev_idx = idx - 1
        
        # Apply offset -1
        wma_close_val = wma_close[idx - 1]
        wma_close_prev_val = wma_close[prev_idx - 1]
        sma_open_val = sma_open[idx - 1]
        sma_open_prev_val = sma_open[prev_idx - 1]
        
        if None in [wma_close_val, wma_close_prev_val, sma_open_val, sma_open_prev_val]:
            return None
            
        # Crossover Checks
        bullish_cross = (wma_close_prev_val <= sma_open_prev_val) and (wma_close_val > sma_open_val)
        bearish_cross = (wma_close_prev_val >= sma_open_prev_val) and (wma_close_val < sma_open_val)
        
        in_trade = len(open_positions) > 0
        
        # Check market time cut-offs (auto-flatten at 15:30 IST)
        ist_now = datetime.datetime.now(self.ist)
        if ist_now.hour >= 15 and ist_now.minute >= 30:
            if in_trade:
                logger.info("Cutoff limit reached. Exiting positions.")
                return {
                    "action": "SELL",
                    "instrument_key": open_positions[0]["instrument_key"],
                    "quantity": abs(open_positions[0]["quantity"]),
                    "product_type": self.product_type,
                    "trading_symbol": open_positions[0]["trading_symbol"]
                }
            return None

        if in_trade:
            active_pos = open_positions[0]
            if bearish_cross and active_pos["quantity"] > 0:
                return {
                    "action": "SELL",
                    "instrument_key": active_pos["instrument_key"],
                    "quantity": abs(active_pos["quantity"]),
                    "product_type": self.product_type,
                    "trading_symbol": active_pos["trading_symbol"]
                }
        else:
            if bullish_cross:
                symbol = instrument_key.split("|")[-1]
                return {
                    "action": "BUY",
                    "instrument_key": instrument_key,
                    "quantity": self.qty,
                    "product_type": self.product_type,
                    "trading_symbol": symbol
                }
                
        return None

# Register to the Global Strategies Catalog Registry
StrategyRegistry.register("kaushalcustomWMASMACall1", KaushalCustomWMASMACall1)
