"""Offline, pure USD-M review. No network, credentials, files, or mutable store."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re

TZ = timezone(timedelta(hours=8))


def day_window(day):
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("invalid day")
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    return int(start.timestamp()*1000), int((start+timedelta(days=1)).timestamp()*1000)-1


def number(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid decimal")
    try:
        n = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid decimal") from None
    if not n.is_finite() or abs(n) > Decimal("1e20") or n.as_tuple().exponent < -18:
        raise ValueError("invalid decimal")
    return n


def totals_add(totals, asset, amount):
    if not isinstance(asset, str) or not re.fullmatch(r"[A-Z0-9]{2,20}", asset):
        raise ValueError("invalid asset")
    totals[asset] = totals.get(asset, Decimal(0)) + amount


def serialized(totals):
    return {a: format(v, "f") for a, v in sorted(totals.items())}


def review(data):
    with localcontext() as ctx:
        ctx.prec = 80
        return _review(data)


def _review(data):
    """Explicit evidence envelope; completeness declarations are input evidence, not verified here.

    initial is the signed quantity at day start for EACH positionSide; absent means unknown.
    Any USD-M symbol (uppercase A-Z0-9). Never infer TP labels from prices.
    """
    day, symbol = data["day"], data["symbol"]
    start, end = day_window(day)
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{2,30}", symbol) or data.get("source") not in {"fixture", "live"}:
        raise ValueError("unsupported scope or source")
    fills, funding = data.get("fills"), data.get("funding")
    if not isinstance(fills, list) or not isinstance(funding, list) or len(fills)+len(funding) > 10000:
        raise ValueError("invalid input size")
    issues = []
    complete = data.get("fills_complete") is True
    if not complete:
        issues.append("fills_incomplete")
    if data.get("funding_complete") is not True:
        issues.append("funding_incomplete")
    if data["source"] == "fixture":
        issues.append("mock_not_account_evidence")
    unique = {}
    for row in fills:
        if not isinstance(row, dict):
            raise ValueError("invalid fill")
        if (row.get("symbol") != symbol or type(row.get("id")) is not int or row["id"] < 0
                or type(row.get("time")) is not int or not start <= row["time"] <= end
                or row.get("side") not in {"BUY", "SELL"}
                or row.get("positionSide") not in {"BOTH", "LONG", "SHORT"}):
            raise ValueError("invalid fill")
        qty = number(row["qty"])
        price = number(row["price"])
        number(row["commission"]); number(row["realizedPnl"])
        if qty <= 0 or price <= 0:
            raise ValueError("invalid fill amounts")
        if row["id"] in unique and unique[row["id"]] != row:
            raise ValueError("conflicting duplicate fill")
        unique[row["id"]] = deepcopy(row)
    rows = sorted(unique.values(), key=lambda r: (r["time"], r["id"]))
    if not isinstance(data.get("initial", {}), dict):
        raise ValueError("invalid initial")
    sides = {r["positionSide"] for r in rows} | set(data.get("initial", {}))
    if not sides <= {"BOTH", "LONG", "SHORT"} or ("BOTH" in sides and len(sides) > 1):
        raise ValueError("mixed position modes")
    initial = data.get("initial", {})
    states, cycles, events = {}, [], []
    for side in sorted(sides):
        q = number(initial[side]) if side in initial else None
        if q is not None and ((side == "LONG" and q < 0) or (side == "SHORT" and q > 0)):
            raise ValueError("invalid initial side")
        # Missing pages invalidate quantity reconstruction even with an initial balance.
        states[side] = {"qty": q if complete else None, "cycle": None}
        if q is None:
            issues.append("initial_position_unknown:"+side)
    pnl, fees, funds = Decimal(0), {}, {}

    def new_cycle(side, at, carried=False):
        cycle = {"cycle_id": f"{day}:{symbol}:{side}:{len(cycles)+1}", "position_side": side,
                 "opened_at_ms": None if carried else at, "closed_at_ms": None,
                 "status": "carried_in" if carried else "open", "initial_history_known": not carried,
                 "realized_pnl": Decimal(0), "fees": {}, "events": [], "net_pnl": None,
                 "net_pnl_issues": ["funding_not_allocated_to_cycle"]}
        cycles.append(cycle)
        return cycle

    for row in rows:
        pnl += number(row["realizedPnl"])
        commission = number(row["commission"])
        totals_add(fees, row["commissionAsset"], commission)
        side, qty = row["positionSide"], number(row["qty"])
        delta = qty if row["side"] == "BUY" else -qty
        state = states[side]
        q = state["qty"]
        if q is None:
            events.append({"fill_id": row["id"], "position_side": side, "kind": "unknown", "qty": str(qty)})
            continue
        after = q + delta
        if (side == "LONG" and after < 0) or (side == "SHORT" and after > 0):
            raise ValueError("hedge position overclose")
        if q != 0 and state["cycle"] is None:
            state["cycle"] = new_cycle(side, row["time"], carried=True)
        closing = min(abs(q), qty) if q*delta < 0 else Decimal(0)
        opening = qty - closing
        parts = []
        if closing:
            parts.append(("full_close" if closing == abs(q) else "partial_close", closing))
        if opening:
            parts.append(("open" if q == 0 or closing == abs(q) else "increase", opening))
        allocated_fee = Decimal(0)
        for index, (kind, part_qty) in enumerate(parts):
            if kind == "open":
                state["cycle"] = new_cycle(side, row["time"])
            cycle = state["cycle"]
            fee = commission-allocated_fee if index == len(parts)-1 else commission*part_qty/qty
            allocated_fee += fee
            totals_add(cycle["fees"], row["commissionAsset"], fee)
            # A reversal's realized PnL belongs entirely to its closing part.
            if closing and kind in {"full_close", "partial_close"}:
                cycle["realized_pnl"] += number(row["realizedPnl"])
            elif not closing and number(row["realizedPnl"]) != 0:
                issues.append("unexpected_opening_realized_pnl")
            event = {"fill_id": row["id"], "time_ms": row["time"], "position_side": side,
                     "kind": kind, "qty": str(part_qty), "price": row["price"],
                     "fee_allocation": "quantity_proportion" if len(parts)>1 else "whole_fill"}
            events.append(event); cycle["events"].append(deepcopy(event))
            if kind == "full_close":
                cycle["status"] = "closed" if cycle["initial_history_known"] else "closed_history_incomplete"
                cycle["closed_at_ms"] = row["time"]
                state["cycle"] = None
        state["qty"] = after
    seen = {}
    for row in funding:
        if not isinstance(row, dict):
            raise ValueError("invalid funding")
        if (row.get("symbol") != symbol or row.get("incomeType") != "FUNDING_FEE"
                or type(row.get("tranId")) is not int or row["tranId"] < 0
                or type(row.get("time")) is not int or not start <= row["time"] <= end):
            raise ValueError("invalid funding")
        key = row["tranId"]
        if key in seen:
            if seen[key] != row:
                raise ValueError("conflicting funding")
            continue
        seen[key] = deepcopy(row)
        totals_add(funds, row["asset"], number(row["income"]))
    for cycle in cycles:
        cycle["realized_pnl"] = str(cycle["realized_pnl"])
        cycle["fees"] = serialized(cycle["fees"])
    net_issues = [i for i in issues if i != "mock_not_account_evidence" and not i.startswith("initial_position_unknown:")]
    if (set(fees) | set(funds)) - {"USDT"}:
        net_issues.append("asset_conversion_missing")
    net = None if net_issues else str(pnl-fees.get("USDT", Decimal(0))+funds.get("USDT", Decimal(0)))
    return {"schema_version": 1, "day": day, "timezone": "UTC+08:00", "symbol": symbol,
            "window_start_ms": start, "window_end_ms": end, "source": data["source"],
            "daily_realized_pnl_usdt": str(pnl), "daily_fees_by_asset": serialized(fees),
            "daily_funding_by_asset": serialized(funds), "daily_trading_net_usdt": net,
            "daily_net_issues": sorted(set(net_issues)), "issues": sorted(set(issues)),
            "daily_net_definition": "window realized PnL minus window commissions plus window funding; not closed-cycle profit or total account cash flow",
            "events": events, "cycles": cycles,
            "ending_quantity": {s: None if v["qty"] is None else str(v["qty"]) for s,v in states.items()},
            "plan": deepcopy(data.get("plan")), "plan_execution_match": "not_inferred",
            "complete_closed_position_ledger": False}
