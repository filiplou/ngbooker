"""Thin client for the Next Gen Club Lounge web API (the same calls the website makes)."""
import json
import os
import re
import time
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    SYD = ZoneInfo("Australia/Sydney")
except Exception:
    from datetime import tzinfo, timedelta

    class _SydneyTz(tzinfo):
        def utcoffset(self, dt):
            if dt is None:
                return timedelta(hours=10)
            year = dt.year
            oct1 = datetime(year, 10, 1)
            oct_first_sun = 1 + (6 - oct1.weekday()) % 7
            dst_start = datetime(year, 10, oct_first_sun, 2, 0)
            apr1 = datetime(year, 4, 1)
            apr_first_sun = 1 + (6 - apr1.weekday()) % 7
            dst_end = datetime(year, 4, apr_first_sun, 3, 0)
            d = dt.replace(tzinfo=None)
            if d >= dst_start or d < dst_end:
                return timedelta(hours=11)
            return timedelta(hours=10)

        def dst(self, dt):
            return timedelta(hours=1) if self.utcoffset(dt) == timedelta(hours=11) else timedelta(0)

        def tzname(self, dt):
            return "AEDT" if self.utcoffset(dt) == timedelta(hours=11) else "AEST"

    SYD = _SydneyTz()

import requests
from requests.exceptions import ReadTimeout, ConnectionError as RequestsConnectionError

BASE = os.environ.get("NG_BASE_URL", "https://members.ngclubs.com.au").rstrip("/")
API = BASE + "/api/ClubLounge"
SENSITIVE = re.compile(r"member|email|phone|mobile|token|password", re.I)

# Session types the club site offers, and the endpoint that lists them for a day.
KINDS = {
    "racquet": "/AvailableTennisProgramsforDate",
    "courts": "/GetSlotAvailabilityForActivity",
    "groupex": "/AvailableClassesForDate",
}
DEFAULT_KIND = "racquet"


class NGError(Exception):
    pass


def parse_local(value):
    """ISO string from the site -> timezone-aware datetime in Sydney time."""
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SYD)
    return dt.astimezone(SYD)


def _display_hhmm(s):
    """'7:30 PM' or '05:30' or '5:30' -> '05:30' (None if it cannot be read)."""
    s = str(s or "").strip()
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*([AaPp])", s)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
        return f"{h:02d}:{m.group(2)}"
    m2 = re.match(r"\s*(\d{1,2}):(\d{2})\s*$", s)
    if m2:
        return f"{int(m2.group(1)):02d}:{m2.group(2)}"
    return None


