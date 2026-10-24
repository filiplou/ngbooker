#!/usr/bin/env python3
"""LOCAL ONLY: print the shape of the site's responses so field names/time formats can be verified.

    $env:NG_MEMBER_NUMBER = "..."; $env:NG_PASSWORD = "..."; python scripts/discover.py

Output contains your own data - do not paste it into chat or commit it.
"""
import json
import sys

from ngclient import NGError, SENSITIVE, client_from_env


def show(label, obj, limit=2):
    def mask(o):
        if isinstance(o, dict):
            return {k: ("<hidden>" if SENSITIVE.search(k) else mask(v)) for k, v in o.items()}
        if isinstance(o, list):
            return [mask(v) for v in o[:limit]]
        return o

    print(f"\n=== {label} ===")
    print(json.dumps(mask(obj), indent=2, default=str)[:3000])


def main():
    c = client_from_env()
    print(f"Logged in OK. site={c.site_id} gold={c.is_gold}")
    print("Members on this login:", [(m["key"], m["siteID"], m["isGold"]) for m in c.members])
    dates = c.dates()
    show("GetAvailableBookingDates (first 2 of %d)" % len(dates), dates)
    open_dates = [d for d in dates if not c.is_blocked(d)]
    print(f"\nBookable days for this membership: {len(open_dates)} of {len(dates)}")
    for d in reversed(open_dates):  # newest day may not be published yet, so walk back to one with programs
        raw = c._call("POST", "/AvailableTennisProgramsforDate",
                      json={"dateAsISOString": d["dateAsISOString"], "siteID": c.site_id, "memberID": c.member_id})
        print(f"\nday {d['dateAsISOString'][:10]}: {len(raw or [])} programs")
        if raw:
            show("AvailableTennisProgramsforDate raw (first 2)", raw)
            for p in c.programs(d)[:5]:
                print(f"  parsed: {p['date']} {p['time']} weekday={p['weekday']} {p['className']!r}  "
                      f"(site shows dateTime={p['raw'].get('dateTime')!r})")
            break
    for d in reversed(open_dates):  # same walk-back for Group Exercise
        raw = c._call("POST", "/AvailableClassesForDate",
                      json={"dateAsISOString": d["dateAsISOString"], "siteID": c.site_id, "memberID": c.member_id})
        print(f"\nday {d['dateAsISOString'][:10]}: {len(raw or [])} group exercise classes")
        if raw:
            show("AvailableClassesForDate raw (first 2)", raw)
            break
    # --- Courts: show facilities first, then slot availability ---
    print("\n=== GetAvailableFacilitiesForSite (raw) ===")
    try:
        m0 = c.members[0]
        fac_raw = c._call(
            "GET",
            f"/GetAvailableFacilitiesForSite?siteID={m0['siteID']}"
            f"&isAceMembership={str(m0.get('isAceMembership', True)).lower()}",
        )
        show("GetAvailableFacilitiesForSite response", fac_raw)
        facs = c.facilities()
        print(f"\nfacilities() returned {len(facs)} facilities")
        for fac in facs:
            durs = fac.get("durations") or []
            print(f"  facility: {fac.get('facilityName')!r}  type={fac.get('facilityType')!r}"
                  f"  durations={len(durs)}  activityIDs={[d.get('activityID') for d in durs]}")
    except NGError as e:
        print(f"Facilities error: {e}")

    for d in reversed(open_dates):  # walk-back for Courts
        try:
            courts = c.programs(d, kind="courts")
            print(f"\nday {d['dateAsISOString'][:10]}: {len(courts)} courts")
            if courts:
                show("Courts raw (first 2)", [p["raw"] for p in courts[:2]])
                for p in courts[:5]:
                    print(f"  parsed: {p['date']} {p['time']} weekday={p['weekday']} {p['className']!r}")
                break
            else:
                # Show raw slot response for first facility to diagnose empty results
                facs = c.facilities()
                if facs:
                    fac = facs[0]
                    durs = fac.get("durations") or []
                    if durs:
                        raw_slots = c._call("POST", "/GetSlotAvailabilityForActivity", json={
                            "memberID": c.member_id,
                            "activityID": durs[0].get("activityID"),
                            "onDate": d["dateAsISOString"],
                            "siteID": c.site_id,
                        })
                        print(f"  Raw GetSlotAvailabilityForActivity response type={type(raw_slots).__name__}:")
                        show("GetSlotAvailabilityForActivity response", raw_slots)
        except NGError as e:
            print(f"Courts check error: {e}")
            break
    for m in c.members:
        print(f"bookings for {m['key']}: {len(c.my_bookings(m['key']))}")
    show("GetMemberBookings item1 (first 2)", c.my_bookings())


if __name__ == "__main__":
    try:
        main()
    except NGError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
