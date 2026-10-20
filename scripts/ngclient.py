"""Thin client for the Next Gen Club Lounge web API (the same calls the website makes)."""
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

SYD = ZoneInfo("Australia/Sydney")
BASE = os.environ.get("NG_BASE_URL", "https://members.ngclubs.com.au").rstrip("/")
API = BASE + "/api/ClubLounge"
SENSITIVE = re.compile(r"member|email|phone|mobile|token|password", re.I)

# Session types the club site offers, and the endpoint that lists them for a day.
KINDS = {"racquet": "/AvailableTennisProgramsforDate", "groupex": "/AvailableClassesForDate"}
DEFAULT_KIND = "racquet"


class NGError(Exception):
    pass


def parse_local(value):
    """ISO string from the site -> timezone-aware datetime in Sydney time."""
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SYD)
    return dt.astimezone(SYD)


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
        r = self.s.request(method, API + path, timeout=30, **kw)
        if r.status_code >= 400:
            msg = None
            try:
                j = r.json()
                msg = j.get("message") or j.get("Message")
            except ValueError:
                pass
            raise NGError(msg or f"HTTP {r.status_code} on {path}")
        return r.json() if r.content else None

    def login(self):
        r = self.s.post(
            BASE + "/token",
            data={"username": self.username, "password": self.password, "grant_type": "password"},
            timeout=30,
        )
        if r.status_code != 200:
            try:
                msg = r.json().get("error_description")
            except ValueError:
                msg = None
            raise NGError(f"Login failed (HTTP {r.status_code}): {msg or 'unknown error'}")
        self.s.headers["Authorization"] = "Bearer " + r.json()["access_token"]
        graph = self._call("GET", "/GetMemberDetailGraph")
        raw = graph["members"]
        keys = first_name_keys([m.get("firstNames") for m in raw])
        self.members = [
            {"key": k, "memberID": m["memberID"], "siteID": m["sites"][0]["siteID"], "isGold": bool(m.get("isGoldMembership"))}
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

    def programs(self, d, kind=DEFAULT_KIND, who=None):
        m = self.member(who)
        data = {"dateAsISOString": d["dateAsISOString"], "siteID": m["siteID"], "memberID": m["memberID"]}
        return [normalise_program(p, kind) for p in (self._call("POST", KINDS[kind], json=data) or [])]

    def check(self, program, d, who=None):
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
        raw = program["raw"]
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


def _display_hhmm(s):
    """'7:30 PM' -> '19:30' (None if it cannot be read)."""
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*([AaPp])", str(s or ""))
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    return f"{h:02d}:{m.group(2)}"


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
        # The site appends the court/level to the booked name ('Social Doubles C1-2' -> 'Social Doubles C1-2 C1').
        have, want_name = str(b.get("className", "")).strip().lower(), class_name.strip().lower()
        if have != want_name and not have.startswith(want_name + " "):
            continue
        if not want.search(str(b["displayDate"])):
            continue
        if time_str and _display_hhmm(b.get("displayTime")) not in (None, time_str):
            continue
        return b
    return None
