import logging
import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import StrategySubscription, Position, BrokerOrder, Trade
from app.services.portfolio import recalculate_unrealized_pnl
from app.services.audit import log_audit
from app.broker.mock_live_adapter import MockLiveAdapter
from app.core.memory_bus import in_memory_ltps

logger = logging.getLogger("rms")

IST = timezone(timedelta(hours=5, minutes=30))  # India has no DST, so a fixed offset is exact


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""


@dataclass
class DayStats:
    entries_today: int = 0
    realized_pnl_today: float = 0.0


def _signed(side: str, quantity: int) -> int:
    return quantity if side == "BUY" else -quantity


def is_exit_order(side: str, quantity: int, instrument_key: Optional[str], open_positions: List[Dict[str, Any]]) -> bool:
    """True when the order only reduces an existing position (never flips or adds to it)."""
    for pos in open_positions:
        if pos.get("instrument_key") != instrument_key:
            continue
        held = pos.get("quantity", 0)
        delta = _signed(side, quantity)
        return held != 0 and (held > 0) != (delta > 0) and abs(delta) <= abs(held)
    return False


def size_from_risk(risk_amount: float, entry_price: float, stop_loss: float, lot_size: int = 1) -> int:
    """
    Quantity whose loss at the stop equals risk_amount, rounded down to whole lots.
    Returns 0 when even one lot would risk more than allowed.
    """
    per_unit = abs(entry_price - stop_loss)
    if risk_amount <= 0 or per_unit <= 0 or lot_size <= 0:
        return 0
    lots = int(risk_amount // (per_unit * lot_size))
    return lots * lot_size


class RMSService:
    """
    Risk Management System (RMS).
    Handles pre-trade validation and background portfolio circuit breakers.
    """

    @staticmethod
    def evaluate_pre_trade(
        subscription: StrategySubscription,
        side: str,
        quantity: int,
        open_positions: List[Dict[str, Any]],
        instrument_key: Optional[str] = None,
        day_stats: Optional[DayStats] = None,
        entry_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        target: Optional[float] = None,
    ) -> RiskDecision:
        """
        Pre-trade risk gate. Exits are always allowed so the RMS can never trap a position;
        entries must pass size, daily-loss, trades-per-day and reward-to-risk limits.

        Optional limits read from subscription.config_json:
          max_trades_per_day  - cap on new entries per IST trading day
          min_reward_to_risk  - e.g. 1.5; only enforced when the signal carries stop and target
        """
        stats = day_stats or DayStats()
        config = getattr(subscription, "config_json", None) or {}

        if quantity <= 0:
            return RiskDecision(False, f"Invalid quantity {quantity}")

        if is_exit_order(side, quantity, instrument_key, open_positions):
            return RiskDecision(True, "exit")

        # Validate order size
        max_allowed_qty = subscription.quantity * 2
        if quantity > max_allowed_qty:
            return RiskDecision(False, f"Order quantity {quantity} exceeds max limit of {max_allowed_qty}")

        # Block new entries once realized losses alone have used up the daily budget
        max_loss = getattr(subscription, "max_loss", 0) or 0
        if max_loss > 0 and stats.realized_pnl_today <= -abs(max_loss):
            return RiskDecision(
                False, f"Daily loss limit reached (realized {stats.realized_pnl_today} <= -{abs(max_loss)})"
            )

        max_trades = int(config.get("max_trades_per_day") or 0)
        if max_trades > 0 and stats.entries_today >= max_trades:
            return RiskDecision(False, f"Max trades per day reached ({stats.entries_today}/{max_trades})")

        min_rr = float(config.get("min_reward_to_risk") or 0)
        if min_rr > 0 and None not in (entry_price, stop_loss, target):
            risk = abs(entry_price - stop_loss)
            reward = abs(target - entry_price)
            rr = reward / risk if risk > 0 else 0.0
            if rr < min_rr:
                return RiskDecision(False, f"Reward-to-risk {rr:.2f} below minimum {min_rr}")

        return RiskDecision(True, "entry")

    @staticmethod
    def validate_pre_trade_limits(
        subscription: StrategySubscription,
        side: str,
        quantity: int,
        open_positions: List[Dict[str, Any]]
    ) -> bool:
        """Boolean shortcut over evaluate_pre_trade (no day stats)."""
        decision = RMSService.evaluate_pre_trade(subscription, side, quantity, open_positions)
        if not decision.allowed:
            logger.warning(f"RMS Reject: {decision.reason}")
        return decision.allowed

    @staticmethod
    async def load_day_stats(db: AsyncSession, subscription_id: str) -> DayStats:
        """
        Entries and realized P&L for the current IST day.
        An order counts as an entry when it grows the absolute position in its instrument;
        the opening position is reconstructed from current holdings minus today's fills.
        """
        ist_midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = ist_midnight.astimezone(timezone.utc).replace(tzinfo=None)  # models store naive UTC

        orders_res = await db.execute(
            select(BrokerOrder)
            .where(
                BrokerOrder.strategy_subscription_id == subscription_id,
                BrokerOrder.status == "FILLED",
                BrokerOrder.created_at >= day_start_utc,
            )
            .order_by(BrokerOrder.created_at)
        )
        orders = orders_res.scalars().all()

        pos_res = await db.execute(select(Position).where(Position.strategy_subscription_id == subscription_id))
        net = {p.instrument_key: p.quantity for p in pos_res.scalars().all()}
        for o in orders:
            net[o.instrument_key] = net.get(o.instrument_key, 0) - _signed(o.side, o.quantity)

        entries = 0
        for o in orders:
            before = net.get(o.instrument_key, 0)
            after = before + _signed(o.side, o.quantity)
            if abs(after) > abs(before) or (before != 0 and (before > 0) != (after > 0)):
                entries += 1
            net[o.instrument_key] = after

        pnl_res = await db.execute(
            select(Trade.realized_pnl)
            .join(BrokerOrder, Trade.broker_order_id == BrokerOrder.id)
            .where(BrokerOrder.strategy_subscription_id == subscription_id, Trade.executed_at >= day_start_utc)
        )
        realized = sum(v or 0.0 for v in pnl_res.scalars().all())
        return DayStats(entries_today=entries, realized_pnl_today=round(realized, 2))

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
                            cached = None
                            try:
                                cached = await redis_client.get(f"ltp:{pos.instrument_key}")
                            except Exception:
                                pass  # Redis unavailable: fall through to local caches
                            if cached:
                                instrument_ltps[pos.instrument_key] = json.loads(cached).get("ltp")
                            elif pos.instrument_key in in_memory_ltps:
                                instrument_ltps[pos.instrument_key] = in_memory_ltps[pos.instrument_key]
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
