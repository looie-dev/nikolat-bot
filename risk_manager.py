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
        loss_streak_reduce_after: int = 2,
        loss_streak_risk_factor: float = 0.5,
    ):
        self.risk_percent = risk_percent
        self.max_open_positions = max_open_positions
        self.max_daily_trades = max_daily_trades
        self.max_daily_loss_percent = max_daily_loss_percent
        self.max_spread_usd = max_spread_usd
        self.fixed_size = fixed_size
        self.max_size = max_size
        self.loss_streak_reduce_after = loss_streak_reduce_after
        self.loss_streak_risk_factor = loss_streak_risk_factor

        self._current_day: Optional[str] = None
        self._daily_trades: int = 0
        self._daily_realized_pnl: float = 0.0
        self._day_start_balance: Optional[float] = None
        self._consecutive_losses: int = 0

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def effective_risk_percent(self) -> float:
        if (
            self.loss_streak_reduce_after > 0
            and 0.0 < self.loss_streak_risk_factor < 1.0
            and self._consecutive_losses >= self.loss_streak_reduce_after
        ):
            return self.risk_percent * self.loss_streak_risk_factor
        return self.risk_percent

    def export_state(self) -> dict:
        return {
            "current_day": self._current_day,
            "daily_trades": self._daily_trades,
            "daily_realized_pnl": self._daily_realized_pnl,
            "day_start_balance": self._day_start_balance,
            "consecutive_losses": self._consecutive_losses,
        }

    def restore_state(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        self._current_day = data.get("current_day")
        self._daily_trades = int(data.get("daily_trades", 0))
        self._daily_realized_pnl = float(data.get("daily_realized_pnl", 0.0))
        dsb = data.get("day_start_balance")
        self._day_start_balance = float(dsb) if dsb is not None else None
        self._consecutive_losses = int(data.get("consecutive_losses", 0))

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
        if pnl_usd < 0:
            self._consecutive_losses += 1
        elif pnl_usd > 0:
            self._consecutive_losses = 0

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

        if self.max_open_positions > 0 and open_positions_count >= self.max_open_positions:
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

        base_target_risk_usd = balance * (self.risk_percent / 100.0)
        target_risk_usd = balance * (self.effective_risk_percent / 100.0)
        raw_size = target_risk_usd / (sl_distance * spec.contract_size)

        # Round down to valid step size so actual risk never exceeds target_risk_usd
        steps = math.floor(raw_size / spec.size_step)
        size = round(steps * spec.size_step, spec.precision)
        if spec.precision == 0:
            size = float(int(size))

        if size < spec.min_size:
            min_risk = spec.min_size * sl_distance * spec.contract_size
            # Allow min_size if base risk limit tolerates it (loss-streak scaling shouldn't lock out min_size=0.01)
            if min_risk <= base_target_risk_usd * 2.0:
                size = spec.min_size
            else:
                return RiskDecision(
                    False,
                    0.0,
                    0.0,
                    f"Balance quá nhỏ cho size tối thiểu {spec.min_size} (rủi ro ${min_risk:.2f} > 2x ${base_target_risk_usd:.2f})",
                )

        if self.max_size > 0 and size > self.max_size:
            size = round(math.floor(self.max_size / spec.size_step) * spec.size_step, spec.precision)
            if spec.precision == 0:
                size = float(int(size))

        actual_risk = round(size * sl_distance * spec.contract_size, 2)
        return RiskDecision(True, size, actual_risk, "OK")
