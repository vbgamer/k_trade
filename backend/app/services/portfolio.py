from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Position

async def update_position(
    db: AsyncSession,
    subscription_id: str,
    instrument_key: str,
    trading_symbol: str,
    side: str,  # BUY, SELL
    quantity: int,
    execution_price: float
) -> Position:
    """
    Updates user positions in the database and calculates realized P&L when positions close.
    """
    query = select(Position).where(
        Position.strategy_subscription_id == subscription_id,
        Position.instrument_key == instrument_key
    )
    result = await db.execute(query)
    position = result.scalar_one_or_none()
    
    # Sign definition: Long Buy = +qty, Short Sell = -qty
    qty_change = quantity if side == "BUY" else -quantity
    
    if not position:
        position = Position(
            strategy_subscription_id=subscription_id,
            instrument_key=instrument_key,
            trading_symbol=trading_symbol,
            quantity=qty_change,
            average_price=execution_price,
            unrealized_pnl=0.0,
            realized_pnl=0.0
        )
        db.add(position)
    else:
        old_qty = position.quantity
        old_avg = position.average_price
        new_qty = old_qty + qty_change
        
        # Determine if we are closing/reducing or opening/adding
        same_direction = (old_qty >= 0 and qty_change >= 0) or (old_qty <= 0 and qty_change <= 0)
        
        if same_direction:
            # Weighted average price update
            total_qty = abs(old_qty) + quantity
            if total_qty > 0:
                position.average_price = round(
                    ((abs(old_qty) * old_avg) + (quantity * execution_price)) / total_qty, 
                    2
                )
            position.quantity = new_qty
        else:
            # Closing/reducing position. Calculate realized P&L
            closed_qty = min(abs(old_qty), quantity)
            
            pnl_direction = 1.0 if old_qty > 0 else -1.0
            realized = closed_qty * (execution_price - old_avg) * pnl_direction
            position.realized_pnl = round(position.realized_pnl + realized, 2)
            
            position.quantity = new_qty
            
            # If position is fully closed, reset average price
            if position.quantity == 0:
                position.average_price = 0.0
                
            # If we reversed direction, the remaining quantity establishes a new position direction
            elif (old_qty > 0 and new_qty < 0) or (old_qty < 0 and new_qty > 0):
                position.average_price = execution_price

    return position

async def recalculate_unrealized_pnl(
    db: AsyncSession,
    subscription_id: str,
    instrument_ltps: dict
) -> float:
    """
    Recalculates unrealized P&L for all active positions in a strategy subscription.
    Returns:
        float: Total Net MTM (realized + unrealized pnl) of the subscription.
    """
    query = select(Position).where(Position.strategy_subscription_id == subscription_id)
    result = await db.execute(query)
    positions = result.scalars().all()
    
    total_mtm = 0.0
    for pos in positions:
        ltp = instrument_ltps.get(pos.instrument_key)
        if ltp is not None:
            if pos.quantity > 0:  # Long
                pos.unrealized_pnl = round(pos.quantity * (ltp - pos.average_price), 2)
            elif pos.quantity < 0:  # Short
                pos.unrealized_pnl = round(abs(pos.quantity) * (pos.average_price - ltp), 2)
            else:
                pos.unrealized_pnl = 0.0
        
        total_mtm += pos.realized_pnl + pos.unrealized_pnl
        
    await db.commit()
    return total_mtm
