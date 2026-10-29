"""Read-only, account-wide USD-M transport for the daily export.

Adds three account-wide reads (income history, all positions, all open orders)
to the original four per-symbol reads, behind ONE fixed allowlist. Same safety
model as binance_readonly_transport.py: GET only, fixed hosts, no redirects,
bounded responses, credentials only in memory, upstream bodies never exposed.
Permissions are checked once per run (read-only + IP-restricted, no write flag).
"""
import hashlib
import hmac
import http.client
import re
import ssl
import time
from urllib.parse import urlencode

from binance_account_readonly import ReadRequest, ReadResponse, ENDPOINTS
from binance_readonly_transport import (MemoryCredentials, ReadOnlyError, _check_permissions,
                                        _strict_json, _MAX_BYTES)

FAPI_HOST = "fapi.binance.com"
SAPI_HOST = "api.binance.com"
PERMISSIONS_PATH = "/sapi/v1/account/apiRestrictions"
INCOME_PATH = "/fapi/v1/income"
POSITIONS_ALL = "/fapi/v3/positionRisk"
OPEN_ORDERS_ALL = "/fapi/v1/openOrders"
INCOME_LIMIT = 1000
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000

# Exact parameter sets for the account-wide reads. Anything else is refused.
_WIDE = {
    INCOME_PATH: {"timestamp", "recvWindow", "startTime", "endTime", "limit"},
    POSITIONS_ALL: {"timestamp", "recvWindow"},
    OPEN_ORDERS_ALL: {"timestamp", "recvWindow"},
}


def _int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ReadOnlyError("invalid_parameters")


def _validate_wide(path, params):
    if path not in _WIDE or set(params) != _WIDE[path]:
        raise ReadOnlyError("path_or_parameters_not_allowed")
    _int(params["timestamp"], 1, 2**63 - 1)
    if params["recvWindow"] != 5000:
        raise ReadOnlyError("invalid_timing_parameters")
    if path == INCOME_PATH:
        _int(params["startTime"], 0, params["timestamp"])
        _int(params["endTime"], params["startTime"], params["timestamp"])
        _int(params["limit"], 1, INCOME_LIMIT)
        if params["endTime"] - params["startTime"] > SEVEN_DAYS_MS:
            raise ReadOnlyError("income_window_exceeds_seven_days")


def _https_get(host, path_and_query, headers):
    """Real network call. Returns (status, body_bytes). Never follows redirects."""
    conn = http.client.HTTPSConnection(host, timeout=10, context=ssl.create_default_context())
    try:
        conn.request("GET", path_and_query, headers=headers)
        response = conn.getresponse()
        return response.status, response.read(_MAX_BYTES + 1)
    finally:
        conn.close()


