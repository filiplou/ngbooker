# NG Booker

Automatically books (or joins the waiting list for) your chosen weekly Tennis Programs at
The Club Lounge, the moment new days are released at **00:05 Sydney time**.

```
main branch  : code (scripts/, workflows, index.html = dashboard on GitHub Pages)
data branch  : selections.json, sessions.json, members.json, my-bookings.json, bookings-log.json, vault.json

Dashboard --GitHub API--> data branch (selections.json)
cron-job.org (23:55 Sydney) --dispatch--> book.yml (runs from main, environment "club")
   logs in via the club site's own API, waits for 00:05, books the day 7 days ahead,
   writes bookings-log.json to the data branch
scrape.yml (daily / on demand) --> sessions.json + members.json + my-bookings.json on the data branch
```

The scripts call the same web API as the club website (`/token`, `/api/ClubLounge/...`), so no browser
is needed. Selections are stored by *class name + weekday + time*, so a choice repeats every week.

## Racquets, Group exercise and members

The dashboard has a **Racquets / Group exercise** switch and a **member selector** (one button per person on the account, by first name). A selection is stored per member and kind as `kind|member|session`, so each person's weekly choices and bookings are separate. Older selections (no member/kind) are treated as Racquets for the first member.

**Group exercise is new and has not been verified against the live site.** After merging, run *Actions > Refresh session list*, check that the Group exercise tab fills in and each member appears, then try **Book now (test)** on one group class for a non-primary member before relying on the 00:05 run.

## Why two branches and an environment

A token stored in the dashboard can write to the repository. To make sure a leaked token can only change
the *schedule* and never the booking code (which runs with your club login):

- Data lives on the `data` branch. Code lives on `main`.
- `main` is protected by a ruleset (no direct pushes), so the token cannot alter `scripts/book.py`.
- Club credentials are stored in the GitHub **Environment `club`**, which only `main` may deploy to.
  Workflows are always dispatched on `main`.

## One-time setup

1. **Push** this folder to `https://github.com/filiplou/ngbooker` (branch `main`). `ghplan.md` is
   git-ignored because it holds private notes.
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
   (Equivalent by hand: put the four JSON files from `seed/` in the root of a new branch called `data`.)
3. **Environment `club`** (Settings -> Environments -> New environment):
   - Deployment branches: *Selected branches* -> `main` only.
   - Add environment secrets `NGMEMBERNUMBER` and `NGPASSWORD`. Do **not** add them as repository secrets.
4. **Allow workflows to push**: Settings -> Actions -> General -> Workflow permissions -> *Read and write*.
5. **GitHub Pages**: Settings -> Pages -> Deploy from branch `main`, folder `/ (root)`.
   Dashboard URL: `https://filiplou.github.io/ngbooker/`
6. **Protect `main`**: Settings -> Rules -> Rulesets -> New branch ruleset, target `main`,
   enable *Restrict updates* / *Require a pull request*. This is what stops a stolen token from editing code.
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
9. Run **Actions -> Refresh session list** once.

## First-time dashboard setup (vault) - do this once, on a computer

Open the dashboard. Because no vault exists yet it shows the setup screen:

1. Paste the **dashboard token**.
2. **Choose a short passphrase.** The screen highlights this step. Use **4 random words joined with dashes**
   (for example `copper-lemon-river-ten` - do not use that one) or anything of **12+ characters**. Type it twice.
3. Press the setup button. The token is encrypted in your browser (PBKDF2-SHA256, 600,000 rounds, then AES-GCM)
   and only the encrypted blob is saved as `vault.json` on the `data` branch.

On every other device (your wife's phone, your phone) just open the page and type the passphrase.
Tick *Remember on this device* to avoid retyping; use **Settings -> Lock now** to forget a device.

### Changing the passphrase or token
Settings tab -> "Change passphrase or replace token". Other devices then need the new passphrase.

### Forgot the passphrase
Create a new dashboard token, delete `vault.json` from the `data` branch on GitHub, reload the dashboard and
redo the setup. Revoke the old token.

## Reliable 00:05 trigger (recommended)

GitHub's own cron is often 15-30 minutes late. `book.yml` has two cron entries as a fallback (they start
at 23:30/00:30 Sydney, covering daylight saving), but for precise timing use cron-job.org:

- URL: `https://api.github.com/repos/filiplou/ngbooker/actions/workflows/book.yml/dispatches`
- Method: POST, timezone `Australia/Sydney`, schedule **23:55 every day**
- Headers: `Authorization: Bearer <the cron-job.org token>`, `Accept: application/vnd.github+json`
- Body: `{"ref":"main","inputs":{"mode":"scheduled"}}`

The script waits until 00:05:00 Sydney time and then polls every few seconds (up to 5 minutes) until the
new day appears. Already-booked sessions are never booked twice.

## Behaviour

- Open slot -> booked. Full -> the site's own booking call puts you on the waiting list.
- Result of every attempt is shown on the **Automatic bookings** tab (`booked`, `waitlisted`, `already`,
  `failed`, `not-published`).
- **Book now (test)** triggers `book.yml` in `now` mode for that one selection across the days currently visible to you.
- Silver members only see 7 days ahead; gold-only days are skipped automatically.

## Security notes

- The repository is public, so the `data` branch is world-readable: class names, dates and your booking
  history are visible. Personal fields are stripped before saving.
- `vault.json` is public too. It is encrypted, but anyone can try to guess the passphrase offline, so a
  strong passphrase (4 random words or 12+ characters) matters.
- The dashboard token can only change the schedule (data branch) and trigger workflows. It cannot alter code
  on `main` (ruleset) or read the club secrets (environment limited to `main`).
- If a phone is lost: revoke the dashboard token on GitHub and redo the vault setup. Tokens expire after at most a year.
- Never put the token, member number or password in any file or in chat.
- Check the club's terms allow automated booking.

## Tests

```
python -m unittest discover -s tests
```
