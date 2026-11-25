"""Telegram notification and interactive reply/button client for XAUUSD bot."""

from dataclasses import dataclass
import html
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import urllib.request

from config import TelegramConfig
from strategy import TradeSignal

logger = logging.getLogger(__name__)

_ENTER_RE = re.compile(r"^(?:vào|vao|đã vào|da vao|enter|ok|in|1)(?:\s+([0-9]+(?:\.[0-9]+)?))?$")
_SKIP_RE = re.compile(r"^(?:bỏ|bo|bỏ qua|bo qua|skip|no|0)$")
_CLOSE_RE = re.compile(r"^(?:đóng|dong|đã đóng|da dong|close|exit|out)(?:\s+([0-9]+(?:\.[0-9]+)?))?$")


@dataclass(frozen=True)
class TelegramUserAction:
    action: str  # "enter", "skip", or "close"
    target_id: str = ""
    reply_to_message_id: Optional[int] = None
    callback_query_id: Optional[str] = None
    custom_price: Optional[float] = None


def parse_telegram_text_command(text: str) -> Optional[Tuple[str, Optional[float]]]:
    """Parse short Vietnamese/English Telegram replies like 'vào', 'vào 3501.2', 'bỏ', 'đóng'."""
    cleaned = " ".join((text or "").strip().lower().split())
    if not cleaned:
        return None
    m_enter = _ENTER_RE.match(cleaned)
    if m_enter:
        price = float(m_enter.group(1)) if m_enter.group(1) else None
        return ("enter", price)
    if _SKIP_RE.match(cleaned):
        return ("skip", None)
    m_close = _CLOSE_RE.match(cleaned)
    if m_close:
        price = float(m_close.group(1)) if m_close.group(1) else None
        return ("close", price)
    return None


