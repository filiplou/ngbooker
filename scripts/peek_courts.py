"""Read-only look at court availability, including dates that are still gold-only. Never books.

Prints, for every booking date the site lists, whether it is still blocked for the logged-in
member, then the 30-minute court slots (or --duration) for each blocked date and each Sunday.
Output deliberately contains no member names or IDs: Actions logs on a public repo are public.

    python scripts/peek_courts.py [--all-days] [--duration 30]
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ngclient import NGError, client_from_env, date_of  # noqa: E402

SUNDAY_WINDOW = ("16:00", "17:30")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-days", action="store_true",
                    help="check every listed date, not only blocked (gold-only) dates and Sundays")
    ap.add_argument("--duration", type=int, default=30, help="court slot length in minutes")
    args = ap.parse_args()

    c = client_from_env()
    lines = [f"Membership: {'gold' if c.is_gold else 'silver'}", "", "Booking dates the site lists:"]
    dates = c.dates() or []
    for d in dates:
        flags = {k: d.get(k) for k in ("goldOnly", "availableAfterHour", "goldAvailableAfterHour",
                                       "bookingsAvailableAfterHour") if k in d}
        lines.append(f"  {date_of(d):%a %d %b}  blocked={c.is_blocked(d)!s:5}  {flags}")
    lines.append("")

    for d in dates:
        day = date_of(d)
        blocked = c.is_blocked(d)
        if not (args.all_days or blocked or day.weekday() == 6):
            continue
        lines.append(f"=== {day:%A %d %b} ({'gold-only / not open yet' if blocked else 'open'}) ===")
        try:
            slots = c.courts(d, duration=args.duration)
        except NGError as e:
            lines += [f"  could not read courts: {e}", ""]
            continue
        if not slots:
            lines += ["  no court slots returned for this date", ""]
            continue
        by_time = defaultdict(list)
        for s in slots:
            by_time[s["time"]].append((s["className"], s["raw"].get("status")))
        for t in sorted(by_time):
            free = [n for n, st in by_time[t] if st == "Available"]
            statuses = ", ".join(sorted({str(st) for _, st in by_time[t]}))
            mark = "  <-- Sunday window" if day.weekday() == 6 and SUNDAY_WINDOW[0] <= t <= SUNDAY_WINDOW[1] else ""
            lines.append(f"  {t}  {len(free)}/{len(by_time[t])} free  [{statuses}]{mark}")
            lines += [f"         {n}" for n in free]
        lines.append("")

    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Court availability (read-only)\n\n```\n" + text + "\n```\n")


if __name__ == "__main__":
    main()
