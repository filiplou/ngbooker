#!/usr/bin/env python3
"""Book (or join the waiting list for) the sessions selected on the dashboard.

Only ever books the day exactly `advance days` (default 7) ahead, so a Tuesday selection books
next Tuesday and never today or other days already visible.

--mode scheduled : morning run. Waits for 08:01 Sydney time, then polls until the newly released
                   day (today + advance days) appears and books every selected session on it.
--mode now       : single pass over that same day (used by the dashboard "Book now" button).

Exit code: 0 when every attempted selection ended booked / waitlisted / already booked,
1 when any ended failed, conflict or not-published (so GitHub flags the run).
"""
import argparse
import calendar
import difflib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from ngclient import NGError, SYD, _snippet, client_from_env, date_of, find_booking, normalise_selection, sanitize

DATA = Path(__file__).resolve().parent.parent / "data"
DONE = {"booked", "waitlisted", "already"}
NOT_BOOKED = {"failed", "conflict", "not-published"}
# After a timeout / 5xx the booking may have landed server-side (the site is slow around 08:00).
# Re-read My Bookings at these offsets (seconds between attempts, ~65s in total) before recording a failure.
AMBIGUOUS_RETRY_DELAYS = (0, 5, 10, 20, 30)
# Poll every --poll seconds for this long after release, then back off to SLOW_POLL to go easy on the site.
FAST_POLL_SECONDS = 120
SLOW_POLL = 15.0


def load(name, default):
    p = DATA / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(name, obj):
    (DATA / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def now():
    return datetime.now(SYD)


def say(msg):
    print(f"[{now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


TIMES_WINDOW_DAYS = 30


def update_booking_times(times, log, today=None):
    """Merge successful automatic (scheduled) bookings from log into times; keep only the last 30 days.

    Manual "now" bookings are not plotted. Older entries saved without a mode take it from the matching log
    entry; if that is no longer in the log they are dropped, since they cannot be shown to be automatic."""
    today = today or now()
    modes = {(e["time"], e["selectionId"]): e.get("mode") for e in log}
    times = [dict(e, mode=e.get("mode") or modes.get((e["time"], e["selectionId"]))) for e in times]
    seen = {(e["time"], e["selectionId"]) for e in times}
    for e in log:
        if e["result"] == "booked" and (e["time"], e["selectionId"]) not in seen:
            times.append({k: e.get(k) for k in ("time", "who", "className", "date", "slot", "selectionId", "mode")})
            seen.add((e["time"], e["selectionId"]))
    cutoff = today - timedelta(days=TIMES_WINDOW_DAYS)
    return [e for e in times if e.get("mode") == "scheduled" and datetime.fromisoformat(e["time"]) >= cutoff]


def already_done(log, sel_id, date_iso):
    return any(e["selectionId"] == sel_id and e["date"] == date_iso and e["result"] in DONE for e in log)


def join_msgs(*parts):
    return "; ".join(p for p in parts if p)


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


def manual_target_date(client, selection, after_date):
    matching_dates = []
    for d in client.dates():
        day = date_of(d)
        if day > after_date and day.weekday() == selection["weekday"] and not client.is_blocked(d):
            matching_dates.append(day)
    return min(matching_dates, default=None)


def ambiguous_error(msg):
    """True when a failed request may still have been applied by the site (timeout / 5xx)."""
    m = msg.lower()
    return (
        "timed out" in m
        or "timeout" in m
        or "connection error" in m
        or "connection reset" in m
        or "connection aborted" in m
        or m.startswith("http 5")
    )


def classify_error(msg):
    """Site messages that are final answers rather than failures worth retrying.
    'already' = the member already holds this booking; 'conflict' = they hold another booking at the
    same time (the site says "another concurrent (at the same time) reservation")."""
    m = msg.lower()
    if "already" in m:
        return "already"
    if "concurrent" in m or "at the same time" in m:
        return "conflict"
    return None


def _words(name):
    return set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(name).lower()))


