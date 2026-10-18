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


def normalise_program(p):
    dt = parse_local(p["dateTimeAsISOString"])
    time_str = dt.strftime("%H:%M")
    return {
        "id": make_id(p["className"], dt.weekday(), time_str),
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
    return {k: n[k] for k in ("id", "className", "activityID", "date", "time", "weekday")} | {"info": info}


def date_of(d):
    return parse_local(d["dateAsISOString"]).date()


class NGClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0 (compatible; ngbooker)"
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
        member = graph["members"][0]
        self.member_id = member["memberID"]
        self.site_id = member["sites"][0]["siteID"]
        self.is_gold = bool(member.get("isGoldMembership"))

    # -- API ---------------------------------------------------------------
    def dates(self):
        return self._call("GET", f"/GetAvailableBookingDates?siteID={self.site_id}")

    def is_blocked(self, d):
        if self.is_gold:
            return bool(d.get("goldAvailableAfterHour"))
        return bool(d.get("goldOnly") or d.get("availableAfterHour"))

    def programs(self, d):
        data = {
            "dateAsISOString": d["dateAsISOString"],
            "siteID": self.site_id,
            "memberID": self.member_id,
        }
        return [normalise_program(p) for p in (self._call("POST", "/AvailableTennisProgramsforDate", json=data) or [])]

    def check(self, program, d):
        data = {
            "dateAsISOString": d["dateAsISOString"],
            "memberID": self.member_id,
            "isGoldMember": self.is_gold,
            "bookingsAvailableAfter": d.get("bookingsAvailableAfterHour"),
            "siteID": self.site_id,
            "selectedClassName": program["className"],
            "activityID": program["activityID"],
        }
        return self._call("POST", "/CheckAvailability", json=data)

    def book(self, program):
        raw = program["raw"]
        data = {
            "dateTimeAsISOString": raw["dateTimeAsISOString"],
            "activityID": raw["activityID"],
            "memberID": self.member_id,
            "siteID": self.site_id,
            "dateTime": raw.get("dateTime"),
            "className": raw["className"],
        }
        return self._call("POST", "/MakeBooking", json=data)

    def my_bookings(self):
        data = self._call("GET", f"/GetMemberBookings/{self.member_id}")
        return (data or {}).get("item1") or []


def client_from_env():
    user = os.environ.get("NG_MEMBER_NUMBER")
    pw = os.environ.get("NG_PASSWORD")
    if not user or not pw:
        raise NGError("NG_MEMBER_NUMBER / NG_PASSWORD environment variables are not set")
    c = NGClient(user, pw)
    c.login()
    return c


def find_booking(bookings, class_name, date_iso):
    """Best-effort match of an existing booking (the exact field names are not known up front)."""
    for b in bookings or []:
        text = json.dumps(b, default=str).lower()
        if class_name.lower() in text and date_iso in text:
            return b
    return None
