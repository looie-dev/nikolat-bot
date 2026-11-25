"""Telegram notification client for XAUUSD bot."""

from datetime import datetime, timedelta, timezone
import html
import json
import logging
from typing import Any, Dict, Optional
import urllib.request

from config import TelegramConfig
from strategy import TradeSignal

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends formatted Vietnamese trade alerts and status updates via Telegram Bot API."""

    def __init__(self, cfg: TelegramConfig):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def send_message(self, text: str) -> bool:
        if not self.enabled:
            logger.debug("Telegram disabled; message skipped: %s", text)
            return False
        url = f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": self.cfg.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:
            logger.error("Telegram send_message failed: %s", exc)
            return False

    def notify_startup(
        self,
        provider: str,
        environment: str,
        symbol: str,
        balance: float,
        currency: str,
        timeframe: str,
        risk_percent: float,
        dry_run: bool,
    ) -> bool:
        mode_tag = "🧪 DRY-RUN (chỉ báo, không đặt lệnh)" if dry_run else f"🤖 AUTO-TRADE ({environment.upper()})"
        msg = (
            f"<b>🚀 XAUUSD BOT ĐÃ KHỞI ĐỘNG</b>\n"
            f"• Chế độ: {mode_tag}\n"
            f"• Sàn: <code>{html.escape(provider.upper())}</code> | Symbol: <code>{html.escape(symbol)}</code>\n"
            f"• Khung: <code>{html.escape(timeframe)}</code> | Risk: <code>{risk_percent:.1f}%/lệnh</code>\n"
            f"• Số dư: <b>{balance:,.2f} {html.escape(currency)}</b>"
        )
        return self.send_message(msg)

    def notify_order_opened(
        self,
        signal: TradeSignal,
        size: float,
        risk_usd: float,
        spread_usd: float,
        order_id: str,
        balance: float,
        dry_run: bool = False,
        mode_label: str = "",
    ) -> bool:
        is_buy = signal.side == "BUY"
        action_label = "🟢 BUY NOW" if is_buy else "🔴 SELL NOW"
        prefix = "[DRY-RUN] " if dry_run else ""
        mode_tag = f" [{html.escape(mode_label)}]" if mode_label else ""

        sl_dist = abs(signal.entry_price - signal.stop_loss)
        tp_dist = abs(signal.take_profit - signal.entry_price)
        if is_buy:
            zone_low = round(signal.entry_price - 0.25 * sl_dist, 2)
            zone_high = round(signal.entry_price + 0.25 * tp_dist, 2)
        else:
            zone_low = round(signal.entry_price - 0.25 * tp_dist, 2)
            zone_high = round(signal.entry_price + 0.25 * sl_dist, 2)

        msg = (
            f"<b>{prefix}{action_label} — XAUUSD{mode_tag}</b>\n"
            f"• Vào: <code>{signal.entry_price:.2f}</code> ({zone_low:.2f} – {zone_high:.2f})\n"
            f"• SL: <code>{signal.stop_loss:.2f}</code> (${sl_dist:.2f})\n"
            f"• TP: <code>{signal.take_profit:.2f}</code> (${tp_dist:.2f} | 1:{signal.rr_ratio:.1f})\n"
            f"• Lot: <b>{size:.2f}</b> (~${risk_usd:.2f})"
        )
        return self.send_message(msg)

    def notify_signal_skipped(self, signal: TradeSignal, reason: str, mode_label: str = "") -> bool:
        side_icon = "🟡 TÍN HIỆU BỊ BỎ QUA"
        mode_tag = f" [{html.escape(mode_label)}]" if mode_label else ""
        msg = (
            f"<b>{side_icon}{mode_tag} ({signal.side} XAUUSD @ {signal.entry_price:.2f})</b>\n"
            f"• Lý do: {html.escape(reason)}"
        )
        return self.send_message(msg)

    def notify_position_closed(
        self,
        side: str,
        entry_price: float,
        close_price: float,
        pnl_usd: float,
        balance: float,
        position_id: str,
        reason: str = "Đóng lệnh",
        mode_label: str = "",
    ) -> bool:
        icon = "✅ CHỐT LỜI" if pnl_usd >= 0 else "❌ CẮT LỖ"
        mode_tag = f" [{html.escape(mode_label)}]" if mode_label else ""
        msg = (
            f"<b>{icon} — XAUUSD {side}{mode_tag}</b>\n"
            f"• Mã lệnh: <code>{html.escape(str(position_id))}</code> ({html.escape(reason)})\n"
            f"• Entry: <code>{entry_price:.2f}</code> → Close: <code>{close_price:.2f}</code>\n"
            f"• PnL: <b>{pnl_usd:+.2f} USD</b>\n"
            f"• Số dư mới: <b>${balance:,.2f}</b>"
        )
        return self.send_message(msg)

    def notify_status(
        self,
        provider: str,
        balance: float,
        open_positions: int,
        last_price: float,
        spread_usd: float,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        msg = (
            f"<b>💓 XAUUSD BOT HEARTBEAT</b>\n"
            f"• Sàn: <code>{html.escape(provider.upper())}</code> | Giá: <b>{last_price:.2f}</b> (Spread ${spread_usd:.2f})\n"
            f"• Số dư: <b>${balance:,.2f}</b> | Lệnh đang mở: <b>{open_positions}</b>"
        )
        if extra and extra.get("next_news"):
            msg += f"\n• Tin USD gần nhất: {html.escape(str(extra['next_news']))}"
        return self.send_message(msg)
