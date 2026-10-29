"""Unattended run, started by Windows Task Scheduler.

--mode tonight : catch up missing past days, then export TODAY so far (partial)
--mode catchup : export any of the last 7 days not yet uploaded as final
--mode signals : Telegram signals only (laptop, after Binance moved to the VPS);
                 status goes to status/last_signals.json
When listener.enabled exists (signal_listener.py runs under systemd and owns the
Telegram session) the Telegram step is only a health check of the listener.

Writes local copies to Documents\\交易复盘 and uploads data/YYYY-MM-DD.json
plus status/last_run.json to the private GitHub repository. Logs contain no
secrets. Never places, cancels or modifies orders.
"""
import argparse
from datetime import datetime, timedelta
import json
import os
import sys
import traceback

from account_wide import AccountWideTransport
from binance_readonly_transport import MemoryCredentials, ReadOnlyError
from daily_export import build_report, write_outputs
from github_upload import GitHubUploader, UploadError
from review_core import TZ
import secure_store

LOCAL_OUT = os.path.join(os.path.expanduser("~"), "Documents", "交易复盘")
STATE_PATH = os.path.join(secure_store.APP_DIR, "state.json")
LOG_PATH = os.path.join(secure_store.APP_DIR, "auto_run.log")
CATCHUP_DAYS = 7
RETRIES = 3            # network to Binance (VPN) may be down for a moment
RETRY_WAIT = 120       # seconds between attempts
RETRYABLE = {"transport_or_decode_error", "rate_limited_no_retry", "http_error_no_retry"}


def _now_ms():
    return int(datetime.now(TZ).timestamp() * 1000)


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(state, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def days_to_run(mode, now, state, start=None, end=None):
    today = now.strftime("%Y-%m-%d")
    if mode == "backfill":
        d0 = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=TZ)
        d1 = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=TZ)
        if d1 < d0 or (d1 - d0).days > 60:
            raise ValueError("backfill range must be 1-61 days")
        days = []
        while d0 <= d1:
            d = d0.strftime("%Y-%m-%d")
            if d < today and state.get(d) != "final":
                days.append(d)
            d0 += timedelta(days=1)
        return days
    days = []
    for back in range(CATCHUP_DAYS, 0, -1):
        d = (now - timedelta(days=back)).strftime("%Y-%m-%d")
        if state.get(d) != "final":
            days.append(d)
    if mode == "tonight":
        days.append(today)
    return days


def run(mode, *, secrets, make_transport, make_uploader, now, clock_ms, local_out, state_path,
        start=None, end=None, progress=None, tg_runner=None, sleep=None):
    import time
    sleep = sleep or time.sleep
    state = _load_state(state_path)
    uploader = make_uploader(secrets["github_token"], secrets["github_repo"])
    results = []
    tg_status = None
    if tg_runner is not None and mode != "backfill":
        try:
            tg_status = tg_runner(uploader)
        except Exception as exc:  # never block the Binance export
            tg_status = {"error": type(exc).__name__ + ":" + str(exc)[:200]}
    days = [] if mode == "signals" else days_to_run(mode, now, state, start, end)
    for day in days:
        if progress:
            progress(day)
        entry = {"day": day}
        try:
            creds = MemoryCredentials(secrets["binance_api_key"], secrets["binance_secret"])
            for attempt in range(RETRIES + 1):
                try:
                    report = build_report(make_transport(creds), day, clock_ms=clock_ms)
                    break
                except ReadOnlyError as exc:
                    if str(exc) not in RETRYABLE or attempt == RETRIES:
                        raise
                    entry.setdefault("retries", []).append(str(exc))
                    sleep(RETRY_WAIT)
            write_outputs(report, local_out)
            body = json.dumps(report, ensure_ascii=False, indent=1).encode("utf-8")
            kind = "final" if report["day_complete"] else "partial"
            entry["upload"] = uploader.put_file(f"data/{day}.json", body,
                                                f"data {day} ({kind}, exported {report['exported_at']})")
            entry["kind"] = kind
            entry["traded"] = report["traded_symbols"]
            entry["net_usdt"] = report["account_summary"]["trading_net_usdt_from_income"]
            if report["issues"]:
                entry["issues"] = report["issues"]
            state[day] = kind
            _save_state(state, state_path)
        except ReadOnlyError as exc:
            entry["error"] = "binance:" + str(exc)
        except UploadError as exc:
            entry["error"] = "github:" + str(exc)
        except ValueError as exc:
            entry["error"] = "value:" + str(exc)
        results.append(entry)
        if entry.get("error", "").startswith(("github:github_token", "binance:authentication",
                                               "binance:write_permission", "binance:permission")):
            break  # credential problem: stop instead of repeating failed requests
    status = {"mode": mode, "finished_at": now.strftime("%Y-%m-%d %H:%M:%S"),
              "timezone": "UTC+08:00", "results": results,
              "ok": all("error" not in r for r in results)}
    if tg_status is not None:
        status["telegram"] = tg_status
        if mode == "signals":
            status["ok"] = "error" not in tg_status
    try:
        uploader.put_file("status/last_signals.json" if mode == "signals" else "status/last_run.json",
                          json.dumps(status, ensure_ascii=False, indent=1).encode("utf-8"),
                          f"status {status['finished_at']}")
    except UploadError as exc:
        status["status_upload_error"] = str(exc)
    return status


