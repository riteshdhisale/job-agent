# job_agent

An AI agent that discovers, filters, scores, and tracks job listings — built to run
your own job search end to end: discovery across every source it can reach compliantly,
resume tailoring gated at ≥90% match, warm-contact outreach drafts, one-click-apply
(fill-and-stop, never auto-submit), and interview prep, all sharing one candidate
profile and one job database.

I built this for my own search after being laid off from a PM role. There's a full
write-up of the product decisions and a couple of debugging war stories here:
**[job_agent case study](https://claude.ai/code/artifact/e921c18a-55ff-47b5-9396-7205005e0707)**.

This repo is a sanitized copy of my working setup — my real resume, application
history, and API keys are excluded (see "What's not in this repo" below). Clone it,
drop in your own data, and it works the same way for you. It can also run as a public,
email-gated demo against a sample profile — see "Deploying the public demo" below.

## Setup (one-time)

```bash
git clone <this-repo-url>
cd job_agent
python3 -m venv venv && source venv/bin/activate   # optional but recommended
pip install -r requirements.txt
python -m playwright install chromium               # needed for the apply command
```

`data/` ships empty. Before running anything beyond `--engine fake`, add your own:

- **`data/resume.txt`** — plain text of your resume.
- **`data/profile.json`** — your contact info and work-authorization answer. Easiest
  way to create it: run `python cli.py profile set full_name "Jane Doe"` (and so on for
  `email`, `phone`, `location`, `linkedin_url`, `work_authorized`) — it creates the file
  on first write.

Everything below works immediately with the default `--engine fake` (crude keyword
matching, free, no key) so you can see the whole system move data end to end before
spending anything. For real quality, copy `.env.example` to `.env` and fill in ONE of:

- `--engine claude` — needs `ANTHROPIC_API_KEY`. Pay-as-you-go billing: you need a
  funded credit balance in the Anthropic Console (Plans & Billing) before any request
  works, even a cheap one. When creating the key, set its Scope to a specific
  workspace (e.g. "Default"), not "Same as linked account" — otherwise requests fail
  asking for a workspace id.
- `--engine gemini` — needs `GEMINI_API_KEY`, from https://aistudio.google.com/apikey.
  This one has a genuine free tier for personal-scale use, no card required to start.
  Important: a personal Gemini Pro/Advanced subscription (the one bundled with Google
  One) does **not** include this — it's a separate, purpose-built API key, but it's
  free where Claude's API is pay-as-you-go, so if budget is the deciding factor, start
  here.

Both give real match scoring/tailoring/drafting; which is "better" varies by prompt
and is worth trying both on the same job to compare, especially since Gemini's free.

## Option A: command line

```bash
python cli.py discover --engine fake
python cli.py list
python cli.py show 1
```

Every command is documented below under "Commands". This is the lower-level interface —
scriptable, good for automation, and what the web app itself calls under the hood.

## Option B: local web app

```bash
python app.py
```

Then open **http://127.0.0.1:5000** in your browser. This is a basic Flask app — plain
HTML forms, no build step, nothing to configure — that calls the exact same
`profile_store.py` / `job_store.py` / `discovery.py` / `tools/*` functions the CLI does.
Nothing is duplicated between the two; `app.py` is just a different front end on the same
engine, so anything you do in one is immediately visible in the other (same `data/jobs.db`,
same `data/profile.json`).

What's on it — a single dashboard:

- **Dashboard (`/`)** — everything lives on this one page: a quick-glance summary
  (total found / ready to submit / applied-or-further / not yet applied), a status filter
  bar, a job list on the left, and a detail panel on the right that updates in place —
  clicking a job never navigates anywhere. Every action — tailor, find contacts, draft
  outreach, apply (with an inline follow-up for any screening question it couldn't
  answer from your profile — answer it once, it's saved), mark applied, generate
  interview prep, change status, delete — happens inline in that detail panel via a
  quick fetch call, with the button showing a busy state while it runs. A prominent
  link near the top of the detail panel opens the actual posting.
- **Discover** — a "+ Run discovery" button opens an inline panel (career sites + Adzuna +
  RemoteOK + Greenhouse + Lever, with an opt-in checkbox for Instahyre) right on the
  dashboard — no separate page. Submitting it runs in the background and the *same panel*
  turns into a live progress bar (polling every second) that refreshes the job list the
  moment it finishes; a real error (bad connection, quota, etc.) shows as a clear banner
  with the actual error text and a "Try again" button, rather than the page just going
  quiet. Execution time isn't capped by design — a run checking several sources with a
  real LLM engine can take anywhere from seconds to a couple of minutes.
- **Add Lead (`/add-lead`)** and **Add Browsed Page (`/add-browsed-page`)** — the paste-based
  intake for LinkedIn posts, portal email alerts, agency messages, and live-browsed pages.
  These stay as their own simple pages (occasional actions, not part of the main browse-
  and-apply loop) and land you back on the dashboard with the new job pre-selected.
- **Profile (`/profile`)** — view and edit your contact info and see every screening-question
  answer that's been cached so far. Also its own simple page, same reasoning.
- An engine selector (fake/claude/gemini) in the top nav, applied to whichever action you run next.

Everything the dashboard needs comes from a small JSON API (`/api/jobs`, `/api/jobs/<id>`,
`/api/jobs/<id>/tailor`, `/apply`, `/prep`, etc.) that `templates/index.html`'s own
JavaScript calls with `fetch()` — no frontend framework, no build step, same
`profile_store.py` / `job_store.py` / `discovery.py` / `tools/*` functions the CLI calls
underneath.

This is a **local, single-user tool**: no login, binds to `127.0.0.1` only, uses a fixed
session secret. Don't deploy it to the open internet as-is — that would need real auth
first, which is out of scope for "basic local web app."

## How job tracking actually works (both interfaces)

Every discovered or pasted job becomes one row in `data/jobs.db` (SQLite) with a single
`status` field that only ever moves forward: `found → tailored → contacted →
ready_to_submit → applied → interviewing → closed`. That status *is* the tracking system —
there's no separate "applied?" checkbox to keep in sync, because `applied` (and everything
after it) already means exactly that. The dashboard's status filter bar and the summary
counts are just different views over that one field, so the CLI's `list --status applied`
and the web app's `/?status=applied` are always showing you the same underlying truth,
never two copies of it that could drift apart.

## Sources covered, and why each one works the way it does

The compliance stance is deliberate — worth understanding since it explains why sources
that look similar ("job portal") are handled completely differently:

| Source | How it's reached | Category |
|---|---|---|
| Company career sites | Direct fetch + LLM extraction (`discover`) | Compliant — normal web access, no login wall |
| Recruiting/staffing agency sites | Same as above — add their URLs to `data/career_sites.json` | Compliant |
| Adzuna (aggregates many portals, 12 countries incl. India) | Public API (`discover`; free `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` from https://developer.adzuna.com); paginates up to 250 results/run, not just one page | Compliant — official API, ~1,000 free calls/month |
| RemoteOK (remote tech/product roles) | Public no-auth JSON feed (`discover`) | Compliant — openly published feed |
| Greenhouse (per-company job boards) | Official public API, no key needed (`discover`; add board tokens to `data/greenhouse_boards.json`) | Compliant — Greenhouse's own docs: "no authentication required for any GET endpoints" |
| Lever (per-company job boards) | Official public API, no key needed (`discover`; add site tokens to `data/lever_sites.json`) | Compliant — same category as Greenhouse |
| Instahyre | Scraping (`discover --include-instahyre`), rate-limited to once per ~3h | **Accepted risk** — their `robots.txt` has no disallow rules, so this doesn't run against their own stated wishes, but there's no official API either. Selectors are best-effort/unverified (see `tools/instahyre_scraper.py`) |
| Naukri | **Not automated** — see `tools/naukri_note.md` | Their `robots.txt` explicitly disallows automated access — a concrete "please don't," not just "weaker enforcement." Use their own email job-alerts + `add-lead` instead |
| LinkedIn Jobs (search results) | Interactive, live, in a Claude conversation — you browse together using your real logged-in Chrome, then `add-browsed-page` ingests what was read | **Accepted risk, minimized by design** — human-paced, only runs while you're present, never an unattended script |
| LinkedIn feed posts, portal email alerts, agency emails | Paste the text in (`add-lead`) | Compliant — you're pasting content already sent to you |

**Why LinkedIn gets a live-browsing approach instead of a scraper:** unlike Instahyre,
LinkedIn requires being logged in to see job search results, which means automating it
means automating actions tied to your own account — exactly the pattern their bot
detection is built to catch, with real precedent (a breach-of-contract judgment against
hiQ Labs, separate from and beyond the earlier ruling that scraping public data isn't a
CFAA crime). The cost of getting flagged isn't legal, it's your own account being
restricted mid-job-search. Browsing live, at your own pace, only when you're actually
looking, is both the lower-risk option and the one that's genuinely equivalent to
reading it yourself, rather than an automated approximation of that.

**How the live LinkedIn flow works:** browse LinkedIn Jobs yourself in a Claude
conversation — this needs the Claude in Chrome extension connected to use your real,
already-logged-in browser. Claude reads the search results page the way you would,
then runs `python cli.py add-browsed-page --file <captured text> --source-url "..."`
to score and store whatever was found, same as any other source.

## Maximizing coverage (finding every good match, not just a few)

`data/career_sites.json`, `data/greenhouse_boards.json`, and `data/lever_sites.json`
all ship **empty** — populate them with the companies you actually want covered.
`data/career_sites.json` also used to ship with two local HTML test fixtures mixed in
by mistake, which got scored as real jobs on a live run before being caught — a
reminder to sanity-check any job from a company you don't recognize.

Two ways to widen the net, and they scale very differently:

- **One URL per company** (`data/career_sites.json`) — add career page URLs for
  companies you specifically want covered. This works, but doesn't scale past a
  handful of companies you hand-pick and maintain yourself, and some career pages (JS-
  heavy single-page apps, e.g. Salesforce's) won't extract cleanly at all — see "What's
  genuinely not built yet" below.
- **Aggregator APIs** (Adzuna, RemoteOK, Greenhouse, Lever) — one config entry each,
  covers many companies/postings at once, and is the actual scalable path to finding
  everything above your match threshold, however long it takes. Adzuna alone
  aggregates across many job portals in 12 countries including India, and pages
  through up to 5 pages (250 listings) per run instead of stopping at the first
  ~20-50 — it stops early the moment a page comes back short, so a narrow query
  doesn't waste calls chasing pages that don't exist. Greenhouse/Lever need company
  board/site tokens added to their respective `data/*.json` files (there's no way to
  search across all companies on either platform at once — but each token you add
  covers that whole company's postings, not just one URL's worth). A bad or outdated
  token just fails gracefully for that one company (see `tests/test_new_sources.py`) —
  it costs one HTTP call and doesn't touch the rest of the run.

  **To turn Adzuna on:** get a free `app_id`/`app_key` pair at
  https://developer.adzuna.com (no cost, ~1,000 calls/month), then set
  `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` in your `.env`. Until those are set, `discover`
  silently skips Adzuna and you'll see `ADZUNA_APP_ID/ADZUNA_APP_KEY not set --
  skipping` in the failed-sources list — that's expected, not a bug, and this is the
  single highest-leverage thing to set up for broader coverage.

If exhaustive coverage matters more than speed (discovery has no time limit, and the
web app's progress bar exists specifically so a long run stays legible instead of
looking hung), setting up Adzuna plus a handful of Greenhouse/Lever tokens for
companies you're targeting will surface far more real postings than maintaining a
long list of individual career-site URLs ever will.

## Narrowing a search (multiple roles, title/location relevance, freshness, salary floor)

The flip side of "maximize coverage" above is that a wide net also means scoring a lot
of listings that were never going to be a real fit — every one of which costs an LLM
call. Five ways to narrow a run, all applied **before** anything is scored, so a
listing that gets filtered out never costs a token:

- **Multiple job titles in one run.** `--query` (CLI) / the query field (web app) takes
  a comma-separated list, not just one title. This is Adzuna's/RemoteOK's own search
  parameter (queried once per title for Adzuna; RemoteOK's feed is fetched once and
  matched against all of them together, since it's a free public feed with no reason
  to fetch it twice) — it does **not** reach career sites or Greenhouse/Lever, which
  have no query parameter of their own to pass it to. That gap used to mean
  Greenhouse/Lever scored a company's *entire* board — every department, every level —
  against the resume, which was the real, confirmed cause of "these roles are not
  matching" once off-topic results started showing up. `--title-keywords`/
  `--exclude-title-keywords` (next bullet) is the fix for that, and it's
  source-agnostic, so it also tightens Adzuna/RemoteOK beyond what `--query` alone does.
- **`--title-keywords "Product Manager,TPM,..."` / `--exclude-title-keywords
  "Intern,VP,..."`** (CLI) / the matching two fields in the web app's discover panel —
  a listing's **title** must contain at least one keyword (case-insensitive substring)
  and none of the excluded ones, checked against every source including
  Greenhouse/Lever. Both are on by default (empty means "score everything") — set them
  to match your own target roles.
- **`--locations "Bengaluru,Pune,Remote - India,..."`** (CLI) / "only these locations"
  (web app) — keeps only listings whose disclosed location matches at least one entry
  (substring, case-insensitive); a listing with no disclosed location always passes
  (same "don't punish missing data" rule as salary/freshness below), since plenty of
  postings simply don't list one.
- **`--max-age-hours`** (CLI) / "posted within the last N hours" (web app) — keeps only
  listings posted inside that window. Only Adzuna reports a real posting timestamp
  today; every other source's listings simply don't have one, and a missing date always
  passes rather than fails (see `tools/listing_filters.py`) — so this narrows Adzuna
  specifically without silently emptying out your other sources.
- **`--min-salary`** (CLI) / "minimum salary" (web app) — keeps only listings whose
  disclosed salary meets this floor (in the source's local currency, e.g. `3000000` for
  ₹30L). This is a real Adzuna API parameter (`salary_min`), so for Adzuna it's filtered
  server-side, before you even pay a page-fetch for it. A listing with a disclosed range
  that straddles your floor (say ₹25-35L against a ₹30L floor) passes on its upper end.
  A listing that doesn't disclose salary at all — common on many Indian postings — is
  **kept, not excluded**; you can judge those yourself once you see them.

`--max-age-hours` and `--min-salary` default to unset (no filtering). Pass an empty
string to `--title-keywords`/`--exclude-title-keywords`/`--locations` if you'd rather
see everything a source returns, unfiltered.

## The five agents

```
                    ┌─────────────────────────────────────────┐
                    │   Candidate Profile (data/profile.json)   │
                    │  contact info, work-auth, cached answers  │
                    └───────────────┬───────────────────────────┘
                                    │ read/written by every command below
     ┌──────────────────────────────┼──────────────────────────────┐
     │                              │                              │
┌────▼─────────┐   90%+ gate  ┌─────▼──────┐   you decide    ┌─────▼─────┐
│ discover /   │─────match───▶│  tailor    │───to apply─────▶│  apply    │
│ add-lead     │  + rationale │            │                 │(fill+stop)│
└──────────────┘              └─────┬──────┘                 └─────┬─────┘
                                     │ same job + rationale         │ you submit,
                              ┌──────▼──────┐                       │ then `mark-applied`
                              │ contacts /  │                 ┌─────▼──────┐
                              │  outreach   │                 │ shortlisted?│
                              │(draft only) │                 │   `prep`    │
                              └─────────────┘                 └─────────────┘
```

## Commands

```bash
python cli.py discover                             # career sites + Adzuna + RemoteOK + Greenhouse + Lever
python cli.py discover --include-instahyre          # + Instahyre (scraping, rate-limited ~once/3h)
python cli.py discover --query "product manager, product owner"  # override the default title search
python cli.py discover --max-age-hours 48 --min-salary 3000000   # freshness + salary floor -- see "Narrowing a search"
python cli.py discover --title-keywords "" --exclude-title-keywords "" --locations ""    # see everything, unfiltered
python cli.py add-lead --file post.txt --source linkedin_post   # LinkedIn post / portal email alert / agency message
python cli.py add-browsed-page --file page.txt --source-url "linkedin.com/jobs/search"  # after browsing live together
python cli.py list [--status found|tailored|contacted|ready_to_submit|applied|interviewing|closed]
python cli.py show 3                               # full detail on job #3
python cli.py tailor 3                             # only works if score >= 90 (use --force to override)
python cli.py contacts 3 --company "Acme Inc"      # needs APOLLO_API_KEY (free tier: 75/mo)
python cli.py outreach 3                           # drafts from the contact found above
python cli.py apply 3                              # fills the form / drafts the email -- never sends/submits
python cli.py mark-applied 3                       # you confirm YOU actually did
python cli.py prep 3                               # once shortlisted
python cli.py profile show
python cli.py profile set work_authorized "Yes, no sponsorship needed"
python cli.py delete 3                             # remove one job (bad match, duplicate, test data)
python cli.py delete --all                         # wipe every job and start fresh
```

Every command that calls an LLM takes `--engine fake` (default), `--engine claude`, or
`--engine gemini` — see "Setup (one-time)" above for what each needs.

## What "apply" actually does — read this before trusting it

`apply` branches on how the job says to apply (`application_method`, set automatically
by `discover` or `add-lead`):

- **web_form / link** — opens the page with Playwright, fills whatever fields it can
  confidently match to your profile (name, email, phone, LinkedIn, location), and takes
  a screenshot. Anything it can't confidently match (a custom screening question, a
  cover-letter box) it asks you for **once**, right there in the terminal, then saves
  your answer to `data/profile.json` so you're never asked again on a future
  application. **It never clicks submit.** Review the screenshot, then submit yourself.
- **email** — drafts a subject + body for you to send with your resume attached. Never sends it.
- **dm / unclear** — just shows you the target/instructions. No automation attempted —
  there's no safe way to automate an unsolicited LinkedIn DM consistent with staying compliant.

**Real limitation, not a bug:** the form-filler matches fields by `<label>`/`<input>`
pairs. Several major ATS platforms (Workday especially) render fully custom widgets
instead of real HTML form elements, and this will find nothing to fill there — you'll
still see the screenshot and can fill it by hand. Worth knowing before you rely on it.

## Setup for the optional pieces

- **Real quality everywhere:** `ANTHROPIC_API_KEY` (pay-as-you-go) or `GEMINI_API_KEY`
  (has a free tier) in `.env` (copy from `.env.example`) — see "Setup (one-time)" above
  for the details and the workspace-scope / subscription-vs-API-key gotchas.
- **Contact-finding (`contacts`):** `APOLLO_API_KEY` — free tier at https://www.apollo.io
  gives 75 lookups/month, plenty to start.
- **Adzuna in `discover`:** `ADZUNA_APP_ID` + `ADZUNA_APP_KEY`, free at
  https://developer.adzuna.com. This is the biggest lever for wide coverage — see
  "Maximizing coverage" above for how it paginates once these are set.
- **RemoteOK:** nothing needed, it's an open feed.
- **Greenhouse in `discover`:** nothing needed, but you must list the companies you care
  about in `data/greenhouse_boards.json` (find a company's token in their
  `boards.greenhouse.io/<token>` URL) — Greenhouse has no cross-company search.
- **Lever in `discover`:** same idea, `data/lever_sites.json` (token from
  `jobs.lever.co/<token>`).
- **Instahyre (`--include-instahyre`):** nothing needed, but read `tools/instahyre_scraper.py`'s
  docstring first — the selectors are best-effort and likely need adjusting against the
  live site on your first real run.

None of these are required to start using the system today with `--engine fake` and
career-site + manual-lead sources.

## Gemini's free tier has a real daily request cap — plan around it

"Free" doesn't mean unlimited. Confirmed live: a fresh API key on `gemini-3.6-flash`
hit `429 RESOURCE_EXHAUSTED` after roughly a dozen requests in one day, with Google's
own error citing `GenerateRequestsPerDayPerProjectPerModel-FreeTier` at a quota value of
**20 requests/day** for that model on that project — newer/preview-tier models
consistently get tighter free quotas than established ones (Google's own docs note rate
limits are "more restricted for experimental and preview models"), and this held true
here even though older Flash models have historically allowed roughly 1,500/day.
Third-party writeups of exact numbers vary and go stale fast — **your own live limits
are at https://aistudio.google.com/rate-limit**, not any blog post (including this
README).

What this means practically, and what's been done about it:

- **Every `discover` call is now several requests, not one.** Career-site extraction is
  one request per site; each of Adzuna/RemoteOK/Greenhouse/Lever is scored in one
  request per `SCORE_BATCH_SIZE` (100) listings — so a full Adzuna page of 250 results is
  3 requests, not 10, after `SCORE_BATCH_SIZE` was raised from an earlier, more
  conservative default specifically to conserve daily request count once this quota
  was discovered live (see `tools/score_tool.py`'s docstring for the full trade-off).
  `tailor`/`outreach`/`prep`/`contacts` are additional requests on top of whatever
  `discover` used that day.
- **If you hit `429 RESOURCE_EXHAUSTED` mid-run:** the quota is per day, not per minute,
  so retrying immediately won't help if it's the daily cap (as opposed to a per-minute
  burst limit, which does clear in under a minute). Check
  https://aistudio.google.com/rate-limit for your actual reset time, or just try again
  the next day.
- **If this becomes a recurring blocker, live-tested findings as of Sept 2026:**
  `gemini-2.0-flash` is fully retired (shut down, not just deprecated) — don't try it.
  `gemini-2.5-flash` and `gemini-2.5-flash-lite` both `404 NOT_FOUND` for a new API key
  ("no longer available to new users"); Google's own error for the latter pointed at
  `gemini-3.5-flash-lite` as the replacement, which is now this codebase's default (see
  `GEMINI_MODEL` in `.env.example`). Flash-Lite models are also just a better fit here
  regardless of quota — they're built for "high-volume classification, simple data
  extraction," which is exactly what `job_agent` asks Gemini to do. If
  `gemini-3.5-flash-lite` itself ever 404s or proves just as tight, check
  https://ai.google.dev/gemini-api/docs/models for whatever's current rather than
  trusting this paragraph indefinitely — model names and quotas both move fast.
- **A batch that comes back with fewer scores than listings sent now prints a warning**
  to the console (`tools/score_tool.py`) instead of silently dropping the unscored
  listings — this is the one case where a Gemini response could be genuinely truncated
  rather than rate-limited, and it's worth knowing if it ever happens.

## Testing

```bash
pytest -q
```

120 tests, all against local fixtures, mocked HTTP/SDK responses, and `FakeLLMClient` —
no API key, no network, no real Playwright browser hitting a live site. Covers:
profile/job store persistence (including `delete`/`delete_all`, posted_at/salary
round-tripping, and migrating a pre-existing `jobs.db` that predates those columns),
the 90-score tailoring gate, the manual-lead parser (job vs. not-a-job, email/link/DM
detection), the Playwright form-filler (correct fields filled, unmapped fields surfaced
not guessed, cached answers reused), the discovery pipeline (including the
`on_progress` callback the web app's progress bar depends on, multi-role query
normalization, cross-query de-duplication, and — mocking `fetch_greenhouse_listings`/
`fetch_lever_listings` — that a bad board token fails gracefully and that
`title_keywords`/`allowed_locations` actually drop unrelated/out-of-region roles pulled
from a company's board), the pre-scoring freshness/salary/title-keyword/location filter
(missing data always passes for freshness/salary/location, boundary conditions, a range
straddling the salary floor, include- vs. exclude-keyword title matching, case
insensitivity, all four filters combined), the Adzuna pagination logic (stops on a short
page, caps at `max_pages`, keeps partial results if a later page errors, never requests
more than Adzuna's own per-page maximum, captures posted-date/salary fields, passes
`salary_min`/`max_days_old` through as real query params), the RemoteOK multi-tag
matching (searches several role keywords in one feed fetch, no duplicate listings), the
score-batching logic (splits large sources into `SCORE_BATCH_SIZE`-sized requests,
handles an exact-multiple and a short trailing batch correctly, and warns rather than
silently dropping listings if a batch comes back truncated), the Greenhouse/Lever
field-mapping logic, the rate limiter (blocks a second run inside the cooldown window,
respects `force`, tracks sources independently), the Gemini client wiring (forces a
function call rather than free text, passes tool schemas through correctly, parses the
response, retries past a simulated connection drop — both a bare `OSError`/
`ConnectionError` and, separately, an `httpx.ReadError`/`httpx.TransportError`, since
those turned out live not to be the same thing (a connection reset while *sending* a
request vs. while *reading* the response can surface as either, and only the broader
`httpx.TransportError` catch covers both) — and gives up cleanly if the connection never
recovers), and the web app's JSON API (`tests/test_app.py` — job listing/filtering/
detail, status changes and deletes, the tailor score-gate, the apply → needs-review →
resolve flow, and the discover background-thread/error handling, via Flask's test
client with `JobStore` pointed at a temp database).

## What's genuinely not built yet

- No headless-browser fallback for JS-rendered career pages.
- Apollo and Adzuna integrations are written from published API docs but untested
  against live keys while building this — expect to debug field names against the real
  response the first time you run them with your own keys. Adzuna's pagination logic
  itself is unit-tested against mocked responses (short page, full pages, errors
  mid-run), but hasn't paginated against Adzuna's real API in this repo's own testing.
- `GeminiClient` is unit-tested against a mocked SDK response (schema/wiring correctness)
  but hasn't produced a real end-to-end extraction/score/tailor against the live Gemini
  API in this build — worth a first-run sanity check on your side, same spirit as
  Apollo/Adzuna above.
- `tools/instahyre_scraper.py`'s CSS selectors are unverified against the live site —
  treat the first run as a debugging session.
- Naukri has no automated connector at all, on purpose — see `tools/naukri_note.md` for
  why, and how to get Naukri coverage via `add-lead` instead.
- The LinkedIn live-browsing flow needs the Claude in Chrome extension connected; it's
  designed but you'll want to walk through it once end to end before trusting it fully.
- The web app (`app.py`) has automated coverage (`tests/test_app.py`, 20 tests, using
  Flask's test client with `JobStore` monkeypatched to a temp database) in addition to
  a manual/visual check of the dashboard. `fill_application_form` (Playwright) and
  `run_discovery` are mocked in those tests rather than exercised for real — their own
  behavior is covered by `test_apply_tool.py` and `test_discovery.py` respectively;
  `test_app.py` only checks that `app.py`'s routes wire into them correctly and return
  what the dashboard's JS expects.
- Freelancing platforms (Upwork etc.) are out of scope for now — this is built around
  full-time roles.

## What's not in this repo

This is a sanitized copy of my personal working install. Left out on purpose:

- `data/jobs.db`, `data/profile.json` — my real application history and contact details.
- `.env` — my real API keys (`.env.example` is included as a template).
- `data/career_sites.json`, `data/greenhouse_boards.json`, `data/lever_sites.json` —
  ship empty here; populate with your own target companies (see "Maximizing coverage").

`data/resume.txt` and `tests/test_apply_tool.py` both use a fictional sample profile
("Jane Doe") instead of a real person's details — replace `data/resume.txt` with your
own before relying on this for a real search; the test fixture just needs *some*
profile object to exercise the form-filler against.

## Deploying the public demo

The repo also supports running as a **public, gated demo** — a live, hosted instance
anyone can request time-boxed access to, without ever touching a real person's resume,
job history, or API quota. This is opt-in and off by default (`DEMO_MODE` unset or `0`
behaves exactly like the local single-user tool described above); flip it on only on a
deployment you intend to be public.

**What demo mode actually changes, all in `app.py`/`demo_gate.py`:**

- Every route sits behind an email-gated magic link (`demo_gate.py`): a visitor enters
  their email at `/demo`, gets a one-time link (single-use, expires after
  `DEMO_LINK_EXPIRY_MINUTES`, default 30), and clicking it starts a session. Rate-limited
  both per-email-per-day and per-IP-per-hour before a token is even issued.
- All demo visitors share **one** sandboxed dataset — `data/demo_resume.txt`,
  `data/demo_profile.json`, `data/demo_jobs.db` — never the real `data/resume.txt` /
  `data/profile.json` / `data/jobs.db` files. Two fictional sample files
  (`data/demo_resume.txt`, `data/demo_profile.json`) ship in the repo for this.
- Discovery is capped at `DEMO_MAX_RUNS_PER_EMAIL` (default 1) real run per visitor, and
  each run is capped to `DEMO_ADZUNA_MAX_PAGES` (default 1) Adzuna page instead of the
  normal 5 — real listings, real scoring, just bounded so a stranger can't run up a real
  Adzuna/Gemini bill. Instahyre scraping is always off in demo mode regardless of what a
  request asks for.
- Tailor/outreach/prep (the other real LLM calls) share a combined
  `DEMO_MAX_EXTRA_ACTIONS_PER_EMAIL` allowance (default 10) per visitor.
- Contact-finding (`/api/jobs/<id>/contacts`) is disabled outright — Apollo's free tier
  is a small *shared* monthly quota (75 lookups), not something to sub-divide across
  anonymous visitors.
- Filling and submitting real application forms (`apply`) is disabled outright — no
  reason to let public traffic drive a real headless browser at a live third-party site.
- Add Lead / Add Browsed Page / editing the profile are all disabled — they'd mean
  arbitrary text from the public internet going straight into an LLM call on your
  account, or one visitor overwriting the shared sample profile for everyone else.
- The engine is fixed to `DEMO_ENGINE` (default `gemini`) — visitors don't get to choose.
- The Flask secret key is **required** to come from `FLASK_SECRET_KEY` (the app refuses
  to start in demo mode without it) rather than the hardcoded local-dev key, since a
  fixed public secret would let anyone forge a session cookie.

**What this is not:** a production-grade abuse-prevention system. The rate limits are
plain SQLite counters, sized for "a handful of people click a portfolio link," not for
withstanding a determined attacker with many real inboxes and a rotating IP. That's a
disclosed, deliberate trade-off for a personal-project demo, not an oversight.

**Sending the magic-link email: an HTTP API, not SMTP.** Most hosts — Render included —
block outbound raw SMTP connections (ports 465/587) to stop their infrastructure being
used for spam, so a Gmail-App-Password-over-SMTP approach fails from a real deployment
with `[Errno 101] Network is unreachable`, even though it works fine locally where
nothing's blocking the port. `demo_gate.py` instead sends via
[SendGrid](https://sendgrid.com)'s HTTP email API, which goes over the same port 443 as
any normal web request and isn't affected by the SMTP block. SendGrid specifically,
rather than an alternative like Resend: Resend's free/no-domain setup only lets you
email the account's own signup address (fine for a private tool, useless for a *public*
demo strangers need to actually receive email from), whereas SendGrid's **Single Sender
Verification** verifies one plain email address — no domain ownership required — and
then lets it send to any recipient.

**Deploying it (Render, or anything similar that reads a `Procfile`):**

1. Push this repo to your own GitHub account (see the main project README/case study
   for why this one's a fork-friendly, sanitized copy).
2. Create a free SendGrid account, then verify a Single Sender: **Settings → Sender
   Authentication → Verify a Single Sender**. Use the email address you want demo
   invitations to appear to come from (a personal Gmail address is fine) — SendGrid
   emails that address a confirmation link, click it to finish verifying. Then create an
   API key: **Settings → API Keys → Create API Key** (Full Access, or at minimum "Mail
   Send" restricted access is enough).
3. Create a Render (or equivalent) web service pointed at your repo. It picks up the
   `Procfile` (`gunicorn app:app --workers 1 --worker-class gthread --threads 8`) —
   deliberately **one worker**, since discovery progress lives in an in-process dict
   shared across requests within a single process; multiple worker processes would each
   have their own copy and the progress bar would poll the wrong one at random.
4. Set these environment variables on the host:
   - `DEMO_MODE=1`
   - `FLASK_SECRET_KEY` — generate one with
     `python -c "import secrets; print(secrets.token_hex(32))"`
   - `SENDGRID_API_KEY` — from step 2
   - `SENDGRID_FROM_EMAIL` — must exactly match the address you verified as a Single
     Sender in step 2, or SendGrid rejects the send outright
   - `GEMINI_API_KEY` (or `ANTHROPIC_API_KEY`, matching whatever `DEMO_ENGINE` you use)
     — consider a **separate** key from your personal daily-driver key, so demo traffic
     and your own usage aren't drawing on the same quota
   - `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` — optional; without these, Adzuna is silently
     skipped in the demo the same way it is locally
   - Optionally override any of `DEMO_ENGINE`, `SENDGRID_FROM_NAME`,
     `DEMO_REPLY_TO_EMAIL`, `DEMO_MAX_RUNS_PER_EMAIL`, `DEMO_ADZUNA_MAX_PAGES`,
     `DEMO_MAX_EXTRA_ACTIONS_PER_EMAIL`, `DEMO_LINK_EXPIRY_MINUTES`,
     `DEMO_MAX_REQUESTS_PER_EMAIL_PER_DAY`, `DEMO_MAX_REQUESTS_PER_IP_PER_HOUR` — all
     documented at the top of `demo_gate.py`.
5. Deploy. Visit the assigned URL's `/demo` and request access with your own email
   first, then — since the whole point of Single Sender Verification over Resend's
   sandbox mode is that it isn't restricted to your own address — try a second, different
   email address (a friend's, or a second inbox of your own) to actually confirm
   strangers can get in too, before sharing the link anywhere. Check spam either way —
   a fresh sending address has no reputation yet.

## License

MIT — see `LICENSE`.
