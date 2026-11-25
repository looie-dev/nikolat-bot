"""OANDA v20 REST API broker adapter (Practice & Live)."""

from datetime import datetime
import json
import logging
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
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

_TIMEFRAME_MAP = {
    "M5": "M5",
    "M15": "M15",
    "M30": "M30",
    "H1": "H1",
    "H4": "H4",
    "D1": "D",
}


class OandaBroker(BrokerClient):
    """Direct OANDA v20 REST client supporting XAU_USD on macOS/Linux/Windows."""

    def __init__(self, cfg: BrokerConfig):
        if not cfg.oanda_api_token or not cfg.oanda_account_id:
            raise ValueError("OANDA_API_TOKEN và OANDA_ACCOUNT_ID là bắt buộc khi dùng BROKER_PROVIDER=oanda")
        self.cfg = cfg
        self._instrument = cfg.oanda_instrument or "XAU_USD"
        self._account_id = cfg.oanda_account_id
        self._base_url = (
            "https://api-fxtrade.oanda.com"
            if cfg.environment == "live"
            else "https://api-fxpractice.oanda.com"
        )
        # 1 unit of XAU_USD on OANDA = 1 troy ounce ($1 move = $1 PnL)
        self._spec = InstrumentSpec(contract_size=1.0, min_size=1.0, size_step=1.0, precision=0)

    @property
    def symbol(self) -> str:
        return self._instrument

    @property
    def instrument_spec(self) -> InstrumentSpec:
        return self._spec

    def _request(
        self, method: str, path: str, body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.cfg.oanda_api_token}",
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "RFC3339",
        }
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OANDA HTTP {exc.code} on {path}: {err_body}") from exc

    def get_account_summary(self) -> AccountSummary:
        data = self._request("GET", f"/v3/accounts/{self._account_id}/summary")
        acc = data["account"]
        return AccountSummary(
            balance=float(acc["balance"]),
            equity=float(acc.get("NAV", acc["balance"])),
            currency=str(acc.get("currency", "USD")),
            open_positions_count=int(acc.get("openTradeCount", 0)),
        )

    def get_candles(self, timeframe: str, count: int = 300) -> List[Dict[str, Any]]:
        granularity = _TIMEFRAME_MAP.get(timeframe.upper())
        if not granularity:
            raise ValueError(f"Unsupported timeframe for OANDA: {timeframe}")
        query = urllib.parse.urlencode(
            {"granularity": granularity, "count": min(count + 2, 5000), "price": "M"}
        )
        data = self._request("GET", f"/v3/instruments/{self._instrument}/candles?{query}")
        candles: List[Dict[str, Any]] = []
        for item in data.get("candles", []):
            if not item.get("complete", False):
                continue
            mid = item["mid"]
            ts = int(datetime.fromisoformat(item["time"].replace("Z", "+00:00")).timestamp())
            candles.append(
                {
                    "time": ts,
                    "open": float(mid["o"]),
                    "high": float(mid["h"]),
                    "low": float(mid["l"]),
                    "close": float(mid["c"]),
                    "volume": float(item.get("volume", 0)),
                }
            )
        return candles[-count:]

    def get_quote(self) -> Quote:
        query = urllib.parse.urlencode({"instruments": self._instrument})
        data = self._request("GET", f"/v3/accounts/{self._account_id}/pricing?{query}")
        prices = data.get("prices", [])
        if not prices:
            raise RuntimeError(f"OANDA pricing empty for {self._instrument}")
        p = prices[0]
        bid = float(p["bids"][0]["price"])
        ask = float(p["asks"][0]["price"])
        mid = round((bid + ask) / 2.0, 3)
        spread = round(ask - bid, 3)
        ts = int(time.time())
        return Quote(bid=bid, ask=ask, mid=mid, spread=spread, timestamp_sec=ts)

    def get_open_positions(self) -> List[Position]:
        data = self._request("GET", f"/v3/accounts/{self._account_id}/openTrades")
        out: List[Position] = []
        for tr in data.get("trades", []):
            if tr.get("instrument") != self._instrument:
                continue
            units = float(tr["currentUnits"])
            side = "BUY" if units > 0 else "SELL"
            sl_obj = tr.get("stopLossOrder")
            tp_obj = tr.get("takeProfitOrder")
            out.append(
                Position(
                    position_id=str(tr["id"]),
                    side=side,
                    size=abs(units),
                    entry_price=float(tr["price"]),
                    stop_loss=float(sl_obj["price"]) if sl_obj and "price" in sl_obj else None,
                    take_profit=float(tp_obj["price"]) if tp_obj and "price" in tp_obj else None,
                    unrealized_pnl=float(tr.get("unrealizedPL", 0.0)),
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
        units_int = int(round(size))
        if units_int <= 0:
            return OrderResult(False, "", 0.0, 0.0, "Invalid OANDA units <= 0")
        signed_units = units_int if side.upper() == "BUY" else -units_int
        payload = {
            "order": {
                "type": "MARKET",
                "instrument": self._instrument,
                "units": str(signed_units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
                "stopLossOnFill": {"price": f"{stop_loss:.2f}", "timeInForce": "GTC"},
                "takeProfitOnFill": {"price": f"{take_profit:.2f}", "timeInForce": "GTC"},
            }
        }
        try:
            resp = self._request("POST", f"/v3/accounts/{self._account_id}/orders", body=payload)
            fill = resp.get("orderFillTransaction")
            if fill:
                trade_opened = fill.get("tradeOpened") or {}
                trade_id = str(trade_opened.get("tradeID") or fill.get("id", ""))
                return OrderResult(
                    success=True,
                    order_id=trade_id,
                    fill_price=float(fill.get("price", 0.0)),
                    size=float(units_int),
                    message="Filled",
                )
            cancel = resp.get("orderCancelTransaction") or {}
            reason = cancel.get("reason") or json.dumps(resp)
            return OrderResult(False, "", 0.0, 0.0, f"Order not filled: {reason}")
        except Exception as exc:
            return OrderResult(False, "", 0.0, 0.0, str(exc))

    def update_stop_loss(self, position_id: str, new_stop_loss: float) -> bool:
        payload = {"stopLoss": {"price": f"{new_stop_loss:.2f}", "timeInForce": "GTC"}}
        try:
            self._request(
                "PUT",
                f"/v3/accounts/{self._account_id}/trades/{position_id}/orders",
                body=payload,
            )
            return True
        except Exception as exc:
            logger.warning("Failed to update SL on OANDA trade %s: %s", position_id, exc)
            return False

    def get_closed_trade_info(self, position_id: str) -> Optional[ClosedTradeInfo]:
        try:
            data = self._request("GET", f"/v3/accounts/{self._account_id}/trades/{position_id}")
            tr = data.get("trade", {})
            if tr.get("state") != "CLOSED":
                return None
            return ClosedTradeInfo(
                position_id=position_id,
                close_price=float(tr.get("averageClosePrice", tr.get("price", 0.0))),
                realized_pnl=float(tr.get("realizedPL", 0.0)),
                reason="SL/TP hoặc đóng lệnh trên OANDA",
            )
        except Exception as exc:
            logger.warning("Failed to fetch closed trade %s: %s", position_id, exc)
            return None
