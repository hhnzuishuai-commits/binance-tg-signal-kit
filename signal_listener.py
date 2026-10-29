"""Real-time group signal listener (runs on the VPS under systemd).

Listens to the signal group with Telethon (NewMessage / MessageEdited, plus a
1-minute poll as a safety net), appends every message to signals/YYYY-MM-DD.jsonl
(same format as tg_collect), tags each new signal (V3.2 hard threshold + V5 draft)
and pushes it to the user's own Telegram bot. Read-only on Telegram and Binance;
never trades.

  ~/venv/bin/python -B signal_listener.py             # run (systemd): collect + upload only, no pushes
  ~/venv/bin/python -B signal_listener.py --push      # also push tagged signals to the bot
  ~/venv/bin/python -B signal_listener.py --dry-run   # print today's last 5 signals as they would be pushed

The user reads signals through Claude (tool/new_signals.py on the repo), so the
default is no pushes: new messages reach GitHub within about a minute. The bot
is only used by the cron health check when the listener is down.

Push rules (--push only): ✅/⬇️ pushed; ⛔ only in an hourly summary; a burst of signals is
merged into one message (sent after 60 s without a new one, at most 3 min after
the first); edits are never re-pushed; handled message ids
are kept in listener_state.json so a restart does not push again.
While this runs it owns the Telegram session: auto_run (cron) only does a health
check when listener.enabled exists.
"""
import argparse
import asyncio
import glob
import json
import os
import sys
import time
from datetime import datetime

import auto_run
from github_upload import GitHubUploader, UploadError
import secure_store
import tg_collect

# Only the repo's signal tools live here (signal_features, v5_tags, signal_stats, rules_research,
# market, notify); never the repo's older copies of auto_run/secure_store/tg_collect.
# Only needed for --push / --dry-run (tagging); plain collection runs without them.
SIG_TOOL = os.environ.get("SIG_TOOL") or os.path.expanduser("~/sig/tool")
sys.path.append(SIG_TOOL)
try:
    import signal_features as sf  # noqa: E402
    import v5_tags  # noqa: E402
    from signal_stats import parse_signal, v32  # noqa: E402
    from rules_research import STOCK, STAR_C  # noqa: E402
    HAVE_TAGS = True
except ImportError:
    HAVE_TAGS = False

TZ = tg_collect.TZ
CHAT_ID = tg_collect.CHAT_ID
MERGE_SECONDS = 60        # a batch is sent once no new signal arrived for this long ...
MERGE_MAX = 180           # ... or at the latest this long after its first signal (owner posts in bursts at :00/:30)
CLUSTER_WINDOW = 7200     # same-direction signals counted over the previous 2 h (v5_tags.CLUSTER_WINDOW)
FRESH_SECONDS = 900       # a message caught up after a restart is still pushed if younger than this
UPLOAD_EVERY = 60         # only when something changed, so quiet hours make no commits
POLL_EVERY = 60
HEARTBEAT_EVERY = 60
BTC_REFRESH = 6 * 3600
STALE_SECONDS = 1800
MAX_MSG = 3800
EXIT_UNAUTHORIZED = 3     # systemd: RestartPreventExitStatus=3 (re-login needed, do not loop)
STATE_PATH = os.path.join(secure_store.APP_DIR, "listener_state.json")
STATUS_PATH = os.path.join(secure_store.APP_DIR, "listener_status.json")
ALERT_PATH = os.path.join(secure_store.APP_DIR, "listener_alert.json")
MARK_PATH = os.path.join(secure_store.APP_DIR, "listener.enabled")
FOOTER = "要详细评级和点位就在 Claude 里说『看 币种』"
V5_ICON = {"不做": "⛔", "降级": "⬇️", "可看": "✅"}
V32_TEXT = {"过门槛": "过门槛", "未过门槛": "未过", "待核实": "待核实"}


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def num(x):
    return ("%.10f" % x).rstrip("0").rstrip(".") if x is not None else "?"


def bj(ts, fmt="%H:%M"):
    return datetime.fromtimestamp(ts, TZ).strftime(fmt)


