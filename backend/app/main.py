import os
import json
import logging
import datetime
import asyncio
from typing import Dict, Optional, Set
from fastapi import FastAPI, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.database import get_db, async_session_maker
from app.core.migrations import run_migrations
from app.core.security import hash_password, verify_password, generate_jwt, decode_jwt, encrypt, decrypt
from app.db.models import (
    User, BrokerCredential, StrategyDefinition,
    StrategyVersion, StrategySubscription, Position, BrokerOrder, Trade
)
from app.schemas import (
    RegisterRequest, LoginRequest, BrokerCredentialsRequest, BrokerVerifyRequest,
    SubscriptionRequest, ToggleRequest, DemoTradeRequest
)
from app.services.bootstrap import ensure_default_strategies
from app.services.demo_seed import seed_user_past_trades
from app.services.scheduler import StrategyScheduler
from app.workers.tasks import start_strategy_task
from app.core.memory_bus import memory_bus
# Import strategy to trigger registration
from app.strategies.kaushalcustomWMASMACall1.strategy import KaushalCustomWMASMACall1

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api_gateway")

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    docs_url=None if settings.is_production else "/docs"
)

# Only the configured frontend origins may call the API from a browser
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

security = HTTPBearer()

async def get_current_user_id(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    user_id = decode_jwt(credentials.credentials)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id

# --- WEBSOCKET CONNECTION MANAGER ---
class ConnectionManager:
    """
    Tracks authenticated WebSocket connections per user.
    Market data is broadcast to everyone; order and strategy events go only to their owner.
    """
    def __init__(self):
        self.connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.connections.setdefault(user_id, set()).add(websocket)

    def disconnect(self, websocket: WebSocket, user_id: str):
        sockets = self.connections.get(user_id)
        if sockets:
            sockets.discard(websocket)
            if not sockets:
                self.connections.pop(user_id, None)

    async def _send(self, websocket: WebSocket, user_id: str, message: str):
        try:
            await websocket.send_text(message)
        except Exception:
            self.disconnect(websocket, user_id)

    async def broadcast(self, message: str):
        for user_id, sockets in list(self.connections.items()):
            for ws in list(sockets):
                await self._send(ws, user_id, message)

    async def send_to_user(self, user_id: str, message: str):
        for ws in list(self.connections.get(user_id, ())):
            await self._send(ws, user_id, message)

manager = ConnectionManager()

def _tick_event(candle: dict) -> str:
    return json.dumps({
        "event_type": "TickUpdate",
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "data": {
            "instrument_key": candle["instrument_key"],
            "ltp": candle["close"],
            "candle": candle
        }
    })

def _user_event(event_type: str, data: dict) -> str:
    return json.dumps({
        "event_type": event_type,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "data": data
    })

# Background task to subscribe to Redis and broadcast market events to all open WebSockets
async def redis_event_bus_listener():
    logger.info("Redis event bus listener task started.")
    pubsub = None
    redis_client = None

    while True:
        try:
            if redis_client is None:
                redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
                pubsub = redis_client.pubsub()
                await pubsub.psubscribe("market:candles:*")
                logger.info("Websocket event bus listener connected to Redis.")

            if pubsub is None:
                await asyncio.sleep(5)
                continue

            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg:
                await manager.broadcast(_tick_event(json.loads(msg["data"])))
        except (ConnectionError, OSError, Exception):
            # Redis unreachable. We operate silently and rely on the Memory Bus callback
            pubsub = None
            redis_client = None
            await asyncio.sleep(5)
        await asyncio.sleep(0.1)

# Memory Bus ws callback for local execution fallback
async def memory_bus_ws_callback(channel: str, message: str):
    try:
        await manager.broadcast(_tick_event(json.loads(message)))
    except Exception:
        pass

# Strategy loops running inside this process, keyed by subscription id
_local_strategy_tasks: Dict[str, asyncio.Task] = {}

def start_strategy_in_process(subscription_id: str) -> None:
    """Spawn the strategy loop on the API event loop, unless one is already running for it."""
    existing = _local_strategy_tasks.get(subscription_id)
    if existing and not existing.done():
        logger.info(f"Strategy loop already running in-process for sub={subscription_id}")
        return
    from app.workers.tasks import run_strategy_loop
    _local_strategy_tasks[subscription_id] = asyncio.create_task(run_strategy_loop(subscription_id))

async def resume_running_strategies() -> None:
    """After a restart, restart loops for subscriptions still marked running (stop them if the market is closed)."""
    async with async_session_maker() as db:
        result = await db.execute(select(StrategySubscription).where(StrategySubscription.status == "running"))
        subs = result.scalars().all()
        market_open = StrategyScheduler.is_market_open()
        for sub in subs:
            if market_open or (sub.config_json or {}).get("bypass_scheduler", False):
                logger.info(f"Recovery: resuming strategy sub={sub.id}")
                start_strategy_in_process(sub.id)
            else:
                logger.info(f"Recovery: market closed, marking strategy sub={sub.id} stopped")
                sub.status = "stopped"
        await db.commit()

@app.on_event("startup")
async def startup_event():
    # Bring the database schema up to date (alembic upgrade head)
    await asyncio.to_thread(run_migrations)

    # Subscribe websocket gateway to Memory Bus fallback
    memory_bus.subscribe(memory_bus_ws_callback)

    # Start Market Data Feed Simulator locally inside the API process
    from app.engine.market_data import MarketDataService
    mds = MarketDataService(candle_duration_sec=5) # 5-second candles for fast visual testing
    await mds.connect()
    await mds.start_stream()

    # Start local RMS Circuit Breaker daemon
    from app.engine.rms import RMSService
    asyncio.create_task(RMSService.run_rms_circuit_breaker(async_session_maker, settings.REDIS_URL))

    asyncio.create_task(redis_event_bus_listener())

    async with async_session_maker() as db:
        await ensure_default_strategies(db)

    if settings.RUN_STRATEGIES_IN_PROCESS:
        await resume_running_strategies()

# --- API ENDPOINTS ---

@app.get("/health")
async def health_check():
    db_ok = True
    try:
        async with async_session_maker() as db:
            await db.execute(select(1))
    except Exception:
        db_ok = False

    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "timestamp": datetime.datetime.utcnow().isoformat()
    }

