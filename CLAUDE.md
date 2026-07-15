# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Goat Search is a personal job-tracking web app. It scrapes career pages / ATS boards for new
postings, matches them against keywords, and uses the Anthropic API to draft tailored cover
letters and resumes from the user's own reference docs. Single-user, runs locally.

## Running it

```bash
source venv/bin/activate            # venv already set up in repo
python job_checker.py               # start web app at http://localhost:5000 — REAL tracked data
python job_checker.py --check       # run one scrape pass and exit (for cron/launchd)
python job_checker.py --port 8080   # custom port
```

**For any development/testing work, always use the dev data file, never the real one:**

```bash
python job_checker.py --dev --port 5099   # uses job_data.dev.json instead of job_data.json
```

`--dev` is shorthand for `--data-file job_data.dev.json` (there's also a bare `--data-file PATH`
flag, and a `JOB_DATA_FILE` env var, for other overrides). `job_data.dev.json` is a gitignored,
one-time seeded copy of the real file — it diverges over time as dev/test sweeps run against it,
and that's fine; it's not meant to stay in sync. **Never point a test run at `job_data.json`
itself** — this file is the user's real, live job search, and a test run that hits the real
Adzuna API can add/mutate real tracked jobs. This came from a real incident (2026-07-15): a
background sweep thread outlived a `kill` during testing and raced with a manual restore
attempt — no data was lost, but it was a close call. Use `--dev` + a distinct `--port` for any
verification, including quick one-off checks.

There is no test suite, linter, or build step — this is a single-file Flask backend plus a
single-file static frontend. Verify changes by running a `--dev` server and exercising the
API directly (`curl localhost:5099/api/...`); the user does hands-on browser verification
against the real instance themselves, not Claude (see project memory).

The project is a git repo (initialized 2026-07-15). `.gitignore` excludes `.env`,
`job_data.json`, `job_data.dev.json`, `venv/`, `applications/`, and `.claude/` — none of these
should ever be committed (`.env` has API keys; the job_data files are the user's personal,
frequently-changing runtime data, not code).

Config lives in `.env` (loaded via `python-dotenv`): `ANTHROPIC_API_KEY`, `OUTPUT_FOLDER`
(where generated application docs are written — normally a locally-synced Drive folder),
`ADZUNA_APP_ID` / `ADZUNA_APP_KEY` (for the job-search tab).

## Architecture

Two files hold essentially all the logic:

- **`job_checker.py`** — Flask backend, all routes and scraping/generation logic in one file.
- **`index.html`** — entire frontend: inline `<style>`, inline vanilla-JS `<script>` (no
  build step, no framework). Talks to the backend only via the `api()` helper hitting
  `/api/*` JSON routes.
- **`job_data.json`** — the entire datastore (companies, jobs, keywords, settings). Read/written
  wholesale via `load_data()` / `save_data()` on every request — no database, no migrations
  framework (schema changes are handled ad hoc inside `load_data()`, e.g. the
  `keywords` → `title_keywords`/`desc_keywords` split).
- **`career_docs/`** — markdown reference files (`bio.md`, `resume.md`, `instructions.md`)
  used as grounding context for the Claude-generated application docs. `instructions.md`
  becomes the system prompt; `resume.md` is excluded from the context sent to Claude because
  it's a template, not source material. Docs can alternatively be pulled live from Google Docs
  (URLs stored in `settings.gdoc_urls`, fetched via the doc's public `/export?format=txt`
  endpoint) — Google Doc content overrides the local file of the same name. Any
  `[exclude]...[/exclude]` block in a doc is stripped before it reaches the model.

### Scraping pipeline

