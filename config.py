"""Configuration loader for XAUUSD Trading Bot."""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Dict, Tuple


def load_dotenv(env_path: str = ".env") -> None:
    """Load simple KEY=VALUE pairs from a .env file into os.environ if unset."""
    path = Path(env_path)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val and val.strip() else default


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    return float(val) if val and val.strip() else default


@dataclass(frozen=True)
class StrategyConfig:
    timeframe: str = "H1"
    ema_fast: int = 50
    ema_slow: int = 200
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    adx_period: int = 14
    adx_min: float = 0.0
    pullback_lookback: int = 5
    pullback_atr_tolerance: float = 0.45
    rsi_buy_min: float = 40.0
    rsi_buy_max: float = 68.0
    rsi_sell_min: float = 32.0
    rsi_sell_max: float = 60.0
    sl_atr_mult: float = 2.5
    rr_ratio: float = 2.5
    min_sl_usd: float = 0.0
    max_sl_usd: float = 0.0
    max_tp_usd: float = 0.0
    session_start_utc: int = 7   # London open
    session_end_utc: int = 17    # London/NY overlap end
    use_session_filter: bool = True
    breakeven_atr_trigger: float = 0.0  # Move SL to entry after +N ATR (0 disables)
    htf_timeframe: str = ""             # Optional higher-TF trend filter (e.g. "M15", "H1")
    htf_ema_fast: int = 20
    htf_ema_slow: int = 50


@dataclass(frozen=True)
class StrategyProfile:
    name: str
    strategy: StrategyConfig


STRATEGY_PRESETS: Dict[str, StrategyConfig] = {
    "h1_swing": StrategyConfig(
        timeframe="H1",
        ema_fast=50,
        ema_slow=200,
        adx_min=0.0,
        sl_atr_mult=2.5,
        rr_ratio=2.5,
        use_session_filter=True,
    ),
    "m15_active": StrategyConfig(
        timeframe="M15",
        ema_fast=20,
        ema_slow=50,
        adx_min=18.0,
        sl_atr_mult=2.5,
        rr_ratio=2.0,
        use_session_filter=False,
    ),
    "m5_scalp": StrategyConfig(
        timeframe="M5",
        ema_fast=50,
        ema_slow=200,
        adx_min=15.0,
        sl_atr_mult=1.2,
        rr_ratio=1.7,
        min_sl_usd=0.0,
        max_sl_usd=0.0,
        max_tp_usd=0.0,
        use_session_filter=True,
        htf_timeframe="M15",
        htf_ema_fast=20,
        htf_ema_slow=50,
    ),
    "m15_intraday": StrategyConfig(
        timeframe="M15",
        ema_fast=20,
        ema_slow=50,
        adx_min=18.0,
        sl_atr_mult=1.0,
        rr_ratio=2.0,
        min_sl_usd=0.0,
        max_sl_usd=0.0,
        max_tp_usd=0.0,
        use_session_filter=True,
    ),
}


@dataclass(frozen=True)
class RiskConfig:
    risk_percent: float = 1.0
    max_open_positions: int = 2
    max_daily_trades: int = 0
    max_daily_loss_percent: float = 3.0
    max_spread_usd: float = 0.60
    news_buffer_minutes: int = 15
    cooldown_candles: int = 2
    allow_opposite_positions: bool = False
    fixed_size: float = 0.0
    max_size: float = 0.0
    max_positions_per_mode: int = 1
    exit_alert_min_minutes: int = 0
    news_warn_before_minutes: int = 30
    loss_streak_reduce_after: int = 2
    loss_streak_risk_factor: float = 0.5
    friday_close_warn_utc_hour: int = 20


@dataclass(frozen=True)
class BrokerConfig:
    provider: str = "paper"  # "oanda", "capital", or "paper"
    environment: str = "demo"  # "demo" or "live"
    # OANDA
    oanda_api_token: str = ""
    oanda_account_id: str = ""
    oanda_instrument: str = "XAU_USD"
    # Capital.com
    capital_api_key: str = ""
    capital_identifier: str = ""
    capital_password: str = ""
    capital_epic: str = "GOLD"
    # Paper
    paper_initial_balance: float = 10000.0
    paper_state_file: str = "data/paper_state.json"
    paper_contract_size: float = 1.0


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    status_interval_minutes: int = 60
    heartbeat_hours: Tuple[int, ...] = (9, 12, 18, 21)
    heartbeat_tz_offset_hours: int = 7

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass(frozen=True)
class BotConfig:
    strategy: StrategyConfig
    risk: RiskConfig
    broker: BrokerConfig
    telegram: TelegramConfig
    poll_interval_seconds: int = 15
    dry_run: bool = False
    log_level: str = "INFO"
    strategy_profiles: Tuple[StrategyProfile, ...] = ()
    manual_confirm_tracking: bool = False
    bot_state_file: str = ""

    @property
    def active_profiles(self) -> Tuple[StrategyProfile, ...]:
        if self.strategy_profiles:
            return self.strategy_profiles
        return (StrategyProfile(name=self.strategy.timeframe, strategy=self.strategy),)


