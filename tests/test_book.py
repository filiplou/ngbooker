"""Offline tests: run with  python -m unittest discover -s tests  (no network, no credentials)."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import book  # noqa: E402
import ngclient  # noqa: E402
from ngclient import SYD, NGError, normalise_program, sanitize  # noqa: E402


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

    def programs(self, d):
        return self.progs

    def check(self, prog, d):
        return {"isWaitingList": self.waitlist}

    def book(self, prog):
        if self.book_error:
            raise NGError(self.book_error)
        self.booked.append(prog["id"])
        return True

    def my_bookings(self):
        mine = [{"className": "Social Tennis", "displayDate": self.day.strftime("%A %d %B"), "displayTime": "6:30 PM", "isOnWaitList": self.waitlist}] if self.booked else []
        return self.existing + mine


class Tests(unittest.TestCase):
    def setUp(self):
        self.day = (datetime.now(SYD) + timedelta(days=7)).date()
        self.dt = datetime(self.day.year, self.day.month, self.day.day, 18, 30, tzinfo=SYD)
        self.prog = make_prog(self.dt)
        self.sel = [{"id": self.prog["id"], "className": "Social Tennis", "time": "18:30", "weekday": self.dt.weekday()}]

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

    def test_not_published_yet(self):
        c, log = FakeClient(self.day, []), []
        pending = self.run_pass(c, log)
        self.assertEqual(pending, {(self.prog["id"], self.day.isoformat())})
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

    def test_naive_time_treated_as_sydney(self):
        self.assertEqual(ngclient.parse_local("2026-01-05T18:30:00").tzinfo, SYD)


if __name__ == "__main__":
    unittest.main()
