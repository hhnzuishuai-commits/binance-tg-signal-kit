"""Offline tests for tg_collect with a fake Telegram client (no network)."""
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
import unittest

import tg_collect

UTC = timezone.utc


class Msg:
    def __init__(self, mid, date, text, edit=None):
        self.id, self.date, self.raw_text, self.edit_date = mid, date, text, edit
        self.media, self.reply_to_msg_id = None, None


class FakeClient:
    def __init__(self, msgs, authorized=True):
        self.msgs, self.authorized, self.calls = msgs, authorized, []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return self.authorized

    async def get_entity(self, cid):
        assert cid == tg_collect.CHAT_ID
        return "entity"

    async def get_dialogs(self):
        pass

    def iter_messages(self, entity, offset_date, reverse, limit, wait_time):
        self.calls.append(offset_date)
        msgs = [m for m in self.msgs if m.date > offset_date][:limit]

        async def gen():
            for m in msgs:
                yield m
        return gen()


def d(day, hour):
    return datetime(2026, 10, day, hour, tzinfo=UTC)


class TgTests(unittest.TestCase):
    def test_collect_groups_by_beijing_day_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, st = os.path.join(tmp, "signals"), os.path.join(tmp, "s.json")
            msgs = [Msg(1, d(18, 15), "做多AUSDT 入场 1"), Msg(2, d(18, 17), "做空BUSDT")]  # 2nd = 10-19 01:00 BJ
            fc = FakeClient(msgs)
            now = datetime(2026, 10, 19, 12, tzinfo=tg_collect.TZ)
            r = tg_collect.collect("1", "h" * 32, "x", out, st, now=now, client_factory=lambda: fc)
            self.assertEqual(r["messages"], 2)
            self.assertEqual(sorted(r["changed_days"]), ["2026-10-18", "2026-10-19"])
            # edit message 1, rerun: original kept, latest refreshed, no duplicate rows
            msgs[0] = Msg(1, d(18, 15), "做多AUSDT 入场 1.1", edit=d(18, 16))
            fc2 = FakeClient(msgs)
            r2 = tg_collect.collect("1", "h" * 32, "x", out, st, now=now + timedelta(hours=1),
                                    client_factory=lambda: fc2)
            self.assertEqual(r2["changed_days"], ["2026-10-18"])
            rows = [json.loads(x) for x in open(os.path.join(out, "2026-10-18.jsonl"), encoding="utf-8")]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["original_text"], "做多AUSDT 入场 1")
            self.assertEqual(rows[0]["latest_text"], "做多AUSDT 入场 1.1")
            # second run starts from last_success - 48h overlap
            self.assertEqual(fc2.calls[0], (now - timedelta(hours=48)).astimezone(UTC))

    def test_unchanged_rerun_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, st = os.path.join(tmp, "signals"), os.path.join(tmp, "s.json")
            msgs = [Msg(1, d(18, 15), "x")]
            now = datetime(2026, 10, 19, 12, tzinfo=tg_collect.TZ)
            tg_collect.collect("1", "h" * 32, "x", out, st, now=now, client_factory=lambda: FakeClient(msgs))
            r = tg_collect.collect("1", "h" * 32, "x", out, st, now=now, client_factory=lambda: FakeClient(msgs))
            self.assertEqual(r["changed_days"], [])

    def test_unauthorized_session_stops_and_keeps_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = os.path.join(tmp, "s.json")
            with self.assertRaises(RuntimeError):
                tg_collect.collect("1", "h" * 32, "x", tmp, st, client_factory=lambda: FakeClient([], False))
            self.assertFalse(os.path.exists(st))

    def test_cap_does_not_advance_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, st = os.path.join(tmp, "signals"), os.path.join(tmp, "s.json")
            old = tg_collect.MAX_MESSAGES
            tg_collect.MAX_MESSAGES = 2
            try:
                msgs = [Msg(i, d(18, i), "m") for i in range(1, 5)]
                r = tg_collect.collect("1", "h" * 32, "x", out, st, client_factory=lambda: FakeClient(msgs))
            finally:
                tg_collect.MAX_MESSAGES = old
            self.assertTrue(r["capped"])
            self.assertNotIn("last_success", json.load(open(st)))


if __name__ == "__main__":
    unittest.main()
