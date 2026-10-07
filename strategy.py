"""Long/short cross-sectional trend strategy using price data available live."""

from __future__ import annotations

import math
import statistics
from typing import Any


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _returns(prices: list[float]) -> list[float]:
    return [math.log(current / previous) for previous, current in zip(prices, prices[1:]) if previous > 0 and current > 0]


def _capped_weights(raw: dict[str, float], total: float, cap: float) -> dict[str, float]:
    """Proportionally allocate a side budget subject to a per-asset cap."""
    remaining = min(total, cap * len(raw))
    active = dict(raw)
    weights = {key: 0.0 for key in raw}
    while active and remaining > 1e-9:
        scale = remaining / sum(active.values())
        capped = [key for key, value in active.items() if value * scale >= cap]
        if not capped:
            for key, value in active.items():
                weights[key] = value * scale
            break
        for key in capped:
            weights[key] = cap
            remaining -= cap
            active.pop(key)
    return {key: value for key, value in weights.items() if value > 0}


def build_targets(
    histories: dict[str, list[dict[str, float]]],
    pairs: list[str],
    max_exposure: float = 0.75,
    max_position_weight: float = 0.25,
    min_score: float = 0.35,
    ticker_changes: dict[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Return signed target weights; positive is long, negative is short.

    Combined absolute weights never exceed max_exposure. In a BTC risk-on
    regime, 75% of the budget is assigned to longs and 25% to shorts; the
    split reverses in risk-off conditions. Cold starts use at most 25% gross.
    """
    signals: dict[str, dict[str, float]] = {}
    ticker_changes = ticker_changes or {}
    for pair in pairs:
        points = histories.get(pair, [])
        prices = [float(row["price"]) for row in points if float(row.get("price", 0)) > 0]
        if len(prices) < 73:
            change = float(ticker_changes.get(pair, 0.0))
            if abs(change) >= 0.01:
                signals[pair] = {
                    "price": prices[-1] if prices else 0.0,
                    "momentum_24h": change,
                    "momentum_72h": 0.0,
                    "daily_volatility": 0.05,
                    "score": change / 0.02,
                    "eligible": 1.0,
                    "cold_start": 1.0,
                }
            continue

        last = prices[-1]
        returns = _returns(prices[-73:])
        if len(returns) < 72:
            continue
        daily_vol = statistics.pstdev(returns[-24:]) * math.sqrt(24)
        weekly_vol = statistics.pstdev(returns[-72:]) * math.sqrt(24)
        if daily_vol <= 1e-6 or weekly_vol <= 1e-6:
            continue
        mom_24 = last / prices[-25] - 1.0
        mom_72 = last / prices[-73] - 1.0
        ema_24 = _ema(prices[-96:], 24)
        score = 0.55 * (mom_24 / daily_vol) + 0.45 * (mom_72 / (weekly_vol * math.sqrt(3)))
        long_signal = mom_24 > 0 and last > ema_24 and score >= min_score
        short_signal = mom_24 < 0 and last < ema_24 and score <= -min_score
        signals[pair] = {
            "price": last,
            "momentum_24h": mom_24,
            "momentum_72h": mom_72,
            "daily_volatility": daily_vol,
            "score": score,
            "eligible": float(long_signal or short_signal),
            "cold_start": 0.0,
        }

    btc = signals.get("BTC/USD")
    btc_prices = [float(row["price"]) for row in histories.get("BTC/USD", []) if float(row.get("price", 0)) > 0]
    if btc and len(btc_prices) >= 73:
        regime_risk_on = btc["price"] > _ema(btc_prices[-168:], 72)
    else:
        regime_risk_on = bool(btc and btc["momentum_24h"] > 0)

    eligible = [(pair, item) for pair, item in signals.items() if item["eligible"]]
    cold_start = any(item.get("cold_start", 0.0) for _, item in eligible)
    longs = sorted(
        ((pair, item) for pair, item in eligible if item["score"] > 0),
        key=lambda entry: entry[1]["score"], reverse=True,
    )[:2]
    shorts = sorted(
        ((pair, item) for pair, item in eligible if item["score"] < 0),
        key=lambda entry: entry[1]["score"],
    )[:2]
    if not longs and not shorts:
        return {}, {"regime": "risk_off", "signals": signals, "selected": [], "long_budget": 0.0, "short_budget": 0.0}

    gross_budget = min(max(0.0, max_exposure), 0.25 if cold_start else max_exposure)
    long_share, short_share = (0.75, 0.25) if regime_risk_on else (0.25, 0.75)
    long_budget = gross_budget * long_share
    short_budget = gross_budget * short_share

    def raw_strength(items: list[tuple[str, dict[str, float]]]) -> dict[str, float]:
        return {
            pair: max(0.01, abs(item["score"])) / max(item["daily_volatility"], 0.01)
            for pair, item in items
        }

    long_weights = _capped_weights(raw_strength(longs), long_budget, max_position_weight) if longs else {}
    short_weights = _capped_weights(raw_strength(shorts), short_budget, max_position_weight) if shorts else {}
    targets = {pair: weight for pair, weight in long_weights.items()}
    targets.update({pair: -weight for pair, weight in short_weights.items()})
    selected = [pair for pair, _ in longs] + [pair for pair, _ in shorts]
    return targets, {
        "regime": "cold_start" if cold_start else ("risk_on" if regime_risk_on else "risk_off"),
        "signals": signals,
        "selected": selected,
        "long_budget": long_budget,
        "short_budget": short_budget,
    }
