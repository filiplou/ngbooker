# Progress: Court Bookings & Tennis Socials Enhancement

## Status / Overview
- [x] 1. Environment & Setup
  - [x] Analyze codebase architecture (scripts, data branch, GitHub Pages dashboard)
  - [x] Diagnose Windows tzdata dependency and implement fallback in `ngclient.py`
  - [x] Add `tzdata` to `requirements.txt`
- [x] 2. Investigation of Court Bookings vs Tennis Programs
  - [x] Inspect existing `index.html`, `scripts/ngclient.py`, `scripts/book.py`, `scripts/scrape.py`
  - [x] Analyze live Club Lounge frontend controllers (`racquetsBookings.js`, `racquetsBookings.html`, `ClubLoungeApp.es5.js`, `TheClubLoungeJS.js`)
  - [x] Confirm endpoints:
    - `GetAvailableFacilitiesForSite` (retrieves facilities, court types, and duration activityIDs)
    - `GetSlotAvailabilityForActivity` (retrieves daily slot availability grid for a court activity)
    - `MakeActivityBooking` (creates court booking with `FacilityType`, `ResourceProductID`, `startDateTime`, `activityID`, `memberID`, `siteID`)
  - [x] Map UI and user requirements:
    - 1: Rename "Racquets" to "Tennis Socials"
    - 2: Add new button "Courts"
- [x] 3. Dashboard (`index.html`) Enhancements
  - [x] Update `KIND_LABEL` to `{ racquet: "Tennis Socials", courts: "Courts", groupex: "Group exercise" }`
  - [x] Update slot/session display helper (`spacesText`) for courts ("Available" / "Unavailable")
  - [x] Enhance booking matching (`nameMatches`) for court bookings
- [x] 4. Backend (`scripts/`) Enhancements
  - [x] Implement Sydney timezone fallback in `ngclient.py` for environments without tzdata
  - [x] Add `"courts"` to `KINDS` in `ngclient.py`
  - [x] Implement `facilities()`, `courts()`, `normalise_court_slot()`, and court handling in `programs()` in `ngclient.py`
  - [x] Implement `MakeActivityBooking` in `NGClient.book()` for `kind == "courts"`
  - [x] Update `NGClient.check()` for courts
  - [x] Enhance `find_booking()` to match court bookings flexibly across site naming formats
  - [x] Verify `scrape.py` and `book.py` handle `"courts"` seamlessly
  - [x] Update `discover.py` to inspect court availability
- [x] 5. Testing & Verification
  - [x] Add unit tests for court slot normalization, booking flow, API payloads, and court matching in `tests/test_book.py`
  - [x] Run complete test suite (all 21 unit tests passing)
- [x] 6. Documentation
  - [x] Update `Project.md` with Tennis Socials and Courts descriptions
  - [x] Create and maintain `progress.md`
- [x] 7. Follow-up fixes
  - [x] Exclude squash facilities from the Courts list
  - [x] Include duration in court selection names and IDs, and verify multiple durations
  - [x] Match court bookings without confusing court identifiers with social-session identifiers
  - [x] Prevent booking a court that is unavailable when the booking workflow rechecks it
  - [x] Verify the complete suite (23 tests passing)
- [x] 8. Scheduling & timeout hardening
  - [x] Remove the GitHub `schedule:` triggers from `book.yml` (scheduler ran 1-3 h late and always missed the window)
  - [x] Keep the external cron-job.org `workflow_dispatch` trigger as the sole scheduled run
  - [x] Re-check My Bookings after ambiguous `MakeBooking` failures (timeout / connection / 5xx) before recording "failed"
  - [x] Add regression tests for landed-vs-not-landed ambiguous failures
  - [x] Raise `MakeBooking` / `MakeActivityBooking` timeouts to 60 s
  - [x] Add per-API-call timing logs (`[api] METHOD path -> status in Xs`)
  - [x] Retry failed scheduled bookings up to 3 attempts, 5 minutes apart (skipped once visible in My Bookings)
  - [x] Update `Project.md` trigger documentation
  - [x] Verify the complete suite (39 tests passing)

- [x] Review-findings fixes (`issue.md`)
  - [x] Dashboard: `saveSettings` waits for the save, handles 409, and edits are blocked from racing with pause/remove
  - [x] Only book courts whose status is `Available`; otherwise `not-published` (now) / keep polling (scheduled)
  - [x] Make the scrape test date-independent (dates relative to today)
  - [x] Separate concurrency groups (`ng-book`, `ng-scrape`) and retry `git push` after `pull --rebase`
  - [x] Retry login on HTTP 429 / 5xx
  - [x] `--release` defaults to `None`, falling back to `selections.json` `releaseTime`, then `08:01`
  - [x] Correct retry count (5, not 3) in `Project.md`; document final-state and concurrency behaviour
  - [x] Verify the complete suite (49 tests passing)

- [x] Remaining review-findings fixes (issue.md, README excluded)
  - [x] Court booking no longer re-checks live availability (documented); removed dead saved_court_program$nl  - [x] Failure is judged per selection: failed only if no candidate booked/waitlisted/already
  - [x] Dashboard 
ameMatches aligned with ind_booking(); token Remember unticked by default with stronger warning
  - [x] Use zoneinfo for Sydney time; pin GitHub Actions to commit SHAs
  - [x] Verify the complete suite (51 tests passing)
