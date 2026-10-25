#!/usr/bin/env python3
"""Read shared sessions once per bookable day, plus each member's current bookings, into data/*.json."""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from ngclient import KINDS, NGError, SYD, client_from_env, public_view, sanitize

DATA = Path(__file__).resolve().parent.parent / "data"


def write(name, obj):
    (DATA / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def court_count_summary(programs):
    unique_slots = {
        (
            program["date"],
            program["time"],
            program["info"].get("facilityName"),
            program["info"].get("productName"),
        )
        for program in programs
    }
    duration_counts = {}
    for program in programs:
        duration = program["info"].get("duration")
        duration_counts[duration] = duration_counts.get(duration, 0) + 1
    durations = ", ".join(
        f"{duration} min: {count}" for duration, count in sorted(duration_counts.items(), key=lambda x: str(x[0]))
    )
    return f"{len(programs)} court slot-duration records, {len(unique_slots)} unique court/time slots ({durations})"


def main():
    client = client_from_env()
    days = [d for d in client.dates() if not client.is_blocked(d)]
    merged = {}  # (kind, date, time, className, activityID) -> public entry with the list of members who see it
    members = client.members
    member_keys = [m["key"] for m in members]
    data_member = members[0]
    for kind in KINDS:
        for d in days:
            date = d.get("dateAsISOString", "unknown")
            started = time.monotonic()
            print(f"Fetching {kind} for all members via {data_member['key']} on {date}", flush=True)
            try:
                progs = client.programs(d, kind, data_member["key"])
            except NGError as e:
                elapsed = time.monotonic() - started
                print(
                    f"WARNING: {kind} for all members via {data_member['key']} on {date} "
                    f"failed after {elapsed:.1f}s: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            elapsed = time.monotonic() - started
            if kind == "courts":
                progs = [program for program in progs if program["raw"].get("status") == "Available"]
                result_summary = court_count_summary([public_view(p) for p in progs])
            else:
                result_summary = f"{len(progs)} {kind} sessions"
            print(f"Fetched {result_summary} on {date} in {elapsed:.1f}s", flush=True)
            for p in progs:
                v = public_view(p)
                k = (kind, v["date"], v["time"], v["className"], v["activityID"])
                merged.setdefault(k, v | {"who": member_keys})
    sessions = sorted(merged.values(), key=lambda s: (s["kind"], s["date"], s["time"], s["className"]))
    stamp = datetime.now(SYD).isoformat(timespec="seconds")

    write("sessions.json", {"updated": stamp, "sessions": sessions})
    write("members.json", {"updated": stamp, "members": member_keys})

    bookings = []
    for m in members:
        try:
            bookings += [b | {"who": m["key"]} for b in sanitize(client.my_bookings(m["key"]))]
        except NGError as e:
            print(f"WARNING: bookings for {m['key']} failed: {e}", file=sys.stderr, flush=True)
    write("my-bookings.json", {"updated": stamp, "bookings": bookings})
    counts = {k: sum(1 for s in sessions if s["kind"] == k) for k in KINDS}
    print(f"Saved {len(sessions)} sessions {counts} for {len(members)} members.")


if __name__ == "__main__":
    try:
        main()
    except NGError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