SIGNALS_OUT = os.path.join(LOCAL_OUT, "signals")
TG_SESSION = os.path.join(secure_store.APP_DIR, "tg_copy.session")
TG_STATE = os.path.join(secure_store.APP_DIR, "tg_state.json")


def telegram_runner(uploader):
    import tg_collect
    tg = secure_store.load_tg()
    res = tg_collect.collect(tg["api_id"], tg["api_hash"], TG_SESSION, SIGNALS_OUT, TG_STATE)
    uploaded = []
    for day in res["changed_days"]:
        with open(os.path.join(SIGNALS_OUT, day + ".jsonl"), "rb") as fh:
            r = uploader.put_file(f"signals/{day}.jsonl", fh.read(), f"signals {day}")
        uploaded.append(f"{day}:{r}")
    res["uploaded"] = uploaded
    return res


LISTENER_MARK = os.path.join(secure_store.APP_DIR, "listener.enabled")


def listener_runner(uploader):
    """signal_listener owns the Telegram session: only check its health (and alert on the bot)."""
    import signal_listener
    return signal_listener.health_check()


def _log(line):
    os.makedirs(secure_store.APP_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["tonight", "catchup", "backfill", "signals"], required=True)
    ap.add_argument("--start")
    ap.add_argument("--end")
    args = ap.parse_args(argv)
    verbose = args.mode == "backfill"
    try:
        secrets = secure_store.load_secrets()
    except Exception as exc:
        _log("cannot load secrets (" + type(exc).__name__ + "); run setup_auto again")
        return 2
    if args.mode == "signals" and not os.path.exists(secure_store.TG_PATH):
        _log("signals mode but Telegram is not set up; run setup_telegram.bat")
        return 2
    try:
        status = run(args.mode, secrets=secrets,
                     make_transport=lambda c: AccountWideTransport(c, live_authorized=True),
                     make_uploader=lambda t, r: GitHubUploader(t, r),
                     now=datetime.now(TZ), clock_ms=_now_ms, local_out=LOCAL_OUT, state_path=STATE_PATH,
                     start=args.start, end=args.end,
                     tg_runner=(listener_runner if os.path.exists(LISTENER_MARK) else
                                telegram_runner if os.path.exists(secure_store.TG_PATH) else None),
                     progress=(lambda d: print("正在导出", d, "……", flush=True)) if verbose else None)
    except Exception:
        # traceback has no secrets (they are never in messages), but keep it short
        _log("unexpected error: " + traceback.format_exc(limit=3).replace("\n", " | "))
        return 3
    _log(json.dumps(status, ensure_ascii=False))
    if verbose:
        for r in status["results"]:
            print(r["day"], "失败：" + r["error"] if "error" in r else f"OK  净额 {r['net_usdt']} USDT  {len(r['traded'])} 个币种")
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
