import os
import json
import logging
import datetime
import hashlib
import jwt
import asyncio
from typing import Dict, Any, List, Set
from cryptography.fernet import Fernet
from fastapi import FastAPI, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.database import get_db, async_session_maker
from app.db.models import User, UserSession, BrokerCredential, StrategyDefinition, StrategyVersion, StrategySubscription, Position, BrokerOrder
from app.services.scheduler import StrategyScheduler
from app.workers.tasks import start_strategy_task
# Import strategy to trigger registration
from app.strategies.kaushalcustomWMASMACall1.strategy import KaushalCustomWMASMACall1

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api_gateway")

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    docs_url="/docs"
)

# Enable CORS for frontend dashboard interactions
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()
fernet = Fernet(settings.ENCRYPTION_KEY.encode() if isinstance(settings.ENCRYPTION_KEY, str) else settings.ENCRYPTION_KEY)

# --- SECURITY HELPERS ---
def hash_password(password: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", 
        password.encode("utf-8"), 
        b"salt_options_saas_platform_key", 
        100000
    ).hex()

def generate_jwt(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

async def get_current_user_id(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload["sub"]
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

# --- WEBSOCKET CONNECTION MANAGER ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass

manager = ConnectionManager()

# Background task to subscribe to Redis and broadcast events to all open WebSockets
async def redis_event_bus_listener():
    redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = redis_client.pubsub()
    await pubsub.psubscribe("market:candles:*")
    logger.info("Websocket event bus listener active.")
    
    while True:
        try:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg:
                # Wrap candle closed event in a structured PnL / data broadcast
                data = json.loads(msg["data"])
                event = {
                    "event_type": "TickUpdate",
                    "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                    "data": {
                        "instrument_key": data["instrument_key"],
                        "ltp": data["close"],
                        "candle": data
                    }
                }
                await manager.broadcast(json.dumps(event))
        except Exception as e:
            logger.error(f"Error in Redis listener thread: {str(e)}")
            await asyncio.sleep(1)
        await asyncio.sleep(0.1)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(redis_event_bus_listener())
    
    # Pre-populate dynamic strategy definitions if not present
    async with async_session_maker() as db:
        query = select(StrategyDefinition).where(StrategyDefinition.slug == "kaushalcustomWMASMACall1")
        result = await db.execute(query)
        existing = result.scalar_one_or_none()
        
        if not existing:
            definition = StrategyDefinition(
                slug="kaushalcustomWMASMACall1",
                name="Nifty ATM WMA5/SMA1 Crossover Call",
                description="NIFTY ATM option using Heikin Ashi candles. Crossovers of WMA5 close and SMA1 open triggers trades.",
                category="Options",
                is_public=True
            )
            db.add(definition)
            await db.commit()
            await db.refresh(definition)
            
            version = StrategyVersion(
                strategy_definition_id=definition.id,
                semver="1.0.0",
                parameters_schema_json={"timeframeMin": 1, "quantity": 50}
            )
            db.add(version)
            await db.commit()

# --- API ENDPOINTS ---

@app.get("/health")
async def health_check():
    # Simple dependency checks
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
    # Basic Prometheus exposition output format
    async with async_session_maker() as db:
        res = await db.execute(select(StrategySubscription))
        count = len(res.scalars().all())
    return (
        f"# HELP active_strategy_subscriptions Total active trading subscribers\n"
        f"# TYPE active_strategy_subscriptions gauge\n"
        f"active_strategy_subscriptions {count}\n"
    )

@app.post("/api/v1/auth/register", status_code=status.HTTP_201_CREATED)
async def register(payload: Dict[str, Any], db: AsyncSession = Depends(get_db)):
    email = payload.get("email")
    password = payload.get("password")
    
    query = select(User).where(User.email == email)
    result = await db.execute(query)
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")
        
    user = User(
        email=email,
        password_hash=hash_password(password),
        first_name=payload.get("first_name"),
        last_name=payload.get("last_name")
    )
    db.add(user)
    await db.commit()
    return {"message": "User registered successfully"}

@app.post("/api/v1/auth/login")
async def login(payload: Dict[str, Any], db: AsyncSession = Depends(get_db)):
    email = payload.get("email")
    password = payload.get("password")
    
    query = select(User).where(User.email == email)
    result = await db.execute(query)
    user = result.scalar_one_or_none()
    
    if not user or user.password_hash != hash_password(password):
        raise HTTPException(status_code=400, detail="Incorrect email or password")
        
    token = generate_jwt(user.id)
    return {"access_token": token, "token_type": "bearer"}

@app.post("/api/v1/broker/credentials")
async def save_credentials(payload: Dict[str, Any], user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    broker_name = payload.get("broker_name")
    api_key = payload.get("api_key")
    client_id = payload.get("client_id")
    
    # Encrypt inputs
    key_enc = fernet.encrypt(api_key.encode()).decode()
    client_enc = fernet.encrypt(client_id.encode()).decode()
    
    # Check if credentials exist
    query = select(BrokerCredential).where(BrokerCredential.user_id == user_id, BrokerCredential.broker_name == broker_name)
    result = await db.execute(query)
    cred = result.scalar_one_or_none()
    
    if not cred:
        cred = BrokerCredential(
            user_id=user_id,
            broker_name=broker_name,
            api_key_encrypted=key_enc,
            client_id_encrypted=client_enc
        )
        db.add(cred)
    else:
        cred.api_key_encrypted = key_enc
        cred.client_id_encrypted = client_enc
        
    await db.commit()
    return {"message": "Credentials encrypted and saved successfully"}

@app.get("/api/v1/strategies")
async def get_strategies(db: AsyncSession = Depends(get_db)):
    query = select(StrategyDefinition)
    result = await db.execute(query)
    strategies = result.scalars().all()
    
    out = []
    for s in strategies:
        # Load versions
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
async def subscribe(payload: Dict[str, Any], user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    version_id = payload.get("strategy_version_id")
    qty = payload.get("quantity", 50)
    product_type = payload.get("product_type", "MIS")
    max_profit = payload.get("max_profit", 0.0)
    max_loss = payload.get("max_loss", 0.0)
    
    sub = StrategySubscription(
        user_id=user_id,
        strategy_version_id=version_id,
        quantity=qty,
        product_type=product_type,
        max_profit=max_profit,
        max_loss=max_loss,
        status="stopped",
        config_json=payload.get("config_json", {})
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return {"subscription_id": sub.id}

@app.post("/api/v1/subscriptions/{id}/toggle")
async def toggle_strategy(id: str, payload: Dict[str, Any], user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    active = payload.get("active", False)
    
    query = select(StrategySubscription).where(StrategySubscription.id == id, StrategySubscription.user_id == user_id)
    result = await db.execute(query)
    sub = result.scalar_one_or_none()
    
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
        
    if active:
        # Check market timings scheduler constraint
        if not StrategyScheduler.is_market_open():
            # For local testing, allow bypassing this constraint if config_json contains bypass_scheduler
            bypass = (sub.config_json or {}).get("bypass_scheduler", False)
            if not bypass:
                raise HTTPException(status_code=400, detail="Cannot start strategy. Markets are closed.")
                
        sub.status = "running"
        await db.commit()
        
        # Enqueue start command task in RabbitMQ Worker Queue
        start_strategy_task.delay(sub.id)
        
        # Broadcast event
        event = {
            "event_type": "StrategyStarted",
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "data": {"subscription_id": sub.id, "user_id": user_id}
        }
        await manager.broadcast(json.dumps(event))
    else:
        sub.status = "stopped"
        await db.commit()
        
        # Broadcast event
        event = {
            "event_type": "StrategyStopped",
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "data": {"subscription_id": sub.id, "user_id": user_id}
        }
        await manager.broadcast(json.dumps(event))
        
    return {"subscription_id": sub.id, "status": sub.status}

@app.get("/api/v1/portfolio/positions")
async def get_positions(user_id: str = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    # Fetch positions for all subscriptions of this user
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
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

from fastapi.staticfiles import StaticFiles

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

