"""Capital.com REST API broker adapter (Demo & Live)."""

from datetime import datetime, timezone
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
    "M5": "MINUTE_5",
    "M15": "MINUTE_15",
    "M30": "MINUTE_30",
    "H1": "HOUR",
    "H4": "HOUR_4",
    "D1": "DAY",
}


class CapitalBroker(BrokerClient):
    """Direct Capital.com REST client with automatic session renewal."""

    def __init__(self, cfg: BrokerConfig):
        if not (cfg.capital_api_key and cfg.capital_identifier and cfg.capital_password):
            raise ValueError(
                "CAPITAL_API_KEY, CAPITAL_IDENTIFIER, CAPITAL_PASSWORD là bắt buộc khi dùng BROKER_PROVIDER=capital"
            )
        self.cfg = cfg
        self._epic = cfg.capital_epic or "GOLD"
        self._base_url = (
            "https://api-capital.backend-capital.com"
            if cfg.environment == "live"
            else "https://demo-api-capital.backend-capital.com"
        )
        self._cst: Optional[str] = None
        self._sec_token: Optional[str] = None
        self._spec = InstrumentSpec(contract_size=1.0, min_size=0.01, size_step=0.01, precision=2)

    @property
    def symbol(self) -> str:
        return self._epic

    @property
    def instrument_spec(self) -> InstrumentSpec:
        return self._spec

    def _login(self) -> None:
        url = f"{self._base_url}/api/v1/session"
        headers = {
            "X-CAP-API-KEY": self.cfg.capital_api_key,
            "Content-Type": "application/json",
        }
        payload = json.dumps(
            {
                "identifier": self.cfg.capital_identifier,
                "password": self.cfg.capital_password,
            }
        ).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                self._cst = resp.headers.get("CST")
                self._sec_token = resp.headers.get("X-SECURITY-TOKEN")
                if not (self._cst and self._sec_token):
                    raise RuntimeError("Capital.com session headers CST/X-SECURITY-TOKEN missing")
                logger.info("Authenticated Capital.com %s session", self.cfg.environment)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Capital.com login failed ({exc.code}): {err_body}") from exc

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        retry_auth: bool = True,
    ) -> Dict[str, Any]:
        if not self._cst or not self._sec_token:
            self._login()

        url = f"{self._base_url}{path}"
        headers = {
            "X-CAP-API-KEY": self.cfg.capital_api_key,
            "CST": self._cst or "",
            "X-SECURITY-TOKEN": self._sec_token or "",
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and retry_auth:
                self._cst = None
                self._sec_token = None
                self._login()
                return self._request(method, path, body=body, retry_auth=False)
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Capital.com HTTP {exc.code} on {path}: {err_body}") from exc

    def get_account_summary(self) -> AccountSummary:
        data = self._request("GET", "/api/v1/accounts")
        accounts = data.get("accounts", [])
        if not accounts:
            raise RuntimeError("No Capital.com accounts returned")
        acc = next((a for a in accounts if a.get("preferred")), accounts[0])
        bal = acc.get("balance", {})
        positions = self.get_open_positions()
        return AccountSummary(
            balance=float(bal.get("deposit", bal.get("balance", 0.0))),
            equity=float(bal.get("balance", 0.0)) + float(bal.get("profitLoss", 0.0)),
            currency=str(acc.get("currency", "USD")),
            open_positions_count=len(positions),
        )

    def get_candles(self, timeframe: str, count: int = 300) -> List[Dict[str, Any]]:
        resolution = _TIMEFRAME_MAP.get(timeframe.upper())
        if not resolution:
            raise ValueError(f"Unsupported timeframe for Capital.com: {timeframe}")
        query = urllib.parse.urlencode({"resolution": resolution, "max": min(count + 2, 1000)})
        data = self._request("GET", f"/api/v1/prices/{self._epic}?{query}")
        raw_prices = data.get("prices", [])
        candles: List[Dict[str, Any]] = []
        for item in raw_prices:
            ts_str = item.get("snapshotTimeUTC") or item.get("snapshotTime")
            if not ts_str:
                continue
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            def _mid(field: str) -> float:
                obj = item[field]
                return (float(obj["bid"]) + float(obj["ask"])) / 2.0

            candles.append(
                {
                    "time": int(dt.timestamp()),
                    "open": _mid("openPrice"),
                    "high": _mid("highPrice"),
                    "low": _mid("lowPrice"),
                    "close": _mid("closePrice"),
                    "volume": float(item.get("lastTradedVolume", 0)),
                }
            )
        # Drop the most recent candle which may still be forming
        closed = candles[:-1] if len(candles) > 1 else candles
        return closed[-count:]

    def get_quote(self) -> Quote:
        data = self._request("GET", f"/api/v1/markets/{self._epic}")
        snap = data.get("snapshot", {})
        bid = float(snap["bid"])
        ask = float(snap["offer"])
        rules = data.get("dealingRules", {})
        min_deal = float(rules.get("minDealSize", {}).get("value", 0.1))
        step = float(rules.get("minSizeIncrement", {}).get("value", min_deal))
        self._spec = InstrumentSpec(
            contract_size=1.0,
            min_size=min_deal,
            size_step=step if step > 0 else min_deal,
            precision=2,
        )
        return Quote(
            bid=bid,
            ask=ask,
            mid=round((bid + ask) / 2.0, 3),
            spread=round(ask - bid, 3),
            timestamp_sec=int(time.time()),
        )

    def get_open_positions(self) -> List[Position]:
        data = self._request("GET", "/api/v1/positions")
        out: List[Position] = []
        for item in data.get("positions", []):
            market = item.get("market", {})
            pos = item.get("position", {})
            if market.get("epic") != self._epic:
                continue
            out.append(
                Position(
                    position_id=str(pos["dealId"]),
                    side=str(pos["direction"]).upper(),
                    size=float(pos["size"]),
                    entry_price=float(pos["level"]),
                    stop_loss=float(pos["stopLevel"]) if pos.get("stopLevel") is not None else None,
                    take_profit=float(pos["profitLevel"]) if pos.get("profitLevel") is not None else None,
                    unrealized_pnl=float(pos.get("upl", 0.0)),
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
        payload = {
            "epic": self._epic,
            "direction": side.upper(),
            "size": round(size, 2),
            "stopLevel": round(stop_loss, 2),
            "profitLevel": round(take_profit, 2),
            "guaranteedStopLoss": False,
        }
        try:
            resp = self._request("POST", "/api/v1/positions", body=payload)
            deal_ref = resp.get("dealReference")
            if not deal_ref:
                return OrderResult(False, "", 0.0, 0.0, f"Missing dealReference: {resp}")
            confirm = self._request("GET", f"/api/v1/confirms/{deal_ref}")
            status = confirm.get("dealStatus")
            if status != "ACCEPTED":
                return OrderResult(
                    False,
                    "",
                    0.0,
                    0.0,
                    f"Deal rejected ({status}): {confirm.get('reason', confirm)}",
                )
            affected = confirm.get("affectedDeals") or []
            deal_id = str(affected[0]["dealId"]) if affected else str(confirm.get("dealId", deal_ref))
            return OrderResult(
                success=True,
                order_id=deal_id,
                fill_price=float(confirm.get("level", 0.0)),
                size=float(confirm.get("size", size)),
                message="Accepted",
            )
        except Exception as exc:
            return OrderResult(False, "", 0.0, 0.0, str(exc))

    def update_stop_loss(self, position_id: str, new_stop_loss: float) -> bool:
        try:
            self._request(
                "PUT",
                f"/api/v1/positions/{position_id}",
                body={"stopLevel": round(new_stop_loss, 2)},
            )
            return True
        except Exception as exc:
            logger.warning("Failed to update SL on Capital.com deal %s: %s", position_id, exc)
            return False

    def get_closed_trade_info(self, position_id: str) -> Optional[ClosedTradeInfo]:
        # Capital.com activity history can be queried if needed; return generic closure fallback
        try:
            data = self._request("GET", "/api/v1/history/activity?lastPeriod=3600")
            for act in data.get("activities", []):
                if str(act.get("dealId")) == str(position_id) and act.get("status") == "ACCEPTED":
                    details = act.get("details") or {}
                    if "profit" in details:
                        return ClosedTradeInfo(
                            position_id=position_id,
                            close_price=float(details.get("level", 0.0)),
                            realized_pnl=float(details.get("profit", 0.0)),
                            reason=str(act.get("type", "Closed")),
                        )
        except Exception as exc:
            logger.debug("Capital.com closed trade lookup failed: %s", exc)
        return None
