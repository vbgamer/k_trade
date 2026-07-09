import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Float, Boolean, DateTime, ForeignKey, JSON, Text
from sqlalchemy.orm import relationship
from app.core.database import Base

def generate_uuid() -> str:
    return str(uuid.uuid4())

class User(Base):
    __tablename__ = "users"
    
    id = Column(String(36), primary_key=True, default=generate_uuid)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")
    credentials = relationship("BrokerCredential", back_populates="user", cascade="all, delete-orphan")
    subscriptions = relationship("StrategySubscription", back_populates="user", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="user", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")

class UserSession(Base):
    __tablename__ = "user_sessions"
    
    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token = Column(String(500), unique=True, nullable=False)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(255), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="sessions")

class BrokerCredential(Base):
    __tablename__ = "broker_credentials"
    
    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    broker_name = Column(String(50), nullable=False)  # e.g., "kite", "shoonya"
    api_key_encrypted = Column(Text, nullable=False)
    api_secret_encrypted = Column(Text, nullable=True)
    client_id_encrypted = Column(Text, nullable=False)
    password_encrypted = Column(Text, nullable=True)
    totp_secret_encrypted = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user = relationship("User", back_populates="credentials")

class StrategyDefinition(Base):
    __tablename__ = "strategy_definitions"
    
    id = Column(String(36), primary_key=True, default=generate_uuid)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    name = Column(String(150), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(100), default="Options")
    is_public = Column(Boolean, default=True)
    creator_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    versions = relationship("StrategyVersion", back_populates="definition", cascade="all, delete-orphan")

class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    
    id = Column(String(36), primary_key=True, default=generate_uuid)
    strategy_definition_id = Column(String(36), ForeignKey("strategy_definitions.id", ondelete="CASCADE"), nullable=False)
    semver = Column(String(20), nullable=False)  # e.g. "1.0.0"
    parameters_schema_json = Column(JSON, nullable=True)  # Schema defining settings
    runtime_code_hash = Column(String(64), nullable=True)
    released_at = Column(DateTime, default=datetime.utcnow)
    
    definition = relationship("StrategyDefinition", back_populates="versions")
    subscriptions = relationship("StrategySubscription", back_populates="version", cascade="all, delete-orphan")

class StrategySubscription(Base):
    __tablename__ = "strategy_subscriptions"
    
    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    strategy_version_id = Column(String(36), ForeignKey("strategy_versions.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(50), default="stopped")  # stopped, running, paused, error
    max_profit = Column(Float, default=0.0)
    max_loss = Column(Float, default=0.0)
    quantity = Column(Integer, nullable=False, default=50)
    product_type = Column(String(20), default="MIS")  # MIS, NRML
    config_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="subscriptions")
    version = relationship("StrategyVersion", back_populates="subscriptions")
    positions = relationship("Position", back_populates="subscription", cascade="all, delete-orphan")
    orders = relationship("BrokerOrder", back_populates="subscription", cascade="all, delete-orphan")

class Position(Base):
    __tablename__ = "positions"
    
    id = Column(String(36), primary_key=True, default=generate_uuid)
    strategy_subscription_id = Column(String(36), ForeignKey("strategy_subscriptions.id", ondelete="CASCADE"), nullable=False)
    instrument_key = Column(String(100), nullable=False)  # Unique contract identifier
    trading_symbol = Column(String(100), nullable=False)
    quantity = Column(Integer, nullable=False, default=0)  # Positive for Long, Negative for Short
    average_price = Column(Float, nullable=False, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    subscription = relationship("StrategySubscription", back_populates="positions")

class BrokerOrder(Base):
    __tablename__ = "broker_orders"
    
    id = Column(String(36), primary_key=True, default=generate_uuid)
    strategy_subscription_id = Column(String(36), ForeignKey("strategy_subscriptions.id", ondelete="CASCADE"), nullable=False)
    client_order_id = Column(String(100), unique=True, nullable=False, index=True)
    broker_order_id = Column(String(100), unique=True, nullable=True, index=True)
    side = Column(String(10), nullable=False)  # BUY, SELL
    instrument_key = Column(String(100), nullable=False)
    order_type = Column(String(20), default="MARKET")  # MARKET, LIMIT
    quantity = Column(Integer, nullable=False)
    product_type = Column(String(20), default="MIS")  # MIS, NRML
    status = Column(String(50), default="PENDING")  # PENDING, FILLED, REJECTED, CANCELLED
    rejection_reason = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    subscription = relationship("StrategySubscription", back_populates="orders")
    trades = relationship("Trade", back_populates="order", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="order")

class Trade(Base):
    __tablename__ = "trades"
    
    id = Column(String(36), primary_key=True, default=generate_uuid)
    broker_order_id = Column(String(36), ForeignKey("broker_orders.id", ondelete="CASCADE"), nullable=False)
    execution_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    slippage = Column(Float, default=0.0)
    brokerage_fees = Column(Float, default=0.0)
    executed_at = Column(DateTime, default=datetime.utcnow)
    
    order = relationship("BrokerOrder", back_populates="trades")

class AuditLog(Base):
    __tablename__ = "audit_logs"
    
    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    broker_order_id = Column(String(36), ForeignKey("broker_orders.id", ondelete="SET NULL"), nullable=True)
    event_type = Column(String(100), nullable=False)  # e.g. "AUTH_LOGIN", "STRATEGY_START", "RISK_TRIGGER", "ORDER_SENT"
    message = Column(Text, nullable=False)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="audit_logs")
    order = relationship("BrokerOrder", back_populates="audit_logs")

class Notification(Base):
    __tablename__ = "notifications"
    
    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(50), nullable=False)  # "info", "warning", "error", "trade"
    title = Column(String(150), nullable=False)
    body = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="notifications")

class SystemSettings(Base):
    __tablename__ = "system_settings"
    
    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    description = Column(String(255), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
