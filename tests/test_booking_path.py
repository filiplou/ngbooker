"""Offline tests for the 08:01 critical path: what may and may not hold a booking up."""
import sys
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import book  # noqa: E402
import ngclient  # noqa: E402
from ngclient import SYD, NGError, normalise_selection  # noqa: E402
from test_book import FakeClient  # noqa: E402
from test_reliability import DAY, RUN_START, Clock, prog, run_main  # noqa: E402

TIMEOUT = "Request timed out: /GetMemberBookings/2417120"
RELEASE = datetime(2026, 10, 26, 8, 1, tzinfo=SYD)


def sel(name, hhmm="09:10", kind="groupex", who="Theresa", weekday=0):
    return normalise_selection({"id": ngclient.make_id(name, weekday, hhmm), "kind": kind, "who": who,
                                "className": name, "time": hhmm, "weekday": weekday}, who)


def this_morning():
    """Thursday 19 Nov 2026 as two selections: Filip's racquet session and Theresa's class."""
    progs = [prog("Social Doubles B2 C1-2", "19:30", "racquet"), prog("BodyPump", "09:30", "groupex")]
    sels = [sel("Social Doubles B2 C1-2", "19:30", "racquet", "Filip"), sel("BodyPump", "09:30", "groupex", "Theresa")]
    return progs, sels


class Recorder(FakeClient):
    """FakeClient that logs every call with the fake clock's time."""

    def __init__(self, clock, *a, **kw):
        super().__init__(*a, **kw)
        self.clock, self.events = clock, []
        self.my_bookings_error = None

    def dates(self):
        self.events.append(("dates", self.clock.now()))
        return super().dates()

    def my_bookings(self, who=None):
        self.events.append(("my_bookings", self.clock.now()))
        if self.my_bookings_error:
            raise NGError(self.my_bookings_error)
        return super().my_bookings(who)

    def book(self, p, who=None):
        self.events.append(("book", self.clock.now()))
        return super().book(p, who)

    def calls(self, name):
        return [t for n, t in self.events if n == name]


def pass_(client, sels, mode="now"):
    log, reasons = [], {}
    with mock.patch.object(book, "now", return_value=RUN_START):
        pending, failed = book.attempt_pass(client, sels, log, mode, only_date=DAY, reasons=reasons)
    return log, pending, failed, reasons


class MyBookingsCheckTests(unittest.TestCase):
    def test_unreadable_my_bookings_does_not_stop_the_booking(self):
        c = Recorder(Clock(RUN_START), DAY, [prog("Spin"), prog("Yoga", "10:10")])
        c.my_bookings_error = TIMEOUT
        log, pending, failed, _ = pass_(c, [sel("Spin"), sel("Yoga", "10:10")])
        self.assertEqual([e["result"] for e in log], ["booked", "booked"])
        self.assertIn("could not check My Bookings first", log[0]["message"])
        self.assertIn(TIMEOUT, log[0]["message"])
        self.assertEqual((pending, failed), (set(), set()))

    def test_failed_my_bookings_read_is_not_repeated_for_each_selection(self):
        # A slow read costs up to 3 x 30s: once per member per pass, not once per selection.
        c = Recorder(Clock(RUN_START), DAY, [prog("Spin"), prog("Yoga", "10:10")])
        c.my_bookings_error = TIMEOUT
        pass_(c, [sel("Spin"), sel("Yoga", "10:10")])
        names = [n for n, _ in c.events if n in ("my_bookings", "book")]
        # pre-check once, then each booking (and its own confirmation read)
        self.assertEqual(names, ["my_bookings", "book", "my_bookings", "book", "my_bookings"])

    def test_session_already_held_is_still_not_rebooked(self):
        held = {"className": "Spin", "displayDate": DAY.strftime("%A %d %B"), "displayTime": "9:10 AM"}
        c = FakeClient(DAY, [prog("Spin")], existing=[held])
        log, *_ = pass_(c, [sel("Spin")])
        self.assertEqual(c.booked, [])
        self.assertEqual(log[0]["result"], "already")

    def test_this_mornings_outage_still_books_both_selections(self):
        # 19 Nov 2026: GetMemberBookings timed out on every call from 08:01 until the deadline. Before the
        # fix nothing was booked and both selections were logged "not-published".
        clock = Clock(RUN_START)
        progs, sels = this_morning()
        c = Recorder(clock, DAY, progs)
        c.my_bookings_error = TIMEOUT
        code, saved = run_main(["--mode", "scheduled"], sels, c, clock)
        self.assertEqual(sorted(e["result"] for e in saved), ["booked", "booked"])
        self.assertEqual(code, 0)
        self.assertTrue(all(t <= RELEASE.replace(second=5) for t in c.calls("book")), c.calls("book"))


