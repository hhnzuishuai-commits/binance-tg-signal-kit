"""Opt-in HTTPS transport for the existing bounded reader; no credential storage.

No CLI, listener, monitor, environment lookup, or network activity on import/init.
Only HMAC keys are supported. Callers must obtain action-time approval separately.
"""
from dataclasses import dataclass, field
import hashlib
import hmac
import http.client
import json
import re
import ssl
import time
from urllib.parse import urlencode

from binance_account_readonly import AccountReader, ReadRequest, ReadResponse, ENDPOINTS

_PERMISSIONS = "/sapi/v1/account/apiRestrictions"
_WRITE_FLAGS = frozenset({"enableWithdrawals", "enableInternalTransfer", "enableMargin",
    "enableFutures", "permitsUniversalTransfer", "enableVanillaOptions", "enableFixApiTrade",
    "enableSpotAndMarginTrading", "enablePortfolioMarginTrading"})
_KNOWN_FLAGS = _WRITE_FLAGS | {"enableReading", "enableFixReadOnly"}
_MAX_BYTES = 2_000_000


class ReadOnlyError(Exception):
    """Only fixed safe messages are emitted; upstream bodies are never exposed."""

    def __init__(self, category, *, http_status=None, binance_code=None):
        super().__init__(category)
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.binance_code = binance_code if type(binance_code) is int and -(2**31) <= binance_code < 2**31 else None

    def safe_diagnostics(self):
        return {key: value for key, value in (("http_status", self.http_status),
                ("binance_code", self.binance_code)) if value is not None}


def _strict_json(payload):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    def reject_constant(value):
        raise ValueError("nonstandard JSON constant")
    return json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object,
                      parse_constant=reject_constant)


@dataclass(frozen=True, repr=False)
class MemoryCredentials:
    api_key: str = field(repr=False)
    secret: str = field(repr=False)

    def __post_init__(self):
        for value in (self.api_key, self.secret):
            if isinstance(value, str) and value and all(c in "*•●·∙⋅＊" for c in value):
                raise ReadOnlyError("credential_mask_placeholder")
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{8,256}", value):
                raise ValueError("invalid HMAC credential format")

    def __repr__(self):
        return "MemoryCredentials(<redacted>)"


def _check_permissions(data):
    if not isinstance(data, dict) or data.get("enableReading") is not True or data.get("ipRestrict") is not True:
        raise ReadOnlyError("permission_check_failed")
    if any(data.get(flag) is not False for flag in _WRITE_FLAGS):
        raise ReadOnlyError("write_permission_or_unknown_permission")
    # New permission fields require code review, even when currently false.
    if any(key.startswith(("enable", "permits")) and key not in _KNOWN_FLAGS for key in data):
        raise ReadOnlyError("unknown_permission_schema")


class BinanceReadOnlyTransport:
    def __init__(self, credentials, *, live_authorized=False, clock_ms=None):
        if not isinstance(credentials, MemoryCredentials):
            raise TypeError("MemoryCredentials required")
        self.__credentials = credentials
        self.__authorized = live_authorized is True
        self.__clock = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def __repr__(self):
        return "BinanceReadOnlyTransport(<redacted>, no_automatic_activity)"

    def __get(self, path, params):
        # Validate again at the last boundary before signing. No arbitrary host,
        # method, headers, body, or generic signing API is accepted.
        if not self.__authorized:
            raise ReadOnlyError("live_access_not_authorized")
        if path == _PERMISSIONS:
            if set(params) != {"timestamp", "recvWindow"}:
                raise ReadOnlyError("invalid_permission_request")
            host = "api.binance.com"
        else:
            ReadRequest("GET", path, tuple(params.items()))
            host = "fapi.binance.com"
        if type(params["timestamp"]) is not int or params["recvWindow"] != 5000:
            raise ReadOnlyError("invalid_timing_parameters")
        query = urlencode(sorted(params.items()))
        signature = hmac.new(self.__credentials.secret.encode("ascii"),
                             query.encode("ascii"), hashlib.sha256).hexdigest()
        conn = None
        failure = None
        result = None
        http_status = None
        binance_code = None
        try:
            conn = http.client.HTTPSConnection(host, timeout=10, context=ssl.create_default_context())
            conn.request("GET", path + "?" + query + "&signature=" + signature,
                         headers={"X-MBX-APIKEY": self.__credentials.api_key, "Accept": "application/json"})
            response = conn.getresponse()
            if type(response.status) is int and 100 <= response.status <= 599:
                http_status = response.status
            if response.status in (418, 429):
                failure = "rate_limited_no_retry"
            elif response.status in (401, 403):
                failure = "authentication_or_permission_denied"
            elif response.status != 200:
                failure = "http_error_no_retry"  # Includes redirects; never followed.
            payload = response.read(_MAX_BYTES + 1)
            if len(payload) > _MAX_BYTES:
                failure = failure or "response_too_large"
            else:
                try:
                    result = _strict_json(payload)
                except (ValueError, UnicodeError):
                    failure = failure or "transport_or_decode_error"
                else:
                    if isinstance(result, dict) and "code" in result:
                        value = result["code"]
                        if type(value) is int and -(2**31) <= value < 2**31:
                            binance_code = value
                        failure = failure or "exchange_error_no_retry"
        except Exception:
            failure = failure or "transport_or_decode_error"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        # Raise outside except: no credential-bearing exception context retained.
        if failure:
            raise ReadOnlyError(failure, http_status=http_status, binance_code=binance_code)
        return result

    def check_permissions(self):
        """One fixed permission request; return only a validated safe summary."""
        data = self.__get(_PERMISSIONS, {"timestamp": self.__clock(), "recvWindow": 5000})
        _check_permissions(data)
        return {"status": "ok", "enableReading": True, "ipRestrict": True,
                "known_write_permissions_disabled": True, "account_data_queried": False}

    def __call__(self, request):
        if type(request) is not ReadRequest:
            raise ReadOnlyError("validated_read_request_required")
        ReadRequest(request.method, request.path, request.params)
        if not self.__authorized:
            raise ReadOnlyError("live_access_not_authorized")
        params = dict(request.params)
        now = self.__clock()
        if not 0 <= now - params["timestamp"] <= 5000:
            raise ReadOnlyError("stale_request")
        permissions = self.__get(_PERMISSIONS, {"timestamp": now, "recvWindow": 5000})
        _check_permissions(permissions)  # Every read: no cached permission approval.
        started = self.__clock()
        params["timestamp"] = started
        rows = self.__get(request.path, params)
        if not isinstance(rows, list):
            raise ReadOnlyError("invalid_account_response")
        # Conservative observation time: slow responses must not look fresh.
        return ReadResponse(rows, started, "live")


class EvidenceReader(AccountReader):
    """Recheck all section ages at the END of a sequential snapshot."""
    def snapshot(self, *args, **kwargs):
        result = super().snapshot(*args, **kwargs)
        now = self._clock()
        for section in result["sections"].values():
            stamp = section["observed_at_ms"]
            if stamp is not None and not 0 <= now - stamp <= self._max_age:
                section["status"] = "stale"
                if "stale_at_snapshot_completion" not in section["issues"]:
                    section["issues"].append("stale_at_snapshot_completion")
        result["usable_live_evidence"] = all(
            s["status"] == "ok" and s["source"] == "live" and not s["possibly_truncated"]
            for s in result["sections"].values())
        return result
