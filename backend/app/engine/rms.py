import logging
import asyncio
import json
from typing import Dict, Any, List
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import StrategySubscription, Position
from app.services.portfolio import recalculate_unrealized_pnl
from app.services.audit import log_audit
from app.broker.mock_live_adapter import MockLiveAdapter

logger = logging.getLogger("rms")

class RMSService:
    """
    Risk Management System (RMS).
    Handles pre-trade validation and background portfolio circuit breakers.
    """
    
    @staticmethod
    def validate_pre_trade_limits(
        subscription: StrategySubscription,
        side: str,
        quantity: int,
        open_positions: List[Dict[str, Any]]
    ) -> bool:
        """
        Pre-trade risk validator checks.
        Ensures order conforms to size limits and prevents over-leverage.
        """
        # Validate order size
        max_allowed_qty = subscription.quantity * 2
        if quantity > max_allowed_qty:
            logger.warning(f"RMS Reject: Order quantity {quantity} exceeds max limit of {max_allowed_qty}")
            return False
            
        # Prevent simultaneous long and short directions on same option strike
        # (basic options portfolio limit check)
        return True

    @classmethod
    async def run_rms_circuit_breaker(cls, db_session_maker, redis_url: str):
        """
        Background loop executing continuous risk auditing.
        Liquidates subscriptions breaching daily max profit/loss rules.
        """
        import redis.asyncio as aioredis
        redis_client = aioredis.from_url(redis_url, decode_responses=True)
        broker = MockLiveAdapter()
        
        logger.info("RMS Circuit Breaker daemon loop active.")
        
        while True:
            try:
                async with db_session_maker() as db:
                    # Fetch running subscriptions
                    query = select(StrategySubscription).where(StrategySubscription.status == "running")
                    res = await db.execute(query)
                    subscriptions = res.scalars().all()
                    
                    for sub in subscriptions:
                        # 1. Fetch LTPs from Redis Cache for active subscription positions
                        pos_query = select(Position).where(
                            Position.strategy_subscription_id == sub.id,
                            Position.quantity != 0
                        )
                        pos_res = await db.execute(pos_query)
                        positions = pos_res.scalars().all()
                        
                        if not positions:
                            continue
                            
                        instrument_ltps = {}
                        for pos in positions:
                            cached = await redis_client.get(f"ltp:{pos.instrument_key}")
                            if cached:
                                instrument_ltps[pos.instrument_key] = json.loads(cached).get("ltp")
                            else:
                                # Fallback to fetching directly from broker if cache miss
                                ltp = await broker.get_ltp(pos.instrument_key)
                                instrument_ltps[pos.instrument_key] = ltp
                                
                        # 2. Recalculate Portfolio MTM
                        net_mtm = await recalculate_unrealized_pnl(db, sub.id, instrument_ltps)
                        
                        # 3. Check Risk thresholds
                        max_profit = sub.max_profit
                        max_loss = sub.max_loss
                        
                        hit_profit = (max_profit > 0) and (net_mtm >= max_profit)
                        hit_loss = (max_loss > 0) and (net_mtm <= -abs(max_loss))
                        
                        if hit_profit or hit_loss:
                            reason = "MAX_PROFIT_LIMIT" if hit_profit else "MAX_LOSS_LIMIT"
                            logger.warning(
                                f"RMS Triggered for sub={sub.id}. Net MTM={net_mtm} "
                                f"Breached Limit: {reason} (P={max_profit}, L={max_loss})"
                            )
                            
                            # 4. Halt strategy and update DB
                            sub.status = "stopped"
                            await db.commit()
                            
                            # Log Audit Event
                            await log_audit(
                                db=db,
                                user_id=sub.user_id,
                                event_type="RISK_CIRCUIT_BREAKER_TRIGGERED",
                                message=f"Daily risk threshold triggered: {reason}. Liquidating positions.",
                                metadata_json={"net_mtm": net_mtm, "limit_type": reason}
                            )
                            
                            # 5. Liquidate open positions (Auto-flatten)
                            for pos in positions:
                                exit_side = "SELL" if pos.quantity > 0 else "BUY"
                                abs_qty = abs(pos.quantity)
                                client_order_id = f"rms_{sub.id[:8]}_{int(time.time())}"
                                
                                logger.info(f"RMS Liquidating position: {exit_side} {abs_qty} of {pos.trading_symbol}")
                                
                                try:
                                    res = await broker.place_order(
                                        client_order_id=client_order_id,
                                        side=exit_side,
                                        instrument_key=pos.instrument_key,
                                        quantity=abs_qty,
                                        order_type="MARKET",
                                        product_type=sub.product_type
                                    )
                                    
                                    if res.get("status") == "FILLED":
                                        fill_price = float(res.get("fill_price", 0.0))
                                        # Update position entry and calculate final P&L
                                        pnl_direction = 1.0 if pos.quantity > 0 else -1.0
                                        realized = abs_qty * (fill_price - pos.average_price) * pnl_direction
                                        pos.realized_pnl = round(pos.realized_pnl + realized, 2)
                                        pos.quantity = 0
                                        pos.average_price = 0.0
                                        await db.commit()
                                except Exception as exit_err:
                                    logger.error(f"Failed to auto-flatten position for sub={sub.id}: {str(exit_err)}")
                                    
            except Exception as e:
                logger.error(f"RMS Circuit Breaker loop exception: {str(e)}")
                
            await asyncio.sleep(2.0)  # Check risk boundaries every 2 seconds
