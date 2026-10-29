"""Offline tests for signal_listener (no Telegram, no network). Needs the repo's tool/ on SIG_TOOL."""
from datetime import datetime, timezone
import json
import os
import tempfile
import unittest
from unittest import mock

import signal_listener as sl

if sl.HAVE_TAGS:
    from test_v5_tags import ZEC, INTC
else:
    ZEC = INTC = ""
needs_tags = unittest.skipUnless(sl.HAVE_TAGS, "signal tagging scripts not on SIG_TOOL (only needed for --push)")

T0 = datetime(2026, 10, 25, 9, 30, 5, tzinfo=sl.TZ).timestamp()   # a :30 burst
ZEC_OK = ZEC.replace("多周期共振多 |顺多|", "多周期共振空 |顺空|").replace("放量疑似新增多头推动↑\n", "")


def rec(mid, text, ts, latest=None):
    iso = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    return {"chat_id": sl.CHAT_ID, "message_id": mid, "message_date": iso, "edit_date": None,
            "original_text": text, "latest_text": latest or text, "first_collected_at": iso}


def sym(text, symbol):
    return text.replace("ZECUSDT", symbol).replace("INTCUSDT", symbol)


@needs_tags
class FormatTests(unittest.TestCase):
    def test_zec_is_skip_and_formats(self):
        r = sl.Router()
        x = r.add(rec(1, ZEC, T0), T0)
        self.assertEqual(x["v5"]["level"], "不做")
        text = sl.format_signal(x, {})
        lines = text.split("\n")
        self.assertEqual(lines[0], "🔴空 ZECUSDT｜震荡｜22x")
        self.assertEqual(lines[1], "V3.2：未过｜V5：⛔ 大周期环境反向；市场行为反向")   # short with tech +0.43
        self.assertEqual(lines[2], "入场 1192.58｜止损 1245.93（距离 4.47%）｜TP1 1139.23 / TP2 1103.66")
        self.assertEqual(len(lines), 3)              # no star / slippage / position marks

    def test_intc_trend_long_with_marks(self):
        r = sl.Router()
        x = r.add(rec(2, INTC, T0), T0)
        self.assertEqual(x["v5"]["level"], "可看")
        text = sl.format_signal(x, {"INTCUSDT": {"SHORT"}})
        self.assertIn("🟢多 INTCUSDT｜趋势｜37x", text)
        self.assertIn("V3.2：过门槛｜V5：✅", text)
        self.assertIn("TP1 128.49 / TP2 131.84 / TP3 135.19", text)
        self.assertIn("⚠️ 美股合约，滑点风险", text)
        self.assertIn("📌 ⚠️ 与你的持仓方向相反", text)
        self.assertNotIn("⭐", text)

    def test_star_and_tight_stop_and_same_side(self):
        x = sl.Router().add(rec(3, sym(INTC, "AMDUSDT").replace("121.79", "124.50"), T0), T0)
        text = sl.format_signal(x, {"AMDUSDT": {"LONG"}})
        self.assertIn("⭐ 高胜率名单", text)
        self.assertIn("⚠️ 美股合约 / 止损 <1%，滑点风险", text)
        self.assertIn("📌 已有同向仓", text)

    def test_chat_message_ignored(self):
        self.assertIsNone(sl.Router().add(rec(4, "大家注意风险", T0), T0))