def find_renamed(progs, sel):
    """The session at the selection's time that looks like the selected class under a new name, or None.

    The club sometimes renames a class ('Sculpt Class' -> 'Sculpt Booty Band Class'), which changes its id.
    A session at the same time counts as the same class when all words of one name appear in the other.
    That is deliberately strict: 'Social Doubles C1-2' must never match 'Social Doubles C3-4'."""
    want = _words(sel.get("className"))
    if not want:
        return None
    scored = []
    for p in progs:
        if p["time"] != sel["time"]:
            continue
        have = _words(p["className"])
        if have and (want <= have or have <= want):
            ratio = difflib.SequenceMatcher(None, sel["className"].lower(), p["className"].lower()).ratio()
            scored.append((ratio, p))
    scored.sort(key=lambda x: -x[0])
    if not scored or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None  # nothing similar, or two equally good guesses: do not pick one at random
    return scored[0][1]


def not_found_reason(progs, sel, date_iso):
    """Explain why a class selection was not found on a day whose timetable is out."""
    if not progs:
        return f"the {sel.get('kind', 'racquet')} timetable for {date_iso} is empty"
    name, at = sel["className"], sel["time"]
    same_name = sorted({p["time"] for p in progs if p["className"].lower() == name.lower()})
    at_time = sorted({p["className"] for p in progs if p["time"] == at})
    parts = [f"'{name}' is not on the timetable at {at} on {date_iso}"]
    if same_name:
        parts.append(f"it runs at {', '.join(same_name)} instead")
    if at_time:
        parts.append(f"at {at} there is: {', '.join(at_time[:5])}")
    else:
        parts.append(f"nothing else runs at {at} ({len(progs)} sessions that day)")
    return "; ".join(parts)


def booking_after_error(client, prog, who):
    """Re-read My Bookings after an ambiguous error. Returns the confirmed result or None."""
    try:
        b = find_booking(client.my_bookings(who), prog["className"], prog["date"], prog["time"])
    except NGError:
        return None
    if b is None:
        return None
    return "waitlisted" if b.get("isOnWaitList") else "booked"


def try_book(client, d, prog, who=None):
    """Return (result, message). The site's MakeBooking call books the slot or joins the waiting list."""
    on_wait_list = False
    note = ""
    # Courts are deliberately not re-checked: attempt_pass only offers slots whose status is "Available" from the
    # fresh programs() read made moments earlier, and courts have no waiting list.
    if prog.get("kind") != "courts":
        raw = prog.get("raw") or {}
        on_wait_list = (raw.get("spacesAvailable", 0) or 0) < 0 or "waiting list" in str(raw.get("bookButtonDisplayString", "")).lower()
        try:
            chk = client.check(prog, d, who)
        except NGError as e:
            msg = str(e)
            final = classify_error(msg)
            if final:
                return final, msg
            # The check only tells a booking apart from a waiting-list place, and MakeBooking does either.
            # A slow or failing check must not cost the slot, so carry on and book.
            say(f"Availability check failed, booking anyway: {msg}")
            note = f"availability check failed ({msg}), booked anyway"
            chk = None
        if isinstance(chk, dict) and chk.get("isWaitingList"):
            on_wait_list = True
    try:
        ok = client.book(prog, who)
    except NGError as e:
        msg = join_msgs(note, str(e))
        final = classify_error(str(e))
        if final:
            return final, msg
        if ambiguous_error(str(e)):
            for delay in AMBIGUOUS_RETRY_DELAYS:
                if delay:
                    time.sleep(delay)
                result = booking_after_error(client, prog, who)
                if result:
                    return result, f"{msg} - but the booking was found in My Bookings"
            return "failed", f"{msg} - not found in My Bookings after {sum(AMBIGUOUS_RETRY_DELAYS)}s of checking"
        return "failed", msg
    if ok is not True:
        got = _snippet(json.dumps(sanitize(ok), default=str), 100)
        return "failed", join_msgs(note, f"site did not confirm the booking (response: {got})")
    # Re-read bookings so we can report booked vs waiting list accurately.
    try:
        b = find_booking(client.my_bookings(who), prog["className"], prog["date"], prog["time"])
        if b is not None and "isOnWaitList" in b:
            on_wait_list = bool(b["isOnWaitList"])
    except NGError:
        pass
    return ("waitlisted" if on_wait_list else "booked"), note