class TelegramNotifier:
    """Sends formatted Vietnamese trade alerts and polls user replies/button taps."""

    def __init__(self, cfg: TelegramConfig):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def _post_json(self, method: str, payload_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        url = f"https://api.telegram.org/bot{self.cfg.bot_token}/{method}"
        payload = json.dumps(payload_dict).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except Exception as exc:
            logger.error("Telegram %s failed: %s", method, exc)
            return None

    def send_message_with_id(
        self,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        if not self.enabled:
            logger.debug("Telegram disabled; message skipped: %s", text)
            return None
        body: Dict[str, Any] = {
            "chat_id": self.cfg.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        res = self._post_json("sendMessage", body)
        if not res or not res.get("ok"):
            return None
        return int((res.get("result") or {}).get("message_id", 0)) or None

    def send_message(self, text: str) -> bool:
        return self.send_message_with_id(text) is not None

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        res = self._post_json(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )
        return bool(res and res.get("ok"))

    def clear_inline_buttons(self, message_id: int) -> bool:
        res = self._post_json(
            "editMessageReplyMarkup",
            {"chat_id": self.cfg.chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )
        return bool(res and res.get("ok"))

    def get_latest_update_offset(self) -> int:
        """Return next update_id beyond any old queued Telegram updates."""
        if not self.enabled:
            return 0
        url = f"https://api.telegram.org/bot{self.cfg.bot_token}/getUpdates?offset=-1&timeout=0"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            results = data.get("result") or []
            if results:
                return int(results[-1]["update_id"]) + 1
        except Exception as exc:
            logger.debug("Telegram get_latest_update_offset failed: %s", exc)
        return 0

    def poll_updates(self, offset: int = 0) -> Tuple[List[TelegramUserAction], int]:
        """Fetch incoming Telegram replies and inline button clicks from authorized chat_id."""
        if not self.enabled:
            return [], offset
        query = urllib.parse.urlencode({"offset": offset, "timeout": 0, "limit": 50})
        url = f"https://api.telegram.org/bot{self.cfg.bot_token}/getUpdates?{query}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("Telegram poll_updates failed: %s", exc)
            return [], offset

        if not data.get("ok"):
            return [], offset

        actions: List[TelegramUserAction] = []
        next_offset = offset
        authorized_chat = str(self.cfg.chat_id)

        for upd in data.get("result", []):
            next_offset = max(next_offset, int(upd["update_id"]) + 1)

            cb = upd.get("callback_query")
            if cb:
                cb_msg = cb.get("message") or {}
                chat_id = str((cb_msg.get("chat") or {}).get("id", ""))
                if chat_id != authorized_chat:
                    continue
                raw_data = str(cb.get("data", ""))
                if ":" in raw_data:
                    act, _, target_id = raw_data.partition(":")
                    if act in ("enter", "skip", "close"):
                        actions.append(
                            TelegramUserAction(
                                action=act,
                                target_id=target_id,
                                reply_to_message_id=cb_msg.get("message_id"),
                                callback_query_id=cb.get("id"),
                            )
                        )
                continue

            msg = upd.get("message")
            if msg:
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if chat_id != authorized_chat:
                    continue
                parsed = parse_telegram_text_command(msg.get("text", ""))
                if parsed is not None:
                    act, custom_price = parsed
                    reply_to = (msg.get("reply_to_message") or {}).get("message_id")
                    actions.append(
                        TelegramUserAction(
                            action=act,
                            target_id="",
                            reply_to_message_id=reply_to,
                            custom_price=custom_price,
                        )
                    )

        return actions, next_offset

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
        confirm_id: Optional[str] = None,
    ) -> Optional[int]:
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
        markup = None
        if confirm_id:
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "✅", "callback_data": f"enter:{confirm_id}"},
                    ]
                ]
            }
        return self.send_message_with_id(msg, reply_markup=markup)

    def notify_tracking_confirmed(
        self,
        side: str,
        entry_price: float,
        position_id: str,
        mode_label: str = "",
    ) -> Optional[int]:
        mode_tag = f" [{html.escape(mode_label)}]" if mode_label else ""
        msg = (
            f"<b>📌 ĐANG THEO DÕI — XAUUSD {side}{mode_tag}</b>\n"
            f"• Giá vào: <code>{entry_price:.2f}</code> | Mã: <code>{html.escape(position_id)}</code>\n"
            f"• Bot sẽ báo nếu cần dời SL hoặc đóng lệnh."
        )
        markup = {
            "inline_keyboard": [
                [{"text": "🔒", "callback_data": f"close:{position_id}"}]
            ]
        }
        return self.send_message_with_id(msg, reply_markup=markup)

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

    def notify_close_warning(
        self,
        side: str,
        entry_price: float,
        current_price: float,
        unrealized_pnl: float,
        reason: str,
        age_minutes: int,
        mode_label: str = "",
    ) -> bool:
        mode_tag = f" [{html.escape(mode_label)}]" if mode_label else ""
        msg = (
            f"<b>⚠️ NÊN ĐÓNG LỆNH — XAUUSD {side}{mode_tag}</b>\n"
            f"• Vào: <code>{entry_price:.2f}</code> → Hiện tại: <code>{current_price:.2f}</code> (~<b>{unrealized_pnl:+.2f}$</b>)\n"
            f"• Lý do: {html.escape(reason)}"
        )
        return self.send_message(msg)

    def notify_move_sl_advice(
        self,
        side: str,
        action: str,
        entry_price: float,
        current_price: float,
        new_sl: float,
        unrealized_pnl: float,
        reason: str,
        mode_label: str = "",
    ) -> bool:
        mode_tag = f" [{html.escape(mode_label)}]" if mode_label else ""
        title = "🎯 NÊN DỜI SL KHOÁ LỜI" if action == "lock_profit" else "🛡 NÊN DỜI SL HOÀ VỐN"
        msg = (
            f"<b>{title} — XAUUSD {side}{mode_tag}</b>\n"
            f"• SL mới: <code>{new_sl:.2f}</code> (hiện tại: <code>{current_price:.2f}</code> | ~<b>{unrealized_pnl:+.2f}$</b>)\n"
            f"• Lý do: {html.escape(reason)}"
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
        time_tag = f" ({html.escape(str(extra['time_label']))} VN)" if extra and extra.get("time_label") else ""
        msg = (
            f"<b>💓 XAUUSD BOT HEARTBEAT{time_tag}</b>\n"
            f"• Sàn: <code>{html.escape(provider.upper())}</code> | Giá: <b>{last_price:.2f}</b> (Spread ${spread_usd:.2f})\n"
            f"• Số dư: <b>${balance:,.2f}</b> | Lệnh đang mở: <b>{open_positions}</b>"
        )
        if extra and extra.get("next_news"):
            msg += f"\n• Tin USD gần nhất: {html.escape(str(extra['next_news']))}"
        return self.send_message(msg)
