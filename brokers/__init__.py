"""Broker factory and exports."""

from config import BrokerConfig
from brokers.base import AccountSummary, BrokerClient, OrderResult, Position, Quote
from brokers.capital import CapitalBroker
from brokers.oanda import OandaBroker
from brokers.paper import PaperBroker


def create_broker(cfg: BrokerConfig) -> BrokerClient:
    provider = cfg.provider.lower()
    if provider == "oanda":
        return OandaBroker(cfg)
    if provider == "capital":
        return CapitalBroker(cfg)
    if provider == "paper":
        feed_client: BrokerClient = None
        if cfg.capital_api_key and cfg.capital_identifier and cfg.capital_password:
            feed_client = CapitalBroker(cfg)
        elif cfg.oanda_api_token and cfg.oanda_account_id:
            feed_client = OandaBroker(cfg)
        return PaperBroker(cfg, feed_client=feed_client)
    raise ValueError(f"Unsupported broker provider: {cfg.provider}")


__all__ = [
    "AccountSummary",
    "BrokerClient",
    "CapitalBroker",
    "OandaBroker",
    "OrderResult",
    "PaperBroker",
    "Position",
    "Quote",
    "create_broker",
]
