"""
Holds the Upstox access token used for the platform's market data feed.
Persisted encrypted in system_settings so an API restart keeps the day's session.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import encrypt, decrypt
from app.db.models import SystemSettings

logger = logging.getLogger("upstox_session")

IST = timezone(timedelta(hours=5, minutes=30))
_TOKEN_KEY = "upstox_access_token"
_META_KEY = "upstox_session_meta"  # "<connected_at ISO>|<user name>"

# Process-wide session; read by the market data loop on every iteration
session: Dict[str, Any] = {"token": None, "connected_at": None, "user_name": None, "last_error": None}


def token_expiry(connected_at: datetime) -> datetime:
    """Upstox access tokens stop working at 3:30 AM IST the day after they are issued."""
    local = connected_at.astimezone(IST)
    cutoff = local.replace(hour=3, minute=30, second=0, microsecond=0)
    return cutoff if local < cutoff else cutoff + timedelta(days=1)


def is_connected(now: Optional[datetime] = None) -> bool:
    if not session["token"] or not session["connected_at"]:
        return False
    return (now or datetime.now(timezone.utc)) < token_expiry(session["connected_at"])


def status() -> Dict[str, Any]:
    connected = is_connected()
    return {
        "connected": connected,
        "user_name": session["user_name"] if connected else None,
        "connected_at": session["connected_at"].isoformat() if connected else None,
        "expires_at": token_expiry(session["connected_at"]).isoformat() if connected else None,
        "last_error": session["last_error"],
    }


async def _upsert(db: AsyncSession, key: str, value: str) -> None:
    row = await db.get(SystemSettings, key)
    if row:
        row.value = value
    else:
        db.add(SystemSettings(key=key, value=value))


async def save(db: AsyncSession, token: str, user_name: Optional[str]) -> None:
    now = datetime.now(timezone.utc)
    session.update(token=token, connected_at=now, user_name=user_name, last_error=None)
    await _upsert(db, _TOKEN_KEY, encrypt(token))
    await _upsert(db, _META_KEY, f"{now.isoformat()}|{user_name or ''}")
    await db.commit()


async def clear(db: Optional[AsyncSession] = None, reason: Optional[str] = None) -> None:
    session.update(token=None, connected_at=None, user_name=None, last_error=reason)
    if db is not None:
        for key in (_TOKEN_KEY, _META_KEY):
            row = await db.get(SystemSettings, key)
            if row:
                await db.delete(row)
        await db.commit()


async def load(db: AsyncSession) -> None:
    token_row = await db.get(SystemSettings, _TOKEN_KEY)
    meta_row = await db.get(SystemSettings, _META_KEY)
    if not token_row or not meta_row:
        return
    try:
        connected_at_s, _, user_name = meta_row.value.partition("|")
        session.update(
            token=decrypt(token_row.value),
            connected_at=datetime.fromisoformat(connected_at_s),
            user_name=user_name or None,
        )
    except Exception as e:  # wrong ENCRYPTION_KEY or corrupt row: behave as disconnected
        logger.warning(f"Could not restore Upstox session: {e}")
        return
    if is_connected():
        logger.info("Restored today's Upstox market data session.")
