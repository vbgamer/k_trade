import logging
import uuid
import asyncio
from typing import Dict, Any, List, Optional
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.engine.registry import StrategyRegistry
from app.db.models import StrategySubscription, Position
from app.services.execution import ExecutionService

logger = logging.getLogger("runtime")

class StrategyRuntime:
    """
    Generic execution host that manages a running strategy instance.
    Receives candle closed events, aggregates position state, evaluates triggers,
    and dispatches execution tasks.
    """
    
    def __init__(self, subscription_id: str, db_session_maker):
        self.subscription_id = subscription_id
        self.session_maker = db_session_maker
        self.active = False
        self.strategy_instance = None
        self.last_candle_ts = None

    async def initialize(self) -> bool:
        """
        Initializes target strategy instance and recovers position state from Postgres.
        """
        async with self.session_maker() as db:
            query = select(StrategySubscription).where(StrategySubscription.id == self.subscription_id)
            res = await db.execute(query)
            sub = res.scalar_one_or_none()
            if not sub:
                logger.error(f"Cannot initialize runtime: Subscription {self.subscription_id} not found.")
                return False
                
            # Load version details
            version_query = select(sub.version.__class__).where(sub.version.__class__.id == sub.strategy_version_id)
            version_res = await db.execute(version_query)
            version = version_res.scalar_one_or_none()
            if not version:
                logger.error("Strategy version detail not found.")
                return False
                
            # Fetch definition slug
            def_query = select(version.definition.__class__).where(version.definition.__class__.id == version.strategy_definition_id)
            def_res = await db.execute(def_query)
            definition = def_res.scalar_one_or_none()
            if not definition:
                logger.error("Strategy definition detail not found.")
                return False
                
            # Dynamic strategy load from registry
            strategy_class = StrategyRegistry.get_strategy(definition.slug)
            if not strategy_class:
                logger.error(f"Strategy slug '{definition.slug}' not registered in registry.")
                return False
                
            # Instantiate strategy with stored parameters
            config = sub.config_json or {}
            config.update({
                "quantity": sub.quantity,
                "product_type": sub.product_type,
                "max_profit": sub.max_profit,
                "max_loss": sub.max_loss
            })
            
            self.strategy_instance = strategy_class(config)
            self.active = True
            logger.info(f"Initialized StrategyRuntime for subscription={self.subscription_id} slug={definition.slug}")
            return True

    async def process_candle(self, candle_data: Dict[str, Any]):
        """
        Processes an aggregated closed candle event.
        Calculates signal flags and handles order transactions.
        """
        if not self.active or not self.strategy_instance:
            return
            
        candle_ts = candle_data.get("time") or candle_data.get("ts_now")
        if self.last_candle_ts == candle_ts:
            # Prevent double candle processing
            return
        self.last_candle_ts = candle_ts
        
        async with self.session_maker() as db:
            # 1. Fetch current open positions from Database (Recovery & State sync)
            pos_query = select(Position).where(
                Position.strategy_subscription_id == self.subscription_id,
                Position.quantity != 0
            )
            pos_res = await db.execute(pos_query)
            positions = pos_res.scalars().all()
            
            # Map positions to standard serializable dictionaries
            open_positions_map = [
                {
                    "instrument_key": pos.instrument_key,
                    "trading_symbol": pos.trading_symbol,
                    "quantity": pos.quantity,
                    "average_price": pos.average_price
                }
                for pos in positions
            ]
            
            # 2. Evaluate Strategy Decision loop
            try:
                signal_intent = await self.strategy_instance.on_candle(
                    candle_data=candle_data,
                    open_positions=open_positions_map
                )
            except Exception as e:
                logger.error(f"Exception during strategy signal evaluation: {str(e)}")
                return
                
            if not signal_intent:
                return
                
            action = signal_intent.get("action")  # BUY, SELL, NONE
            if action not in ["BUY", "SELL"]:
                return
                
            instrument_key = signal_intent.get("instrument_key")
            quantity = signal_intent.get("quantity", self.strategy_instance.parameters.get("quantity", 50))
            product_type = signal_intent.get("product_type", self.strategy_instance.parameters.get("product_type", "MIS"))
            
            # 3. Create unique Idempotency Client Order ID
            # Structure: co_<sub_id>_<candle_timestamp_formatted>_<buy/sell>
            clean_ts = str(candle_ts).replace(":", "").replace("-", "").replace(" ", "")
            client_order_id = f"co_{self.subscription_id[:8]}_{clean_ts}_{action.lower()}"
            
            # 4. Route Order to Execution Service
            execution_service = ExecutionService(db)
            logger.info(f"Signal detected. Placing {action} order for sub={self.subscription_id} key={instrument_key}")
            
            await execution_service.execute_trade(
                subscription_id=self.subscription_id,
                side=action,
                instrument_key=instrument_key,
                quantity=quantity,
                client_order_id=client_order_id,
                product_type=product_type,
                trading_symbol=signal_intent.get("trading_symbol")
            )
