#!/usr/bin/env python3
"""Read the Racquets programs and Group Exercise classes for every bookable day, for every member
on the account, plus each member's current bookings, into data/*.json."""
import json
import sys
from datetime import datetime
from pathlib import Path

from ngclient import KINDS, NGError, SYD, client_from_env, public_view, sanitize

DATA = Path(__file__).resolve().parent.parent / "data"


def write(name, obj):
    (DATA / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    client = client_from_env()
    days = [d for d in client.dates() if not client.is_blocked(d)]
    merged = {}  # (kind, date, time, className, activityID) -> public entry with the list of members who see it
    for kind in KINDS:
        for m in client.members:
            for d in days:
                try:
                    progs = client.programs(d, kind, m["key"])
                except NGError as e:
                    print(f"WARNING: {kind} for {m['key']} on {d.get('dateAsISOString')}: {e}", file=sys.stderr)
                    continue
                for p in progs:
                    v = public_view(p)
                    k = (kind, v["date"], v["time"], v["className"], v["activityID"])
                    merged.setdefault(k, v | {"who": []})["who"].append(m["key"])
    sessions = sorted(merged.values(), key=lambda s: (s["kind"], s["date"], s["time"], s["className"]))
    stamp = datetime.now(SYD).isoformat(timespec="seconds")

    write("sessions.json", {"updated": stamp, "sessions": sessions})
    write("members.json", {"updated": stamp, "members": [m["key"] for m in client.members]})

    bookings = []
    for m in client.members:
        try:
            bookings += [b | {"who": m["key"]} for b in sanitize(client.my_bookings(m["key"]))]
        except NGError as e:
            print(f"WARNING: bookings for {m['key']}: {e}", file=sys.stderr)
    write("my-bookings.json", {"updated": stamp, "bookings": bookings})
    counts = {k: sum(1 for s in sessions if s["kind"] == k) for k in KINDS}
    print(f"Saved {len(sessions)} sessions {counts} for {len(client.members)} members.")


if __name__ == "__main__":
    try:
        main()
    except NGError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
