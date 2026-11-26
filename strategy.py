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


@dataclass(frozen=True)
class TradeAdvice:
    action: str  # "close_now", "move_sl_be", or "lock_profit"
    suggested_sl: Optional[float]
    reason: str


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


def timeframe_seconds(tf: str) -> int:
    """Return candle duration in seconds for a timeframe code."""
    return {
        "M1": 60,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
        "D1": 86400,
    }.get(tf.upper(), 3600)


def compute_htf_bias_series(
    ltf_candles: Sequence[Dict[str, Any]],
    ltf_timeframe: str,
    htf_candles: Sequence[Dict[str, Any]],
    htf_timeframe: str,
    ema_fast_period: int = 20,
    ema_slow_period: int = 50,
) -> List[int]:
    """
    Compute non-repainting higher-timeframe trend direction aligned to each LTF candle close:
      +1 = bullish (HTF EMA fast > slow and HTF close > slow)
      -1 = bearish (HTF EMA fast < slow and HTF close < slow)
       0 = neutral / mixed (blocks counter-chop entries)
    """
    if not ltf_candles or not htf_candles:
        return [0] * len(ltf_candles)
    htf_closes = [float(c["close"]) for c in htf_candles]
    htf_fast = ema_series(htf_closes, ema_fast_period)
    htf_slow = ema_series(htf_closes, ema_slow_period)
    ltf_sec = timeframe_seconds(ltf_timeframe)
    htf_sec = timeframe_seconds(htf_timeframe)

    biases: List[int] = []
    j = -1
    n_htf = len(htf_candles)
    min_htf_idx = max(ema_slow_period, 5)
    for c in ltf_candles:
        ltf_close_ts = int(c["time"]) + ltf_sec
        while j + 1 < n_htf and int(htf_candles[j + 1]["time"]) + htf_sec <= ltf_close_ts:
            j += 1
        if j < min_htf_idx:
            biases.append(0)
            continue
        c_htf = htf_closes[j]
        f_htf = htf_fast[j]
        s_htf = htf_slow[j]
        if f_htf > s_htf and c_htf > s_htf:
            biases.append(1)
        elif f_htf < s_htf and c_htf < s_htf:
            biases.append(-1)
        else:
            biases.append(0)
    return biases


