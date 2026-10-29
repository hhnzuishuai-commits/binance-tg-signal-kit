"""Incremental, read-only collection of the signal group's messages.

Writes Documents\\交易复盘\\signals\\YYYY-MM-DD.jsonl (Beijing date of publish).
Only reads history: never sends, joins, or changes anything on Telegram.
Uses a private COPY of an already-logged-in Telethon session, so the original
session files are never modified. Credentials come from secure_store (DPAPI).
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os

TZ = timezone(timedelta(hours=8))
CHAT_ID = 0                  # 改成你要监听的群 ID（负数，形如 -100xxxxxxxxxx）
MAX_MESSAGES = 5000          # hard cap per run
MAX_FLOOD_WAIT = 60          # seconds; longer waits stop the run
DEFAULT_SINCE = datetime(2026, 10, 17, 0, 0, tzinfo=TZ)
OVERLAP = timedelta(hours=48)  # re-read recent window to catch edits


def message_record(m, collected_at):
    return {"chat_id": CHAT_ID, "message_id": m.id,
            "message_date": m.date.astimezone(timezone.utc).isoformat() if m.date else None,
            "edit_date": m.edit_date.astimezone(timezone.utc).isoformat() if m.edit_date else None,
            "text": m.raw_text or "",
            "reply_to_message_id": getattr(m, "reply_to_msg_id", None),
            "media_type": type(m.media).__name__ if m.media else None,
            "collected_at": collected_at}


def merge_into_days(records, out_dir):
    """Merge records into per-day jsonl. Keeps the first observed text as
    original_text and refreshes latest_text. Returns the list of changed days."""
    by_day = {}
    for r in records:
        if not r["message_date"]:
            continue
        day = datetime.fromisoformat(r["message_date"]).astimezone(TZ).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(r)
    os.makedirs(out_dir, exist_ok=True)
    changed = []
    for day, recs in sorted(by_day.items()):
        path = os.path.join(out_dir, day + ".jsonl")
        existing = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        row = json.loads(line)
                        existing[row["message_id"]] = row
        before = json.dumps(existing, sort_keys=True, ensure_ascii=False)
        for r in recs:
            old = existing.get(r["message_id"])
            row = {k: v for k, v in r.items() if k not in ("text", "collected_at")}
            row["original_text"] = old["original_text"] if old else r["text"]
            row["latest_text"] = r["text"]
            row["first_collected_at"] = old["first_collected_at"] if old else r["collected_at"]
            existing[r["message_id"]] = row
        if json.dumps(existing, sort_keys=True, ensure_ascii=False) != before:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for mid in sorted(existing):
                    fh.write(json.dumps(existing[mid], ensure_ascii=False) + "\n")
            os.replace(tmp, path)
            changed.append(day)
    return changed


async def fetch(client_factory, since, collected_at):
    try:
        from telethon.errors import FloodWaitError
    except ImportError:  # offline tests without telethon installed
        class FloodWaitError(Exception):
            seconds = 0
    client = client_factory()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("session_not_authorized")
        try:
            entity = await client.get_entity(CHAT_ID)
        except ValueError:
            await client.get_dialogs()  # read-only: fills the entity cache
            entity = await client.get_entity(CHAT_ID)
        records = []
        try:
            async for m in client.iter_messages(entity, offset_date=since, reverse=True,
                                                limit=MAX_MESSAGES, wait_time=1):
                records.append(message_record(m, collected_at))
        except FloodWaitError as e:
            if e.seconds > MAX_FLOOD_WAIT:
                raise RuntimeError(f"flood_wait_{e.seconds}s") from None
            raise
        return records, len(records) >= MAX_MESSAGES
    finally:
        await client.disconnect()


def collect(api_id, api_hash, session_path, out_dir, state_path, *, now=None, client_factory=None):
    now = now or datetime.now(TZ)
    state = {}
    if os.path.exists(state_path):
        state = json.load(open(state_path, encoding="utf-8"))
    last = datetime.fromisoformat(state["last_success"]) if state.get("last_success") else DEFAULT_SINCE
    since = max(DEFAULT_SINCE, last - OVERLAP)
    if client_factory is None:
        if not CHAT_ID:
            raise RuntimeError("chat_id_not_set: set CHAT_ID in tg_collect.py")
        from telethon import TelegramClient

        def client_factory():
            return TelegramClient(session_path, int(api_id), api_hash, receive_updates=False,
                                  flood_sleep_threshold=MAX_FLOOD_WAIT, request_retries=2)
    records, capped = asyncio.run(fetch(client_factory, since.astimezone(timezone.utc),
                                        now.astimezone(timezone.utc).isoformat()))
    changed = merge_into_days(records, out_dir)
    if not capped:  # only advance when we read everything up to now
        state["last_success"] = now.isoformat()
    state["last_run"] = {"at": now.isoformat(), "since": since.isoformat(), "messages": len(records),
                         "capped": capped, "changed_days": changed}
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    json.dump(state, open(state_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return {"messages": len(records), "changed_days": changed, "capped": capped, "since": since.isoformat()}
