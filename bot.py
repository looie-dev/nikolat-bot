"""Main runner for the XAUUSD Multi-Confluence Trading Bot."""

import argparse
from datetime import datetime, timezone
import logging
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional

from brokers import BrokerClient, Position, create_broker
from config import BotConfig, StrategyProfile, load_config
from health_server import start_health_server
from news_filter import NewsFilter
from notifier import TelegramNotifier
from risk_manager import RiskManager
from strategy import TradeSignal, TrendPullbackStrategy

logger = logging.getLogger("xauusd_bot")


class TradingBot:
    """Coordinates market polling, multi-mode strategy evaluation, risk checks, and Telegram alerts."""

    def __init__(
        self,
        cfg: BotConfig,
        broker: Optional[BrokerClient] = None,
        notifier: Optional[TelegramNotifier] = None,
        news_filter: Optional[NewsFilter] = None,
    ):
        self.cfg = cfg
        self.broker = broker or create_broker(cfg.broker)
        self.notifier = notifier or TelegramNotifier(cfg.telegram)
        self.news_filter = news_filter or NewsFilter(buffer_minutes=cfg.risk.news_buffer_minutes)
        self.profiles = cfg.active_profiles
        self._strategies: Dict[str, TrendPullbackStrategy] = {
            p.name: TrendPullbackStrategy(p.strategy) for p in self.profiles
        }
        self.risk_manager = RiskManager(
            risk_percent=cfg.risk.risk_percent,
            max_open_positions=cfg.risk.max_open_positions,
            max_daily_trades=cfg.risk.max_daily_trades,
            max_daily_loss_percent=cfg.risk.max_daily_loss_percent,
            max_spread_usd=cfg.risk.max_spread_usd,
            fixed_size=cfg.risk.fixed_size,
            max_size=cfg.risk.max_size,
        )
        self._tracked_positions: Dict[str, Position] = {}
        self._position_modes: Dict[str, str] = {}
        self._last_evaluated_candle_time: Dict[str, int] = {}
        self._candles_since_close: Dict[str, int] = {
            p.name: cfg.risk.cooldown_candles for p in self.profiles
        }
        self._last_atr: Dict[str, float] = {}
        self._last_status_time: float = 0.0
        self._running: bool = True

    def initialize(self) -> None:
        """Load initial broker state and send startup Telegram notification."""
        account = self.broker.get_account_summary()
        for pos in self.broker.get_open_positions():
            self._tracked_positions[pos.position_id] = pos
        self.news_filter.refresh(force=True)
        self._last_status_time = time.time()
        mode_summary = " + ".join(f"{p.name}({p.strategy.timeframe})" for p in self.profiles)
        logger.info(
            "Bot initialized | provider=%s env=%s symbol=%s modes=[%s] balance=%.2f %s open=%d",
            self.cfg.broker.provider,
            self.cfg.broker.environment,
            self.broker.symbol,
            mode_summary,
            account.balance,
            account.currency,
            len(self._tracked_positions),
        )
        self.notifier.notify_startup(
            provider=self.cfg.broker.provider,
            environment=self.cfg.broker.environment,
            symbol=self.broker.symbol,
            balance=account.balance,
            currency=account.currency,
            timeframe=mode_summary,
            risk_percent=self.cfg.risk.risk_percent,
            dry_run=self.cfg.dry_run,
        )

    def _sync_positions(self, current_price: float) -> None:
        current_positions = self.broker.get_open_positions()
        current_map = {p.position_id: p for p in current_positions}

        closed_ids = [pid for pid in self._tracked_positions if pid not in current_map]
        if closed_ids:
            account = self.broker.get_account_summary()
            for pid in closed_ids:
                prev = self._tracked_positions[pid]
                mode_label = self._position_modes.pop(pid, "")
                if mode_label:
                    self._candles_since_close[mode_label] = 0
                info = self.broker.get_closed_trade_info(pid)
                close_price = info.close_price if info else current_price
                pnl = info.realized_pnl if info else 0.0
                reason = info.reason if info else "Đã đóng"
                self.risk_manager.record_trade_closed(pnl, account.balance)
                logger.info(
                    "Position %s [%s] closed | pnl=%.2f | balance=%.2f",
                    pid,
                    mode_label or "UNKNOWN",
                    pnl,
                    account.balance,
                )
                self.notifier.notify_position_closed(
                    side=prev.side,
                    entry_price=prev.entry_price,
                    close_price=close_price,
                    pnl_usd=pnl,
                    balance=account.balance,
                    position_id=pid,
                    reason=reason,
                    mode_label=mode_label,
                )

        # Optional breakeven move per position's mode config
        profile_map = {p.name: p for p in self.profiles}
        for pid, pos in current_map.items():
            mode_name = self._position_modes.get(pid)
            prof = profile_map.get(mode_name) if mode_name else self.profiles[0]
            be_mult = prof.strategy.breakeven_atr_trigger if prof else 0.0
            atr_val = self._last_atr.get(prof.name) if prof else None
            if be_mult > 0 and atr_val:
                trigger_dist = be_mult * atr_val
                if pos.side == "BUY" and (pos.stop_loss is None or pos.stop_loss < pos.entry_price):
                    if current_price >= pos.entry_price + trigger_dist:
                        if self.broker.update_stop_loss(pid, pos.entry_price):
                            logger.info("Moved BUY %s SL to breakeven %.2f", pid, pos.entry_price)
                elif pos.side == "SELL" and (pos.stop_loss is None or pos.stop_loss > pos.entry_price):
                    if current_price <= pos.entry_price - trigger_dist:
                        if self.broker.update_stop_loss(pid, pos.entry_price):
                            logger.info("Moved SELL %s SL to breakeven %.2f", pid, pos.entry_price)

        self._tracked_positions = current_map

    def _adjust_levels_to_quote(self, sig: TradeSignal, bid: float, ask: float) -> TradeSignal:
        """Anchor SL/TP distances to actual executable bid/ask price."""
        sl_dist = abs(sig.entry_price - sig.stop_loss)
        tp_dist = abs(sig.take_profit - sig.entry_price)
        if sig.side == "BUY":
            entry = round(ask, 2)
            sl = round(entry - sl_dist, 2)
            tp = round(entry + tp_dist, 2)
        else:
            entry = round(bid, 2)
            sl = round(entry + sl_dist, 2)
            tp = round(entry - tp_dist, 2)
        return TradeSignal(
            side=sig.side,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            atr=sig.atr,
            rr_ratio=sig.rr_ratio,
            candle_time=sig.candle_time,
            reason=sig.reason,
            indicators=sig.indicators,
        )

    def _evaluate_profile(
        self,
        profile: StrategyProfile,
        candles: List[Dict[str, Any]],
        quote_bid: float,
        quote_ask: float,
        quote_spread: float,
    ) -> Optional[TradeSignal]:
        if not candles:
            return None

        latest_candle_time = int(candles[-1]["time"])
        if self._last_evaluated_candle_time.get(profile.name) == latest_candle_time:
            return None

        self._last_evaluated_candle_time[profile.name] = latest_candle_time
        self._candles_since_close[profile.name] = self._candles_since_close.get(profile.name, 0) + 1

        # Avoid opening a second concurrent trade from the same mode
        if profile.name in self._position_modes.values():
            return None

        if self._candles_since_close[profile.name] <= self.cfg.risk.cooldown_candles:
            logger.debug(
                "[%s] Cooldown active (%d/%d candles)",
                profile.name,
                self._candles_since_close[profile.name],
                self.cfg.risk.cooldown_candles,
            )
            return None

        sig = self._strategies[profile.name].evaluate_latest(candles)
        if sig is None:
            return None

        self._last_atr[profile.name] = sig.atr
        logger.info("[%s] Signal detected: %s @ %.2f (%s)", profile.name, sig.side, sig.entry_price, sig.reason)

        blocked, news_ev = self.news_filter.check_blocked()
        if blocked and news_ev is not None:
            reason = f"Tránh tin USD High-Impact: {news_ev.title}"
            logger.info("[%s] Signal skipped: %s", profile.name, reason)
            self.notifier.notify_signal_skipped(sig, reason, mode_label=profile.name)
            return None

        live_sig = self._adjust_levels_to_quote(sig, quote_bid, quote_ask)
        account = self.broker.get_account_summary()
        open_positions = self.broker.get_open_positions()

        if not self.cfg.risk.allow_opposite_positions and any(
            p.side != live_sig.side for p in open_positions
        ):
            reason = "Đang có lệnh ngược chiều đang mở — bỏ qua để tránh netting đóng lệnh cũ"
            logger.info("[%s] Order blocked: %s", profile.name, reason)
            self.notifier.notify_signal_skipped(live_sig, reason, mode_label=profile.name)
            return None

        decision = self.risk_manager.evaluate_order(
            balance=account.balance,
            entry_price=live_sig.entry_price,
            stop_loss=live_sig.stop_loss,
            spread_usd=quote_spread,
            open_positions_count=len(open_positions),
            spec=self.broker.instrument_spec,
        )
        if not decision.allowed:
            logger.info("[%s] Order blocked by RiskManager: %s", profile.name, decision.reason)
            self.notifier.notify_signal_skipped(live_sig, decision.reason, mode_label=profile.name)
            return None

        if self.cfg.dry_run:
            logger.info("[%s] [DRY-RUN] Would place %s size=%.2f", profile.name, live_sig.side, decision.size)
            self.notifier.notify_order_opened(
                signal=live_sig,
                size=decision.size,
                risk_usd=decision.risk_usd,
                spread_usd=quote_spread,
                order_id="DRY-RUN",
                balance=account.balance,
                dry_run=True,
                mode_label=profile.name,
            )
            return live_sig

        result = self.broker.place_market_order(
            side=live_sig.side,
            size=decision.size,
            stop_loss=live_sig.stop_loss,
            take_profit=live_sig.take_profit,
        )
        if result.success:
            self.risk_manager.record_trade_opened(account.balance)
            self._position_modes[result.order_id] = profile.name
            self._tracked_positions[result.order_id] = Position(
                position_id=result.order_id,
                side=live_sig.side,
                size=result.size,
                entry_price=result.fill_price or live_sig.entry_price,
                stop_loss=live_sig.stop_loss,
                take_profit=live_sig.take_profit,
                unrealized_pnl=0.0,
            )
            filled_sig = TradeSignal(
                side=live_sig.side,
                entry_price=round(result.fill_price or live_sig.entry_price, 2),
                stop_loss=live_sig.stop_loss,
                take_profit=live_sig.take_profit,
                atr=live_sig.atr,
                rr_ratio=live_sig.rr_ratio,
                candle_time=live_sig.candle_time,
                reason=live_sig.reason,
                indicators=live_sig.indicators,
            )
            logger.info(
                "[%s] Order filled: id=%s price=%.2f size=%.2f",
                profile.name,
                result.order_id,
                filled_sig.entry_price,
                result.size,
            )
            self.notifier.notify_order_opened(
                signal=filled_sig,
                size=result.size,
                risk_usd=decision.risk_usd,
                spread_usd=quote_spread,
                order_id=result.order_id,
                balance=account.balance,
                dry_run=False,
                mode_label=profile.name,
            )
            return filled_sig

        logger.error("[%s] Order placement failed: %s", profile.name, result.message)
        self.notifier.send_message(f"⚠️ <b>LỖI ĐẶT LỆNH [{profile.name}]:</b> {result.message}")
        return None

    def run_once(self) -> Optional[TradeSignal]:
        """Run one polling cycle across all configured strategy modes."""
        candles_by_tf: Dict[str, List[Dict[str, Any]]] = {}
        for prof in self.profiles:
            tf = prof.strategy.timeframe
            if tf not in candles_by_tf:
                candles_by_tf[tf] = self.broker.get_candles(tf, count=300)

        quote = self.broker.get_quote()
        self._sync_positions(quote.mid)

        triggered: Optional[TradeSignal] = None
        for prof in self.profiles:
            tf = prof.strategy.timeframe
            res = self._evaluate_profile(
                prof,
                candles_by_tf.get(tf, []),
                quote.bid,
                quote.ask,
                quote.spread,
            )
            if res is not None:
                triggered = res

        if triggered is None:
            self._maybe_send_heartbeat(quote.mid, quote.spread)
        return triggered

    def _maybe_send_heartbeat(self, price: float, spread: float) -> None:
        interval = self.cfg.telegram.status_interval_minutes * 60
        if interval <= 0:
            return
        now = time.time()
        if now - self._last_status_time < interval:
            return
        self._last_status_time = now
        account = self.broker.get_account_summary()
        upcoming = self.news_filter.upcoming_events(hours=12)
        next_news = None
        if upcoming:
            ev = upcoming[0]
            dt_str = datetime.fromtimestamp(ev.timestamp_sec, tz=timezone.utc).strftime("%H:%M UTC")
            next_news = f"{ev.title} ({dt_str})"
        self.notifier.notify_status(
            provider=self.cfg.broker.provider,
            balance=account.balance,
            open_positions=len(self._tracked_positions),
            last_price=price,
            spread_usd=spread,
            extra={"next_news": next_news} if next_news else None,
        )

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "symbol": self.broker.symbol,
            "modes": [p.name for p in self.profiles],
            "open_positions": len(self._tracked_positions),
        }

    def run_forever(self) -> None:
        while self._running:
            try:
                self.initialize()
                break
            except Exception as exc:
                logger.exception("Initialization failed, retrying in 15s: %s", exc)
                time.sleep(15)
        consecutive_errors = 0
        while self._running:
            try:
                self.run_once()
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                logger.exception("Error in bot loop (%d): %s", consecutive_errors, exc)
                if consecutive_errors == 3:
                    self.notifier.send_message(f"⚠️ <b>XAUUSD Bot lỗi liên tiếp 3 lần:</b> {exc}")
            time.sleep(self.cfg.poll_interval_seconds)

    def stop(self) -> None:
        self._running = False


