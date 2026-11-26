"""Unit tests for XAUUSD trading bot modules."""

import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import TradingBot
from brokers.capital import CapitalBroker
from brokers.oanda import OandaBroker
from brokers.paper import PaperBroker
from config import BotConfig, BrokerConfig, RiskConfig, StrategyConfig, StrategyProfile, TelegramConfig
from health_server import build_health_response, start_health_server
from indicators import adx_series, atr_series, ema_series, macd_series, rsi_series
from news_filter import NewsEvent, NewsFilter
from notifier import TelegramNotifier, TelegramUserAction, parse_telegram_text_command
from risk_manager import InstrumentSpec, RiskManager
from strategy import TradeSignal, TrendPullbackStrategy


def _make_candles(closes, start_ts=1700046000, step=3600, spread=1.0):
    """Build synthetic OHLCV candles from close prices (start_ts = 11:00 UTC)."""
    candles = []
    prev_c = closes[0]
    for i, c in enumerate(closes):
        o = prev_c
        h = max(o, c) + spread
        l = min(o, c) - spread
        candles.append(
            {
                "time": start_ts + i * step,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": 100.0,
            }
        )
        prev_c = c
    return candles


class TestIndicators(unittest.TestCase):
    def test_ema_and_rsi_and_atr(self):
        closes = [2000.0 + i * 0.5 for i in range(60)]
        ema20 = ema_series(closes, 20)
        self.assertEqual(len(ema20), len(closes))
        self.assertGreater(ema20[-1], ema20[0])

        rsi = rsi_series(closes, 14)
        self.assertEqual(len(rsi), len(closes))
        self.assertGreater(rsi[-1], 70.0)

        macd_l, sig_l, hist = macd_series(closes)
        self.assertEqual(len(macd_l), len(closes))
        self.assertEqual(len(sig_l), len(closes))
        self.assertEqual(len(hist), len(closes))

        candles = _make_candles(closes)
        atr = atr_series(candles, 14)
        adx = adx_series(candles, 14)
        self.assertGreater(atr[-1], 0.0)
        self.assertGreater(adx[-1], 0.0)


class TestStrategy(unittest.TestCase):
    @staticmethod
    def _build_bull_pullback_candles():
        closes = [2000.0 + i * 0.6 for i in range(230)]
        last = closes[-1]
        # Dip down to EMA50 (~15 below peak), then rebound strongly so MACD hist crosses > 0
        pullback = [last - 4, last - 9, last - 15, last - 20, last - 22, last - 19]
        rebound = [last - 12, last - 5, last + 2, last + 8]
        all_closes = closes + pullback + rebound
        end_ts = 1705320000  # 2024-01-15 12:00:00 UTC (inside 07:00-17:00 UTC)
        start_ts = end_ts - (len(all_closes) - 1) * 3600
        return _make_candles(all_closes, start_ts=start_ts, step=3600, spread=2.0)

    def test_bullish_pullback_signal_and_session_filter(self):
        candles = self._build_bull_pullback_candles()
        cfg = StrategyConfig(
            timeframe="H1",
            ema_fast=50,
            ema_slow=200,
            adx_min=0.0,
            pullback_lookback=6,
            pullback_atr_tolerance=0.6,
            rsi_buy_min=40.0,
            rsi_buy_max=72.0,
            sl_atr_mult=2.5,
            rr_ratio=2.5,
            use_session_filter=True,
        )
        strat = TrendPullbackStrategy(cfg)
        sig = strat.evaluate_latest(candles)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, "BUY")
        self.assertLess(sig.stop_loss, sig.entry_price)
        self.assertGreater(sig.take_profit, sig.entry_price)

        # Shift final candle outside session (22:00 UTC) -> must be filtered out
        candles[-1]["time"] = 1705356000  # 2024-01-15 22:00:00 UTC
        self.assertIsNone(strat.evaluate_latest(candles))

    def test_bearish_pullback_signal(self):
        closes = [2600.0 - i * 0.6 for i in range(230)]
        last = closes[-1]
        pullback = [last + 4, last + 9, last + 15, last + 20, last + 22, last + 19]
        rebound = [last + 12, last + 5, last - 2, last - 8]
        all_closes = closes + pullback + rebound
        end_ts = 1705320000
        start_ts = end_ts - (len(all_closes) - 1) * 3600
        candles = _make_candles(all_closes, start_ts=start_ts, step=3600, spread=2.0)
        cfg = StrategyConfig(
            timeframe="H1",
            ema_fast=50,
            ema_slow=200,
            adx_min=0.0,
            pullback_lookback=6,
            pullback_atr_tolerance=0.6,
            rsi_sell_min=28.0,
            rsi_sell_max=60.0,
            sl_atr_mult=2.5,
            rr_ratio=2.5,
            use_session_filter=True,
        )
        sig = TrendPullbackStrategy(cfg).evaluate_latest(candles)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, "SELL")
        self.assertGreater(sig.stop_loss, sig.entry_price)
        self.assertLess(sig.take_profit, sig.entry_price)

    def test_sl_tp_usd_clamping(self):
        candles = self._build_bull_pullback_candles()
        cfg = StrategyConfig(
            timeframe="H1",
            ema_fast=50,
            ema_slow=200,
            adx_min=0.0,
            pullback_lookback=6,
            pullback_atr_tolerance=0.6,
            rsi_buy_min=40.0,
            rsi_buy_max=72.0,
            sl_atr_mult=5.0,
            rr_ratio=2.0,
            min_sl_usd=8.0,
            max_sl_usd=15.0,
            max_tp_usd=25.0,
            use_session_filter=True,
        )
        sig = TrendPullbackStrategy(cfg).evaluate_latest(candles)
        self.assertIsNotNone(sig)
        self.assertAlmostEqual(sig.entry_price - sig.stop_loss, 15.0, places=2)
        self.assertAlmostEqual(sig.take_profit - sig.entry_price, 25.0, places=2)


