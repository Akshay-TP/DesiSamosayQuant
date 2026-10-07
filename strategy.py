"""Long-only cross-sectional trend strategy using price data available live."""

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


def build_targets(
    histories: dict[str, list[dict[str, float]]],
    pairs: list[str],
    max_exposure: float = 0.75,
    max_position_weight: float = 0.25,
    min_score: float = 0.35,
    ticker_changes: dict[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Return target weights and diagnostics. Weights are long-only and sum <= 1."""
    signals: dict[str, dict[str, float]] = {}
    ticker_changes = ticker_changes or {}
    for pair in pairs:
        points = histories.get(pair, [])
        prices = [float(row["price"]) for row in points if float(row.get("price", 0)) > 0]
        if len(prices) < 73:
            # Roostoo's ticker includes its own 24-hour return. Use it as a
            # conservative cold-start signal, without relying on another data feed.
            change = float(ticker_changes.get(pair, -1.0))
            if change >= 0.01:
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
        r = _returns(prices[-73:])
        if len(r) < 72:
            continue
        daily_vol = statistics.pstdev(r[-24:]) * math.sqrt(24)
        weekly_vol = statistics.pstdev(r[-72:]) * math.sqrt(24)
        if daily_vol <= 1e-6 or weekly_vol <= 1e-6:
            continue
        mom_24 = last / prices[-25] - 1.0
        mom_72 = last / prices[-73] - 1.0
        ema_24 = _ema(prices[-96:], 24)
        ema_72 = _ema(prices[-168:], 72)
        score = 0.55 * (mom_24 / daily_vol) + 0.45 * (mom_72 / (weekly_vol * math.sqrt(3)))
        eligible = mom_24 > 0 and last > ema_24 and score >= min_score
        signals[pair] = {
            "price": last,
            "momentum_24h": mom_24,
            "momentum_72h": mom_72,
            "daily_volatility": daily_vol,
            "score": score,
            "eligible": float(eligible),
            "cold_start": 0.0,
        }

    btc = signals.get("BTC/USD")
    btc_prices = [float(row["price"]) for row in histories.get("BTC/USD", []) if float(row.get("price", 0)) > 0]
    regime_risk_on = bool(
        btc and (
            btc["price"] > _ema(btc_prices[-168:], 72)
            if len(btc_prices) >= 73
            else btc["momentum_24h"] > 0
        )
    )
    ranked = sorted(
        ((pair, item) for pair, item in signals.items() if item["eligible"]),
        key=lambda entry: entry[1]["score"],
        reverse=True,
    )[:4]
    if not ranked:
        return {}, {"regime": "risk_off", "signals": signals, "selected": []}

    cold_start = any(item.get("cold_start", 0.0) for _, item in ranked)
    if cold_start:
        ranked = ranked[:2]
    exposure_cap = min(max_exposure, 0.25) if cold_start else (
        max_exposure if regime_risk_on else min(max_exposure, 0.30)
    )
    exposure = min(exposure_cap, max_position_weight * len(ranked))
    raw = {
        pair: max(0.01, item["score"]) / max(item["daily_volatility"], 0.01)
        for pair, item in ranked
    }
    weights = _capped_weights(raw, exposure, max_position_weight)
    return weights, {
        "regime": "cold_start" if cold_start else ("risk_on" if regime_risk_on else "risk_off"),
        "signals": signals,
        "selected": [pair for pair, _ in ranked],
    }


def _capped_weights(raw: dict[str, float], total: float, cap: float) -> dict[str, float]:
    """Proportionally allocate a total exposure subject to a per-asset cap."""
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