class IsolationTests(unittest.TestCase):
    def test_timetable_error_for_one_selection_does_not_stop_another(self):
        progs, sels = this_morning()
        c = FakeClient(DAY, progs)
        real = c.programs

        def programs(d, kind="racquet", who=None, duration=None):
            if kind == "racquet":
                raise NGError("Request timed out: /AvailableTennisProgramsforDate")
            return real(d, kind, who, duration)

        c.programs = programs
        log, pending, failed, reasons = pass_(c, sels, mode="scheduled")
        self.assertEqual([(e["who"], e["result"]) for e in log], [("Theresa", "booked")])
        self.assertEqual(pending, {(sels[0]["id"], DAY.isoformat())})
        self.assertIn("could not read the club timetable", reasons[(sels[0]["id"], DAY.isoformat())])
        self.assertEqual(failed, set())

    def test_timetable_error_in_book_now_is_a_failure_for_that_selection_only(self):
        c = FakeClient(DAY, [prog("Spin")])
        c.programs = mock.Mock(side_effect=NGError("HTTP 503 POST /AvailableClassesForDate: down"))
        log, pending, *_ = pass_(c, [sel("Spin")], mode="now")
        self.assertEqual([e["result"] for e in log], ["failed"])
        self.assertIn("HTTP 503", log[0]["message"])
        self.assertEqual(pending, set())

    def test_unexpected_error_while_booking_is_a_failed_attempt_and_the_next_selection_is_tried(self):
        c = FakeClient(DAY, [prog("Spin"), prog("Yoga", "10:10")])
        c.my_bookings = lambda who=None: []
        booked = []

        def book_call(p, who=None):
            if p["className"] == "Spin":
                raise RuntimeError("unexpected response shape")
            booked.append(p["className"])
            return True

        c.book = book_call
        with mock.patch("traceback.print_exc"):
            log, _, failed, _ = pass_(c, [sel("Spin"), sel("Yoga", "10:10")])
        self.assertEqual([e["result"] for e in log], ["failed", "booked"])
        self.assertIn("internal error (RuntimeError", log[0]["message"])
        self.assertEqual(booked, ["Yoga"])
        self.assertEqual(failed, {(sel("Spin")["id"], DAY.isoformat())})


