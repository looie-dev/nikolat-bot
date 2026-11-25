"""Backtest runner for TrendPullbackStrategy on historical OHLCV CSV files."""

import argparse
import csv
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config import StrategyConfig
from strategy import TrendPullbackStrategy, compute_indicators


@dataclass
class BacktestMetrics:
    total_trades: int
    wins: int
    losses: int
    breakevens: int
    win_rate_pct: float
    total_r: float
    expectancy_r: float
    profit_factor: float
    max_drawdown_r: float


def load_csv_candles(csv_path: str) -> List[Dict[str, Any]]:
    candles: List[Dict[str, Any]] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append(
                {
                    "time": int(float(row["time"])),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("tick_volume") or row.get("volume") or 0),
                }
            )
    return candles


def run_backtest(
    candles: List[Dict[str, Any]],
    cfg: StrategyConfig,
    spread_usd: float = 0.30,
    cooldown_candles: int = 2,
) -> BacktestMetrics:
    strategy = TrendPullbackStrategy(cfg)
    cache = compute_indicators(candles, cfg)

    r_results: List[float] = []
    open_trade: Optional[Dict[str, Any]] = None
    cooldown_until = -1

    for i in range(strategy.min_candles, len(candles)):
        c = candles[i]
        high = c["high"]
        low = c["low"]

        if open_trade is not None:
            side = open_trade["side"]
            sl = open_trade["sl"]
            tp = open_trade["tp"]
            risk = open_trade["risk"]
            entry = open_trade["entry"]
            be_done = open_trade["be_done"]

            if side == "BUY":
                # Conservative: check SL before TP if both hit in the same bar
                if low <= sl:
                    r_results.append((sl - entry) / risk)
                    open_trade = None
                    cooldown_until = i + cooldown_candles
                    continue
                if high >= tp:
                    r_results.append((tp - entry) / risk)
                    open_trade = None
                    cooldown_until = i + cooldown_candles
                    continue
                if cfg.breakeven_atr_trigger > 0 and not be_done:
                    if high >= entry + cfg.breakeven_atr_trigger * open_trade["atr"]:
                        open_trade["sl"] = entry
                        open_trade["be_done"] = True
            else:
                if high >= sl:
                    r_results.append((entry - sl) / risk)
                    open_trade = None
                    cooldown_until = i + cooldown_candles
                    continue
                if low <= tp:
                    r_results.append((entry - tp) / risk)
                    open_trade = None
                    cooldown_until = i + cooldown_candles
                    continue
                if cfg.breakeven_atr_trigger > 0 and not be_done:
                    if low <= entry - cfg.breakeven_atr_trigger * open_trade["atr"]:
                        open_trade["sl"] = entry
                        open_trade["be_done"] = True
            continue

        if i < cooldown_until:
            continue

        sig = strategy.evaluate_at(candles, i, cache)
        if sig is None:
            continue

        half_spread = spread_usd / 2.0
        if sig.side == "BUY":
            entry = sig.entry_price + half_spread
            sl = sig.stop_loss
            tp = sig.take_profit
            risk = entry - sl
        else:
            entry = sig.entry_price - half_spread
            sl = sig.stop_loss
            tp = sig.take_profit
            risk = sl - entry

        if risk <= 0:
            continue

        open_trade = {
            "side": sig.side,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "risk": risk,
            "atr": sig.atr,
            "be_done": False,
        }

    total = len(r_results)
    if total == 0:
        return BacktestMetrics(0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)

    wins = sum(1 for r in r_results if r > 1e-6)
    losses = sum(1 for r in r_results if r < -1e-6)
    bes = total - wins - losses
    gross_win = sum(r for r in r_results if r > 0)
    gross_loss = abs(sum(r for r in r_results if r < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in r_results:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return BacktestMetrics(
        total_trades=total,
        wins=wins,
        losses=losses,
        breakevens=bes,
        win_rate_pct=round(100.0 * wins / total, 2),
        total_r=round(equity, 2),
        expectancy_r=round(equity / total, 3),
        profit_factor=round(pf, 2),
        max_drawdown_r=round(max_dd, 2),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest XAUUSD TrendPullbackStrategy")
    parser.add_argument("--csv", required=True, help="Path to OHLCV CSV file")
    parser.add_argument("--spread", type=float, default=0.30, help="Spread in USD")
    args = parser.parse_args()

    candles = load_csv_candles(args.csv)
    split = int(len(candles) * 0.6)
    cfg = StrategyConfig()

    for label, subset in [("FULL", candles), ("TRAIN (60%)", candles[:split]), ("TEST (40%)", candles[split:])]:
        m = run_backtest(subset, cfg, spread_usd=args.spread)
        print(
            f"{label:12s} | trades={m.total_trades:4d} | win={m.win_rate_pct:5.1f}% "
            f"| BE={m.breakevens:3d} | total={m.total_r:+7.2f}R | exp={m.expectancy_r:+.3f}R "
            f"| PF={m.profit_factor:4.2f} | maxDD={m.max_drawdown_r:5.2f}R"
        )


if __name__ == "__main__":
    main()