def load_config(env_path: str = ".env") -> BotConfig:
    load_dotenv(env_path)
    strategy = StrategyConfig(
        timeframe=os.environ.get("TIMEFRAME", "H1").upper(),
        ema_fast=_env_int("EMA_FAST", 50),
        ema_slow=_env_int("EMA_SLOW", 200),
        rsi_period=_env_int("RSI_PERIOD", 14),
        atr_period=_env_int("ATR_PERIOD", 14),
        adx_period=_env_int("ADX_PERIOD", 14),
        adx_min=_env_float("ADX_MIN", 0.0),
        pullback_lookback=_env_int("PULLBACK_LOOKBACK", 5),
        pullback_atr_tolerance=_env_float("PULLBACK_ATR_TOLERANCE", 0.45),
        sl_atr_mult=_env_float("SL_ATR_MULT", 2.5),
        rr_ratio=_env_float("RR_RATIO", 2.5),
        min_sl_usd=_env_float("MIN_SL_USD", 0.0),
        max_sl_usd=_env_float("MAX_SL_USD", 0.0),
        max_tp_usd=_env_float("MAX_TP_USD", 0.0),
        session_start_utc=_env_int("SESSION_START_UTC", 7),
        session_end_utc=_env_int("SESSION_END_UTC", 17),
        use_session_filter=_env_bool("USE_SESSION_FILTER", True),
        breakeven_atr_trigger=_env_float("BREAKEVEN_ATR_TRIGGER", 0.0),
        htf_timeframe=os.environ.get("HTF_TIMEFRAME", "").strip().upper(),
        htf_ema_fast=_env_int("HTF_EMA_FAST", 20),
        htf_ema_slow=_env_int("HTF_EMA_SLOW", 50),
    )
    modes_raw = os.environ.get("STRATEGY_MODES", "").strip()
    profiles: Tuple[StrategyProfile, ...] = ()
    if modes_raw:
        parsed = []
        for item in modes_raw.split(","):
            key = item.strip().lower()
            if not key:
                continue
            if key not in STRATEGY_PRESETS:
                raise ValueError(f"Unknown STRATEGY_MODES preset: {key}. Valid: {list(STRATEGY_PRESETS)}")
            parsed.append(StrategyProfile(name=key.upper(), strategy=STRATEGY_PRESETS[key]))
        profiles = tuple(parsed)
    risk = RiskConfig(
        risk_percent=_env_float("RISK_PERCENT", 1.0),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", 2),
        max_daily_trades=_env_int("MAX_DAILY_TRADES", 0),
        max_daily_loss_percent=_env_float("MAX_DAILY_LOSS_PERCENT", 3.0),
        max_spread_usd=_env_float("MAX_SPREAD_USD", 0.60),
        news_buffer_minutes=_env_int("NEWS_BUFFER_MINUTES", 15),
        cooldown_candles=_env_int("COOLDOWN_CANDLES", 2),
        allow_opposite_positions=_env_bool("ALLOW_OPPOSITE_POSITIONS", False),
        fixed_size=_env_float("FIXED_SIZE", 0.0),
        max_size=_env_float("MAX_SIZE", 0.0),
        max_positions_per_mode=_env_int("MAX_POSITIONS_PER_MODE", 1),
        exit_alert_min_minutes=_env_int("EXIT_ALERT_MIN_MINUTES", 0),
        news_warn_before_minutes=_env_int("NEWS_WARN_BEFORE_MINUTES", 30),
        loss_streak_reduce_after=_env_int("LOSS_STREAK_REDUCE_AFTER", 2),
        loss_streak_risk_factor=_env_float("LOSS_STREAK_RISK_FACTOR", 0.5),
        friday_close_warn_utc_hour=_env_int("FRIDAY_CLOSE_WARN_UTC_HOUR", 20),
    )
    broker = BrokerConfig(
        provider=os.environ.get("BROKER_PROVIDER", "paper").strip().lower(),
        environment=os.environ.get("BROKER_ENV", "demo").strip().lower(),
        oanda_api_token=os.environ.get("OANDA_API_TOKEN", "").strip(),
        oanda_account_id=os.environ.get("OANDA_ACCOUNT_ID", "").strip(),
        oanda_instrument=os.environ.get("OANDA_INSTRUMENT", "XAU_USD").strip(),
        capital_api_key=os.environ.get("CAPITAL_API_KEY", "").strip(),
        capital_identifier=os.environ.get("CAPITAL_IDENTIFIER", "").strip(),
        capital_password=os.environ.get("CAPITAL_PASSWORD", "").strip(),
        capital_epic=os.environ.get("CAPITAL_EPIC", "GOLD").strip(),
        paper_initial_balance=_env_float("PAPER_INITIAL_BALANCE", 10000.0),
        paper_state_file=os.environ.get("PAPER_STATE_FILE", "data/paper_state.json").strip(),
        paper_contract_size=_env_float("PAPER_CONTRACT_SIZE", 1.0),
    )
    hb_raw = os.environ.get("HEARTBEAT_HOURS", "9,12,18,21").strip()
    hb_hours: Tuple[int, ...] = tuple(
        int(h.strip()) for h in hb_raw.split(",") if h.strip() and 0 <= int(h.strip()) <= 23
    )
    telegram = TelegramConfig(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        status_interval_minutes=_env_int("STATUS_INTERVAL_MINUTES", 60),
        heartbeat_hours=hb_hours,
        heartbeat_tz_offset_hours=_env_int("HEARTBEAT_TZ_OFFSET_HOURS", 7),
    )
    return BotConfig(
        strategy=strategy,
        risk=risk,
        broker=broker,
        telegram=telegram,
        poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 15),
        dry_run=_env_bool("DRY_RUN", False),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        strategy_profiles=profiles,
        manual_confirm_tracking=_env_bool("MANUAL_CONFIRM_TRACKING", True),
        bot_state_file=os.environ.get("BOT_STATE_FILE", "").strip(),
    )
