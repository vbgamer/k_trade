import logging
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Notification

logger = logging.getLogger("notifications")

class NotificationService:
    """
    Notification Dispatcher.
    Distributes critical logs and order alerts over push, email, or Telegram channels.
    """
    
    @staticmethod
    async def dispatch_notification(
        db: AsyncSession,
        user_id: str,
        notif_type: str,  # info, warning, error, trade
        title: str,
        body: str
    ) -> Notification:
        # 1. Log notification in database
        notif = Notification(
            user_id=user_id,
            type=notif_type,
            title=title,
            body=body,
            is_read=False
        )
        db.add(notif)
        await db.commit()
        await db.refresh(notif)
        
        # 2. Distribute alerts to active channels (Email, Telegram, Push notifications)
        logger.info(f"[NOTIFICATION ALERT] To User: {user_id} | Type: {notif_type.upper()} | {title}: {body}")
        
        # Simulated Telegram integration
        # (For production, this hits: https://api.telegram.org/bot<token>/sendMessage)
        logger.debug(f"[Telegram MOCK] Sent message: {title} - {body}")
        
        # Simulated SMTP email integration
        logger.debug(f"[Email MOCK] Sent email notification: {title}")
        
        return notif
