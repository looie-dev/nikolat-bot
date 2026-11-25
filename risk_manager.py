"""Risk management and position sizing for XAUUSD."""

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Optional


@dataclass(frozen=True)
class InstrumentSpec:
    """Sizing constraints for a broker's XAUUSD instrument."""

    contract_size: float = 1.0  # USD PnL per 1.0 price move per 1.0 size unit (1.0 for 1 oz)
    min_size: float = 1.0       # Minimum order size (1 unit on OANDA, 0.1 on Capital.com)
    size_step: float = 1.0      # Increment step
    precision: int = 0          # Decimal places for order size


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    size: float
    risk_usd: float
    reason: str


class RiskManager:
    """Validates risk limits and calculates position size from ATR stop distance."""

    def __init__(
        self,
        risk_percent: float,
        max_open_positions: int,
        max_daily_trades: int,
        max_daily_loss_percent: float,
        max_spread_usd: float,
        fixed_size: float = 0.0,
        max_size: float = 0.0,
    ):
        self.risk_percent = risk_percent
        self.max_open_positions = max_open_positions
        self.max_daily_trades = max_daily_trades
        self.max_daily_loss_percent = max_daily_loss_percent
        self.max_spread_usd = max_spread_usd
        self.fixed_size = fixed_size
        self.max_size = max_size

        self._current_day: Optional[str] = None
        self._daily_trades: int = 0
        self._daily_realized_pnl: float = 0.0
        self._day_start_balance: Optional[float] = None

    def _roll_day_if_needed(self, balance: float, now_utc: Optional[datetime] = None) -> None:
        now = now_utc or datetime.now(timezone.utc)
        day_str = now.strftime("%Y-%m-%d")
        if self._current_day != day_str:
            self._current_day = day_str
            self._daily_trades = 0
            self._daily_realized_pnl = 0.0
            self._day_start_balance = balance

    def record_trade_opened(self, balance: float, now_utc: Optional[datetime] = None) -> None:
        self._roll_day_if_needed(balance, now_utc)
        self._daily_trades += 1

    def record_trade_closed(
        self, pnl_usd: float, balance: float, now_utc: Optional[datetime] = None
    ) -> None:
        self._roll_day_if_needed(balance, now_utc)
        self._daily_realized_pnl += pnl_usd

    def evaluate_order(
        self,
        balance: float,
        entry_price: float,
        stop_loss: float,
        spread_usd: float,
        open_positions_count: int,
        spec: InstrumentSpec,
        now_utc: Optional[datetime] = None,
    ) -> RiskDecision:
        self._roll_day_if_needed(balance, now_utc)

        if balance <= 0:
            return RiskDecision(False, 0.0, 0.0, "Số dư tài khoản <= 0")

        if open_positions_count >= self.max_open_positions:
            return RiskDecision(
                False,
                0.0,
                0.0,
                f"Đã đạt giới hạn vị thế mở ({open_positions_count}/{self.max_open_positions})",
            )

        if spread_usd > self.max_spread_usd:
            return RiskDecision(
                False,
                0.0,
                0.0,
                f"Spread quá cao: ${spread_usd:.2f} > giới hạn ${self.max_spread_usd:.2f}",
            )

        if self.max_daily_trades > 0 and self._daily_trades >= self.max_daily_trades:
            return RiskDecision(
                False,
                0.0,
                0.0,
                f"Đã đạt giới hạn lệnh trong ngày ({self._daily_trades}/{self.max_daily_trades})",
            )

        base_bal = self._day_start_balance or balance
        max_loss_usd = base_bal * (self.max_daily_loss_percent / 100.0)
        if self._daily_realized_pnl <= -max_loss_usd:
            return RiskDecision(
                False,
                0.0,
                0.0,
                f"Chạm giới hạn lỗ ngày ({self._daily_realized_pnl:+.2f}$ <= -{max_loss_usd:.2f}$)",
            )

        sl_distance = abs(entry_price - stop_loss)
        if sl_distance <= 0:
            return RiskDecision(False, 0.0, 0.0, "Khoảng cách SL không hợp lệ")

        if self.fixed_size > 0:
            steps = math.floor(self.fixed_size / spec.size_step)
            size = round(steps * spec.size_step, spec.precision)
            if spec.precision == 0:
                size = float(int(size))
            if size < spec.min_size:
                size = spec.min_size
            actual_risk = round(size * sl_distance * spec.contract_size, 2)
            return RiskDecision(True, size, actual_risk, "OK (Fixed Size)")

        target_risk_usd = balance * (self.risk_percent / 100.0)
        raw_size = target_risk_usd / (sl_distance * spec.contract_size)

        # Round down to valid step size so actual risk never exceeds target_risk_usd
        steps = math.floor(raw_size / spec.size_step)
        size = round(steps * spec.size_step, spec.precision)
        if spec.precision == 0:
            size = float(int(size))

        if size < spec.min_size:
            min_risk = spec.min_size * sl_distance * spec.contract_size
            # Allow min_size only if it doesn't exceed 2x target risk
            if min_risk <= target_risk_usd * 2.0:
                size = spec.min_size
            else:
                return RiskDecision(
                    False,
                    0.0,
                    0.0,
                    f"Balance quá nhỏ cho size tối thiểu {spec.min_size} (rủi ro ${min_risk:.2f} > 2x ${target_risk_usd:.2f})",
                )

        if self.max_size > 0 and size > self.max_size:
            size = round(math.floor(self.max_size / spec.size_step) * spec.size_step, spec.precision)
            if spec.precision == 0:
                size = float(int(size))

        actual_risk = round(size * sl_distance * spec.contract_size, 2)
        return RiskDecision(True, size, actual_risk, "OK")
