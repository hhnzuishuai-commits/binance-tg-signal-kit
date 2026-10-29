"""Offline tests for secure storage, GitHub upload and the unattended run."""
import base64
from datetime import datetime
import json
import os
import tempfile
import unittest

import auto_run
from github_upload import GitHubUploader, UploadError
import secure_store
from review_core import TZ
from test_daily_export import FakeBinance, NOW, transport as fake_transport
from account_wide import AccountWideTransport

TOKEN = "github_pat_FAKE0000000000000000000000"
SECRETS = {"binance_api_key": "FAKEKEY00000000", "binance_secret": "FAKESECRET000000",
           "github_token": TOKEN, "github_repo": "someone/trading-review"}


class FakeGitHub:
    def __init__(self, status_on_put=None):
        self.files, self.calls, self.status_on_put = {}, [], status_on_put

    def __call__(self, method, path, headers, body):
        assert headers["Authorization"] == "Bearer " + TOKEN
        self.calls.append((method, path))
        name = path.split("/contents/")[1]
        if method == "GET":
            if name not in self.files:
                return 404, b"{}"
            return 200, json.dumps({"sha": "abc", "content": base64.b64encode(self.files[name]).decode()}).encode()
        if self.status_on_put:
            return self.status_on_put, b"{}"
        data = json.loads(body)
        assert TOKEN not in body.decode()
        self.files[name] = base64.b64decode(data["content"])
        return (200 if "sha" in data else 201), b"{}"


class SecureStoreTests(unittest.TestCase):
    def test_roundtrip_and_ciphertext_has_no_plaintext(self):
        flip = lambda b: bytes(x ^ 0x5A for x in b)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.bin")
            secure_store.save_secrets(SECRETS, path=p, protect=flip)
            raw = open(p, "rb").read()
            self.assertNotIn(b"FAKESECRET", raw)
            self.assertEqual(secure_store.load_secrets(path=p, unprotect=flip), SECRETS)

    def test_incomplete_refused(self):
        with self.assertRaises(ValueError):
            secure_store.save_secrets({"binance_api_key": "x"}, path="unused", protect=lambda b: b)


class UploaderTests(unittest.TestCase):
    def test_create_update_unchanged(self):
        gh = FakeGitHub()
        up = GitHubUploader(TOKEN, "someone/trading-review", send=gh)
        self.assertEqual(up.put_file("data/2026-10-23.json", b"a", "m"), "created")
        self.assertEqual(up.put_file("data/2026-10-23.json", b"b", "m"), "updated")
        self.assertEqual(up.put_file("data/2026-10-23.json", b"b", "m"), "unchanged")
        self.assertNotIn(TOKEN, repr(up))

    def test_only_data_and_status_paths(self):
        up = GitHubUploader(TOKEN, "someone/trading-review", send=FakeGitHub())
        for bad in ("reviews/x.md", "../x.json", "data/../../x.json", ".github/workflows/x.json"):
            with self.assertRaises(UploadError):
                up.put_file(bad, b"x", "m")

    def test_expired_token_reported(self):
        up = GitHubUploader(TOKEN, "someone/trading-review", send=FakeGitHub(status_on_put=401))
        with self.assertRaises(UploadError) as ctx:
            up.put_file("data/x.json", b"x", "m")
        self.assertEqual(str(ctx.exception), "github_token_invalid_or_expired")


