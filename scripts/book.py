#!/usr/bin/env python3
"""Book (or join the waiting list for) the sessions selected on the dashboard.

Only ever books the day exactly `advance days` (default 7) ahead, so a Tuesday selection books
next Tuesday and never today or other days already visible.

--mode scheduled : morning run. Waits for 08:01 Sydney time, then polls until the newly released
                   day (today + advance days) appears and books every selected session on it.
--mode now       : single pass over that same day (used by the dashboard "Book now" button).
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from ngclient import NGError, SYD, client_from_env, date_of, find_booking, normalise_selection

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
        "kind": sel.get("kind", "racquet"),
        "who": sel.get("who", ""),
        "className": sel["className"],
        "date": prog["date"] if prog else "",
        "slot": sel["time"],
        "result": result,
        "message": message,
    }
    log.append(entry)
    say(f"[{entry['who']}] {sel['className']} {entry['date']} {sel['time']}: {result} {message}".strip())


def try_book(client, d, prog, who=None):
    """Return (result, message). The site's MakeBooking call books the slot or joins the waiting list."""
    raw = prog.get("raw") or {}
    on_wait_list = (raw.get("spacesAvailable", 0) or 0) < 0 or "waiting list" in str(raw.get("bookButtonDisplayString", "")).lower()
    if prog.get("kind") != "courts":
        try:
            chk = client.check(prog, d, who)
        except NGError as e:
            msg = str(e)
            return ("already" if "already" in msg.lower() else "failed"), msg
        if isinstance(chk, dict) and chk.get("isWaitingList"):
            on_wait_list = True
    try:
        ok = client.book(prog, who)
    except NGError as e:
        msg = str(e)
        return ("already" if "already" in msg.lower() else "failed"), msg
    if ok is not True:
        return "failed", "site did not confirm the booking"
    # Re-read bookings so we can report booked vs waiting list accurately.
    try:
        b = find_booking(client.my_bookings(who), prog["className"], prog["date"], prog["time"])
        if b is not None and "isOnWaitList" in b:
            on_wait_list = bool(b["isOnWaitList"])
    except NGError:
        pass
    return ("waitlisted" if on_wait_list else "booked"), ""


def saved_court_program(selection, day):
    duration = selection.get("duration")
    resource_id = selection.get("resourceProductID")
    if not resource_id or selection.get("activityID") is None:
        return None
    if not isinstance(duration, (int, float)) or duration <= 0:
        return None

    hour, minute = map(int, selection["time"].split(":"))
    dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=SYD)
    start = dt.strftime("%Y-%m-%dT%H:%M:%S")
    raw = {
        "facilityType": selection.get("facilityType") or "TENNIS_SYNTHETIC",
        "resourceProductID": resource_id,
        "activityID": selection["activityID"],
        "duration": duration,
        "className": selection["className"],
        "dateTime": start,
        "dateTimeAsISOString": dt.isoformat(),
    }
    return {
        "id": selection["progId"],
        "kind": "courts",
        "className": selection["className"],
        "activityID": selection["activityID"],
        "date": day.isoformat(),
        "time": selection["time"],
        "weekday": day.weekday(),
        "dt": dt,
        "raw": raw,
    }


def target_day(advance_days=7):
    return now().date() + timedelta(days=advance_days)