`check_all_companies()` iterates tracked companies and dispatches by URL pattern
(`scrape_company()`): Greenhouse and Lever and Ashby have public JSON APIs and get scraped
directly; anything else falls through to `scrape_generic()`, an HTML heuristic scraper looking
for job-shaped anchor tags. `detect_ats_for_url()` is a separate helper (used by the "add
company" UI flow) that sniffs a careers page's HTML to suggest the correct ATS board URL,
including flagging JS-rendered boards (Workday, Rippling, etc.) that the scraper can't handle.

Jobs are identified by an md5 hash of `company_id|title|url` (`make_job_id`), so a job
re-appearing on a later scrape is recognized as the same job rather than duplicated. Newly
discovered jobs get `is_new: True` and stay flagged until the user dismisses them from the UI
— dismissal is separate from "seen in latest scrape."

Keyword matching (`score_job`) is **not stored** — it's computed live at read time in
`GET /api/jobs` against whatever keywords currently exist, so editing keywords instantly
re-scores the existing job list without re-scraping.

### Application generation pipeline

`POST /api/jobs/<id>/generate` (and the ad-hoc variant `POST /api/generate-adhoc` for jobs not
in the tracked list): fetches/reuses the job description text, loads career docs, calls Claude
via `generate_application_docs()`, and parses the response into named XML-tagged sections
(`<cover_letter>`, `<introduction>`, `<rentredi_bullets>`, `<metis_bullets>`, etc. — one bullet
block per resume role, hardcoded to this user's actual job history). `HARD_RULES` in that
function (no em dashes, no buzzwords, no fabricated numbers) are prepended to the system prompt
unconditionally, even if `instructions.md` fails to load — do not weaken or remove these
without being asked.

Output is written to `{OUTPUT_FOLDER}/{Company}/{Job Title}/{YYYY-MM-DD}/` as plain `.txt`
files plus a `sections.json` (used to reload a previously generated application in the UI
without re-calling Claude — see `api_existing_documents`). There's also a `.docx` template-fill
path (`_fill_template` / `_replace_paragraph`) that replaces `{{PLACEHOLDER}}` tokens in a
downloaded Google Docs template, for cases where a formatted resume doc is wanted instead of
loose bullet text files.

Two model tiers are configured in `MODELS`: `haiku` for cheap testing, `sonnet` for real
application generation, selected via `settings.model`.

### Adzuna search

`GET /api/search` queries Adzuna with `remote_only=1`, `salary_min=100000`, `full_time=1`, and a
configurable `max_days_old`; results are additionally filtered so only jobs with the query
phrase in the title survive. `POST /api/jobs/add-from-search` adds a result to the tracked list
— it follows the Adzuna redirect URL to resolve the real ATS URL before saving, and always sets
`is_remote: True` since results are pre-filtered remote-only. Note the redirect resolution often
doesn't get past Adzuna's own landing page (`adzuna.com/details/...`) — some listings resolve to
the real ATS URL, others don't; this is an Adzuna-side limitation, not a bug.

`_adzuna_request()` / `_adzuna_credentials()` / `_resolve_redirect_url()` are the shared helpers
behind both the manual search route and `sweep_adzuna_jobs()` below — don't reintroduce
duplicate inline Adzuna-calling code.

### Adzuna sweep

`sweep_adzuna_jobs()` (triggered via `POST /api/sweep-adzuna`, "Sweep Now" button on the Search
tab) pulls everything posted since the last *successful* sweep, across every configured
`title_keywords` phrase, and adds new matches directly to the tracked job list — same
`is_new: True` treatment as company-board scrapes. It is **manually triggered only, by design**
— there is no scheduler, cron, or launchd job for this, and the in-process `apscheduler` code
referenced by `/api/schedule` is dead code (imported but `_scheduler` is never instantiated) left
over from an earlier, abandoned approach to the company-board check. Do not add scheduling here
unless explicitly asked.

Mechanics worth knowing before touching this function:
- `last_adzuna_sweep` (top-level in `job_data.json`) only advances when a sweep completes with
  zero errors — a failed sweep is retried from the same starting point next click. This is safe
  because adding is idempotent (URL-based dedup against `existing_urls`).
- Adzuna's `max_days_old` filter is day-granularity only, so the sweep over-fetches by day and
  then does exact filtering client-side against `created` timestamps
  (`_parse_adzuna_created`), stopping pagination early once a page's results cross the
  `since` boundary (results are `sort_by=date` descending).
- First-ever sweep (no `last_adzuna_sweep` yet) defaults to a 24h lookback
  (`SWEEP_DEFAULT_LOOKBACK_HOURS`).
- `last_adzuna_sweep_result` is saved on every attempt (success or failure) so the UI always has
  something to show — distinct from `last_adzuna_sweep`, which is success-only.

### Frontend

`index.html` is tab-based (Jobs / Companies / Keywords / Settings / Search, via `switchTab`).
Polling (`startPolling` / `pollUntilDone`) drives the "check in progress" UI while a background
scrape thread runs server-side. Job filtering (new-only, remote-only, keyword match) happens
client-side in `renderJobs()`, not via query params, except where `GET /api/jobs` itself
supports `company` / `new_only` / `matching_only`. The apply flow is a modal
(`openApplyModal` → `startGeneration`) that previews the job description and career-doc word
counts before spending an API call; the result view shows Claude's feedback and contact info
alongside per-section copy buttons, and "View Documents" swaps to "Re-Generate Documents" once
docs already exist for that job.

## Roadmap

This app is being evolved toward an autonomous agent. See project memory for current phase
and status.

1. ~~Adzuna sweep~~ — done, see above. Built as a manual button, not a scheduled job (explicit
   user decision — see CLAUDE.md history / memory, not automated).
2. Autonomous triage — Claude scores each new JD apply / skip / maybe with reasoning.
3. Auto-generate documents above a confidence threshold, human reviews before submitting.

Human stays in the loop for: which jobs to apply to, and final review of generated docs.
