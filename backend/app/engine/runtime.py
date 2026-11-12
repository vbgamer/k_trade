import logging
import uuid
import asyncio
from typing import Dict, Any, List, Optional
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.engine.registry import StrategyRegistry
from app.db.models import StrategySubscription, StrategyVersion, StrategyDefinition, Position
from app.services.execution import ExecutionService
from app.engine.rms import is_exit_order, size_from_risk
from app.services.audit import log_audit

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
            version_query = select(StrategyVersion).where(StrategyVersion.id == sub.strategy_version_id)
            version_res = await db.execute(version_query)
            version = version_res.scalar_one_or_none()
            if not version:
                logger.error("Strategy version detail not found.")
                return False
                
            # Fetch definition slug
            def_query = select(StrategyDefinition).where(StrategyDefinition.id == version.strategy_definition_id)
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
                "max_loss": sub.max_loss,
                "subscription_id": sub.id,
                "user_id": sub.user_id,
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
            # a strategy may return one signal or a list (e.g. the AI agent exits several trades and enters one)
            intents = signal_intent if isinstance(signal_intent, list) else [signal_intent]
            for intent in intents:
                await self._place_signal(db, intent, candle_ts, open_positions_map, many=len(intents) > 1)


    async def _place_signal(self, db: AsyncSession, signal_intent: Dict[str, Any], candle_ts,
                            open_positions_map: List[Dict[str, Any]], many: bool = False):
        action = signal_intent.get("action")  # BUY, SELL, NONE
        if action not in ["BUY", "SELL"]:
            return
            
        instrument_key = signal_intent.get("instrument_key")
        quantity = signal_intent.get("quantity", self.strategy_instance.parameters.get("quantity", 50))
        product_type = signal_intent.get("product_type", self.strategy_instance.parameters.get("product_type", "MIS"))
        entry_price = signal_intent.get("entry_price")
        stop_loss = signal_intent.get("stop_loss")
        target = signal_intent.get("target")

        # Risk-based sizing: when the signal carries a stop and the subscription sets
        # risk_per_trade, size so a stop-out loses at most that amount (exits keep their size)
        params = self.strategy_instance.parameters
        risk_per_trade = float(params.get("risk_per_trade") or 0)
        if risk_per_trade > 0 and entry_price is not None and stop_loss is not None \
                and not is_exit_order(action, quantity, instrument_key, open_positions_map):
            quantity = size_from_risk(risk_per_trade, entry_price, stop_loss, int(params.get("lot_size") or 1))
            if quantity <= 0:
                logger.warning(
                    f"Skipping signal for sub={self.subscription_id}: one lot risks more than {risk_per_trade}"
                )
                return

        # 3. Create unique Idempotency Client Order ID
        # Structure: co_<sub_id>_<candle_timestamp_formatted>_<buy/sell>
        clean_ts = str(candle_ts).replace(":", "").replace("-", "").replace(" ", "")
        client_order_id = f"co_{self.subscription_id[:8]}_{clean_ts}_{action.lower()}"
        if many:  # several orders on one candle: make each contract's id distinct (still deterministic)
            client_order_id += "_" + uuid.uuid5(uuid.NAMESPACE_URL, instrument_key).hex[:8]
        
        # 4. Route Order to Execution Service
        execution_service = ExecutionService(db)
        logger.info(f"Signal detected. Placing {action} order for sub={self.subscription_id} key={instrument_key}")
        
        result = await execution_service.execute_trade(
            subscription_id=self.subscription_id,
            side=action,
            instrument_key=instrument_key,
            quantity=quantity,
            client_order_id=client_order_id,
            product_type=product_type,
            trading_symbol=signal_intent.get("trading_symbol"),
            entry_price=entry_price,
            stop_loss=stop_loss,
            target=target
        )

        # ML agent journal: what it saw and predicted, plus the outcome, for retraining on its own trades
        journal = signal_intent.get("journal")
        if journal:
            try:
                sub = await db.get(StrategySubscription, self.subscription_id)
                await log_audit(
                    db=db, user_id=sub.user_id, event_type="AI_TRADE_JOURNAL",
                    message=f"AI {action} {signal_intent.get('trading_symbol') or instrument_key}: "
                            f"{(result or {}).get('status')}",
                    metadata_json={**journal, "action": action, "instrument_key": instrument_key,
                                   "client_order_id": client_order_id, "candle_time": str(candle_ts),
                                   "result": result},
                )
            except Exception as e:
                logger.warning(f"Could not write AI journal: {e}")