class TestRiskAndNews(unittest.TestCase):
    def test_position_sizing_and_limits(self):
        rm = RiskManager(
            risk_percent=1.0,
            max_open_positions=1,
            max_daily_trades=2,
            max_daily_loss_percent=3.0,
            max_spread_usd=0.60,
        )
        spec = InstrumentSpec(contract_size=1.0, min_size=1.0, size_step=1.0, precision=0)
        # Balance 10000 -> 1% = $100 risk. SL distance = $10 -> 10 units.
        dec = rm.evaluate_order(10000.0, 2600.0, 2590.0, 0.30, 0, spec)
        self.assertTrue(dec.allowed)
        self.assertEqual(dec.size, 10.0)
        self.assertEqual(dec.risk_usd, 100.0)

        # Spread too wide
        bad_spread = rm.evaluate_order(10000.0, 2600.0, 2590.0, 0.85, 0, spec)
        self.assertFalse(bad_spread.allowed)

        # Max open positions
        max_pos = rm.evaluate_order(10000.0, 2600.0, 2590.0, 0.30, 1, spec)
        self.assertFalse(max_pos.allowed)

        # Daily loss limit
        rm.record_trade_closed(-350.0, 9650.0)
        loss_block = rm.evaluate_order(9650.0, 2600.0, 2590.0, 0.30, 0, spec)
        self.assertFalse(loss_block.allowed)

        # max_daily_trades=0 disables daily trade count cap
        rm_unlimited = RiskManager(
            risk_percent=1.0,
            max_open_positions=1,
            max_daily_trades=0,
            max_daily_loss_percent=3.0,
            max_spread_usd=0.60,
        )
        for _ in range(10):
            rm_unlimited.record_trade_opened(10000.0)
        self.assertTrue(rm_unlimited.evaluate_order(10000.0, 2600.0, 2590.0, 0.30, 0, spec).allowed)

    def test_fixed_size_override(self):
        rm = RiskManager(
            risk_percent=1.0,
            max_open_positions=2,
            max_daily_trades=4,
            max_daily_loss_percent=3.0,
            max_spread_usd=0.80,
            fixed_size=1.0,
        )
        spec = InstrumentSpec(contract_size=1.0, min_size=0.01, size_step=0.01, precision=2)
        dec = rm.evaluate_order(100.0, 3500.0, 3490.0, 0.50, 0, spec)
        self.assertTrue(dec.allowed)
        self.assertEqual(dec.size, 1.0)
        self.assertEqual(dec.risk_usd, 10.0)

    def test_exness_contract_size_and_max_lot_cap(self):
        rm = RiskManager(
            risk_percent=10.0,
            max_open_positions=2,
            max_daily_trades=4,
            max_daily_loss_percent=25.0,
            max_spread_usd=0.80,
            max_size=0.10,
        )
        exness_spec = InstrumentSpec(contract_size=100.0, min_size=0.01, size_step=0.01, precision=2)
        # Balance $100, risk 10% = $10. SL=$5 -> $10 / (5 * 100) = 0.02 lot
        dec1 = rm.evaluate_order(100.0, 3500.0, 3495.0, 0.50, 0, exness_spec)
        self.assertTrue(dec1.allowed)
        self.assertEqual(dec1.size, 0.02)
        self.assertEqual(dec1.risk_usd, 10.0)

        # Balance $1000, SL=$5 -> raw=0.20 lot, capped at max_size=0.10 lot
        dec2 = rm.evaluate_order(1000.0, 3500.0, 3495.0, 0.50, 0, exness_spec)
        self.assertTrue(dec2.allowed)
        self.assertEqual(dec2.size, 0.10)

        # PaperBroker with contract_size=100.0 PnL calculation
        with tempfile.TemporaryDirectory() as tmpdir:
            b_cfg = BrokerConfig(
                provider="paper",
                paper_initial_balance=100.0,
                paper_contract_size=100.0,
                paper_state_file=str(Path(tmpdir) / "paper.json"),
            )
            prices = [3500.0] * 5
            broker = PaperBroker(b_cfg, candle_fetcher=lambda tf, cnt: _make_candles(prices))
            broker.get_candles("M5", 5)
            res = broker.place_market_order("BUY", 0.01, stop_loss=3490.0, take_profit=3515.0)
            self.assertTrue(res.success)
            prices.append(3516.0)
            broker.get_candles("M5", 5)
            closed = broker.get_closed_trade_info(res.order_id)
            self.assertIsNotNone(closed)
            # Entry fills at ask (3500.15), TP hits at 3515.00 -> 14.85 * 0.01 * 100 = 14.85
            self.assertAlmostEqual(closed.realized_pnl, 14.85, places=2)

    def test_news_filter_window(self):
        nf = NewsFilter(buffer_minutes=15)
        ev = NewsEvent(title="US CPI m/m", country="USD", impact="High", timestamp_sec=1700000000)
        nf.set_events_for_test([ev])

        blocked, found = nf.check_blocked(now_sec=1700000000 + 600)
        self.assertTrue(blocked)
        self.assertEqual(found.title, "US CPI m/m")

        blocked_far, _ = nf.check_blocked(now_sec=1700000000 + 1800)
        self.assertFalse(blocked_far)

    def test_telegram_buy_now_message_format(self):
        notifier = TelegramNotifier(TelegramConfig(bot_token="tok", chat_id="123"))
        sent_payloads = []
        notifier.send_message_with_id = lambda text, reply_markup=None: (sent_payloads.append((text, reply_markup)) or 999)
        sig = TradeSignal(
            side="BUY",
            entry_price=3500.0,
            stop_loss=3492.0,
            take_profit=3516.0,
            atr=8.0,
            rr_ratio=2.0,
            candle_time=1705320000,
            reason="Test",
            indicators={"ema50": 3495.0, "ema200": 3480.0, "rsi": 52.0, "adx": 22.0, "atr": 8.0},
        )
        msg_id = notifier.notify_order_opened(
            signal=sig,
            size=0.01,
            risk_usd=8.0,
            spread_usd=0.50,
            order_id="PAPER-1",
            balance=100.0,
            mode_label="M5_SCALP",
            confirm_id="M5_SCALP-1",
        )
        self.assertEqual(msg_id, 999)
        self.assertEqual(len(sent_payloads), 1)
        body, markup = sent_payloads[0]
        self.assertIn("BUY NOW", body)
        self.assertIn("M5_SCALP", body)
        self.assertIn("<code>3492.00</code>", body)
        self.assertIn("<code>3516.00</code>", body)
        self.assertIn("3498.00 – 3504.00", body)
        self.assertNotIn("Chỉ báo", body)
        self.assertNotIn("Nến (VN)", body)
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "✅")
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "enter:M5_SCALP-1")
        self.assertEqual(len(markup["inline_keyboard"][0]), 1)