def summarize_market_state(
    candles: Sequence[Dict[str, Any]],
    cfg: StrategyConfig,
    htf_candles: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return latest closed candle indicator snapshot for Telegram /status reporting."""
    if not candles:
        return {}
    cache = compute_indicators(candles, cfg)
    idx = len(candles) - 1
    c = candles[idx]
    ema_f = cache.ema_fast[idx]
    ema_s = cache.ema_slow[idx]
    htf_bias: Optional[int] = None
    if cfg.htf_timeframe and htf_candles:
        htf_bias = compute_htf_bias_series(
            [c],
            cfg.timeframe,
            htf_candles,
            cfg.htf_timeframe,
            cfg.htf_ema_fast,
            cfg.htf_ema_slow,
        )[0]
    trend = "TĂNG" if ema_f > ema_s else ("GIẢM" if ema_f < ema_s else "NGANG")
    return {
        "close": float(c["close"]),
        "ema_fast": ema_f,
        "ema_slow": ema_s,
        "rsi": cache.rsi[idx],
        "adx": cache.adx[idx],
        "atr": cache.atr[idx],
        "trend": trend,
        "in_session": is_in_trading_session(int(c["time"]), cfg),
        "htf_bias": htf_bias,
    }


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
        htf_bias: Optional[int] = None,
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
        if htf_bias is not None:
            indicators["htf_bias"] = float(htf_bias)

        # --- LONG (BUY) ---
        bull_htf_ok = htf_bias is None or htf_bias == 1
        bull_trend = bull_htf_ok and ema_f > ema_s and ema_f > ema_f_prev and close_p > ema_f
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
        bear_htf_ok = htf_bias is None or htf_bias == -1
        bear_trend = bear_htf_ok and ema_f < ema_s and ema_f < ema_f_prev and close_p < ema_f
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

    def evaluate_latest(
        self,
        candles: Sequence[Dict[str, Any]],
        htf_candles: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Optional[TradeSignal]:
        """Evaluate the most recent closed candle."""
        if len(candles) <= self.min_candles:
            return None
        cache = compute_indicators(candles, self.cfg)
        htf_bias: Optional[int] = None
        if self.cfg.htf_timeframe and htf_candles:
            htf_bias = compute_htf_bias_series(
                [candles[-1]],
                self.cfg.timeframe,
                htf_candles,
                self.cfg.htf_timeframe,
                self.cfg.htf_ema_fast,
                self.cfg.htf_ema_slow,
            )[0]
        return self.evaluate_at(candles, len(candles) - 1, cache, htf_bias=htf_bias)


def evaluate_trade_management(
    side: str,
    entry_price: float,
    current_sl: Optional[float],
    take_profit: Optional[float],
    max_favorable_price: float,
    candles: Sequence[Dict[str, Any]],
    cfg: StrategyConfig,
    be_done: bool = False,
    lock_done: bool = False,
) -> Optional[TradeAdvice]:
    """
    Observe an open trade on each closed candle and suggest:
      - close_now: structure & momentum broke against the trade
      - lock_profit: reached >=75% TP and stalling -> lock +45% TP
      - move_sl_be: reached >=50% TP and stalling -> move SL to breakeven (+$0.50 spread buffer)
    """
    min_bars = max(cfg.ema_slow + 5, 60)
    if len(candles) < min_bars:
        return None
    cache = compute_indicators(candles, cfg)
    idx = len(candles) - 1
    c = candles[idx]
    open_p = float(c["open"])
    close_p = float(c["close"])
    ema_f = cache.ema_fast[idx]
    rsi = cache.rsi[idx]
    macd_l = cache.macd_line[idx]
    macd_s = cache.macd_signal[idx]
    macd_h = cache.macd_hist[idx]
    macd_h_prev = cache.macd_hist[idx - 1]
    is_buy = side.upper() == "BUY"

    # 1. Trend structure broken -> close early
    if is_buy and close_p < ema_f and macd_l < macd_s and macd_h < 0.0 and rsi < 45.0:
        return TradeAdvice("close_now", None, f"Gãy EMA{cfg.ema_fast}, MACD âm, RSI={rsi:.1f}")
    if not is_buy and close_p > ema_f and macd_l > macd_s and macd_h > 0.0 and rsi > 55.0:
        return TradeAdvice("close_now", None, f"Vượt EMA{cfg.ema_fast}, MACD dương, RSI={rsi:.1f}")

    if take_profit is None:
        return None
    tp_dist = abs(take_profit - entry_price)
    if tp_dist <= 0:
        return None

    mfe_frac = (
        (max_favorable_price - entry_price) / tp_dist
        if is_buy
        else (entry_price - max_favorable_price) / tp_dist
    )
    stalling = (
        (macd_h < macd_h_prev and close_p < open_p)
        if is_buy
        else (macd_h > macd_h_prev and close_p > open_p)
    )
    if not stalling:
        return None

    # 2. >= 75% TP reached and stalling -> lock +45% TP
    if not lock_done and mfe_frac >= 0.75:
        new_sl = round(entry_price + 0.45 * tp_dist, 2) if is_buy else round(entry_price - 0.45 * tp_dist, 2)
        safer = current_sl is None or (new_sl > current_sl if is_buy else new_sl < current_sl)
        still_valid = close_p > new_sl if is_buy else close_p < new_sl
        if safer and still_valid:
            return TradeAdvice("lock_profit", new_sl, f"Đã đạt {int(mfe_frac * 100)}% TP và nến có dấu hiệu hụt hơi")

    # 3. >= 50% TP reached and stalling -> move SL to breakeven + $0.50 spread buffer
    if not be_done and mfe_frac >= 0.50:
        new_sl = round(entry_price + 0.50, 2) if is_buy else round(entry_price - 0.50, 2)
        safer = current_sl is None or (new_sl > current_sl if is_buy else new_sl < current_sl)
        still_valid = close_p > new_sl if is_buy else close_p < new_sl
        if safer and still_valid:
            return TradeAdvice("move_sl_be", new_sl, f"Đã đạt {int(mfe_frac * 100)}% TP và động lượng đang chững lại")

    return None


def evaluate_early_exit(
    side: str,
    candles: Sequence[Dict[str, Any]],
    cfg: StrategyConfig,
) -> Optional[str]:
    """Backwards-compatible helper returning exit reason when structure breaks."""
    adv = evaluate_trade_management(side, 0.0, None, None, 0.0, candles, cfg)
    if adv and adv.action == "close_now":
        return adv.reason
    return None
