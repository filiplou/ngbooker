"""Offline tests for renamed classes, error classification, retries, summaries and error messages."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import book  # noqa: E402
import ngclient  # noqa: E402
from ngclient import SYD, NGError, normalise_program, normalise_selection  # noqa: E402
from test_book import FakeClient  # noqa: E402

DAY = datetime(2026, 11, 2).date()  # a Monday, 7 days after the scheduled run below
RUN_START = datetime(2026, 10, 26, 7, 55, tzinfo=SYD)


def prog(name, hhmm="09:10", kind="groupex", day=DAY):
    hh, mm = map(int, hhmm.split(":"))
    dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=SYD)
    return normalise_program({"className": name, "activityID": "A" + name, "dateTimeAsISOString": dt.isoformat(),
                              "dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S")}, kind)


def sel(name, hhmm="09:10", kind="groupex", weekday=0):
    return normalise_selection({"id": ngclient.make_id(name, weekday, hhmm), "kind": kind, "who": "Theresa",
                                "className": name, "time": hhmm, "weekday": weekday}, "Theresa")


class Clock:
    """Fake Sydney clock: time.sleep() moves it forward."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


def run_main(argv, sels, client, clock, summary_path=None):
    data = {"selections.json": {"selections": sels}, "bookings-log.json": [], "booking-times.json": []}
    saved = []
    client.member_keys = lambda: ["Theresa"]
    env = {"GITHUB_STEP_SUMMARY": summary_path} if summary_path else {}
    with (
        mock.patch("sys.argv", ["book.py"] + argv),
        mock.patch.object(book, "now", side_effect=clock.now),
        mock.patch("book.time.sleep", side_effect=clock.sleep),
        mock.patch.object(book, "load", side_effect=lambda name, default: data[name]),
        mock.patch.object(book, "client_from_env", return_value=client),
        mock.patch.object(book, "save", side_effect=lambda name, v: saved.extend(v) if name == "bookings-log.json" else None),
        mock.patch.dict(os.environ, env, clear=False),
    ):
        code = book.main()
    return code, saved


class RenamedClassTests(unittest.TestCase):
    def pass_(self, client, sels, mode="now"):
        log, reasons = [], {}
        with mock.patch.object(book, "date_of", lambda d: DAY), \
                mock.patch.object(book, "now", return_value=RUN_START):
            pending, _ = book.attempt_pass(client, sels, log, mode, only_date=DAY, reasons=reasons)
        return log, pending, reasons

    def test_renamed_class_at_the_same_time_is_booked(self):
        renamed = prog("Sculpt Booty Band Class", "08:30")
        c = FakeClient(DAY, [prog("Yoga", "08:30"), renamed])
        log, pending, _ = self.pass_(c, [sel("Sculpt Class", "08:30")])
        self.assertEqual(c.booked, [renamed["id"]])
        self.assertEqual(log[0]["result"], "booked")
        self.assertEqual(log[0]["className"], "Sculpt Class")  # still logged under the selection's name
        self.assertIn("booked 'Sculpt Booty Band Class'", log[0]["message"])
        self.assertEqual(pending, set())

    def test_different_level_at_the_same_time_is_never_booked(self):
        c = FakeClient(DAY, [prog("Social Doubles C3-4", "19:30", "racquet")])
        log, pending, reasons = self.pass_(c, [sel("Social Doubles C1-2", "19:30", "racquet")])
        self.assertEqual((c.booked, log), ([], []))
        self.assertEqual(len(pending), 1)
        self.assertIn("at 19:30 there is: Social Doubles C3-4", reasons[next(iter(pending))])

    def test_two_equally_good_renames_are_not_guessed(self):
        c = FakeClient(DAY, [prog("Sculpt Class A", "08:30"), prog("Sculpt Class B", "08:30")])
        log, pending, _ = self.pass_(c, [sel("Sculpt Class", "08:30")])
        self.assertEqual(c.booked, [])
        self.assertEqual(len(pending), 1)

    def test_not_found_reason_mentions_a_moved_class(self):
        c = FakeClient(DAY, [prog("Sculpt Class", "09:40")])
        _, pending, reasons = self.pass_(c, [sel("Sculpt Class", "08:30")])
        self.assertIn("it runs at 09:40 instead", reasons[next(iter(pending))])

    def test_now_mode_records_why_a_class_was_not_found(self):
        c = FakeClient(DAY, [prog("Pilates", "09:10")])
        c.dates = lambda: [{"dateAsISOString": "2026-11-02T00:00:00"}]
        code, saved = run_main(["--mode", "now"], [sel("Spin", "09:10")], c, Clock(RUN_START))
        self.assertEqual(saved[-1]["result"], "not-published")
        self.assertIn("'Spin' is not on the timetable at 09:10", saved[-1]["message"])
        self.assertEqual(code, 1)


