"""Main runner for the XAUUSD Multi-Confluence Trading Bot."""

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import html
import logging
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from brokers import BrokerClient, PaperBroker, Position, create_broker
from config import BotConfig, StrategyProfile, load_config
from health_server import start_health_server
from news_filter import NewsFilter
from notifier import TelegramNotifier, TelegramUserAction
from risk_manager import RiskManager
from state_store import load_json_state, save_json_state
from strategy import (
    TradeSignal,
    TrendPullbackStrategy,
    evaluate_trade_management,
    summarize_market_state,
)

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
            loss_streak_reduce_after=cfg.risk.loss_streak_reduce_after,
            loss_streak_risk_factor=cfg.risk.loss_streak_risk_factor,
        )
        if cfg.bot_state_file:
            self._bot_state_path = Path(cfg.bot_state_file)
        elif cfg.broker.paper_state_file:
            self._bot_state_path = Path(cfg.broker.paper_state_file).with_name("bot_state.json")
        else:
            self._bot_state_path = Path("data/bot_state.json")

        self._tracked_positions: Dict[str, Position] = {}
        self._position_modes: Dict[str, str] = {}
        self._position_opened_at: Dict[str, int] = {}
        self._position_opened_candle: Dict[str, int] = {}
        self._position_mfe_price: Dict[str, float] = {}
        self._position_exit_alerted: Set[str] = set()
        self._position_be_alerted: Set[str] = set()
        self._position_lock_alerted: Set[str] = set()
        self._last_evaluated_candle_time: Dict[str, int] = {}
        self._last_exit_check_candle_by_tf: Dict[str, int] = {}
        self._candles_since_close: Dict[str, int] = {
            p.name: cfg.risk.cooldown_candles for p in self.profiles
        }
        self._last_atr: Dict[str, float] = {}
        self._last_status_time: float = 0.0
        self._sent_heartbeat_slots: Set[str] = set()
        self._sent_news_warnings: Set[str] = set()
        self._sent_weekend_warnings: Set[str] = set()
        self._pending_confirmations: Dict[str, Dict[str, Any]] = {}
        self._msg_to_confirm: Dict[int, str] = {}
        self._msg_to_position: Dict[int, str] = {}
        self._deferred_info_actions: List[TelegramUserAction] = []
        self._tg_offset: int = 0
        self._confirm_seq: int = 0
        self._paused: bool = False
        self._running: bool = True

    @property
    def _last_exit_check_candle_time(self) -> Optional[int]:
        if not self._last_exit_check_candle_by_tf:
            return None
        return max(self._last_exit_check_candle_by_tf.values())

    @_last_exit_check_candle_time.setter
    def _last_exit_check_candle_time(self, value: Optional[int]) -> None:
        if value is None:
            self._last_exit_check_candle_by_tf.clear()
        else:
            for tf in list(self._last_exit_check_candle_by_tf.keys()):
                self._last_exit_check_candle_by_tf[tf] = value

    def _serialize_signal(self, sig: TradeSignal) -> Dict[str, Any]:
        return asdict(sig)

    def _deserialize_signal(self, raw: Dict[str, Any]) -> TradeSignal:
        return TradeSignal(
            side=str(raw["side"]),
            entry_price=float(raw["entry_price"]),
            stop_loss=float(raw["stop_loss"]),
            take_profit=float(raw["take_profit"]),
            atr=float(raw["atr"]),
            rr_ratio=float(raw["rr_ratio"]),
            candle_time=int(raw["candle_time"]),
            reason=str(raw.get("reason", "")),
            indicators={str(k): float(v) for k, v in (raw.get("indicators") or {}).items()},
        )

    def _save_runtime_state(self) -> None:
        pending_ser: Dict[str, Any] = {}
        for cid, item in self._pending_confirmations.items():
            pending_ser[cid] = {
                "signal": self._serialize_signal(item["signal"]),
                "size": float(item["size"]),
                "risk_usd": float(item["risk_usd"]),
                "spread": float(item.get("spread", 0.0)),
                "mode": str(item["mode"]),
                "msg_id": item.get("msg_id"),
                "created_at": int(item["created_at"]),
            }
        payload = {
            "position_modes": self._position_modes,
            "position_opened_at": self._position_opened_at,
            "position_opened_candle": self._position_opened_candle,
            "position_mfe_price": self._position_mfe_price,
            "position_exit_alerted": sorted(self._position_exit_alerted),
            "position_be_alerted": sorted(self._position_be_alerted),
            "position_lock_alerted": sorted(self._position_lock_alerted),
            "last_evaluated_candle_time": self._last_evaluated_candle_time,
            "candles_since_close": self._candles_since_close,
            "last_atr": self._last_atr,
            "sent_heartbeat_slots": sorted(self._sent_heartbeat_slots),
            "sent_news_warnings": sorted(self._sent_news_warnings),
            "sent_weekend_warnings": sorted(self._sent_weekend_warnings),
            "pending_confirmations": pending_ser,
            "msg_to_confirm": {str(k): v for k, v in self._msg_to_confirm.items()},
            "msg_to_position": {str(k): v for k, v in self._msg_to_position.items()},
            "confirm_seq": self._confirm_seq,
            "paused": self._paused,
            "risk_state": self.risk_manager.export_state(),
        }
        try:
            save_json_state(self._bot_state_path, payload, remote_key=f"bot:{self._bot_state_path.name}")
        except Exception as exc:
            logger.warning("Failed to save bot runtime state: %s", exc)

    def _load_runtime_state(self) -> None:
        state = load_json_state(
            self._bot_state_path,
            default_factory=dict,
            remote_key=f"bot:{self._bot_state_path.name}",
        )
        if not state:
            return
        self._position_modes = {str(k): str(v) for k, v in (state.get("position_modes") or {}).items()}
        self._position_opened_at = {str(k): int(v) for k, v in (state.get("position_opened_at") or {}).items()}
        self._position_opened_candle = {str(k): int(v) for k, v in (state.get("position_opened_candle") or {}).items()}
        self._position_mfe_price = {str(k): float(v) for k, v in (state.get("position_mfe_price") or {}).items()}
        self._position_exit_alerted = {str(x) for x in (state.get("position_exit_alerted") or [])}
        self._position_be_alerted = {str(x) for x in (state.get("position_be_alerted") or [])}
        self._position_lock_alerted = {str(x) for x in (state.get("position_lock_alerted") or [])}
        self._last_evaluated_candle_time = {
            str(k): int(v) for k, v in (state.get("last_evaluated_candle_time") or {}).items()
        }
        if state.get("candles_since_close"):
            self._candles_since_close.update({str(k): int(v) for k, v in state["candles_since_close"].items()})
        self._last_atr = {str(k): float(v) for k, v in (state.get("last_atr") or {}).items()}
        self._sent_heartbeat_slots = {str(x) for x in (state.get("sent_heartbeat_slots") or [])}
        self._sent_news_warnings = {str(x) for x in (state.get("sent_news_warnings") or [])}
        self._sent_weekend_warnings = {str(x) for x in (state.get("sent_weekend_warnings") or [])}
        self._confirm_seq = int(state.get("confirm_seq", 0))
        self._paused = bool(state.get("paused", False))
        if state.get("risk_state"):
            self.risk_manager.restore_state(state["risk_state"])
        for cid, raw in (state.get("pending_confirmations") or {}).items():
            try:
                self._pending_confirmations[str(cid)] = {
                    "signal": self._deserialize_signal(raw["signal"]),
                    "size": float(raw["size"]),
                    "risk_usd": float(raw["risk_usd"]),
                    "spread": float(raw.get("spread", 0.0)),
                    "mode": str(raw["mode"]),
                    "msg_id": raw.get("msg_id"),
                    "created_at": int(raw["created_at"]),
                }
            except Exception as exc:
                logger.warning("Skipping corrupt pending confirmation %s: %s", cid, exc)
        self._msg_to_confirm = {int(k): str(v) for k, v in (state.get("msg_to_confirm") or {}).items()}
        self._msg_to_position = {int(k): str(v) for k, v in (state.get("msg_to_position") or {}).items()}

    def _clear_position_tracking(self, pid: str) -> None:
        self._tracked_positions.pop(pid, None)
        self._position_modes.pop(pid, None)
        self._position_opened_at.pop(pid, None)
        self._position_opened_candle.pop(pid, None)
        self._position_mfe_price.pop(pid, None)
        self._position_exit_alerted.discard(pid)
        self._position_be_alerted.discard(pid)
        self._position_lock_alerted.discard(pid)

    def initialize(self) -> None:
        """Load saved runtime state + broker state and send startup Telegram notification."""
        self._load_runtime_state()
        account = self.broker.get_account_summary()
        now_sec = int(time.time())
        open_positions = self.broker.get_open_positions()
        active_ids = {p.position_id for p in open_positions}
        for stale_id in list(self._position_modes.keys()):
            if stale_id not in active_ids:
                self._clear_position_tracking(stale_id)
        for pos in open_positions:
            self._tracked_positions[pos.position_id] = pos
            self._position_opened_at.setdefault(pos.position_id, now_sec)
            self._position_mfe_price.setdefault(pos.position_id, pos.entry_price)
        self.news_filter.refresh(force=True)
        if self.cfg.manual_confirm_tracking or isinstance(self.notifier, TelegramNotifier):
            offset = self.notifier.get_latest_update_offset()
            if isinstance(offset, int):
                self._tg_offset = offset
        self._last_status_time = time.time()
        mode_summary = " + ".join(f"{p.name}({p.strategy.timeframe})" for p in self.profiles)
        logger.info(
            "Bot initialized | provider=%s env=%s symbol=%s modes=[%s] balance=%.2f %s open=%d paused=%s",
            self.cfg.broker.provider,
            self.cfg.broker.environment,
            self.broker.symbol,
            mode_summary,
            account.balance,
            account.currency,
            len(self._tracked_positions),
            self._paused,
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
        self._save_runtime_state()

    def _sync_positions(self, current_price: float) -> None:
        current_positions = self.broker.get_open_positions()
        current_map = {p.position_id: p for p in current_positions}
        state_changed = False

        closed_ids = [pid for pid in self._tracked_positions if pid not in current_map]
        if closed_ids:
            state_changed = True
            account = self.broker.get_account_summary()
            for pid in closed_ids:
                prev = self._tracked_positions[pid]
                mode_label = self._position_modes.get(pid, "")
                self._clear_position_tracking(pid)
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
                            current_map[pid] = replace(pos, stop_loss=round(pos.entry_price, 2))
                            state_changed = True
                            logger.info("Moved BUY %s SL to breakeven %.2f", pid, pos.entry_price)
                elif pos.side == "SELL" and (pos.stop_loss is None or pos.stop_loss > pos.entry_price):
                    if current_price <= pos.entry_price - trigger_dist:
                        if self.broker.update_stop_loss(pid, pos.entry_price):
                            current_map[pid] = replace(pos, stop_loss=round(pos.entry_price, 2))
                            state_changed = True
                            logger.info("Moved SELL %s SL to breakeven %.2f", pid, pos.entry_price)

        self._tracked_positions = current_map
        if state_changed:
            self._save_runtime_state()

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
        htf_candles: Optional[List[Dict[str, Any]]] = None,
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
                self._save_runtime_state()
                return None

        if self._candles_since_close[profile.name] <= self.cfg.risk.cooldown_candles:
            logger.debug(
                "[%s] Cooldown active (%d/%d candles)",
                profile.name,
                self._candles_since_close[profile.name],
                self.cfg.risk.cooldown_candles,
            )
            self._save_runtime_state()
            return None

        sig = self._strategies[profile.name].evaluate_latest(candles, htf_candles=htf_candles)
        if sig is None:
            self._save_runtime_state()
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
            self._save_runtime_state()
            logger.info("[%s] Sent signal %s waiting for Telegram confirmation", profile.name, confirm_id)
            return live_sig

        result = self.broker.place_market_order(
            side=live_sig.side,
            size=decision.size,
            stop_loss=live_sig.stop_loss,
            take_profit=live_sig.take_profit,
        )
        if result.success:
            fill_p = result.fill_price or live_sig.entry_price
            self._register_open_position(
                order_id=result.order_id,
                mode_name=profile.name,
                sig=live_sig,
                size=result.size,
                fill_price=fill_p,
                stop_loss=live_sig.stop_loss,
                take_profit=live_sig.take_profit,
                balance=account.balance,
                now_sec=int(time.time()),
            )
            filled_sig = replace(live_sig, entry_price=round(fill_p, 2))
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
            self._save_runtime_state()
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

    def _register_open_position(
        self,
        order_id: str,
        mode_name: str,
        sig: TradeSignal,
        size: float,
        fill_price: float,
        stop_loss: float,
        take_profit: float,
        balance: float,
        now_sec: int,
    ) -> None:
        self.risk_manager.record_trade_opened(balance)
        self._position_modes[order_id] = mode_name
        self._position_opened_at[order_id] = now_sec
        self._position_opened_candle[order_id] = int(sig.candle_time)
        self._position_mfe_price[order_id] = fill_price
        self._tracked_positions[order_id] = Position(
            position_id=order_id,
            side=sig.side,
            size=size,
            entry_price=fill_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            unrealized_pnl=0.0,
        )

    def _reanchor_sl_tp(self, sig: TradeSignal, entry_override: Optional[float]) -> Tuple[float, float]:
        if entry_override is None or entry_override <= 0:
            return sig.stop_loss, sig.take_profit
        sl_dist = abs(sig.entry_price - sig.stop_loss)
        tp_dist = abs(sig.take_profit - sig.entry_price)
        if sig.side == "BUY":
            return round(entry_override - sl_dist, 2), round(entry_override + tp_dist, 2)
        return round(entry_override + sl_dist, 2), round(entry_override - tp_dist, 2)

    def _handle_enter_action(self, act: TelegramUserAction, now: int) -> bool:
        cid = self._resolve_pending_id(act)
        if not cid:
            if act.callback_query_id:
                self.notifier.answer_callback_query(act.callback_query_id, "Tín hiệu đã hết hạn hoặc đã xử lý")
            return False
        pending = self._pending_confirmations.pop(cid)
        sig: TradeSignal = pending["signal"]
        msg_id = pending.get("msg_id")
        if msg_id:
            self._msg_to_confirm.pop(msg_id, None)
            self.notifier.clear_inline_buttons(msg_id)

        entry_override = act.custom_price
        adj_sl, adj_tp = self._reanchor_sl_tp(sig, entry_override)
        order_kwargs: Dict[str, Any] = {
            "side": sig.side,
            "size": pending["size"],
            "stop_loss": adj_sl,
            "take_profit": adj_tp,
        }
        if isinstance(self.broker, PaperBroker):
            order_kwargs["entry_override"] = entry_override
        result = self.broker.place_market_order(**order_kwargs)
        if not result.success:
            if act.callback_query_id:
                self.notifier.answer_callback_query(act.callback_query_id, f"Lỗi: {result.message}")
            self.notifier.send_message(f"⚠️ Không ghi nhận được lệnh: {result.message}")
            return True

        account = self.broker.get_account_summary()
        fill_p = result.fill_price or entry_override or sig.entry_price
        mode_name = pending["mode"]
        self._register_open_position(
            order_id=result.order_id,
            mode_name=mode_name,
            sig=sig,
            size=result.size,
            fill_price=fill_p,
            stop_loss=adj_sl,
            take_profit=adj_tp,
            balance=account.balance,
            now_sec=now,
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
        if entry_override is not None and entry_override > 0:
            diff = abs(fill_p - sig.entry_price)
            if diff >= max(1.0, 2.0 * float(pending.get("spread", 0.5))):
                self.notifier.send_message(
                    f"⚠️ <b>Lệch giá sàn ${diff:.2f}</b> so với tín hiệu gốc ({sig.entry_price:.2f}) — "
                    f"SL/TP đã tự dịch thành <code>{adj_sl:.2f}</code> / <code>{adj_tp:.2f}</code>."
                )
        logger.info("[%s] User confirmed trade entry %s @ %.2f", mode_name, result.order_id, fill_p)
        return True

    def _handle_movesl_action(self, act: TelegramUserAction) -> bool:
        pid = self._resolve_position_id(act)
        new_sl = act.custom_price
        if not pid or new_sl is None or new_sl <= 0:
            if act.callback_query_id:
                self.notifier.answer_callback_query(act.callback_query_id, "Không tìm thấy lệnh hoặc giá SL không hợp lệ")
            return False
        pos = self._tracked_positions[pid]
        if not self.broker.update_stop_loss(pid, new_sl):
            if act.callback_query_id:
                self.notifier.answer_callback_query(act.callback_query_id, "Cập nhật SL thất bại")
            return False
        rounded_sl = round(new_sl, 2)
        self._tracked_positions[pid] = replace(pos, stop_loss=rounded_sl)
        if (pos.side == "BUY" and rounded_sl >= pos.entry_price) or (
            pos.side == "SELL" and rounded_sl <= pos.entry_price
        ):
            self._position_be_alerted.add(pid)
        if act.callback_query_id:
            self.notifier.answer_callback_query(act.callback_query_id, f"Đã dời SL -> {rounded_sl:.2f}")
            if act.reply_to_message_id:
                self.notifier.clear_inline_buttons(act.reply_to_message_id)
        mode_label = self._position_modes.get(pid, "")
        mode_tag = f" [{html.escape(mode_label)}]" if mode_label else ""
        self.notifier.send_message(
            f"🛡 <b>ĐÃ CẬP NHẬT SL — XAUUSD {pos.side}{mode_tag}</b>\n"
            f"• Mã lệnh: <code>{html.escape(pid)}</code> | SL mới: <code>{rounded_sl:.2f}</code>"
        )
        return True

    def _handle_close_action(self, act: TelegramUserAction) -> bool:
        pid = self._resolve_position_id(act)
        if not pid:
            if act.callback_query_id:
                self.notifier.answer_callback_query(act.callback_query_id, "Không tìm thấy lệnh đang mở")
            return False
        pos = self._tracked_positions.get(pid)
        mode_label = self._position_modes.get(pid, "")
        if not isinstance(self.broker, PaperBroker):
            if act.callback_query_id:
                self.notifier.answer_callback_query(act.callback_query_id, "Đã dừng theo dõi")
            self._clear_position_tracking(pid)
            return True
        closed = self.broker.close_position(pid, close_price=act.custom_price, reason="Đóng thủ công qua Telegram")
        if not closed:
            return False
        account = self.broker.get_account_summary()
        self.risk_manager.record_trade_closed(closed.realized_pnl, account.balance)
        self._clear_position_tracking(pid)
        if mode_label:
            self._candles_since_close[mode_label] = 0
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
        return True

    def _process_telegram_actions(self, now_sec: Optional[int] = None) -> None:
        if not (self.cfg.manual_confirm_tracking or isinstance(self.notifier, TelegramNotifier)):
            return
        now = int(now_sec if now_sec is not None else time.time())
        state_changed = False
        # Expire unconfirmed signals older than 20 minutes (default skipped)
        for k in [cid for cid, v in self._pending_confirmations.items() if now - v["created_at"] > 1200]:
            item = self._pending_confirmations.pop(k)
            state_changed = True
            if item.get("msg_id"):
                self._msg_to_confirm.pop(item["msg_id"], None)
                self.notifier.clear_inline_buttons(item["msg_id"])

        polled = self.notifier.poll_updates(self._tg_offset)
        if not isinstance(polled, tuple) or len(polled) != 2:
            if state_changed:
                self._save_runtime_state()
            return
        actions, next_offset = polled
        if isinstance(next_offset, int):
            self._tg_offset = next_offset
        if not isinstance(actions, list):
            if state_changed:
                self._save_runtime_state()
            return

        for act in actions:
            if act.action in ("status", "news", "help"):
                self._deferred_info_actions.append(act)
            elif act.action == "pause":
                self._paused = True
                state_changed = True
                self.notifier.send_message(
                    "⏸ <b>ĐÃ TẠM DỪNG TÍN HIỆU MỚI</b>\n"
                    "• Bot vẫn tiếp tục theo dõi lệnh đang mở và cảnh báo dời SL/đóng lệnh.\n"
                    "• Gửi <code>/resume</code> để bật lại quét kèo."
                )
            elif act.action == "resume":
                self._paused = False
                state_changed = True
                self.notifier.send_message("▶️ <b>ĐÃ BẬT LẠI QUÉT TÍN HIỆU MỚI</b>")
            elif act.action == "enter":
                state_changed |= self._handle_enter_action(act, now)
            elif act.action == "skip":
                cid = self._resolve_pending_id(act)
                if cid:
                    pending = self._pending_confirmations.pop(cid)
                    state_changed = True
                    if pending.get("msg_id"):
                        self._msg_to_confirm.pop(pending["msg_id"], None)
                        self.notifier.clear_inline_buttons(pending["msg_id"])
                if act.callback_query_id:
                    self.notifier.answer_callback_query(act.callback_query_id, "Đã bỏ qua")
                self.notifier.send_message("👌 Đã bỏ qua tín hiệu — bot sẽ không theo dõi lệnh này.")
            elif act.action == "movesl":
                state_changed |= self._handle_movesl_action(act)
            elif act.action == "close":
                state_changed |= self._handle_close_action(act)

        if state_changed:
            self._save_runtime_state()

    def _handle_info_actions(
        self,
        candles_by_tf: Dict[str, List[Dict[str, Any]]],
        bid: float,
        ask: float,
        spread: float,
    ) -> None:
        if not self._deferred_info_actions:
            return
        actions = list(self._deferred_info_actions)
        self._deferred_info_actions.clear()
        tz = timezone(timedelta(hours=self.cfg.telegram.heartbeat_tz_offset_hours))
        mid = round((bid + ask) / 2.0, 2)

        for act in actions:
            if act.action == "help":
                self.notifier.send_message(
                    "<b>📖 LỆNH TELEGRAM HỖ TRỢ</b>\n"
                    "• <code>/status</code> — Giá, số dư, lệnh đang mở & chỉ báo EMA/RSI/ADX\n"
                    "• <code>/news</code> — Lịch tin USD High-Impact tuần này\n"
                    "• <code>/pause</code> / <code>/resume</code> — Tạm dừng hoặc bật lại báo kèo mới\n"
                    "• <code>/sl 3570.5</code> — Cập nhật mức SL bot đang theo dõi\n"
                    "• Trả lời tín hiệu: <code>vào [giá]</code>, <code>bỏ</code>, <code>đóng [giá]</code>"
                )
            elif act.action == "news":
                events = self.news_filter.upcoming_events(hours=168)
                if not events:
                    self.notifier.send_message("📅 Không có tin <b>USD High-Impact</b> sắp tới trong tuần này.")
                else:
                    lines = ["<b>📅 LỊCH TIN USD HIGH-IMPACT SẮP TỚI</b>"]
                    for ev in events[:10]:
                        dt_str = datetime.fromtimestamp(ev.timestamp_sec, tz=tz).strftime("%a %d/%m %H:%M VN")
                        lines.append(f"• <code>{dt_str}</code> — {html.escape(ev.title)}")
                    self.notifier.send_message("\n".join(lines))
            elif act.action == "status":
                account = self.broker.get_account_summary()
                state_str = "⏸ TẠM DỪNG" if self._paused else "🟢 ĐANG CHẠY"
                risk_str = f"{self.risk_manager.effective_risk_percent:.1f}%"
                if self.risk_manager.consecutive_losses >= max(1, self.cfg.risk.loss_streak_reduce_after) and self.cfg.risk.loss_streak_reduce_after > 0:
                    risk_str += f" (giảm sau {self.risk_manager.consecutive_losses} lệnh thua)"
                lines = [
                    f"<b>📊 TRẠNG THÁI BOT</b> ({state_str})",
                    f"• Giá: <b>{mid:.2f}</b> (Spread ${spread:.2f}) | Balance: <b>${account.balance:,.2f}</b>",
                    f"• Risk hiệu dụng: <b>{risk_str}</b> | Đang mở: <b>{len(self._tracked_positions)}</b>",
                ]
                for pid, pos in self._tracked_positions.items():
                    mode_tag = self._position_modes.get(pid, "?")
                    sl_txt = f"{pos.stop_loss:.2f}" if pos.stop_loss is not None else "—"
                    tp_txt = f"{pos.take_profit:.2f}" if pos.take_profit is not None else "—"
                    lines.append(
                        f"  └ <code>{html.escape(pid)}</code> [{html.escape(mode_tag)}] {pos.side} @ <code>{pos.entry_price:.2f}</code> "
                        f"| SL <code>{sl_txt}</code> / TP <code>{tp_txt}</code> (~<b>{pos.unrealized_pnl:+.2f}$</b>)"
                    )
                for prof in self.profiles:
                    snap = summarize_market_state(
                        candles_by_tf.get(prof.strategy.timeframe, []),
                        prof.strategy,
                        htf_candles=candles_by_tf.get(prof.strategy.htf_timeframe) if prof.strategy.htf_timeframe else None,
                    )
                    if snap:
                        htf_txt = ""
                        if snap.get("htf_bias") is not None:
                            htf_map = {1: "TĂNG", -1: "GIẢM", 0: "TRUNG LẬP"}
                            htf_txt = f" | {prof.strategy.htf_timeframe}: {htf_map.get(snap['htf_bias'], '?')}"
                        sess_txt = "Trong phiên" if snap["in_session"] else "Ngoài phiên"
                        lines.append(
                            f"• <b>{html.escape(prof.name)}</b>: {snap['trend']} ({sess_txt}{htf_txt}) "
                            f"| RSI <code>{snap['rsi']:.1f}</code> | ADX <code>{snap['adx']:.1f}</code> | ATR <code>${snap['atr']:.2f}</code>"
                        )
                self.notifier.send_message("\n".join(lines))

    def run_once(self) -> Optional[TradeSignal]:
        """Run one polling cycle across all configured strategy modes."""
        self._process_telegram_actions()
        candles_by_tf: Dict[str, List[Dict[str, Any]]] = {}
        for prof in self.profiles:
            for tf in (prof.strategy.timeframe, prof.strategy.htf_timeframe):
                if tf and tf not in candles_by_tf:
                    candles_by_tf[tf] = self.broker.get_candles(tf, count=300)

        quote = self.broker.get_quote()
        self._sync_positions(quote.mid)
        self._update_mfe(quote.mid)
        self._check_early_exits(candles_by_tf, quote.mid)
        self._check_upcoming_news_for_open_positions()
        self._maybe_send_weekend_warning()
        self._handle_info_actions(candles_by_tf, quote.bid, quote.ask, quote.spread)

        triggered: Optional[TradeSignal] = None
        if not self._paused:
            for prof in self.profiles:
                tf = prof.strategy.timeframe
                htf = prof.strategy.htf_timeframe
                res = self._evaluate_profile(
                    prof,
                    candles_by_tf.get(tf, []),
                    quote.bid,
                    quote.ask,
                    quote.spread,
                    htf_candles=candles_by_tf.get(htf) if htf else None,
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

    def _update_mfe_from_candles(self, candles_by_tf: Dict[str, List[Dict[str, Any]]]) -> None:
        for tf in ("M1", "M5", "M15", "M30", "H1"):
            bars = candles_by_tf.get(tf)
            if not bars:
                continue
            latest_c = bars[-1]
            latest_ts = int(latest_c["time"])
            for pid, pos in self._tracked_positions.items():
                opened_candle = self._position_opened_candle.get(pid, 0)
                if latest_ts > opened_candle:
                    prev_mfe = self._position_mfe_price.get(pid, pos.entry_price)
                    if pos.side == "BUY":
                        self._position_mfe_price[pid] = max(prev_mfe, float(latest_c["high"]))
                    else:
                        self._position_mfe_price[pid] = min(prev_mfe, float(latest_c["low"]))

    def _check_early_exits(
        self,
        candles_by_tf: Dict[str, List[Dict[str, Any]]],
        current_price: float,
        now_sec: Optional[int] = None,
    ) -> None:
        if not self._tracked_positions:
            return
        self._update_mfe_from_candles(candles_by_tf)

        profile_map = {p.name: p for p in self.profiles}
        fallback_tf = "M5" if "M5" in candles_by_tf else self.profiles[0].strategy.timeframe
        now = int(now_sec if now_sec is not None else time.time())
        min_age_sec = max(0, self.cfg.risk.exit_alert_min_minutes) * 60
        checked_new_tf: Dict[str, int] = {}
        state_changed = False

        for pid, pos in list(self._tracked_positions.items()):
            if pid in self._position_exit_alerted:
                continue
            mode_label = self._position_modes.get(pid, "")
            pos_prof = profile_map.get(mode_label, self.profiles[0])
            eval_tf = pos_prof.strategy.timeframe if pos_prof.strategy.timeframe in candles_by_tf else fallback_tf
            eval_candles = candles_by_tf.get(eval_tf, [])
            if not eval_candles:
                continue
            latest_ts = int(eval_candles[-1]["time"])
            if self._last_exit_check_candle_by_tf.get(eval_tf) == latest_ts:
                continue
            checked_new_tf[eval_tf] = latest_ts

            opened_at = self._position_opened_at.get(pid, now)
            if min_age_sec > 0 and (now - opened_at) < min_age_sec:
                continue
            advice = evaluate_trade_management(
                side=pos.side,
                entry_price=pos.entry_price,
                current_sl=pos.stop_loss,
                take_profit=pos.take_profit,
                max_favorable_price=self._position_mfe_price.get(pid, pos.entry_price),
                candles=eval_candles,
                cfg=pos_prof.strategy,
                be_done=pid in self._position_be_alerted,
                lock_done=pid in self._position_lock_alerted,
            )
            if advice is None:
                continue
            age_min = max(0, (now - opened_at) // 60)
            label = mode_label or pos_prof.name
            if advice.action == "close_now":
                self._position_exit_alerted.add(pid)
                state_changed = True
                logger.info("[%s] Close advice for %s: %s", label, pid, advice.reason)
                warn_id = self.notifier.notify_close_warning(
                    side=pos.side,
                    entry_price=pos.entry_price,
                    current_price=current_price,
                    unrealized_pnl=pos.unrealized_pnl,
                    reason=advice.reason,
                    age_minutes=age_min,
                    mode_label=label,
                    position_id=pid,
                )
                if isinstance(warn_id, int) and warn_id > 0:
                    self._msg_to_position[warn_id] = pid
            elif advice.action in ("move_sl_be", "lock_profit") and advice.suggested_sl is not None:
                if advice.action == "move_sl_be":
                    self._position_be_alerted.add(pid)
                else:
                    self._position_lock_alerted.add(pid)
                state_changed = True
                logger.info("[%s] Move SL advice (%s -> %.2f) for %s: %s", label, advice.action, advice.suggested_sl, pid, advice.reason)
                sl_msg_id = self.notifier.notify_move_sl_advice(
                    side=pos.side,
                    action=advice.action,
                    entry_price=pos.entry_price,
                    current_price=current_price,
                    new_sl=advice.suggested_sl,
                    unrealized_pnl=pos.unrealized_pnl,
                    reason=advice.reason,
                    mode_label=label,
                    position_id=pid,
                )
                if isinstance(sl_msg_id, int) and sl_msg_id > 0:
                    self._msg_to_position[sl_msg_id] = pid

        self._last_exit_check_candle_by_tf.update(checked_new_tf)
        if state_changed:
            self._save_runtime_state()

    def _check_upcoming_news_for_open_positions(self, now_sec: Optional[int] = None) -> bool:
        warn_mins = self.cfg.risk.news_warn_before_minutes
        if warn_mins <= 0 or not self._tracked_positions:
            return False
        now = int(now_sec if now_sec is not None else time.time())
        upcoming = self.news_filter.upcoming_events(hours=2, now_sec=now)
        tz = timezone(timedelta(hours=self.cfg.telegram.heartbeat_tz_offset_hours))
        sent = False
        for ev in upcoming:
            mins_left = int((ev.timestamp_sec - now + 59) // 60)
            if not (0 < (ev.timestamp_sec - now) <= warn_mins * 60):
                continue
            key = f"{ev.timestamp_sec}:{ev.title}"
            if key in self._sent_news_warnings:
                continue
            if len(self._sent_news_warnings) > 32:
                self._sent_news_warnings.clear()
            self._sent_news_warnings.add(key)
            dt_str = datetime.fromtimestamp(ev.timestamp_sec, tz=tz).strftime("%H:%M %d/%m VN")
            self.notifier.notify_news_warning(ev.title, mins_left, len(self._tracked_positions), dt_str)
            sent = True
        if sent:
            self._save_runtime_state()
        return sent

    def _maybe_send_weekend_warning(self, now_utc: Optional[datetime] = None) -> bool:
        warn_hour = self.cfg.risk.friday_close_warn_utc_hour
        if warn_hour < 0 or not self._tracked_positions:
            return False
        current_utc = now_utc or datetime.now(timezone.utc)
        if current_utc.weekday() != 4 or current_utc.hour < warn_hour:
            return False
        key = current_utc.strftime("%Y-%m-%d")
        if key in self._sent_weekend_warnings:
            return False
        if len(self._sent_weekend_warnings) > 16:
            self._sent_weekend_warnings.clear()
        self._sent_weekend_warnings.add(key)
        tz = timezone(timedelta(hours=self.cfg.telegram.heartbeat_tz_offset_hours))
        time_label_vn = current_utc.astimezone(tz).strftime("%H:%M %d/%m VN")
        self.notifier.notify_weekend_warning(len(self._tracked_positions), time_label_vn)
        self._save_runtime_state()
        return True

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
        self._save_runtime_state()
        return True

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "symbol": self.broker.symbol,
            "modes": [p.name for p in self.profiles],
            "open_positions": len(self._tracked_positions),
            "paused": self._paused,
            "consecutive_losses": self.risk_manager.consecutive_losses,
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
        cfg = replace(cfg, dry_run=True)

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
