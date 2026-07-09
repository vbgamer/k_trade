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
        
    use_redis = True
    pubsub = None
    redis_client = None
    
    # Try connecting to Redis
    try:
        redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await redis_client.ping()
        pubsub = redis_client.pubsub()
        await pubsub.psubscribe("market:candles:*")
        logger.info(f"Strategy Runtime connected to Redis Pub/Sub for sub={subscription_id}")
    except Exception:
        use_redis = False
        logger.warning(f"Redis down. Operating in Local Memory-Bus mode for sub={subscription_id}")
        if redis_client:
            try:
                await redis_client.close()
            except Exception:
                pass
            
    # Set up in-memory message queue fallback
    memory_queue = asyncio.Queue()
    
    async def memory_bus_callback(channel: str, message: str):
        await memory_queue.put(message)
        
    if not use_redis:
        from app.core.memory_bus import memory_bus
        memory_bus.subscribe(memory_bus_callback)
        
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
            
            message_data = None
            if use_redis:
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message:
                        message_data = message["data"]
                except Exception:
                    # Switch to memory bus if Redis connection crashes mid-trade
                    use_redis = False
                    from app.core.memory_bus import memory_bus
                    memory_bus.subscribe(memory_bus_callback)
                    logger.warning("Lost connection to Redis. Switched to local Memory-Bus.")
            else:
                try:
                    # Read from local asyncio queue with timeout
                    message_data = await asyncio.wait_for(memory_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                    
            if message_data:
                try:
                    candle_data = json.loads(message_data)
                    await runtime.process_candle(candle_data)
                except Exception as eval_err:
                    logger.error(f"Error handling event candle payload: {str(eval_err)}")
                    
            await asyncio.sleep(0.1)
            
    except Exception as run_err:
        logger.error(f"Strategy loop encountered runner error: {str(run_err)}")
    finally:
        if use_redis:
            try:
                await pubsub.punsubscribe("market:candles:*")
                await pubsub.close()
                await redis_client.close()
            except Exception:
                pass
        else:
            from app.core.memory_bus import memory_bus
            memory_bus.unsubscribe(memory_bus_callback)
            
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
