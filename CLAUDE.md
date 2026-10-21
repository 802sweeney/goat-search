# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Goat Search is a personal job-tracking web app. It scrapes career pages / ATS boards for new
postings, matches them against keywords, and uses the Anthropic API to draft tailored cover
letters and resumes from the user's own reference docs. Single-user, runs locally.

## Running it

```bash
source venv/bin/activate            # venv already set up in repo
python -u job_checker.py            # start web app at http://localhost:5000 — REAL tracked data
python job_checker.py --check       # company boards only, run once and exit
python job_checker.py --get-new-jobs # full pipeline (company boards + Adzuna sweep), run once and exit —
                                      # this is what the launchd schedule runs nightly, see "Scheduling" below
python -u job_checker.py --port 8080   # custom port
python job_checker.py --google-auth # one-time interactive OAuth setup for live Google Doc generation —
                                      # needs a real terminal with a browser, see Application generation pipeline
```

**Always use `-u` (unbuffered stdout) for the long-running server mode** — found 2026-08-07 while
debugging a real "why didn't the Google Doc get created" report (Hone Health): the server had
been running for hours with real generations happening, and `logs/server.log` had **zero**
`[gdoc]`/`[gdrive]`/`[parse]` lines anywhere — not even the routine ones every generation logs.
Python fully block-buffers stdout by default when it isn't a real terminal (i.e. once redirected
to a log file via `nohup ... > logs/server.log 2>&1 &`), so every `print()`-based soft-fail
message this app relies on for diagnosing exactly this kind of issue was silently sitting in an
unflushed buffer instead of reaching disk. This doesn't affect the one-shot CLI flags
(`--check`/`--get-new-jobs`/`--google-auth`) — a normal process exit flushes stdout regardless, so
`logs/get_new_jobs.log` has always been reliable — only the indefinitely-running server was ever
affected. `-u` (or equivalently `PYTHONUNBUFFERED=1`) makes every print appear immediately.