class TryBookTests(unittest.TestCase):
    def setUp(self):
        self.p = prog("Sculpt Booty Band", "09:10")

    def test_failed_availability_check_still_books(self):
        client = mock.Mock()
        client.check.side_effect = NGError("Request timed out: /CheckAvailability")
        client.book.return_value = True
        client.my_bookings.return_value = []
        result, message = book.try_book(client, {}, self.p, "Theresa")
        self.assertEqual(result, "booked")
        self.assertIn("availability check failed", message)
        client.book.assert_called_once()

    def test_conflict_from_availability_check_does_not_book(self):
        client = mock.Mock()
        client.check.side_effect = NGError("You have another concurrent (at the same time) reservation.")
        result, _ = book.try_book(client, {}, self.p, "Theresa")
        self.assertEqual(result, "conflict")
        client.book.assert_not_called()

    def test_unconfirmed_booking_shows_the_response_without_personal_data(self):
        client = mock.Mock()
        client.check.return_value = {}
        client.book.return_value = {"ok": False, "memberID": "SECRET99"}
        result, message = book.try_book(client, {}, self.p, "Theresa")
        self.assertEqual(result, "failed")
        self.assertIn('"ok": false', message)
        self.assertNotIn("SECRET99", message)


class ScheduledRunTests(unittest.TestCase):
    def test_conflict_is_not_retried_and_fails_the_run(self):
        c = FakeClient(DAY, [prog("Spin")], book_error="You have another concurrent (at the same time) reservation")
        c.book = mock.Mock(side_effect=NGError("You have another concurrent (at the same time) reservation"))
        with tempfile.TemporaryDirectory() as tmp:
            summary = os.path.join(tmp, "summary.md")
            code, saved = run_main(["--mode", "scheduled"], [sel("Spin")], c, Clock(RUN_START), summary)
            text = Path(summary).read_text(encoding="utf-8")
        self.assertEqual(c.book.call_count, 1)
        self.assertEqual([e["result"] for e in saved], ["conflict"])
        self.assertEqual(code, 1)
        self.assertIn("| Theresa | Spin | 2026-11-02 | 09:10 |", text)
        self.assertIn("conflict", text)

    def test_failed_booking_is_retried_after_30s_while_another_selection_is_still_pending(self):
        c = FakeClient(DAY, [prog("Spin")])
        c.my_bookings = lambda who=None: []
        c.book = mock.Mock(side_effect=[NGError("HTTP 400 POST /MakeBooking: boom"), True])
        clock = Clock(RUN_START)
        code, saved = run_main(["--mode", "scheduled"], [sel("Spin"), sel("Missing Class")], c, clock)
        spin = [e for e in saved if e["className"] == "Spin"]
        self.assertEqual([e["result"] for e in spin], ["failed", "booked"])
        gap = datetime.fromisoformat(spin[1]["time"]) - datetime.fromisoformat(spin[0]["time"])
        self.assertTrue(timedelta(seconds=30) <= gap < timedelta(seconds=45), gap)
        missing = [e for e in saved if e["className"] == "Missing Class"]
        self.assertEqual(missing[-1]["result"], "not-published")
        self.assertIn("'Missing Class' is not on the timetable", missing[-1]["message"])
        self.assertEqual(code, 1)

    def test_day_that_is_never_released_is_reported(self):
        c = FakeClient(DAY - timedelta(days=1), [])  # only yesterday's date is ever listed
        code, saved = run_main(["--mode", "scheduled"], [sel("Spin")], c, Clock(RUN_START))
        self.assertEqual(saved[-1]["result"], "not-published")
        self.assertIn("was not released by the club", saved[-1]["message"])
        self.assertEqual(code, 1)

    def test_unexpected_error_in_a_pass_keeps_polling(self):
        c = FakeClient(DAY, [prog("Spin")])
        c.my_bookings = lambda who=None: []
        real = c.programs
        calls = []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise KeyError("dateTimeAsISOString")
            return real(*a, **kw)

        c.programs = flaky
        code, saved = run_main(["--mode", "scheduled"], [sel("Spin")], c, Clock(RUN_START))
        self.assertEqual([e["result"] for e in saved], ["booked"])
        self.assertEqual(code, 0)

    def test_polling_backs_off_after_two_minutes(self):
        c = FakeClient(DAY - timedelta(days=1), [])
        clock = Clock(RUN_START)
        sleeps = []
        real_sleep = clock.sleep
        clock.sleep = lambda s: (sleeps.append(s), real_sleep(s))
        run_main(["--mode", "scheduled"], [sel("Spin")], c, clock)
        self.assertEqual(sleeps[1], 3.0)
        self.assertEqual(sleeps[-1], book.SLOW_POLL)
        self.assertLess(len(sleeps), 120)  # ~40 fast polls + ~40 slow ones over the 12-minute window


