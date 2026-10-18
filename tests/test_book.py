"""Offline tests: run with  python -m unittest discover -s tests  (no network, no credentials)."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import book  # noqa: E402
import ngclient  # noqa: E402
from ngclient import SYD, NGError, first_name_keys, normalise_program, normalise_selection, sanitize  # noqa: E402


def make_prog(dt, name="Social Tennis"):
    raw = {"className": name, "activityID": 7, "dateTimeAsISOString": dt.isoformat(), "dateTime": "x", "memberID": "SECRET"}
    return normalise_program(raw)


class FakeClient:
    site_id = "TCL"
    is_gold = False

    def __init__(self, day, progs, waitlist=False, book_error=None, existing=None):
        self.day, self.progs, self.waitlist, self.book_error = day, progs, waitlist, book_error
        self.booked, self.existing = [], existing or []

    def dates(self):
        return [{"dateAsISOString": self.day.isoformat() + "T00:00:00"}]

    def is_blocked(self, d):
        return False

    def programs(self, d, kind="racquet", who=None):
        self.asked = getattr(self, "asked", []) + [(kind, who)]
        return [p for p in self.progs if p.get("kind", "racquet") == kind]

    def check(self, prog, d, who=None):
        return {"isWaitingList": self.waitlist}

    def book(self, prog, who=None):
        if self.book_error:
            raise NGError(self.book_error)
        self.booked.append(prog["id"])
        self.booked_for = getattr(self, "booked_for", []) + [who]
        return True

    def my_bookings(self, who=None):
        mine = [{"className": "Social Tennis", "displayDate": self.day.strftime("%A %d %B"), "displayTime": "6:30 PM", "isOnWaitList": self.waitlist}] if self.booked else []
        return self.existing + mine


class Tests(unittest.TestCase):
    def setUp(self):
        self.day = (datetime.now(SYD) + timedelta(days=7)).date()
        self.dt = datetime(self.day.year, self.day.month, self.day.day, 18, 30, tzinfo=SYD)
        self.prog = make_prog(self.dt)
        self.sel = [normalise_selection({"id": self.prog["id"], "className": "Social Tennis", "time": "18:30", "weekday": self.dt.weekday()}, "Filip")]

    def run_pass(self, client, log):
        with mock.patch.object(book, "date_of", lambda d: self.day):
            return book.attempt_pass(client, self.sel, log, "now")

    def test_books_open_slot(self):
        c, log = FakeClient(self.day, [self.prog]), []
        self.run_pass(c, log)
        self.assertEqual(c.booked, [self.prog["id"]])
        self.assertEqual(log[0]["result"], "booked")

    def test_waiting_list(self):
        c, log = FakeClient(self.day, [self.prog], waitlist=True), []
        self.run_pass(c, log)
        self.assertEqual(log[0]["result"], "waitlisted")

    def test_waiting_list_from_real_program_fields(self):
        raw = {"className": "Social Tennis", "activityID": "A1", "dateTimeAsISOString": self.dt.strftime("%Y-%m-%d %H:%M:%S"),
               "dateTime": self.dt.strftime("%Y-%m-%dT%H:%M:%S"), "spacesAvailable": -2, "bookButtonDisplayString": "Book into waiting list"}
        prog = normalise_program(raw)
        c, log = FakeClient(self.day, [prog]), []
        c.my_bookings = lambda who=None: []
        self.run_pass(c, log)
        self.assertEqual(log[0]["result"], "waitlisted")

    def test_not_published_yet(self):
        c, log = FakeClient(self.day, []), []
        pending = self.run_pass(c, log)
        self.assertEqual(pending, {(self.sel[0]["id"], self.day.isoformat())})
        self.assertEqual(log, [])

    def test_no_double_booking(self):
        c, log = FakeClient(self.day, [self.prog]), []
        self.run_pass(c, log)
        self.run_pass(c, log)
        self.assertEqual(len(c.booked), 1)

    def test_failure_is_retried_not_marked_done(self):
        c, log = FakeClient(self.day, [self.prog], book_error="boom"), []
        self.run_pass(c, log)
        self.assertEqual(log[0]["result"], "failed")
        c.book_error = None
        self.run_pass(c, log)
        self.assertEqual(c.booked, [self.prog["id"]])

    def test_sanitize_strips_personal_keys(self):
        self.assertEqual(sanitize({"a": 1, "memberID": "x", "n": [{"email": "e", "ok": 2}]}), {"a": 1, "n": [{"ok": 2}]})

    def test_id_is_stable_across_weeks(self):
        a, b = make_prog(self.dt), make_prog(self.dt + timedelta(days=7))
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(a["date"], b["date"])

    def test_find_booking_real_shape(self):
        b = [{"className": "Social Doubles C1-2 C1", "displayDate": "Tuesday 20 October", "displayTime": "7:30 PM"}]
        self.assertIsNotNone(ngclient.find_booking(b, "Social Doubles C1-2 C1", "2026-10-20", "19:30"))
        self.assertIsNone(ngclient.find_booking(b, "Social Doubles C1-2 C1", "2026-10-20", "18:30"))
        self.assertIsNone(ngclient.find_booking(b, "Social Doubles C1-2 C1", "2026-10-27", "19:30"))
        self.assertIsNone(ngclient.find_booking(b, "Other", "2026-10-20", "19:30"))

    def test_legacy_selection_defaults_to_racquet_for_first_member(self):
        s = normalise_selection({"id": "Social Tennis|1|18:30", "className": "Social Tennis", "time": "18:30", "weekday": 1}, "Filip")
        self.assertEqual((s["kind"], s["who"], s["progId"]), ("racquet", "Filip", "Social Tennis|1|18:30"))
        self.assertEqual(s["id"], "racquet|Filip|Social Tennis|1|18:30")
        # Normalising an already-normalised selection must not change it.
        self.assertEqual(normalise_selection(s, "Other"), s)

    def test_selection_for_another_member_and_kind(self):
        gx = normalise_program({"className": "Hatha Yoga", "activityID": "G1", "dateTimeAsISOString": self.dt.isoformat(), "dateTime": "x"}, "groupex")
        sel = [normalise_selection({"id": gx["id"], "kind": "groupex", "who": "Annika", "className": "Hatha Yoga",
                                    "time": "18:30", "weekday": self.dt.weekday()}, "Filip")]
        c, log = FakeClient(self.day, [self.prog, gx]), []
        with mock.patch.object(book, "date_of", lambda d: self.day):
            book.attempt_pass(c, sel, log, "now")
        self.assertEqual(c.booked, [gx["id"]])
        self.assertEqual(c.booked_for, ["Annika"])
        self.assertEqual(c.asked, [("groupex", "Annika")])
        self.assertEqual((log[0]["who"], log[0]["kind"], log[0]["result"]), ("Annika", "groupex", "booked"))

    def test_same_session_for_two_members_has_distinct_ids(self):
        both = [normalise_selection({"id": self.prog["id"], "who": w, "className": "Social Tennis", "time": "18:30",
                                     "weekday": self.dt.weekday()}, "Filip") for w in ("Filip", "Annika")]
        self.assertNotEqual(both[0]["id"], both[1]["id"])
        c, log = FakeClient(self.day, [self.prog]), []
        c.my_bookings = lambda who=None: []
        with mock.patch.object(book, "date_of", lambda d: self.day):
            book.attempt_pass(c, both, log, "now")
        self.assertEqual(c.booked_for, ["Filip", "Annika"])

    def test_first_name_keys_are_unique(self):
        self.assertEqual(first_name_keys(["Filip", "Annika Jo", None, "Filip"]), ["Filip", "Annika", "Member3", "Filip4"])

    def test_naive_time_treated_as_sydney(self):
        self.assertEqual(ngclient.parse_local("2026-01-05T18:30:00").tzinfo, SYD)


if __name__ == "__main__":
    unittest.main()
