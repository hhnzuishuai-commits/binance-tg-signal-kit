"""Offline tests with a fake Binance. No network, no real keys (fictional values)."""
from datetime import datetime
from decimal import Decimal
import csv
import json
import os
import tempfile
import unittest
from urllib.parse import urlsplit, parse_qs

from account_wide import AccountWideTransport, collect_income, INCOME_PATH
from binance_readonly_transport import MemoryCredentials, ReadOnlyError
from daily_export import build_report, write_outputs
from review_core import TZ

DAY = "2026-10-23"
D0 = int(datetime(2026, 10, 23, tzinfo=TZ).timestamp() * 1000)
NOW = int(datetime(2026, 10, 24, 10, 0, tzinfo=TZ).timestamp() * 1000)
H = 3600_000
CREDS = MemoryCredentials("FAKEKEY00000000", "FAKESECRET000000")
PERMS = {"ipRestrict": True, "enableReading": True, "enableWithdrawals": False,
         "enableInternalTransfer": False, "enableMargin": False, "enableFutures": False,
         "permitsUniversalTransfer": False, "enableVanillaOptions": False,
         "enableFixApiTrade": False, "enableFixReadOnly": False,
         "enableSpotAndMarginTrading": False, "enablePortfolioMarginTrading": False}


def fill(i, sym, t, side, qty, price, pnl="0", fee="0.01", ps="BOTH"):
    return {"id": i, "symbol": sym, "time": t, "side": side, "qty": qty, "price": price,
            "realizedPnl": pnl, "commission": fee, "commissionAsset": "USDT",
            "positionSide": ps, "orderId": 100 + i, "buyer": side == "BUY", "maker": False,
            "quoteQty": "0", "marginAsset": "USDT"}


def income(i, sym, typ, amount, t):
    return {"symbol": sym, "incomeType": typ, "income": amount, "asset": "USDT",
            "info": "", "time": t, "tranId": i, "tradeId": ""}


def world():
    fills = {
        # NEAR: open long 10, TP1 close 6 (60%), close 4 at 22:00 -> one closed cycle
        "NEARUSDT": [fill(1, "NEARUSDT", D0 + 1 * H, "BUY", "10", "2.0"),
                     fill(2, "NEARUSDT", D0 + 3 * H, "SELL", "6", "2.2", pnl="1.2"),
                     fill(3, "NEARUSDT", D0 + 22 * H, "SELL", "4", "2.1", pnl="0.4")],
        # AAVE: short 1 carried in from yesterday, add 1 today; still short 2 now
        "AAVEUSDT": [fill(4, "AAVEUSDT", D0 + 5 * H, "SELL", "1", "150")],
    }
    inc = [income(1, "NEARUSDT", "COMMISSION", "-0.01", D0 + 1 * H),
           income(2, "NEARUSDT", "REALIZED_PNL", "1.2", D0 + 3 * H),
           income(3, "NEARUSDT", "COMMISSION", "-0.01", D0 + 3 * H),
           income(4, "NEARUSDT", "REALIZED_PNL", "0.4", D0 + 22 * H),
           income(5, "NEARUSDT", "COMMISSION", "-0.01", D0 + 22 * H),
           income(6, "AAVEUSDT", "COMMISSION", "-0.01", D0 + 5 * H),
           income(7, "AAVEUSDT", "FUNDING_FEE", "0.05", D0 + 8 * H),
           income(8, "", "TRANSFER", "100", D0 + 9 * H)]
    positions = [{"symbol": "AAVEUSDT", "positionSide": "BOTH", "positionAmt": "-2",
                  "entryPrice": "149", "markPrice": "148", "unRealizedProfit": "2",
                  "liquidationPrice": "200"}]
    orders = [{"symbol": "BTCUSDT", "orderId": 9, "side": "BUY", "positionSide": "BOTH",
               "type": "LIMIT", "price": "60000", "origQty": "0.01", "status": "NEW"}]
    algos = {"AAVEUSDT": [{"symbol": "AAVEUSDT", "algoId": 1, "orderType": "TAKE_PROFIT_MARKET",
                           "triggerPrice": "140", "side": "BUY"}]}
    return fills, inc, positions, orders, algos


class FakeBinance:
    def __init__(self, perms=PERMS, income_cap=1000):
        self.fills, self.income, self.positions, self.orders, self.algos = world()
        self.perms, self.cap, self.log = perms, income_cap, []

    def __call__(self, host, pq, headers):
        assert headers["X-MBX-APIKEY"] == "FAKEKEY00000000"
        u = urlsplit(pq)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        assert "signature" in q
        self.log.append(u.path)
        if u.path == "/sapi/v1/account/apiRestrictions":
            assert host == "api.binance.com"
            body = self.perms
        else:
            assert host == "fapi.binance.com"
            sym = q.get("symbol")
            if u.path == INCOME_PATH:
                lo, hi, lim = int(q["startTime"]), int(q["endTime"]), int(q["limit"])
                body = [r for r in self.income if lo <= r["time"] <= hi][:min(lim, self.cap)]
            elif u.path == "/fapi/v3/positionRisk":
                body = self.positions
            elif u.path == "/fapi/v1/openOrders" and sym is None:
                body = self.orders
            elif u.path == "/fapi/v1/openAlgoOrders":
                body = self.algos.get(sym, [])
            elif u.path == "/fapi/v1/userTrades":
                lo, hi = int(q["startTime"]), int(q["endTime"])
                body = [f for f in self.fills.get(sym, []) if lo <= f["time"] <= hi]
            else:
                return 404, b'{"code":-1,"msg":"x"}'
        return 200, json.dumps(body).encode()