class TestBrokersAndBotLoop(unittest.TestCase):
    def test_paper_broker_tp_and_sl_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = str(Path(tmpdir) / "paper.json")
            prices = [2500.0] * 10
            cfg = BrokerConfig(provider="paper", paper_initial_balance=10000.0, paper_state_file=state_file)
            broker = PaperBroker(cfg, candle_fetcher=lambda tf, cnt: _make_candles(prices))

            candles = broker.get_candles("H1", 10)
            self.assertEqual(len(candles), 10)
            res = broker.place_market_order("BUY", 2.0, stop_loss=2490.0, take_profit=2520.0)
            self.assertTrue(res.success)
            self.assertEqual(len(broker.get_open_positions()), 1)

            # Price jumps above TP -> triggers automatic TP close on next poll
            prices.append(2525.0)
            broker.get_candles("H1", 10)
            self.assertEqual(len(broker.get_open_positions()), 0)
            closed = broker.get_closed_trade_info(res.order_id)
            self.assertIsNotNone(closed)
            self.assertGreater(closed.realized_pnl, 0.0)
            self.assertGreater(broker.get_account_summary().balance, 10000.0)

    def test_paper_broker_triggers_sl_on_candle_wick(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = str(Path(tmpdir) / "paper.json")
            now_ts = int(__import__("time").time())
            candles = [
                {"time": now_ts - 300, "open": 3500.0, "high": 3502.0, "low": 3498.0, "close": 3500.0, "volume": 10.0}
            ]
            cfg = BrokerConfig(provider="paper", paper_initial_balance=300.0, paper_state_file=state_file)
            broker = PaperBroker(cfg, candle_fetcher=lambda tf, cnt: candles)
            broker.get_candles("M5", 5)
            res = broker.place_market_order("BUY", 1.0, stop_loss=3492.0, take_profit=3515.0)
            self.assertTrue(res.success)

            # New closed candle wicks down to 3491 (hits SL) then closes back at 3501
            candles.append(
                {"time": now_ts + 5, "open": 3500.0, "high": 3503.0, "low": 3491.0, "close": 3501.0, "volume": 10.0}
            )
            broker.get_candles("M5", 5)
            self.assertEqual(len(broker.get_open_positions()), 0)
            closed = broker.get_closed_trade_info(res.order_id)
            self.assertIsNotNone(closed)
            self.assertLess(closed.realized_pnl, 0.0)
            self.assertIn("Stop Loss", closed.reason)

    def test_oanda_order_payload(self):
        cfg = BrokerConfig(
            provider="oanda",
            environment="demo",
            oanda_api_token="tok",
            oanda_account_id="001-004-123",
        )
        oanda = OandaBroker(cfg)
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {
                "orderFillTransaction": {
                    "id": "101",
                    "price": "2650.25",
                    "tradeOpened": {"tradeID": "777"},
                }
            }
        ).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as m_open:
            res = oanda.place_market_order("SELL", 5.0, stop_loss=2665.50, take_profit=2620.00)
            self.assertTrue(res.success)
            self.assertEqual(res.order_id, "777")
            req_obj = m_open.call_args[0][0]
            sent = json.loads(req_obj.data.decode("utf-8"))
            self.assertEqual(sent["order"]["units"], "-5")
            self.assertEqual(sent["order"]["stopLossOnFill"]["price"], "2665.50")

    def test_capital_session_and_order_confirm(self):
        cfg = BrokerConfig(
            provider="capital",
            environment="demo",
            capital_api_key="key",
            capital_identifier="user@example.com",
            capital_password="secret",
            capital_epic="GOLD",
        )
        cap = CapitalBroker(cfg)

        login_resp = MagicMock()
        login_resp.headers = {"CST": "cst-token", "X-SECURITY-TOKEN": "sec-token"}
        login_resp.read.return_value = b"{}"
        login_resp.__enter__.return_value = login_resp

        pos_resp = MagicMock()
        pos_resp.read.return_value = json.dumps({"dealReference": "REF-1"}).encode("utf-8")
        pos_resp.__enter__.return_value = pos_resp

        conf_resp = MagicMock()
        conf_resp.read.return_value = json.dumps(
            {
                "dealStatus": "ACCEPTED",
                "level": 2655.5,
                "size": 0.2,
                "affectedDeals": [{"dealId": "DEAL-99"}],
            }
        ).encode("utf-8")
        conf_resp.__enter__.return_value = conf_resp

        with patch("urllib.request.urlopen", side_effect=[login_resp, pos_resp, conf_resp]):
            res = cap.place_market_order("BUY", 0.2, stop_loss=2640.0, take_profit=2690.0)
            self.assertTrue(res.success)
            self.assertEqual(res.order_id, "DEAL-99")
            self.assertEqual(res.fill_price, 2655.5)

    def test_bot_run_once_places_order_and_notifies_telegram(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candles = TestStrategy._build_bull_pullback_candles()

            b_cfg = BrokerConfig(
                provider="paper",
                paper_initial_balance=10000.0,
                paper_state_file=str(Path(tmpdir) / "paper.json"),
            )
            broker = PaperBroker(b_cfg, candle_fetcher=lambda tf, cnt: candles)
            notifier = MagicMock()
            nf = NewsFilter(buffer_minutes=15)
            nf.set_events_for_test([])

            bot_cfg = BotConfig(
                strategy=StrategyConfig(
                    timeframe="H1",
                    ema_fast=50,
                    ema_slow=200,
                    adx_min=0.0,
                    pullback_lookback=6,
                    pullback_atr_tolerance=0.6,
                    rsi_buy_min=40.0,
                    rsi_buy_max=72.0,
                    sl_atr_mult=2.5,
                    rr_ratio=2.5,
                ),
                risk=RiskConfig(risk_percent=1.0, max_spread_usd=1.0, cooldown_candles=0),
                broker=b_cfg,
                telegram=TelegramConfig(bot_token="x", chat_id="y"),
            )
            bot = TradingBot(bot_cfg, broker=broker, notifier=notifier, news_filter=nf)
            bot.initialize()
            sig = bot.run_once()
            self.assertIsNotNone(sig)
            notifier.notify_order_opened.assert_called_once()
            self.assertEqual(len(broker.get_open_positions()), 1)

            # Same candle again -> should NOT evaluate or place duplicate order
            self.assertIsNone(bot.run_once())
            self.assertEqual(len(broker.get_open_positions()), 1)

    def test_multi_mode_evaluates_both_and_blocks_opposite_direction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            h1_candles = TestStrategy._build_bull_pullback_candles()
            # Build bearish M15 pullback candles
            closes_sell = [2600.0 - i * 0.6 for i in range(230)]
            last_s = closes_sell[-1]
            all_sell = closes_sell + [last_s + 4, last_s + 9, last_s + 15, last_s + 20, last_s + 22, last_s + 19, last_s + 12, last_s + 5, last_s - 2, last_s - 8]
            m15_candles = _make_candles(all_sell, start_ts=1705320000 - (len(all_sell) - 1) * 900, step=900, spread=2.0)

            b_cfg = BrokerConfig(
                provider="paper",
                paper_initial_balance=10000.0,
                paper_state_file=str(Path(tmpdir) / "paper.json"),
            )
            broker = PaperBroker(
                b_cfg,
                candle_fetcher=lambda tf, cnt: h1_candles if tf == "H1" else m15_candles,
            )
            notifier = MagicMock()
            nf = NewsFilter(buffer_minutes=15)
            nf.set_events_for_test([])

            p_h1 = StrategyProfile(
                name="H1_SWING",
                strategy=StrategyConfig(
                    timeframe="H1", ema_fast=50, ema_slow=200, adx_min=0.0,
                    pullback_lookback=6, pullback_atr_tolerance=0.6,
                    rsi_buy_min=40.0, rsi_buy_max=72.0, sl_atr_mult=2.5, rr_ratio=2.5,
                ),
            )
            p_m15 = StrategyProfile(
                name="M15_ACTIVE",
                strategy=StrategyConfig(
                    timeframe="M15", ema_fast=50, ema_slow=200, adx_min=0.0,
                    pullback_lookback=6, pullback_atr_tolerance=0.6,
                    rsi_sell_min=28.0, rsi_sell_max=60.0, sl_atr_mult=2.5, rr_ratio=2.0,
                    use_session_filter=False,
                ),
            )
            bot_cfg = BotConfig(
                strategy=p_h1.strategy,
                risk=RiskConfig(
                    risk_percent=1.0, max_open_positions=2, max_spread_usd=1.0,
                    cooldown_candles=0, allow_opposite_positions=False,
                ),
                broker=b_cfg,
                telegram=TelegramConfig(bot_token="x", chat_id="y"),
                strategy_profiles=(p_h1, p_m15),
            )
            bot = TradingBot(bot_cfg, broker=broker, notifier=notifier, news_filter=nf)
            bot.initialize()
            bot.run_once()

            # H1_SWING opened BUY; M15_ACTIVE SELL was blocked by anti-netting guard
            self.assertEqual(len(broker.get_open_positions()), 1)
            self.assertEqual(broker.get_open_positions()[0].side, "BUY")
            notifier.notify_order_opened.assert_called_once()
            notifier.notify_signal_skipped.assert_called_once()

    def test_health_server_responds_ok(self):
        code, body = build_health_response("/health", lambda: {"symbol": "XAUUSD-PAPER", "open_positions": 0})
        self.assertEqual(code, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["symbol"], "XAUUSD-PAPER")

        code_404, _ = build_health_response("/unknown", lambda: {})
        self.assertEqual(code_404, 404)

        srv = start_health_server(0, lambda: {"symbol": "XAUUSD-PAPER"}, host="127.0.0.1")
        try:
            self.assertGreater(srv.server_address[1], 0)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_scheduled_heartbeat_hours_vn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            b_cfg = BrokerConfig(
                provider="paper",
                paper_initial_balance=100.0,
                paper_state_file=str(Path(tmpdir) / "paper.json"),
            )
            broker = PaperBroker(b_cfg, candle_fetcher=lambda tf, cnt: _make_candles([3500.0] * 5))
            notifier = MagicMock()
            nf = NewsFilter(buffer_minutes=15)
            nf.set_events_for_test([])

            bot_cfg = BotConfig(
                strategy=StrategyConfig(),
                risk=RiskConfig(),
                broker=b_cfg,
                telegram=TelegramConfig(
                    bot_token="x",
                    chat_id="y",
                    heartbeat_hours=(9, 12, 18, 21),
                    heartbeat_tz_offset_hours=7,
                ),
            )
            bot = TradingBot(bot_cfg, broker=broker, notifier=notifier, news_filter=nf)

            # 08:59 VN = 01:59 UTC -> should NOT send
            self.assertFalse(bot._maybe_send_heartbeat(3500.0, 0.50, datetime(2026, 11, 26, 1, 59, tzinfo=timezone.utc)))
            # 09:00 VN = 02:00 UTC -> should send once
            self.assertTrue(bot._maybe_send_heartbeat(3500.0, 0.50, datetime(2026, 11, 26, 2, 0, tzinfo=timezone.utc)))
            # 09:25 VN = 02:25 UTC -> same hour slot, must NOT send again
            self.assertFalse(bot._maybe_send_heartbeat(3500.0, 0.50, datetime(2026, 11, 26, 2, 25, tzinfo=timezone.utc)))
            # 12:00 VN (05:00 UTC), 18:00 VN (11:00 UTC), 21:00 VN (14:00 UTC) -> all send
            self.assertTrue(bot._maybe_send_heartbeat(3500.0, 0.50, datetime(2026, 11, 26, 5, 0, tzinfo=timezone.utc)))
            self.assertTrue(bot._maybe_send_heartbeat(3500.0, 0.50, datetime(2026, 11, 26, 11, 0, tzinfo=timezone.utc)))
            self.assertTrue(bot._maybe_send_heartbeat(3500.0, 0.50, datetime(2026, 11, 26, 14, 0, tzinfo=timezone.utc)))
            # 22:00 VN (15:00 UTC) -> should NOT send
            self.assertFalse(bot._maybe_send_heartbeat(3500.0, 0.50, datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)))
            self.assertEqual(notifier.notify_status.call_count, 4)

    def test_unlimited_positions_per_mode_and_early_exit_alert_after_10m(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bull_candles = TestStrategy._build_bull_pullback_candles()
            candles = list(bull_candles)

            b_cfg = BrokerConfig(
                provider="paper",
                paper_initial_balance=100.0,
                paper_contract_size=100.0,
                paper_state_file=str(Path(tmpdir) / "paper.json"),
            )
            broker = PaperBroker(b_cfg, candle_fetcher=lambda tf, cnt: candles)
            notifier = MagicMock()
            nf = NewsFilter(buffer_minutes=15)
            nf.set_events_for_test([])

            prof = StrategyProfile(
                name="M5_SCALP",
                strategy=StrategyConfig(
                    timeframe="M5", ema_fast=50, ema_slow=200, adx_min=0.0,
                    pullback_lookback=6, pullback_atr_tolerance=0.6,
                    rsi_buy_min=40.0, rsi_buy_max=72.0, sl_atr_mult=1.2, rr_ratio=1.7,
                    use_session_filter=True,
                ),
            )
            bot_cfg = BotConfig(
                strategy=prof.strategy,
                risk=RiskConfig(
                    risk_percent=10.0, max_open_positions=0, max_positions_per_mode=0,
                    max_spread_usd=1.0, cooldown_candles=0, exit_alert_min_minutes=0,
                ),
                broker=b_cfg,
                telegram=TelegramConfig(bot_token="x", chat_id="y"),
                strategy_profiles=(prof,),
            )
            bot = TradingBot(bot_cfg, broker=broker, notifier=notifier, news_filter=nf)
            bot.initialize()

            # Trade 1 opens
            sig1 = bot.run_once()
            self.assertIsNotNone(sig1)
            self.assertEqual(len(broker.get_open_positions()), 1)

            # Append another bullish signal sequence -> opens 2nd position when max_positions_per_mode=0
            candles2 = TestStrategy._build_bull_pullback_candles()
            candles2[-1]["time"] = candles[-1]["time"] + 300
            candles[:] = candles2
            sig2 = bot.run_once()
            self.assertIsNotNone(sig2)
            self.assertEqual(len(broker.get_open_positions()), 2)

            # Verify max_positions_per_mode=1 blocks a 2nd signal from the same mode
            bot.cfg = BotConfig(
                strategy=prof.strategy,
                risk=RiskConfig(
                    risk_percent=10.0, max_open_positions=2, max_positions_per_mode=1,
                    max_spread_usd=1.0, cooldown_candles=0, exit_alert_min_minutes=0,
                ),
                broker=b_cfg,
                telegram=TelegramConfig(bot_token="x", chat_id="y"),
                strategy_profiles=(prof,),
            )
            candles3 = TestStrategy._build_bull_pullback_candles()
            candles3[-1]["time"] = candles[-1]["time"] + 300
            candles[:] = candles3
            self.assertIsNone(bot.run_once())
            self.assertEqual(len(broker.get_open_positions()), 2)

            # Simulate price rallying >= 60% TP and then printing a stalling red candle above breakeven
            pos1 = broker.get_open_positions()[0]
            tp_dist = abs(pos1.take_profit - pos1.entry_price)
            push_t = candles[-1]["time"]
            for k in (1, 2):
                candles.append({
                    "time": push_t + k * 300,
                    "open": candles[-1]["close"],
                    "high": candles[-1]["close"] + tp_dist * 0.35,
                    "low": candles[-1]["close"] - 0.5,
                    "close": candles[-1]["close"] + tp_dist * 0.32,
                    "volume": 100.0,
                })
            # Mild 2-candle pullback staying well above entry + $0.50 so MACD hist genuinely shrinks
            for k in (3, 4):
                candles.append({
                    "time": push_t + k * 300,
                    "open": candles[-1]["close"],
                    "high": candles[-1]["close"] + 0.1,
                    "low": candles[-1]["close"] - 2.0,
                    "close": candles[-1]["close"] - 1.5,
                    "volume": 100.0,
                })
            bot._last_exit_check_candle_time = None
            bot._check_early_exits({"M5": candles}, candles[-1]["close"])
            self.assertEqual(notifier.notify_move_sl_advice.call_count, 2)

            # Now simulate deep reversal breaking EMA50, MACD < 0, RSI < 45 -> sends close warning
            rev_start = candles[-1]["time"] + 300
            for k in range(1, 16):
                candles.append({
                    "time": rev_start + k * 300,
                    "open": candles[-1]["close"],
                    "high": candles[-1]["close"] + 0.5,
                    "low": candles[-1]["close"] - 4.0,
                    "close": candles[-1]["close"] - 3.5,
                    "volume": 100.0,
                })

            bot._last_exit_check_candle_time = None
            bot._check_early_exits({"M5": candles}, candles[-1]["close"])
            self.assertEqual(notifier.notify_close_warning.call_count, 2)

    def test_manual_confirm_via_telegram_reply_and_button(self):
        self.assertEqual(parse_telegram_text_command("vào"), ("enter", None))
        self.assertEqual(parse_telegram_text_command("Vào 3501.25"), ("enter", 3501.25))
        self.assertEqual(parse_telegram_text_command("bỏ qua"), ("skip", None))
        self.assertEqual(parse_telegram_text_command("đóng 3510.5"), ("close", 3510.5))
        self.assertEqual(parse_telegram_text_command("/sl 3520.5"), ("movesl", 3520.5))
        self.assertEqual(parse_telegram_text_command("/status@xauusd_bot"), ("status", None))
        self.assertEqual(parse_telegram_text_command("/news"), ("news", None))
        self.assertEqual(parse_telegram_text_command("/pause"), ("pause", None))
        self.assertEqual(parse_telegram_text_command("/resume"), ("resume", None))

        with tempfile.TemporaryDirectory() as tmpdir:
            candles = list(TestStrategy._build_bull_pullback_candles())
            b_cfg = BrokerConfig(
                provider="paper",
                paper_initial_balance=100.0,
                paper_contract_size=100.0,
                paper_state_file=str(Path(tmpdir) / "paper.json"),
            )
            broker = PaperBroker(b_cfg, candle_fetcher=lambda tf, cnt: candles)
            notifier = MagicMock()
            notifier.get_latest_update_offset.return_value = 10
            notifier.notify_order_opened.return_value = 501
            notifier.notify_tracking_confirmed.return_value = 502
            notifier.poll_updates.return_value = ([], 10)
            nf = NewsFilter(buffer_minutes=15)
            nf.set_events_for_test([])

            prof = StrategyProfile(
                name="M5_SCALP",
                strategy=StrategyConfig(
                    timeframe="M5", ema_fast=50, ema_slow=200, adx_min=0.0,
                    pullback_lookback=6, pullback_atr_tolerance=0.6,
                    rsi_buy_min=40.0, rsi_buy_max=72.0, sl_atr_mult=1.2, rr_ratio=1.7,
                    use_session_filter=True,
                ),
            )
            bot_cfg = BotConfig(
                strategy=prof.strategy,
                risk=RiskConfig(
                    risk_percent=10.0, max_open_positions=2, max_positions_per_mode=1,
                    max_spread_usd=1.0, cooldown_candles=0,
                ),
                broker=b_cfg,
                telegram=TelegramConfig(bot_token="x", chat_id="y"),
                strategy_profiles=(prof,),
                manual_confirm_tracking=True,
            )
            bot = TradingBot(bot_cfg, broker=broker, notifier=notifier, news_filter=nf)
            bot.initialize()

            # Signal fires -> sent to Telegram with confirm buttons, NOT tracked yet
            sig = bot.run_once()
            self.assertIsNotNone(sig)
            orig_sl_dist = abs(sig.entry_price - sig.stop_loss)
            orig_tp_dist = abs(sig.take_profit - sig.entry_price)
            self.assertEqual(len(broker.get_open_positions()), 0)
            self.assertEqual(len(bot._pending_confirmations), 1)

            # User replies "vào 2146.00" to message 501 -> starts tracking with entry 2146.00 & re-anchored SL/TP
            notifier.poll_updates.return_value = (
                [TelegramUserAction(action="enter", reply_to_message_id=501, custom_price=2146.00)],
                11,
            )
            bot.run_once()
            positions = broker.get_open_positions()
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0].entry_price, 2146.00)
            self.assertAlmostEqual(2146.00 - positions[0].stop_loss, orig_sl_dist, places=2)
            self.assertAlmostEqual(positions[0].take_profit - 2146.00, orig_tp_dist, places=2)
            notifier.notify_tracking_confirmed.assert_called_once()

            # Advance market above BE before moving SL to 2146.50 via button / /sl command
            candles.append({
                "time": candles[-1]["time"] + 300,
                "open": 2146.00,
                "high": 2151.00,
                "low": 2146.00,
                "close": 2150.00,
                "volume": 100.0,
            })
            notifier.poll_updates.return_value = (
                [TelegramUserAction(action="movesl", target_id=positions[0].position_id, custom_price=2146.50, callback_query_id="cb1", reply_to_message_id=502)],
                12,
            )
            bot.run_once()
            self.assertEqual(broker.get_open_positions()[0].stop_loss, 2146.50)

            # User replies "đóng 2152.00" to confirmation message 502 -> closes tracked trade
            notifier.poll_updates.return_value = (
                [TelegramUserAction(action="close", reply_to_message_id=502, custom_price=2152.00)],
                13,
            )
            bot.run_once()
            self.assertEqual(len(broker.get_open_positions()), 0)
            notifier.notify_position_closed.assert_called_once()

    def test_htf_filter_blocks_counter_trend_and_allows_aligned(self):
        bull_ltf = TestStrategy._build_bull_pullback_candles()
        end_ts = bull_ltf[-1]["time"]
        bull_htf = _make_candles(
            [2000.0 + i * 1.0 for i in range(80)],
            start_ts=end_ts - 79 * 900,
            step=900,
            spread=2.0,
        )
        bear_htf = _make_candles(
            [2600.0 - i * 1.0 for i in range(80)],
            start_ts=end_ts - 79 * 900,
            step=900,
            spread=2.0,
        )
        cfg = StrategyConfig(
            timeframe="M5",
            ema_fast=50,
            ema_slow=200,
            adx_min=0.0,
            pullback_lookback=6,
            pullback_atr_tolerance=0.6,
            rsi_buy_min=40.0,
            rsi_buy_max=72.0,
            sl_atr_mult=1.2,
            rr_ratio=1.7,
            htf_timeframe="M15",
            htf_ema_fast=20,
            htf_ema_slow=50,
        )
        strat = TrendPullbackStrategy(cfg)
        self.assertIsNotNone(strat.evaluate_latest(bull_ltf, htf_candles=bull_htf))
        self.assertIsNone(strat.evaluate_latest(bull_ltf, htf_candles=bear_htf))

    def test_early_exit_uses_position_timeframe_not_m5(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            m15_candles = list(TestStrategy._build_bull_pullback_candles())
            # M5 candles turn sharply bearish while M15 stays bullish
            m5_bear = _make_candles(
                [m15_candles[-1]["close"] - i * 4.0 for i in range(70)],
                start_ts=m15_candles[-1]["time"] - 69 * 300,
                step=300,
                spread=1.0,
            )
            b_cfg = BrokerConfig(
                provider="paper",
                paper_initial_balance=100.0,
                paper_contract_size=100.0,
                paper_state_file=str(Path(tmpdir) / "paper.json"),
            )
            broker = PaperBroker(b_cfg, candle_fetcher=lambda tf, cnt: m15_candles if tf == "M15" else m5_bear)
            notifier = MagicMock()
            nf = NewsFilter(buffer_minutes=15)
            nf.set_events_for_test([])
            prof15 = StrategyProfile(
                name="M15_INTRADAY",
                strategy=StrategyConfig(timeframe="M15", ema_fast=20, ema_slow=50, sl_atr_mult=1.0, rr_ratio=2.0),
            )
            bot_cfg = BotConfig(
                strategy=prof15.strategy,
                risk=RiskConfig(max_spread_usd=1.0, cooldown_candles=0, exit_alert_min_minutes=0),
                broker=b_cfg,
                telegram=TelegramConfig(bot_token="x", chat_id="y"),
                strategy_profiles=(prof15,),
            )
            bot = TradingBot(bot_cfg, broker=broker, notifier=notifier, news_filter=nf)
            broker.get_candles("M15", 10)
            res = broker.place_market_order("BUY", 0.01, stop_loss=2100.0, take_profit=2200.0)
            bot._tracked_positions = {p.position_id: p for p in broker.get_open_positions()}
            bot._position_modes[res.order_id] = "M15_INTRADAY"
            bot._position_opened_at[res.order_id] = 1000
            bot._position_opened_candle[res.order_id] = m15_candles[-1]["time"]

            bot._check_early_exits({"M5": m5_bear, "M15": m15_candles}, m15_candles[-1]["close"])
            notifier.notify_close_warning.assert_not_called()

    def test_loss_streak_drawdown_guard_and_runtime_persistence(self):
        rm = RiskManager(
            risk_percent=10.0,
            max_open_positions=2,
            max_daily_trades=0,
            max_daily_loss_percent=50.0,
            max_spread_usd=0.80,
            max_size=0.10,
            loss_streak_reduce_after=2,
            loss_streak_risk_factor=0.5,
        )
        spec = InstrumentSpec(contract_size=100.0, min_size=0.01, size_step=0.01, precision=2)
        self.assertEqual(rm.evaluate_order(100.0, 3500.0, 3495.0, 0.40, 0, spec).size, 0.02)
        rm.record_trade_closed(-5.0, 95.0)
        self.assertEqual(rm.effective_risk_percent, 10.0)
        rm.record_trade_closed(-5.0, 100.0)
        self.assertEqual(rm.effective_risk_percent, 5.0)
        # Halved risk: $5 / (5 * 100) = 0.01 lot; and $66 balance with $9 SL still allows min_size=0.01
        self.assertEqual(rm.evaluate_order(100.0, 3500.0, 3495.0, 0.40, 0, spec).size, 0.01)
        dec_min_floor = rm.evaluate_order(66.0, 3500.0, 3491.0, 0.40, 0, spec)
        self.assertTrue(dec_min_floor.allowed)
        self.assertEqual(dec_min_floor.size, 0.01)
        rm.record_trade_closed(+10.0, 110.0)
        self.assertEqual(rm.consecutive_losses, 0)
        self.assertEqual(rm.effective_risk_percent, 10.0)

        # Runtime persistence + /pause + /status + /news + move SL button format
        tg = TelegramNotifier(TelegramConfig(bot_token="tok", chat_id="123"))
        sent = []
        tg.send_message_with_id = lambda text, reply_markup=None: (sent.append((text, reply_markup)) or 888)
        tg.notify_move_sl_advice("BUY", "move_sl_be", 3500.0, 3510.0, 3500.5, 10.0, "50% TP", "M5_SCALP", position_id="PAPER-1")
        self.assertEqual(sent[0][1]["inline_keyboard"][0][0]["callback_data"], "movesl:PAPER-1:3500.50")
        tg.notify_close_warning("BUY", 3500.0, 3496.0, -4.0, "Gãy EMA", 10, "M5_SCALP", position_id="PAPER-1")
        self.assertEqual(sent[1][1]["inline_keyboard"][0][0]["callback_data"], "close:PAPER-1")

        with tempfile.TemporaryDirectory() as tmpdir:
            candles = list(TestStrategy._build_bull_pullback_candles())
            b_cfg = BrokerConfig(
                provider="paper",
                paper_initial_balance=100.0,
                paper_contract_size=100.0,
                paper_state_file=str(Path(tmpdir) / "paper.json"),
            )
            bot_cfg = BotConfig(
                strategy=StrategyConfig(timeframe="M5", ema_fast=50, ema_slow=200, adx_min=0.0, pullback_lookback=6, pullback_atr_tolerance=0.6, rsi_buy_min=40.0, rsi_buy_max=72.0),
                risk=RiskConfig(cooldown_candles=0, max_spread_usd=1.0),
                broker=b_cfg,
                telegram=TelegramConfig(bot_token="x", chat_id="y"),
                manual_confirm_tracking=True,
                bot_state_file=str(Path(tmpdir) / "bot_state.json"),
            )
            broker1 = PaperBroker(b_cfg, candle_fetcher=lambda tf, cnt: candles)
            notifier1 = MagicMock()
            notifier1.get_latest_update_offset.return_value = 1
            notifier1.poll_updates.return_value = (
                [TelegramUserAction(action="pause"), TelegramUserAction(action="status"), TelegramUserAction(action="news")],
                2,
            )
            nf = NewsFilter(buffer_minutes=15)
            nf.set_events_for_test([NewsEvent(title="US CPI m/m", country="USD", impact="High", timestamp_sec=int(__import__("time").time()) + 900)])
            bot1 = TradingBot(bot_cfg, broker=broker1, notifier=notifier1, news_filter=nf)
            bot1.initialize()
            bot1.risk_manager.record_trade_closed(-5.0, 95.0)
            bot1._save_runtime_state()

            # Pause blocks new entry while answering /status and /news
            self.assertIsNone(bot1.run_once())
            self.assertTrue(bot1._paused)
            notifier1.notify_order_opened.assert_not_called()
            self.assertGreaterEqual(notifier1.send_message.call_count, 3)

            # Restart bot2 from persisted files -> preserves _paused and consecutive_losses
            broker2 = PaperBroker(b_cfg, candle_fetcher=lambda tf, cnt: candles)
            notifier2 = MagicMock()
            notifier2.get_latest_update_offset.return_value = 2
            notifier2.poll_updates.return_value = ([], 2)
            bot2 = TradingBot(bot_cfg, broker=broker2, notifier=notifier2, news_filter=nf)
            bot2.initialize()
            self.assertTrue(bot2._paused)
            self.assertEqual(bot2.risk_manager.consecutive_losses, 1)

            # Pre-news and Friday close warnings fire cleanly once when a position is open
            broker2.place_market_order("BUY", 0.01, stop_loss=2100.0, take_profit=2200.0)
            bot2._sync_positions(2150.0)
            self.assertTrue(bot2._check_upcoming_news_for_open_positions())
            self.assertFalse(bot2._check_upcoming_news_for_open_positions())
            fri_night = datetime(2026, 11, 27, 20, 15, tzinfo=timezone.utc)
            self.assertTrue(bot2._maybe_send_weekend_warning(fri_night))
            self.assertFalse(bot2._maybe_send_weekend_warning(fri_night))


if __name__ == "__main__":
    unittest.main()
