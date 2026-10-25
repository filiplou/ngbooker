"""Offline tests for the session refresh workflow."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import scrape  # noqa: E402
from ngclient import SYD, normalise_court_slot, normalise_program  # noqa: E402


class FakeClient:
    def __init__(self, day, members, programs):
        self.day = day
        self.members = [{"key": name} for name in members]
        self.programs_by_kind = programs
        self.program_calls = []
        self.booking_calls = []

    def dates(self):
        return [
            {"dateAsISOString": (self.day + timedelta(days=offset)).isoformat() + "T00:00:00"}
            for offset in range(7)
        ]

    def is_blocked(self, _date):
        return False

    def programs(self, date, kind, who):
        self.program_calls.append((kind, who))
        target_date = date["dateAsISOString"][:10]
        return [program for program in self.programs_by_kind[kind] if program["date"] == target_date]

    def my_bookings(self, who):
        self.booking_calls.append(who)
        return [{"className": f"Booking for {who}"}]


class ScrapeTests(unittest.TestCase):
    def test_future_bookable_dates_excludes_today_and_past(self):
        today = datetime(2026, 10, 25, tzinfo=SYD).date()
        days = [
            {"dateAsISOString": (today + timedelta(days=offset)).isoformat() + "T00:00:00"}
            for offset in (-1, 0, 1, 2)
        ]

        self.assertEqual(
            scrape.future_bookable_dates(days, today),
            days[2:],
        )

    def test_fetches_sessions_once_and_assigns_them_to_every_member(self):
        day = datetime(2026, 10, 30, tzinfo=SYD).date()
        date_time = datetime(2026, 10, 30, 9, 30, tzinfo=SYD)
        programs = {
            "racquet": [normalise_program({
                "className": "Tennis Social",
                "activityID": "SOCIAL1",
                "dateTimeAsISOString": date_time.isoformat(),
            })],
            "courts": [
                normalise_court_slot(
                    {"productName": "1", "dateTime": date_time.isoformat(), "status": status},
                    {"facilityName": "SYNTH COURTS"},
                    {"duration": duration, "activityID": f"COURT{duration}"},
                    day,
                )
                for duration, status in ((30, "Available"), (60, "Available"), (90, "NotAvailable"))
            ],
            "groupex": [normalise_program({
                "className": "Yoga",
                "activityID": "YOGA1",
                "dateTimeAsISOString": date_time.isoformat(),
            }, "groupex")],
        }
        client = FakeClient(day, ["Filip", "Theresa", "Annika"], programs)

        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(scrape, "client_from_env", return_value=client), \
                    mock.patch.object(scrape, "DATA", Path(temp_dir)), \
                    contextlib.redirect_stdout(io.StringIO()):
                scrape.main()

            sessions = json.loads((Path(temp_dir) / "sessions.json").read_text(encoding="utf-8"))["sessions"]
            available_dates = json.loads((Path(temp_dir) / "sessions.json").read_text(encoding="utf-8"))["availableDates"]
            bookings = json.loads((Path(temp_dir) / "my-bookings.json").read_text(encoding="utf-8"))["bookings"]
            members = json.loads((Path(temp_dir) / "members.json").read_text(encoding="utf-8"))["members"]

        self.assertEqual(
            client.program_calls,
            [ (kind, "Filip") for kind in ("racquet", "courts", "groupex") for _ in range(7)],
        )
        self.assertEqual(client.booking_calls, ["Filip", "Theresa", "Annika"])
        self.assertEqual(members, ["Filip", "Theresa", "Annika"])
        self.assertEqual(available_dates, [(day + timedelta(days=offset)).isoformat() for offset in range(7)])
        self.assertEqual(len(sessions), 4)
        court_sessions = [session for session in sessions if session["kind"] == "courts"]
        self.assertEqual(len(court_sessions), 2)
        self.assertTrue(all(session["info"]["status"] == "Available" for session in court_sessions))
        self.assertTrue(all(session["who"] == members for session in sessions))
        self.assertEqual(len(bookings), 3)

    def test_court_log_distinguishes_duration_records_from_unique_slots(self):
        day = datetime(2026, 10, 30, tzinfo=SYD).date()
        date_time = datetime(2026, 10, 30, 9, 30, tzinfo=SYD)
        courts = [
            scrape.public_view(normalise_court_slot(
                {"productName": court, "dateTime": date_time.isoformat(), "status": "Available"},
                {"facilityName": "SYNTH COURTS"},
                {"duration": duration, "activityID": f"COURT{duration}"},
                day,
            ))
            for duration in (30, 60)
            for court in ("1", "2")
        ]

        self.assertEqual(
            scrape.court_count_summary(courts),
            "4 court slot-duration records, 2 unique court/time slots (30 min: 2, 60 min: 2)",
        )


if __name__ == "__main__":
    unittest.main()
