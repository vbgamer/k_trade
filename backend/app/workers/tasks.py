import asyncio
import json
import logging
from sqlalchemy.future import select
import redis.asyncio as aioredis
from app.workers.celery_app import celery_app
from app.core.database import async_session_maker
from app.core.config import settings
from app.db.models import StrategySubscription
from app.engine.runtime import StrategyRuntime

logger = logging.getLogger("tasks")

async def run_strategy_loop(subscription_id: str):
    runtime = StrategyRuntime(subscription_id, async_session_maker)
    initialized = await runtime.initialize()
    if not initialized:
        logger.error(f"Failed to initialize strategy runtime for subscription {subscription_id}.")
        return
        
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = redis_client.pubsub()
    
    # Subscribe to option & index candles fanning out from the Event Bus
    await pubsub.psubscribe("market:candles:*")
    logger.info(f"Strategy Runtime task listener active for subscription={subscription_id}")
    
    try:
        while runtime.active:
            # Periodic Database Status Check
            async with async_session_maker() as db:
                query = select(StrategySubscription).where(StrategySubscription.id == subscription_id)
                result = await db.execute(query)
                sub = result.scalar_one_or_none()
                
                if not sub or sub.status != "running":
                    logger.info(f"Halt command detected in DB. StrategyRuntime exiting loop for sub={subscription_id}")
                    runtime.active = False
                    break
            
            # Fetch message from Event Bus
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message:
                try:
                    candle_data = json.loads(message["data"])
                    await runtime.process_candle(candle_data)
                except Exception as eval_err:
                    logger.error(f"Error handling event candle payload: {str(eval_err)}")
                    
            await asyncio.sleep(0.1)
            
    except Exception as run_err:
        logger.error(f"Strategy loop encountered runner error: {str(run_err)}")
    finally:
        await pubsub.punsubscribe("market:candles:*")
        await pubsub.close()
        await redis_client.close()
        logger.info(f"Strategy Runtime loop closed for subscription={subscription_id}")

@celery_app.task(name="start_strategy_task")
def start_strategy_task(subscription_id: str):
    """
    Spawns the strategy execution runtime in a Celery worker thread.
    """
    asyncio.run(run_strategy_loop(subscription_id))

@celery_app.task(name="recovery_resume_task")
def recovery_resume_task():
    """
    Runs during system boot/crash recovery.
    Resumes any active strategy executions that were running prior to shutdown/crash.
    """
    async def recover():
        async with async_session_maker() as db:
            query = select(StrategySubscription).where(StrategySubscription.status == "running")
            result = await db.execute(query)
            subs = result.scalars().all()
            
            for sub in subs:
                logger.info(f"Failure Recovery: Resuming Strategy Subscription {sub.id}")
                start_strategy_task.delay(sub.id)
                
    asyncio.run(recover())
