"""Request body schemas for the public API. Invalid input is rejected with HTTP 422."""
from typing import Any, Dict, Literal, Optional
from pydantic import BaseModel, ConfigDict, EmailStr, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Side = Literal["BUY", "SELL"]
ProductType = Literal["MIS", "NRML"]
BrokerName = Literal["paper_trading", "kite", "shoonya"]


class RegisterRequest(_Strict):
    email: EmailStr
    # bcrypt only uses the first 72 bytes, so cap the length
    password: str = Field(min_length=8, max_length=64)
    first_name: str = Field(default="Algo", max_length=100)
    last_name: str = Field(default="Trader", max_length=100)


class LoginRequest(_Strict):
    email: EmailStr
    password: str = Field(min_length=1, max_length=64)


class BrokerCredentialsRequest(_Strict):
    broker_name: BrokerName = "paper_trading"
    api_key: str = Field(default="paper_key", max_length=256)
    client_id: str = Field(default="ALGO_USER", max_length=64)
    api_secret: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=256)
    totp_secret: str = Field(default="", max_length=256)


class BrokerVerifyRequest(_Strict):
    broker_name: BrokerName = "paper_trading"
    client_id: str = Field(default="", max_length=64)
    api_key: str = Field(default="", max_length=256)
    api_secret: str = Field(default="", max_length=256)


class SubscriptionRequest(_Strict):
    strategy_version_id: str = Field(min_length=1, max_length=36)
    quantity: int = Field(default=50, gt=0, le=10000)
    product_type: ProductType = "MIS"
    max_profit: float = Field(default=0.0, ge=0)
    max_loss: float = Field(default=0.0, ge=0)
    config_json: Dict[str, Any] = Field(default_factory=dict)


class ToggleRequest(_Strict):
    active: bool = False


class DemoTradeRequest(_Strict):
    side: Side = "BUY"
    quantity: int = Field(default=50, gt=0, le=10000)
    segment: Literal["FNO", "INTRADAY"] = "FNO"
    symbol: str = Field(default="NIFTY26JUL24300CE", pattern=r"^[A-Z0-9\-]{1,40}$")


class OrderRequest(_Strict):
    """Manual order from the Trade screen. instrument_key must be a contract with a known price."""
    instrument_key: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z_]+\|[A-Za-z0-9 \-]+$")
    trading_symbol: Optional[str] = Field(default=None, max_length=100)
    side: Side
    quantity: int = Field(gt=0, le=10000)
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price: Optional[float] = Field(default=None, gt=0)
    product_type: ProductType = "MIS"
    stop_loss: Optional[float] = Field(default=None, gt=0)
    target: Optional[float] = Field(default=None, gt=0)
