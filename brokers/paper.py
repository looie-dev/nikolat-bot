"""Local Paper Trading broker adapter using live market data or injected feeds."""

import json
import logging
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional
import urllib.request

from brokers.base import (
    AccountSummary,
    BrokerClient,
    ClosedTradeInfo,
    OrderResult,
    Position,
    Quote,
)
from config import BrokerConfig
from risk_manager import InstrumentSpec

logger = logging.getLogger(__name__)

_YF_INTERVAL = {
    "M5": ("5m", "5d"),
    "M15": ("15m", "1mo"),
    "M30": ("30m", "1mo"),
    "H1": ("60m", "3mo"),
    "D1": ("1d", "2y"),
}

_TF_SECONDS = {
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


class PaperBroker(BrokerClient):
    """
    Simulated demo account persisted to JSON with no broker margin restrictions.
    Uses a live Spot XAUUSD feed (e.g. Capital.com/OANDA read-only) when provided,
    or falls back to Yahoo Finance Gold (GC=F).
    """

    def __init__(
        self,
        cfg: BrokerConfig,
        candle_fetcher: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None,
        feed_client: Optional[BrokerClient] = None,
    ):
        self.cfg = cfg
        self._state_path = Path(cfg.paper_state_file)
        self._spec = InstrumentSpec(
            contract_size=float(cfg.paper_contract_size or 1.0),
            min_size=0.01,
            size_step=0.01,
            precision=2,
        )
        self._candle_fetcher = candle_fetcher
        self._feed_client = feed_client
        self._state = self._load_state()
        self._last_quote: Optional[Quote] = None

    @property
    def symbol(self) -> str:
        if self._feed_client is not None:
            return f"XAUUSD-PAPER ({self._feed_client.symbol})"
        return "XAUUSD-PAPER (GC=F)"

    @property
    def instrument_spec(self) -> InstrumentSpec:
        return self._spec

    def _load_state(self) -> Dict[str, Any]:
        if self._state_path.exists():
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        return {
            "balance": float(self.cfg.paper_initial_balance),
            "next_id": 1,
            "open_positions": [],
            "closed_trades": {},
        }

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    def _fetch_yahoo_candles(self, timeframe: str, count: int) -> List[Dict[str, Any]]:
        interval, rng = _YF_INTERVAL.get(timeframe.upper(), ("15m", "1mo"))
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval={interval}&range={rng}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        candles: List[Dict[str, Any]] = []
        for idx, ts in enumerate(timestamps):
            o, h, l, c = quote["open"][idx], quote["high"][idx], quote["low"][idx], quote["close"][idx]
            if None in (o, h, l, c):
                continue
            candles.append(
                {
                    "time": int(ts),
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                    "volume": float(quote["volume"][idx] or 0),
                }
            )
        closed = candles[:-1] if len(candles) > 1 else candles
        return closed[-count:]

    def _evaluate_position_exit(
        self,
        pos: Dict[str, Any],
        bid: float,
        ask: float,
        recent_candles: List[Dict[str, Any]],
        tf_sec: int,
    ):
        side = pos["side"]
        sl = pos.get("stop_loss")
        tp = pos.get("take_profit")
        opened_at = int(pos.get("opened_at", 0))

        # 1. Check closed candle wicks strictly after the position was opened (conservative SL-first)
        for c in recent_candles:
            if int(c["time"]) + tf_sec <= opened_at:
                continue
            high = float(c["high"])
            low = float(c["low"])
            if side == "BUY":
                if sl is not None and low <= sl:
                    return float(sl), "Stop Loss (wick)"
                if tp is not None and high >= tp:
                    return float(tp), "Take Profit (wick)"
            else:
                if sl is not None and high >= sl:
                    return float(sl), "Stop Loss (wick)"
                if tp is not None and low <= tp:
                    return float(tp), "Take Profit (wick)"

        # 2. Check live bid/ask quote
        if side == "BUY":
            if sl is not None and bid <= sl:
                return float(sl), "Stop Loss"
            if tp is not None and bid >= tp:
                return float(tp), "Take Profit"
        else:
            if sl is not None and ask >= sl:
                return float(sl), "Stop Loss"
            if tp is not None and ask <= tp:
                return float(tp), "Take Profit"

        return None

    def _check_sl_tp(
        self,
        bid: float,
        ask: float,
        recent_candles: Optional[List[Dict[str, Any]]] = None,
        tf_sec: int = 300,
    ) -> None:
        remaining = []
        changed = False
        bars = recent_candles or []
        for pos in self._state["open_positions"]:
            entry = float(pos["entry_price"])
            size = float(pos["size"])
            hit = self._evaluate_position_exit(pos, bid, ask, bars, tf_sec)
            if hit is not None:
                exit_price, reason = hit
                cs = self._spec.contract_size
                pnl = (
                    (exit_price - entry) * size * cs
                    if pos["side"] == "BUY"
                    else (entry - exit_price) * size * cs
                )
                self._state["balance"] = round(self._state["balance"] + pnl, 2)
                self._state["closed_trades"][pos["position_id"]] = {
                    "position_id": pos["position_id"],
                    "close_price": round(exit_price, 2),
                    "realized_pnl": round(pnl, 2),
                    "reason": reason,
                }
                changed = True
            else:
                remaining.append(pos)
        if changed:
            self._state["open_positions"] = remaining
            self._save_state()

    def get_candles(self, timeframe: str, count: int = 300) -> List[Dict[str, Any]]:
        candles: List[Dict[str, Any]] = []
        if self._candle_fetcher is not None:
            candles = self._candle_fetcher(timeframe, count)
        elif self._feed_client is not None:
            try:
                candles = self._feed_client.get_candles(timeframe, count)
            except Exception as exc:
                logger.warning("Paper feed_client get_candles failed, falling back to Yahoo: %s", exc)
                candles = self._fetch_yahoo_candles(timeframe, count)
        else:
            candles = self._fetch_yahoo_candles(timeframe, count)

        quote_updated = False
        if self._feed_client is not None:
            try:
                self._last_quote = self._feed_client.get_quote()
                quote_updated = True
            except Exception as exc:
                logger.debug("Paper feed_client get_quote failed: %s", exc)

        if not quote_updated and candles:
            last_close = float(candles[-1]["close"])
            self._last_quote = Quote(
                bid=round(last_close - 0.15, 2),
                ask=round(last_close + 0.15, 2),
                mid=round(last_close, 2),
                spread=0.30,
                timestamp_sec=int(time.time()),
            )

        if self._last_quote is not None:
            tf_sec = _TF_SECONDS.get(timeframe.upper(), 300)
            self._check_sl_tp(self._last_quote.bid, self._last_quote.ask, candles[-5:], tf_sec)
        return candles

    def get_quote(self) -> Quote:
        if self._feed_client is not None:
            try:
                self._last_quote = self._feed_client.get_quote()
                self._check_sl_tp(self._last_quote.bid, self._last_quote.ask)
                return self._last_quote
            except Exception as exc:
                logger.debug("Paper feed_client get_quote failed: %s", exc)
        if self._last_quote is None:
            self.get_candles("M15", 5)
        if self._last_quote is None:
            raise RuntimeError("PaperBroker could not obtain a market quote")
        return self._last_quote

    def get_account_summary(self) -> AccountSummary:
        open_pos = self.get_open_positions()
        unrealized = sum(p.unrealized_pnl for p in open_pos)
        bal = float(self._state["balance"])
        return AccountSummary(
            balance=bal,
            equity=round(bal + unrealized, 2),
            currency="USD",
            open_positions_count=len(open_pos),
        )

    def get_open_positions(self) -> List[Position]:
        mid = self._last_quote.mid if self._last_quote else None
        out: List[Position] = []
        for pos in self._state["open_positions"]:
            entry = float(pos["entry_price"])
            size = float(pos["size"])
            upl = 0.0
            if mid is not None:
                cs = self._spec.contract_size
                upl = (mid - entry) * size * cs if pos["side"] == "BUY" else (entry - mid) * size * cs
            out.append(
                Position(
                    position_id=pos["position_id"],
                    side=pos["side"],
                    size=size,
                    entry_price=entry,
                    stop_loss=pos.get("stop_loss"),
                    take_profit=pos.get("take_profit"),
                    unrealized_pnl=round(upl, 2),
                )
            )
        return out

    def place_market_order(
        self,
        side: str,
        size: float,
        stop_loss: float,
        take_profit: float,
    ) -> OrderResult:
        quote = self.get_quote()
        fill = quote.ask if side.upper() == "BUY" else quote.bid
        pos_id = f"PAPER-{self._state['next_id']}"
        self._state["next_id"] += 1
        self._state["open_positions"].append(
            {
                "position_id": pos_id,
                "side": side.upper(),
                "size": float(size),
                "entry_price": round(fill, 2),
                "stop_loss": round(stop_loss, 2),
                "take_profit": round(take_profit, 2),
                "opened_at": int(time.time()),
            }
        )
        self._save_state()
        return OrderResult(True, pos_id, round(fill, 2), float(size), "Paper order filled")

    def update_stop_loss(self, position_id: str, new_stop_loss: float) -> bool:
        for pos in self._state["open_positions"]:
            if pos["position_id"] == position_id:
                pos["stop_loss"] = round(new_stop_loss, 2)
                self._save_state()
                return True
        return False

    def get_closed_trade_info(self, position_id: str) -> Optional[ClosedTradeInfo]:
        info = self._state["closed_trades"].get(position_id)
        if not info:
            return None
        return ClosedTradeInfo(
            position_id=info["position_id"],
            close_price=float(info["close_price"]),
            realized_pnl=float(info["realized_pnl"]),
            reason=str(info["reason"]),
        )
