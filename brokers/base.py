"""Abstract broker interface and shared trading models."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from risk_manager import InstrumentSpec


@dataclass(frozen=True)
class AccountSummary:
    balance: float
    equity: float
    currency: str
    open_positions_count: int


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    mid: float
    spread: float
    timestamp_sec: int


@dataclass(frozen=True)
class Position:
    position_id: str
    side: str  # "BUY" or "SELL"
    size: float
    entry_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    unrealized_pnl: float


@dataclass(frozen=True)
class OrderResult:
    success: bool
    order_id: str
    fill_price: float
    size: float
    message: str


@dataclass(frozen=True)
class ClosedTradeInfo:
    position_id: str
    close_price: float
    realized_pnl: float
    reason: str


class BrokerClient(ABC):
    """Unified broker contract for market data, account info, and order execution."""

    @property
    @abstractmethod
    def symbol(self) -> str:
        pass

    @property
    @abstractmethod
    def instrument_spec(self) -> InstrumentSpec:
        pass

    @abstractmethod
    def get_account_summary(self) -> AccountSummary:
        pass

    @abstractmethod
    def get_candles(self, timeframe: str, count: int = 300) -> List[Dict[str, Any]]:
        """Return closed OHLCV candles sorted oldest -> newest."""
        pass

    @abstractmethod
    def get_quote(self) -> Quote:
        pass

    @abstractmethod
    def get_open_positions(self) -> List[Position]:
        pass

    @abstractmethod
    def place_market_order(
        self,
        side: str,
        size: float,
        stop_loss: float,
        take_profit: float,
    ) -> OrderResult:
        pass

    @abstractmethod
    def update_stop_loss(self, position_id: str, new_stop_loss: float) -> bool:
        pass

    @abstractmethod
    def get_closed_trade_info(self, position_id: str) -> Optional[ClosedTradeInfo]:
        pass
