from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional

class BaseBrokerAdapter(ABC):
    """
    Abstract Base Class for Broker Integrations (e.g. Zerodha Kite, Finvasia Shoonya, mock live broker).
    Enforces a common interface for SaaS multi-tenant execution.
    """
    
    @abstractmethod
    async def login(self, credentials: Dict[str, Any]) -> bool:
        """
        Authenticate with the broker using credentials (API keys, password, client ID, and TOTP secret).
        """
        pass

    @abstractmethod
    async def place_order(
        self,
        client_order_id: str,
        side: str,
        instrument_key: str,
        quantity: int,
        order_type: str = "MARKET",
        limit_price: Optional[float] = None,
        product_type: str = "MIS"
    ) -> Dict[str, Any]:
        """
        Dispatches an order to the broker and returns execution result.
        Returns:
            dict containing:
                "status": "FILLED", "REJECTED", "PENDING"
                "broker_order_id": str (optional)
                "fill_price": float (if filled)
                "rejection_reason": str (if rejected)
        """
        pass

    @abstractmethod
    async def get_ltp(self, instrument_key: str) -> float:
        """
        Retrieves the real-time Last Traded Price (LTP) for a specific contract.
        """
        pass

    @abstractmethod
    async def get_option_chain(self, underlying_key: str) -> List[Dict[str, Any]]:
        """
        Retrieves option chain contracts for an index (e.g., NIFTY 50).
        """
        pass

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """
        Cancels a pending order at the exchange.
        """
        pass