def target_day(advance_days=7):
    return now().date() + timedelta(days=advance_days)


def attempt_pass(client, sels, log, mode, only_date=None, skip=(), reasons=None):
    """One pass over the single target day (default: today + 7). Returns
    (unpublished, failed) sets of (selection id, date): not yet published, and attempted
    but not booked. Each selection is for one session type (kind) and one member (who).
    `skip` lists selection ids to leave alone (e.g. waiting for a delayed retry).
    `reasons`, when given, is filled with {(selection id, date): why it is still unpublished}."""
    only_date = only_date or target_day()
    start_index = len(log)
    skip = set(skip)
    unpublished = set()

    def still_pending(s, date_iso, why):
        unpublished.add((s["id"], date_iso))
        if reasons is not None:
            reasons[(s["id"], date_iso)] = why

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
            if s["id"] in skip:
                continue
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
            note = ""
            if s["kind"] == "courts":
                if s.get("duration") not in (None, 30):
                    record(log, mode, s, {"date": date_iso}, "failed", "court booking slots must be 30 minutes")
                    continue
                if s.get("duration") == 30:
                    key = (s["kind"], s["who"], 30)
                    if key not in progs:
                        # Only 30-minute slots are ever booked: skip the 60/90-minute queries.
                        progs[key] = client.programs(d, s["kind"], s["who"], duration=30)
                    at_time = [
                        p for p in progs[key]
                        if p["time"] == s["time"] and (p.get("raw") or {}).get("duration") == 30
                    ]
                    candidates = [p for p in at_time if (p.get("raw") or {}).get("status") == "Available"]
                    if not candidates:
                        # No free court at this time: not a booking failure, so do not burn a POST per court
                        # or start the failed-booking retry loop. Scheduled runs keep polling for cancellations.
                        why = (f"no court is available at {s['time']} on {date_iso} "
                               f"({len(at_time)} court(s) checked, all booked)" if at_time
                               else f"no court is available at {s['time']} on {date_iso} "
                                    f"(the site lists no 30-minute slots at that time)")
                        if mode == "now":
                            record(log, mode, s, {"date": date_iso}, "not-published", why)
                        else:
                            still_pending(s, date_iso, why)
                        continue
                else:
                    if key not in progs:
                        progs[key] = client.programs(d, s["kind"], s["who"])
                    prog = next((p for p in progs[key] if p["id"] == s["progId"]), None)
                    candidates = [prog] if prog else []
                    if not candidates:
                        still_pending(s, date_iso, f"court slot '{s['className']}' at {s['time']} is not listed on {date_iso}")
                        continue
            else:
                if key not in progs:
                    progs[key] = client.programs(d, s["kind"], s["who"])
                prog = next((p for p in progs[key] if p["id"] == s["progId"]), None)
                if prog is None:
                    prog = find_renamed(progs[key], s)
                    if prog is not None:
                        note = (f"'{s['className']}' is not on the timetable; booked '{prog['className']}' "
                                f"at the same time instead (renamed?)")
                        say(f"[{s['who']}] {note}")
                if prog is None:
                    still_pending(s, date_iso, not_found_reason(progs[key], s, date_iso))
                    continue
                candidates = [prog]
            for prog in candidates:
                if s["kind"] == "courts" and (prog.get("raw") or {}).get("duration", 0) > 30:
                    record(log, mode, s, prog, "failed", "court booking slots must be 30 minutes")
                    continue
                if prog["dt"] <= now():
                    continue
                if s["who"] not in bookings:
                    bookings[s["who"]] = client.my_bookings(s["who"])
                if find_booking(bookings[s["who"]], prog["className"], date_iso, prog["time"]):
                    record(log, mode, s, prog, "already", join_msgs(note, "found in My Bookings"))
                    if s["kind"] == "courts":
                        court_booking_done = True
                        break
                    continue
                result, msg = try_book(client, d, prog, s["who"])
                record(log, mode, s, prog, result, join_msgs(note, msg))
                if s["kind"] == "courts" and result in {"booked", "already"}:
                    court_booking_done = True
                    break
    # A selection failed in this pass only if it has a failure and no success: with several court candidates
    # a later "failed" must not hide an earlier "booked", and vice versa.
    results = {}
    for entry in log[start_index:]:
        if entry.get("date") == only_date.isoformat():
            results.setdefault(entry.get("selectionId"), set()).add(entry.get("result"))
    failed = {(sid, only_date.isoformat()) for sid, rs in results.items() if "failed" in rs and not (rs & DONE)}
    return unpublished, failed