def transport(fake):
    return AccountWideTransport(CREDS, live_authorized=True, clock_ms=lambda: NOW, send=fake)


class DailyExportTests(unittest.TestCase):
    def test_full_day_report(self):
        fake = FakeBinance()
        r = build_report(transport(fake), DAY, clock_ms=lambda: NOW)
        self.assertEqual(r["traded_symbols"], ["AAVEUSDT", "NEARUSDT"])
        self.assertEqual(set(r["symbols"]), {"AAVEUSDT", "BTCUSDT", "NEARUSDT"})
        self.assertFalse(r["symbols"]["BTCUSDT"]["traded_today"])
        near = r["symbols"]["NEARUSDT"]["review"]
        self.assertEqual(near["status"], "ok")
        self.assertEqual(near["daily_trading_net_usdt"], "1.57")
        [cycle] = near["cycles"]
        self.assertEqual(cycle["status"], "closed")
        self.assertEqual([e["kind"] for e in cycle["events"]], ["open", "partial_close", "full_close"])
        aave = r["symbols"]["AAVEUSDT"]["review"]
        self.assertEqual(aave["ending_quantity"], {"BOTH": "-2"})
        self.assertEqual(aave["cycles"][0]["status"], "carried_in")
        self.assertEqual(aave["daily_trading_net_usdt"], "0.04")
        s = r["account_summary"]
        self.assertEqual(s["trading_net_usdt_from_income"], "1.61")
        self.assertEqual(r["income_by_type_asset"]["TRANSFER USDT"], "100")
        self.assertEqual(r["symbols"]["AAVEUSDT"]["algo_orders_now"][0]["triggerPrice"], "140")
        self.assertTrue(r["day_complete"])
        # only allowlisted read paths were used
        self.assertTrue(set(fake.log) <= {"/sapi/v1/account/apiRestrictions", INCOME_PATH,
            "/fapi/v3/positionRisk", "/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders",
            "/fapi/v1/userTrades"})
        self.assertEqual(fake.log.count("/sapi/v1/account/apiRestrictions"), 1)

    def test_write_permission_key_refused_before_any_account_read(self):
        fake = FakeBinance(perms={**PERMS, "enableFutures": True})
        with self.assertRaises(ReadOnlyError) as ctx:
            build_report(transport(fake), DAY, clock_ms=lambda: NOW)
        self.assertEqual(str(ctx.exception), "write_permission_or_unknown_permission")
        self.assertEqual(fake.log, ["/sapi/v1/account/apiRestrictions"])

    def test_no_ip_restriction_refused(self):
        fake = FakeBinance(perms={**PERMS, "ipRestrict": False})
        with self.assertRaises(ReadOnlyError):
            build_report(transport(fake), DAY, clock_ms=lambda: NOW)

    def test_reads_refused_without_permission_check(self):
        t = transport(FakeBinance())
        with self.assertRaises(ReadOnlyError):
            t.wide("/fapi/v3/positionRisk")

    def test_unlisted_path_refused(self):
        t = transport(FakeBinance()); t.check_permissions()
        for path in ("/fapi/v1/order", "/fapi/v1/leverage", "/sapi/v1/capital/withdraw/apply"):
            with self.assertRaises(ReadOnlyError):
                t.wide(path)

    def test_income_saturation_splits_window(self):
        fake = FakeBinance()
        t = transport(fake); t.check_permissions()
        res = collect_income(t, start_ms=D0, end_ms=D0 + 24 * H - 1, limit=3)
        self.assertTrue(res["complete"])
        self.assertEqual(len(res["rows"]), 8)

    def test_income_saturation_reported_honestly_when_budget_runs_out(self):
        fake = FakeBinance()
        t = transport(fake); t.check_permissions()
        res = collect_income(t, start_ms=D0, end_ms=D0 + 24 * H - 1, max_pages=2, limit=1)
        self.assertFalse(res["complete"])

    def test_future_and_too_old_days_refused(self):
        with self.assertRaises(ValueError):
            build_report(transport(FakeBinance()), "2026-10-25", clock_ms=lambda: NOW)
        with self.assertRaises(ValueError):
            build_report(transport(FakeBinance()), "2026-06-01", clock_ms=lambda: NOW)

    def test_secret_never_in_output(self):
        r = build_report(transport(FakeBinance()), DAY, clock_ms=lambda: NOW)
        text = json.dumps(r)
        self.assertNotIn("FAKESECRET", text)
        self.assertNotIn("FAKEKEY", text)
        self.assertNotIn("FAKE", repr(transport(FakeBinance())))

    def test_outputs_and_annotations_never_overwritten(self):
        r = build_report(transport(FakeBinance()), DAY, clock_ms=lambda: NOW)
        with tempfile.TemporaryDirectory() as d:
            path, ann = write_outputs(r, d)
            self.assertEqual(json.load(open(path, encoding="utf-8"))["day"], DAY)
            rows = list(csv.reader(open(ann, encoding="utf-8-sig")))
            self.assertEqual(len(rows), 3)  # header + NEAR cycle + AAVE cycle
            # user fills in a rating, then re-export: the note must survive, no dup rows
            rows[1][8] = "B+"
            with open(ann, "w", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh).writerows(rows)
            write_outputs(r, d)
            again = list(csv.reader(open(ann, encoding="utf-8-sig")))
            self.assertEqual(len(again), 3)
            self.assertEqual(again[1][8], "B+")


if __name__ == "__main__":
    unittest.main()