def sanitize(obj):
    """Drop personal keys (member ids, emails...) before anything is written to the public repo."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items() if not SENSITIVE.search(k)}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


def make_id(class_name, weekday, time_str):
    return f"{class_name}|{weekday}|{time_str}"


def selection_id(kind, who, prog_id):
    return f"{kind}|{who}|{prog_id}"


def normalise_selection(s, default_who):
    """Selections saved before group exercise / members existed have no kind or who:
    treat them as Racquets for the primary member. Returns a new dict with kind, who, progId and id."""
    out = dict(s)
    out["kind"] = s.get("kind") or DEFAULT_KIND
    out["who"] = s.get("who") or default_who
    out["progId"] = s.get("progId") or s["id"]
    out["id"] = selection_id(out["kind"], out["who"], out["progId"])
    return out


def first_name_keys(names):
    """Short, unique, non-secret key per member: the first name (index appended on a clash)."""
    keys, seen = [], set()
    for i, n in enumerate(names):
        k = (str(n or "").strip().split(" ")[0]) or f"Member{i + 1}"
        if k in seen:
            k = f"{k}{i + 1}"
        seen.add(k)
        keys.append(k)
    return keys


def normalise_program(p, kind=DEFAULT_KIND):
    dt = parse_local(p["dateTimeAsISOString"])
    time_str = dt.strftime("%H:%M")
    return {
        "id": make_id(p["className"], dt.weekday(), time_str),
        "kind": kind,
        "className": p["className"],
        "activityID": p.get("activityID"),
        "date": dt.date().isoformat(),
        "time": time_str,
        "weekday": dt.weekday(),
        "dt": dt,
        "raw": p,
    }


def normalise_court_slot(slot, facility, duration, day_date=None, kind="courts"):
    """Convert a court slot from GetSlotAvailabilityForActivity into a normalised program dict."""
    product_name = str(slot.get("productName", "")).strip()
    facility_name = str(facility.get("facilityName", "")).strip()
    if product_name.lower().startswith("court"):
        court_label = product_name
    else:
        court_label = f"Court {product_name}" if product_name else "Court"

    if facility_name.upper().endswith(" COURTS"):
        base_facility = facility_name[:-7].strip()
    elif facility_name.upper().endswith(" COURT"):
        base_facility = facility_name[:-6].strip()
    else:
        base_facility = facility_name

    class_name = f"{base_facility} {court_label}".strip() if base_facility else court_label

    dt_str = slot.get("dateTime") or slot.get("dateTimeAsISOString")
    if dt_str:
        dt = parse_local(dt_str)
        time_str = dt.strftime("%H:%M")
    else:
        time_str = _display_hhmm(slot.get("displayTime")) or "00:00"
        hh, mm = map(int, time_str.split(":"))
        if day_date is None:
            day_date = datetime.now(SYD).date()
        dt = datetime(day_date.year, day_date.month, day_date.day, hh, mm, tzinfo=SYD)

    raw = dict(slot)
    raw.update({
        "facilityName": facility.get("facilityName"),
        "facilityType": facility.get("facilityType", "TENNIS_SYNTHETIC"),
        "duration": duration.get("duration", 60),
        "durationName": duration.get("durationName", "60 mins"),
        "activityID": duration.get("activityID"),
        "className": class_name,
        "dateTimeAsISOString": dt.isoformat(),
        "dateTime": slot.get("dateTime") or dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": slot.get("status", "Available"),
        "spacesAvailable": 1 if slot.get("status") == "Available" else 0,
        "bookButtonDisplayString": "Book" if slot.get("status") == "Available" else "Unavailable",
    })
    return {
        "id": make_id(class_name, dt.weekday(), time_str),
        "kind": kind,
        "className": class_name,
        "activityID": duration.get("activityID"),
        "date": dt.date().isoformat(),
        "time": time_str,
        "weekday": dt.weekday(),
        "dt": dt,
        "raw": raw,
    }


def public_view(n):
    """Version of a normalised program that is safe to publish."""
    skip = {"className", "activityID", "dateTimeAsISOString", "dateTime"}
    info = sanitize({k: v for k, v in n["raw"].items() if k not in skip})
    return {k: n[k] for k in ("id", "kind", "className", "activityID", "date", "time", "weekday")} | {"info": info}


def date_of(d):
    return parse_local(d["dateAsISOString"]).date()


class NGClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0 (compatible; ngbooker)"
        self.members = []
        self.member_id = None
        self.site_id = None
        self.is_gold = False

    # -- low level ---------------------------------------------------------
    def _call(self, method, path, **kw):
        try:
            r = self.s.request(method, API + path, timeout=30, **kw)
        except ReadTimeout:
            raise NGError(f"Request timed out: {path}")
        except RequestsConnectionError as e:
            raise NGError(f"Connection error on {path}: {e}")
        if r.status_code >= 400:
            msg = None
            try:
                j = r.json()
                msg = j.get("message") or j.get("Message")
            except ValueError:
                pass
            raise NGError(msg or f"HTTP {r.status_code} on {path}")
        return r.json() if r.content else None

    def login(self, retries=3, retry_delay=10):
        """Login with retries to handle transient site timeouts."""
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                r = self.s.post(
                    BASE + "/token",
                    data={"username": self.username, "password": self.password, "grant_type": "password"},
                    timeout=30,
                )
            except (ReadTimeout, RequestsConnectionError) as e:
                last_err = NGError(f"Login timed out (attempt {attempt}/{retries}): {e}")
                if attempt < retries:
                    time.sleep(retry_delay)
                continue
            if r.status_code != 200:
                try:
                    msg = r.json().get("error_description")
                except ValueError:
                    msg = None
                raise NGError(f"Login failed (HTTP {r.status_code}): {msg or 'unknown error'}")
            self.s.headers["Authorization"] = "Bearer " + r.json()["access_token"]
            try:
                graph = self._call("GET", "/GetMemberDetailGraph")
            except NGError as e:
                last_err = NGError(f"GetMemberDetailGraph failed (attempt {attempt}/{retries}): {e}")
                if attempt < retries:
                    time.sleep(retry_delay)
                    self.s.headers.pop("Authorization", None)
                continue
            break
        else:
            raise last_err
        raw = graph["members"]
        keys = first_name_keys([m.get("firstNames") for m in raw])
        self.members = [
            {
                "key": k,
                "memberID": m["memberID"],
                "siteID": m["sites"][0]["siteID"],
                "isGold": bool(m.get("isGoldMembership")),
                "isAceMembership": bool(m.get("isAceMembership", True)),
            }
            for k, m in zip(keys, raw)
        ]
        # The first member drives the date list and the "gold" rules, as before.
        self.member_id = self.members[0]["memberID"]
        self.site_id = self.members[0]["siteID"]
        self.is_gold = self.members[0]["isGold"]

    def member(self, who=None):
        if not who:
            return self.members[0]
        for m in self.members:
            if m["key"] == who:
                return m
        raise NGError(f"Unknown member '{who}'")

    def member_keys(self):
        return [m["key"] for m in self.members]

    # -- API ---------------------------------------------------------------
    def dates(self):
        return self._call("GET", f"/GetAvailableBookingDates?siteID={self.site_id}")

    def is_blocked(self, d):
        if self.is_gold:
            return bool(d.get("goldAvailableAfterHour"))
        return bool(d.get("goldOnly") or d.get("availableAfterHour"))

    def facilities(self, who=None):
        m = self.member(who)
        if not hasattr(self, "_facilities_cache"):
            self._facilities_cache = {}
        site_id = m["siteID"]
        if site_id not in self._facilities_cache:
            is_ace = m.get("isAceMembership", True)
            res = self._call("GET", f"/GetAvailableFacilitiesForSite?siteID={site_id}&isAceMembership={str(is_ace).lower()}")
            self._facilities_cache[site_id] = (res or {}).get("facilities") or []
        return self._facilities_cache[site_id]

    def courts(self, d, who=None):
        m = self.member(who)
        facs = self.facilities(who)
        day_date = date_of(d)
        all_courts = []
        for fac in facs:
            durs = fac.get("durations") or []
            if not durs:
                continue
            duration = durs[0]
            activity_id = duration.get("activityID")
            if not activity_id:
                continue
            data = {
                "memberID": m["memberID"],
                "activityID": activity_id,
                "onDate": d["dateAsISOString"],
                "siteID": m["siteID"],
            }
            slots_data = self._call("POST", "/GetSlotAvailabilityForActivity", json=data)
            raw_slots = []
            if isinstance(slots_data, list):
                for item in slots_data:
                    if isinstance(item, list):
                        raw_slots.extend(item)
                    elif isinstance(item, dict):
                        raw_slots.append(item)
            for slot in raw_slots:
                all_courts.append(normalise_court_slot(slot, fac, duration, day_date, kind="courts"))
        return all_courts

    def programs(self, d, kind=DEFAULT_KIND, who=None):
        if kind == "courts":
            return self.courts(d, who=who)
        m = self.member(who)
        data = {"dateAsISOString": d["dateAsISOString"], "siteID": m["siteID"], "memberID": m["memberID"]}
        return [normalise_program(p, kind) for p in (self._call("POST", KINDS[kind], json=data) or [])]

    def check(self, program, d, who=None):
        if program.get("kind") == "courts":
            raw = program.get("raw") or {}
            is_avail = raw.get("status") == "Available" or raw.get("spacesAvailable", 0) > 0
            return {"isWaitingList": False, "isAvailable": is_avail}
        m = self.member(who)
        raw = program.get("raw") or {}
        data = {
            "dateAsISOString": d["dateAsISOString"],
            "memberID": m["memberID"],
            "isGoldMember": m["isGold"],
            "bookingsAvailableAfter": d.get("bookingsAvailableAfterHour"),
            "siteID": m["siteID"],
            "selectedClassName": program["className"],
            "activityID": program["activityID"],
        }
        if program.get("kind") == "groupex":
            # The site's group exercise page sends the class time and a fixed 8 here.
            data["dateAsISOString"] = raw.get("dateTimeAsISOString") or d["dateAsISOString"]
            data["bookingsAvailableAfter"] = 8
            data["ResourceProductID"] = raw.get("resourceProductID")
        return self._call("POST", "/CheckAvailability", json=data)

    def book(self, program, who=None):
        m = self.member(who)
        raw = program.get("raw") or {}
        if program.get("kind") == "courts":
            data = {
                "FacilityType": raw.get("facilityType", "TENNIS_SYNTHETIC"),
                "ResourceProductID": raw.get("resourceProductID"),
                "startDateTime": raw.get("dateTime") or raw.get("startDateTime") or raw.get("dateTimeAsISOString"),
                "activityID": raw.get("activityID") or program.get("activityID"),
                "memberID": m["memberID"],
                "siteID": m["siteID"],
            }
            return self._call("POST", "/MakeActivityBooking", json=data)
        data = {
            "dateTimeAsISOString": raw["dateTimeAsISOString"],
            "activityID": raw["activityID"],
            "memberID": m["memberID"],
            "siteID": m["siteID"],
            "dateTime": raw.get("dateTime"),
            "className": raw["className"],
        }
        return self._call("POST", "/MakeBooking", json=data)

    def my_bookings(self, who=None):
        data = self._call("GET", f"/GetMemberBookings/{self.member(who)['memberID']}")
        return (data or {}).get("item1") or []


def client_from_env():
    user = os.environ.get("NG_MEMBER_NUMBER")
    pw = os.environ.get("NG_PASSWORD")
    if not user or not pw:
        raise NGError("NG_MEMBER_NUMBER / NG_PASSWORD environment variables are not set")
    c = NGClient(user, pw)
    c.login()
    return c


def find_booking(bookings, class_name, date_iso, time_str=None):
    """Match an existing booking. The site's My Bookings items have className, displayDate
    ('Tuesday 20 October') and displayTime ('7:30 PM'); no ISO date."""
    day = datetime.fromisoformat(date_iso)
    want = re.compile(r"\b%d\s+%s\b" % (day.day, day.strftime("%B")), re.I)
    for b in bookings or []:
        if "displayDate" not in b:  # unknown shape: fall back to a text search
            text = json.dumps(b, default=str).lower()
            if class_name.lower() in text and date_iso in text:
                return b
            continue
        if not want.search(str(b["displayDate"])):
            continue
        if time_str and _display_hhmm(b.get("displayTime")) not in (None, time_str):
            continue
        # The site appends the court/level to the booked name ('Social Doubles C1-2' -> 'Social Doubles C1-2 C1').
        have, want_name = str(b.get("className", "")).strip().lower(), class_name.strip().lower()
        if have == want_name or have.startswith(want_name + " ") or want_name.startswith(have + " "):
            return b
        # Check matching court numbers for court bookings (e.g. "SYNTH Court 1" and "Synthetic Court 1" or "Court 1")
        mc_have = re.search(r"\bcourt\s*(\d+)\b", have)
        mc_want = re.search(r"\bcourt\s*(\d+)\b", want_name)
        if mc_have and mc_want and mc_have.group(1) == mc_want.group(1):
            return b
    return None
