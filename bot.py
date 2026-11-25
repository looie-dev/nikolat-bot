"""Main runner for the XAUUSD Multi-Confluence Trading Bot."""

import argparse
from datetime import datetime, timedelta, timezone
import logging
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Set

from brokers import BrokerClient, PaperBroker, Position, create_broker
from config import BotConfig, StrategyProfile, load_config
from health_server import start_health_server
from news_filter import NewsFilter
from notifier import TelegramNotifier, TelegramUserAction
from risk_manager import RiskManager
from strategy import TradeSignal, TrendPullbackStrategy, evaluate_trade_management

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
        self._position_opened_at: Dict[str, int] = {}
        self._position_opened_candle: Dict[str, int] = {}
        self._position_mfe_price: Dict[str, float] = {}
        self._position_exit_alerted: Set[str] = set()
        self._position_be_alerted: Set[str] = set()
        self._position_lock_alerted: Set[str] = set()
        self._last_evaluated_candle_time: Dict[str, int] = {}
        self._last_exit_check_candle_time: Optional[int] = None
        self._candles_since_close: Dict[str, int] = {
            p.name: cfg.risk.cooldown_candles for p in self.profiles
        }
        self._last_atr: Dict[str, float] = {}
        self._last_status_time: float = 0.0
        self._sent_heartbeat_slots: Set[str] = set()
        self._pending_confirmations: Dict[str, Dict[str, Any]] = {}
        self._msg_to_confirm: Dict[int, str] = {}
        self._msg_to_position: Dict[int, str] = {}
        self._tg_offset: int = 0
        self._confirm_seq: int = 0
        self._running: bool = True

    def initialize(self) -> None:
        """Load initial broker state and send startup Telegram notification."""
        account = self.broker.get_account_summary()
        now_sec = int(time.time())
        for pos in self.broker.get_open_positions():
            self._tracked_positions[pos.position_id] = pos
            self._position_opened_at[pos.position_id] = now_sec
            self._position_mfe_price[pos.position_id] = pos.entry_price
        self.news_filter.refresh(force=True)
        if self.cfg.manual_confirm_tracking:
            self._tg_offset = self.notifier.get_latest_update_offset()
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
                self._position_opened_at.pop(pid, None)
                self._position_opened_candle.pop(pid, None)
                self._position_mfe_price.pop(pid, None)
                self._position_exit_alerted.discard(pid)
                self._position_be_alerted.discard(pid)
                self._position_lock_alerted.discard(pid)
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

        # Respect per-mode open position cap only if configured (> 0)
        per_mode_cap = self.cfg.risk.max_positions_per_mode
        if per_mode_cap > 0:
            mode_open_count = sum(1 for m in self._position_modes.values() if m == profile.name)
            if mode_open_count >= per_mode_cap:
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

        if self.cfg.manual_confirm_tracking:
            self._confirm_seq += 1
            confirm_id = f"{profile.name}-{self._confirm_seq}"
            msg_id = self.notifier.notify_order_opened(
                signal=live_sig,
                size=decision.size,
                risk_usd=decision.risk_usd,
                spread_usd=quote_spread,
                order_id="CHỜ XÁC NHẬN",
                balance=account.balance,
                dry_run=False,
                mode_label=profile.name,
                confirm_id=confirm_id,
            )
            self._pending_confirmations[confirm_id] = {
                "signal": live_sig,
                "size": decision.size,
                "risk_usd": decision.risk_usd,
                "spread": quote_spread,
                "mode": profile.name,
                "msg_id": msg_id,
                "created_at": int(time.time()),
            }
            if msg_id:
                self._msg_to_confirm[msg_id] = confirm_id
            logger.info("[%s] Sent signal %s waiting for Telegram confirmation", profile.name, confirm_id)
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
            self._position_opened_at[result.order_id] = int(time.time())
            self._position_opened_candle[result.order_id] = int(live_sig.candle_time)
            fill_p = result.fill_price or live_sig.entry_price
            self._position_mfe_price[result.order_id] = fill_p
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

    def _resolve_pending_id(self, act: TelegramUserAction) -> Optional[str]:
        if act.target_id and act.target_id in self._pending_confirmations:
            return act.target_id
        if act.reply_to_message_id and act.reply_to_message_id in self._msg_to_confirm:
            cid = self._msg_to_confirm[act.reply_to_message_id]
            if cid in self._pending_confirmations:
                return cid
        if self._pending_confirmations:
            return list(self._pending_confirmations.keys())[-1]
        return None

    def _resolve_position_id(self, act: TelegramUserAction) -> Optional[str]:
        if act.target_id and act.target_id in self._tracked_positions:
            return act.target_id
        if act.reply_to_message_id and act.reply_to_message_id in self._msg_to_position:
            pid = self._msg_to_position[act.reply_to_message_id]
            if pid in self._tracked_positions:
                return pid
        if len(self._tracked_positions) == 1:
            return next(iter(self._tracked_positions.keys()))
        return None

    def _process_telegram_actions(self, now_sec: Optional[int] = None) -> None:
        if not self.cfg.manual_confirm_tracking:
            return
        now = int(now_sec if now_sec is not None else time.time())
        # Expire unconfirmed signals older than 20 minutes (default skipped)
        expired = [k for k, v in self._pending_confirmations.items() if now - v["created_at"] > 1200]
        for k in expired:
            item = self._pending_confirmations.pop(k)
            if item.get("msg_id"):
                self._msg_to_confirm.pop(item["msg_id"], None)
                self.notifier.clear_inline_buttons(item["msg_id"])

        actions, self._tg_offset = self.notifier.poll_updates(self._tg_offset)
        for act in actions:
            if act.action == "enter":
                cid = self._resolve_pending_id(act)
                if not cid:
                    if act.callback_query_id:
                        self.notifier.answer_callback_query(act.callback_query_id, "Tín hiệu đã hết hạn hoặc đã xử lý")
                    continue
                pending = self._pending_confirmations.pop(cid)
                sig: TradeSignal = pending["signal"]
                msg_id = pending.get("msg_id")
                if msg_id:
                    self._msg_to_confirm.pop(msg_id, None)
                    self.notifier.clear_inline_buttons(msg_id)

                entry_override = act.custom_price
                if isinstance(self.broker, PaperBroker):
                    result = self.broker.place_market_order(
                        side=sig.side,
                        size=pending["size"],
                        stop_loss=sig.stop_loss,
                        take_profit=sig.take_profit,
                        entry_override=entry_override,
                    )
                else:
                    result = self.broker.place_market_order(
                        side=sig.side,
                        size=pending["size"],
                        stop_loss=sig.stop_loss,
                        take_profit=sig.take_profit,
                    )
                if result.success:
                    account = self.broker.get_account_summary()
                    self.risk_manager.record_trade_opened(account.balance)
                    fill_p = result.fill_price or entry_override or sig.entry_price
                    mode_name = pending["mode"]
                    self._position_modes[result.order_id] = mode_name
                    self._position_opened_at[result.order_id] = now
                    self._position_opened_candle[result.order_id] = int(sig.candle_time)
                    self._position_mfe_price[result.order_id] = fill_p
                    self._tracked_positions[result.order_id] = Position(
                        position_id=result.order_id,
                        side=sig.side,
                        size=result.size,
                        entry_price=fill_p,
                        stop_loss=sig.stop_loss,
                        take_profit=sig.take_profit,
                        unrealized_pnl=0.0,
                    )
                    if act.callback_query_id:
                        self.notifier.answer_callback_query(act.callback_query_id, "Đã ghi nhận vào lệnh!")
                    conf_msg_id = self.notifier.notify_tracking_confirmed(
                        side=sig.side,
                        entry_price=fill_p,
                        position_id=result.order_id,
                        mode_label=mode_name,
                    )
                    if conf_msg_id:
                        self._msg_to_position[conf_msg_id] = result.order_id
                    if msg_id:
                        self._msg_to_position[msg_id] = result.order_id
                    logger.info("[%s] User confirmed trade entry %s @ %.2f", mode_name, result.order_id, fill_p)
                else:
                    if act.callback_query_id:
                        self.notifier.answer_callback_query(act.callback_query_id, f"Lỗi: {result.message}")
                    self.notifier.send_message(f"⚠️ Không ghi nhận được lệnh: {result.message}")

            elif act.action == "skip":
                cid = self._resolve_pending_id(act)
                if cid:
                    pending = self._pending_confirmations.pop(cid)
                    msg_id = pending.get("msg_id")
                    if msg_id:
                        self._msg_to_confirm.pop(msg_id, None)
                        self.notifier.clear_inline_buttons(msg_id)
                if act.callback_query_id:
                    self.notifier.answer_callback_query(act.callback_query_id, "Đã bỏ qua")
                self.notifier.send_message("👌 Đã bỏ qua tín hiệu — bot sẽ không theo dõi lệnh này.")

            elif act.action == "close":
                pid = self._resolve_position_id(act)
                if not pid:
                    if act.callback_query_id:
                        self.notifier.answer_callback_query(act.callback_query_id, "Không tìm thấy lệnh đang mở")
                    continue
                pos = self._tracked_positions.get(pid)
                mode_label = self._position_modes.get(pid, "")
                if isinstance(self.broker, PaperBroker):
                    closed = self.broker.close_position(pid, close_price=act.custom_price, reason="Đóng thủ công qua Telegram")
                    if closed:
                        account = self.broker.get_account_summary()
                        self.risk_manager.record_trade_closed(closed.realized_pnl, account.balance)
                        self._tracked_positions.pop(pid, None)
                        self._position_modes.pop(pid, None)
                        self._position_opened_at.pop(pid, None)
                        self._position_opened_candle.pop(pid, None)
                        self._position_mfe_price.pop(pid, None)
                        self._position_exit_alerted.discard(pid)
                        self._position_be_alerted.discard(pid)
                        self._position_lock_alerted.discard(pid)
                        if act.callback_query_id:
                            self.notifier.answer_callback_query(act.callback_query_id, "Đã đóng lệnh!")
                        if act.reply_to_message_id:
                            self.notifier.clear_inline_buttons(act.reply_to_message_id)
                        self.notifier.notify_position_closed(
                            side=pos.side if pos else "",
                            entry_price=pos.entry_price if pos else 0.0,
                            close_price=closed.close_price,
                            pnl_usd=closed.realized_pnl,
                            balance=account.balance,
                            position_id=pid,
                            reason=closed.reason,
                            mode_label=mode_label,
                        )
                else:
                    if act.callback_query_id:
                        self.notifier.answer_callback_query(act.callback_query_id, "Đã dừng theo dõi")
                    self._tracked_positions.pop(pid, None)
                    self._position_modes.pop(pid, None)

    def run_once(self) -> Optional[TradeSignal]:
        """Run one polling cycle across all configured strategy modes."""
        self._process_telegram_actions()
        candles_by_tf: Dict[str, List[Dict[str, Any]]] = {}
        for prof in self.profiles:
            tf = prof.strategy.timeframe
            if tf not in candles_by_tf:
                candles_by_tf[tf] = self.broker.get_candles(tf, count=300)

        quote = self.broker.get_quote()
        self._sync_positions(quote.mid)
        self._update_mfe(quote.mid)
        self._check_early_exits(candles_by_tf, quote.mid)

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

        self._maybe_send_heartbeat(quote.mid, quote.spread)
        return triggered

    def _update_mfe(self, current_price: float) -> None:
        for pid, pos in self._tracked_positions.items():
            prev_mfe = self._position_mfe_price.get(pid, pos.entry_price)
            if pos.side == "BUY":
                self._position_mfe_price[pid] = max(prev_mfe, current_price)
            else:
                self._position_mfe_price[pid] = min(prev_mfe, current_price)

    def _check_early_exits(
        self,
        candles_by_tf: Dict[str, List[Dict[str, Any]]],
        current_price: float,
        now_sec: Optional[int] = None,
    ) -> None:
        if not self._tracked_positions:
            return
        exit_tf = "M5" if "M5" in candles_by_tf else self.profiles[0].strategy.timeframe
        exit_candles = candles_by_tf.get(exit_tf, [])
        if not exit_candles:
            return
        latest_c = exit_candles[-1]
        latest_ts = int(latest_c["time"])
        if self._last_exit_check_candle_time == latest_ts:
            return
        self._last_exit_check_candle_time = latest_ts

        # Also update MFE from the latest closed candle high/low
        for pid, pos in self._tracked_positions.items():
            opened_candle = self._position_opened_candle.get(pid, 0)
            if latest_ts > opened_candle:
                prev_mfe = self._position_mfe_price.get(pid, pos.entry_price)
                if pos.side == "BUY":
                    self._position_mfe_price[pid] = max(prev_mfe, float(latest_c["high"]))
                else:
                    self._position_mfe_price[pid] = min(prev_mfe, float(latest_c["low"]))

        exit_prof = next((p for p in self.profiles if p.strategy.timeframe == exit_tf), self.profiles[0])
        now = int(now_sec if now_sec is not None else time.time())
        min_age_sec = max(0, self.cfg.risk.exit_alert_min_minutes) * 60

        for pid, pos in self._tracked_positions.items():
            if pid in self._position_exit_alerted:
                continue
            opened_at = self._position_opened_at.get(pid, now)
            if min_age_sec > 0 and (now - opened_at) < min_age_sec:
                continue
            advice = evaluate_trade_management(
                side=pos.side,
                entry_price=pos.entry_price,
                current_sl=pos.stop_loss,
                take_profit=pos.take_profit,
                max_favorable_price=self._position_mfe_price.get(pid, pos.entry_price),
                candles=exit_candles,
                cfg=exit_prof.strategy,
                be_done=pid in self._position_be_alerted,
                lock_done=pid in self._position_lock_alerted,
            )
            if advice is None:
                continue
            age_min = max(0, (now - opened_at) // 60)
            mode_label = self._position_modes.get(pid, exit_prof.name)
            if advice.action == "close_now":
                self._position_exit_alerted.add(pid)
                logger.info("[%s] Close advice for %s: %s", mode_label, pid, advice.reason)
                self.notifier.notify_close_warning(
                    side=pos.side,
                    entry_price=pos.entry_price,
                    current_price=current_price,
                    unrealized_pnl=pos.unrealized_pnl,
                    reason=advice.reason,
                    age_minutes=age_min,
                    mode_label=mode_label,
                )
            elif advice.action in ("move_sl_be", "lock_profit") and advice.suggested_sl is not None:
                if advice.action == "move_sl_be":
                    self._position_be_alerted.add(pid)
                else:
                    self._position_lock_alerted.add(pid)
                logger.info("[%s] Move SL advice (%s -> %.2f) for %s: %s", mode_label, advice.action, advice.suggested_sl, pid, advice.reason)
                self.notifier.notify_move_sl_advice(
                    side=pos.side,
                    action=advice.action,
                    entry_price=pos.entry_price,
                    current_price=current_price,
                    new_sl=advice.suggested_sl,
                    unrealized_pnl=pos.unrealized_pnl,
                    reason=advice.reason,
                    mode_label=mode_label,
                )

    def _maybe_send_heartbeat(
        self, price: float, spread: float, now_utc: Optional[datetime] = None
    ) -> bool:
        hb_hours = self.cfg.telegram.heartbeat_hours
        tz = timezone(timedelta(hours=self.cfg.telegram.heartbeat_tz_offset_hours))
        time_label = ""

        if hb_hours:
            current_utc = now_utc or datetime.now(timezone.utc)
            now_local = current_utc.astimezone(tz)
            if now_local.hour not in hb_hours:
                return False
            slot = f"{now_local.strftime('%Y-%m-%d')}-{now_local.hour:02d}"
            if slot in self._sent_heartbeat_slots:
                return False
            if len(self._sent_heartbeat_slots) > 32:
                self._sent_heartbeat_slots.clear()
            self._sent_heartbeat_slots.add(slot)
            time_label = now_local.strftime("%H:%M")
        else:
            interval = self.cfg.telegram.status_interval_minutes * 60
            if interval <= 0:
                return False
            now = time.time()
            if now - self._last_status_time < interval:
                return False
            self._last_status_time = now

        account = self.broker.get_account_summary()
        upcoming = self.news_filter.upcoming_events(hours=12)
        next_news = None
        if upcoming:
            ev = upcoming[0]
            dt_str = datetime.fromtimestamp(ev.timestamp_sec, tz=tz).strftime("%H:%M %d/%m VN")
            next_news = f"{ev.title} ({dt_str})"
        extra: Dict[str, Any] = {}
        if next_news:
            extra["next_news"] = next_news
        if time_label:
            extra["time_label"] = time_label
        self.notifier.notify_status(
            provider=self.cfg.broker.provider,
            balance=account.balance,
            open_positions=len(self._tracked_positions),
            last_price=price,
            spread_usd=spread,
            extra=extra or None,
        )
        return True

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
