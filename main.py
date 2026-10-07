"""Autonomous Roostoo competition bot. Run with `python main.py`."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from roostoo_client import (
    RoostooAPIError,
    RoostooClient,
    exchange_pairs,
    floor_quantity,
    ticker_data,
    wallet_amount,
    wallet_data,
)
from strategy import build_targets

load_dotenv()
STOP = False
LOG = logging.getLogger(__name__)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None or value == "" else float(value)


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None or value == "" else int(value)


def config() -> dict[str, Any]:
    return {
        "api_key": os.getenv("ROOSTOO_API_KEY", "").strip(),
        "api_secret": os.getenv("ROOSTOO_API_SECRET", "").strip(),
        "base_url": os.getenv("ROOSTOO_BASE_URL", "https://mock-api.roostoo.com").strip(),
        "dry_run": env_bool("DRY_RUN", False),
        "poll_seconds": max(60, env_int("POLL_SECONDS", 3600)),
        "rebalance_hours": max(1, env_int("REBALANCE_HOURS", 4)),
        "max_exposure": min(1.0, max(0.0, env_float("MAX_EXPOSURE", 0.75))),
        "max_position_weight": min(1.0, max(0.0, env_float("MAX_POSITION_WEIGHT", 0.25))),
        "max_drawdown": min(0.50, max(0.01, env_float("MAX_DRAWDOWN_PCT", 0.08))),
        "max_daily_loss": min(0.50, max(0.01, env_float("MAX_DAILY_LOSS_PCT", 0.04))),
        "min_trade_usd": max(1.0, env_float("MIN_TRADE_USD", 250.0)),
        "max_order_usd": max(1.0, env_float("MAX_ORDER_USD", 25000.0)),
        "fee_rate": max(0.0, env_float("TAKER_FEE_RATE", 0.001)),
        "bootstrap": env_bool("BOOTSTRAP_BINANCE", True),
        "binance_url": os.getenv("BINANCE_BASE_URL", "https://api.binance.com").rstrip("/"),
        "history_hours": min(1000, max(73, env_int("HISTORY_HOURS", 168))),
        "data_dir": Path(os.getenv("DATA_DIR", "runtime")),
        "universe": [p.strip().upper() for p in os.getenv(
            "TRADING_PAIRS",
            "BTC/USD,ETH/USD,BNB/USD,SOL/USD,XRP/USD,LINK/USD,DOGE/USD,AVAX/USD,LTC/USD,TRX/USD",
        ).split(",") if p.strip()],
    }


def setup_logging(data_dir: Path) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_dir / "bot.log", encoding="utf-8")],
    )
    logging.Formatter.converter = time.gmtime


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"history": {}, "peak_nav": None, "day": None, "day_start_nav": None, "halted": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        data.setdefault("history", {})
        data.setdefault("peak_nav", None)
        data.setdefault("day", None)
        data.setdefault("day_start_nav", None)
        data.setdefault("halted", False)
        return data
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {path}; preserve the file and inspect it before restarting: {exc}") from exc


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def log_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"time": now_iso(), **event}, sort_keys=True) + "\n")


def append_portfolio(path: Path, values: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    fields = ["time", "nav", "cash", "gross_exposure", "drawdown", "daily_return", "regime"]
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: values.get(key, "") for key in fields})


def load_history_file(state: dict[str, Any]) -> None:
    normalized: dict[str, list[dict[str, float]]] = {}
    for pair, points in state.get("history", {}).items():
        if not isinstance(points, list):
            continue
        valid = []
        for point in points:
            try:
                ts, price = int(point["hour"]), float(point["price"])
                if price > 0:
                    valid.append({"hour": ts, "price": price})
            except (KeyError, TypeError, ValueError):
                continue
        normalized[pair] = sorted({p["hour"]: p for p in valid}.values(), key=lambda p: p["hour"])[-1000:]
    state["history"] = normalized


def bootstrap_binance(cfg: dict[str, Any], state: dict[str, Any], pairs: list[str]) -> None:
    if not cfg["bootstrap"]:
        return
    last_attempt = int(state.get("last_seed_attempt", 0) or 0)
    if time.time() - last_attempt < 6 * 3600 and last_attempt:
        return
    state["last_seed_attempt"] = int(time.time())
    for pair in pairs:
        existing = state["history"].get(pair, [])
        if len(existing) >= 73:
            continue
        base = pair.split("/", 1)[0]
        symbol = f"{base}USDT"
        try:
            response = requests.get(
                f"{cfg['binance_url']}/api/v3/klines",
                params={"symbol": symbol, "interval": "1h", "limit": cfg["history_hours"]},
                timeout=12,
            )
            response.raise_for_status()
            candles = response.json()
            if not isinstance(candles, list):
                continue
            merged = {int(point["hour"]): point for point in existing}
            current_hour = int(time.time()) // 3600
            for candle in candles:
                hour = int(candle[0]) // 3_600_000
                price = float(candle[4])
                if hour < current_hour and price > 0:
                    merged[hour] = {"hour": hour, "price": price}
            state["history"][pair] = sorted(merged.values(), key=lambda p: p["hour"])[-1000:]
            LOG.info("Seeded %s with %d historical hourly prices", pair, len(state["history"][pair]))
        except (requests.RequestException, ValueError, TypeError, IndexError) as exc:
            LOG.warning("Historical seed unavailable for %s (%s); bot will warm up from live prices", pair, exc)


def record_prices(state: dict[str, Any], prices: dict[str, float]) -> None:
    hour = int(time.time()) // 3600
    history = state["history"]
    for pair, price in prices.items():
        if not math.isfinite(price) or price <= 0:
            continue
        points = history.setdefault(pair, [])
        if points and int(points[-1]["hour"]) == hour:
            points[-1]["price"] = price
        elif not points or int(points[-1]["hour"]) < hour:
            points.append({"hour": hour, "price": price})
        history[pair] = points[-1000:]


def nav_and_positions(
    wallet: dict[str, dict[str, Any]], prices: dict[str, float], short_positions: list[dict[str, Any]],
) -> tuple[float, float, dict[str, float], dict[str, float], dict[str, dict[str, Any]], float]:
    cash_free, cash_locked = wallet_amount(wallet, "USD")
    cash = cash_free + cash_locked
    long_positions: dict[str, float] = {}
    short_map: dict[str, dict[str, Any]] = {}
    signed_positions: dict[str, float] = {}
    total = cash
    gross = 0.0
    for coin, pair in ((key.split("/")[0], key) for key in prices):
        free, locked = wallet_amount(wallet, coin)
        quantity = free + locked
        value = quantity * prices[pair]
        if quantity > 0:
            long_positions[pair] = value
            signed_positions[pair] = value
            total += value
            gross += value
    for position in short_positions:
        pair = str(position.get("Pair", ""))
        if not pair:
            continue
        qty = float(position.get("ShortQty", 0) or 0)
        mark = float(prices.get(pair, position.get("CurrentPrice", 0)) or 0)
        notional = qty * mark
        if notional <= 0:
            continue
        short_map[pair] = {**position, "ShortQty": qty, "MarkPrice": mark, "Notional": notional}
        signed_positions[pair] = signed_positions.get(pair, 0.0) - notional
        # USD collateral is already included in wallet Free+Lock; add only unrealized PnL.
        total += float(position.get("UnrealizedPNL", 0) or 0)
        gross += notional
    return total, cash_free, signed_positions, long_positions, short_map, gross


def evaluate_risk(cfg: dict[str, Any], state: dict[str, Any], nav: float) -> tuple[float, float, bool]:
    peak = float(state["peak_nav"] or nav)
    peak = max(peak, nav)
    state["peak_nav"] = peak
    drawdown = (nav / peak - 1.0) if peak > 0 else 0.0
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("day") != today:
        state["day"] = today
        state["day_start_nav"] = nav
    day_start = float(state["day_start_nav"] or nav)
    daily_return = nav / day_start - 1.0 if day_start > 0 else 0.0
    if drawdown <= -cfg["max_drawdown"]:
        state["halted"] = True
        state["halt_reason"] = f"portfolio drawdown reached {drawdown:.2%}"
    return drawdown, daily_return, bool(state.get("halted")) or daily_return <= -cfg["max_daily_loss"]


def market_symbols(payload: dict[str, Any], configured: list[str]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    available = exchange_pairs(payload)
    pairs = [
        pair for pair in configured
        if pair in available and bool(available[pair].get("CanTrade", True))
    ]
    if not pairs:
        pairs = [
            pair for pair, meta in available.items()
            if pair.endswith("/USD") and bool(meta.get("CanTrade", True))
        ]
    return pairs, available


def make_market_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for pair, item in ticker_data(payload).items():
        if not isinstance(item, dict):
            continue
        try:
            price = float(item.get("LastPrice", 0))
        except (TypeError, ValueError):
            continue
        if price > 0:
            result[pair] = {**item, "LastPrice": price}
    return result


def current_wallet(client: RoostooClient) -> dict[str, dict[str, Any]]:
    return wallet_data(client.balance())


def rebalance(
    cfg: dict[str, Any], state: dict[str, Any], client: RoostooClient,
    pairs: list[str], pair_info: dict[str, dict[str, Any]], market: dict[str, dict[str, Any]],
    wallet: dict[str, dict[str, Any]], nav: float, cash_free: float,
    long_positions: dict[str, float], short_positions: dict[str, dict[str, Any]],
    targets: dict[str, float], halt_buys: bool,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if cfg["dry_run"]:
        LOG.info("DRY_RUN: would rebalance to %s", {p: round(w, 3) for p, w in targets.items()})
        return events
    if nav <= 0:
        LOG.warning("Skipping rebalance because NAV is not positive")
        return events

    if halt_buys:
        targets = {}

    reductions: list[tuple[str, str, Any]] = []
    for pair in pairs:
        price = float(market.get(pair, {}).get("LastPrice", 0))
        if price <= 0:
            continue
        target_weight = targets.get(pair, 0.0)
        target_value = abs(target_weight) * nav
        minimum = max(cfg["min_trade_usd"], float(pair_info.get(pair, {}).get("MiniOrder", 1.0) or 1.0))
        current_short = short_positions.get(pair)
        short_value = float(current_short.get("Notional", 0) or 0) if current_short else 0.0
        long_value = long_positions.get(pair, 0.0)

        # Close the opposing side before building exposure in the target direction.
        long_to_reduce = max(0.0, long_value - (target_value if target_weight > 0 else 0.0))
        if long_to_reduce >= minimum:
            coin = pair.split("/")[0]
            free, _locked = wallet_amount(wallet, coin)
            precision = int(pair_info.get(pair, {}).get("AmountPrecision", 6))
            quantity = floor_quantity(min(free, long_to_reduce / price), precision)
            if quantity > 0 and float(quantity) * price >= float(pair_info.get(pair, {}).get("MiniOrder", 1.0) or 1.0):
                reductions.append(("sell_long", pair, quantity))

        short_target = target_value if target_weight < 0 else 0.0
        short_to_reduce = max(0.0, short_value - short_target)
        if short_to_reduce >= minimum and current_short:
            precision = int(pair_info.get(pair, {}).get("AmountPrecision", 6))
            if short_target == 0:
                close_qty = None  # API's omitted close_qty closes the position fully.
            else:
                close_qty = floor_quantity(short_to_reduce / price, precision)
                if close_qty <= 0:
                    continue
            reductions.append(("close_short", pair, close_qty))

    # Reduce and close existing risk first, then refresh account state before opening risk.
    for action, pair, quantity in reductions:
        event = (
            execute_order(cfg, client, pair, "SELL", quantity, {"nav": nav, "reason": "target_reduction"})
            if action == "sell_long"
            else execute_short_close(cfg, client, pair, quantity, {"nav": nav, "reason": "target_reduction"})
        )
        events.append(event)
        if not event.get("success"):
            LOG.error("Stopping rebalance after an unsuccessful/uncertain reduction")
            return events

    try:
        wallet = current_wallet(client)
        live_shorts = client.short_positions()
    except RoostooAPIError as exc:
        LOG.error("Could not reconcile positions after reductions; stopping rebalance: %s", exc)
        return events
    cash_free, _ = wallet_amount(wallet, "USD")
    _nav, _cash, _signed, refreshed_longs, refreshed_shorts, _gross = nav_and_positions(
        wallet, {p: float(v["LastPrice"]) for p, v in market.items()}, live_shorts
    )

    if halt_buys:
        return events

    # Build target exposure only after the opposite side has been reduced to zero.
    for pair in pairs:
        target_weight = targets.get(pair, 0.0)
        if target_weight == 0:
            continue
        price = float(market.get(pair, {}).get("LastPrice", 0))
        if price <= 0:
            continue
        target_value = abs(target_weight) * nav
        minimum = max(cfg["min_trade_usd"], float(pair_info.get(pair, {}).get("MiniOrder", 1.0) or 1.0))
        precision = int(pair_info.get(pair, {}).get("AmountPrecision", 6))

        if target_weight > 0:
            if pair in refreshed_shorts:
                LOG.warning("Skipping long add for %s because its short did not close", pair)
                continue
            delta = target_value - refreshed_longs.get(pair, 0.0)
            if delta < minimum or cash_free <= 0:
                continue
            affordable = cash_free / (1.0 + cfg["fee_rate"] + 0.001)
            spend = min(delta, affordable, cfg["max_order_usd"])
            quantity = floor_quantity(spend / price, precision)
            if quantity <= 0 or float(quantity) * price < float(pair_info.get(pair, {}).get("MiniOrder", 1.0) or 1.0):
                continue
            event = execute_order(cfg, client, pair, "BUY", quantity, {"nav": nav, "reason": "target_increase"})
            events.append(event)
            if not event.get("success"):
                return events
            cash_free = max(0.0, cash_free - float(quantity) * price * (1.0 + cfg["fee_rate"]))
        else:
            dust_limit = float(pair_info.get(pair, {}).get("MiniOrder", 1.0) or 1.0)
            if refreshed_longs.get(pair, 0.0) > dust_limit:
                LOG.warning("Skipping short add for %s because its long did not close", pair)
                continue
            current_short = refreshed_shorts.get(pair)
            current_value = float(current_short.get("Notional", 0) or 0) if current_short else 0.0
            collateral = min(target_value - current_value, cfg["max_order_usd"], cash_free / (1.0 + cfg["fee_rate"] + 0.001))
            if collateral < minimum:
                continue
            event = execute_short_open(cfg, client, pair, collateral, {"nav": nav, "reason": "target_increase"})
            events.append(event)
            if not event.get("success"):
                return events
            cash_free = max(0.0, cash_free - collateral * (1.0 + cfg["fee_rate"]))
    return events


def execute_order(cfg: dict[str, Any], client: RoostooClient, pair: str, side: str, quantity: Any, context: dict[str, Any]) -> dict[str, Any]:
    details = {"pair": pair, "side": side, "quantity": format(quantity, "f"), "context": context}
    order_log = cfg["data_dir"] / "logs" / "orders.jsonl"
    log_event(order_log, {"event": "order_intent", **details})
    if cfg["dry_run"]:
        LOG.info("DRY_RUN order: %s", details)
        result = {"event": "order_result", "success": True, "dry_run": True, **details}
        log_event(order_log, result)
        return result
    LOG.info("Submitting market order: %s %s %s", side, pair, quantity)
    try:
        response = client.place_market_order(pair, side, quantity)
        order = response.get("OrderDetail", response)
        status = order.get("Status", "FILLED")
        event = {
            "event": "order_result",
            "success": status in {"FILLED", "PARTIALLY_FILLED"},
            **details,
            "order_id": order.get("OrderID", order.get("ID")),
            "status": status,
            "filled_quantity": order.get("FilledQuantity", order.get("Quantity")),
            "filled_average_price": order.get("FilledAverPrice", order.get("Price")),
            "fee": order.get("CommissionChargeValue", order.get("Commission", 0)),
        }
        LOG.info("Order accepted: id=%s status=%s filled=%s", event["order_id"], event["status"], event["filled_quantity"])
        log_event(order_log, event)
        return event
    except RoostooAPIError as exc:
        LOG.error("Order failed or has uncertain status: %s", exc)
        event = {"event": "order_result", "success": False, "error": str(exc), **details}
        log_event(order_log, event)
        return event


def execute_short_open(cfg: dict[str, Any], client: RoostooClient, pair: str, collateral: float, context: dict[str, Any]) -> dict[str, Any]:
    details = {"pair": pair, "action": "SHORT_OPEN", "collateral": round(collateral, 8), "context": context}
    order_log = cfg["data_dir"] / "logs" / "orders.jsonl"
    log_event(order_log, {"event": "order_intent", **details})
    if cfg["dry_run"]:
        result = {"event": "order_result", "success": True, "dry_run": True, **details}
        log_event(order_log, result)
        return result
    try:
        response = client.open_short_market(pair, collateral)
        status = response.get("Status", "OPEN")
        event = {
            "event": "order_result",
            "success": status == "OPEN",
            **details,
            "position_id": response.get("ID"),
            "status": status,
            "entry_price": response.get("EntryPrice"),
            "short_quantity": response.get("ShortQty"),
            "open_fee": response.get("OpenFee", 0),
        }
        LOG.info("Short open response: pair=%s id=%s status=%s", pair, event["position_id"], status)
        log_event(order_log, event)
        return event
    except RoostooAPIError as exc:
        LOG.error("Short open failed or has uncertain status: %s", exc)
        event = {"event": "order_result", "success": False, "error": str(exc), **details}
        log_event(order_log, event)
        return event


def execute_short_close(
    cfg: dict[str, Any], client: RoostooClient, pair: str, close_quantity: Any, context: dict[str, Any],
) -> dict[str, Any]:
    details = {
        "pair": pair,
        "action": "SHORT_CLOSE",
        "close_quantity": "ALL" if close_quantity is None else format(close_quantity, "f"),
        "context": context,
    }
    order_log = cfg["data_dir"] / "logs" / "orders.jsonl"
    log_event(order_log, {"event": "order_intent", **details})
    if cfg["dry_run"]:
        result = {"event": "order_result", "success": True, "dry_run": True, **details}
        log_event(order_log, result)
        return result
    try:
        response = client.close_short_market(pair, close_quantity)
        event = {
            "event": "order_result",
            "success": bool(response.get("Success", False)),
            **details,
            "close_price": response.get("ClosePrice"),
            "realized_pnl": response.get("RealizedPNL"),
            "close_fee": response.get("CloseFee", 0),
            "closed_quantity": response.get("ClosedQty"),
            "fully_closed": response.get("FullyClosed"),
            "remaining_quantity": response.get("RemainingQty", 0),
        }
        LOG.info("Short close response: pair=%s qty=%s fully_closed=%s", pair, event["closed_quantity"], event["fully_closed"])
        log_event(order_log, event)
        return event
    except RoostooAPIError as exc:
        LOG.error("Short close failed or has uncertain status: %s", exc)
        event = {"event": "order_result", "success": False, "error": str(exc), **details}
        log_event(order_log, event)
        return event


def run_cycle(cfg: dict[str, Any], state: dict[str, Any], client: RoostooClient, once: bool = False) -> None:
    info_payload = client.exchange_info()
    if info_payload.get("IsRunning") is False:
        raise RoostooAPIError("Roostoo exchange is not currently running")
    pairs, pair_info = market_symbols(info_payload, cfg["universe"])
    if not pairs:
        raise RoostooAPIError("No tradable USD pairs found in exchangeInfo")
    market = make_market_map(client.ticker())
    prices = {pair: float(market[pair]["LastPrice"]) for pair in pairs if pair in market}
    if not prices:
        raise RoostooAPIError("Ticker returned no usable prices for configured pairs")
    bootstrap_binance(cfg, state, pairs)
    record_prices(state, prices)
    wallet = current_wallet(client)
    shorts = client.short_positions()
    nav, cash_free, positions, long_positions, short_map, gross = nav_and_positions(
        wallet, {p: v["LastPrice"] for p, v in market.items()}, shorts
    )
    drawdown, daily_return, halt_buys = evaluate_risk(cfg, state, nav)
    if state.get("halted"):
        halt_buys = True

    pending = client.pending_count()
    pending_total = int(pending.get("TotalPending", 0) or 0)
    hour = int(time.time()) // 3600
    last_rebalance = state.get("last_rebalance_hour")
    do_rebalance = last_rebalance is None or hour - int(last_rebalance) >= cfg["rebalance_hours"]
    targets: dict[str, float] = {}
    diagnostics: dict[str, Any] = {"regime": "warming_up", "signals": {}, "selected": []}
    if do_rebalance:
        changes = {
            pair: float(item.get("Change", 0.0) or 0.0)
            for pair, item in market.items()
        }
        targets, diagnostics = build_targets(
            state["history"], pairs, cfg["max_exposure"], cfg["max_position_weight"],
            ticker_changes=changes,
        )
        state["last_rebalance_hour"] = hour
        # Persist the decision boundary before any order request so a restart cannot
        # blindly repeat a rebalance whose response may have been interrupted.
        save_state(cfg["data_dir"] / "state.json", state)
        LOG.info(
            "Decision: regime=%s targets=%s nav=%.2f drawdown=%.2f%% daily=%.2f%%",
            diagnostics["regime"], {p: round(w, 3) for p, w in targets.items()}, nav, drawdown * 100, daily_return * 100,
        )
        if pending_total:
            LOG.warning("Found %d pending orders; skipping this rebalance", pending_total)
        elif state.get("halted"):
            LOG.error("Risk halt active (%s); liquidating toward cash", state.get("halt_reason", "max drawdown"))
            rebalance(
                cfg, state, client, pairs, pair_info, market, wallet, nav, cash_free,
                long_positions, short_map, {}, True,
            )
        else:
            rebalance(
                cfg, state, client, pairs, pair_info, market, wallet, nav, cash_free,
                long_positions, short_map, targets, halt_buys,
            )
        state["last_decision"] = now_iso()
        state["last_regime"] = diagnostics["regime"]
    elif once:
        LOG.info("Not a scheduled rebalance hour; no orders will be placed")

    gross_pct = gross / nav if nav > 0 else 0.0
    append_portfolio(cfg["data_dir"] / "portfolio.csv", {
        "time": now_iso(), "nav": round(nav, 4), "cash": round(cash_free, 4),
        "gross_exposure": round(gross_pct, 6), "drawdown": round(drawdown, 6),
        "daily_return": round(daily_return, 6), "regime": diagnostics["regime"],
    })
    log_event(cfg["data_dir"] / "logs" / "cycles.jsonl", {
        "nav": nav, "cash_free": cash_free, "positions": positions, "short_positions": short_map, "prices": prices,
        "targets": targets, "regime": diagnostics["regime"], "signals": diagnostics.get("signals", {}), "drawdown": drawdown,
        "daily_return": daily_return, "pending_orders": pending_total,
        "risk_halt": bool(state.get("halted")),
    })
    save_state(cfg["data_dir"] / "state.json", state)


def show_status(cfg: dict[str, Any], client: RoostooClient) -> None:
    info = client.exchange_info()
    market = make_market_map(client.ticker())
    wallet = current_wallet(client)
    shorts = client.short_positions()
    nav, cash_free, positions, _long_positions, short_map, gross = nav_and_positions(
        wallet, {p: v["LastPrice"] for p, v in market.items()}, shorts
    )
    print(json.dumps({
        "exchange_running": bool(info.get("IsRunning", True)),
        "nav_usd": round(nav, 2), "free_cash_usd": round(cash_free, 2),
        "gross_exposure_pct": round(gross / nav, 4) if nav else 0,
        "positions_usd": {key: round(value, 2) for key, value in positions.items()},
        "short_positions": short_map,
    }, indent=2))


def ask_stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous Roostoo trend bot")
    parser.add_argument("--once", action="store_true", help="Run one collection/decision cycle and exit")
    parser.add_argument("--status", action="store_true", help="Print exchange portfolio status and exit")
    parser.add_argument("--check", action="store_true", help="Check public and signed API connectivity, without trading")
    parser.add_argument("--dry-run", action="store_true", help="Override config and do not submit orders")
    args = parser.parse_args()
    cfg = config()
    if args.dry_run:
        cfg["dry_run"] = True
    setup_logging(cfg["data_dir"])
    if not cfg["api_key"] or not cfg["api_secret"]:
        LOG.error("Set ROOSTOO_API_KEY and ROOSTOO_API_SECRET in .env before running")
        return 2
    client = RoostooClient(cfg["api_key"], cfg["api_secret"], cfg["base_url"])
    try:
        if args.check:
            LOG.info("Server time: %s", client.server_time())
            payload = client.exchange_info()
            LOG.info("Exchange running=%s; tradable pairs=%d", payload.get("IsRunning"), len(exchange_pairs(payload)))
            wallet = current_wallet(client)
            shorts = client.short_positions()
            LOG.info("Signed balance and short-position requests succeeded; wallet currencies=%d open_shorts=%d", len(wallet), len(shorts))
            return 0
        if args.status:
            show_status(cfg, client)
            return 0
        state_path = cfg["data_dir"] / "state.json"
        state = read_state(state_path)
        load_history_file(state)
        signal.signal(signal.SIGINT, ask_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, ask_stop)
        LOG.info(
            "Starting bot: dry_run=%s poll=%ss rebalance=%dh history=%s",
            cfg["dry_run"], cfg["poll_seconds"], cfg["rebalance_hours"], "Binance bootstrap" if cfg["bootstrap"] else "live only",
        )
        while not STOP:
            started = time.time()
            try:
                run_cycle(cfg, state, client, once=args.once)
            except RoostooAPIError as exc:
                LOG.exception("Cycle failed; no blind order retry will occur: %s", exc)
                log_event(cfg["data_dir"] / "logs" / "cycles.jsonl", {"error": str(exc)})
                save_state(state_path, state)
                if args.once:
                    return 1
            if args.once:
                return 0
            remaining = max(1, cfg["poll_seconds"] - (time.time() - started))
            while remaining > 0 and not STOP:
                sleep_for = min(remaining, 5)
                time.sleep(sleep_for)
                remaining -= sleep_for
        LOG.info("Stop signal received; state saved")
        save_state(state_path, state)
        return 0
    except (RoostooAPIError, requests.RequestException) as exc:
        LOG.exception("Startup/status operation failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