class ParallelTests(unittest.TestCase):
    def test_units_group_by_kind_and_member_with_all_courts_together(self):
        a, b = sel("Spin", who="Theresa"), sel("Yoga", "10:10", who="Theresa")
        r = sel("Social", "19:30", "racquet", "Filip")
        c1 = dict(sel("Any court (30 min)", "13:00", "courts", "Filip"), duration=30)
        c2 = dict(sel("Any court (30 min)", "13:30", "courts", "Theresa"), duration=30)
        units = book.booking_units([a, r, c1, b, c2])
        self.assertEqual([[s["id"] for s in u] for u in units],
                         [[a["id"], b["id"]], [r["id"]], [c1["id"], c2["id"]]])

    def test_independent_selections_are_booked_at_the_same_time(self):
        progs, sels = this_morning()
        c = FakeClient(DAY, progs)
        c.my_bookings = lambda who=None: []
        both_in_flight = threading.Barrier(2, timeout=5)  # breaks unless both bookings run at once

        def book_call(p, who=None):
            both_in_flight.wait()
            return True

        c.book = book_call
        log, *_ = pass_(c, sels)
        self.assertEqual([(e["who"], e["result"]) for e in log], [("Filip", "booked"), ("Theresa", "booked")])

    def test_log_order_follows_the_selections_not_thread_timing(self):
        progs, sels = this_morning()
        c = FakeClient(DAY, progs)
        c.my_bookings = lambda who=None: []
        first_done = threading.Event()

        def book_call(p, who=None):
            if who == "Filip":
                first_done.wait(5)  # Theresa's booking finishes first
            else:
                first_done.set()
            return True

        c.book = book_call
        log, *_ = pass_(c, sels)
        self.assertEqual([e["who"] for e in log], ["Filip", "Theresa"])

    def test_each_parallel_unit_gets_its_own_session_and_they_are_reused(self):
        c = ngclient.NGClient("u", "p")
        c.s.headers["Authorization"] = "Bearer t"
        c.members = [{"key": "Filip", "memberID": "M1", "siteID": "S"}]
        first = book.worker_clients(c, 3)
        self.assertIs(first[0], c)
        self.assertEqual(len({id(x.s) for x in first}), 3)
        self.assertTrue(all(x.s.headers["Authorization"] == "Bearer t" and x.members is c.members for x in first))
        self.assertEqual([id(x) for x in book.worker_clients(c, 3)], [id(x) for x in first])

    def test_more_units_than_workers_never_share_a_client_at_once(self):
        names = [f"Class{i}" for i in range(6)]
        progs = [prog(n, f"{9 + i:02d}:00") for i, n in enumerate(names)]
        sels = [sel(n, f"{9 + i:02d}:00", who=f"M{i}") for i, n in enumerate(names)]
        busy, clash = set(), []
        lock = threading.Lock()

        class Worker(FakeClient):
            def clone(self):
                return Worker(self.day, self.progs)

            def my_bookings(self, who=None):
                return []

            def book(self, p, who=None):
                with lock:
                    if id(self) in busy:
                        clash.append(who)
                    busy.add(id(self))
                threading.Event().wait(0.02)
                with lock:
                    busy.discard(id(self))
                return True

        log, *_ = pass_(Worker(DAY, progs), sels)
        self.assertEqual(len(log), 6)
        self.assertEqual(clash, [])


class AvailabilityCheckTests(unittest.TestCase):
    def test_check_gets_one_short_attempt(self):
        client = mock.Mock()
        client.check.return_value = {}
        client.book.return_value = True
        client.my_bookings.return_value = []
        book.try_book(client, {}, prog("Spin"), "Theresa")
        self.assertEqual(client.check.call_args.kwargs, {"timeout": book.CHECK_TIMEOUT, "retries": 0})
        self.assertLessEqual(book.CHECK_TIMEOUT, 10)

    def test_client_passes_the_short_timeout_to_the_request(self):
        c = ngclient.NGClient("u", "p")
        c.members = [{"key": "Theresa", "memberID": "M1", "siteID": "S", "isGold": False}]
        c._call = mock.Mock(return_value={})
        c.check(prog("Spin"), {"dateAsISOString": "2026-11-02T00:00:00"}, "Theresa", timeout=10, retries=0)
        self.assertEqual((c._call.call_args.kwargs["timeout"], c._call.call_args.kwargs["retries"]), (10, 0))


