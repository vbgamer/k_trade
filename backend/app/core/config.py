import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_NAME: str = "Enterprise Options Trading SaaS"
    API_V1_STR: str = "/api/v1"
    
    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./trading.db")
    
    # Messaging and Event Bus
    RABBITMQ_URL: str = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672//")
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    
    # JWT Security
    SECRET_KEY: str = os.getenv("SECRET_KEY", "9ca7688267df88e99e4cb2e64dcfa82e88a3857d4a6bb4cb4a8f94d13bfdc9e9")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 1 week
    
    # Encryption key for AES vault (Fernet key format)
    ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "h8m3n8L5d_5rV-Fj1K3Pz9OqP7L9wN3mK5L8J9R3f2U=")
    
    # System settings
    MARKET_OPEN_HOUR: int = 9
    MARKET_OPEN_MINUTE: int = 15
    MARKET_CLOSE_HOUR: int = 15
    MARKET_CLOSE_MINUTE: int = 30
    
    class Config:
        case_sensitive = True
        env_file = ".env"

settings = Settings()
