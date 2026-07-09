from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "trading_workers",
    broker=settings.RABBITMQ_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.tasks"]
)

# Standard performance settings for trading
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Kolkata",
    enable_utc=False,
    worker_prefetch_multiplier=1,  # Deliver task one at a time for strategy run loops
    task_acks_late=True,           # Retry on crash
)
