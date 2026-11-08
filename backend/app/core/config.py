import os
from typing import List
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/.env, resolved independently of the current working directory
ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, case_sensitive=True, extra="ignore")

    PROJECT_NAME: str = "Enterprise Options Trading SaaS"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "development"  # development | production

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./trading.db"

    # Messaging and Event Bus
    RABBITMQ_URL: str = "amqp://guest:guest@localhost:5672//"
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT Security (required: no defaults, generate with `python manage.py genkeys`)
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 1 week

    # Encryption key for broker credential vault (Fernet key format, required)
    ENCRYPTION_KEY: str

    # Comma-separated list of allowed browser origins
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000"

    # Enables demo-only endpoints such as seeding sample trades
    DEMO_MODE: bool = False

    # Run strategy loops inside the API process instead of Celery workers (single-server deploys).
    # Strategies left "running" are resumed automatically when the API restarts.
    RUN_STRATEGIES_IN_PROCESS: bool = False

    # Set false on a public server once your own account exists, so strangers cannot sign up
    ALLOW_REGISTRATION: bool = True

    # System settings
    MARKET_OPEN_HOUR: int = 9
    MARKET_OPEN_MINUTE: int = 15
    MARKET_CLOSE_HOUR: int = 15
    MARKET_CLOSE_MINUTE: int = 30

    @field_validator("SECRET_KEY")
    @classmethod
    def _secret_key_strength(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters. Run `python manage.py genkeys`.")
        return v

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() == "production"


settings = Settings()