`venv/` was recreated from `/opt/anaconda3/bin/python3` on 2026-07-17 (Python 3.12) after the
original venv broke: it had been created against `/usr/bin/python3` (macOS Command Line Tools),
and a subsequent CLT/OS update silently changed what that path pointed to, breaking the venv's
self-detection (`venv/bin/python3` stopped resolving into the venv's own site-packages at all).
Rebuilt against Anaconda's python3 instead specifically because that's a stable, independently-
versioned binary not tied to Apple's CLT update cycle — if `source venv/bin/activate` or
`venv/bin/python3 -c "import flask"` ever silently stops working again, check `sys.prefix`
(should point inside this repo's `venv/`, not `/opt/anaconda3` or a CLT framework path) before
assuming it's a code problem. Separately: `pip install` anything for this project **only**
via `venv/bin/pip` (or after confirming `which pip` really points into `venv/bin` post-activate)
— installing into the wrong environment by mistake here once already required real cleanup in
the user's global Anaconda base env (a version conflict with unrelated `dbt`/`streamlit`
packages there).

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

Config lives in `.env` (loaded via `python-dotenv`): `ANTHROPIC_API_KEY`, `ADZUNA_APP_ID` /
`ADZUNA_APP_KEY` (for the job-search tab), `OUTPUT_DRIVE_FOLDER_ID` (the Drive API folder ID of
the "Applications" folder generated application docs get uploaded into — see Application
generation pipeline below). `credentials.json` / `token.json` (also gitignored, also in the repo
root) hold the Google OAuth client and cached token for that same feature. `OUTPUT_FOLDER` is no
longer read anywhere in code (2026-07-21 — see "Real duplicate-folder bug" below for why); local
generated-doc caching now always uses `BASE_DIR/applications/`, not an env-configurable path.

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

### Ingestion: "Get New Jobs" — one funnel, every source

There are two raw-candidate sources — company career-page scraping and the Adzuna sweep — but
**every candidate, regardless of source, goes through the same `evaluate_candidates()` funnel**
in `job_checker.py` before it's ever added to `data['jobs']`. This was a deliberate unification
(2026-07-15): before it, company-scraped jobs were added completely unfiltered while only Adzuna
candidates got keyword-filtered and duplicate-checked. Don't reintroduce a source-specific
shortcut that bypasses the shared funnel — if a new ingestion source is ever added, it should
build a list of raw candidates (shape: `title`, `company_name`, `url`, `location`, `description`,
`posted`, `source`) and hand them to `evaluate_candidates()` like the other two do.

**`evaluate_candidates(candidates, data)`** runs each batch through, cheapest-first:
1. Title-keyword pre-filter — two checks, both simple case-insensitive substring matching
   (consistent with `score_job()`'s style everywhere else in the app, not word-boundary-aware —
   e.g. `title_exclude_keywords: ["director"]` will also match "Directory Services"; a known,
   accepted tradeoff, not a bug):
   - `title_exclude_keywords` (e.g. `["staff", "principal", "director"]` — seniority levels not
     being targeted): any match → exclude `title_excluded`, checked first
   - `title_keywords` (empty list = show everything, same convention as `score_job()`): no match
     → exclude `title_mismatch`
2. Adzuna redirect resolution (`_resolve_redirect_url`) — only for survivors of #1, so excluded
   candidates never cost a network call
3. URL dedup against already-tracked jobs → exclude `duplicate_url`
4. **Recent-repost check** (PROBLEMS.md #3): scans `data['jobs']` for a normalized
   `(company_name, title)` match with `first_seen` inside `RECENT_REPOST_DAYS` (30) → exclude
   `recent_repost`. No separate ledger — jobs are never deleted from `data['jobs']`, so this is
   just a scan.
5. **Location-cluster dedup** (PROBLEMS.md #2): groups same-batch `(company_name, title)`
   duplicates and keeps only the one ranked best by `state_rank()` (a hand-authored
   `STATE_RANK` list — New England first, fanning out — instead of real geocoding; a
   `parse_state()` regex/lookup pulls a state out of free-text locations like `"New York, NY"`)
   → exclude `duplicate_location`
6. **Remote + software-relevance classification** (PROBLEMS.md #1 and #4): fetches full JD text
   via `fetch_job_description()` and makes **one** Haiku call via `classify_job()` returning both
   judgments with reasoning. A failed fetch or API call does **not** exclude the job — it's kept
   with `remote_status`/`software_status: 'unknown'` so nothing silently vanishes. On success:
   not-remote → exclude `not_remote`; not software-relevant → exclude `not_software`. The full
   fetched text is always written to `job['description']` here now (fixed 2026-07-20 — it used to
   be fetched into a local variable, used for classification, then thrown away, leaving only
   whatever short snippet the raw candidate started with) — every job, accepted or excluded, ends
   up with the real text it was actually judged against. This is what makes Sonnet re-evaluation
   (see below) possible for jobs excluded via this funnel, not just ones classified through
   `_apply_classification()`'s callers, which already persisted it.

Returns `(to_save, excluded)`. `excluded` is transient (this run's list only) and carries
`reason` (+ `reasoning` for the classification-based reasons) for the "View excluded" panel.
`to_save` is what actually goes into `data['jobs']` — every job that passed, **plus** the four
judgment-call rejections (`title_excluded`, `title_mismatch`, `not_remote`, `not_software`),
persisted with `exclusion_reason`/`exclusion_reasoning` set so they're reviewable later (see
Excluded sub-tab, below). The three pure-dedup reasons (`duplicate_url`, `recent_repost`,
`duplicate_location`) are **not** added to `to_save` — nothing to review when an equivalent job
is already tracked. `_build_excluded_job()` builds the minimal record for the two title-filter
reasons (no JD fetch/classification ever ran); the two classification reasons just mark the
already-built `job` dict in place instead of discarding it.

**Company-board scraping**: `check_all_companies()` iterates tracked companies and dispatches by
URL pattern (`scrape_company()`): Greenhouse, Lever, and Ashby have public JSON APIs and get
scraped directly; anything else falls through to `scrape_generic()`, an HTML heuristic scraper
looking for job-shaped anchor tags. `detect_ats_for_url()` is a separate helper (used by the "add
company" UI flow) that sniffs a careers page's HTML to suggest the correct ATS board URL,
including flagging JS-rendered boards (Workday, Rippling, etc.) that the scraper can't handle.

Company-scraped jobs are identified by an md5 hash of `company_id|title|url` (`make_job_id`), so
a job re-appearing on a later scrape is recognized as the same job rather than duplicated.
**Important:** `check_all_companies()` only ever adds/refreshes/drops jobs whose `company_id` is
a currently-tracked company — jobs from other sources (Adzuna, ad-hoc) are explicitly preserved
untouched at the top of the function. This fixes a real bug found 2026-07-15: the function used
to unconditionally rebuild `data['jobs']` from just that pass's scrape results, silently deleting
every non-company-sourced job on every check. If you touch this function, keep that preservation
step — it's not optional cleanup, it's the fix.

Newly discovered jobs get `is_new: True` and stay flagged until the user dismisses them from the
UI — dismissal is separate from "seen in latest scrape." Keyword matching (`score_job`) for
*display* purposes (sorting/highlighting already-tracked jobs) is separate from the
`evaluate_candidates()` pre-filter above and is **not stored** — it's computed live at read time
in `GET /api/jobs` against whatever keywords currently exist, so editing keywords instantly
re-scores the existing job list without re-scraping. The pre-filter only gates whether a *new*
candidate gets added at all.

### Per-job token usage tracking (2026-08-03)

Every Claude API call made for a job accumulates onto `job['token_usage']`, broken out by which
part of the pipeline spent it:

```
job['token_usage'] = {
  'classification':  {'input_tokens': N, 'output_tokens': N, 'calls': N},
  'fit_rating':       {...},
  'generation':       {...},
  'todo_matching':    {...},
}
```

`_add_token_usage(job, part, usage)` (job_checker.py) is the single shared accumulator — sums
rather than overwrites, since a job's *lifetime* cost is the useful number (a job can be
re-evaluated, re-rated, or regenerated more than once), and no-ops on a falsy `usage` so every
call site can pass a result straight through without an extra guard. Every Claude-calling
function (`classify_job`, `extract_and_classify_job`, `rate_job_fit`, `generate_application_docs`,
`generate_interview_prep`, `_extract_and_match_todo_items`) returns its usual result plus a
`usage: {'input_tokens', 'output_tokens'}` dict (`_usage_dict(message)` — pulled straight off the
Anthropic SDK response) — two of these (`generate_interview_prep`, `_extract_and_match_todo_items`)
previously returned a bare list, not a dict, so their signatures changed to `(result, usage)`
tuples; both had exactly one call site, updated alongside.

**Where each part gets recorded, and why so few call sites needed touching**: `classify_job()`'s
and `rate_job_fit()`'s usage gets recorded in exactly two places — `evaluate_candidates()`'s own
inline classify/rate block (its documented reason for not sharing `_apply_classification()`
still applies, so this needed its own two `_add_token_usage()` calls), and inside
`_apply_classification()` itself. That second one covers **every other classification call site
at once** (capture_job()'s three outcomes, screen_found_jobs(), the manual reclassify route,
reevaluate_job(), add-from-search, generate-adhoc's classify step) for free, since they all
already funnel through that one shared helper — exactly the kind of shared choke point this
codebase already favors (see `_apply_classification()`'s own docstring). `track_job_manually()`
calls `rate_job_fit()` directly (no classification involved in a manual override), so it got its
own `_add_token_usage()` call. `generation` and `todo_matching` are both recorded inside
`save_application_docs()` — the one place both `generate_application_docs()`'s result and
`_update_todo_doc()`'s result are already in hand.

**A real, pre-existing gap surfaced by this work, not introduced by it**: neither
`api_generate_application()` nor `api_generate_adhoc()` previously called `save_data()` after
generating — generation never used to mutate any job-record field, so there was nothing to
persist. Since `token_usage` is now mutated in place on the job dict during
`save_application_docs()`, both routes needed a `save_data(data)` added after it, or the usage
would compute correctly in memory and then vanish on the next `load_data()`. One residual gap
**not** fixed here (pre-existing, and out of scope to fix now): regenerating an already-tracked
ad-hoc job rebuilds `synthetic_job` as a fresh dict sharing the old job's id but disconnected
from the actual object sitting in `data['jobs']`, so classification/rating updates on a
regenerate were already silently discarded before this change too — `token_usage` inherits that
same limitation rather than fixing it, since fixing it is a separate, real piece of work.

**Interview prep is the one part NOT stored on `job['token_usage']` by default** — deliberately,
matching that whole feature's disk-native design (see "Interviews area" below: resolved via
`_walk_application_folders()`, not `data['jobs']`, specifically so it still works after a job
record is purged). Its usage is written into `interview_prep.json` itself
(`prep['token_usage']`) instead, with a best-effort mirror onto the matched `data['jobs']`
record (`_add_token_usage(matched_job, 'interview_prep', usage)` + `save_data()`) only when that
record still exists.

`sections.json` also gets a `token_usage` snapshot of *that specific generation's* own cost
(`{'generation': ..., 'todo_matching': ...}`) alongside the existing `todo_changes` field — same
reasoning as `todo_changes` (2026-07-27): durable on disk even after `job['token_usage']`'s
underlying job record is purged by `JOB_RETENTION_DAYS`. `job['token_usage']` itself is the
separate, cumulative lifetime total, valid only for as long as the job record exists.

**Verified end-to-end 2026-08-03** against a real disposable `__TEST__` ad-hoc job (per the
standing rule that `BASE_DIR/applications/`, Drive, and the To Do doc are **not** `--dev`-isolated
— see "Dev/prod data isolation" in project memory): a real generate-adhoc call correctly populated
all four `token_usage` parts on the job record. Because `todo_matching` ran against the **real**
To Do doc (fit_rating came back 4, which passes the add-new-items gate), 3 real test items landed
in the real doc — caught immediately and removed the same way the To Do checkbox feature's own
verification did (read current items, filter out the exact test text, `_write_todo_table()` the
rest). The real filled Google Doc and its Drive folder created during the same test were both
trashed (reversible, not permanent-deleted) via the Drive API directly, and the local
`applications/__TEST__ .../` folder was removed.

**Costs area (2026-08-03, same day)** — a new 6th sidebar nav item (`#/costs`, between To Do and
Settings) surfaces this directly: `GET /api/costs` returns every job with a non-empty
`token_usage`, sorted by total tokens descending, and `renderCostsList()` shows one card per job
(company/title, total input/output tokens, then a per-part breakdown line for each of
classification/fit_rating/generation/todo_matching/interview_prep that's actually present on that
job) plus a running grand-total card at the top. Fetched fresh on every visit (`loadCosts()`),
same reasoning as the To Do area — token usage changes in the background from ingestion sweeps
and generations, not just actions taken on this page. `COST_PART_LABELS` maps the internal part
keys to display names. Same `data['jobs']`-sourced limitation as everything else in this
section — a purged job's costs stop showing here even though a per-generation snapshot may still
exist in that application's `sections.json`/`interview_prep.json`.

### Application generation pipeline

`POST /api/jobs/<id>/generate` (and the ad-hoc variant `POST /api/generate-adhoc` for jobs not
in the tracked list): fetches/reuses the job description text, loads career docs, calls Claude
via `generate_application_docs()`, and parses the response into named XML-tagged sections
(`<cover_letter>`, `<introduction>`, `<rentredi_bullets>`, `<metis_bullets>`, etc. — one bullet
block per resume role, hardcoded to this user's actual job history). `HARD_RULES` in that
function (no em dashes, no buzzwords, no fabricated numbers) are prepended to the system prompt
unconditionally, even if `instructions.md` fails to load — do not weaken or remove these
without being asked.

**Real bug found and fixed 2026-08-05: preview/generate always re-fetched the JD from `job['url']`,
ignoring `job['description']` even when it was already good.** Surfaced by a real House Rx
(Himalayas-sourced) job: the generated cover letter literally said "I wasn't able to retrieve the
job description from that URL" — Claude was telling the truth about what it was handed. Root
cause: `api_preview_application()`/`api_generate_application()` only ever checked (a) an existing
saved `Job Description.txt` from earlier today, then (b) a fresh `fetch_job_description(job)` —
skipping `job['description']` entirely, even though that field already held the real, full JD
text from ingestion-time classification (`jd_fetch_status: 'ok'`, confirmed for this exact job).
That gap was invisible before Jobicy/Himalayas existed, because every other source's URL is
normally re-fetchable — those two are the first sources whose real pages are *permanently*
Cloudflare-blocked (see their sweep functions' module notes), so a fresh fetch at generation time
reliably reproduces the same 403 that ingestion already worked around via the `_fallback_jd_text`
mechanism, instead of reusing the text that mechanism already captured.

Fixed with a shared `_resolve_job_description_for_generation(job)` (job_checker.py, right after
`_find_existing_jd_file()`), used by both routes: existing today's-file → `job['description']`
if `is_real_job_description()` says it's usable (**new**) → fresh fetch as a last resort (for jobs
whose description was never populated, e.g. very old records or a job added without ever being
classified).

**Second bug, found immediately after via a real user regenerate attempt that still didn't work**:
the "existing today's-file" step was unconditionally trusting any file that existed, with no
content check — so once the *first* failed attempt had already written the fetch-failure string
to disk as `Job Description.txt`, every regenerate that same day kept reusing that exact bad
cached file and never even reached the `job['description']` fallback. Fixed by gating step 1 on
`is_real_job_description()` too: a stale fetch-failure file is now treated as if no file existed
at all. The mirror-image gap also existed in `save_application_docs()`'s own JD-file write, which
was previously "only written once per day" unconditionally — a real fetch failure's stale file
would never get overwritten even after a later regenerate resolved real content in memory, so the
in-memory generation would be correct but the on-disk `Job Description.txt` the user actually
looks at would stay stale forever. Fixed the same way: the existing file only blocks a rewrite if
its *content* is real, not merely if it exists.

Verified against the real House Rx job/file (read-only checks, no writes performed by the
verification itself): confirmed the existing on-disk file is correctly detected as not-real, and
the resolver now correctly falls through to `job['description']`'s full 8000-char text. The
already-generated House Rx application still has the broken files on disk from before this fix —
clicking "Generate Documents" again will now both produce a real cover letter and overwrite the
stale local `Job Description.txt` with the real text.

**Per-application "Additional Instructions" field (2026-08-07)**: a new textarea at the bottom of
the preview step (`#preview-additional-instructions`, right above the "Generate documents"
button), shared by both the tracked-job and ad-hoc flows since `startGeneration()` already
handles both through one form. Free text the user types per-application — personal details or
context specific to just this one job/company (a connection they have there, why this company
matters to them) — meant mainly to inform the cover letter, but **deliberately not restricted to
only the cover letter**: the user explicitly didn't want that enforced yet, so it's just appended
as an extra `## Additional Instructions For This Application` block in `generate_application_docs()`'s
user message, after the job description, and the model decides where (if anywhere else) it's
relevant. This supplements, not replaces, `instructions.md`'s always-sent generic system prompt —
there's no persistent setting behind it; it only exists for that one generation.

Threaded through both `api_generate_application()` and `api_generate_adhoc()`
(`body.get('additional_instructions', '')`) into `generate_application_docs()` and
`save_application_docs()`, which persists it into `sections.json` (and `_load_application_content()`
returns it) purely as a record of what was actually asked for on that generation — same reasoning
as `todo_changes`/`token_usage` already living there. Reset to empty every time `loadJobDetail()`
loads a fresh job/ad-hoc page, same as the JD textarea, so it never leaks from one application to
the next.

Verified live with a disposable `__TEST__` ad-hoc application: a fabricated personal detail
("I used ZORPTRACK9000 at a previous company, saved 10 hours/week") given in this field became
the cover letter's literal opening line — confirming it's actually reaching and influencing
generation, not just being silently accepted and ignored. Real Drive doc/folder created during
that test were trashed after, and the test job record was removed from `job_data.json` directly
while the production server was still running — a real (if narrow, single-writer-window) race
risk against the server's own `load_data()`/`save_data()` cycle, worth avoiding next time by
checking `get_jobs_running` immediately before a direct file write, not just earlier in the
session.

**Displayed on the Application detail page (2026-08-07, same day)**: a new
`#result-additional-instructions-block` (styled and positioned identically to the existing
"To Do List Changes" block, right below it) shows exactly what was typed into this specific
generation's Additional Instructions field, via `renderAdditionalInstructionsUsed()` — hidden
entirely when empty, which correctly covers both "field genuinely left blank" and "this
application predates the feature" (`_load_application_content()` defaults the key to `''` for
older `sections.json` files with no such key at all, same convention `todo_changes` already
established with `null` for its own pre-existing-application case). `GET /api/applications/<id>`
needed no route change — `_load_application_content()`'s dict is already spread directly into
that endpoint's response (`{**match, **content}`). Verified against a real pre-existing
application (Iron Mountain, generated before this feature existed): confirms as empty string, so
the block correctly stays hidden rather than showing empty noise.

**`api_generate_adhoc()` classifies + rates like every other job (2026-07-20)**: fixed a real
gap — this route used to build a bare job record with no `remote_status`/`software_status`/
`fit_rating`/`evaluated_at` at all, ever (an ad-hoc job never went through the classifier just
because of how it entered the system). Now calls `classify_job()` and `_apply_classification()`
right after building the record, using the JD text already in hand (the user pasted it, no fetch
needed). Generation still proceeds regardless of the verdict — the user explicitly asked to
generate for this specific job by pasting a JD and clicking the button; classification here is
about giving the resulting record accurate, consistent metadata, not gating an action already
taken.

Output is written to `{Company}/{Job Title}/{YYYY-MM-DD}/` under a local cache folder (see
`get_output_dir()` — deliberately not Drive-synced as of 2026-07-21, see "Real duplicate-folder
bug" below) as plain `.txt` files plus a `sections.json` (used to reload a previously generated
application in the UI without re-calling Claude — see `api_existing_documents`). If Drive is
configured, copies of the same files get uploaded directly to Drive too.

### Live Google Doc generation (2026-07-17)

`save_application_docs()` also copies the user's Google Docs application template into that
same job's folder — as a real Drive folder now, not just the local sync path — and fills its
`{{PLACEHOLDER}}`s directly via the Google Docs API, so the user gets a fully formatted,
ready-to-use Doc instead of copy/pasting each section by hand. This **replaced** an earlier
`.docx`-based template-fill path (`_fill_template`/`_replace_paragraph`, python-docx paragraph
XML surgery) that was built but never wired to any route — found and deleted 2026-07-17 rather
than left as unused dead code once the live-Doc approach was chosen instead (the user had
already been formatting `.docx` output by hand and didn't want more of that).

Needs real Google OAuth (not a service account — the user's Drive is a personal Gmail account,
and service accounts have no storage quota of their own on personal Drive, reliably hitting
permission/quota errors; OAuth acts as the user directly). One-time setup, done by the user in a
real terminal with a browser (same reasoning as the `pmset` sudo step under Scheduling — no TTY
here for the browser-based consent flow):
1. Create a Google Cloud project, enable the **Drive API** and **Docs API**.
2. Create an OAuth 2.0 Client ID (Desktop app), download as `credentials.json` in the repo root.
3. Set `OUTPUT_DRIVE_FOLDER_ID` in `.env` — the Drive folder ID of wherever generated
   application docs should live in Drive (from that folder's Drive web UI URL).
4. Set the template Doc URL in Settings → Google Docs → "Application Template" — a normal Doc
   the user owns, no public sharing needed (unlike `bio`/`resume`/`instructions`, which use the
   public `/export?format=txt` endpoint and do need "anyone with the link" sharing).
5. Run `python job_checker.py --google-auth` once — opens a browser for consent, caches a
   refresh token to `token.json`. Both `credentials.json` and `token.json` are gitignored
   secrets, same category as `.env`.

`_get_google_creds()` / `_drive_service()` / `_docs_service()` are the shared auth/client
helpers (job_checker.py). `_get_or_create_job_drive_folder()` mirrors `get_output_dir()`'s
Company/Title/Date structure as real Drive folders (idempotent — `_find_or_create_drive_folder()`
looks up by name before creating, so regenerating the same day doesn't create duplicates).

**Refresh token expires every few days — real, recurring, root-caused 2026-07-27:** this app's
OAuth consent screen is still in Google Cloud Console's "Testing" publish status, which caps
refresh-token lifetime to a few days regardless of usage — confirmed twice for real (tokens
issued 2026-07-23 and again after re-auth both died within days, `invalid_grant: Token has been
expired or revoked` on the next Drive call). Re-running `python job_checker.py --google-auth`
(same real-terminal-with-browser requirement as initial setup) always fixes it, but the
recurrence itself needs a human decision, not a code fix: publishing the OAuth consent screen to
"In production" (Google Cloud Console → APIs & Services → OAuth consent screen → Publish App) is
worth trying, since Testing status's flat short expiry is specifically what a production-status
app doesn't have — not guaranteed to fully resolve it, since `GOOGLE_OAUTH_SCOPES` requests the
full `drive` scope (not the narrower `drive.file`), which Google treats as sensitive/restricted,
so an unverified production app may still hit some friction.

**Update 2026-07-27, same day: the user published the app** (no formal verification submission
needed in their case — the sensitive-scope verification requirement discussed above turned out
not to block a plain publish for this app). Ran `--google-auth` again immediately after
publishing to mint a fresh token under the new status, since a token already issued under Testing
status doesn't retroactively gain the longer lifetime just because the app was published
afterward. **Outcome not yet confirmed** — the only real test is whether this new token survives
longer than the ~4-day pattern seen twice before under Testing status. If a future session finds
`token.json` expired again despite this, the "In production" change did not fully resolve it
(plausible given the sensitive `drive` scope caveat above) — check the file's age via
`ls -la token.json` and don't assume it's a new/different bug before checking this history.

**`GoogleAuthExpired`** (job_checker.py, a `RuntimeError` subclass) is what `_get_google_creds()`
raises specifically when `creds.refresh()` rejects the stored refresh token (as opposed to any
other Drive/Docs failure — network blip, bad file ID, missing `OUTPUT_DRIVE_FOLDER_ID`, etc.),
so callers that care can tell "auth needs re-running" apart from "something else broke" without
string-matching error text. Added after this exact failure was found silently indistinguishable
from an ordinary "no Google Doc configured" case in the doc-health panel (see "Doc health panel"
below) — every Drive-dependent feature's error logs get the clear, actionable message for free
from this one shared choke point (the resume-template fill, the To Do doc update, and the
doc-health metadata lookup all go through `_get_google_creds()`), not just the one that prompted
finding it.

**Real duplicate-folder bug, found 2026-07-20, root-caused and eliminated 2026-07-21:**
`OUTPUT_FOLDER`'s local `.txt` writes used to get picked up and synced to Drive by Google Drive
Desktop, completely independently of this code's own direct Drive API calls — that sync wasn't
instant, and calling `_get_or_create_job_drive_folder()` right after the local writes could race
it: the folder-search found nothing yet (Drive Desktop's sync-created folder wasn't indexed
server-side yet), so a second, duplicate folder got created, and Drive Desktop — unaware of that
one — ended up with its own separate folder for the same local path too (visible locally as a
Finder-style `"Name (1)"` folder holding just a `.gdoc` shortcut, alongside the original holding
the real `.txt` files). Confirmed for real three times (NineTwoThree, FlowFuse, Encyclopaedia
Britannica), each merged by hand. Two rounds of widening the retry-before-create window (3s,
then ~12s across a few retries) both failed to close the gap — the third occurrence's actual
folder-creation gap was only 3.86s, comfortably inside the 12s window, meaning Drive's search
index has more lag than the raw creation timestamp suggests. Timeout-guessing had a proven
track record of failing.

**The actual fix**: stop giving Drive Desktop anything to race in the first place. `.txt`/
`sections.json` writes now go to a purely local cache (`get_output_dir()` always resolves under
`BASE_DIR/applications/`, never `OUTPUT_FOLDER` — that env var is fully unused in code now,
though still harmless if left in `.env`), which Drive Desktop doesn't watch at all. This app
uploads copies of those same files directly to Drive itself (`_upload_file_to_drive()`, new
helper near `_create_filled_google_doc()` — same trash-existing-then-create-fresh idempotency
pattern) into the same folder the resume template gets filled into. One writer, structurally,
not probabilistically — Drive Desktop still mirrors the Drive-API-created folder back down to
the user's local Drive-synced view (for normal browsing), just in the opposite sync direction,
uncoordinated with anything this app writes locally now. `_find_or_create_drive_folder()`'s
retry loop is kept (docstring updated) for a narrower residual case — this app's own two
overlapping calls for the same job — not the original two-system race, which is now impossible.
Verified end-to-end against a real throwaway job: one Drive folder gets all five files plus the
filled Doc, no duplicate; regenerating replaces content in place rather than duplicating.
This only affects *new* generations — every job's existing Drive folder and local file history
from before 2026-07-21 is untouched.

`_create_filled_google_doc()` trashes any previous Doc of the same name in that folder first
(not permanent delete — reversible), then `files.copy`s the template and fills it via one
`documents.batchUpdate` `replaceAllText` request per placeholder — a regenerate always gets a
fresh copy rather than trying to edit the old one in place, since once a copy's placeholders are
replaced with real text there's nothing left to re-fill. The resulting Doc's filename is always
the fixed literal `ksweeney_letter_resume` (2026-07-20, per the user — no per-job variation;
distinguishing between jobs is what the Company/Title/Date folder structure is already for, not
the filename). Uses the exact same uppercase section keys (`COVER_LETTER`, `INTRODUCTION`,
`RENTREDI_BULLETS`, `METIS_BULLETS`, `SONOS_BULLETS`, `SPOTIFY_PM_BULLETS`,
`SPOTIFY_ENG_BULLETS`, `PHONE`, `ADDRESS`) that `generate_application_docs()` already produces,
plus two added directly in `save_application_docs()` (2026-07-20, not part of Claude's output):
`TODAY` (`datetime.now().strftime('%B %d, %Y')` — e.g. "July 20, 2026") and `COMPANY`
(`job['company_name']`) — for a template's date line / salutation. `_create_filled_google_doc()`
no longer takes a `job` parameter — it only ever needed it for the old per-job filename, which
is gone now that the filename is fixed.

This step **soft-fails**: a missing template, missing auth, or any API error is caught and
logged, never blocks the four steps that already worked before this existed (the `.txt` files
and `sections.json`) — same resilience pattern used everywhere else in this app (e.g. a failed
JD fetch never blocks the rest of `evaluate_candidates()`). `results['google_doc_url']` is unset
when this happens; the frontend's "Open in Google Docs" link only shows when it's present.

**Verified end-to-end 2026-07-17** against the user's real template and real Drive (test job
`__TEST__ Company/Role`, trashed after). One real formatting bug found and fixed in the process:
Google Docs auto-applies the placeholder paragraph's own list-bullet style to every paragraph a
`replaceAllText` newline splits it into — so Claude's own literal `- `/`* `/`• ` markers on each
bullet line (its normal style for bullet sections) produced a real bullet *and* a literal dash
side by side on every line ("bullet pairs" per the user). Fixed with `_strip_bullet_markers()`,
applied to every placeholder value before insertion — strips a leading marker per line, leaves
everything else untouched (single-paragraph fields like `COVER_LETTER`/`PHONE`/`ADDRESS` pass
through unaffected). Plain `replaceAllText` turned out sufficient; the structural
`insertText`/`createParagraphBullets` fallback discussed during planning was never needed.

### To Do doc — vote-tracked gaps from generation feedback (2026-07-20)

Every generation's `feedback` (the `<strategy>`/`<missing_info>`/`<assessment>` blob from
`generate_application_docs()`) gets scanned for actionable gaps, which accumulate in a
dedicated Google Doc (`gdoc_urls.todo`, OAuth-accessed like the resume template — no public
sharing needed) as a **2-column table (Votes | Task), ranked by vote count descending** — the
same doc a gap has already been mentioned in gets a vote instead of a duplicate entry, so it
surfaces the most-repeated feedback across every application, not just the most recent.

**Gating by `fit_rating`** (user-specified rule, `_update_todo_doc()` in job_checker.py):
- 4 or 5 → gaps get added as new items, or vote up an existing match.
- 3 → never creates new items from this feedback, but a match still gets a vote.
- 1, 2, or unrated (`None`) → skipped entirely, no API calls at all — matches the existing
  instinct elsewhere in this app to never spend a call on something already excluded/poor fit.

**Matching "by meaning," not exact text** — whether a new gap is the same as an existing item is
a semantic judgment (different wording, same issue), so this is a Haiku call
(`_extract_and_match_todo_items()`), not string matching: given the feedback text and the
current item texts, it extracts distinct actionable gaps and, for each, either echoes an
existing item's text back **verbatim** (never paraphrased) or proposes new wording. The verbatim
requirement is what lets the caller match by exact string equality afterward without wording
drift silently creating near-duplicate entries over time.

**Read/write mechanics** (`_read_todo_items()` / `_write_todo_table()`): every update does a
full read-modify-rewrite of the doc, never an incremental edit — same reasoning as
`_create_filled_google_doc()`'s always-fresh-copy approach, reconciling a changed item list
against existing table rows in place would be far more fragile. Reading fresh before every write
also means anything the user manually edits/deletes in the doc between generations is respected
as the next update's baseline — no separate ledger, consistent with how this app already
handles repost/dedup detection by scanning current state instead of keeping history. Writing a
table via the Docs API needs two round trips, verified empirically against a real throwaway test
doc (created, exercised through several rating/vote scenarios, trashed after): one
`batchUpdate` clears the body and inserts an empty table, then — only after a re-fetch, since
cell indices aren't known until the table actually exists server-side — a second `batchUpdate`
fills each cell via `insertText`, processed last-cell-to-first within that batch so an earlier
insertion never shifts a not-yet-processed cell's index. Falls back to parsing plain paragraph
lines (`'<count> <text>'`, no leading number defaults to count 1) if the doc has no table yet, so
a doc the user seeds by hand with a plain list converts smoothly on the first automated update.

This step **soft-fails** exactly like the resume-template step above — a missing to-do doc,
missing auth, or any API error is caught and logged, never blocks anything else in
`save_application_docs()`.

**Per-application change summary surfaced on the Application detail page (2026-07-27)**:
`_update_todo_doc()` now returns `{'added': [str, ...], 'voted': [str, ...], 'reason': str}` —
`reason` explains why nothing happened when both lists are empty (rating too low, no to-do doc
configured, no gaps identified, or an error), so the UI always has something informative to show
rather than a bare empty state. Persisted into that generation's `sections.json` as
`todo_changes` (via `save_application_docs()`) and threaded through `_load_application_content()`
so `GET /api/applications/<id>` returns it — a "To Do List Changes" section on the Application
detail page (`renderTodoChanges()`) shows exactly what this specific generation changed, not just
that the feature exists somewhere. Applications generated before this field existed have
`todo_changes: null`; the frontend hides the section entirely for those rather than implying
"nothing changed" for an application that was never tracked in the first place.

**To Do area — surfaces the doc itself in the app, with checkboxes (2026-07-28)**: previously the
to-do list was only viewable by opening the actual Google Doc; a new left-sidebar "To Do" area
(`#/todo`) mirrors it read/write. `GET /api/todo` returns the current items (`_read_todo_items()`,
sorted by vote count descending, same as the doc's own table) — fetched fresh on every visit
(`renderRoute()`), not cached in `state.*` between navigations, since items can change from
generation feedback happening entirely outside this page. `_todo_doc_id(settings)` (extracted
from what was inline logic in `_update_todo_doc()`) is the shared doc-ID lookup both this and the
existing auto-update path use.

Checking an item off (**user-specified**: no "done but kept" state, no undo) calls
`POST /api/todo/complete` with the item's exact text — `_extract_and_match_todo_items()`'s
verbatim-echo-on-match requirement is what makes this exact-string match reliable, same
assumption the auto-update path already depends on. The route re-reads current items, filters out
the match, and rewrites the table via the existing `_write_todo_table()` — same full
read-modify-rewrite pattern as everywhere else this doc is touched, so a manual edit the user made
directly in the Doc between page loads is still respected as the baseline.

**Verified against the real to-do doc, not a throwaway** (2026-07-28) — unlike `job_data.json`,
the To Do Google Doc is **not** `--dev`-isolated at all (same class of gap as
`BASE_DIR/applications/`, see the Interviews section above: it's referenced by URL in settings,
identical regardless of which data file is loaded). Verification appended a disposable
`__TEST_ITEM_FOR_VERIFICATION__` row to the real doc, completed it through the real
`POST /api/todo/complete` route, and confirmed both that it was removed and that the other 43
real items came back byte-for-byte identical (same order, same counts) — rather than testing
destructively against real content.

Two model tiers are configured in `MODELS`: `haiku` for cheap testing, `sonnet` for real
application generation, selected via `settings.model`. Document generation is **always manually
triggered** — there is no automatic/batch generation anywhere, and the fit-rating system below
exists specifically to make that manual decision cheaper to reach (a ~$0.005 Haiku rating call
to decide whether a ~$0.05 Sonnet generation call is worth spending).

### Fit rating (2026-07-16)

For any job that comes out of `evaluate_candidates()` confirmed remote + software-relevant
*and* has a real JD (`jd_fetch_status: 'ok'`), a second, separate Claude call —
`rate_job_fit()` — scores how well the user's career docs (bio/resume, via the same
`load_career_docs()` used by generation) match the role, 1-5, using the user's own rubric
(5 = everything matches, ... 1 = not qualified), with reasoning. Deliberately a *second* call
rather than folded into `classify_job()` — a job that turns out not-remote/not-relevant gets
excluded before ever reaching this step, so it never costs a rating call. Same rating logic also
runs from `POST /api/jobs/<id>/reclassify` (the manual-paste flow) if the pasted JD comes out
remote + relevant.

`fit_rating` (int 1-5 or `None`) and `fit_reasoning` live directly on the job record.
`GET /api/jobs`'s sort now ranks by `fit_rating` (unrated jobs sort below any explicit 1-5,
since "unconfirmed" isn't the same as "confirmed poor fit") ahead of the old keyword
`match_score`, so strong matches surface first without the user needing to open each job. The
rating and its reasoning are shown on the Job detail page's preview step (`renderFitRating()`) —
by design, that's the only place it's surfaced beyond the list badge, since the whole point is
the user reviews the JD *and* the rating before ever clicking "Generate documents," which stays
a fully manual, per-job action with no rating-based gate on the button itself.

Not yet done: no retroactive rating for jobs tracked before this feature existed (currently
zero of the 641 real tracked jobs have a `fit_rating`) — only new jobs going through the
pipeline going forward get rated.

**Fixed a real data-loss bug (2026-07-20):** `rate_job_fit()`'s Claude call used `max_tokens=300`,
but Haiku consistently ignores the "1-3 sentences" instruction and writes full paragraphs
(~200-230 tokens observed in testing) — too little margin, so a response that ran even slightly
long got cut off before the closing `</reasoning>` tag ever appeared, and `extract()`'s regex
(which requires both tags to match) silently returned `''`. Real data confirmed this had already
happened on **35 of 69** rated jobs — a real rating with a genuinely empty `fit_reasoning`, not a
display bug. Fixed two ways: bumped `max_tokens` to 600 (real headroom over observed lengths),
and added a fallback in `rate_job_fit()` — if the closing tag is still missing for any reason,
grab everything after the opening `<reasoning>` tag instead of losing it outright (an
unterminated-but-real fragment beats silently discarding it). Does not retroactively fix the 35
already-affected jobs — that needs a real re-rate call per job (real Haiku cost), not something
this fix does automatically.

**`rate_job_fit()` also cleans the stored JD now (2026-08-06)** — user-requested follow-up to the
same-day capture-cleaning fix above: "there is always a bunch of other garbage in the JD that
doesn't need to be there." Since this call already reads the full JD text to produce the rating,
it now also extracts a `cleaned_description` — the substantive role content (responsibilities/
requirements/qualifications) with generic company boilerplate, benefits/perks lists, legal/EEO
notices, and repeated navigation/apply-button text stripped out. All three callers
(`evaluate_candidates()`, `_apply_classification()`, `track_job_manually()`) now run this through
a new shared `_apply_cleaned_description(job, cleaned)` — replaces `job['description']` only if
the cleaned text passes `is_real_job_description()`, otherwise leaves the existing description
untouched (a cleaning miss shouldn't destroy a perfectly good stored value). This means the
*stored* JD gets progressively cleaner going forward, not just this one rating's own internal
judgment — the same text is what later feeds document generation, where leftover garbage matters
more than it does for a rating.

Deliberately attached to `rate_job_fit()` rather than `classify_job()`: rating only ever runs for
jobs already confirmed remote + software-relevant, the same "don't pay for it on a job that gets
excluded anyway" reasoning `rate_job_fit()` already exists to satisfy — cleaning at classification
time would mean paying for it on every candidate, including ones that never reach rating at all.

`<rating>`/`<reasoning>` are requested BEFORE `<cleaned_description>` in the response format, and
`max_tokens` went from 600 to 6000 — deliberately ordered so a long JD's echo can safely run into
the token ceiling without ever threatening the rating/reasoning fields themselves (which are
small and always complete); losing the cleaned description to truncation just means falling back
to the original text, not losing the rating. The existing missing-closing-tag fallback for
`<reasoning>` (added 2026-07-20, see above) was also bounded to stop at a following
`<cleaned_description>` tag, so a truncated reasoning capture can't accidentally swallow the next
field too. Verified live with a synthetic noisy JD (cookie/privacy/social-media/copyright/EEO
boilerplate wrapped around a real posting): rating and reasoning parsed correctly, and the
cleaned description contained zero trace of the surrounding noise while preserving all the real
content (About, Responsibilities, Requirements, Benefits).

### Adzuna search

`GET /api/search` queries Adzuna with `remote_only=1`, `salary_min=100000`, `full_time=1`, and a
configurable `max_days_old`; results are additionally filtered so only jobs with the query
phrase in the title survive. `POST /api/jobs/add-from-search` adds a result to the tracked list
— it follows the Adzuna redirect URL to resolve the real ATS URL before saving, and always sets
`is_remote: True` since results are pre-filtered remote-only. Note the redirect resolution often
doesn't get past Adzuna's own landing page (`adzuna.com/details/...`) — some listings resolve to
the real ATS URL, others don't; this is an Adzuna-side limitation, not a bug.

**Classifies + rates like every other job (2026-07-20)**: fixed a real gap — this route used to
save a bare job record with no `remote_status`/`software_status`/`fit_rating` at all, meaning a
manually-added job never got the same treatment anything else in the system gets. Now attempts
`fetch_job_description()` right after building the record; on success, classifies and applies
via the shared `_apply_classification()` (passing `jd_fetch_status='ok'` since the fetch was
automatic, not pasted — cosmetic only, every consumer treats `'ok'`/`'manual'` identically) — on
failure, saves with `jd_fetch_status: 'failed'`/`evaluated_at: None`, the same shape any other
unfetchable JD gets, landing in Found and eligible for a manual JD paste later. No
`title_keywords`/`title_exclude_keywords` gating, same as every other manual-add route (this is
a deliberate action, not a filtered sweep).

`_adzuna_request()` / `_adzuna_credentials()` / `_resolve_redirect_url()` are the shared helpers
behind both the manual search route and `sweep_adzuna_jobs()` below — don't reintroduce
duplicate inline Adzuna-calling code.

### Adzuna sweep

`sweep_adzuna_jobs()` pulls everything posted since the last *successful* sweep, across every
configured `title_keywords` phrase, gathers raw candidates, and hands them to
`evaluate_candidates()` (see above) rather than filtering/deduping inline.

Mechanics worth knowing before touching this function:
- `last_adzuna_sweep` (top-level in `job_data.json`) only advances when a sweep completes with
  zero errors — a failed sweep is retried from the same starting point next click. This is safe
  because adding is idempotent (URL-based dedup against already-tracked jobs).
- Adzuna's `max_days_old` filter is day-granularity only, so the sweep over-fetches by day and
  then does exact filtering client-side against `created` timestamps
  (`_parse_adzuna_created`), stopping pagination early once a page's results cross the
  `since` boundary (results are `sort_by=date` descending).
- First-ever sweep (no `last_adzuna_sweep` yet) defaults to a 24h lookback
  (`SWEEP_DEFAULT_LOOKBACK_HOURS`).
- `last_adzuna_sweep_result` is saved on every attempt (success or failure) so callers always
  have something to show — distinct from `last_adzuna_sweep`, which is success-only.
- **Retries once on failure (2026-07-22)**: a real pattern — Adzuna's own API returned a `503
  Service Temporarily Unavailable` during the scheduled 06:00 run two mornings running, and
  since a failed sweep only retries from the same point at the *next scheduled run*, that meant
  losing a full day's worth of Adzuna results per occurrence, confirmed the API was healthy again
  within the same session both times. Each per-page request now waits
  `ADZUNA_RETRY_WAIT_SECONDS` (5 minutes) and retries once before recording the error — cheap
  when Adzuna's healthy (zero cost, never triggered), meaningful when it's a brief blip. This is
  sweep-only, deliberately not added to the shared `_adzuna_request()` helper itself — the manual
  Search tab's interactive route uses that same helper and should fail fast, not hang the UI for
  5 minutes.

### Jobicy sweep (2026-08-03)

Added after the user's friend sent a job at a small company (Skylight, on Workable) that none of
this app's existing sources had any visibility into — prompted a look at what other keyword-
searchable job APIs exist, tested live rather than assumed from docs. Findings, all verified by
actually calling each API, not just reading about it:
- **Jobicy** (`https://jobicy.com/api/v2/remote-jobs`) — public, no auth, and `tag=<phrase>`
  performs genuine server-side filtering (confirmed: a `tag=product manager` query returns
  actual PM roles, not the unfiltered default feed).
- **Remotive** and **Arbeitnow** were tested and rejected: their `search=`/`category=`/`tags=`
  params are silently ignored — every query returns the same fixed default feed regardless of
  the parameter's value. Not worth building against something whose advertised filtering doesn't
  actually filter.
- **RemoteOK** was tested and rejected: every job in the feed carries one identical, ~50-term
  boilerplate tag list (not real per-job tags), so filtering by tag is meaningless there, and the
  untagged feed itself skews toward non-tech roles (BD reps, field technicians) rather than a
  software-jobs board.

**Jobicy's own job detail pages are behind Cloudflare** — a plain fetch 403s every time (verified
live), so `fetch_job_description()`'s normal per-job fetch will always fail for this source. This
doesn't block classification because the listing API response already includes the full
`jobDescription` HTML inline per job. `_fetch_jobicy_jobs(tag)` strips that to text and stashes it
in each raw candidate's `_fallback_jd_text` field — the same mechanism `evaluate_candidates()`
already had for Adzuna's own unscrapeable-real-destination case (that field was generalized from
`_adzuna_fallback_text` to the source-agnostic `_fallback_jd_text` this same day so Jobicy could
reuse it rather than duplicating the fallback logic). Verified end-to-end against `--dev`: for a
job whose real page 403'd, `jd_fetch_status` still came back `'ok'` with the fallback text,
`remote_status`/`software_status`/`fit_rating` all populated correctly — classification/rating
never distinguishes "fetched the real page" from "used the fallback text," by design.

`sweep_jobicy_jobs()` (job_checker.py) mirrors `sweep_remote_com_jobs()`'s shape: no time-window
cursor (Jobicy's API doesn't offer one), one API call per configured `title_keywords` phrase,
raw candidates deduped by URL within the run, then handed to the shared `evaluate_candidates()`
funnel like every other source — no source-specific shortcut. `JOBICY_COUNT_PER_QUERY` (50) caps
results per keyword per run; confirmed live the API honors at least `count=100`, 50 was chosen as
comfortably above what one keyword phrase actually returns day to day. Saves
`last_jobicy_sweep`/`last_jobicy_sweep_result`, same pattern as the Adzuna/remote.com sweeps.

First real run against `--dev` data: 50 candidates seen for the single configured keyword
("product manager"), 24 added, 26 excluded (4 `title_excluded`, 22 `title_mismatch`), 0 errors —
the two spot-checked non-excluded jobs both correctly used the Cloudflare-fallback path and came
back `remote`/`relevant` with a real `fit_rating`.

Recruitee, SmartRecruiters, and BambooHR were also found to have public job-listing APIs during
this same research (BambooHR's isn't officially documented/stable, the other two are), but none
were built speculatively since there's no company currently tracked on any of them to verify a
scraper against — same reasoning documented under "Chrome extension"/elsewhere in this file:
don't ship code with nothing real to exercise it against. Workable was also confirmed to have a
public per-company API (`https://apply.workable.com/api/v1/widget/accounts/{slug}`, verified live
against the friend-shared Skylight Frame board) as a **direct company-scraper** candidate (like
Greenhouse/Lever/Ashby) rather than a keyword-sweep source — deliberately **not built**: the user
explicitly said individual-company scraping isn't a priority right now, aggregator sources are
(2026-08-03) — this stays as a documented, not-yet-built option if that changes.

### Himalayas sweep (2026-08-03, same day as Jobicy)

Built immediately after Jobicy per the user's explicit direction to keep pursuing aggregator APIs
rather than individual-company scrapers. `https://himalayas.app/jobs/api/search` — public, no
auth, `q=` performs genuine free-text search and `country=` genuinely filters (verified live:
542 real "product manager" matches, spanning company sizes from Kickstarter down to much smaller
employers — a different mix than Adzuna already surfaces well, which is the actual goal here:
broader small/mid-size-company coverage, not just another feed of the same large employers).
Same Cloudflare situation as Jobicy — the job's own page 403s on a plain fetch (confirmed live),
but the search API's own JSON already includes the full `description` HTML per job, so this reuses
the same generic `_fallback_jd_text` mechanism, no new plumbing needed.

`_fetch_himalayas_jobs(query)` / `sweep_himalayas_jobs()` (job_checker.py) mirror
`sweep_jobicy_jobs()`'s shape exactly: no time-window cursor, one paginated API call (capped at
`HIMALAYAS_MAX_PAGES` = 3, 20 results/page) per configured `title_keywords` phrase, `sort=recent`
so pagination naturally favors newest postings, `country=US` matching this app's US-remote focus,
raw candidates deduped by URL within the run, then handed to the shared `evaluate_candidates()`
funnel like every other source. In practice `sort=recent` returns far fewer than the full
`totalCount` per page (confirmed live: 7-9 results for a query with a `totalCount` in the
hundreds) — that's a feature, not a bug, for a sweep that only cares about "what's new since
last time," not the full historical backlog.

First real run against `--dev` data: 9 candidates seen, 8 added, 1 excluded (`not_software`), 0
errors — spot-checked all 8 added records: real, distinct mid-size companies (Newsela,
PointClickCare, Snapsheet, Crypto.com, Common App, Compassion International, RF-SMART, Acxiom),
all correctly used the Cloudflare-fallback path (`jd_fetch_status: 'ok'` off `_fallback_jd_text`,
not a real page fetch), and confirmed-remote ones got real `fit_rating`s.

**Other aggregators found during this research but not built, because they need the user's own
signup (a free API key), not because of any technical problem**: **Jooble**
(jooble.org/api/about — instant free key, no credit card) and **Careerjet**
(careerjet.com/partners/api — free tier, 1000 req/hr) both look like real, functioning, keyword-
searchable aggregators per their docs, but neither can be verified live without a personal key
tied to the user's own email, unlike Jobicy/Himalayas which needed no key at all. If the user
wants either of these added, they'd need to sign up first (same one-time step as the existing
`ADZUNA_APP_ID`/`ADZUNA_APP_KEY` setup in `.env`) — worth revisiting if aggregator coverage still
feels thin after Jobicy + Himalayas run for a while.

### Workable cross-company sweep (2026-08-05)

Grew directly out of the Skylight investigation: a friend-shared Workable-hosted job that none of
Adzuna/Jobicy/Himalayas had any path to, since none of them crawl arbitrary company career pages
and Skylight never separately posted to Jobicy/Himalayas. Rather than building a per-company
`scrape_workable()` (explicitly deprioritized — see "individual companies don't seem like a good
use of time," 2026-08-03), digging into what happens when a candidate searches Workable directly
turned up `https://jobs.workable.com/search` — Workable's own candidate-facing search across
*every* company on the platform, not the per-company widget API investigated earlier for Skylight
itself (`apply.workable.com/api/v1/widget/accounts/{slug}`, which only lists one known company).

The search page's own network calls reveal a clean underlying JSON API:
`GET https://jobs.workable.com/api/v1/jobs?query=...&location=United+States&workplace=remote&employment_type=full_time`.
Confirmed live: real cross-company results (228 for a plain "product manager" query), working
pagination via `nextPageToken`, and — unlike Jobicy/Himalayas — each result already includes the
**full description, requirementsSection, and benefitsSection as complete HTML**, so this source
needs no `_fallback_jd_text` fallback mechanism at all.

**Robots.txt consideration, resolved before building**: `jobs.workable.com/robots.txt` disallows
the `/search` page's query-string paths, which on first read looked like a "don't automate this"
signal and nearly stopped this from being built. Closer read changed that: the disallow is
standard SEO hygiene (keeps a search engine from indexing infinite `?query=x&location=y`
permutations as thin pages), not a data-access restriction — the same robots.txt's
`Content-Signal: search=yes, ai-input=yes, ai-train=no` header explicitly names "search" and
"ai-input" use as fine (only training a model on the content is excluded, which isn't what this
is). Weighed against this app's own existing precedent — it already scrapes `remote.com` with no
explicit permission at all, and works around `www.adzuna.com`'s bot protection with delays rather
than avoiding it — a low-frequency nightly sweep plus occasional manual runs for one person's own
job search was judged squarely inside the same "careful, not a bulk-indexing bot" stance already
applied elsewhere, not the harder case actual access restrictions (Jobicy/Himalayas/Cloudflare)
represent.

**Real time-window cursor, unlike Jobicy/Himalayas** — user-specified: only pull jobs posted
since the last successful run, first run capped to the last 24 hours, explicitly *not* a bulk
backfill ("I don't want to pull in 1000 new jobs, just 10 from the last day"). Mirrors
`sweep_adzuna_jobs()`'s cursor shape (`last_workable_sweep`, advanced only on zero errors) but
without Adzuna's backfill-lag buffer (`SWEEP_LOOKBACK_BUFFER_HOURS`) — no evidence this source
has similar indexing lag, so that complexity wasn't added speculatively. No confirmed working
server-side date-filter or date-sort query param (tried several plausible names live — `sortBy`,
`sort`, `postedAfter`, `createdAfter`, `since`, `publishedAfter` — none changed the result count
or order), so `_fetch_workable_jobs(query, since_dt)` filters by each result's `created` timestamp
client-side instead, same "over-fetch and filter exactly" approach `sweep_adzuna_jobs()` already
uses for its own source's quirks. `WORKABLE_MAX_PAGES` (5, 20 results/page) bounds how deep a
sweep will page before giving up — this source is meant to catch a handful of genuinely new
postings a day, not pull hundreds just to filter almost all of them out by date.

First real run against `--dev` data: 3 candidates seen (last 24h, "product manager"), 2 added
(ArcSite, LawnStarter — both real, correctly classified `remote`/`relevant` with real
`fit_rating`s, full JD text straight from the API with no fetch needed), 1 excluded
(`duplicate_location`), 0 errors. A second run moments later correctly found 0 new candidates,
confirming the cursor advances and the sweep is genuinely incremental rather than re-pulling the
same window every time.

### Get New Jobs — the unified orchestrator

`get_new_jobs()` (triggered via `POST /api/get-new-jobs`, the single "Get New Jobs" button in
the header) runs `check_all_companies()`, then `sweep_adzuna_jobs()`, then
`sweep_remote_com_jobs()`, then `sweep_jobicy_jobs()`, then `sweep_himalayas_jobs()` (both added
2026-08-03), then `sweep_workable_jobs()` (added 2026-08-05) **sequentially** — not a single
merged batch. This is deliberate: each source saves before the next runs, so later sources' own
dedup pass (inside `evaluate_candidates()`) naturally sees earlier ones and won't double-add a job
that showed up in more than one place. Their six result dicts get summed/merged into one report,
saved as `data['last_get_jobs_at']` / `data['last_get_jobs_result']`.

This supersedes the old separate "Check Now" and "Sweep Now" buttons/routes (`/api/check` and
`/api/sweep-adzuna` were removed — nothing else called them). The CLI `--check` flag still calls
`check_all_companies()` directly (company boards only, not the Adzuna/remote.com sweeps) — kept
as a narrower option, separate from the scheduled path below.

Expect a "Get New Jobs" run to take a while — full JD fetch + a Haiku classification call per
new candidate that survives the cheap filters, sequential (no concurrency), plus a deliberate
`ADZUNA_SCRAPE_DELAY_SECONDS` (10s) pause between each Adzuna candidate's real-URL resolution to
avoid tripping `www.adzuna.com`'s bot protection (PROBLEMS.md #5). This is expected and fine —
see scheduling below, it's designed to run unattended overnight, not to be fast.

**30-day job retention (2026-07-20)**: the last step, after all three sweeps save, is
`cleanup_old_jobs(data)` — removes `data['jobs']` entries older than `JOB_RETENTION_DAYS` (30, by
`last_seen`, falling back to `first_seen`/`created_at`; a record with no parseable date at all is
kept, never deleted on missing/unparseable data). The removed count is reported as
`jobs_cleaned_up` in the combined result. This **only ever touches the JSON job list** — nothing
under the local `applications/` cache or Drive is ever removed by this or anything else; generated application
documents are the actual permanent record of what was applied to, `data['jobs']` is a rolling
working-set window. Company-scraped jobs already get implicitly pruned once they vanish from a
scrape (`check_all_companies()` rebuilds `data['jobs']` from that pass's results plus preserved
non-company jobs) — this mechanism is really about Adzuna/remote.com/ad-hoc/search-added jobs,
which had no cleanup at all before this. No conflict with `RECENT_REPOST_DAYS` (also 30) — that
check only ever looks back 30 days too, so anything old enough to purge is already outside its
window regardless.

### Scheduling (launchd)

**As of 2026-07-16, this runs on a schedule** — a reversal of the earlier "manual trigger only"
decision (see project memory for the history; the user explicitly asked for this after seeing
`www.adzuna.com` rate-limit interactive runs). A macOS LaunchAgent
(`~/Library/LaunchAgents/com.goatsearch.getnewjobs.plist`) runs
`venv/bin/python job_checker.py --get-new-jobs` daily at **06:00** against the **real** data file
(`job_data.json` — no `--dev`), logging to `logs/get_new_jobs.log` (gitignored). The intent is
for the user to have fresh, classified results ready to review starting around 7am.

**Originally scheduled for 00:15**, moved to 06:00 the same day after discovering (from the
first real overnight run) that Adzuna's own search index has backfill lag — a listing's
`created` timestamp reflects when the employer posted it, not when Adzuna's crawler indexed it,
so a query run too soon after "now" undercounts and (worse) permanently misses late-indexed
listings once the sweep's cursor advances past them. See `SWEEP_LOOKBACK_BUFFER_HOURS` in
`sweep_adzuna_jobs()` (currently 6h) — every sweep, scheduled or manual, asks about
`[since last sweep, now - 6h]`, never right up to "now", and the cursor advances to that
buffered boundary rather than wall-clock time. Moving the schedule to 06:00 means the buffered
window naturally lines up with "all of yesterday" by the time it runs. **Tradeoff worth
knowing:** this leaves much less runway before the user's ~7am check-in than the original 00:15
slot did (1 hour vs. ~7) — a busy night could plausibly still be running post-classification
work at 7am. Not yet load-tested against real full-scale nightly volume; revisit if it becomes a
problem. See `PROBLEMS.md` #6.

**Critical dependency: the Mac must be awake at 06:00.** `launchd`'s `StartCalendarInterval`
does not wake a sleeping machine — it just silently fails to fire, with no error anywhere (this
happened for real on the first morning after the move to 06:00: `launchctl print` showed `runs =
0`, root-caused via `pmset -g log` showing the Mac had actually woken at 10:27 AM). Fixed with a
system-level wake schedule: `sudo pmset repeat wake MTWRFSU 05:55:00` (confirm with
`pmset -g sched` — should show "wake at 5:55AM every day"). This has to be run by the user in a
real terminal, not through a Claude Code session — there's no TTY here for sudo's password
prompt. If the scheduled run ever appears to silently not happen again, check `pmset -g sched`
and `pmset -g log | grep -i wake` before assuming it's a code problem.

Since this now runs as a fresh OS process every night (not the long-lived Flask server), the
in-process `_get_jobs_lock`/`_get_jobs_running` guard inside `get_new_jobs()` can't prevent two
scheduled runs from overlapping if one is unexpectedly still going. A separate cross-process
lock (`GET_NEW_JOBS_LOCKFILE` — a PID file at `.get_new_jobs.lock`, gitignored) guards the
`--get-new-jobs` CLI entry point specifically: `_acquire_get_new_jobs_lock()` /
`_release_get_new_jobs_lock()` check whether the PID in an existing lockfile is still alive
before proceeding, so an overrunning previous night's job causes the next trigger to skip rather
than run concurrently. If you touch `--get-new-jobs`, keep this lock — it's the direct lesson
from the 2026-07-15 near-incident (background thread outliving a `kill` during manual testing).

Managing the schedule:
```bash
launchctl print gui/$(id -u)/com.goatsearch.getnewjobs   # inspect / confirm next run
launchctl bootout gui/$(id -u)/com.goatsearch.getnewjobs # unload (stop scheduling)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.goatsearch.getnewjobs.plist  # (re)load
```
Do not run `launchctl start com.goatsearch.getnewjobs` casually — that fires an immediate real
run against production data, same as if it were midnight.

The in-process `apscheduler` code referenced by `/api/schedule` is unrelated dead code (imported
but `_scheduler` is never instantiated) — a leftover from an earlier, separate, abandoned
approach to scheduling the company check specifically. Don't confuse it with the launchd job
above, and don't "fix" it into working — the launchd approach is the one actually in use.

### Frontend

`index.html` is a left-sidebar app with 4 functional areas (Jobs / Applications / Interviews /
Settings — 2026-07-22 restructuring, see "Applications area" below), driven by a minimal
hash router: `parseRoute()` / `navigate(path)` / `renderRoute()`, hooked to the `hashchange`
event. Routes: `#/jobs`, `#/jobs/found|screened|excluded`, `#/jobs/job/<id>`, `#/applications`,
`#/applications/new`, `#/applications/<id>`, `#/interviews`, `#/settings`,
`#/settings/companies|keywords|search|general`. This makes job/application detail pages real,
deep-linkable, back/forward-safe URLs — not just another `classList` toggle — which is what
"a new page" (vs. the old modal) actually requires. Sidebar nav items (`.nav-item`) and page
containers (`.page`) get their active state set by `renderRoute()`; within an area, the existing
`.subtab`/`.subtab-content` mechanism (`switchJobsSubTab()`, `switchSettingsSubTab()` — each
scoped to its own area with a `#page-jobs .subtab` / `#page-settings .subtab` selector so the two
don't stomp each other's active state, since both reuse the same `.subtab` class) still drives
sub-navigation exactly as before (Found/Screened/Excluded under Jobs; Companies/Keywords/Search/
general Settings under Settings — folded in "for now" per the user, previously their own
top-level tabs). Settings' general sub-view is what used to be the whole Settings tab (Agent
Instructions/Resume Template/Career Reference/To Do gdoc fields, model radio, remote.com URLs,
accepted_locations) — unchanged content, just relocated.

The "Get New Jobs" button lives in a slim header bar above the routed page content
(`triggerGetNewJobs`); polling (`startPolling` / `pollGetJobsUntilDone`) drives the "in progress"
banner while the background thread runs server-side. The Jobs area's home page (Found sub-tab's
container) has a status card at the top (`renderGetJobsStatus`) showing the last run's summary and
a "View excluded (N)" toggle that expands `renderExcludedPanel` — a review list of every excluded
candidate (from either source) with its reason and, for the classification-based reasons
(`not_remote`/`not_software`), Claude's reasoning text. `EXCLUDE_REASON_LABELS` /
`NON_ACTIONABLE_REASONS` control the panel's labels and which reasons get an override button —
split by reason kind (2026-07-20, see "Sonnet re-evaluation" below): `not_remote`/`not_software`
get **Re-evaluate** (`POST /api/jobs/<id>/reevaluate`); `title_mismatch`/`title_excluded` keep
**"+ Add anyway"** (`addSearchJob()` / `POST /api/jobs/add-from-search`) since those are plain
keyword matching, not a model verdict there's anything to re-check. `add-from-search` accepts
either `company_name` (unified candidate shape) or `company` (manual search's older shape) for
the company field, and an optional `source` (defaults to `'adzuna'` for backward compat with
manual search).

Job filtering (new-only, remote-only, keyword match) happens client-side in `renderJobs()`, not
via query params, except where `GET /api/jobs` itself supports `company` / `new_only` /
`matching_only`. The apply flow is now a real page (`loadJobDetail()` → `startGeneration()`,
formerly a modal — see "Applications area" below) that previews the job description and
career-doc word counts before spending an API call.

### Found / Screened / Excluded sub-tabs (2026-07-16, Excluded added 2026-07-17)

The Jobs area is split into three sub-tabs (`.subtab`/`.subtab-content`/`switchJobsSubTab()`,
scoped to `#page-jobs` — see "Frontend" above), **Screened as the default/home sub-tab** since
2026-07-22's area restructuring (previously Found). `renderJobs()` computes all three lists
from `state.jobs`, first excluding anything with a matching entry in `state.applications` (see
"Applications area" below — a job that already has generated documents has "moved" to
Applications and no longer shows in any Jobs sub-tab):
- **Found**: `job.evaluated_at == null` (JS loose equality — deliberately also catches
  `undefined` for jobs from before these fields existed, not just explicit `null`). Covers both
  "still needs a JD pasted" and the rare case where a JD exists but an earlier automatic
  classify attempt failed transiently. Rendered via the shared `renderJobList(containerId, jobs, opts)`.
- **Screened**: `evaluated_at != null && remote_status === 'remote' && software_status === 'relevant'`.
  Also via `renderJobList()`.
- **Excluded**: `job.exclusion_reason` is set (one of `title_mismatch`, `title_excluded`,
  `not_remote`, `not_software` — the judgment-call rejections; pure dedup reasons are never
  persisted, see `evaluate_candidates()` above). Rendered via a dedicated `renderExcludedJobsList()`,
  **not** `renderJobList()` — deliberately sourced straight from `state.jobs`, bypassing the
  `is_remote`/`match_score > 0` gates the other two tabs apply, since an excluded job is
  frequently a non-match on one of those by definition (e.g. `title_mismatch` always fails
  `match_score`) and would otherwise silently vanish from the one tab meant to review exactly
  that. Has its own reason filter (`#excluded-reason-filter`).

**Classification is still fully automatic** — this was the key scope correction from the
original ask (see project memory): jobs move Found → Screened on their own via the existing
`evaluate_candidates()` pipeline or the existing paste-and-reclassify flow
(`POST /api/jobs/<id>/reclassify`), neither of which changed. A job only sits on Found because
it doesn't have a usable JD yet.

Small pieces supporting the workflow:
- **Reject** (`DELETE /api/jobs/<job_id>`, `rejectJob()`) — removes a job outright. Available on
  Found and Excluded cards (`btn-reject` class, distinct from `btn-dismiss` so `dismissJob()`'s
  `querySelector` doesn't grab the wrong button when both exist on the same card).
- **Screen retry** (`screen_found_jobs()` in `job_checker.py`, `POST /api/jobs/screen`,
  `triggerScreen()`) — a narrow safety net, not the primary classification path: finds jobs
  with a real JD (`jd_fetch_status` `'ok'`/`'manual'`) that never got `evaluated_at` set (a
  prior automatic attempt failed transiently), retries via the shared `_apply_classification()`
  helper (see Chrome extension section below). Passing jobs move to Screened; failing jobs are
  **kept** with `exclusion_reason` set (moved to Excluded, not deleted) — this changed 2026-07-17;
  it used to delete them, which is exactly what made rejection patterns invisible before the
  Excluded tab existed.
- **Restore to Found** (`POST /api/jobs/<id>/restore`, `restoreJob()`) — Excluded-tab-only, now
  scoped to `title_mismatch`/`title_excluded` only (2026-07-20; see "Sonnet re-evaluation" below
  for the other two reasons). Clears `exclusion_reason`/`exclusion_reasoning`/`evaluated_at`,
  putting the job back through the normal Found flow so it can get a second look via Screen
  retry, rather than being final forever.

`jd_fetch_status` is treated as "needs a JD" whenever it's anything other than `'ok'` or
`'manual'` (not just the literal `'failed'` string) — this deliberately also covers jobs from
before this field existed, where it's simply absent, so legacy jobs correctly show the "needs
JD" badge instead of silently sitting in an ambiguous state.

**"Posted N days ago" (2026-07-21)**: Found/Screened/Excluded cards show `postedInfo(job)`
instead of a bare tracked-date — real "days since posted" when `job.posted` is available,
falling back to a clearly-labeled `Tracked {date}` (not a bare date, to avoid it being misread
as a posted date) when it isn't. `job.posted` availability varies by source: Adzuna and
remote.com always had it; Greenhouse (`updated_at` — closest available signal, Greenhouse
doesn't expose a true first-posted date) and Lever (`createdAt`, epoch ms) were backfilled the
same day once found to be sitting unused in API responses the scrapers already fetch. Ashby's
`publishedAt` was added as a best-guess field name — unverified, since none of the currently
tracked Ashby companies had live public postings to check against — it degrades to the same
`Tracked` fallback if wrong, so this was worth adding speculatively rather than blocking on
confirmation. `scrape_generic()`, ad-hoc, and manually-added jobs still have no posted date at
all and always show the fallback.

**`title_mismatch` no longer persisted (2026-07-30)**: unlike every other exclusion reason,
`title_mismatch` ("title doesn't contain any of your `title_keywords` phrases") is "everything
that isn't what I'm looking for" — in practice the overwhelming majority of what any job board
returns, not a judgment call worth reviewing the way `not_remote`/`not_software` are. It had
flooded the Excluded tab: of 2,582 real tracked job records at the time this was found, 1,733
(67%) were `title_mismatch`. `evaluate_candidates()`'s title-keyword pre-filter (step 1, see
above) still excludes these candidates from `to_save` — meaning `data['jobs']` — but no longer
calls `_build_excluded_job()` for this specific reason; they're still counted in the transient
per-run `excluded` list (so "N excluded this run" / the "View excluded" panel's breakdown stay
accurate), just never written to disk. `title_excluded` (a title containing an explicitly
configured exclude term like "staff"/"principal"/"director") is unaffected — that's a narrower,
deliberate exclusion still worth being able to review and restore. The Excluded tab's reason
filter dropdown lost its "Title mismatch" option accordingly (no persisted records will ever
have that reason again); `EXCLUDE_REASON_LABELS`'s `title_mismatch` entry stays, since the
transient per-run report still uses it. The 1,733 already-persisted `title_mismatch` records
were removed from the real `job_data.json` in the same pass (user-specified: clean up existing
clutter, not just stop it from growing) — verified before restarting production that every
other job record and every other top-level key were byte-for-byte unchanged, and a full backup
was taken first (`logs/backups/job_data_before_title_mismatch_cleanup_*.json`).

**14-day display-only age filter on the Jobs tab (2026-07-30, made configurable same day)**:
distinct from `cleanup_old_jobs()`'s 30-day *server-side, data-deleting* retention (see "Get New
Jobs" below) — this is purely a client-side display filter in `renderJobs()`, defaulting to 14
but reading `settings.job_age_limit_days` (Settings → General → "Job Age Limit", a plain number
input; `saveJobAgeLimitSetting()` also calls `renderJobs()` immediately so the change is visible
without navigating away and back). Nothing is removed from `state.jobs`/`job_data.json`; a job
older than the configured limit simply stops rendering in
Found/Screened/Excluded. This distinction matters because `evaluate_candidates()`'s URL/
recent-repost dedup checks (see "Ingestion" above) scan the *full* `data['jobs']` list regardless
of what the Jobs tab happens to be showing — if this had instead deleted old records the way
`cleanup_old_jobs()` does, a re-scraped duplicate of a hidden job would stop deduping correctly
and would reappear as if new (the user explicitly called this out as a requirement, not just an
implementation detail). Age is computed via the same `effectivePostedDate()` already shown on
every card as "Posted/Tracked N days ago" — hiding a job here always matches what the user would
get from doing the math on the card themselves, rather than introducing a second, inconsistent
notion of "how old is this." A job with no parseable date at all is never hidden, same safety
rule `cleanup_old_jobs()` already uses. Applied since jobs that have already moved to
Applications/Interviews are excluded from the Jobs tab entirely regardless of age (via
`appliedJobIds`, see above) — no special-casing needed to "keep those regardless of the 14-day
cutoff," since they were never subject to it in the first place. Checked against real data before
shipping: only 40 of 849 tracked jobs were older than 14 days.

### Applications area (2026-07-22)

Restructured the app around the actual phases of a job search — Jobs / Applications /
Interviews (a stub for now) / Settings — as left-sidebar functional areas instead of a flat set
of top tabs, matching where a job actually is in its lifecycle. Clicking a job used to open the
Apply modal; it now opens a real page (`#/jobs/job/<id>`, breadcrumb "Jobs > Company, Title")
with the exact same pre-generation flow (fetch → preview/JD/doc-health/fit-rating → generate).
Once "Generate Documents" succeeds, the job **moves out of Jobs entirely** (excluded from Found/
Screened/Excluded — see above) and becomes an **Application**, viewable at
`#/applications/<id>` (breadcrumb "Applications > Company, Title") — the exact same
post-generation content the modal's "result" view used to show, just no longer only reachable
right after generating; it's now a real, permanently revisitable page.

**Applications are sourced from disk, not from `data['jobs']`** — this is the one genuinely
non-obvious design decision here. `cleanup_old_jobs()` purges job records after
`JOB_RETENTION_DAYS` (30 days), but generated documents are never deleted (see "Get New Jobs" 30-
day retention above: "generated application documents are the actual permanent record of what
was applied to"). Sourcing the Applications list from `data['jobs']` instead would have silently
dropped an application from view the moment its underlying job record aged out, even though the
real generated docs are still sitting on disk. New `list_applications()` (job_checker.py) walks
`BASE_DIR/applications/<Company>/<Title>/<Date>/` directly (plus the legacy Drive-synced
`OUTPUT_FOLDER` root if set — real, substantial application history from before 2026-07-21's
local-path fix lives only there, and would otherwise be invisible in this new list entirely;
same fallback pattern `_find_existing_jd_file()`/`api_existing_documents()` already use). One
entry per (Company, Title) — always the latest dated folder (matching how regeneration already
works: in place same-day, new dated folder on a new day); if both roots have an entry for the
same (Company, Title), the newer-dated one wins. Each entry's `id` is
`hashlib.md5(f'{_safe_name(company)}|{_safe_name(title)}'.encode()).hexdigest()` (`_application_id()`)
— stable across regeneration and independent of `data['jobs']` entirely, which is what lets an
application survive its job record being purged. Best-effort joins back to a live `data['jobs']`
record (matching `_safe_name()`'d company/title against the folder names) for `job_id`/`url`/
`is_applied`/`fit_rating` — these come back `null`/empty once the underlying job record is gone,
which real production data already confirms happens (several pre-2026-07-15 applications in the
legacy folder have no matching job record at all). Routes: `GET /api/applications` (the list,
sorted `generated_at` descending) and `GET /api/applications/<app_id>` (list entry + full
`sections.json` content, same shape `api_existing_documents()` already returns — both now share
`_find_latest_dated_folder()` and `_load_application_content()` rather than duplicating that
lookup logic).

`api_generate_application()` and `api_generate_adhoc()` both now include `application_id` in
their response (via the same `_application_id()` helper) so the frontend can navigate straight
to `#/applications/<id>` right after a successful generate, without an extra round trip.
Regenerating from an existing application (`btn-regenerate`, shown only when the entry still has
a live `job_id`) just calls `navigate('jobs/job/' + job_id)` — re-entering the same pre-generation
flow rather than duplicating it on the application page — and lands back on
`#/applications/<id>` again once it completes, same as a fresh generation. If the job record has
been purged, there's nothing to regenerate against and the button is hidden — a known,
accepted MVP limitation, not solved by this pass.

The old Apply modal's "+ Apply" ad-hoc entry point (paste a JD with no tracked job) moved to the
Applications area itself as "+ New Application" (`#/applications/new`) — conceptually cleaner,
since an ad-hoc apply never went through a tracked Job in the first place, it just directly
creates an Application.

**Fit rating carried onto the Application detail page (2026-07-23)**: `fit_rating`/`fit_reasoning`
were only ever shown pre-generation (Job detail page's preview step); once a job became an
Application, that context was gone from the UI even though it's still useful when reviewing the
application later. `list_applications()` now includes `fit_reasoning` alongside the `fit_rating`
it already had (both best-effort from the matched `data['jobs']` record, same as everything else
in that function). Frontend: `renderFitRating(job)` was generalized into
`renderFitRatingInto(wrapId, elId, rating, reasoning)` so both the Job detail page
(`preview-fit-rating-wrap`) and the Application detail page (`application-fit-rating-wrap`) share
one render function instead of duplicating it.

**Job/Application detail pages de-boxed (2026-07-23)**: both still had the `.card` styling
(background/border/padding) left over from when this content was the Apply modal's body — after
the 2026-07-22 restructuring into real pages, that made them look like a leftover dialog box
floating on the page instead of a page in their own right. Removed `.card` from `#view-preview`,
`#view-error`'s inner wrapper, and `#view-result` — content now flows directly in `.main`'s own
padding, same as every other area (Applications list, Settings, etc.). Purely cosmetic; no
behavior changed.

**Doc health panel shows real Google Doc titles, not filenames (2026-07-23)**: the "Input docs"
rows on the Job detail page used to be keyed off whatever `load_career_docs()` happened to name
its dict entries (`bio.md`/`instructions.md`/`resume.md`) with a label lookup that never actually
matched (`{instructions: 'Instructions', bio: 'Reference doc', ...}` — keyed without `.md`, so it
always silently fell through to the raw filename) — which is what surfaced `.md` suffixes in the
UI and prompted the "are we converting these to markdown?" question (answer: no, `.md` was just
an internal dict-key convention, never a real format). Replaced with `CAREER_DOC_FIELDS = {'bio':
'Bio', 'instructions': 'Agent Instructions', 'resume': 'Resume Template'}` (matching the Settings
tab's own field names) and a new `_career_doc_metadata(settings)` that fetches each field's real
linked Google Doc title via Drive API metadata (`name`, alongside `modifiedTime` — same call,
same OAuth/soft-fail pattern as the rest of this section), falling back to
`{field}.md (local file)` + filesystem mtime when no Google Doc is configured for that field.
Notably fetches this for **all three** fields including `resume` — `load_career_docs()` itself
deliberately skips fetching resume's *content* from Google Docs (it's edited via OAuth for the
live-template-fill feature, not pulled in as career context — see that function's docstring), but
the doc is still linked in settings and its title/freshness is still worth showing here; this is
a separate, purpose-built lookup from `load_career_docs()`, not a change to what that function
loads or what reaches Claude. `api_preview_application()`'s response `docs` field is now keyed by
plain field name (`bio`/`instructions`/`resume`, not `bio.md`) with `{label, title, last_updated}`
per field.

**Word-count health dot removed (2026-07-23, same day)**: the color-coded status dot (green/
yellow/red by word count) never meant anything for Resume Template — its content is never sent
to Claude, so length is irrelevant, and the word count driving it came from the local
`career_docs/resume.md` stub file, not even the real Drive template. Confirmed as a pure relic
(surfaced by the user asking why Resume Template showed yellow) and dropped entirely rather than
special-cased, since "last updated" already covers what the dot was trying to signal for
Bio/Instructions too. `word_count` is no longer computed or returned by `api_preview_application()`
at all — `load_career_docs()` isn't even called there anymore, since nothing left needs its
content, only `_career_doc_metadata()`'s title/timestamp lookup.

**Real bug, found 2026-07-27: expired OAuth was silently indistinguishable from "no Google Doc
configured."** All three fields fell back to `_career_doc_metadata()`'s local-file mtime path
identically whether the user genuinely hadn't linked a Google Doc for that field, or had one
linked but the Drive fetch failed (see `GoogleAuthExpired`, above) — surfaced only because the
user happened to notice all three showing the same stale "Jul 1" date and asked. Fixed with a
`fetch_failed` flag threaded through `_career_doc_metadata()` → `api_preview_application()` →
the frontend, rendering a `⚠ unconfirmed` badge (`.badge-warn`) next to any field where a Google
Doc is configured but its real title/date couldn't be confirmed — visually distinct from the
ordinary, silent local-file fallback for a field with no Google Doc linked at all.

### Interviews area (2026-07-24)

Built out the Interviews stub with the same design already proven for Applications: a job
"moves" from Applications to Interviews the instant it has interview-prep content, the same way
it moved from Jobs to Applications the instant it had generated documents — presence of a file on
disk, not a stored flag, so the same reasoning (survives `cleanup_old_jobs()`'s 30-day purge)
applies again.

**"Interviewing" button** on each Applications-list card (`renderApplicationsList()`) — this
required converting `.app-card` from an `<a onclick=...>` to a `<div onclick=...>` first, since a
nested `<button>` inside an `<a>` is invalid HTML (latent in the original markup, only surfaced
once this feature needed to nest an interactive control inside the card). Click →
`POST /api/applications/<id>/interview`, then navigates to `#/interviews/<id>`.

**`generate_interview_prep(job, job_description, career_docs, model_key)`**: modeled directly on
`generate_application_docs()` — shares the same `HARD_RULES` voice guardrails (hoisted from a
local variable to a module-level constant so both functions use the exact one, not duplicated
copies) and the same `_build_career_context()` background material. Drafts 6-8 questions each
paired with an answer outline grounded in real, named stories from the career reference doc. This
is the one place in the app that needed a *repeating* XML block
(`<qa><question>...</question><outline>...</outline></qa>`, parsed with `re.findall`) instead of
the usual fixed-named-tag extraction, since the question count is variable — verified against a
hand-written malformed-trailing-block case: an incomplete final `<qa>` is silently dropped rather
than corrupting the parse.

**Interview Instructions doc + recruiter-screen default (2026-08-04)**: previously this function's
system prompt was a fixed hardcoded string with zero user customization — no equivalent of
`instructions.md` existed for interview prep at all. Added a 4th career-doc field,
`interview_instructions` (Settings → "Interview Instructions", same `gdoc_urls` pattern as Agent
Instructions — `.gdoc-input`/`data-doc` are already generic in both `renderSettings()` and
`saveGdocSettings()`, so this needed zero JS changes beyond adding the one input field). Local
fallback file `career_docs/interview_instructions.md` exists for consistency with `bio.md`/
`resume.md`/`instructions.md`, but its actual content is wrapped in an `[exclude]...[/exclude]`
block — it's pure human-readable documentation about the field itself, not real instructional
content, and `strip_excluded_blocks()` (applied to every loaded doc, local or Google Doc) reduces
it to an empty string, so it correctly falls through to the built-in default rather than
accidentally overriding it with its own meta-text about itself. `_build_career_context()`'s
EXCLUDE set also gained `interview_instructions.md`, same reasoning as `instructions.md` already
being excluded — writing-style/system-prompt guidance, not career background material.

The **built-in default** (used whenever `interview_instructions.md` is empty, which is every
install until the user writes something) is deliberately written for the **initial recruiter
screen** specifically — user-specified (2026-08-04): the first interview in a process is
categorically different from a hiring-manager or technical round, high-level and focused on
background walkthrough, motivation/fit, and logistics rather than deep technical drilling.
Verified live against a real Senior Backend Engineer JD: the default produced a background
walkthrough question, light (not deep) treatment of the role's core technical responsibility, a
mentoring question, motivation/fit, a behavioral question, and a career-trajectory/logistics
question — no deep system-design/technical-drilling questions, which is exactly the
recruiter-vs-hiring-manager distinction this was built for.

**Multiple interview rounds (2026-08-04, same day)**: the "mechanism for adding additional
interviews" mentioned as deferred above turned out not to need much deferring — built the same
day, once there was a concrete second case to design against. `interview_prep.json` changed from
a single flat `{questions, generated_at, token_usage}` to `{'interviews': [round, round, ...]}`,
each round `{'id', 'heading', 'interviewer_name', 'interviewer_title', 'additional_info',
'questions', 'generated_at', 'token_usage'}`. Round 0 (created by the "Interviewing" button, as
before) is always the recruiter screen — `_interview_heading()` hardcodes the `'Recruiter Screen'`
label specifically for index 0 with no interviewer info. Every later round comes from the new
**"+ Add Interview"** button on the Interview detail page, which opens a modal (interviewer name,
interviewer title, free-text additional context — the app's first real modal dialog; nothing else
in `index.html` used one before this, so `.modal-overlay`/`.modal-box` CSS was added fresh) and
posts to `POST /api/applications/<app_id>/interview/add`.

**Deliberately no fixed stage enum** (hiring_manager/technical/onsite, etc.) — `generate_interview_prep()`
gained `interviewer_name`/`interviewer_title`/`additional_info` params instead, and when any are
given, swaps in a built-in **adaptive** system prompt that explicitly tells the model to infer
likely depth/focus from the interviewer's title rather than assume any one fixed stage (a hiring
manager round probes fit/ownership differently than a peer engineer's technical round, which
differs again from a final round with an executive). Verified live: a fake "Alex Rivera,
Engineering Manager" round produced entirely different, appropriately deeper questions (technical
collaboration process, compliance-as-data-model-constraint, day-to-day partnership) than the
recruiter screen's background/motivation/logistics questions for the same job — confirming the
adaptive prompt actually changes register based on title, not just cosmetically labeling the
round differently. `career_docs['interview_instructions.md']`, when configured, still fully
overrides *either* built-in default (recruiter-screen or adaptive) — same full-override semantics
as `instructions.md` for cover letters, regardless of which round this is.

**Regenerate is now per-round**: `POST /api/applications/<app_id>/interview/<round_id>/regenerate`
replaces the old single-file-level regenerate — it re-runs `generate_interview_prep()` using that
*specific* round's already-stored interviewer_name/title/additional_info (so regenerating the
recruiter screen stays a recruiter screen, and regenerating "Alex Rivera" stays that round), and
only overwrites that one entry in `interviews`; every other round is untouched. The frontend shows
each round as its own collapsible `<details class="interview-round" open>` block (open by default
— the point of collapsing is to keep a growing list manageable, not to hide content on load) with
its own "↻ Regenerate" button in the summary row (`event.preventDefault()`/`stopPropagation()`
needed there, since a native `<details>` toggles open/closed on any click inside `<summary>`
unless stopped).

**Backward compatibility**: `_read_interview_prep(folder)` detects the old flat format (no
`'interviews'` key) and migrates it in place — wraps the existing questions/generated_at/
token_usage into a single `'Recruiter Screen'` round, immediately writes the migrated shape back
to disk (not just in memory) so the newly-assigned round `id` stays stable across reads, same ad
hoc schema-evolution approach `load_data()` already uses for `job_data.json`. **Confirmed for
real**: this migration was accidentally triggered mid-development against all three of the
account's actual existing `interview_prep.json` files (Tilt, Create Music Group, NineTwoThree AI
Studio — the latter two under the legacy `OUTPUT_FOLDER` Drive path, not the local `applications/`
tree) by a `GET /api/interviews` call made against a `--dev` server while testing this feature —
a fresh reminder of the same standing gap documented above (`applications/`, and by extension
`OUTPUT_FOLDER`, are not `--dev`-isolated; a list-all route walks every real folder regardless of
which data file is loaded). All three were verified to retain every original question, unchanged,
after migration, and backed up to `logs/backups/` before/after as a precaution regardless.

**Storage**: `interview_prep.json` is written into the **same folder** the application's
`Cover Letter.txt` already lives in — not a freshly-computed `get_output_dir()`, which always
resolves to today's date and would split interview prep into a different dated folder than the
application content it belongs with. The job description needs no re-fetch: `Job
Description.txt` is already saved in that same folder by `save_application_docs()`, so interview
prep generation works even if the underlying `data['jobs']` record has since been purged, same as
everything else in this design. No Drive upload for `interview_prep.json` — `list_applications()`/
`list_interviews()` are purely local-disk reads already (Drive is a write-through mirror for
browsing, never this app's own read path), so skipping it is a deliberate, consistent scope cut.

**Shared disk walk**: `list_applications()`'s folder-resolution logic (walk
`BASE_DIR/applications` + the legacy `OUTPUT_FOLDER`, collapse to the latest dated folder per
(Company, Title)) is now factored into `_walk_application_folders()`, returning the raw disk
facts with no job-record join or status filtering. `list_applications()` filters to folders
*without* `interview_prep.json`; the new `list_interviews()` filters to folders *with* one — the
two are structurally exclusive by construction (one shared walk, two opposite filters) rather
than two independently-maintained conditions that could drift apart. `list_interviews()` also
carries the full original application content (`cover_letter`/`sections`/`contact`/`feedback`)
alongside the parsed questions, so a job's cover letter and resume don't become unreachable just
because it moved to Interviews — the Interview detail page's "View application documents"
disclosure section shows both.

**Route reuse for regenerate — a real bug found via live testing**: `POST
/api/applications/<app_id>/interview` first resolved its target via `list_applications()`, which
by definition excludes anything that already has `interview_prep.json` — meaning a second call
("Re-generate Questions" from the Interview detail page) always 404'd. Fixed by resolving via
`_walk_application_folders()` + `_application_id()` matching directly, independent of which list
(Applications or Interviews) the entry currently belongs to. The route is otherwise idempotent —
overwrites `interview_prep.json` in place on every call.

**Jobs → Interviews deduping gap, found 2026-07-28**: `renderJobs()` (index.html) excludes from
every Jobs sub-tab any job whose id appears in an `appliedJobIds` set — but that set was built
only from `state.applications`, and a job that progresses all the way to Interviewing disappears
from the Applications list entirely (`list_applications()` excludes anything with
`interview_prep.json` — see above). Its job id silently fell out of the exclusion set, and the
job reappeared in Found/Screened as if it had never been applied to (confirmed for real: a real
Tilt job with `interview_prep.json` already generated was still showing in Jobs). Fixed by
unioning `state.interviews`' job ids into the same set — `[...state.applications.map(a =>
a.job_id), ...state.interviews.map(i => i.job_id)].filter(Boolean)`. A job is "gone from Jobs"
once it's *either* an application or an interview, not just the former; keep this union in mind
if a third downstream stage is ever added after Interviews.

**A real, unrelated finding from verifying this feature**: `BASE_DIR/applications/` is **not**
scoped by `--dev` — only `job_data.json` vs. `job_data.dev.json` is. A `--dev`-server API call
that writes into an application's folder (as `POST /api/applications/<id>/interview` does) writes
to the *same real, permanent* folder tree a production run would, regardless of which data file
is loaded. Verifying this feature against the dev server for real used real applications
(Headspace, Greenlight Financial Technology) as test subjects and moved both to Interviewing for
real, with Haiku-drafted content, before being caught and reverted (deleting their
`interview_prep.json` files) by request. **The standing dev/prod isolation rule needs a mental
addendum**: `--dev` isolates the job list, not the `applications/` directory — any verification
that writes generated-document or interview-prep content needs either a disposable throwaway
job/application (real Adzuna-style `__TEST__` naming, trashed after — the pattern already used
for Google Doc template testing) or explicit confirmation before running against a real company's
folder, even on a `--dev` port.

### Sonnet re-evaluation (2026-07-20)

Resolves PROBLEMS.md #7 (a confirmed real Haiku misclassification — the Evolve case, flagged
"not remote" despite unambiguous nationwide-remote-eligibility language, verified against a
side-by-side Sonnet test that got it right). Rather than switching `classify_job()` to Sonnet
everywhere (an ongoing per-job cost increase for every candidate that survives the cheap
pre-filters) or leaving Haiku's mistakes to be caught only manually, this adds one deliberately
manual, one-click action: **Re-evaluate**.

`classify_job()` gained an optional `model_key: str = 'haiku'` parameter (every existing call
site is unaffected — the default is identical to before). `reevaluate_job(job, settings)` — only
valid for `job['exclusion_reason'] in ('not_remote', 'not_software')`, the two reasons that
actually involved a model judgment call (`title_mismatch`/`title_excluded` are plain keyword
matching; a stronger model doesn't read a string-contains check any differently, so re-evaluating
those isn't offered at all). Gets the job's persisted JD text (falls back to a fresh
`fetch_job_description()` for older records saved before full text started getting persisted
unconditionally — see the ingestion funnel's step 6 above), calls `classify_job(..., model_key='sonnet')`,
and applies the result through the same `_apply_classification()` every other classification path
uses. If Sonnet disagrees with the original Haiku verdict, the job moves to Found/Screened with a
real fit rating, exactly like any other job reaching that helper — not a special-cased "restored"
state. If Sonnet agrees, the job stays excluded, now with Sonnet's reasoning recorded instead of
Haiku's. Route: `POST /api/jobs/<id>/reevaluate`.

Replaces two previous half-measures that both just re-ran Haiku (the same model that produced the
original wrong verdict, so neither actually caught anything): the transient "View excluded"
panel's "+ Add anyway" button (for `not_remote`/`not_software` entries specifically — kept as-is
for the two keyword-filter reasons) and the persistent Excluded tab's "Restore to Found" +
generic Screen-retry combo (same split — Restore to Found now only shows for
`title_mismatch`/`title_excluded`). The transient panel's excluded entries are built from the raw
candidate dict, not the persisted job record, and for Adzuna/remote.com candidates specifically
that dict may never have had a real `id` before the persisted record's was generated — fixed by
having `evaluate_candidates()`'s `_exclude()` calls for `not_remote`/`not_software` pass the full
`job` dict (post-id-assignment) instead of the original `cand`, so the transient report's entries
reliably link to the same record "Re-evaluate" needs to look up.

### Manual "Track anyway" override (2026-07-23)

Real gap found via the two Headspace jobs from "Accepted locations" below: Sonnet's remote
verdict on a genuinely ambiguous, location-conditional posting isn't fully deterministic —
re-running Re-evaluate on the same job can come back `not_remote` again even after a *prior* run
already flipped a functionally-identical case to `remote`. Before this, a job stuck disagreeing
with the user's own judgment had no path forward except repeatedly mashing Re-evaluate and hoping
for a different sample, or manually editing `job_data.json`.

`reevaluate_job()` now sets a persisted `job['reevaluate_failed']` flag (`True` if the job is
still excluded after the Sonnet call, `False`/irrelevant once it isn't — persisted, not just a
one-off response value, so the frontend doesn't need the user to have just clicked Re-evaluate in
this session to know it's already been tried). The Excluded tab reads this flag
(`renderExcludedJobsList()`) to decide which action to offer for `not_remote`/`not_software`
entries: **Re-evaluate** as before on the first look, swapping to **Track anyway** once
`reevaluate_failed` is `True` — Sonnet has already had its shot, offering it again would just be
the same coin flip.

**Track anyway** (`track_job_manually()`, `POST /api/jobs/<id>/track`) is a deliberate, manual,
last-resort override: forces `remote_status`/`software_status` to accepted values and clears
`exclusion_reason`, moving the job straight to Screened — then rates it exactly like any other
job reaching that state (skipped originally since it was excluded before ever reaching the
rating step). Leaves `remote_reasoning`/`software_reasoning` untouched, so the model's original
objection stays visible on the job card for reference; only `exclusion_reason` itself is cleared.
Sets `job['manually_tracked'] = True`, shown as a small "manual" badge on the job card
(`.badge-hybrid`, reused — same "accepted despite not being a clean automatic match" visual
category as the hybrid badge) so it stays clear later which jobs got there by a human override
rather than a passing classification.

### Accepted locations: candidate-location context + accepted hybrid states (2026-07-22)

Grew out of investigating two real Headspace jobs stuck as `not_remote`: the JD said "hybrid
only for greater SF Bay Area residents," `remote` for everyone else — a genuinely
location-conditional requirement that a location-blind classifier can't resolve correctly. A live
side-by-side test proved that simply telling the classifier where the candidate actually lives
flips the verdict correctly. The user asked to build on that and go further: track an array of
accepted location/arrangement combinations, turning the app from strictly remote-only into
"remote (with my location as classification context) OR hybrid in specific states."

`settings.accepted_locations` — an array of `"{type}-{STATE}"` strings, e.g.
`["remote-VT", "hybrid-NH", "hybrid-MA", "hybrid-VT"]`, edited via a Settings-tab textarea (same
one-entry-per-line pattern as `remote_com_search_urls`). `_parse_accepted_locations(settings)`
splits this into `(candidate_location, accepted_hybrid_states)`:
- The (expected single) `remote-X` entry isn't a new eligibility check — it's exactly the
  location-context experiment above: `classify_job()`/`extract_and_classify_job()` gained an
  optional `candidate_location: str = ''` parameter that appends a short "the candidate is based
  in {location}, judge remote-ness based on what they'd actually experience" instruction to the
  system prompt (via the shared `_location_context()` helper). Empty by default, so the prompt is
  byte-identical for anyone who hasn't configured this. Threaded through all 8 real call sites —
  every place a job gets classified, same "no source-specific shortcut" principle as the
  ingestion funnel itself.
- Every `hybrid-X` entry means a genuinely hybrid (not fully onsite) role based in that state
  should be accepted, not excluded — a deliberate **MVP, deterministic, no-AI-call** check
  (`is_hybrid_accepted(location, description, accepted_hybrid_states)`, reusing the existing
  `parse_state()` built for location-cluster dedup): the location parses to an accepted state
  AND the description contains the literal word "hybrid" (case-insensitive substring, consistent
  with this app's existing simple-matching style, e.g. `title_exclude_keywords`).

`remote_status` is now 3-way: `'remote' | 'hybrid' | 'not_remote' | 'unknown'`, decided by the
new shared `_decide_remote_status(remote_verdict, location, description, settings)` — a `'no'`
verdict gets one more chance via `is_hybrid_accepted()` before finalizing as `not_remote`. Called
from both `_apply_classification()` and `evaluate_candidates()`'s own inline step-6 logic (kept
separate per that function's existing documented reasoning — not consolidated). `job['hybrid_state']`
records which state matched (`''` otherwise). Every place that gated on `remote_status == 'remote'`
now accepts `'hybrid'` too, since an accepted hybrid role should flow through identically: the
fit-rating condition (in both `_apply_classification()` and `evaluate_candidates()` step 6) and the
frontend Screened-tab filter (`index.html`'s `renderJobs()`). A `'hybrid'` status never enters the
`not_remote` exclusion branch, so no separate exclusion-logic change was needed. Job cards show a
`hybrid-{state}` badge (`.badge-hybrid`) next to the fit rating when applicable.

**Adzuna query expansion**: Adzuna has no combined "remote or hybrid-in-this-state" filter, so
`_adzuna_request()` gained an optional `where: str = ''` param — mutually exclusive with
`remote_only`: set `where` (a state abbreviation or full name both work, verified live against the
real API) for a location-scoped query, omit it for the existing remote-only behavior.
`sweep_adzuna_jobs()` now runs, per configured title keyword, the existing `remote_only=1` query
**plus** one additional `where=<state>` query per accepted hybrid state (refactored the
per-query pagination into a local `_run_adzuna_query(query, where='')` closure so both modes share
retry/boundary/dedup logic). Real added cost — more requests, more candidates through the funnel,
longer sweep runtime — accepted as an explicit MVP tradeoff. Location-scoped candidates aren't
remote-prefiltered, so `is_remote` is left unset for them (`None`) rather than hardcoded `True`,
falling back to the existing `detect_remote()` heuristic inside `evaluate_candidates()`.
remote.com and company-board scraping needed no code change: company boards are already
unfiltered at the source (classification handles it), and remote.com search is entirely driven by
the user's own saved search URLs.

**A real, unrelated bug found via live testing this feature**: `classify_job()`'s tag-extraction
`extract()` helper used `re.search` (first match). On a genuinely ambiguous case, Sonnet
occasionally self-corrects mid-response — emitting a draft `<remote>no</remote>` block with a
malformed reasoning close (`</remote>` instead of `</remote_reasoning>`), then a corrected
`<remote>yes</remote>` block — and the first-match regex silently picked up the stale draft
verdict instead of the corrected one. Fixed in both `classify_job()` and
`extract_and_classify_job()` by switching to `re.findall` + last match, which prefers whatever the
model ultimately settled on. Verified live: re-running the two real Headspace jobs through
`reevaluate_job()` with `accepted_locations` configured now correctly classifies them `remote`
(previously `not_remote`).

### LinkedIn connections (2026-07-28)

LinkedIn has no usable public API for checking whether the user has a connection at a given
company — the old Connections API was shut down specifically to protect LinkedIn's own social
graph, and live scraping risks a real account ban. The ToS-safe workaround: the user exports
their own connections as a CSV from LinkedIn's own data-export feature and imports it manually,
periodically, via Settings → General → LinkedIn Connections (file picker + Import button).

**Getting the right file is the hard part, not the code.** LinkedIn's "Get a copy of your data"
flow lets the user pick which data categories to include, and different category selections
produce differently-bundled zip files — in practice this took several attempts to get right (a
"Basic" export with no connections data at all, then a partial export that happened to include
`Patents.csv` but not `Connections.csv`, before a "Complete" export that finally included all 37
categories together). The one thing that matters: the export must include the **Connections**
category specifically — it's its own distinct checkbox, separate from Contacts, Invitations, or
any of the other categories LinkedIn bundles by default. Once the right zip is in hand,
`Connections.csv` is reliably identifiable as the file whose header row (after a couple of
"Notes:" preamble lines) reads `First Name,Last Name,URL,Email Address,Company,Position,
Connected On`.

**`_parse_linkedin_connections_csv(csv_text)`** (job_checker.py) searches for that header row by
content rather than assuming a fixed line number, strips a leading BOM if present, and skips any
row with no company text at all (nothing to ever match a job against). Raises a clear
`ValueError` if no header is found, so `POST /api/linkedin-connections/import` can tell the user
"this doesn't look like a LinkedIn connections export" instead of silently importing zero rows.
Stored as `data['linkedin_connections']` (list of `{name, company, position, url,
connected_on}`) + `data['linkedin_connections_imported_at']` — same whole-blob JSON pattern as
everything else in `job_data.json`, and notably **not** a separate Google Doc or disk folder, so
(unlike the applications/interview-prep folder and the To Do doc) this data *is* fully isolated
by `--dev` / `job_data.dev.json` like the rest of the job list.

**`find_connections_at_company(company_name, connections)`**: case-insensitive substring match in
both directions ("Google" matches a connection's "Google LLC") — the same simple-matching style
already used throughout this app (`title_exclude_keywords`, `is_hybrid_accepted()`'s "hybrid"
check), not fuzzy/semantic matching. Used in two places:
- `api_preview_application()` (Job detail page) — full match list with names/positions, shown in
  a Fit-Rating-styled section, hidden entirely if nothing's imported or nothing matches.
- `GET /api/jobs` (2026-07-29) — a `linkedin_connection_count` per job, computed for every job on
  every request (benchmarked against real data: ~80ms for ~2,500 jobs × ~300 connections, cheap
  enough at this app's scale not to need caching). Rendered as a small yellow star right next to
  the company name on Found/Screened/Excluded cards (`renderJobList()`/`renderExcludedJobsList()`)
  — deliberately a different color/position from the fit-rating star badge, and placed by the
  company name specifically (not in the actions column) so it's scannable across the whole list
  without opening each job.

### Chrome extension: capture a JD from the browser (2026-07-16)

`chrome-extension/` (new top-level directory, separate from the Flask app) — a Manifest V3
extension that solves what server-side scraping structurally can't: `fetch_job_description()`
never executes JavaScript or carries a real browser session, so it can't get past bot-blocked
sites (Adzuna's own site, Wellfound, Workday) or JS-rendered ATS pages (Ashby). The extension
captures whatever's already rendered in the user's own browser instead of trying to re-fetch it
server-side.

One click on the toolbar icon (`background.js`, a service worker — no popup, `activeTab` +
`scripting` permissions inject a tiny function into the active tab to grab
`{url: location.href, text: document.body.innerText}`) → `POST http://localhost:5000/api/capture`
→ result reported via a **badge on the toolbar icon itself**
(`chrome.action.setBadgeText`/`setBadgeBackgroundColor`/`setTitle` — ✓ green / – yellow (excluded,
not saved) / ✕ red, full message in the hover tooltip). Deliberately **not** `chrome.notifications`
— tried that first, but OS-level notification permissions (macOS Notification Center settings,
Focus/DND) silently swallowed it with no error, which looked exactly like "the extension isn't
doing anything" even though captures were succeeding server-side (confirmed via server logs while
debugging). The action badge is rendered by Chrome directly and can't be silenced that way — no
`notifications` permission in the manifest at all now. Badge clears after ~8s, tooltip after ~30s.
Hardcoded to port 5000 (the real instance) — not configurable for `--dev` testing.

Backend (`job_checker.py`): `capture_job(url, page_text)` tries, in order:
1. **Exact URL match** against every tracked job — cheapest, covers "clicked a link out of the
   app itself." Reuses `classify_job()` with the job's existing title/company.
2. **No URL match** → `extract_and_classify_job(page_text)`, a new function alongside
   `classify_job()` that additionally extracts `<title>`/`<company>` from raw, noisy scraped
   text in the same Haiku call (needed because we don't have title/company yet to feed
   `classify_job()`'s existing signature).
3. Match the extracted (title, company) against jobs specifically **stuck for lack of a JD**
   (`evaluated_at is None and jd_fetch_status not in ('ok', 'manual')` — a different, narrower
   set than `screen_found_jobs()` targets, which is for jobs that already have a JD but got
   stuck some other way).
4. No match anywhere → create a brand-new job from scratch (`source: 'extension'`), same
   construction pattern as `api_add_from_search`/`api_generate_adhoc`.

All three "found something" outcomes funnel through one shared helper, `_apply_classification()`
(job_checker.py; renamed 2026-07-17 from `_finalize_captured_job` once it stopped being
capture-only — see below) — sets description/status/reasoning/`evaluated_at`, and either calls
`rate_job_fit()` if the verdict passes or sets `exclusion_reason`/`exclusion_reasoning` if it
doesn't, since `classify_job()`'s and `extract_and_classify_job()`'s return shapes share the same
`remote`/`remote_reasoning`/`software`/`software_reasoning` keys. **A captured job that fails
classification is still saved** (changed 2026-07-17 — it used to return without saving anything,
which is exactly what hid the Evolve misclassification: the badge showed the rejection but there
was no record to go back and review). `capture_job()` returns `{'action': 'excluded', 'job': {...},
'reason', 'reasoning'}` in that case; `background.js` already reads `body.job.title`/`company_name`
first, so no extension-side change was needed. No `title_keywords`/`title_exclude_keywords`
gating on captures — deliberate manual action, same reasoning as the other manual-add routes.

`_apply_classification()` is also now shared by `screen_found_jobs()` and
`POST /api/jobs/<id>/reclassify` (previously each had its own near-duplicate inline
classify-then-rate block) — one place sets `exclusion_reason` on a failed verdict, so all three
callers get Excluded-tab visibility for free instead of each needing its own handling.

Not yet done: real end-to-end browser testing (loading the unpacked extension, clicking it on an
actual job page) — verified thoroughly at the backend/route level, but the extension itself
needs the user's own hands-on click-through, consistent with how UI verification works
throughout this project (see project memory).

**Real bug found and fixed 2026-08-06: a capture from a search-results page produced a JD cut off
mid-heading, and a cover letter generated against it.** The user captured a Workable *search
results* page with one job open in a side panel (URL ending `...&selectedJobId=...`, not a
dedicated per-job page) — `document.body.innerText` on that page is dominated by ~20 unrelated
job listings' worth of chrome, which happened to appear *before* the real job's actual content in
reading order. `capture_job()` used to persist that raw noisy text directly (truncated to 8000
chars at the time), so the real job description got cut off right as its "Responsibilities"
heading began — a cover letter then got generated against that garbage, since nothing checked
whether the "job description" being used for generation was actually a coherent job posting
versus a truncated fragment of page chrome.

Two separate, compounding causes, both fixed:
1. **No structural cleanup on the capture path.** Every other ingestion path already has some
   protection against exactly this: a normal URL fetch goes through `_extract_readable_text()`,
   which strips `<nav>`/`<header>`/`<footer>` tags before anything else. The extension's raw
   `innerText` has no HTML structure left by the time it reaches the backend, so none of that
   applies — there was no cleanup step at all for this one path. Fixed by having
   `extract_and_classify_job()` (already reading the full, untruncated page text to find title/
   company) additionally extract a `<description>` — the model isolates the actual job posting's
   real content and excludes anything belonging to other listings or page chrome, rather than the
   raw text being persisted and truncated blindly afterward. `capture_job()` now uses this cleaned
   `extraction['description']` (falling back to the raw page text only if the cleaned version
   itself looks invalid, same "something beats nothing" pattern as `_fallback_jd_text`) for the
   two "no URL match" outcomes (`matched`/`created`); the exact-URL-match outcome (`attached`)
   is unchanged, since a URL that already matches a previously-tracked job's canonical URL is
   lower-risk than an arbitrary page the user happened to have open. `max_tokens` for this call
   went from 450 to 6000 to give real headroom for echoing back a full posting, same "don't cut
   off mid-response" lesson as `rate_job_fit()`'s own earlier `max_tokens` bump.
   Verified with a synthetic reproduction of the exact failure shape (25 fake unrelated job cards
   followed by real Hospitable-shaped content): the fix correctly isolated only the real content
   (zero trace of the fake filler company), including sections after "Responsibilities" that the
   old truncation would have cut off.
2. **The 8000-character cap itself was genuinely too tight, independent of the capture bug.**
   Checking real production data found 141 already-tracked jobs sitting at *exactly* 8000
   characters — real content was already being silently truncated broadly, across every source,
   not just this one capture-path failure mode. All four `[:8000]` call sites (`evaluate_candidates()`,
   `_apply_classification()`, `_extract_readable_text()`, the remote.com structured-extraction
   path) now share one `MAX_JD_CHARS = 20000` constant instead of a repeated magic number — chosen
   generously since real JD length is nowhere near this even for verbose postings, and the added
   token cost is trivial next to what generation calls already spend on career context (15-17k
   input tokens observed in real generations, per the token-usage tracking work above).

**Manual text-selection capture (2026-08-06, same day)**: the Hospitable investigation surfaced a
second, structurally unfixable risk alongside the truncation bug above — the extension can only
ever report what's actually rendered on the page, and a source site's own data can itself be
wrong (confirmed for real: Workable's search index had a duplicate/stale-titled copy of the same
posting, unrelated to anything this app did). No amount of server-side cleanup can defend against
the source page itself being wrong. The mitigation is putting the choice in the user's hands:
`background.js`'s injected capture function now checks `window.getSelection().toString()` first —
if the user has manually highlighted text on the page (e.g. selecting just the real JD out of a
confusing multi-job page), that exact selection is sent as `text` instead of
`document.body.innerText`. Falls back to the existing whole-page capture when nothing is
selected, so this is purely additive — no change to the common case. No new manifest permissions
needed (`window.getSelection()` works under the existing `activeTab`/`scripting` grants). A
`fromSelection` flag (client-side only, not sent to the backend — `capture_job()` doesn't need to
know or care which path supplied its text) drives a small wording addition in the result tooltip
("... (from your selected text)") so the user can confirm which path was actually used.
**Requires reloading the unpacked extension** in `chrome://extensions` for this change to take
effect — not something this session can do on the user's machine.

## Roadmap

This app is being evolved toward an autonomous agent. See project memory for current phase
and status.

1. ~~Adzuna sweep~~ — done. Manual button, not scheduled (explicit user decision, still true
   after the 2026-07-15 unification into "Get New Jobs" — see project memory).
2. Autonomous triage — partially done. Remote-ness and software-relevance classification
   (PROBLEMS.md #1, #4) are built and run on every new candidate via `evaluate_candidates()`.
   The fuller apply/skip/maybe judgment with reasoning is not built yet.
3. Auto-generate documents above a confidence threshold, human reviews before submitting.

Human stays in the loop for: which jobs to apply to, and final review of generated docs. The
excluded-review panel (see Frontend, above) is the human-in-the-loop check for the new
classification steps — every automatic exclusion is visible and reversible (via "Re-evaluate" for
the two model-judgment reasons, "+ Add anyway"/"Restore to Found" for the two keyword-filter
reasons — see "Sonnet re-evaluation"). The persistent Excluded sub-tab (2026-07-17) extends this
from "review this run's rejections before they're gone" to a standing, filterable record of every
judgment-call rejection with reasoning — built specifically to surface classifier accuracy
patterns (e.g. a real Haiku misclassification found via the extension, see PROBLEMS.md), which is
exactly what led to building Sonnet re-evaluation (2026-07-20) rather than switching the whole
pipeline to Sonnet or leaving it unaddressed.
