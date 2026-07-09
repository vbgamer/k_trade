from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import AuditLog

async def log_audit(
    db: AsyncSession,
    user_id: str,
    event_type: str,
    message: str,
    broker_order_id: Optional[str] = None,
    metadata_json: Optional[Dict[str, Any]] = None
) -> AuditLog:
    """
    Creates an immutable audit log entry in the database.
    Useful for transaction records, configuration overrides, and security events.
    """
    log_entry = AuditLog(
        user_id=user_id,
        broker_order_id=broker_order_id,
        event_type=event_type,
        message=message,
        metadata_json=metadata_json or {}
    )
    db.add(log_entry)
    await db.commit()
    await db.refresh(log_entry)
    return log_entry
