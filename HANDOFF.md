# Goat Search — Project Handoff

## What this is

Goat Search is a personal job-tracking Flask app. It scrapes career pages / ATS boards for new postings, matches them against keywords, and uses the Anthropic API to draft tailored cover letters and resume bullets from the user's own reference docs. Single-user, runs locally.

## Running it

```bash
source venv/bin/activate
python job_checker.py               # web app at http://localhost:5000
python job_checker.py --check       # one scrape pass (for cron/launchd)
```

Config in `.env`: `ANTHROPIC_API_KEY`, `OUTPUT_FOLDER`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`.

## Architecture

- **`job_checker.py`** — Flask backend, all routes and scraping/generation logic
- **`index.html`** — entire frontend: inline CSS + vanilla JS, talks to backend via `/api/*` JSON routes
- **`job_data.json`** — entire datastore (companies, jobs, keywords, settings)
- **`career_docs/`** — markdown reference files (`bio.md`, `resume.md`, `instructions.md`) used as grounding context for Claude-generated docs. `instructions.md` becomes the system prompt. Any `[exclude]...[/exclude]` block is stripped before reaching the model.

## Key backend details

- `strip_excluded_blocks(text)` — strips `[exclude]...[/exclude]` blocks from career docs before API calls
- `HARD_RULES` — hardcoded style rules prepended to every system prompt (no em dashes, no fabricated numbers, no buzzwords). Do not remove or weaken.
- `generate_application_docs()` — calls Claude, parses XML-tagged sections from response: `<cover_letter>`, `<introduction>`, `<rentredi_bullets>`, `<metis_bullets>`, `<sonos_bullets>`, `<spotify_pm_bullets>`, `<spotify_eng_bullets>`, `<phone>`, `<address>`, `<strategy>`, `<missing_info>`, `<assessment>`
- `save_application_docs()` — writes output to `{OUTPUT_FOLDER}/{Company}/{Title}/{YYYY-MM-DD}/` as `.txt` files + `sections.json` (used to reload docs without re-calling Claude)
- `get_output_dir(job)` — returns today's output folder path for a job
- `make_job_id(company_id, title, url)` — md5 hash, deduplicates jobs across scrapes
- `score_job()` — keyword matching, computed live at read time (not stored)
- Two model tiers in `MODELS`: `haiku` for testing, `sonnet` for real generation, selected via `settings.model`
- Google Docs integration: career docs can be pulled from Google Docs URLs (stored in `settings.gdoc_urls`), fetched via `/export?format=txt`

## Scraping pipeline

- Greenhouse, Lever, Ashby: public JSON APIs scraped directly
- Everything else: `scrape_generic()` HTML heuristic scraper
- `detect_ats_for_url()` — sniffs a careers page to suggest correct ATS board URL
- Jobs identified by md5 hash of `company_id|title|url` — deduplicates across scrapes
- New jobs get `is_new: True` until user dismisses them

## Adzuna search

- `GET /api/search` — searches Adzuna with `remote_only=1`, `what_phrase`, `salary_min=100000`, `max_days_old` (configurable in UI), `full_time=1`
- Results filtered client-side to jobs where query phrase appears in title
- `POST /api/jobs/add-from-search` — adds an Adzuna job to tracked list, follows redirect to get real ATS URL, sets `is_remote: True`

## Application generation pipeline

- `POST /api/jobs/<id>/generate` — fetches/reuses JD, loads career docs, calls Claude, saves files
- `POST /api/generate-adhoc` — same but for jobs not in tracked list; saves the job to `job_data.json` after generation
- `GET /api/jobs/<id>/documents` — returns previously generated docs from `sections.json`
- `GET /api/jobs/<id>/preview` — checks for saved `Job Description.txt` first, only scrapes if not found

## Frontend

- Tab-based: Jobs / Companies / Keywords / Settings / Search
- All job filtering is client-side in `renderJobs()` (new only, remote only, keyword matches)
- Apply modal: preview JD (editable), company + title fields (editable), then result view with individual copy buttons per section
- "View Documents" button appears if docs already exist; button changes to "Re-Generate Documents"
- Feedback from Claude displayed at top of result view as plain text (no copy button)
- Contact info (phone, address) displayed at bottom of result view
- "Applied" is a checkbox in the modal footer
- "+ Apply" button in header opens ad-hoc modal with blank fields

## What we were about to build next

The next phase is turning this into an agent:

**Phase 1 (immediate):** Daily automated Adzuna sweep — paginate through all PM roles posted in the last 24 hours and add them to `job_data.json`. The existing `--check` flag + launchd scheduled task handles company boards; Adzuna needs its own scheduled sweep with pagination.

**Phase 2:** Autonomous triage — Claude evaluates each new JD against the user's background and outputs apply / skip / maybe with reasoning.

**Phase 3:** Auto-generate documents for jobs above a confidence threshold; user reviews before submitting.

Human in the loop stays at: which jobs to apply to, and final review of generated docs.