class ReleaseTimeWorkTests(unittest.TestCase):
    def test_my_bookings_is_read_before_the_release_and_not_again_before_booking(self):
        clock = Clock(RUN_START)
        progs, sels = this_morning()
        c = Recorder(clock, DAY, progs)
        code, saved = run_main(["--mode", "scheduled"], sels, c, clock)
        self.assertEqual(code, 0)
        before = [n for n, t in c.events if t < RELEASE]
        self.assertEqual(before, ["my_bookings", "my_bookings"])  # one per member
        after = [n for n, t in c.events if t >= RELEASE]
        self.assertEqual(after[:2], ["dates", "book"])  # straight to booking after the release

    def test_booking_dates_are_read_once_per_poll(self):
        clock = Clock(RUN_START)
        progs, sels = this_morning()
        c = Recorder(clock, DAY, progs)
        run_main(["--mode", "scheduled"], sels, c, clock)
        self.assertEqual(len(c.calls("dates")), 1)

    def test_no_prefetch_when_the_run_starts_too_close_to_the_release(self):
        start = datetime(2026, 10, 26, 8, 0, 30, tzinfo=SYD)
        clock = Clock(start)
        c = Recorder(clock, DAY, [prog("Spin")])
        run_main(["--mode", "scheduled"], [sel("Spin")], c, clock)
        self.assertTrue(all(t >= RELEASE for t in c.calls("my_bookings")))

    def test_prefetch_failure_is_harmless(self):
        clock = Clock(RUN_START)
        c = Recorder(clock, DAY, [prog("Spin")])
        c.my_bookings_error = TIMEOUT
        code, saved = run_main(["--mode", "scheduled"], [sel("Spin")], c, clock)
        self.assertEqual([e["result"] for e in saved], ["booked"])
        self.assertEqual(code, 0)

    def test_book_now_reads_booking_dates_once_for_all_selections(self):
        clock = Clock(RUN_START)
        c = Recorder(clock, DAY, [prog("Spin"), prog("Yoga", "10:10")])
        code, saved = run_main(["--mode", "now"], [sel("Spin"), sel("Yoga", "10:10")], c, clock)
        self.assertEqual([e["result"] for e in saved], ["booked", "booked"])
        self.assertEqual(len(c.calls("dates")), 1)


BOOK_TIMEOUT = "Request timed out: /MakeBooking"
CLASH = ("HTTP 403 POST /MakeBooking: You have another concurrent (at the same time) reservation. Please cancel "
         "your existing reservation and try again or choose a class with a different time.")


class TimedOutThenClash(Recorder):
    """24 Nov 2026: the first MakeBooking times out but lands, My Bookings times out until `readable_at`,
    and the retry is refused as a clash with that booking. `held` is what My Bookings shows once readable."""

    def __init__(self, clock, progs, readable_at, held=None, lands=True, first_error=BOOK_TIMEOUT):
        super().__init__(clock, DAY, progs)
        self.readable_at, self.held, self.lands, self.first_error = readable_at, list(held or []), lands, first_error

    def my_bookings(self, who=None):
        self.events.append(("my_bookings", self.clock.now()))
        if self.readable_at is None or self.clock.now() < self.readable_at:
            raise NGError(TIMEOUT)
        return list(self.held)

    def book(self, p, who=None):
        self.events.append(("book", self.clock.now()))
        if len(self.calls("book")) == 1:
            if self.lands:
                self.held.append(held_entry("Social Doubles C1-2 C1", "7:30 PM"))  # the site's real shape
            raise NGError(self.first_error)
        raise NGError(CLASH)


def held_entry(name, display_time):
    return {"className": name, "displayDate": DAY.strftime("%A %d %B"), "displayTime": display_time}


def doubles():
    return [prog("Social Doubles C1-2", "19:30", "racquet")], [sel("Social Doubles C1-2", "19:30", "racquet", "Filip")]


