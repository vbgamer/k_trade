import datetime
import pytz
from typing import Set
from app.core.config import settings

# Predefined market holidays list (example Indian NSE market holidays in 2026/standard holidays)
HOLIDAYS: Set[str] = {
    "2026-01-26",  # Republic Day
    "2026-03-06",  # Holi
    "2026-04-02",  # Ram Navami
    "2026-05-01",  # Maharashtra Day / May Day
    "2026-08-15",  # Independence Day
    "2026-10-02",  # Gandhi Jayanti
    "2026-12-25",  # Christmas
}

class StrategyScheduler:
    """
    Market hours schedule & holiday execution check engine.
    Ensures algorithms only trade within exchange boundary constraints.
    """
    
    @staticmethod
    def get_ist_now() -> datetime.datetime:
        ist = pytz.timezone("Asia/Kolkata")
        return datetime.datetime.now(ist)

    @classmethod
    def is_holiday(cls, dt: datetime.datetime) -> bool:
        # Check for weekends (Saturday=5, Sunday=6)
        if dt.weekday() >= 5:
            return True
            
        # Check standard holiday lists
        date_str = dt.strftime("%Y-%m-%d")
        if date_str in HOLIDAYS:
            return True
            
        return False

    @classmethod
    def is_market_open(cls) -> bool:
        """
        Validates if current time is within trading window.
        """
        now = cls.get_ist_now()
        
        # Holiday gate
        if cls.is_holiday(now):
            return False
            
        # Time boundary gates
        open_time = now.replace(
            hour=settings.MARKET_OPEN_HOUR, 
            minute=settings.MARKET_OPEN_MINUTE, 
            second=0, 
            microsecond=0
        )
        close_time = now.replace(
            hour=settings.MARKET_CLOSE_HOUR, 
            minute=settings.MARKET_CLOSE_MINUTE, 
            second=0, 
            microsecond=0
        )
        
        return open_time <= now <= close_time
