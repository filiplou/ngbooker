# NG Booker

Automatically books (or joins the waiting list for) your chosen weekly sessions at
The Club Lounge (Next Gen Clubs, Ryde, Sydney), the moment new days are released at **08:01 Sydney time** (configurable in Settings).

Supports **Tennis Socials**, **Courts**, and **Group exercise** bookings, for up to 5 members.

```
main branch  : code (scripts/, workflows, index.html = dashboard on GitHub Pages)
data branch  : selections.json, sessions.json, members.json, my-bookings.json, bookings-log.json, vault.json

Dashboard --GitHub API--> data branch (selections.json)
cron-job.org (07:55 Sydney, daily) --workflow_dispatch--> book.yml (GitHub's own cron is a late backup)
book.yml --runs from main, environment "club"-->
   logs in via the club site's own API, waits for the configured release time, books the day 7 days ahead,
   writes bookings-log.json to the data branch
scrape.yml (daily / on demand) --> sessions.json + members.json + my-bookings.json on the data branch
```

The refresh workflow has a 10-minute timeout. Since available sessions are shared across members, each session
kind is fetched once per date using the first member; those sessions are then associated with every member.
Current bookings are still fetched separately for each member. Court availability is returned as a grid per
duration, so its log distinguishes slot-duration records from unique court/time slots. If an individual lookup
fails, the warning includes the request context and the scraper continues, so that lookup may be missing from
the refreshed data.

The scripts call the same web API as the club website (`/token`, `/api/ClubLounge/...`), so no browser
is needed. Selections are stored by *class name + weekday + time*, so a choice repeats every week.

## Dashboard tabs

**Sessions** — pick which weekly sessions to auto-book, per member and per kind (Tennis Socials / Courts / Group exercise).
Date tabs include every bookable date reported by the club, including dates with no available sessions. Court availability
is shown as a time-by-court grid with selectable duration options; unavailable court slots are hidden.
Court availability is checked again immediately before booking. Squash courts are excluded.
Tick a session to add it; untick to remove. Press **Save selection** to persist.

**Automatic bookings** — shows:
- *Selected sessions* grouped by member. Each entry has a ⏸ pause button (skips that session for the next booking run) and a **Book now (test)** button.
- *⏸ Pause all automatic bookings* toggle — suspends all bookings in one tap.
- *Current bookings on the club site* grouped by member, each with a Booked / Waitlisted tag, refreshed by the scrape workflow.
- *Booking history* (bottom of the page) — last 10 booking attempts with result (`booked`, `waitlisted`, `already`, `failed`, `not-published`).

**⚙ Settings** — configure the release time (default `08:01`), change the passphrase or GitHub token, lock this device.

## Members and booking kinds

Each member on the account gets their own button (by first name). A selection is stored as `kind|member|session`, so each person's weekly choices and booking history are fully separate. Older selections (created before this feature) are treated as Tennis Socials for the first member.

## Why two branches and an environment

A token stored in the dashboard can write to the repository. To make sure a leaked token can only change
the *schedule* and never the booking code (which runs with your club login):

- Data lives on the `data` branch. Code lives on `main`.
- `main` is protected by a ruleset (no direct pushes), so the token cannot alter `scripts/book.py`.
- Club credentials are stored in the GitHub **Environment `club`**, which only `main` may deploy to.
  Workflows are always dispatched on `main`.

## One-time setup

1. **Push** this folder to `https://github.com/filiplou/ngbooker` (branch `main`). `ghplan.md` and `pats.md` are
   git-ignored because they hold private notes.
2. **Create the `data` branch** with the starting files from `seed/`:
   ```powershell
   git checkout --orphan data
   git rm -rf . -q
   git checkout main -- seed
   Move-Item seed\*.json . ; git rm -r --cached seed -q ; Remove-Item seed -Recurse -Force
   git add *.json
   git commit -m "Initial data"
   git push origin data
   git checkout main
   ```
   (Equivalent by hand: put the JSON files from `seed/` in the root of a new branch called `data`.)
3. **Environment `club`** (Settings -> Environments -> New environment):
   - Deployment branches: *Selected branches* -> `main` only.
   - Add environment secrets `NGMEMBERNUMBER` and `NGPASSWORD`. Do **not** add them as repository secrets.
4. **Allow workflows to push**: Settings -> Actions -> General -> Workflow permissions -> *Read and write*.
5. **GitHub Pages**: Settings -> Pages -> Deploy from branch `main`, folder `/ (root)`.
   Dashboard URL: `https://filiplou.github.io/ngbooker/`
6. **Protect `main`**: Settings -> Rules -> Rulesets -> New branch ruleset, target `main`,
   enable *Restrict updates* / *Require a pull request*. This stops a stolen token from editing code.
7. **Create two fine-grained tokens** (github.com -> Settings -> Developer settings), each for repository
   `filiplou/ngbooker` only:
   - *Dashboard token*: **Contents: Read and write** + **Actions: Read and write**.
   - *cron-job.org token*: **Actions: Read and write** only.
   Revoke any token that was ever pasted into chat or a file.
