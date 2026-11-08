"""
Sample past trades for demos. Only reachable when DEMO_MODE is enabled or via
`python manage.py seed`. Never runs for real users automatically.
"""
import datetime
import uuid
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import User, StrategyDefinition, StrategyVersion, StrategySubscription, BrokerOrder, Trade
from app.services.bootstrap import DEFAULT_STRATEGY_SLUG


async def seed_user_past_trades(db: AsyncSession, user_id: str):
    q = select(Trade).join(BrokerOrder).join(StrategySubscription).where(StrategySubscription.user_id == user_id)
    res = await db.execute(q)
    if len(res.scalars().all()) > 0:
        return

    v_query = select(StrategyVersion).join(StrategyDefinition).where(StrategyDefinition.slug == DEFAULT_STRATEGY_SLUG)
    v_res = await db.execute(v_query)
    version = v_res.scalar_one_or_none()
    if not version:
        return

    sub = StrategySubscription(
        user_id=user_id,
        strategy_version_id=version.id,
        quantity=50,
        product_type="MIS",
        max_profit=5000.0,
        max_loss=2500.0,
        status="stopped",
        config_json={"timeframeMin": 1, "bypass_scheduler": True}
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)

    base_time = datetime.datetime.utcnow()
    sample_trades_data = [
        {
            "symbol": "NIFTY26JUL24300CE",
            "key": "NSE_OPTION|NIFTY26JUL24300CE",
            "segment": "FNO",
            "product_type": "MIS",
            "orders": [
                {"side": "BUY", "qty": 50, "price": 142.50, "pnl": 0.0, "time_offset": 48},
                {"side": "SELL", "qty": 50, "price": 168.20, "pnl": 1285.00, "time_offset": 47.5}
            ]
        },
        {
            "symbol": "NIFTY26JUL24350PE",
            "key": "NSE_OPTION|NIFTY26JUL24350PE",
            "segment": "FNO",
            "product_type": "MIS",
            "orders": [
                {"side": "BUY", "qty": 50, "price": 110.00, "pnl": 0.0, "time_offset": 32},
                {"side": "SELL", "qty": 50, "price": 95.50, "pnl": -725.00, "time_offset": 31.2}
            ]
        },
        {
            "symbol": "NIFTY26JUL24250CE",
            "key": "NSE_OPTION|NIFTY26JUL24250CE",
            "segment": "FNO",
            "product_type": "NRML",
            "orders": [
                {"side": "BUY", "qty": 100, "price": 178.40, "pnl": 0.0, "time_offset": 24},
                {"side": "SELL", "qty": 100, "price": 205.10, "pnl": 2670.00, "time_offset": 22.8}
            ]
        },
        {
            "symbol": "NIFTY-INTRADAY-MIS",
            "key": "NSE_INDEX|NIFTY50",
            "segment": "INTRADAY",
            "product_type": "MIS",
            "orders": [
                {"side": "BUY", "qty": 50, "price": 24280.00, "pnl": 0.0, "time_offset": 18},
                {"side": "SELL", "qty": 50, "price": 24345.50, "pnl": 3275.00, "time_offset": 17.1}
            ]
        },
        {
            "symbol": "NIFTY-INTRADAY-MIS",
            "key": "NSE_INDEX|NIFTY50",
            "segment": "INTRADAY",
            "product_type": "MIS",
            "orders": [
                {"side": "BUY", "qty": 50, "price": 24350.00, "pnl": 0.0, "time_offset": 10},
                {"side": "SELL", "qty": 50, "price": 24320.00, "pnl": -1500.00, "time_offset": 9.4}
            ]
        },
        {
            "symbol": "NIFTY-INTRADAY-MIS",
            "key": "NSE_INDEX|NIFTY50",
            "segment": "INTRADAY",
            "product_type": "MIS",
            "orders": [
                {"side": "BUY", "qty": 50, "price": 24260.00, "pnl": 0.0, "time_offset": 4},
                {"side": "SELL", "qty": 50, "price": 24310.00, "pnl": 2500.00, "time_offset": 3.2}
            ]
        }
    ]

    total_realized_seed = 0.0
    for item in sample_trades_data:
        for ord_info in item["orders"]:
            exec_time = base_time - datetime.timedelta(hours=ord_info["time_offset"])
            client_oid = f"co_seed_{uuid.uuid4().hex[:8]}"
            broker_oid = f"bo_seed_{uuid.uuid4().hex[:8]}"

            b_order = BrokerOrder(
                strategy_subscription_id=sub.id,
                client_order_id=client_oid,
                broker_order_id=broker_oid,
                side=ord_info["side"],
                instrument_key=item["key"],
                order_type="MARKET",
                quantity=ord_info["qty"],
                product_type=item["product_type"],
                segment=item["segment"],
                status="FILLED",
                created_at=exec_time
            )
            db.add(b_order)
            await db.commit()
            await db.refresh(b_order)

            trade = Trade(
                broker_order_id=b_order.id,
                trading_symbol=item["symbol"],
                execution_price=ord_info["price"],
                quantity=ord_info["qty"],
                slippage=0.10,
                brokerage_fees=20.0,
                realized_pnl=ord_info["pnl"],
                executed_at=exec_time
            )
            db.add(trade)
            total_realized_seed += ord_info["pnl"] - 20.0

    u_res = await db.execute(select(User).where(User.id == user_id))
    u = u_res.scalar_one_or_none()
    if u:
        u.virtual_balance = round(500000.0 + total_realized_seed, 2)
    await db.commit()
