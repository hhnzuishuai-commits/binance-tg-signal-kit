"""Bounded USD-M account reader. No network, credentials, or signing implementation.

An explicitly injected adapter receives validated read requests. The only bundled
adapter is in-memory fixtures. Importing this module has no external effects.
"""
from copy import deepcopy
from dataclasses import dataclass
import re
import time
from types import MappingProxyType


ENDPOINTS = MappingProxyType({
    "positions": "/fapi/v3/positionRisk",
    "open_orders": "/fapi/v1/openOrders",
    "algo_orders": "/fapi/v1/openAlgoOrders",
    "fills": "/fapi/v1/userTrades",
})
ALLOWLIST = frozenset(("GET", path) for path in ENDPOINTS.values())
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("integer outside permitted range")


@dataclass(frozen=True)
class ReadRequest:
    method: str
    path: str
    params: tuple

    def __post_init__(self):
        if (self.method, self.path) not in ALLOWLIST:
            raise ValueError("method/path not allowed")
        if not isinstance(self.params, tuple) or any(
            not isinstance(item, tuple) or len(item) != 2 for item in self.params
        ):
            raise ValueError("parameters must be immutable pairs")
        p = dict(self.params)
        required = {"symbol", "timestamp", "recvWindow"}
        if self.path == ENDPOINTS["fills"]:
            required |= {"startTime", "endTime", "limit"}
        if set(p) != required or len(p) != len(self.params):
            raise ValueError("unexpected or missing parameters")
        if not isinstance(p["symbol"], str) or not re.fullmatch(r"[A-Z0-9]{2,30}", p["symbol"]):
            raise ValueError("invalid symbol")
        _integer(p["timestamp"], 1, 2**63 - 1)
        _integer(p["recvWindow"], 1, 5000)
        if self.path == ENDPOINTS["fills"]:
            _integer(p["startTime"], 0, p["timestamp"])
            _integer(p["endTime"], p["startTime"], p["timestamp"])
            _integer(p["limit"], 1, 1000)
            if p["endTime"] - p["startTime"] > SEVEN_DAYS_MS:
                raise ValueError("fill window exceeds seven days")


@dataclass(frozen=True)
class ReadResponse:
    rows: object
    observed_at_ms: int
    source: str  # fixture or live; supplied by the trusted adapter, never inferred


class FixtureAdapter:
    """In-memory test data only. Missing fixtures fail rather than mean empty."""
    def __init__(self, responses):
        self._responses = deepcopy(responses)

    def __call__(self, request):
        ReadRequest(request.method, request.path, request.params)
        key = (request.path, dict(request.params)["symbol"])
        response = deepcopy(self._responses[key])
        if isinstance(response, Exception):
            raise response
        if response.source != "fixture":
            raise ValueError("fixture provenance required")
        return response


class AccountReader:
    """Four reads, one symbol per call; no retries, discovery, or pagination loop.

    Adapters must enforce their own timeouts and prohibit redirects. No live
    adapter is supplied; injected code is a trust boundary, not a sandbox.
    """
    def __init__(self, adapter, *, clock_ms=None, max_age_ms=60_000):
        _integer(max_age_ms, 1, 3_600_000)
        self._adapter = adapter
        self._clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._max_age = max_age_ms

    def _read(self, kind, symbol, **extra):
        params = {"symbol": symbol, "timestamp": self._clock(), "recvWindow": 5000, **extra}
        request = ReadRequest("GET", ENDPOINTS[kind], tuple(params.items()))
        result = {"kind": kind, "exchange": "Binance", "market": "USD-M",
                  "symbol": symbol, "endpoint": request.path, "scope": "single_symbol",
                  "status": "unavailable", "rows": None, "observed_at_ms": None,
                  "source": "unknown", "issues": [], "possibly_truncated": False}
        if kind == "fills":
            result["window"] = {key: extra[key] for key in ("startTime", "endTime", "limit")}
        try:
            response = self._adapter(request)
            if not isinstance(response, ReadResponse) or response.source not in ("fixture", "live"):
                raise ValueError("invalid response")
            _integer(response.observed_at_ms, 1, 2**63 - 1)
            rows = response.rows
            if not isinstance(rows, list) or len(rows) > 10_000:
                raise ValueError("invalid or oversized response")
            identity = {"positions": "positionSide", "open_orders": "orderId",
                        "algo_orders": "algoId", "fills": "id"}[kind]
            if any(not isinstance(row, dict) or row.get("symbol") != symbol or
                   identity not in row for row in rows):
                raise ValueError("malformed or wrong-symbol rows")
            age = self._clock() - response.observed_at_ms
            result.update(rows=deepcopy(rows), source=response.source,
                          observed_at_ms=response.observed_at_ms, status="ok")
            if age < 0 or age > self._max_age:
                result["status"] = "stale"
                result["issues"].append("future_timestamp" if age < 0 else "stale_evidence")
            if response.source == "fixture":
                result["issues"].append("mock_data_not_account_evidence")
            if kind == "fills" and len(rows) >= extra["limit"]:
                result["possibly_truncated"] = True
                result["issues"].append("fill_limit_reached")
        except Exception:
            # Never include adapter exception text: it can contain signed URLs/keys.
            result["issues"].append("read_failed_or_invalid_response")
        return result

    def positions(self, symbol):
        return self._read("positions", symbol)

    def open_orders(self, symbol):
        return self._read("open_orders", symbol)

    def algo_orders(self, symbol):
        return self._read("algo_orders", symbol)

    def fills(self, symbol, *, start_ms, end_ms, limit=500):
        return self._read("fills", symbol, startTime=start_ms, endTime=end_ms, limit=limit)

    def snapshot(self, symbol, *, start_ms, end_ms, limit=500):
        # Validate the entire request before any adapter is invoked.
        ReadRequest("GET", ENDPOINTS["fills"], tuple({"symbol": symbol,
            "timestamp": self._clock(), "recvWindow": 5000, "startTime": start_ms,
            "endTime": end_ms, "limit": limit}.items()))
        sections = {"positions": self.positions(symbol), "open_orders": self.open_orders(symbol),
                    "algo_orders": self.algo_orders(symbol),
                    "fills": self.fills(symbol, start_ms=start_ms, end_ms=end_ms, limit=limit)}
        return {"schema_version": 1, "atomic": False, "sections": sections,
                "usable_live_evidence": all(s["status"] == "ok" and s["source"] == "live"
                    and not s["possibly_truncated"] for s in sections.values()),
                "limitations": ["single_symbol_only", "fills_are_windowed_not_full_history",
                                "orders_do_not_prove_fills", "no_strategy_rating_computed"]}