@needs_tags
class RoutingTests(unittest.TestCase):
    def test_burst_merged_until_quiet(self):
        r = sl.Router()
        r.add(rec(10, INTC, T0), T0)
        r.add(rec(11, sym(INTC, "AMDUSDT"), T0 + 40), T0 + 40)
        self.assertEqual(r.due(T0 + 70), [])                  # only 30 s quiet so far
        r.add(rec(12, sym(ZEC_OK, "ETHUSDT"), T0 + 90), T0 + 90)
        msgs = r.due(T0 + 150)
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0].startswith("📣 新信号 3 条（09:30）"))
        self.assertEqual(msgs[0].count("\n\n"), 4)            # header | 3 signal blocks | footer
        self.assertTrue(msgs[0].endswith(sl.FOOTER))
        self.assertEqual(r.due(T0 + 300), [])

    def test_long_burst_capped_at_three_minutes(self):
        r = sl.Router()
        for i in range(8):                                     # one every 30 s
            r.add(rec(20 + i, sym(INTC, f"A{i}USDT"), T0 + 30 * i), T0 + 30 * i)
            if T0 + 30 * i - T0 >= sl.MERGE_MAX:
                break
        msgs = r.due(T0 + sl.MERGE_MAX)
        self.assertEqual(len(msgs), 1)

    def test_skip_not_pushed_but_in_hourly_summary(self):
        r = sl.Router()
        r.due(T0)                                              # starts the hour clock
        r.add(rec(30, ZEC, T0), T0)
        r.add(rec(31, sym(ZEC, "SOLUSDT"), T0 + 5), T0 + 5)
        self.assertEqual(r.due(T0 + 120), [])
        top = (int(T0 // 3600) + 1) * 3600
        self.assertEqual(r.due(top), ["过去 1 小时 ⛔ 2 条：ZECUSDT SOLUSDT"])
        self.assertEqual(r.due(top + 3600), [])                # 0 -> nothing sent

    def test_dedupe_edit_and_restart(self):
        r = sl.Router()
        r.add(rec(40, INTC, T0), T0)
        self.assertIsNone(r.add(rec(40, INTC, T0, latest=INTC + "\n已更新"), T0 + 10))   # edit / poll duplicate
        self.assertEqual(len(r.due(T0 + 100)), 1)
        r2 = sl.Router(json.loads(json.dumps(r.state())))       # restart from saved state
        self.assertIsNone(r2.add(rec(40, INTC, T0), T0 + 200))
        self.assertEqual(r2.due(T0 + 400), [])

    def test_backlog_not_fresh_is_silent(self):
        r = sl.Router()
        x = r.add(rec(50, INTC, T0), T0 + 3600, fresh=False)
        self.assertIsNotNone(x)
        self.assertEqual(r.due(T0 + 4000), [])

    def test_cluster_uses_history_and_new(self):
        r = sl.Router()
        for i in range(5):
            r.remember(rec(60 + i, sym(ZEC_OK, f"S{i}USDT"), T0 - 3000 + i * 60))
        for i in range(3):
            r.add(rec(70 + i, sym(ZEC_OK, f"N{i}USDT"), T0 + i), T0 + i)
        x = r.add(rec(80, sym(ZEC_OK, "LASTUSDT"), T0 + 10), T0 + 10)
        self.assertEqual(x["v5"]["cluster2h"], 8)
        self.assertEqual(x["v5"]["level"], "降级")


class PositionAndHealthTests(unittest.TestCase):
    def test_position_sides_reads_export(self):
        with tempfile.TemporaryDirectory() as d:
            json.dump({"symbols": {"XMRUSDT": {"position_now": [
                {"positionSide": "BOTH", "positionAmt": "-1.5"}]},
                "BTCUSDT": {"position_now": [{"positionSide": "LONG", "positionAmt": "0"}]}}},
                open(os.path.join(d, "2026-10-25.json"), "w"))
            self.assertEqual(sl.position_sides(d, "2026-10-25"), {"XMRUSDT": {"SHORT"}})
            self.assertEqual(sl.position_sides(d, "2026-10-26"), {})

    def test_health_alerts_once_and_recovery(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(sl, "STATUS_PATH", os.path.join(d, "s.json")), \
                mock.patch.object(sl, "ALERT_PATH", os.path.join(d, "a.json")):
            sent = []
            sl._save_json(sl.STATUS_PATH, {"heartbeat_ts": T0})
            self.assertEqual(sl.health_check(T0 + 60, send=sent.append)["listener"], "ok")
            out = sl.health_check(T0 + 2000, send=sent.append)
            self.assertEqual(out["error"], "no_heartbeat_30min")
            sl.health_check(T0 + 2900, send=sent.append)          # still down: no repeat
            sl._save_json(sl.STATUS_PATH, {"heartbeat_ts": T0, "error": "session_not_authorized"})
            sl.health_check(T0 + 3000, send=sent.append)
            sl._save_json(sl.STATUS_PATH, {"heartbeat_ts": T0 + 3500})
            sl.health_check(T0 + 3600, send=sent.append)
            self.assertEqual(len(sent), 3)
            self.assertTrue(sent[0].startswith("⚠️ 信号监听掉线"))
            self.assertIn("重新登录", sent[1])
            self.assertEqual(sent[2], "✅ 信号监听已恢复")


@needs_tags
class DryRunTests(unittest.TestCase):
    def test_dry_run_last_five(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [rec(100 + i, sym(INTC if i % 2 else ZEC, f"C{i}USDT"), T0 + i * 20) for i in range(7)]
            rows.append(rec(200, "聊天消息", T0 + 200))
            with open(os.path.join(d, "2026-10-25.jsonl"), "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            routes, msgs = sl.dry_run("2026-10-25", 5, local_out=d, signals_dir=d)
            self.assertEqual(len(routes), 5)
            self.assertTrue(routes[0].startswith("09:30 C2USDT"))
            self.assertTrue(any(m.startswith("📣 新信号 2 条") for m in msgs))
            self.assertTrue(any(m.startswith("过去 1 小时 ⛔ 3 条") for m in msgs))


if __name__ == "__main__":
    unittest.main()
