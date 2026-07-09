from abc import ABC, abstractmethod
from typing import Dict, Any, Type, Optional, List

class BaseStrategy(ABC):
    """
    Common interface for all option strategies loaded by the Strategy Runtime.
    """
    
    @abstractmethod
    def __init__(self, parameters: Dict[str, Any]):
        """
        Initialize strategy with parameter dictionary (e.g. timeframe, quant, max loss).
        """
        self.parameters = parameters

    @abstractmethod
    async def on_candle(
        self,
        candle_data: Dict[str, Any],
        open_positions: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """
        Called when a new candle closes.
        Args:
            candle_data: Dict containing open, high, low, close, time, etc.
            open_positions: List of dictionaries of currently active positions.
        Returns:
            dict containing order action (e.g., {"action": "BUY"/"SELL"/"HOLD", "instrument_key": str}) or None.
        """
        pass


class StrategyRegistry:
    """
    Global in-memory catalog of strategy definitions and versions.
    """
    _registry: Dict[str, Type[BaseStrategy]] = {}

    @classmethod
    def register(cls, slug: str, strategy_class: Type[BaseStrategy]):
        cls._registry[slug] = strategy_class

    @classmethod
    def get_strategy(cls, slug: str) -> Optional[Type[BaseStrategy]]:
        return cls._registry.get(slug)