def _resolve_health_port() -> int:
    raw = os.environ.get("HEALTH_PORT") or os.environ.get("PORT") or ""
    if raw.strip():
        return int(raw.strip())
    # Hugging Face Spaces sets SPACE_ID and routes traffic to port 7860 by default
    if os.environ.get("SPACE_ID"):
        return 7860
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="XAUUSD Automated Demo Trading Bot")
    parser.add_argument("--env", default=".env", help="Path to .env config file")
    parser.add_argument("--once", action="store_true", help="Run a single check cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Send Telegram alerts without placing orders")
    args = parser.parse_args()

    cfg = load_config(args.env)
    if args.dry_run:
        cfg = BotConfig(
            strategy=cfg.strategy,
            risk=cfg.risk,
            broker=cfg.broker,
            telegram=cfg.telegram,
            poll_interval_seconds=cfg.poll_interval_seconds,
            dry_run=True,
            log_level=cfg.log_level,
            strategy_profiles=cfg.strategy_profiles,
        )

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    bot = TradingBot(cfg)
    health_srv = None
    if not args.once:
        health_port = _resolve_health_port()
        if health_port > 0:
            health_srv = start_health_server(health_port, bot.status_snapshot)

    def _handle_sig(_signum, _frame):
        logger.info("Shutting down bot...")
        bot.stop()
        if health_srv is not None:
            health_srv.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    if args.once:
        bot.initialize()
        bot.run_once()
    else:
        bot.run_forever()


if __name__ == "__main__":
    main()