def attempt_pass(client, sels, log, mode, only_date=None):
    """One pass over the single target day (default: today + 7). Returns the set of
    (selection id, date) not yet published. Each selection is for one session type (kind) and one member (who)."""
    only_date = only_date or target_day()
    unpublished = set()
    bookings = {}  # per member, read lazily
    court_booking_done = any(
        entry.get("kind") == "courts"
        and entry.get("date") == only_date.isoformat()
        and entry.get("result") in {"booked", "already"}
        for entry in log
    )
    for d in client.dates():
        if client.is_blocked(d):
            continue
        day = date_of(d)
        if day != only_date:
            continue
        wanted = [s for s in sels if s["weekday"] == day.weekday()]
        if not wanted:
            continue
        progs = {}  # (kind, who) -> programs for this day
        for s in wanted:
            date_iso = day.isoformat()
            if already_done(log, s["id"], date_iso):
                if s["kind"] == "courts" and any(
                    entry.get("selectionId") == s["id"]
                    and entry.get("date") == date_iso
                    and entry.get("result") in {"booked", "already"}
                    for entry in log
                ):
                    court_booking_done = True
                continue
            if s["kind"] == "courts" and court_booking_done:
                continue
            key = (s["kind"], s["who"])
            if s["kind"] == "courts":
                if s.get("duration") not in (None, 30):
                    record(log, mode, s, {"date": date_iso}, "failed", "court booking slots must be 30 minutes")
                    continue
                if s.get("duration") == 30:
                    if key not in progs:
                        progs[key] = client.programs(d, s["kind"], s["who"])
                    candidates = [
                        p for p in progs[key]
                        if p["time"] == s["time"] and (p.get("raw") or {}).get("duration") == 30
                    ]
                    if not candidates:
                        unpublished.add((s["id"], date_iso))
                        continue
                else:
                    prog = saved_court_program(s, day)
                    if prog and prog["raw"]["duration"] > 30:
                        record(log, mode, s, prog, "failed", "court booking slots must be 30 minutes")
                        continue
                    if prog is None:
                        if key not in progs:
                            progs[key] = client.programs(d, s["kind"], s["who"])
                        prog = next((p for p in progs[key] if p["id"] == s["progId"]), None)
                    candidates = [prog] if prog else []
            else:
                if key not in progs:
                    progs[key] = client.programs(d, s["kind"], s["who"])
                prog = next((p for p in progs[key] if p["id"] == s["progId"]), None)
                candidates = [prog] if prog else []
            if not candidates:
                unpublished.add((s["id"], date_iso))
                continue
            for prog in candidates:
                if s["kind"] == "courts" and (prog.get("raw") or {}).get("duration", 0) > 30:
                    record(log, mode, s, prog, "failed", "court booking slots must be 30 minutes")
                    continue
                if prog["dt"] <= now():
                    continue
                if s["who"] not in bookings:
                    bookings[s["who"]] = client.my_bookings(s["who"])
                if find_booking(bookings[s["who"]], prog["className"], date_iso, prog["time"]):
                    record(log, mode, s, prog, "already", "found in My Bookings")
                    if s["kind"] == "courts":
                        court_booking_done = True
                        break
                    continue
                result, msg = try_book(client, d, prog, s["who"])
                record(log, mode, s, prog, result, msg)
                if s["kind"] == "courts" and result in {"booked", "already"}:
                    court_booking_done = True
                    break
    return unpublished


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["scheduled", "now"], default="scheduled")
    ap.add_argument("--selection-id", default="")
    ap.add_argument("--release", default="08:01", help="HH:MM Sydney time the new day is published")
    ap.add_argument("--advance-days", type=int, default=7)
    ap.add_argument("--max-wait", type=int, default=300, help="seconds to keep polling after release")
    ap.add_argument("--poll", type=float, default=3.0)
    args = ap.parse_args()

    sel_data = load("selections.json", {"selections": []})
    if sel_data.get("allPaused"):
        say("All automatic bookings are paused - exiting.")
        return 0
    raw_sels = [s for s in sel_data["selections"] if not s.get("paused")]
    if not raw_sels:
        say("No matching selections - nothing to do.")
        return 0
    # Release time can be overridden via selections.json (set from the dashboard Settings tab).
    if not args.release or args.release == "08:01":
        args.release = sel_data.get("releaseTime", "08:01")

    log = load("bookings-log.json", [])
    sels = None  # normalised once we know the primary member (after login)
    start = now()
    only_date = target_day(args.advance_days)
    release_at = None

    if args.mode == "scheduled":
        # Accept any run that starts within 90 minutes before or after the configured release time.
        hh_r, mm_r = map(int, args.release.split(":"))
        release_minutes = hh_r * 60 + mm_r
        minutes = start.hour * 60 + start.minute
        if not (release_minutes - 90 <= minutes <= release_minutes + 90):
            say(f"Outside the booking window (release={args.release}, now={start:%H:%M}) - exiting.")
            return 0
        biz_day = start.date() + timedelta(days=1) if start.hour >= 12 else start.date()
        only_date = biz_day + timedelta(days=args.advance_days)
        hh, mm = map(int, args.release.split(":"))
        release_at = datetime(biz_day.year, biz_day.month, biz_day.day, hh, mm, tzinfo=SYD)

    if not any(s["weekday"] == only_date.weekday() for s in raw_sels):
        say(f"No selected session falls on {only_date:%A} {only_date} - nothing to do.")
        return 0

    client = client_from_env()
    say(f"Logged in. Site={client.site_id} gold={client.is_gold} members={client.member_keys()}")
    primary = client.member_keys()[0]
    sels = [normalise_selection(s, primary) for s in raw_sels]
    # Entries logged before kinds/members existed used the old id; map them so they still count as done.
    for e in log:
        if e.get("selectionId") and "|" in e["selectionId"] and not e.get("who"):
            e["selectionId"] = normalise_selection({"id": e["selectionId"]}, primary)["id"]
            e["who"], e["kind"] = primary, "racquet"
    if args.selection_id:
        sels = [s for s in sels if args.selection_id in (s["id"], s["progId"])]
    if not sels:
        say("No matching selections - nothing to do.")
        return 0

    try:
        if args.mode == "now":
            attempt_pass(client, sels, log, "now", only_date=only_date)
        else:
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