def final_results(entries):
    """Last logged outcome per (selection, date) in this run."""
    final = {}
    for e in entries:
        final[(e["selectionId"], e["date"])] = e
    return list(final.values())


def _md(text):
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def report(entries):
    """Print a summary of this run, add it to the GitHub job summary, and return the exit code."""
    rows = final_results(entries)
    if not rows:
        say("Summary: no booking attempts were made in this run.")
        return 0
    bad = [e for e in rows if e["result"] in NOT_BOOKED]
    say(f"Summary: {len(rows) - len(bad)} of {len(rows)} selection(s) booked or already held.")
    for e in rows:
        say(f"  [{e['who']}] {e['className']} {e['date']} {e['slot']}: {e['result']} {e['message']}".rstrip())
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        lines = ["## Booking results", "", "| Who | Session | Date | Time | Result | Details |",
                 "|---|---|---|---|---|---|"]
        icon = {"booked": "✅", "already": "✅", "waitlisted": "🕒", "failed": "❌", "conflict": "⚠️",
                "not-published": "❌"}
        for e in rows:
            lines.append(f"| {_md(e['who'])} | {_md(e['className'])} | {_md(e['date'])} | {_md(e['slot'])} | "
                         f"{icon.get(e['result'], '')} {_md(e['result'])} | {_md(e['message'])} |")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            say(f"Could not write the job summary: {e}")
    return 1 if bad else 0


def run_scheduled(client, sels, log, args, only_date, release_at, run_start):
    """Poll for the newly released day, book every selection on it, and retry failed bookings."""
    wait = (release_at - now()).total_seconds()
    if wait > 0:
        say(f"Waiting {wait:.0f}s until {release_at:%H:%M} Sydney time...")
        time.sleep(wait)
    fast_until = max(now(), release_at) + timedelta(seconds=FAST_POLL_SECONDS)
    retry_extra = max(0, args.max_tries - 1) * args.retry_delay
    deadline = max(now(), release_at) + timedelta(seconds=args.max_wait + retry_extra)
    date_iso = only_date.isoformat()
    failures = {}   # selection id -> failed attempts so far
    retry_at = {}   # selection id -> when its failed booking may be tried again
    gave_up = set()  # selection ids not to try again in this run (out of tries, or a booking clash)
    reasons = {}    # (selection id, date) -> why it is still unpublished
    visible, complete, pending, last_error = False, False, set(), ""
    while True:
        for sid in [sid for sid, t in retry_at.items() if now() >= t]:
            retry_at.pop(sid)
        complete = False
        try:
            visible = any(date_of(d) == only_date and not client.is_blocked(d) for d in client.dates())
            if visible:
                pending, failed = attempt_pass(client, sels, log, "scheduled", only_date=only_date,
                                               skip=gave_up | set(retry_at), reasons=reasons)
                complete = True
            else:
                pending, failed = set(), set()
        except NGError as e:
            # Read calls already retried inside the client; keep polling until the deadline.
            last_error = str(e)
            say(f"Pass failed, will retry until the deadline: {e}")
            failed = set()
        except Exception as e:  # an unexpected response shape must not end the morning run
            last_error = f"{type(e).__name__}: {e}"
            say(f"Unexpected error during a booking pass, will retry until the deadline: {last_error}")
            traceback.print_exc()
            failed = set()
        # A clash with another booking will not resolve itself: do not retry it.
        gave_up |= {e["selectionId"] for e in log[run_start:] if e["result"] == "conflict"}
        for sid, _ in failed:
            if sid in gave_up:
                continue
            failures[sid] = failures.get(sid, 0) + 1
            if failures[sid] >= args.max_tries:
                gave_up.add(sid)
                say(f"Giving up on {sid} after {failures[sid]} failed attempt(s).")
            else:
                retry_at[sid] = now() + timedelta(seconds=args.retry_delay)
                say(f"Retrying {sid} in {args.retry_delay}s (attempt {failures[sid] + 1}/{args.max_tries})...")
        if complete and not pending and not retry_at:
            return
        if now() > deadline:
            break
        if complete and not pending:
            # Only delayed retries are left: sleep until the first one is due rather than polling.
            first = min(retry_at.values())
            time.sleep(max(0.0, (first - now()).total_seconds()))
            for sid in [sid for sid, t in retry_at.items() if t <= first]:
                retry_at.pop(sid)
            continue
        time.sleep(args.poll if now() < fast_until else max(args.poll, SLOW_POLL))

    # Deadline reached: say why each outstanding selection was not booked.
    if complete:
        for sid, d_iso in pending:
            s = next((x for x in sels if x["id"] == sid), None)
            if s is not None:
                record(log, "scheduled", s, {"date": d_iso}, "not-published",
                       reasons.get((sid, d_iso), "session not found before the polling deadline"))
        return
    attempted = {e["selectionId"] for e in log[run_start:] if e["date"] == date_iso}
    if not visible and not last_error:
        why = f"{only_date:%A} {date_iso} was not released by the club before the polling deadline"
    else:
        why = f"could not read the club timetable before the polling deadline (last error: {last_error})"
    for s in sels:
        if (s["weekday"] == only_date.weekday() and s["id"] not in attempted
                and not already_done(log, s["id"], date_iso)):
            record(log, "scheduled", s, {"date": date_iso}, "not-published",
                   reasons.get((s["id"], date_iso), why) if visible else why)


