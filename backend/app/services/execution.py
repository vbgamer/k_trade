import logging
from typing import Dict, Any, Optional
from datetime import datetime
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import BrokerOrder, Trade, StrategySubscription
from app.broker.mock_live_adapter import MockLiveAdapter
from app.services.audit import log_audit
from app.services.portfolio import update_position

logger = logging.getLogger("execution")

class ExecutionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        # Instantiating the broker adapter gateway (defaulting to MockLiveAdapter for local execution)
        self.broker = MockLiveAdapter()

    async def execute_trade(
        self,
        subscription_id: str,
        side: str,  # BUY, SELL
        instrument_key: str,
        quantity: int,
        client_order_id: str,
        order_type: str = "MARKET",
        limit_price: Optional[float] = None,
        product_type: str = "MIS",
        trading_symbol: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Deduplicates, validates, logs, and executes orders to the exchange.
        """
        # 1. Idempotency Check (Duplicate Order Prevention)
        query = select(BrokerOrder).where(BrokerOrder.client_order_id == client_order_id)
        result = await self.db.execute(query)
        existing_order = result.scalar_one_or_none()
        if existing_order:
            logger.warning(f"Duplicate trade call detected for client_order_id={client_order_id}. Skipping execution.")
            return {
                "ok": True,
                "status": existing_order.status,
                "broker_order_id": existing_order.broker_order_id,
                "duplicate": True
            }

        # 2. Fetch Subscription details to extract active credentials and isolation
        sub_query = select(StrategySubscription).where(StrategySubscription.id == subscription_id)
        sub_result = await self.db.execute(sub_query)
        subscription = sub_result.scalar_one_or_none()
        if not subscription:
            raise ValueError(f"Strategy Subscription ID {subscription_id} not found.")
            
        user_id = subscription.user_id
        symbol = trading_symbol or instrument_key.split("|")[-1]

        # 3. Create Pending BrokerOrder in database
        order = BrokerOrder(
            strategy_subscription_id=subscription_id,
            client_order_id=client_order_id,
            side=side,
            instrument_key=instrument_key,
            order_type=order_type,
            quantity=quantity,
            product_type=product_type,
            status="PENDING"
        )
        self.db.add(order)
        await self.db.commit()
        await self.db.refresh(order)

        # 4. Audit Log order dispatch
        await log_audit(
            db=self.db,
            user_id=user_id,
            event_type="ORDER_DISPATCHED",
            message=f"Dispatched {side} order for {quantity} qty of {symbol}",
            broker_order_id=None,
            metadata_json={"client_order_id": client_order_id}
        )

        try:
            # 5. Place order through adapter
            res = await self.broker.place_order(
                client_order_id=client_order_id,
                side=side,
                instrument_key=instrument_key,
                quantity=quantity,
                order_type=order_type,
                limit_price=limit_price,
                product_type=product_type
            )
            
            status = res.get("status", "REJECTED")
            
            if status == "FILLED":
                # Fill transaction
                fill_price = float(res.get("fill_price", 0.0))
                broker_order_id = res.get("broker_order_id")
                slippage = float(res.get("slippage", 0.0))
                brokerage_fees = float(res.get("brokerage_fees", 0.0))
                
                order.broker_order_id = broker_order_id
                order.status = "FILLED"
                
                # Write trade detail record
                trade = Trade(
                    broker_order_id=order.id,
                    execution_price=fill_price,
                    quantity=quantity,
                    slippage=slippage,
                    brokerage_fees=brokerage_fees
                )
                self.db.add(trade)
                
                # Trigger Portfolio Service positions adjustments
                await update_position(
                    db=self.db,
                    subscription_id=subscription_id,
                    instrument_key=instrument_key,
                    trading_symbol=symbol,
                    side=side,
                    quantity=quantity,
                    execution_price=fill_price
                )
                
                await self.db.commit()
                
                # Audit Filled Status
                await log_audit(
                    db=self.db,
                    user_id=user_id,
                    event_type="ORDER_FILLED",
                    message=f"Order Filled. Price={fill_price}, Slippage={slippage}, Fees={brokerage_fees}",
                    broker_order_id=broker_order_id,
                    metadata_json={"fill_price": fill_price, "slippage": slippage}
                )
                
                return {
                    "ok": True,
                    "status": "FILLED",
                    "broker_order_id": broker_order_id,
                    "fill_price": fill_price
                }
            else:
                reason = res.get("rejection_reason", "Broker rejected order execution")
                order.status = "REJECTED"
                order.rejection_reason = reason
                await self.db.commit()
                
                # Audit Rejection
                await log_audit(
                    db=self.db,
                    user_id=user_id,
                    event_type="ORDER_REJECTED",
                    message=f"Order rejected: {reason}",
                    metadata_json={"rejection_reason": reason}
                )
                
                return {
                    "ok": False,
                    "status": "REJECTED",
                    "rejection_reason": reason
                }
                
        except Exception as e:
            logger.exception("Error executing broker adapter order placement")
            order.status = "FAILED"
            order.rejection_reason = str(e)
            await self.db.commit()
            
            await log_audit(
                db=self.db,
                user_id=user_id,
                event_type="ORDER_FAILED",
                message=f"Execution system exception: {str(e)}",
                metadata_json={"error": str(e)}
            )
            
            return {
                "ok": False,
                "status": "FAILED",
                "rejection_reason": str(e)
            }
