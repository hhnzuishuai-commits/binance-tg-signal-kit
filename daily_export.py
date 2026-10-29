"""Daily export: one JSON report per UTC+08 day, plus a CSV for manual tags.

Read-only. Uses AccountWideTransport (or any object with the same interface,
e.g. a test fake). Writes only into the chosen output folder.
"""
import csv
from datetime import datetime, timezone
from decimal import Decimal
import json
import os

from account_wide import (collect_income, POSITIONS_ALL, OPEN_ORDERS_ALL, SYMBOL_RE)
from binance_account_readonly import AccountReader
from binance_fill_history import collect_fills
from review_core import day_window, review, TZ

TRADE_INCOME = {"REALIZED_PNL", "COMMISSION"}
NINETY_DAYS_MS = 90 * 24 * 60 * 60 * 1000
ANNOTATION_HEADER = ["日期", "币种", "周期ID", "方向", "开仓时间", "平仓时间", "状态", "已实现盈亏",
                     "信号评级(A/B+/B/⭐C)", "震荡或趋势", "止盈到第几步", "是否接针单", "备注"]


def _dec(value):
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _fmt_ms(ms):
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M:%S")


def _sum_by(rows, *keys):
    out = {}
    for r in rows:
        amount = _dec(r.get("income"))
        if amount is None:
            continue
        k = " ".join(str(r.get(x)) for x in keys)
        out[k] = out.get(k, Decimal(0)) + amount
    return {k: format(v, "f") for k, v in sorted(out.items())}


def _side_delta(fills):
    """Signed quantity change per positionSide (BUY +, SELL -)."""
    delta = {}
    for f in fills:
        q = _dec(f.get("qty")) or Decimal(0)
        delta[f["positionSide"]] = delta.get(f["positionSide"], Decimal(0)) + (q if f["side"] == "BUY" else -q)
    return delta


def build_report(transport, day, *, clock_ms):
    start, end = day_window(day)
    now = clock_ms()
    if start > now:
        raise ValueError("day_in_future")
    if now - start > NINETY_DAYS_MS:
        raise ValueError("day_older_than_90_days")
    window_end = min(end, now)
    report = {"schema_version": 1, "day": day, "timezone": "UTC+08:00",
              "exported_at": _fmt_ms(now), "day_complete": now > end,
              "source": "live", "issues": [], "notes": [
                  "持仓/挂单是导出时刻的快照，不是当天收盘时的状态",
                  "日内净额 = 已实现盈亏 - 手续费 + 资金费；不是完整平仓周期利润",
                  "评级、震荡/趋势、止盈步数需要在标注 CSV 里手动填写"]}

    report["permissions"] = transport.check_permissions()

    income = collect_income(transport, start_ms=start, end_ms=window_end)
    report["issues"] += income["issues"]
    report["income_complete"] = income["complete"]
    report["income_by_type_asset"] = _sum_by(income["rows"], "incomeType", "asset")

    positions = transport.wide(POSITIONS_ALL)
    open_orders = transport.wide(OPEN_ORDERS_ALL)
    live_positions = [p for p in positions if (_dec(p.get("positionAmt")) or 0) != 0]

    traded = sorted({r["symbol"] for r in income["rows"]
                     if r.get("incomeType") in TRADE_INCOME and SYMBOL_RE.fullmatch(str(r.get("symbol", "")))})
    watched = sorted(set(traded) | {p["symbol"] for p in live_positions}
                     | {o["symbol"] for o in open_orders if SYMBOL_RE.fullmatch(str(o.get("symbol", "")))})

    reader = AccountReader(transport, clock_ms=clock_ms, max_age_ms=3_600_000)
    symbols = {}
    for sym in watched:
        entry = {"traded_today": sym in traded,
                 "position_now": [p for p in positions if p.get("symbol") == sym],
                 "open_orders_now": [o for o in open_orders if o.get("symbol") == sym]}
        algo = reader.algo_orders(sym)
        entry["algo_orders_now"] = algo["rows"] if algo["status"] in ("ok", "stale") else None
        if algo["status"] not in ("ok", "stale"):
            entry["algo_orders_issue"] = "read_failed"
        if sym in traded:
            entry["review"] = _review_symbol(reader, sym, day, start, end, now, positions, income)
        symbols[sym] = entry
    report["symbols"] = symbols
    report["traded_symbols"] = traded
    report["account_summary"] = _account_summary(symbols, income)
    report["request_count"] = transport.request_count
    return report


