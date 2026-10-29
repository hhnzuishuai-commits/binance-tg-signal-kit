"""Bounded inclusive time-window pagination preserving raw fills, not trade ledgers."""
from copy import deepcopy
from binance_account_readonly import ReadRequest, ENDPOINTS, SEVEN_DAYS_MS, _integer


def collect_fills(reader, symbol, *, start_ms, end_ms, limit=1000, max_pages=20):
    """Split saturated windows rather than skip same-millisecond fills.

    At most max_pages data calls (each production read also checks permissions).
    Complete means window traversal only, NOT complete account/position history.
    """
    _integer(max_pages, 1, 100)
    now = reader._clock()
    _integer(start_ms, 0, now)
    _integer(end_ms, start_ms, now)
    # Bound total user range to 90 days; older retained data is not assumed.
    if end_ms - start_ms > 90 * 24 * 60 * 60 * 1000:
        raise ValueError("history range exceeds ninety days")
    ReadRequest("GET", ENDPOINTS["fills"], tuple({"symbol": symbol, "timestamp": now,
        "recvWindow": 5000, "startTime": start_ms, "endTime": min(end_ms, start_ms+SEVEN_DAYS_MS),
        "limit": limit}.items()))
    pending = []
    cursor = start_ms
    while cursor <= end_ms:
        stop = min(cursor + SEVEN_DAYS_MS, end_ms)
        pending.append((cursor, stop))
        cursor = stop + 1
    pages, fills, issues, unresolved = [], {}, [], []
    while pending and len(pages) < max_pages:
        low, high = pending.pop(0)
        page = reader.fills(symbol, start_ms=low, end_ms=high, limit=limit)
        pages.append(deepcopy(page))  # Keep original pages, including failures/overlaps.
        if page["status"] != "ok":
            unresolved.append((low, high))
            issues.append("page_unavailable_or_stale")
            # Stop on permission/auth/limit errors rather than multiplying requests.
            break
        malformed = False
        for row in page["rows"]:
            trade_id, stamp = row.get("id"), row.get("time")
            if type(trade_id) is not int or trade_id < 0 or type(stamp) is not int or not low <= stamp <= high:
                malformed = True
                continue
            key = (symbol, trade_id)
            if key in fills and fills[key] != row:
                issues.append("conflicting_duplicate_fill")
            else:
                fills[key] = deepcopy(row)
        if malformed:
            issues.append("invalid_fill_identity_or_time")
            unresolved.append((low, high))
            break
        if page["possibly_truncated"]:
            if low == high:
                unresolved.append((low, high))
                issues.append("saturated_single_millisecond")
            else:
                middle = (low + high) // 2
                pending[0:0] = [(low, middle), (middle+1, high)]
    unresolved.extend(pending)
    if pending and len(pages) >= max_pages:
        issues.append("page_budget_exhausted")
    finished = reader._clock()
    if any(p["observed_at_ms"] is not None and
           not 0 <= finished-p["observed_at_ms"] <= reader._max_age for p in pages):
        issues.append("stale_at_history_completion")
    if any(p["source"] != "live" for p in pages):
        issues.append("not_live_evidence")
    return {"symbol": symbol, "start_ms": start_ms, "end_ms": end_ms,
            "raw_pages": pages, "fills": sorted(fills.values(), key=lambda row: (row["time"], row["id"])),
            "unresolved_windows": unresolved, "issues": sorted(set(issues)),
            "window_traversal_complete": not unresolved and not any(i in issues for i in
                ("invalid_fill_identity_or_time", "conflicting_duplicate_fill", "page_unavailable_or_stale")),
            "complete_closed_position_ledger": False,
            "limitations": ["exchange_retention_not_verified", "funding_not_queried",
                            "realizedPnl_excludes_commission_and_funding", "commission_assets_not_converted"]}
