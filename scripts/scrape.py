#!/usr/bin/env python3
"""Read the Tennis Programs for every bookable day (and your current bookings) into data/*.json."""
import json
import sys
from datetime import datetime
from pathlib import Path

from ngclient import NGError, SYD, client_from_env, public_view, sanitize

DATA = Path(__file__).resolve().parent.parent / "data"


def main():
    client = client_from_env()
    sessions = []
    for d in client.dates():
        if client.is_blocked(d):
            continue
        sessions += [public_view(p) for p in client.programs(d)]
    sessions.sort(key=lambda s: (s["date"], s["time"], s["className"]))
    stamp = datetime.now(SYD).isoformat(timespec="seconds")

    (DATA / "sessions.json").write_text(
        json.dumps({"updated": stamp, "sessions": sessions}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (DATA / "my-bookings.json").write_text(
        json.dumps({"updated": stamp, "bookings": sanitize(client.my_bookings())}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"Saved {len(sessions)} sessions.")


if __name__ == "__main__":
    try:
        main()
    except NGError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