def signal_of(rec):
    """Parsed signal from a stored jsonl record, or None for chat / unparsable messages."""
    orig, latest = rec.get("original_text") or "", rec.get("latest_text") or ""
    text = orig if "入场" in orig else latest
    p = sf.parse(text)
    if not p or p["entry"] is None or p["stop"] is None or not p["entry"]:
        return None
    p["message_id"] = rec["message_id"]
    p["ts"] = datetime.fromisoformat(rec["message_date"]).timestamp()
    s = parse_signal(text)
    p["v32"] = v32(s) if s else "待核实"
    return p


def stored_record(m, collected_at):
    """tg_collect.message_record -> the merged jsonl row shape (first sighting)."""
    r = tg_collect.message_record(m, collected_at)
    row = {k: v for k, v in r.items() if k not in ("text", "collected_at")}
    row.update(original_text=r["text"], latest_text=r["text"], first_collected_at=collected_at)
    return r, row


def position_sides(local_out, day):
    """{symbol: {"LONG", "SHORT"}} from the VPS's latest export of `day`."""
    report = _load_json(os.path.join(local_out, day + ".json"), {})
    out = {}
    for sym, e in (report.get("symbols") or {}).items():
        for p in e.get("position_now") or []:
            try:
                amt = float(p.get("positionAmt") or 0)
            except ValueError:
                continue
            if amt == 0:
                continue
            side = p.get("positionSide")
            if side not in ("LONG", "SHORT"):
                side = "LONG" if amt > 0 else "SHORT"
            out.setdefault(sym, set()).add(side)
    return out


def format_signal(x, positions):
    short = x["direction"] == "SHORT"
    head = ("🔴空 " if short else "🟢多 ") + x["symbol"] + "｜" + (x.get("strategy") or "策略未标")
    if x.get("leverage"):
        head += f"｜{num(x['leverage'])}x"
    v5 = x["v5"]
    line2 = f"V3.2：{V32_TEXT.get(x['v32'], x['v32'])}｜V5：{V5_ICON[v5['level']]}"
    if v5["reasons"]:
        line2 += " " + "；".join(v5["reasons"])
    if v5["refs"]:
        line2 += "｜" + "；".join(v5["refs"])
    risk = v5["risk_pct"]
    line3 = f"入场 {num(x['entry'])}｜止损 {num(x['stop'])}（距离 {risk:.2f}%）"
    if x["targets"]:
        line3 += "｜" + " / ".join(f"TP{i} {num(t)}" for i, t in enumerate(x["targets"][:3], 1))
    lines = [head, line2, line3]
    base = x["symbol"][:-4]
    if base in STAR_C:
        lines.append("⭐ 高胜率名单")
    slip = (["美股合约"] if base in STOCK else []) + (["止损 <1%"] if risk < 1 else [])
    if slip:
        lines.append("⚠️ " + " / ".join(slip) + "，滑点风险")
    sides = positions.get(x["symbol"])
    if sides:
        if x["direction"] not in sides:
            lines.append("📌 ⚠️ 与你的持仓方向相反")
        else:
            lines.append("📌 已有同向仓" + ("（另有反向仓）" if len(sides) > 1 else ""))
    return "\n".join(lines)