class ProbableOwnClashTests(unittest.TestCase):
    def test_this_mornings_clash_is_confirmed_as_booked(self):
        # Before the fix this was logged "conflict" and the run failed, though the booking had landed.
        clock = Clock(RUN_START)
        progs, sels = doubles()
        c = TimedOutThenClash(clock, progs, readable_at=RELEASE.replace(minute=6))
        code, saved = run_main(["--mode", "scheduled"], sels, c, clock)
        self.assertEqual([e["result"] for e in saved], ["failed", "conflict", "booked"])
        self.assertIn(book.PROBABLY_OURS, saved[1]["message"])
        self.assertIn("confirmed in My Bookings", saved[2]["message"])
        self.assertEqual(code, 0)
        self.assertEqual(len(c.calls("book")), 2)  # confirming never sends another booking

    def test_clash_with_a_different_booking_stays_a_conflict(self):
        clock = Clock(RUN_START)
        progs, sels = doubles()
        c = TimedOutThenClash(clock, progs, readable_at=RELEASE.replace(minute=6), lands=False,
                              held=[held_entry("Pickleball Social", "7:30 PM")])
        code, saved = run_main(["--mode", "scheduled"], sels, c, clock)
        self.assertEqual([e["result"] for e in saved], ["failed", "conflict"])
        self.assertEqual(code, 1)

    def test_unreadable_my_bookings_leaves_the_conflict_and_gives_up(self):
        clock = Clock(RUN_START)
        progs, sels = doubles()
        c = TimedOutThenClash(clock, progs, readable_at=None)
        code, saved = run_main(["--mode", "scheduled"], sels, c, clock)
        self.assertEqual([e["result"] for e in saved], ["failed", "conflict"])
        self.assertIn(book.PROBABLY_OURS, saved[1]["message"])
        self.assertEqual(code, 1)
        confirm_reads = [t for t in c.calls("my_bookings") if t > c.calls("book")[-1]]
        self.assertEqual(len(confirm_reads), len(book.CONFIRM_DELAYS))

    def test_clash_without_an_earlier_timeout_is_not_second_guessed(self):
        # A plain failure before the clash (not a timeout) means nothing of ours can have landed.
        clock = Clock(RUN_START)
        progs, sels = doubles()
        c = TimedOutThenClash(clock, progs, readable_at=RUN_START, lands=False,
                              first_error="HTTP 400 POST /MakeBooking: session is full")
        code, saved = run_main(["--mode", "scheduled"], sels, c, clock)
        self.assertEqual([e["result"] for e in saved][-1], "conflict")
        self.assertNotIn(book.PROBABLY_OURS, saved[-1]["message"])
        self.assertEqual(code, 1)

    def test_book_now_after_a_timed_out_morning_run_confirms_the_booking(self):
        clock = Clock(RUN_START)
        progs, sels = doubles()
        s = sels[0]
        c = TimedOutThenClash(clock, progs, readable_at=RUN_START, held=[held_entry("Social Doubles C1-2 C1", "7:30 PM")])
        c.events.append(("book", RUN_START))  # the morning's attempt: the next book() is refused as a clash
        morning = {"time": "", "mode": "scheduled", "selectionId": s["id"], "kind": "racquet", "who": "Filip",
                   "className": s["className"], "date": DAY.isoformat(), "slot": "19:30", "result": "failed",
                   "message": f"{BOOK_TIMEOUT} - {book.NOT_FOUND_AFTER} 65s of checking"}
        old_clash = dict(morning, result="conflict", message=CLASH, date="2026-10-01")
        log = [old_clash, morning]
        # My Bookings is readable now, but the dashboard can still send "Book now" for a held session
        # if My Bookings is slow; simulate that by making the pre-check fail once.
        reads = iter([NGError(TIMEOUT)])

        def first_read_fails(who=None):
            err = next(reads, None)
            if err:
                raise err
            return list(c.held)
        c.my_bookings = first_read_fails
        with mock.patch.object(book, "now", side_effect=clock.now), mock.patch("book.time.sleep", side_effect=clock.sleep), \
                mock.patch.object(book, "manual_target_date", return_value=DAY):
            book.run_now(c, sels, log)
        self.assertEqual([e["result"] for e in log[2:]], ["conflict", "booked"])
        self.assertEqual(len(log), 4)  # the old, unrelated conflict from October is left alone


if __name__ == "__main__":
    unittest.main()