8. **Verify the field names locally (recommended before the first real run):**
   ```powershell
   $env:NG_MEMBER_NUMBER = "<your number>"; $env:NG_PASSWORD = "<your password>"
   pip install -r requirements.txt
   python scripts/discover.py
   ```
   Do not share its output. It shows whether the time format, `isWaitingList` and My Bookings fields
   match what the scripts expect.
9. Run **Actions -> Refresh session list** once. All 5 members and all session kinds should appear.

## First-time dashboard setup (vault) — do this once, on a computer

Open the dashboard. Because no vault exists yet it shows the setup screen:

1. Paste the **dashboard token**.
2. **Choose a short passphrase.** Use **4 random words joined with dashes**
   (for example `copper-lemon-river-ten` — do not use that one) or anything of **12+ characters**. Type it twice.
3. Press the setup button. The token is encrypted in your browser (PBKDF2-SHA256, 600,000 rounds, then AES-GCM)
   and only the encrypted blob is saved as `vault.json` on the `data` branch.

On every other device just open the page and type the passphrase.
Tick *Remember on this device* to avoid retyping; use **⚙ Settings -> Lock now** to forget a device.

### Changing the passphrase or token
Settings tab -> "Change passphrase or replace token". Other devices then need the new passphrase.

### Forgot the passphrase
Create a new dashboard token, delete `vault.json` from the `data` branch on GitHub, reload the dashboard and
redo the setup. Revoke the old token.

## Booking trigger

**Primary: cron-job.org.** GitHub's built-in `schedule:` is best-effort and has been observed running
1-3 hours late, which misses the booking window. A cron-job.org job calls `workflow_dispatch` instead:

- URL: `https://api.github.com/repos/filiplou/ngbooker/actions/workflows/book.yml/dispatches`
- Method: POST, timezone `Australia/Sydney`, schedule **07:55 every day** (a few minutes before the release time,
  so the runner can start and log in; the script then waits for the release time)
- Headers: `Authorization: Bearer <the cron-job.org token>` (the `Bearer ` prefix is required, otherwise 401),
  `Accept: application/vnd.github+json`, `User-Agent: ngbooker`
- Body: `{"ref":"main","inputs":{"mode":"scheduled"}}`
- A successful call returns `204 No Content`.

If you change the release time in ⚙ Settings, move the cron-job.org schedule to match; it does not follow the
dashboard setting. The dispatch must land within 90 minutes of the release time and no more than about an hour
early (the job times out after 60 minutes).

**Backup: GitHub cron.** `book.yml` also keeps two cron entries (UTC 20:30 and 21:30, covering AEDT UTC+11 and
AEST UTC+10). The script exits silently if the current Sydney time is not within 90 minutes of the release
time, and it never rebooks a session already done, so these are harmless.

## Pausing bookings

- **Pause one session**: tap ⏸ next to it on the Automatic bookings tab. The card dims. Tap ▶ to resume.
- **Pause everything**: tap *⏸ Pause all automatic bookings*. The script exits immediately without booking anything.
- Both flags are saved in `selections.json` on the `data` branch and take effect on the next scheduled run.

## Behaviour

- Open slot → booked. Full → the site's own booking call puts you on the waiting list.
- Only the day exactly 7 days ahead is ever attempted, so a Tuesday selection books next Tuesday and never today or other visible days (this avoids "concurrent reservation" errors).
- Result of every attempt is shown in the *Booking history* section (`booked`, `waitlisted`, `already`, `failed`, `not-published`). The last 300 entries are stored in `bookings-log.json`; the dashboard shows the newest 10.
- The booked/waitlisted result is taken from the site's My Bookings (`isOnWaitList`) after booking. The site may append the court to the name (for example `Social Doubles C1-2` becomes `Social Doubles C1-2 C1`), so a booking matches if its name equals the selection name or starts with it followed by a space.
- **Book now (test)** triggers `book.yml` in `now` mode for that one selection. Like the scheduled run, it only targets the day 7 days ahead, so it does nothing if that date's weekday doesn't match the selection.
- Silver members only see 7 days ahead; gold-only days are skipped automatically.

## Security notes

- The repository is public, so the `data` branch is world-readable: class names, dates and your booking
  history are visible. Personal fields (member numbers, emails, tokens) are stripped before saving.
- `vault.json` is public too. It is encrypted, but anyone can try to guess the passphrase offline, so a
  strong passphrase (4 random words or 12+ characters) matters.
- The dashboard token can only change the schedule (data branch) and trigger workflows. It cannot alter code
  on `main` (ruleset) or read the club secrets (environment limited to `main`).
- If a phone is lost: revoke the dashboard token on GitHub and redo the vault setup.
- Never put the token, member number or password in any file or in chat.
- Check the club's terms allow automated booking.

## Tests

```
python -m unittest discover -s tests
```