class AccountWideTransport:
    """Usable as the adapter for AccountReader (per-symbol reads) and for wide reads."""

    def __init__(self, credentials, *, live_authorized=False, clock_ms=None, send=None):
        if not isinstance(credentials, MemoryCredentials):
            raise TypeError("MemoryCredentials required")
        self.__credentials = credentials
        self.__authorized = live_authorized is True
        self.__clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.__send = send or _https_get  # tests inject a fake; production uses HTTPS
        self.__permissions_ok = False
        self.request_count = 0

    def __repr__(self):
        return "AccountWideTransport(<redacted>)"

    def _now(self):
        return self.__clock()

    def __get(self, host, path, params):
        if not self.__authorized:
            raise ReadOnlyError("live_access_not_authorized")
        if host not in (FAPI_HOST, SAPI_HOST):
            raise ReadOnlyError("host_not_allowed")
        query = urlencode(sorted(params.items()))
        signature = hmac.new(self.__credentials.secret.encode("ascii"), query.encode("ascii"),
                             hashlib.sha256).hexdigest()
        failure = http_status = binance_code = result = None
        try:
            self.request_count += 1
            status, payload = self.__send(host, path + "?" + query + "&signature=" + signature,
                {"X-MBX-APIKEY": self.__credentials.api_key, "Accept": "application/json"})
            if type(status) is int and 100 <= status <= 599:
                http_status = status
            if status in (418, 429):
                failure = "rate_limited_no_retry"
            elif status in (401, 403):
                failure = "authentication_or_permission_denied"
            elif status != 200:
                failure = "http_error_no_retry"
            if len(payload) > _MAX_BYTES:
                failure = failure or "response_too_large"
            else:
                try:
                    result = _strict_json(payload)
                except (ValueError, UnicodeError):
                    failure = failure or "transport_or_decode_error"
                else:
                    if isinstance(result, dict) and "code" in result:
                        code = result["code"]
                        if type(code) is int and -(2**31) <= code < 2**31:
                            binance_code = code
                        failure = failure or "exchange_error_no_retry"
        except Exception:
            failure = failure or "transport_or_decode_error"
        if failure:  # raised outside except: no credential-bearing context kept
            raise ReadOnlyError(failure, http_status=http_status, binance_code=binance_code)
        return result

    def check_permissions(self):
        data = self.__get(SAPI_HOST, PERMISSIONS_PATH, {"timestamp": self._now(), "recvWindow": 5000})
        _check_permissions(data)
        self.__permissions_ok = True
        return {"status": "ok", "enableReading": True, "ipRestrict": True,
                "known_write_permissions_disabled": True}

    def _require_permissions(self):
        if not self.__permissions_ok:
            raise ReadOnlyError("permission_check_required_first")

    def wide(self, path, **extra):
        """Account-wide read. Returns a list of row dicts."""
        self._require_permissions()
        params = {"timestamp": self._now(), "recvWindow": 5000, **extra}
        _validate_wide(path, params)
        rows = self.__get(FAPI_HOST, path, params)
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise ReadOnlyError("invalid_account_response")
        return rows

    def __call__(self, request):
        """Adapter for the original AccountReader (four per-symbol endpoints)."""
        if type(request) is not ReadRequest:
            raise ReadOnlyError("validated_read_request_required")
        ReadRequest(request.method, request.path, request.params)
        self._require_permissions()
        params = dict(request.params)
        started = self._now()
        if not 0 <= started - params["timestamp"] <= 5000:
            raise ReadOnlyError("stale_request")
        params["timestamp"] = started
        rows = self.__get(FAPI_HOST, request.path, params)
        if not isinstance(rows, list):
            raise ReadOnlyError("invalid_account_response")
        return ReadResponse(rows, started, "live")


def collect_income(transport, *, start_ms, end_ms, max_pages=30, limit=INCOME_LIMIT):
    """All income rows (every type, every symbol) in [start_ms, end_ms].

    Splits saturated windows (1000 rows) in half instead of skipping rows.
    Income history only covers the last three months on Binance.
    """
    _int(start_ms, 0, end_ms)
    _int(limit, 1, INCOME_LIMIT)
    pending, cursor = [], start_ms
    while cursor <= end_ms:
        stop = min(cursor + SEVEN_DAYS_MS, end_ms)
        pending.append((cursor, stop))
        cursor = stop + 1
    rows, issues, pages = {}, set(), 0
    while pending and pages < max_pages:
        low, high = pending.pop(0)
        pages += 1
        try:
            page = transport.wide(INCOME_PATH, startTime=low, endTime=high, limit=limit)
        except ReadOnlyError as exc:
            issues.add("income_read_failed:" + str(exc))
            pending.insert(0, (low, high))
            break
        for row in page:
            key = (row.get("incomeType"), row.get("tranId"))
            if type(row.get("tranId")) is not int or type(row.get("time")) is not int \
                    or not low <= row["time"] <= high:
                issues.add("invalid_income_row")
                continue
            if key in rows and rows[key] != row:
                issues.add("conflicting_duplicate_income")
            rows[key] = row
        if len(page) >= limit:
            if low == high:
                issues.add("income_saturated_single_millisecond")
            else:
                mid = (low + high) // 2
                pending[0:0] = [(low, mid), (mid + 1, high)]
    if pending:
        issues.add("income_windows_unresolved")
    ordered = sorted(rows.values(), key=lambda r: (r["time"], str(r.get("incomeType")), r["tranId"]))
    complete = not pending and not issues - {"conflicting_duplicate_income"}
    return {"rows": ordered, "complete": complete and "conflicting_duplicate_income" not in issues,
            "issues": sorted(issues), "pages": pages}


SYMBOL_RE = re.compile(r"[A-Z0-9]{2,30}")