def _review_symbol(reader, sym, day, start, end, now, positions, income):
    hist = collect_fills(reader, sym, start_ms=start, end_ms=now, limit=1000, max_pages=40)
    fills_ok = hist["window_traversal_complete"]
    day_fills = [f for f in hist["fills"] if start <= f["time"] <= end]
    initial = None
    if fills_ok:
        current = {}
        for p in positions:
            if p.get("symbol") == sym:
                current[p["positionSide"]] = _dec(p.get("positionAmt")) or Decimal(0)
        delta_since_start = _side_delta(hist["fills"])
        sides = set(delta_since_start) | {s for s, q in current.items() if q != 0}
        initial = {s: format(current.get(s, Decimal(0)) - delta_since_start.get(s, Decimal(0)), "f")
                   for s in sides}
    funding = [r for r in income["rows"] if r.get("symbol") == sym and r.get("incomeType") == "FUNDING_FEE"
               and start <= r["time"] <= end]
    data = {"day": day, "symbol": sym, "source": "live", "fills": day_fills, "funding": funding,
            "fills_complete": fills_ok, "funding_complete": income["complete"],
            "initial": initial or {}, "plan": None}
    try:
        result = review(data)
    except ValueError as exc:
        return {"status": "review_failed", "reason": str(exc), "fills_count": len(day_fills),
                "fill_issues": hist["issues"]}
    result["status"] = "ok"
    result["initial_quantity_method"] = ("derived: position_now - fills_since_day_start"
                                         if initial is not None else "unknown")
    result["fill_issues"] = hist["issues"]
    result["fills_count"] = len(day_fills)
    return result


def _account_summary(symbols, income):
    nets, unknown = [], []
    for sym, e in symbols.items():
        r = e.get("review")
        if not r:
            continue
        if r.get("status") != "ok" or r.get("daily_trading_net_usdt") is None:
            unknown.append(sym)
            continue
        nets.append((sym, Decimal(r["daily_trading_net_usdt"])))
    by_type = {}
    for row in income["rows"]:
        if row.get("asset") == "USDT":
            by_type[row["incomeType"]] = by_type.get(row["incomeType"], Decimal(0)) + (_dec(row["income"]) or 0)
    pnl = by_type.get("REALIZED_PNL", Decimal(0))
    fee = by_type.get("COMMISSION", Decimal(0))
    funding = by_type.get("FUNDING_FEE", Decimal(0))
    return {"realized_pnl_usdt": format(pnl, "f"), "commission_usdt": format(fee, "f"),
            "funding_usdt": format(funding, "f"),
            "trading_net_usdt_from_income": format(pnl + fee + funding, "f"),
            "per_symbol_net_usdt": {s: format(v, "f") for s, v in sorted(nets, key=lambda x: x[1])},
            "symbols_net_unknown": unknown,
            "note": "income 口径：COMMISSION 为负数；只统计 USDT 资产"}


def write_outputs(report, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, report["day"] + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    ann = write_annotation_template(report, os.path.join(out_dir, "标注"))
    return path, ann


def write_annotation_template(report, ann_dir):
    """Create/extend the manual-tag CSV. Never overwrites the user's existing notes."""
    os.makedirs(ann_dir, exist_ok=True)
    path = os.path.join(ann_dir, report["day"] + ".csv")
    existing = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                existing.add((row.get("币种"), row.get("周期ID")))
    new_rows = []
    for sym, e in report["symbols"].items():
        r = e.get("review")
        if not r:
            continue
        cycles = r.get("cycles") or []
        if not cycles:
            key = (sym, "")
            if key not in existing:
                new_rows.append([report["day"], sym, "", "", "", "", "周期未知", "", "", "", "", "", ""])
            continue
        for c in cycles:
            if (sym, c["cycle_id"]) in existing:
                continue
            side = c["position_side"]
            new_rows.append([report["day"], sym, c["cycle_id"], side, _fmt_ms(c["opened_at_ms"]),
                             _fmt_ms(c["closed_at_ms"]), c["status"], c["realized_pnl"],
                             "", "", "", "", ""])
    write_header = not os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig" if write_header else "utf-8", newline="") as fh:
        w = csv.writer(fh)
        if write_header:
            w.writerow(ANNOTATION_HEADER)
        w.writerows(new_rows)
    return path
