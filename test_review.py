import unittest
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch
from review_core import review, day_window

START, END = day_window("2026-10-21")

def fill(i, side, qty, pnl="0", ps="BOTH", fee="0.1", asset="USDT"):
    return dict(id=i, time=START+i, symbol="NEARUSDT", side=side, qty=qty,
                price="3.25", realizedPnl=pnl, positionSide=ps, commission=fee, commissionAsset=asset)

def envelope(rows=(), **kw):
    d = dict(day="2026-10-21", symbol="NEARUSDT", source="fixture", initial={"BOTH":"0"},
             fills=list(rows), funding=[], fills_complete=True, funding_complete=True)
    d.update(kw)
    return d

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch("socket.socket", side_effect=AssertionError("network forbidden"))
        self.guard.start(); self.addCleanup(self.guard.stop)

    def test_day_timezone(self):
        self.assertEqual(END-START+1, 86400000)
        from datetime import datetime, timezone
        self.assertEqual(datetime.fromtimestamp(START/1000, timezone.utc).isoformat(), "2026-10-20T16:00:00+00:00")

    def test_partial_full_and_plan_preserved(self):
        d = envelope([fill(1,"BUY","4"),fill(2,"SELL","2","1"),fill(3,"SELL","1","2"),fill(4,"SELL","1","3")],
                     plan={"original_qty":"4", "tp_percent_original":[50,25,25]})
        before = deepcopy(d); r=review(d)
        self.assertEqual([e["kind"] for e in r["events"]],["open","partial_close","partial_close","full_close"])
        self.assertEqual(r["cycles"][0]["status"],"closed")
        self.assertEqual(Decimal(r["daily_trading_net_usdt"]),Decimal("5.6"))
        self.assertIsNone(r["cycles"][0]["net_pnl"])
        self.assertEqual(d,before); self.assertEqual(r["plan"],d["plan"])

    def test_add_and_short(self):
        r=review(envelope([fill(1,"SELL","2"),fill(2,"SELL","1"),fill(3,"BUY","3","2")]))
        self.assertEqual([e["kind"] for e in r["events"]],["open","increase","full_close"])

    def test_reversal_splits_fee_and_pnl(self):
        r=review(envelope([fill(1,"BUY","2"),fill(2,"SELL","3","4",fee="0.3")]))
        self.assertEqual(len(r["cycles"]),2)
        self.assertEqual(r["ending_quantity"]["BOTH"],"-1")
        self.assertEqual(Decimal(r["cycles"][0]["fees"]["USDT"]),Decimal("0.3"))
        self.assertEqual(r["cycles"][1]["realized_pnl"],"0")

    def test_hedge_sides_separate(self):
        r=review(envelope([fill(1,"BUY","1",ps="LONG"),fill(2,"SELL","2",ps="SHORT")],initial={"LONG":"0","SHORT":"0"}))
        self.assertEqual(r["ending_quantity"],{"LONG":"1","SHORT":"-2"})

    def test_carried_position_not_complete_cycle(self):
        r=review(envelope([fill(1,"SELL","2","1")],initial={"BOTH":"2"}))
        self.assertEqual(r["cycles"][0]["status"],"closed_history_incomplete")
        self.assertIsNone(r["cycles"][0]["opened_at_ms"])

    def test_unknown_initial_no_invented_cycles(self):
        r=review(envelope([fill(1,"SELL","2","1")],initial={}))
        self.assertEqual(r["cycles"],[]); self.assertEqual(r["events"][0]["kind"],"unknown")

    def test_truncated_blocks_reconstruction_and_net(self):
        r=review(envelope([fill(1,"BUY","2")],fills_complete=False))
        self.assertEqual(r["cycles"],[]); self.assertIsNone(r["daily_trading_net_usdt"])

    def test_funding_dedup_and_signed_amount(self):
        f=dict(symbol="NEARUSDT",incomeType="FUNDING_FEE",tranId=1,time=START,asset="USDT",income="-0.2")
        r=review(envelope(funding=[f,deepcopy(f)]))
        self.assertEqual(r["daily_trading_net_usdt"],"-0.2")
        self.assertEqual(review(envelope(funding=[f,{**f,"tranId":2,"income":"0.3"}]))["daily_trading_net_usdt"],"0.1")

    def test_funding_missing_and_foreign_fee(self):
        self.assertIsNone(review(envelope(funding_complete=False))["daily_trading_net_usdt"])
        self.assertIsNone(review(envelope([fill(1,"BUY","1",asset="BNB")]))["daily_trading_net_usdt"])

    def test_duplicate_and_conflict(self):
        f=fill(1,"BUY","1")
        self.assertEqual(len(review(envelope([f,f]))["events"]),1)
        with self.assertRaises(ValueError): review(envelope([f,{**f,"qty":"2"}]))

    def test_funding_conflict(self):
        f=dict(symbol="NEARUSDT",incomeType="FUNDING_FEE",tranId=1,time=START,asset="USDT",income="1")
        with self.assertRaises(ValueError): review(envelope(funding=[f,{**f,"income":"2"}]))

    def test_bad_amounts(self):
        for v in ["NaN","Infinity","1e100","0","-1",True]:
            with self.subTest(v=v), self.assertRaises(ValueError): review(envelope([fill(1,"BUY",v)]))

    def test_modes_and_overclose(self):
        with self.assertRaises(ValueError): review(envelope([fill(1,"SELL","1",ps="LONG")],initial={"LONG":"0"}))
        with self.assertRaises(ValueError): review(envelope([fill(1,"BUY","1",ps="LONG")]))

    def test_sort_and_zero_pnl_close(self):
        r=review(envelope([fill(2,"SELL","1"),fill(1,"BUY","1")]))
        self.assertEqual(r["cycles"][0]["status"],"closed")

    def test_wrong_symbol_and_outside_window(self):
        for changes in [{"symbol":"BTCUSDT"},{"time":END+1},{"time":START-1}]:
            with self.assertRaises(ValueError): review(envelope([{**fill(1,"BUY","1"),**changes}]))

    def test_fixture_provenance(self):
        self.assertIn("mock_not_account_evidence",review(envelope())["issues"])
        with self.assertRaises(ValueError): review(envelope(source="unknown"))

    def test_reopened_cycles(self):
        r=review(envelope([fill(1,"BUY","1"),fill(2,"SELL","1"),fill(3,"BUY","1")]))
        self.assertEqual([c["status"] for c in r["cycles"]],["closed","open"])
        self.assertEqual(r["plan_execution_match"],"not_inferred")

    def test_missing_rows_not_empty_success(self):
        for key in ("fills","funding"):
            d=envelope(); del d[key]
            with self.assertRaises(ValueError): review(d)

    def test_pure_no_file_or_network_io(self):
        with patch("builtins.open",side_effect=AssertionError("file IO forbidden")):
            review(envelope([fill(1,"BUY","1")]))

    def test_aave_and_partial_plan_mismatch(self):
        rows=[{**fill(1,"BUY","4"),"symbol":"AAVEUSDT"},{**fill(2,"SELL","1"),"symbol":"AAVEUSDT"}]
        r=review(envelope(rows,symbol="AAVEUSDT",plan={"tp_percent_original":[50,25,25]}))
        self.assertEqual(r["events"][1]["qty"],"1")
        self.assertEqual(r["plan_execution_match"],"not_inferred")

if __name__ == "__main__": unittest.main()