class AutoRunTests(unittest.TestCase):
    def _run(self, mode, gh, state=None, fake=None, **kw):
        fake = fake or FakeBinance()
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "state.json")
            if state:
                json.dump(state, open(sp, "w"))
            status = auto_run.run(mode, secrets=SECRETS,
                make_transport=lambda c: AccountWideTransport(c, live_authorized=True,
                                                              clock_ms=lambda: NOW, send=fake),
                make_uploader=lambda t, r: GitHubUploader(t, r, send=gh),
                now=datetime.fromtimestamp(NOW / 1000, TZ), clock_ms=lambda: NOW,
                local_out=os.path.join(d, "out"), state_path=sp, **kw)
            saved = json.load(open(sp)) if os.path.exists(sp) else {}
        return status, saved

    def test_tonight_uploads_past_week_final_and_today_partial(self):
        gh = FakeGitHub()
        status, state = self._run("tonight", gh)
        self.assertTrue(status["ok"], status)
        self.assertIn("data/2026-10-23.json", gh.files)
        self.assertIn("data/2026-10-24.json", gh.files)
        self.assertEqual(state["2026-10-23"], "final")
        self.assertEqual(state["2026-10-24"], "partial")
        self.assertFalse(json.loads(gh.files["data/2026-10-24.json"])["day_complete"])
        self.assertIn("status/last_run.json", gh.files)
        self.assertEqual(len([k for k in gh.files if k.startswith("data/")]), 8)

    def test_catchup_skips_final_days_and_finalises_partial(self):
        gh = FakeGitHub()
        state = {f"2026-10-{d}": "final" for d in range(17, 23)}
        state["2026-10-23"] = "partial"
        status, saved = self._run("catchup", gh, state)
        self.assertEqual([r["day"] for r in status["results"]], ["2026-10-23"])
        self.assertEqual(saved["2026-10-23"], "final")
        self.assertNotIn("data/2026-10-24.json", gh.files)

    def test_bad_key_stops_and_reports_without_secrets(self):
        from test_daily_export import PERMS
        gh = FakeGitHub()
        status, _ = self._run("tonight", gh, fake=FakeBinance(perms={**PERMS, "enableFutures": True}))
        self.assertFalse(status["ok"])
        self.assertEqual(len(status["results"]), 1)
        text = gh.files["status/last_run.json"].decode()
        self.assertIn("write_permission", text)
        for s in ("FAKEKEY", "FAKESECRET", TOKEN):
            self.assertNotIn(s, text)


class SignalsModeTests(AutoRunTests):
    def test_signals_skips_binance_and_writes_own_status(self):
        gh = FakeGitHub()
        status, _ = self._run("signals", gh, tg_runner=lambda up: {"messages": 3, "changed_days": []})
        self.assertEqual(status["results"], [])
        self.assertTrue(status["ok"])
        self.assertIn("status/last_signals.json", gh.files)
        self.assertNotIn("status/last_run.json", gh.files)
        self.assertFalse([k for k in gh.files if k.startswith("data/")])


class RetryTests(unittest.TestCase):
    def test_transport_error_retried_then_succeeds(self):
        gh = FakeGitHub()
        good = FakeBinance()
        calls = {"n": 0}

        def flaky(host, pq, headers):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise OSError("network down")
            return good(host, pq, headers)
        waits = []
        with tempfile.TemporaryDirectory() as d:
            status = auto_run.run("catchup", secrets=SECRETS,
                make_transport=lambda c: AccountWideTransport(c, live_authorized=True, clock_ms=lambda: NOW, send=flaky),
                make_uploader=lambda t, r: GitHubUploader(t, r, send=gh),
                now=datetime.fromtimestamp(NOW / 1000, TZ), clock_ms=lambda: NOW,
                local_out=os.path.join(d, "out"), state_path=os.path.join(d, "s.json"),
                start=None, end=None, sleep=waits.append)
        self.assertTrue(status["results"][0].get("upload"))
        self.assertEqual(len(waits), 2)


class BackfillTests(AutoRunTests):
    def test_backfill_range_skips_final_and_today(self):
        gh = FakeGitHub()
        status, state = self._run("backfill", gh, state={"2026-10-21": "final"},
                                  start="2026-10-19", end="2026-10-24")
        self.assertEqual([r["day"] for r in status["results"]],
                         ["2026-10-19", "2026-10-20", "2026-10-22", "2026-10-23"])
        self.assertTrue(status["ok"])

    def test_backfill_bad_range(self):
        with self.assertRaises(ValueError):
            auto_run.days_to_run("backfill", datetime.fromtimestamp(NOW / 1000, TZ), {}, "2026-10-20", "2026-10-10")


if __name__ == "__main__":
    unittest.main()