class ClientErrorTests(unittest.TestCase):
    def client(self, response):
        c = ngclient.NGClient("u", "p")
        c.s = mock.Mock()
        c.s.request.return_value = response
        return c

    def test_non_json_success_raises_ngerror_with_the_body(self):
        r = mock.Mock(status_code=200, content=b"<html>", text="<html><body><h1>Down for maintenance</h1></body></html>")
        r.json.side_effect = ValueError("no json")
        with self.assertRaisesRegex(NGError, r"non-JSON response from GET /GetAvailableBookingDates.*Down for maintenance"):
            self.client(r)._call("GET", "/GetAvailableBookingDates")

    def test_http_error_without_json_shows_the_body(self):
        r = mock.Mock(status_code=503, content=b"x", text="<html><title>503</title><p>Service Unavailable</p></html>")
        r.json.side_effect = ValueError("no json")
        with self.assertRaisesRegex(NGError, r"HTTP 503 POST /MakeBooking: 503 Service Unavailable"):
            self.client(r)._call("POST", "/MakeBooking")

    def test_error_paths_hide_member_ids(self):
        r = mock.Mock(status_code=404, content=b"x", text="not found")
        r.json.side_effect = ValueError("no json")
        with self.assertRaises(NGError) as ctx:
            self.client(r)._call("GET", "/GetMemberBookings/M12345")
        self.assertNotIn("M12345", str(ctx.exception))

    def test_snippet_masks_personal_data_and_truncates(self):
        out = ngclient._snippet("<b>Member</b> 1234567 jo@example.com " + "x" * 300)
        self.assertIn("[number]", out)
        self.assertIn("[email]", out)
        self.assertNotIn("1234567", out)
        self.assertTrue(out.endswith("..."))

    def test_login_without_token_is_retried_with_a_clear_error(self):
        c = ngclient.NGClient("u", "p")
        c.s = mock.Mock()
        c.s.headers = {}
        bad = mock.Mock(status_code=200, content=b"<html>", text="<html>Login page</html>")
        bad.json.side_effect = ValueError("no json")
        c.s.post.return_value = bad
        with mock.patch.object(ngclient.time, "sleep"):
            with self.assertRaisesRegex(NGError, "no access token.*Login page"):
                c.login(retries=2)
        self.assertEqual(c.s.post.call_count, 2)

    def test_courts_for_booking_query_only_30_minute_slots_with_retries(self):
        c = ngclient.NGClient("u", "p")
        c.members = [{"key": "Filip", "memberID": "M1", "siteID": "RY", "isAceMembership": True}]
        c._facilities_cache = {"RY": [{"facilityName": "SYNTH COURTS", "durations": [
            {"activityID": "ACT30", "duration": 30}, {"activityID": "ACT60", "duration": 60},
            {"activityID": "ACT90", "duration": 90}]}]}
        asked = []

        def fake_call(method, path, **kw):
            asked.append((kw["json"]["activityID"], kw.get("retries")))
            return []

        c._call = fake_call
        c.programs({"dateAsISOString": "2026-11-08T00:00:00"}, "courts", "Filip", duration=30)
        self.assertEqual(asked, [("ACT30", ngclient.READ_RETRIES)])


if __name__ == "__main__":
    unittest.main()