@app.get("/metrics")
async def prometheus_metrics():
    async with async_session_maker() as db:
        res = await db.execute(select(StrategySubscription))
        count = len(res.scalars().all())
    return (
        f"# HELP active_strategy_subscriptions Total active trading subscribers\n"
        f"# TYPE active_strategy_subscriptions gauge\n"
        f"active_strategy_subscriptions {count}\n"
    )

@app.post("/api/v1/auth/register", status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    if not settings.ALLOW_REGISTRATION:
        raise HTTPException(status_code=403, detail="Registration is disabled on this server")

    email = payload.email.lower()

    query = select(User).where(User.email == email)
    result = await db.execute(query)
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        first_name=payload.first_name,
        last_name=payload.last_name,
        virtual_balance=500000.0
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Seed default Paper Trading credential
    cred = BrokerCredential(
        user_id=user.id,
        broker_name="paper_trading",
        api_key_encrypted=encrypt("paper_sim_key_default"),
        client_id_encrypted=encrypt("ALGO_PAPER_01")
    )
    db.add(cred)
    await db.commit()

    token = generate_jwt(user.id)
    return {
        "message": "User registered successfully with Paper Trading Account",
        "access_token": token,
        "token_type": "bearer"
    }

@app.post("/api/v1/auth/login")
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    query = select(User).where(User.email == payload.email.lower())
    result = await db.execute(query)
    user = result.scalar_one_or_none()

    valid, needs_rehash = verify_password(payload.password, user.password_hash) if user else (False, False)
    if not valid or not user.is_active:
        raise HTTPException(status_code=400, detail="Incorrect email or password")

    if needs_rehash:
        # Transparently upgrade legacy PBKDF2 hashes to bcrypt
        user.password_hash = hash_password(payload.password)
        await db.commit()

    token = generate_jwt(user.id)
    return {"access_token": token, "token_type": "bearer"}

@app.get("/api/v1/user/profile")
async def get_profile(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    query = select(User).where(User.id == user_id)
    res = await db.execute(query)
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "virtual_balance": user.virtual_balance or 500000.0,
        "is_active": user.is_active
    }

@app.post("/api/v1/broker/credentials")
async def save_credentials(payload: BrokerCredentialsRequest, user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    broker_name = payload.broker_name

    key_enc = encrypt(payload.api_key)
    client_enc = encrypt(payload.client_id)
    secret_enc = encrypt(payload.api_secret) if payload.api_secret else None
    pass_enc = encrypt(payload.password) if payload.password else None
    totp_enc = encrypt(payload.totp_secret) if payload.totp_secret else None

    query = select(BrokerCredential).where(BrokerCredential.user_id == user_id, BrokerCredential.broker_name == broker_name)
    result = await db.execute(query)
    cred = result.scalar_one_or_none()

    if not cred:
        cred = BrokerCredential(
            user_id=user_id,
            broker_name=broker_name,
            api_key_encrypted=key_enc,
            client_id_encrypted=client_enc,
            api_secret_encrypted=secret_enc,
            password_encrypted=pass_enc,
            totp_secret_encrypted=totp_enc
        )
        db.add(cred)
    else:
        cred.api_key_encrypted = key_enc
        cred.client_id_encrypted = client_enc
        cred.api_secret_encrypted = secret_enc
        cred.password_encrypted = pass_enc
        cred.totp_secret_encrypted = totp_enc

    await db.commit()
    return {
        "message": f"Credentials for {broker_name} encrypted and saved successfully",
        "broker_name": broker_name,
        "client_id": payload.client_id,
        "status": "CONNECTED"
    }

@app.post("/api/v1/broker/verify")
async def verify_broker(payload: BrokerVerifyRequest, user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    if payload.broker_name == "paper_trading":
        u_res = await db.execute(select(User).where(User.id == user_id))
        u = u_res.scalar_one_or_none()
        balance = u.virtual_balance if u else 500000.0
        return {
            "verified": True,
            "status": "CONNECTED",
            "broker_name": "Paper Trading Simulator",
            "client_id": payload.client_id or "ALGO_PAPER_01",
            "account_type": "VIRTUAL_PAPER",
            "virtual_margin": balance,
            "message": "Paper Trading account connected successfully! Orders will route to high-fidelity market simulator."
        }

    # No real broker adapters exist yet; refuse rather than pretend to authenticate.
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"Live trading via {payload.broker_name.upper()} is not available yet. Use Paper Trading for now."
    )

@app.get("/api/v1/broker/status")
async def get_broker_status(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    query = select(BrokerCredential).where(BrokerCredential.user_id == user_id)
    result = await db.execute(query)
    creds = result.scalars().all()

    u_res = await db.execute(select(User).where(User.id == user_id))
    u = u_res.scalar_one_or_none()
    balance = u.virtual_balance if u else 500000.0

    active_broker = "paper_trading"
    client_id = "ALGO_PAPER_01"
    if creds:
        active_broker = creds[-1].broker_name
        try:
            client_id = decrypt(creds[-1].client_id_encrypted)
        except Exception:
            client_id = "CONNECTED"

    return {
        "broker_name": active_broker,
        "client_id": client_id,
        "status": "CONNECTED",
        "virtual_balance": balance,
        "credentials_count": len(creds)
    }

@app.get("/api/v1/trades")
async def get_trades(
    segment: Optional[str] = Query(None, max_length=20),
    side: Optional[str] = Query(None, max_length=10),
    limit: int = Query(100, ge=1, le=500),
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db)
):
    query = (
        select(Trade, BrokerOrder, StrategySubscription, StrategyDefinition.name.label("strategy_name"))
        .join(BrokerOrder, Trade.broker_order_id == BrokerOrder.id)
        .join(StrategySubscription, BrokerOrder.strategy_subscription_id == StrategySubscription.id)
        .join(StrategyVersion, StrategySubscription.strategy_version_id == StrategyVersion.id)
        .join(StrategyDefinition, StrategyVersion.strategy_definition_id == StrategyDefinition.id)
        .where(StrategySubscription.user_id == user_id)
        .order_by(Trade.executed_at.desc())
    )

    if segment and segment.upper() != "ALL":
        query = query.where(BrokerOrder.segment == segment.upper())

    if side and side.upper() != "ALL":
        query = query.where(BrokerOrder.side == side.upper())

    query = query.limit(limit)
    result = await db.execute(query)
    rows = result.all()

    out = []
    for row in rows:
        trade = row[0]
        order = row[1]
        strategy_name = row[3]
        out.append({
            "id": trade.id,
            "broker_order_id": order.broker_order_id or order.client_order_id,
            "client_order_id": order.client_order_id,
            "trading_symbol": trade.trading_symbol or order.instrument_key.split("|")[-1],
            "instrument_key": order.instrument_key,
            "side": order.side,
            "segment": order.segment or "FNO",
            "product_type": order.product_type,
            "order_type": order.order_type,
            "quantity": trade.quantity,
            "execution_price": trade.execution_price,
            "slippage": trade.slippage,
            "brokerage_fees": trade.brokerage_fees,
            "realized_pnl": trade.realized_pnl,
            "status": order.status,
            "executed_at": trade.executed_at.isoformat() if trade.executed_at else None,
            "strategy_name": strategy_name
        })
    return out

@app.get("/api/v1/portfolio/summary")
async def get_portfolio_summary(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db)
):
    u_res = await db.execute(select(User).where(User.id == user_id))
    user = u_res.scalar_one_or_none()
    virtual_balance = user.virtual_balance if user else 500000.0

    query = (
        select(Trade, BrokerOrder)
        .join(BrokerOrder, Trade.broker_order_id == BrokerOrder.id)
        .join(StrategySubscription, BrokerOrder.strategy_subscription_id == StrategySubscription.id)
        .where(StrategySubscription.user_id == user_id)
    )
    result = await db.execute(query)
    trades = result.all()

    total_realized = 0.0
    total_brokerage = 0.0
    wins = 0
    losses = 0
    total_trades = len(trades)

    for row in trades:
        t = row[0]
        pnl = t.realized_pnl or 0.0
        total_realized += pnl
        total_brokerage += (t.brokerage_fees or 0.0)
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    decided_trades = wins + losses
    win_rate = round((wins / decided_trades) * 100.0, 1) if decided_trades > 0 else 0.0

    pos_query = select(Position).join(StrategySubscription).where(StrategySubscription.user_id == user_id)
    pos_res = await db.execute(pos_query)
    positions = pos_res.scalars().all()
    unrealized_total = sum(p.unrealized_pnl for p in positions)

    total_net_mtm = round(total_realized + unrealized_total, 2)

    return {
        "virtual_capital": 500000.0,
        "available_margin": round(virtual_balance, 2),
        "total_realized_pnl": round(total_realized, 2),
        "total_unrealized_pnl": round(unrealized_total, 2),
        "net_mtm": total_net_mtm,
        "total_brokerage": round(total_brokerage, 2),
        "net_pnl_after_fees": round(total_realized - total_brokerage, 2),
        "total_trades": total_trades,
        "winning_trades": wins,
        "losing_trades": losses,
        "win_rate_percent": win_rate
    }

@app.post("/api/v1/paper-trading/seed-demo")
async def manual_seed_trades(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    if not settings.DEMO_MODE:
        raise HTTPException(status_code=404, detail="Not found")
    await seed_user_past_trades(db, user_id)
    return {"message": "Demo past trades generated successfully"}

@app.post("/api/v1/paper-trading/reset")
async def reset_paper_trading(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    u_res = await db.execute(select(User).where(User.id == user_id))
    user = u_res.scalar_one_or_none()
    if user:
        user.virtual_balance = 500000.0
        await db.commit()
    return {"message": "Paper trading capital reset to ₹5,00,000", "virtual_balance": 500000.0}

@app.post("/api/v1/paper-trading/execute-demo-trade")
async def execute_demo_trade(
    payload: DemoTradeRequest,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db)
):
    side = payload.side
    qty = payload.quantity
    segment = payload.segment
    symbol = payload.symbol
    instrument_key = f"NSE_OPTION|{symbol}" if segment == "FNO" else f"NSE_INDEX|{symbol}"
    product_type = "MIS"

    sub_query = select(StrategySubscription).where(StrategySubscription.user_id == user_id)
    sub_res = await db.execute(sub_query)
    sub = sub_res.scalars().first()
    if not sub:
        v_res = await db.execute(select(StrategyVersion))
        v = v_res.scalars().first()
        if not v:
            raise HTTPException(status_code=503, detail="No strategy versions are registered yet.")
        sub = StrategySubscription(
            user_id=user_id,
            strategy_version_id=v.id,
            quantity=qty,
            product_type=product_type
        )
        db.add(sub)
        await db.commit()
        await db.refresh(sub)

    from app.services.execution import ExecutionService
    import time
    clean_ts = int(time.time() * 1000)
    client_order_id = f"co_demo_{clean_ts}_{side.lower()}"

    execution_service = ExecutionService(db)
    result = await execution_service.execute_trade(
        subscription_id=sub.id,
        side=side,
        instrument_key=instrument_key,
        quantity=qty,
        client_order_id=client_order_id,
        product_type=product_type,
        trading_symbol=symbol
    )

    await manager.send_to_user(user_id, _user_event("OrderFilled", {
        "trading_symbol": symbol,
        "side": side,
        "quantity": qty,
        "fill_price": result.get("fill_price", 150.0),
        "segment": segment
    }))

    return {"message": "Demo paper trade executed", "result": result}


@app.get("/api/v1/strategies")
async def get_strategies(db: AsyncSession = Depends(get_db)):
    query = select(StrategyDefinition)
    result = await db.execute(query)
    strategies = result.scalars().all()

    out = []
    for s in strategies:
        v_query = select(StrategyVersion).where(StrategyVersion.strategy_definition_id == s.id)
        v_res = await db.execute(v_query)
        versions = v_res.scalars().all()
        out.append({
            "id": s.id,
            "slug": s.slug,
            "name": s.name,
            "description": s.description,
            "category": s.category,
            "versions": [{"id": v.id, "semver": v.semver} for v in versions]
        })
    return out

@app.post("/api/v1/subscriptions", status_code=status.HTTP_201_CREATED)
async def subscribe(payload: SubscriptionRequest, user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    v_res = await db.execute(select(StrategyVersion).where(StrategyVersion.id == payload.strategy_version_id))
    if not v_res.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Strategy version not found")

    sub = StrategySubscription(
        user_id=user_id,
        strategy_version_id=payload.strategy_version_id,
        quantity=payload.quantity,
        product_type=payload.product_type,
        max_profit=payload.max_profit,
        max_loss=payload.max_loss,
        status="stopped",
        config_json=payload.config_json
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return {"subscription_id": sub.id}

@app.post("/api/v1/subscriptions/{id}/toggle")
async def toggle_strategy(id: str, payload: ToggleRequest, user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    query = select(StrategySubscription).where(StrategySubscription.id == id, StrategySubscription.user_id == user_id)
    result = await db.execute(query)
    sub = result.scalar_one_or_none()

    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    if payload.active:
        if not StrategyScheduler.is_market_open():
            bypass = (sub.config_json or {}).get("bypass_scheduler", False)
            if not bypass:
                raise HTTPException(status_code=400, detail="Cannot start strategy. Markets are closed.")

        sub.status = "running"
        await db.commit()

        if settings.RUN_STRATEGIES_IN_PROCESS:
            start_strategy_in_process(sub.id)
        else:
            # Enqueue start command task in Celery worker (RabbitMQ)
            try:
                start_strategy_task.delay(sub.id)
                logger.info(f"Strategy enqueued in Celery worker for sub={sub.id}")
            except Exception as celery_err:
                logger.warning(f"RabbitMQ/Celery connection failed. Running locally inside FastAPI loop: {str(celery_err)}")
                start_strategy_in_process(sub.id)

        await manager.send_to_user(user_id, _user_event("StrategyStarted", {"subscription_id": sub.id}))
    else:
        sub.status = "stopped"
        await db.commit()

        await manager.send_to_user(user_id, _user_event("StrategyStopped", {"subscription_id": sub.id}))

    return {"subscription_id": sub.id, "status": sub.status}

@app.get("/api/v1/portfolio/positions")
async def get_positions(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    query = select(Position).join(StrategySubscription).where(StrategySubscription.user_id == user_id)
    result = await db.execute(query)
    positions = result.scalars().all()
    return [
        {
            "instrument_key": pos.instrument_key,
            "trading_symbol": pos.trading_symbol,
            "quantity": pos.quantity,
            "average_price": pos.average_price,
            "unrealized_pnl": pos.unrealized_pnl,
            "realized_pnl": pos.realized_pnl
        }
        for pos in positions
    ]

# --- WEBSOCKET ROUTING ---
@app.websocket("/api/v1/ws/stream")
async def ws_endpoint(websocket: WebSocket, token: str = Query("")):
    # Browsers cannot set headers on WebSocket upgrades, so the JWT comes as ?token=
    user_id = decode_jwt(token) if token else None
    if not user_id:
        # Accept before closing so the browser receives code 1008 (and logs out) instead of a generic failure
        await websocket.accept()
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(websocket, user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)

from fastapi.staticfiles import StaticFiles

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
