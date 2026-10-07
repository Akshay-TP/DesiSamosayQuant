"""Small, auditable client for the Roostoo public trading API."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode

import requests

LOG = logging.getLogger(__name__)


class RoostooAPIError(RuntimeError):
    """Raised for an API or transport error that needs operator attention."""


class RoostooClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str, timeout: int = 15):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"RST-API-KEY": api_key})

    @staticmethod
    def _timestamp() -> str:
        return str(int(time.time() * 1000))

    @staticmethod
    def _canonical(params: dict[str, Any]) -> str:
        return "&".join(f"{key}={params[key]}" for key in sorted(params))

    def _signature(self, params: dict[str, Any]) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            self._canonical(params).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _check(endpoint: str, response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RoostooAPIError(f"{endpoint} returned non-JSON HTTP {response.status_code}") from exc
        if not isinstance(payload, dict):
            raise RoostooAPIError(f"{endpoint} returned an unexpected response")
        if response.status_code >= 400:
            raise RoostooAPIError(f"{endpoint} returned HTTP {response.status_code}: {payload}")
        if payload.get("Success") is False and payload.get("ErrMsg") != "no pending order under this account":
            raise RoostooAPIError(f"{endpoint} rejected the request: {payload.get('ErrMsg', 'unknown error')}")
        return payload

    def _get(self, endpoint: str, params: dict[str, Any] | None = None, signed: bool = False) -> dict[str, Any]:
        payload = dict(params or {})
        payload["timestamp"] = self._timestamp()
        headers = {"MSG-SIGNATURE": self._signature(payload)} if signed else None
        # Safe to retry GETs; order-placing POSTs are deliberately never retried.
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self.session.get(
                    f"{self.base_url}{endpoint}", params=payload, headers=headers, timeout=self.timeout
                )
                return self._check(endpoint, response)
            except (requests.RequestException, RoostooAPIError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                time.sleep(0.5 * (attempt + 1))
        raise RoostooAPIError(f"GET {endpoint} failed after retries: {last_error}") from last_error

    def _post(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {key: str(value) for key, value in params.items()}
        payload["timestamp"] = self._timestamp()
        body = self._canonical(payload)
        headers = {
            "MSG-SIGNATURE": self._signature(payload),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            response = self.session.post(
                f"{self.base_url}{endpoint}", data=body, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            # A timeout can happen after the exchange accepted an order. Never blindly retry it.
            raise RoostooAPIError(
                f"POST {endpoint} transport failure; order status is uncertain, reconcile account before retrying"
            ) from exc
        return self._check(endpoint, response)

    def server_time(self) -> dict[str, Any]:
        return self._get("/v3/serverTime", signed=False)

    def exchange_info(self) -> dict[str, Any]:
        return self._get("/v3/exchangeInfo", signed=False)

    def ticker(self) -> dict[str, Any]:
        return self._get("/v3/ticker", signed=False)

    def balance(self) -> dict[str, Any]:
        return self._get("/v3/balance", signed=True)

    def pending_count(self) -> dict[str, Any]:
        return self._get("/v3/pending_count", signed=True)

    def place_market_order(self, pair: str, side: str, quantity: Decimal) -> dict[str, Any]:
        return self._post(
            "/v3/place_order",
            {"pair": pair, "side": side.upper(), "type": "MARKET", "quantity": format(quantity, "f")},
        )


def floor_quantity(quantity: float, precision: int) -> Decimal:
    """Round a quantity down to exchange precision; never round up exposure."""
    step = Decimal(1).scaleb(-max(0, precision))
    return Decimal(str(max(0.0, quantity))).quantize(step, rounding=ROUND_DOWN)


def ticker_data(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    data = payload.get("Data", payload)
    return data if isinstance(data, dict) else {}


def exchange_pairs(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    data = payload.get("Data", payload)
    pairs = data.get("TradePairs", {}) if isinstance(data, dict) else {}
    return pairs if isinstance(pairs, dict) else {}


def wallet_data(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    for key in ("SpotWallet", "Wallet"):
        wallet = payload.get(key)
        if isinstance(wallet, dict):
            return wallet
    return {}


def wallet_amount(wallet: dict[str, dict[str, Any]], coin: str) -> tuple[float, float]:
    item = wallet.get(coin, {})
    if not isinstance(item, dict):
        return 0.0, 0.0
    return float(item.get("Free", 0) or 0), float(item.get("Lock", 0) or 0)


def encode_params(params: dict[str, Any]) -> str:
    """Kept public for clear diagnostics and future signature interoperability checks."""
    return urlencode(sorted((key, str(value)) for key, value in params.items()))
