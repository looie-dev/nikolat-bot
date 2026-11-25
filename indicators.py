"""Pure-Python technical indicators for XAUUSD algorithmic trading."""

from typing import Dict, List, Sequence, Tuple


def ema_series(values: Sequence[float], period: int) -> List[float]:
    """Compute Exponential Moving Average (EMA) with standard 2/(period+1) smoothing."""
    if not values:
        return []
    if period <= 0:
        raise ValueError("period must be positive")
    k = 2.0 / (period + 1)
    out: List[float] = []
    prev = float(values[0])
    for i, val in enumerate(values):
        v = float(val)
        prev = v if i == 0 else v * k + prev * (1.0 - k)
        out.append(prev)
    return out


def wilder_smooth(values: Sequence[float], period: int) -> List[float]:
    """Compute Wilder's smoothing (RMA) used in RSI, ATR, and ADX."""
    if not values:
        return []
    if period <= 0:
        raise ValueError("period must be positive")
    alpha = 1.0 / period
    out: List[float] = []
    prev = float(values[0])
    for i, val in enumerate(values):
        v = float(val)
        prev = v if i == 0 else v * alpha + prev * (1.0 - alpha)
        out.append(prev)
    return out


def rsi_series(closes: Sequence[float], period: int = 14) -> List[float]:
    """Compute Relative Strength Index (RSI) using Wilder smoothing."""
    if not closes:
        return []
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(closes)):
        delta = float(closes[i]) - float(closes[i - 1])
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = wilder_smooth(gains, period)
    avg_loss = wilder_smooth(losses, period)
    out: List[float] = []
    for g, l in zip(avg_gain, avg_loss):
        if l == 0.0:
            out.append(50.0 if g == 0.0 else 100.0)
        else:
            rs = g / l
            out.append(100.0 - 100.0 / (1.0 + rs))
    return out


def macd_series(
    closes: Sequence[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> Tuple[List[float], List[float], List[float]]:
    """Compute MACD line, signal line, and histogram."""
    if not closes:
        return [], [], []
    ema_fast = ema_series(closes, fast_period)
    ema_slow = ema_series(closes, slow_period)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema_series(macd_line, signal_period)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram


def true_range_series(candles: Sequence[Dict[str, float]]) -> List[float]:
    """Compute True Range for each OHLC candle."""
    out: List[float] = []
    for i, c in enumerate(candles):
        high = float(c["high"])
        low = float(c["low"])
        if i == 0:
            out.append(high - low)
        else:
            prev_close = float(candles[i - 1]["close"])
            out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return out


def atr_series(candles: Sequence[Dict[str, float]], period: int = 14) -> List[float]:
    """Compute Average True Range (ATR)."""
    if not candles:
        return []
    return wilder_smooth(true_range_series(candles), period)


def adx_series(candles: Sequence[Dict[str, float]], period: int = 14) -> List[float]:
    """Compute Average Directional Index (ADX)."""
    if not candles:
        return []
    tr = true_range_series(candles)
    tr_smooth = wilder_smooth(tr, period)
    plus_dm = [0.0]
    minus_dm = [0.0]
    for i in range(1, len(candles)):
        up_move = float(candles[i]["high"]) - float(candles[i - 1]["high"])
        down_move = float(candles[i - 1]["low"]) - float(candles[i]["low"])
        plus_dm.append(up_move if (up_move > down_move and up_move > 0.0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0.0) else 0.0)
    plus_smooth = wilder_smooth(plus_dm, period)
    minus_smooth = wilder_smooth(minus_dm, period)
    dx: List[float] = []
    for trs, pdm, mdm in zip(tr_smooth, plus_smooth, minus_smooth):
        plus_di = (100.0 * pdm) / trs if trs else 0.0
        minus_di = (100.0 * mdm) / trs if trs else 0.0
        denom = plus_di + minus_di
        dx.append(0.0 if denom == 0.0 else (100.0 * abs(plus_di - minus_di)) / denom)
    return wilder_smooth(dx, period)
