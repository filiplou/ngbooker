#!/usr/bin/env python3
"""Book (or join the waiting list for) the sessions selected on the dashboard.

--mode scheduled : nightly run. Waits for 00:05 Sydney time, then polls until the newly released
                   day (today + advance days) appears and books every selected session on it.
--mode now       : single pass over all visible days (used by the dashboard "Book now" button).
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from ngclient import NGError, SYD, client_from_env, date_of, find_booking

DATA = Path(__file__).resolve().parent.parent / "data"
DONE = {"booked", "waitlisted", "already"}


def load(name, default):
    p = DATA / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(name, obj):
    (DATA / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def now():
    return datetime.now(SYD)


def say(msg):
    print(f"[{now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def already_done(log, sel_id, date_iso):
    return any(e["selectionId"] == sel_id and e["date"] == date_iso and e["result"] in DONE for e in log)


def record(log, mode, sel, prog, result, message=""):
    entry = {
        "time": now().isoformat(timespec="seconds"),
        "mode": mode,
        "selectionId": sel["id"],
        "className": sel["className"],
        "date": prog["date"] if prog else "",
        "slot": sel["time"],
        "result": result,
        "message": message,
    }
    log.append(entry)
    say(f"{sel['className']} {entry['date']} {sel['time']}: {result} {message}".strip())


def try_book(client, d, prog):
    """Return (result, message). The site's MakeBooking call books the slot or joins the waiting list."""
    try:
        chk = client.check(prog, d)
    except NGError as e:
        msg = str(e)
        return ("already" if "already" in msg.lower() else "failed"), msg
    on_wait_list = bool(chk.get("isWaitingList")) if isinstance(chk, dict) else False
    try:
        ok = client.book(prog)
    except NGError as e:
        msg = str(e)
        return ("already" if "already" in msg.lower() else "failed"), msg
    if ok is not True:
        return "failed", "site did not confirm the booking"
    # Re-read bookings so we can report booked vs waiting list accurately.
    try:
        b = find_booking(client.my_bookings(), prog["className"], prog["date"])
        if b is not None and "isOnWaitList" in b:
            on_wait_list = bool(b["isOnWaitList"])
    except NGError:
        pass
    return ("waitlisted" if on_wait_list else "booked"), ""


def attempt_pass(client, sels, log, mode, only_date=None):
    """One pass over the visible days. Returns the set of (selection id, date) not yet published."""
    unpublished = set()
    bookings = None
    for d in client.dates():
        if client.is_blocked(d):
            continue
        day = date_of(d)
        if only_date and day != only_date:
            continue
        wanted = [s for s in sels if s["weekday"] == day.weekday()]
        if not wanted:
            continue
        progs = client.programs(d)
        for s in wanted:
            date_iso = day.isoformat()
            if already_done(log, s["id"], date_iso):
                continue
            prog = next((p for p in progs if p["id"] == s["id"]), None)
            if prog is None:
                unpublished.add((s["id"], date_iso))
                continue
            if prog["dt"] <= now():
                continue
            if bookings is None:
                bookings = client.my_bookings()
            if find_booking(bookings, prog["className"], date_iso):
                record(log, mode, s, prog, "already", "found in My Bookings")
                continue
            result, msg = try_book(client, d, prog)
            record(log, mode, s, prog, result, msg)
    return unpublished


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["scheduled", "now"], default="scheduled")
    ap.add_argument("--selection-id", default="")
    ap.add_argument("--release", default="00:05", help="HH:MM Sydney time the new day is published")
    ap.add_argument("--advance-days", type=int, default=7)
    ap.add_argument("--max-wait", type=int, default=300, help="seconds to keep polling after release")
    ap.add_argument("--poll", type=float, default=3.0)
    args = ap.parse_args()

    sels = load("selections.json", {"selections": []})["selections"]
    if args.selection_id:
        sels = [s for s in sels if s["id"] == args.selection_id]
    if not sels:
        say("No matching selections - nothing to do.")
        return 0

    log = load("bookings-log.json", [])
    start = now()
    only_date = None
    release_at = None

    if args.mode == "scheduled":
        # Valid windows are around Sydney midnight (the workflow has a cron for each DST offset).
        minutes = start.hour * 60 + start.minute
        if not (minutes >= 23 * 60 or minutes <= 90):
            say(f"Outside the Sydney midnight window ({start:%H:%M}) - exiting.")
            return 0
        biz_day = start.date() + timedelta(days=1) if start.hour >= 12 else start.date()
        target = biz_day + timedelta(days=args.advance_days)
        if not any(s["weekday"] == target.weekday() for s in sels):
            say(f"No selected session falls on {target:%A} {target} - nothing to wait for.")
            return 0
        hh, mm = map(int, args.release.split(":"))
        release_at = datetime(biz_day.year, biz_day.month, biz_day.day, hh, mm, tzinfo=SYD)
        only_date = target

    client = client_from_env()
    say(f"Logged in. Site={client.site_id} gold={client.is_gold}")

    try:
        if args.mode == "now":
            attempt_pass(client, sels, log, "now")
        else:
            # Catch anything outstanding on other visible days, then wait for the new day.
            attempt_pass(client, sels, log, "scheduled")
            wait = (release_at - now()).total_seconds()
            if wait > 0:
                say(f"Waiting {wait:.0f}s until {release_at:%H:%M} Sydney time...")
                time.sleep(wait)
            deadline = max(now(), release_at) + timedelta(seconds=args.max_wait)
            while True:
                visible = any(date_of(d) == only_date and not client.is_blocked(d) for d in client.dates())
                pending = attempt_pass(client, sels, log, "scheduled", only_date=only_date) if visible else {None}
                if visible and not pending:
                    break
                if now() > deadline:
                    for s in sels:
                        if s["weekday"] == only_date.weekday() and not already_done(log, s["id"], only_date.isoformat()):
                            record(log, "scheduled", s, {"date": only_date.isoformat()}, "not-published",
                                   "session not found before the polling deadline")
                    break
                time.sleep(args.poll)
    finally:
        save("bookings-log.json", log[-300:])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except NGError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