def run_now(client, sels, log):
    """Single pass per selection over its next bookable day (the dashboard "Book now" button)."""
    today = now().date()
    for s in sels:
        manual_date = None
        try:
            manual_date = manual_target_date(client, s, today)
            if manual_date is None:
                selected_day = calendar.day_name[s["weekday"]]
                record(log, "now", s, {"date": ""}, "not-published",
                       f"no future {selected_day} is currently available to book")
                continue
            reasons = {}
            pending, _ = attempt_pass(client, [s], log, "now", only_date=manual_date, reasons=reasons)
            for sid, d_iso in pending:
                record(log, "now", s, {"date": d_iso}, "not-published",
                       reasons.get((sid, d_iso), "session not found"))
        except Exception as e:  # one broken selection must not stop the others
            if not isinstance(e, NGError):
                traceback.print_exc()
            msg = str(e) if isinstance(e, NGError) else f"internal error ({type(e).__name__}: {e})"
            record(log, "now", s, {"date": manual_date.isoformat() if manual_date else ""}, "failed", msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["scheduled", "now"], default="scheduled")
    ap.add_argument("--selection-id", default="")
    ap.add_argument("--release", default=None,
                    help="HH:MM Sydney time the new day is published (default: releaseTime in selections.json, else 08:01)")
    ap.add_argument("--advance-days", type=int, default=7)
    ap.add_argument("--max-wait", type=int, default=600, help="seconds to keep polling after release")
    ap.add_argument("--poll", type=float, default=3.0)
    ap.add_argument("--max-tries", type=int, default=5, help="scheduled-mode attempts per failed booking")
    ap.add_argument("--retry-delay", type=int, default=30, help="seconds between failed-booking retries")
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
    if not args.release:
        args.release = sel_data.get("releaseTime", "08:01")

    log = load("bookings-log.json", [])
    run_start = len(log)
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
        release_at = datetime(biz_day.year, biz_day.month, biz_day.day, hh_r, mm_r, tzinfo=SYD)

    if args.mode == "scheduled" and not any(s["weekday"] == only_date.weekday() for s in raw_sels):
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
    exit_code = 0
    try:
        if args.mode == "now":
            run_now(client, sels, log)
        else:
            run_scheduled(client, sels, log, args, only_date, release_at, run_start)
    finally:
        save("bookings-log.json", log[-300:])
        save("booking-times.json", update_booking_times(load("booking-times.json", []), log))
        exit_code = report(log[run_start:])
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except NGError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
