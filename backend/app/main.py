import os
import json
import logging
import datetime
import asyncio
import secrets
import time
from typing import Dict, Optional, Set
from fastapi import FastAPI, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
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
    StrategyVersion, StrategySubscription, Position, BrokerOrder, Trade, AuditLog
)
from app.schemas import (
    RegisterRequest, LoginRequest, BrokerCredentialsRequest, BrokerVerifyRequest,
    SubscriptionRequest, ToggleRequest, DemoTradeRequest, OrderRequest
)
from app.services.bootstrap import ensure_default_strategies
from app.services.demo_seed import seed_user_past_trades
from app.services.scheduler import StrategyScheduler
from app.workers.tasks import start_strategy_task
from app.core.memory_bus import memory_bus, market_snapshot, in_memory_quotes
from app.broker.upstox import UpstoxClient, UpstoxError, authorization_url
from app.services import upstox_session
from app.services.audit import log_audit
# Import strategy to trigger registration
from app.strategies.kaushalcustomWMASMACall1.strategy import KaushalCustomWMASMACall1
from app.strategies.aiOptionsAgent.strategy import AIOptionsAgent

# Configure logging
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # the market feed polls Upstox every 2s; keep request lines out of the logs
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
    Market data is broadcast to everyone; order and strategy events go to every device of their owner.

    Each socket gets its own outbound queue and sender task, so publishing never waits on a slow phone
    connection (the market-data loop and strategies publish through here). If a client falls far behind,
    its oldest messages are dropped; it re-syncs from the latest quote anyway.
    """
    QUEUE_LIMIT = 300

    def __init__(self):
        self.connections: Dict[str, Set[WebSocket]] = {}
        self._queues: Dict[WebSocket, asyncio.Queue] = {}
        self._senders: Dict[WebSocket, asyncio.Task] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.connections.setdefault(user_id, set()).add(websocket)
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_LIMIT)
        self._queues[websocket] = queue
        self._senders[websocket] = asyncio.create_task(self._sender(websocket, user_id, queue))

    def disconnect(self, websocket: WebSocket, user_id: str):
        sockets = self.connections.get(user_id)
        if sockets:
            sockets.discard(websocket)
            if not sockets:
                self.connections.pop(user_id, None)
        self._queues.pop(websocket, None)
        task = self._senders.pop(websocket, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    async def _sender(self, websocket: WebSocket, user_id: str, queue: asyncio.Queue):
        try:
            while True:
                message = await queue.get()
                await asyncio.wait_for(websocket.send_text(message), timeout=10)
        except asyncio.CancelledError:
            pass
        except Exception:
            self.disconnect(websocket, user_id)
            try:
                await websocket.close()
            except Exception:
                pass

    def _enqueue(self, websocket: WebSocket, message: str):
        queue = self._queues.get(websocket)
        if queue is None:
            return
        if queue.full():
            try:
                queue.get_nowait()  # drop the oldest; fresher data follows
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(message)

    async def broadcast(self, message: str):
        for sockets in list(self.connections.values()):
            for ws in list(sockets):
                self._enqueue(ws, message)

    async def send_to_user_socket(self, websocket: WebSocket, message: str):
        self._enqueue(websocket, message)

    async def send_to_user(self, user_id: str, message: str):
        for ws in list(self.connections.get(user_id, ())):
            self._enqueue(ws, message)

manager = ConnectionManager()

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

            await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)  # candles: strategies only
        except (ConnectionError, OSError, Exception):
            # Redis unreachable. We operate silently and rely on the Memory Bus callback
            pubsub = None
            redis_client = None
            await asyncio.sleep(5)
        await asyncio.sleep(0.1)

# Memory Bus ws callback for local execution fallback
async def memory_bus_ws_callback(channel: str, message: str):
    try:
        if channel == "market:quotes":
            await manager.broadcast(json.dumps({
                "event_type": "QuoteUpdate",
                "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                "data": json.loads(message)
            }))
        elif channel == "user:events":
            # Fills, rejections and AI status from strategies/execution: push to every device of that user
            evt = json.loads(message)
            await manager.send_to_user(evt["user_id"], _user_event(evt["event_type"], evt.get("data") or {}))
        # market:candles:* are for strategies only; quotes already carry every price the UI needs
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

    # Restore today's Upstox session (if any) so the feed starts live after a restart
    async with async_session_maker() as db:
        await upstox_session.load(db)

    # Start Market Data Feed locally inside the API process (Upstox when connected, simulator otherwise)
    from app.engine.market_data import MarketDataService
    mds = MarketDataService(candle_duration_sec=5) # simulator: 5-second candles for fast visual testing
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
        "is_active": user.is_active,
        "is_admin": user.email.lower() in settings.admin_emails,
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

@app.get("/api/v1/subscriptions")
async def list_subscriptions(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    """The user's strategy runs, newest first (manual-trading holder excluded)."""
    res = await db.execute(
        select(StrategySubscription)
        .where(StrategySubscription.user_id == user_id)
        .order_by(StrategySubscription.created_at.desc())
    )
    return [
        {
            "subscription_id": s.id,
            "strategy_version_id": s.strategy_version_id,
            "status": s.status,
            "quantity": s.quantity,
            "product_type": s.product_type,
            "max_profit": s.max_profit,
            "max_loss": s.max_loss,
            "config_json": s.config_json or {},
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in res.scalars().all()
        if not (s.config_json or {}).get("manual")
    ]

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

        # One running engine per user: stop anything else running (an older copy started before a page reload,
        # or a different strategy) so trades never double up and the dashboard always controls what is running
        others = await db.execute(select(StrategySubscription).where(
            StrategySubscription.user_id == user_id,
            StrategySubscription.status == "running",
            StrategySubscription.id != sub.id,
        ))
        for old in others.scalars().all():
            old.status = "stopped"
            logger.info(f"Stopping older running copy sub={old.id} in favour of sub={sub.id}")

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

# --- UPSTOX MARKET DATA CONNECTION ---

# One-time OAuth states: state -> (user_id, expires_at epoch)
_upstox_oauth_states: Dict[str, tuple] = {}

@app.get("/api/v1/broker/upstox/status")
async def upstox_status(user_id: str = Depends(get_current_user_id)):
    return {
        "configured": settings.upstox_configured,
        "data_source": market_snapshot.get("source", "simulator"),
        **upstox_session.status(),
    }

@app.get("/api/v1/broker/upstox/login")
async def upstox_login(user_id: str = Depends(get_current_user_id)):
    if not settings.upstox_configured:
        raise HTTPException(status_code=503, detail="Upstox API key/secret are not configured on the server.")
    now = time.time()
    for s, (_, exp) in list(_upstox_oauth_states.items()):
        if exp < now:
            _upstox_oauth_states.pop(s, None)
    state = secrets.token_urlsafe(24)
    _upstox_oauth_states[state] = (user_id, now + 600)
    return {"url": authorization_url(settings.UPSTOX_API_KEY, settings.UPSTOX_REDIRECT_URI, state)}

@app.get("/api/v1/broker/upstox/callback", include_in_schema=False)
async def upstox_callback(code: str = "", state: str = "", db: AsyncSession = Depends(get_db)):
    """Upstox redirects the browser here after login; no Authorization header, so `state` authenticates it."""
    entry = _upstox_oauth_states.pop(state, None)
    if not entry or entry[1] < time.time():
        return RedirectResponse("/?broker=upstox_error&reason=expired_login_link", status_code=303)
    if not code:
        return RedirectResponse("/?broker=upstox_error&reason=login_cancelled", status_code=303)
    try:
        body = await UpstoxClient().exchange_code(
            code, settings.UPSTOX_API_KEY, settings.UPSTOX_API_SECRET, settings.UPSTOX_REDIRECT_URI
        )
    except UpstoxError as e:
        logger.warning(f"Upstox token exchange failed: {e}")
        return RedirectResponse("/?broker=upstox_error&reason=token_exchange_failed", status_code=303)

    await upstox_session.save(db, body["access_token"], body.get("user_name"))
    await log_audit(db=db, user_id=entry[0], event_type="UPSTOX_CONNECTED",
                    message=f"Upstox market data connected ({body.get('user_name') or 'unknown user'})")
    return RedirectResponse("/?broker=upstox_connected", status_code=303)

@app.post("/api/v1/broker/upstox/disconnect")
async def upstox_disconnect(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    await upstox_session.clear(db)
    return {"connected": False}

@app.get("/api/v1/market/snapshot")
async def get_market_snapshot(user_id: str = Depends(get_current_user_id)):
    """Option chain window around ATM with live prices; refreshed by the market data loop."""
    return market_snapshot

_candle_backfill_at: Dict[str, float] = {}

@app.get("/api/v1/market/candles")
async def get_market_candles(instrument_key: str, user_id: str = Depends(get_current_user_id)):
    """Today's 1-minute candles for one instrument (the last one may still be forming).

    Bars are built live from quotes; missing minutes (server started late, or the contract was outside the
    quoted ATM window for a while) are filled from Upstox intraday candles, throttled per instrument. `time` is the IST wall-clock minute expressed as a
    UTC epoch, so charts that render UTC show Indian market time.
    """
    import time as _time
    from datetime import timezone
    from app.core.memory_bus import minute_bars
    from app.engine import minute_bars as mb

    now = _time.time()
    bars = minute_bars.get(instrument_key, [])
    session_open = 9 * 60 + 15
    today, minute_now = mb.ist_minute(now)
    if (market_snapshot.get("source") == "upstox" and upstox_session.is_connected()
            and minute_now > session_open + 1
            # late start or holes (a contract is only quoted live while it sits near the ATM strike)
            and len(bars) < min(minute_now, 15 * 60 + 30) - session_open - 1
            and now - _candle_backfill_at.get(instrument_key, 0) > 120):
        _candle_backfill_at[instrument_key] = now
        try:
            from app.broker.upstox import UpstoxClient
            hist = await UpstoxClient(upstox_session.session["token"]).get_intraday_candles(instrument_key)
            mb.backfill(instrument_key, [c for c in hist if c["date"] == today], now)
            bars = minute_bars.get(instrument_key, [])
        except Exception as e:
            logger.warning(f"Candle backfill failed for {instrument_key}: {e}")

    day0 = int(datetime.datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp())
    return {
        "instrument_key": instrument_key,
        "source": market_snapshot.get("source"),
        "bars": [{"time": day0 + b["minute"] * 60, "open": b["open"], "high": b["high"],
                  "low": b["low"], "close": b["close"]} for b in bars],
    }

@app.get("/api/v1/public/overview")
async def public_overview():
    """Landing-page data (no login): NIFTY spot, ATM premiums and the AI model's test results."""
    from app.ml.agent import load_models
    snap = market_snapshot
    atm = next((r for r in snap.get("rows", []) if r.get("strike") == snap.get("atm_strike")), None) or {}
    meta = ((load_models() or {}).get("meta") or {})
    return {
        "ticker": {
            "source": snap.get("source"),
            "spot": snap.get("spot"),
            "prev_close": snap.get("spot_prev_close"),
            "expiry": snap.get("expiry"),
            "atm_strike": snap.get("atm_strike"),
            "atm_ce": (atm.get("ce") or {}).get("ltp"),
            "atm_pe": (atm.get("pe") or {}).get("ltp"),
        },
        "ai_model": {
            "backtest": (meta.get("results") or {}).get("test"),
            "walk_forward": meta.get("walk_forward"),
        },
    }

@app.get("/api/v1/ai/status")
async def get_ai_status(user_id: str = Depends(get_current_user_id)):
    """The AI agent's latest view for this user, plus how the deployed model did in its backtest."""
    from app.core.memory_bus import ai_status
    from app.ml.agent import load_models
    models = load_models()
    meta = (models or {}).get("meta", {})
    return {
        "status": ai_status.get(user_id),
        "model": {
            "deployed": models is not None,
            "exit_rule": meta.get("exit_rule"),
            "sim": meta.get("sim"),
            "trained_until": meta.get("train_end"),
            "backtest": (meta.get("results") or {}).get("test"),
            "walk_forward": meta.get("walk_forward"),
        } if models else {"deployed": False},
    }

# --- ADMIN: AI LAB ---

async def require_admin(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)) -> str:
    user = await db.get(User, user_id)
    if not user or user.email.lower() not in settings.admin_emails:
        raise HTTPException(status_code=403, detail="Admins only")
    return user_id

def _journal_symbol(message: str) -> str:
    parts = (message or "").split(" ")  # "AI BUY NIFTY25N1325500CE: FILLED"
    return parts[2].rstrip(":") if len(parts) > 2 else ""

@app.get("/api/v1/admin/ai-lab")
async def admin_ai_lab(_: str = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Everything about the model for the admin: how it was trained, what it thinks now and why, and how its
    own live trades turned out (predicted reward vs actual)."""
    from app.core.memory_bus import ai_score_log, ai_status
    from app.ml import live as ml_live
    from app.ml.agent import load_models
    from app.services.upstox_session import IST

    models = load_models()
    rows = (await db.execute(
        select(AuditLog, User.email).join(User, User.id == AuditLog.user_id)
        .where(AuditLog.event_type == "AI_TRADE_JOURNAL")
        .order_by(AuditLog.created_at.desc()).limit(400)
    )).all()
    journal = [{"user_id": a.user_id, "user": email, "at": a.created_at.isoformat() + "Z",
                "symbol": _journal_symbol(a.message), "metadata": a.metadata_json or {}}
               for a, email in reversed(rows)]
    return {
        "deployed": models is not None,
        "meta": (models or {}).get("meta"),
        "report": ml_live.load_report(),
        "live": ml_live.decide(datetime.datetime.now(IST)),
        "score_log": ai_score_log.get("points", []),
        "agents_running": [{"mode": s.get("mode"), "status": s.get("status"), "message": s.get("message"),
                            "trades_today": s.get("trades_today")} for s in ai_status.values()],
        "journal": ml_live.pair_journal(journal),
        "market_source": market_snapshot.get("source"),
    }

# --- MANUAL ORDERS (Trade screen) ---

MANUAL_SUBSCRIPTION_QTY = 1800  # RMS caps a single order at 2x this

async def _manual_subscription(db: AsyncSession, user_id: str) -> StrategySubscription:
    res = await db.execute(select(StrategySubscription).where(StrategySubscription.user_id == user_id))
    for sub in res.scalars().all():
        if (sub.config_json or {}).get("manual"):
            return sub
    version = (await db.execute(select(StrategyVersion))).scalars().first()
    if not version:
        raise HTTPException(status_code=503, detail="No strategy versions are registered yet.")
    sub = StrategySubscription(
        user_id=user_id, strategy_version_id=version.id, status="manual",
        quantity=MANUAL_SUBSCRIPTION_QTY, config_json={"manual": True}
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub

@app.post("/api/v1/orders")
async def place_manual_order(payload: OrderRequest, user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    if payload.instrument_key.startswith("NSE_INDEX|"):
        raise HTTPException(status_code=400, detail="An index cannot be traded directly. Pick an option contract.")
    if payload.order_type == "LIMIT" and payload.limit_price is None:
        raise HTTPException(status_code=422, detail="limit_price is required for LIMIT orders")

    sub = await _manual_subscription(db, user_id)
    from app.services.execution import ExecutionService
    client_order_id = f"mo_{user_id[:8]}_{int(time.time() * 1000)}_{payload.side.lower()}"
    quote = in_memory_quotes.get(payload.instrument_key) or {}
    result = await ExecutionService(db).execute_trade(
        subscription_id=sub.id,
        side=payload.side,
        instrument_key=payload.instrument_key,
        quantity=payload.quantity,
        client_order_id=client_order_id,
        order_type=payload.order_type,
        limit_price=payload.limit_price,
        product_type=payload.product_type,
        trading_symbol=payload.trading_symbol,
        entry_price=payload.limit_price or quote.get("ltp"),
        stop_loss=payload.stop_loss,
        target=payload.target,
    )
    return result  # OrderFilled / OrderRejected reach every device from ExecutionService

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
            text = await websocket.receive_text()
            if text == "ping":  # client heartbeat: lets phones detect a dead connection after sleeping
                await manager.send_to_user_socket(websocket, json.dumps({"event_type": "Pong", "data": {}}))
    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)
    except Exception:
        manager.disconnect(websocket, user_id)

from fastapi.staticfiles import StaticFiles

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
