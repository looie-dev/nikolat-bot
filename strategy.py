"""Multi-Confluence Trend Pullback strategy for XAUUSD."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from config import StrategyConfig
from indicators import adx_series, atr_series, ema_series, macd_series, rsi_series


@dataclass(frozen=True)
class TradeSignal:
    side: str  # "BUY" or "SELL"
    entry_price: float
    stop_loss: float
    take_profit: float
    atr: float
    rr_ratio: float
    candle_time: int
    reason: str
    indicators: Dict[str, float]


@dataclass
class IndicatorCache:
    ema_fast: List[float]
    ema_slow: List[float]
    rsi: List[float]
    macd_line: List[float]
    macd_signal: List[float]
    macd_hist: List[float]
    atr: List[float]
    adx: List[float]


def compute_indicators(
    candles: Sequence[Dict[str, Any]],
    cfg: StrategyConfig,
) -> IndicatorCache:
    """Precompute all indicator series for a candle sequence."""
    closes = [float(c["close"]) for c in candles]
    macd_line, macd_signal, macd_hist = macd_series(
        closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal
    )
    return IndicatorCache(
        ema_fast=ema_series(closes, cfg.ema_fast),
        ema_slow=ema_series(closes, cfg.ema_slow),
        rsi=rsi_series(closes, cfg.rsi_period),
        macd_line=macd_line,
        macd_signal=macd_signal,
        macd_hist=macd_hist,
        atr=atr_series(candles, cfg.atr_period),
        adx=adx_series(candles, cfg.adx_period),
    )


def is_in_trading_session(timestamp_sec: int, cfg: StrategyConfig) -> bool:
    """Return True if timestamp falls within configured active UTC hours."""
    if not cfg.use_session_filter:
        return True
    hour = datetime.fromtimestamp(int(timestamp_sec), tz=timezone.utc).hour
    if cfg.session_start_utc <= cfg.session_end_utc:
        return cfg.session_start_utc <= hour < cfg.session_end_utc
    return hour >= cfg.session_start_utc or hour < cfg.session_end_utc


def _compute_sl_tp_distances(atr_val: float, cfg: StrategyConfig):
    """Compute ATR-based SL/TP distances bounded by optional USD caps."""
    sl_dist = cfg.sl_atr_mult * atr_val
    if cfg.min_sl_usd > 0.0:
        sl_dist = max(sl_dist, cfg.min_sl_usd)
    if cfg.max_sl_usd > 0.0:
        sl_dist = min(sl_dist, cfg.max_sl_usd)
    tp_dist = cfg.rr_ratio * sl_dist
    if cfg.max_tp_usd > 0.0:
        tp_dist = min(tp_dist, cfg.max_tp_usd)
    return sl_dist, tp_dist


class TrendPullbackStrategy:
    """
    Multi-confluence XAUUSD strategy combining:
      - Session liquidity filter (London & London/NY overlap)
      - Trend alignment (EMA 50 / EMA 200 + EMA slope + ADX strength)
      - Pullback toward EMA 50 followed by trend-aligned candle close
      - Momentum & confirmation (RSI + MACD histogram turn/cross)
      - Dynamic ATR-based Stop Loss and Take Profit
    """

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    @property
    def min_candles(self) -> int:
        return max(self.cfg.ema_slow + 5, 220)

    def evaluate_at(
        self,
        candles: Sequence[Dict[str, Any]],
        index: int,
        cache: IndicatorCache,
    ) -> Optional[TradeSignal]:
        """Evaluate signal at a specific closed candle index."""
        cfg = self.cfg
        if index < self.min_candles or index >= len(candles):
            return None

        candle = candles[index]
        candle_time = int(candle["time"])
        if not is_in_trading_session(candle_time, cfg):
            return None

        open_p = float(candle["open"])
        close_p = float(candle["close"])
        ema_f = cache.ema_fast[index]
        ema_s = cache.ema_slow[index]
        ema_f_prev = cache.ema_fast[index - 3]
        rsi_now = cache.rsi[index]
        rsi_prev = cache.rsi[index - 1]
        macd_l = cache.macd_line[index]
        macd_sig = cache.macd_signal[index]
        hist_now = cache.macd_hist[index]
        hist_prev = cache.macd_hist[index - 1]
        hist_prev2 = cache.macd_hist[index - 2]
        atr_val = cache.atr[index]
        adx_val = cache.adx[index]

        if atr_val <= 0.0 or adx_val < cfg.adx_min:
            return None

        lookback_start = max(0, index - cfg.pullback_lookback + 1)
        recent_candles = candles[lookback_start : index + 1]
        tol = cfg.pullback_atr_tolerance * atr_val

        indicators = {
            "ema50": round(ema_f, 3),
            "ema200": round(ema_s, 3),
            "rsi": round(rsi_now, 2),
            "macd_hist": round(hist_now, 4),
            "atr": round(atr_val, 3),
            "adx": round(adx_val, 2),
        }

        # --- LONG (BUY) ---
        bull_trend = ema_f > ema_s and ema_f > ema_f_prev and close_p > ema_f
        if bull_trend:
            pulled_back = any(
                float(c["low"]) <= cache.ema_fast[lookback_start + offset] + tol
                and float(c["close"]) > cache.ema_slow[lookback_start + offset]
                for offset, c in enumerate(recent_candles)
            )
            bull_candle = close_p > open_p
            rsi_ok = cfg.rsi_buy_min <= rsi_now <= cfg.rsi_buy_max and rsi_now > rsi_prev
            macd_cross = hist_prev <= 0.0 and hist_now > 0.0
            macd_hook = macd_l > macd_sig and hist_now > hist_prev and hist_prev <= hist_prev2
            if pulled_back and bull_candle and rsi_ok and (macd_cross or macd_hook):
                sl_dist, tp_dist = _compute_sl_tp_distances(atr_val, cfg)
                sl = round(close_p - sl_dist, 2)
                tp = round(close_p + tp_dist, 2)
                return TradeSignal(
                    side="BUY",
                    entry_price=round(close_p, 2),
                    stop_loss=sl,
                    take_profit=tp,
                    atr=round(atr_val, 3),
                    rr_ratio=cfg.rr_ratio,
                    candle_time=candle_time,
                    reason=(
                        f"Bull trend pullback EMA{cfg.ema_fast}/{cfg.ema_slow}, "
                        f"RSI={rsi_now:.1f}, ADX={adx_val:.1f}, "
                        f"MACD={'cross' if macd_cross else 'hook'}"
                    ),
                    indicators=indicators,
                )

        # --- SHORT (SELL) ---
        bear_trend = ema_f < ema_s and ema_f < ema_f_prev and close_p < ema_f
        if bear_trend:
            pulled_back = any(
                float(c["high"]) >= cache.ema_fast[lookback_start + offset] - tol
                and float(c["close"]) < cache.ema_slow[lookback_start + offset]
                for offset, c in enumerate(recent_candles)
            )
            bear_candle = close_p < open_p
            rsi_ok = cfg.rsi_sell_min <= rsi_now <= cfg.rsi_sell_max and rsi_now < rsi_prev
            macd_cross = hist_prev >= 0.0 and hist_now < 0.0
            macd_hook = macd_l < macd_sig and hist_now < hist_prev and hist_prev >= hist_prev2
            if pulled_back and bear_candle and rsi_ok and (macd_cross or macd_hook):
                sl_dist, tp_dist = _compute_sl_tp_distances(atr_val, cfg)
                sl = round(close_p + sl_dist, 2)
                tp = round(close_p - tp_dist, 2)
                return TradeSignal(
                    side="SELL",
                    entry_price=round(close_p, 2),
                    stop_loss=sl,
                    take_profit=tp,
                    atr=round(atr_val, 3),
                    rr_ratio=cfg.rr_ratio,
                    candle_time=candle_time,
                    reason=(
                        f"Bear trend pullback EMA{cfg.ema_fast}/{cfg.ema_slow}, "
                        f"RSI={rsi_now:.1f}, ADX={adx_val:.1f}, "
                        f"MACD={'cross' if macd_cross else 'hook'}"
                    ),
                    indicators=indicators,
                )

        return None

    def evaluate_latest(self, candles: Sequence[Dict[str, Any]]) -> Optional[TradeSignal]:
        """Evaluate the most recent closed candle."""
        if len(candles) <= self.min_candles:
            return None
        cache = compute_indicators(candles, self.cfg)
        return self.evaluate_at(candles, len(candles) - 1, cache)