class Router:
    """Pure push logic; time is passed in so tests can drive it."""

    def __init__(self, state=None, btc=None, positions=lambda: {}):
        state = state or {}
        self.handled = set(state.get("handled", []))
        self.skips = [tuple(s) for s in state.get("skips", [])]   # (ts, symbol) of ⛔ signals
        self.last_summary_hour = state.get("last_summary_hour")
        self.recent = {}              # message_id -> signal, for the 2h same-direction count
        self.pending, self.pending_since, self.pending_last = [], None, None
        self.btc = btc or []
        self.positions = positions

    def state(self):
        return {"handled": sorted(self.handled)[-5000:], "skips": self.skips,
                "last_summary_hour": self.last_summary_hour}

    def remember(self, rec):
        """History only (startup): counts toward the 2h cluster, never pushed."""
        x = signal_of(rec)
        if x:
            self.recent[x["message_id"]] = x

    def add(self, rec, now, fresh=True):
        x = signal_of(rec)
        if x is None or x["message_id"] in self.handled:
            return None
        self.handled.add(x["message_id"])
        cluster = sum(1 for r in self.recent.values()
                      if r["message_id"] != x["message_id"] and r["direction"] == x["direction"]
                      and x["ts"] - CLUSTER_WINDOW <= r["ts"] < x["ts"])
        btc = v5_tags.btc_state(x["ts"], self.btc) if self.btc else 0
        x["v5"] = v5_tags.judge(x, cluster, btc)
        self.recent[x["message_id"]] = x
        for mid in [m for m, r in self.recent.items() if r["ts"] < now - 3 * CLUSTER_WINDOW]:
            del self.recent[mid]
        if not fresh:
            return x
        if x["v5"]["level"] == "不做":
            self.skips.append((x["ts"], x["symbol"]))
            return x
        if not self.pending:
            self.pending_since = now
        self.pending_last = now
        self.pending.append(x)
        return x

    def due(self, now):
        """Messages to send now: the merged batch once the burst is over, ⛔ summary on the hour."""
        out = []
        if self.pending and (now - self.pending_last >= MERGE_SECONDS or now - self.pending_since >= MERGE_MAX):
            out += self.batch_texts(sorted(self.pending, key=lambda s: s["ts"]))
            self.pending, self.pending_since, self.pending_last = [], None, None
        hour = int(now // 3600)
        if self.last_summary_hour is None:
            self.last_summary_hour = hour
        elif hour > self.last_summary_hour:
            lo, hi = (hour - 1) * 3600, hour * 3600
            syms = list(dict.fromkeys(s for t, s in self.skips if lo <= t < hi))
            n = sum(1 for t, s in self.skips if lo <= t < hi)
            if n:
                out.append(f"过去 1 小时 ⛔ {n} 条：{' '.join(syms)}")
            self.skips = [(t, s) for t, s in self.skips if t >= hi]
            self.last_summary_hour = hour
        return out

    def batch_texts(self, sigs):
        positions = self.positions()
        head = f"📣 新信号 {len(sigs)} 条（{bj(sigs[0]['ts'])}）" if len(sigs) > 1 else f"📣 新信号（{bj(sigs[0]['ts'])}）"
        parts = [format_signal(x, positions) for x in sigs]
        msgs, cur = [], head
        for p in parts:
            if len(cur) + len(p) + len(FOOTER) + 4 > MAX_MSG:
                msgs.append(cur)
                cur = head + "（续）"
            cur += "\n\n" + p
        msgs.append(cur + "\n\n" + FOOTER)
        return msgs


class Quiet:
    """No-push mode: nothing is parsed, tagged or sent; messages are only stored and uploaded."""

    def __init__(self):
        self.handled, self.pending = set(), []

    def remember(self, rec):
        pass

    def add(self, rec, now, fresh=True):
        return None

    def due(self, now):
        return []

    def state(self):
        return {}


# ---------- health check (called from auto_run by cron) ----------

def health_check(now=None, send=None):
    """Status for status/last_run.json; alerts once when the listener goes down or recovers."""
    now = now or time.time()
    st = _load_json(STATUS_PATH, {})
    hb = st.get("heartbeat_ts")
    problem = None
    if st.get("error") == "session_not_authorized":
        problem = "session_not_authorized"
    elif hb is None or now - hb > STALE_SECONDS:
        problem = "no_heartbeat_30min"
    out = {"listener": "down" if problem else "ok",
           "heartbeat": bj(hb, "%Y-%m-%d %H:%M:%S") if hb else None,
           "signals_data_until": st.get("last_message_at"),
           "pushed_today": st.get("pushed_today")}
    if problem:
        out["error"] = problem
    alert = _load_json(ALERT_PATH, {})
    if problem != alert.get("problem"):
        text = (f"⚠️ 信号监听掉线（{problem}）" + ("：需要在 VPS 上重新登录 Telegram" if problem == "session_not_authorized" else "")
                if problem else "✅ 信号监听已恢复")
        try:
            (send or _bot_send)(text)
            _save_json(ALERT_PATH, {"problem": problem, "at": now})
        except Exception as exc:  # noqa: BLE001 - token never in messages
            out["alert_error"] = type(exc).__name__
    return out


def _bot_send(text):
    import notify
    notify.send(text)


# ---------- live run ----------

class Live:
    def __init__(self, client, entity, router, uploader, send):
        self.client, self.entity, self.router, self.uploader, self.send = client, entity, router, uploader, send
        self.changed, self.outbox = set(), []
        self.max_id = 0
        self.last_message_at = None
        self.pushed_today, self.pushed_day = 0, bj(time.time(), "%Y-%m-%d")
        self.error = None

    def ingest(self, messages, fresh_check=True):
        now = time.time()
        recs = []
        for m in messages:      # duplicates (event + poll) are dropped by Router.handled
            r, row = stored_record(m, datetime.now(TZ).isoformat())
            recs.append(r)
            self.max_id = max(self.max_id, m.id)
            ts = m.date.timestamp() if m.date else now
            self.router.add(row, now, fresh=(not fresh_check) or now - ts < FRESH_SECONDS)
            self.last_message_at = bj(ts, "%m-%d %H:%M")
        if recs:
            self.changed.update(tg_collect.merge_into_days(recs, auto_run.SIGNALS_OUT))

    def edited(self, m):
        r, _ = stored_record(m, datetime.now(TZ).isoformat())
        self.changed.update(tg_collect.merge_into_days([r], auto_run.SIGNALS_OUT))

    async def flush(self):
        due = self.router.due(time.time())
        if self.send is None:          # no-push mode: the router only keeps handled ids / 2h counts
            return
        self.outbox += due
        sent = False
        while self.outbox:
            try:
                await asyncio.to_thread(self.send, self.outbox[0])
            except Exception as exc:  # noqa: BLE001 - retry on the next tick
                self.error = "bot_send:" + type(exc).__name__
                break
            self.outbox.pop(0)
            sent = True
            day = bj(time.time(), "%Y-%m-%d")
            if day != self.pushed_day:
                self.pushed_day, self.pushed_today = day, 0
            self.pushed_today += 1
        if sent:
            self.error = None
            _save_json(STATE_PATH, self.router.state())

    async def upload(self):
        for day in sorted(self.changed):
            path = os.path.join(auto_run.SIGNALS_OUT, day + ".jsonl")
            try:
                with open(path, "rb") as fh:
                    await asyncio.to_thread(self.uploader.put_file, f"signals/{day}.jsonl", fh.read(), f"signals {day}")
                self.changed.discard(day)
            except (OSError, UploadError) as exc:
                self.error = "github:" + str(exc)
        st = _load_json(auto_run.TG_STATE, {})
        st["last_success"] = datetime.now(TZ).isoformat()   # cron fallback resumes from here
        _save_json(auto_run.TG_STATE, st)

    def heartbeat(self):
        _save_json(STATUS_PATH, {"heartbeat_ts": time.time(), "heartbeat": bj(time.time(), "%Y-%m-%d %H:%M:%S"),
                                 "pid": os.getpid(), "connected": self.client.is_connected(),
                                 "last_message_at": self.last_message_at, "pushed_today": self.pushed_today,
                                 "pending": len(self.router.pending), "error": self.error})

    async def poll(self):
        msgs = [m async for m in self.client.iter_messages(self.entity, min_id=self.max_id, limit=100)]
        self.ingest(sorted(msgs, key=lambda m: m.id))


async def _btc():
    try:
        return await asyncio.to_thread(v5_tags.btc_daily)
    except BaseException:  # noqa: BLE001 - market.key() calls sys.exit when no key
        return []


async def run(push=False):
    if not CHAT_ID:
        raise SystemExit("set CHAT_ID in tg_collect.py (the group to listen to)")
    from telethon import TelegramClient, events
    tg = secure_store.load_tg()
    secrets = secure_store.load_secrets()
    client = TelegramClient(auto_run.TG_SESSION, int(tg["api_id"]), tg["api_hash"],
                            device_model="trading-review VPS", flood_sleep_threshold=60,
                            request_retries=5, connection_retries=-1, retry_delay=5)
    await client.connect()
    if not await client.is_user_authorized():
        _save_json(STATUS_PATH, {"heartbeat_ts": time.time(), "error": "session_not_authorized"})
        await client.disconnect()
        return EXIT_UNAUTHORIZED
    try:
        entity = await client.get_entity(CHAT_ID)
    except ValueError:
        await client.get_dialogs()
        entity = await client.get_entity(CHAT_ID)

    day_of = lambda: bj(time.time(), "%Y-%m-%d")  # noqa: E731
    if push:
        router = Router(_load_json(STATE_PATH, {}), btc=await _btc(),
                        positions=lambda: position_sides(auto_run.LOCAL_OUT, day_of()))
    else:
        router = Quiet()
    live = Live(client, entity, router, GitHubUploader(secrets["github_token"], secrets["github_repo"]),
                _bot_send if push else None)

    # history for the 2h count, then catch up on anything missed while down
    for path in sorted(glob.glob(os.path.join(auto_run.SIGNALS_OUT, "*.jsonl")))[-2:]:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    router.remember(rec)
                    live.max_id = max(live.max_id, rec["message_id"])
    last = _load_json(auto_run.TG_STATE, {}).get("last_success")
    since = datetime.fromisoformat(last) - tg_collect.OVERLAP if last else None
    if since:
        backlog = [m async for m in client.iter_messages(entity, offset_date=since, reverse=True, limit=2000)]
        live.max_id = 0
        live.ingest(backlog)

    @client.on(events.NewMessage(chats=entity))
    async def _new(ev):
        live.ingest([ev.message])

    @client.on(events.MessageEdited(chats=entity))
    async def _edit(ev):
        live.edited(ev.message)

    _save_json(MARK_PATH, {"since": datetime.now(TZ).isoformat()})
    last = {"poll": 0, "upload": 0, "beat": 0, "btc": time.time()}
    try:
        while True:
            now = time.time()
            if not client.is_connected():
                await client.connect()
            if now - last["poll"] >= POLL_EVERY:
                try:
                    await live.poll()
                except Exception as exc:  # noqa: BLE001
                    live.error = "poll:" + type(exc).__name__
                last["poll"] = now
            await live.flush()
            if now - last["upload"] >= UPLOAD_EVERY and live.changed:
                await live.upload()
                last["upload"] = now
            if now - last["btc"] >= BTC_REFRESH:
                router.btc = await _btc() or router.btc
                last["btc"] = now
            if now - last["beat"] >= HEARTBEAT_EVERY:
                live.heartbeat()
                last["beat"] = now
            await asyncio.sleep(2)
    finally:
        await live.flush()
        if live.changed:
            await live.upload()
        await client.disconnect()


# ---------- dry run ----------

def dry_run(day=None, n=5, local_out=None, signals_dir=None):
    """Replays the last n signals of `day` through the Router and returns what would be sent."""
    signals_dir = signals_dir or auto_run.SIGNALS_OUT
    local_out = local_out or auto_run.LOCAL_OUT
    day = day or bj(time.time(), "%Y-%m-%d")
    recs = []
    for path in sorted(glob.glob(os.path.join(signals_dir, "*.jsonl"))):
        if os.path.basename(path)[:10] > day:
            continue
        with open(path, encoding="utf-8") as fh:
            recs += [json.loads(line) for line in fh if line.strip()]
    recs.sort(key=lambda r: r["message_date"])
    sig_recs = [r for r in recs if r["message_date"] and
                datetime.fromisoformat(r["message_date"]).astimezone(TZ).strftime("%Y-%m-%d") == day
                and signal_of(r)]
    chosen = sig_recs[-n:]
    if not chosen:
        return [], []
    first_ts = signal_of(chosen[0])["ts"]
    router = Router(positions=lambda: position_sides(local_out, day))
    for r in recs:
        x = signal_of(r)
        if x and x["ts"] < first_ts:
            router.remember(r)
    out, routes = [], []
    for r in chosen:
        x = signal_of(r)
        out += router.due(x["ts"])
        y = router.add(r, x["ts"])
        routes.append(f"{bj(y['ts'])} {y['symbol']} {y['direction']} -> "
                      + ("⛔ 只进整点汇总" if y["v5"]["level"] == "不做" else "推送"))
    end = signal_of(chosen[-1])["ts"] + MERGE_SECONDS
    out += router.due(end)
    out += router.due((int(end // 3600) + 1) * 3600)   # force the next hourly summary
    return routes, out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--push", action="store_true", help="also push tagged signals to the bot")
    ap.add_argument("--day")
    ap.add_argument("-n", type=int, default=5)
    a = ap.parse_args(argv)
    if (a.push or a.dry_run) and not HAVE_TAGS:
        print("--push / --dry-run 需要信号解析脚本（signal_features、v5_tags、signal_stats、rules_research），"
              "放在 SIG_TOOL 目录（默认 ~/sig/tool）")
        return 2
    if a.dry_run:
        routes, msgs = dry_run(a.day, a.n)
        print("== 路由 ==")
        print("\n".join(routes) or "（没有信号）")
        for m in msgs:
            print("\n== 将推送 ==\n" + m)
        return 0
    return asyncio.run(run(push=a.push))


if __name__ == "__main__":
    sys.exit(main())
