"""Offline tests: run with  python -m unittest discover -s tests  (no network, no credentials)."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import book  # noqa: E402
import ngclient  # noqa: E402
from ngclient import SYD, NGError, first_name_keys, normalise_court_slot, normalise_program, normalise_selection, sanitize  # noqa: E402


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

    def test_only_books_the_day_seven_ahead(self):
        # Same weekday, but today (and 14 days out) must never be attempted.
        for offset in (0, 14):
            other = self.day - timedelta(days=7) + timedelta(days=offset)
            prog = make_prog(datetime(other.year, other.month, other.day, 18, 30, tzinfo=SYD))
            c, log = FakeClient(other, [prog]), []
            with mock.patch.object(book, "date_of", lambda d, o=other: o):
                book.attempt_pass(c, self.sel, log, "now")
            self.assertEqual((c.booked, log), ([], []))

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
        # Session name without the court suffix still matches, but only on a word boundary.
        self.assertIsNotNone(ngclient.find_booking(b, "Social Doubles C1-2", "2026-10-20", "19:30"))
        self.assertIsNone(ngclient.find_booking(b, "Social Doubles C1", "2026-10-20", "19:30"))

    def test_court_slot_ids_include_duration(self):
        facility = {"facilityName": "SYNTH COURTS", "facilityType": "TENNIS_SYNTHETIC"}
        slot = {
            "productName": "1",
            "displayTime": "09:30",
            "dateTime": "2026-10-31T09:30:00",
            "resourceProductID": "RES999",
            "status": "Available",
        }
        short = normalise_court_slot(slot, facility, {"activityID": "ACT30", "duration": 30}, self.day)
        long = normalise_court_slot(slot, facility, {"activityID": "ACT60", "duration": 60}, self.day)
        self.assertNotEqual(short["id"], long["id"])
        self.assertNotEqual(short["className"], long["className"])

    def test_status_comes_from_my_bookings_not_the_stale_waiting_list_label(self):
        # Sold-out label on the session, but the site actually confirmed a real booking under a longer name.
        raw = {"className": "Social Tennis", "activityID": "A1", "dateTimeAsISOString": self.dt.strftime("%Y-%m-%d %H:%M:%S"),
               "dateTime": self.dt.strftime("%Y-%m-%dT%H:%M:%S"), "spacesAvailable": 0, "bookButtonDisplayString": "Book into waiting list"}
        c, log = FakeClient(self.day, [normalise_program(raw)]), []
        c.my_bookings = lambda who=None: [{"className": "Social Tennis C1", "displayDate": self.day.strftime("%A %d %B"),
                                           "displayTime": "6:30 PM", "isOnWaitList": False}] if c.booked else []
        self.run_pass(c, log)
        self.assertEqual(log[0]["result"], "booked")

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

    def test_normalise_court_slot(self):
        facility = {"facilityName": "SYNTH COURTS", "facilityType": "TENNIS_SYNTHETIC"}
        duration = {"durationName": "60 mins", "activityID": "ACT123", "duration": 60}
        slot = {
            "productName": "1",
            "displayTime": "09:30",
            "dateTime": "2026-10-31T09:30:00",
            "resourceProductID": "RES999",
            "status": "Available",
        }
        prog = normalise_court_slot(slot, facility, duration, self.day)
        self.assertEqual(prog["kind"], "courts")
        self.assertEqual(prog["className"], "SYNTH Court 1 (60 min)")
        self.assertEqual(prog["time"], "09:30")
        self.assertEqual(prog["activityID"], "ACT123")
        self.assertEqual(prog["id"], f"SYNTH Court 1 (60 min)|{prog['weekday']}|09:30")
        self.assertEqual(prog["raw"]["resourceProductID"], "RES999")
        self.assertEqual(prog["raw"]["facilityType"], "TENNIS_SYNTHETIC")
        self.assertEqual(prog["raw"]["status"], "Available")
        self.assertEqual(prog["raw"]["spacesAvailable"], 1)

    def test_selection_and_booking_for_courts(self):
        facility = {"facilityName": "SYNTH COURTS", "facilityType": "TENNIS_SYNTHETIC"}
        duration = {"durationName": "60 mins", "activityID": "ACT123", "duration": 60}
        slot = {
            "productName": "2",
            "displayTime": "09:30",
            "dateTime": self.dt.isoformat(),
            "resourceProductID": "RES102",
            "status": "Available",
        }
        court_prog = normalise_court_slot(slot, facility, duration, self.day)
        sel = [normalise_selection({"id": court_prog["id"], "kind": "courts", "who": "Filip",
                                    "className": court_prog["className"], "time": "18:30",
                                    "weekday": self.dt.weekday()}, "Filip")]
        c, log = FakeClient(self.day, [self.prog, court_prog]), []
        with mock.patch.object(book, "date_of", lambda d: self.day):
            book.attempt_pass(c, sel, log, "now")
        self.assertEqual(c.booked, [court_prog["id"]])
        self.assertEqual(c.booked_for, ["Filip"])
        self.assertEqual(c.asked, [("courts", "Filip")])
        self.assertEqual((log[0]["who"], log[0]["kind"], log[0]["result"]), ("Filip", "courts", "booked"))

    def test_court_booking_payload_calls_make_activity_booking(self):
        c = ngclient.NGClient("testuser", "testpw")
        c.members = [{"key": "Filip", "memberID": "M123", "siteID": "RY", "isGold": False}]
        facility = {"facilityName": "SYNTH COURTS", "facilityType": "TENNIS_SYNTHETIC"}
        duration = {"durationName": "60 mins", "activityID": "ACT123", "duration": 60}
        slot = {
            "productName": "2",
            "dateTime": "2026-10-31T09:30:00",
            "resourceProductID": "RES102",
            "status": "Available",
        }
        prog = normalise_court_slot(slot, facility, duration, self.day)
        captured = {}

        def fake_call(method, path, **kw):
            captured["method"] = method
            captured["path"] = path
            captured["json"] = kw.get("json")
            return True

        c._call = fake_call
        res = c.book(prog, "Filip")
        self.assertTrue(res)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/MakeActivityBooking")
        self.assertEqual(captured["json"]["FacilityType"], "TENNIS_SYNTHETIC")
        self.assertEqual(captured["json"]["ResourceProductID"], "RES102")
        self.assertEqual(captured["json"]["activityID"], "ACT123")
        self.assertEqual(captured["json"]["memberID"], "M123")
        self.assertEqual(captured["json"]["siteID"], "RY")

    def test_find_booking_court_variations(self):
        b1 = [{"className": "Synthetic Court 1", "displayDate": "Saturday 31 October", "displayTime": "9:30 AM"}]
        # Different prefixes for Court 1 at 09:30 match
        self.assertIsNotNone(ngclient.find_booking(b1, "SYNTH Court 1", "2026-10-31", "09:30"))
        self.assertIsNotNone(ngclient.find_booking(b1, "Court 1", "2026-10-31", "09:30"))
        # Different court number does not match
        self.assertIsNone(ngclient.find_booking(b1, "SYNTH Court 2", "2026-10-31", "09:30"))
        # Different time does not match
        self.assertIsNone(ngclient.find_booking(b1, "SYNTH Court 1", "2026-10-31", "10:30"))
        # Different date does not match
        self.assertIsNone(ngclient.find_booking(b1, "SYNTH Court 1", "2026-11-07", "09:30"))
        # Duration suffix in className still matches (new naming: "Synth Court 1 (60 min)")
        self.assertIsNotNone(ngclient.find_booking(b1, "SYNTH Court 1 (60 min)", "2026-10-31", "09:30"))
        # Site's real booking format: "Tennis Court Hire 0.5 Hr C3" should match "Synth Court 3 (30 min)"
        b2 = [{"className": "Tennis Court Hire 0.5 Hr C3", "displayDate": "Sunday 25 October", "displayTime": "1:30 PM"}]
        self.assertIsNotNone(ngclient.find_booking(b2, "Synth Court 3 (30 min)", "2026-10-25", "13:30"))
        self.assertIsNone(ngclient.find_booking(b2, "Synth Court 2 (30 min)", "2026-10-25", "13:30"))

    def test_client_courts_flow(self):
        c = ngclient.NGClient("testuser", "testpw")
        c.members = [{"key": "Filip", "memberID": "M123", "siteID": "RY", "isGold": False, "isAceMembership": True}]
        c.member_id = "M123"
        c.site_id = "RY"

        def fake_call(method, path, **kw):
            if "GetAvailableFacilitiesForSite" in path:
                return {
                    "facilities": [
                        {
                            "facilityName": "SYNTH COURTS",
                            "facilityType": "TENNIS_SYNTHETIC",
                            "durations": [
                                {"durationName": "30 mins", "activityID": "ACT05", "duration": 30},
                                {"durationName": "60 mins", "activityID": "ACT10", "duration": 60},
                            ],
                        },
                        {
                            "facilityName": "SQUASH COURTS",
                            "facilityType": "SQUASH",
                            "durations": [{"durationName": "60 mins", "activityID": "SQUASH10", "duration": 60}],
                        },
                    ]
                }
            if path == "/GetSlotAvailabilityForActivity":
                if kw["json"]["activityID"] == "SQUASH10":
                    self.fail("Squash availability must not be requested as tennis court availability")
                return [
                    [
                        {"productName": "1", "displayTime": "09:30", "dateTime": "2026-10-31T09:30:00",
                         "resourceProductID": "R1", "status": "Available"},
                        {"productName": "2", "displayTime": "09:30", "dateTime": "2026-10-31T09:30:00",
                         "resourceProductID": "R2", "status": "NotAvailable"},
                    ]
                ]
            return None

        c._call = fake_call
        d = {"dateAsISOString": "2026-10-31T00:00:00"}
        courts = c.programs(d, kind="courts", who="Filip")
        self.assertEqual(len(courts), 4)
        self.assertEqual(len({court["id"] for court in courts}), 4)
        self.assertEqual(courts[0]["className"], "SYNTH Court 1 (30 min)")
        self.assertEqual(courts[0]["kind"], "courts")
        self.assertEqual(courts[0]["time"], "09:30")
        self.assertEqual(courts[0]["raw"]["status"], "Available")
        self.assertEqual(courts[1]["className"], "SYNTH Court 2 (30 min)")
        self.assertEqual(courts[1]["raw"]["status"], "NotAvailable")
        self.assertEqual(courts[2]["className"], "SYNTH Court 1 (60 min)")
        self.assertTrue(c.check(courts[0], d, "Filip")["isAvailable"])
        self.assertFalse(c.check(courts[1], d, "Filip")["isAvailable"])

    def test_court_api_error_includes_facility_duration_and_date(self):
        c = ngclient.NGClient("testuser", "testpw")
        c.members = [{"key": "Filip", "memberID": "M123", "siteID": "RY", "isAceMembership": True}]
        c._facilities_cache = {
            "RY": [{
                "facilityName": "SYNTH COURTS",
                "durations": [{"durationName": "60 mins", "activityID": "ACT60", "duration": 60}],
            }]
        }
        c._call = mock.Mock(side_effect=NGError("HTTP 500 POST /GetSlotAvailabilityForActivity: Error Occured"))
        d = {"dateAsISOString": "2026-10-30T00:00:00"}

        with self.assertRaisesRegex(
            NGError,
            r"facility='SYNTH COURTS'.*duration='60 mins'.*activityID='ACT60'.*date='2026-10-30T00:00:00'",
        ):
            c.courts(d, "Filip")

    def test_http_error_includes_status_method_and_endpoint(self):
        c = ngclient.NGClient("testuser", "testpw")
        response = mock.Mock(status_code=500)
        response.json.return_value = {"Message": "Error Occured"}
        c.s.request = mock.Mock(return_value=response)

        with self.assertRaisesRegex(
            NGError,
            r"HTTP 500 GET /GetAvailableBookingDates: Error Occured",
        ):
            c._call("GET", "/GetAvailableBookingDates")

    def test_unavailable_court_is_not_booked(self):
        facility = {"facilityName": "SYNTH COURTS", "facilityType": "TENNIS_SYNTHETIC"}
        duration = {"durationName": "60 mins", "activityID": "ACT123", "duration": 60}
        slot = {
            "productName": "2",
            "displayTime": "09:30",
            "dateTime": self.dt.isoformat(),
            "resourceProductID": "RES102",
            "status": "NotAvailable",
        }
        prog = normalise_court_slot(slot, facility, duration, self.day)
        client = mock.Mock()
        client.check.return_value = {"isAvailable": False}
        result, message = book.try_book(client, {}, prog, "Filip")
        self.assertEqual(result, "failed")
        self.assertIn("unavailable", message)
        client.book.assert_not_called()


if __name__ == "__main__":
    unittest.main()
