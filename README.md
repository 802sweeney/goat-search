# Goat Search

> **Work in progress.** This is an active personal project, not a finished product — it's
> rebuilt and extended continuously as real gaps turn up while actually using it for a real job
> search. Expect the feature set and even the architecture to keep changing.

A personal job-search agent: it scrapes job postings from multiple sources, classifies them
against a candidate's real background using LLMs, and for anything worth applying to, suggests
an application strategy and drafts tailored materials — a cover letter and resume bullets — for
the candidate to review, edit, and submit themselves. It also drafts interview prep questions
and tracks the whole pipeline — screened → applied → interviewed — in a local dashboard with
historical cost/token metrics.

**The agent never submits anything on the candidate's behalf.** Every output — the fit
assessment, the strategy notes, the cover letter, the resume bullets, the interview questions —
is a draft or a recommendation. A human reads it, decides what to do with it, and is the one who
actually applies. "Applied" and "Interviewing" are states the candidate marks themselves, not
actions the system takes.

Single-user, runs entirely locally.

## Why this exists

Generic job boards surface volume, not fit. This project inverts that: it pulls candidates from
six different sources, runs every single one through the same classification and fit-rating
funnel against the candidate's actual career background, and only then presents a short, ranked
list of real candidates — with the reasoning for every inclusion and exclusion kept visible and
reversible, and a human making every decision about what happens next.

## Architecture

Two files hold essentially all of the application logic, intentionally — no framework, no
build step, no ORM:

| Layer | File | What it is |
|---|---|---|
| Backend | `job_checker.py` (~7,000 lines) | Flask app — every route, scraper, and LLM call, in one file |
| Frontend | `index.html` (~4,500 lines) | The entire UI — inline CSS + vanilla JS, a hash router, zero dependencies |
| Working-set store | `job_data.json` | Companies, tracked jobs, settings — read/written wholesale per request |
| Permanent records | `applications/<Company>/<Title>/<Date>/` | Generated cover letters, resumes, interview prep — never purged |
| Long-term metrics | `metrics.db` (SQLite) | Daily time-series: funnel counts, token usage/cost by model |

**Why no database for the main data model?** At this scale (a few thousand jobs, single user,
no concurrent writers beyond the app itself), a JSON blob with atomic writes (`tempfile` +
`os.replace`) is simpler to reason about than a schema migration story, and it's trivially
inspectable by hand. The one place a real database earns its keep — long-term metrics that need
daily granularity for trend charts — got a dedicated SQLite file instead of bolting more ad hoc
counters onto the JSON blob.

**Why a Chrome extension too?** Server-side scraping structurally can't get past bot-blocked
sites or JS-rendered pages. The extension captures whatever's already rendered in the user's own
authenticated browser session and posts it to the same classification pipeline — same funnel,
different entry point.

## Core workflow: ingestion

Six independent sources feed one shared funnel — no source gets a shortcut around it:

```mermaid
flowchart LR
    subgraph Sources
        A[Company boards<br/>Greenhouse / Lever / Ashby / generic HTML]
        B[Adzuna sweep]
        C[remote.com sweep]
        D[Jobicy sweep]
        E[Himalayas sweep]
        F[Workable cross-company sweep]
        G[Chrome extension capture]
    end
    A --> H[evaluate_candidates<br/>shared funnel]
    B --> H
    C --> H
    D --> H
    E --> H
    F --> H
    G --> H
    H --> I{"Title keywords"}
    I -->|no match| X1["excluded"]
    I -->|match| J["URL / repost / location dedup"]
    J -->|duplicate| X2["excluded"]
    J --> K["classify_job()<br/>remote + software-relevant<br/>(Haiku)"]
    K -->|no| X3["excluded, reviewable"]
    K -->|yes| L["rate_job_fit()<br/>1-5 fit score + reasoning<br/>(Haiku)"]
    L --> M[("job_data.json")]
```

Every automatic exclusion is persisted with the model's reasoning and stays reviewable/
reversible — nothing is a silent drop. A weaker-model misclassification can be escalated to a
stronger model for a second opinion (`reevaluate_job`) rather than trusting the first verdict
forever.

## Core workflow: application generation

```mermaid
flowchart TD
    A["Candidate clicks Generate"] --> B["Resolve job description<br/>cached file to stored text to fresh fetch"]
    B --> C["Load career docs<br/>Bio, Instructions, Career Summary"]
    C --> D["generate_application_docs()<br/>Claude / Gemini / OpenAI, candidate's choice"]
    D --> E["audit_cover_letter()<br/>independent model checks the draft against<br/>a hard style rulebook"]
    E --> F["Parse into draft sections:<br/>cover letter, per-role resume bullets,<br/>strategy notes, contact info"]
    F --> G["Write local .txt + sections.json"]
    F --> H["Upload draft to Drive + fill Google Doc template"]
    F --> I["Log token usage and cost by model"]
    F --> J["Mine feedback for recurring resume gaps"]
    G --> K["Candidate reviews, edits, and submits<br/>the application themselves"]
    H --> K
```

Everything produced here is a draft plus a recommendation, not a finished, submitted
application. The "strategy notes" are the model's read on how to position for this specific
role — which stories to lead with, what's a genuine gap versus bridgeable, what's missing from
the candidate's own background doc — meant to inform the candidate's own judgment, not replace
it. The generation call and the audit call are deliberately independent models by default (e.g.
Sonnet writes, Opus audits) — the same model grading its own work tends to miss the same
mistakes it makes. Nothing in this pipeline ever submits anything to an employer; that step is
entirely manual, outside this app.

## Feature areas

- **Jobs** — Found / Screened / Excluded, with live keyword re-scoring, LinkedIn-connection
  matching, and a persistent, filterable record of every automatic rejection and why.
- **Applications** — a draft cover letter, resume bullets, and strategy notes per role, sourced
  from disk so it survives the 30-day working-set retention window on the job list itself.
  Supports per-generation model choice, additional free-text instructions, and a structured
  cover-letter audit against a fixed style rulebook (no em dashes, no fabricated numbers, no
  buzzwords, one idea per sentence, etc.) — all still drafts for the candidate to review and
  submit themselves, never auto-submitted.
- **Interviews** — once the candidate marks an application as interviewing, drafts round-specific
  prep questions (recruiter screen vs. an adaptive prompt for later rounds, grounded in named
  specifics from the candidate's real background) with support for multiple rounds per
  application — study material, not a transcript of what will actually be asked.
- **Practice** — rehearse either generic recruiter questions or any already-generated
  job-specific question. Supports dictation via the browser's native speech API; submitting an
  answer returns structured feedback (strengths / improvements / delivery / overall), checked
  against the candidate's own background doc so an unsupported claim gets flagged rather than
  assumed true.
- **To Do** — mines recurring "missing information" gaps out of generation feedback into a
  vote-ranked Google Doc, so the same gap mentioned across many applications surfaces instead of
  getting lost in one-off feedback.
- **Data** — a 30-day funnel (Sankey diagram, hand-rolled SVG), permanent lifetime counters, a
  per-model cost breakdown, and a token-usage trend chart per application over time.

## Multi-provider model support

Generation and the cover-letter audit can run on any of three providers, picked per-generation
without changing the standing default:

| Provider | Tiers |
|---|---|
| Anthropic | Haiku · Sonnet · Opus |
| Google Gemini | Flash Lite · Flash · Pro |
| OpenAI | three tiers, cheapest to flagship |

Classification, fit-rating, and interview-prep generation stay Claude-only by deliberate scope
decision — those are automated, high-volume pipeline steps rather than a one-off creative
artifact, so provider choice there would add complexity without a clear user-facing benefit.

## Notable engineering problems solved

A few representative ones, out of many documented in `PROBLEMS.md` / `CLAUDE.md`:

- **A non-atomic write race corrupted the data file under concurrent requests.** `open(path, 'w')`
  truncates before writing; a request landing in that window read a 0-byte file. Fixed with
  `tempfile.mkstemp()` + `os.replace()` (atomic on POSIX/Windows), verified with a concurrency
  stress test (1,101 failures under the old code → 0 under the new one, same load).
- **A Google Drive folder race created duplicates.** Drive Desktop's own background sync and this
  app's direct Drive API calls could both decide a folder didn't exist yet and create it
  independently. Root-caused after two failed timeout-based mitigations; the actual fix was
  removing the race's precondition entirely — local writes moved to a path Drive Desktop never
  watches, making this app the only writer.
- **A cheap classifier's mistakes were invisible.** Early on, a misclassified job was just gone —
  no record, no way to notice the pattern. Added a persistent, reviewable Excluded list with the
  model's reasoning attached, plus a one-click escalation path to a stronger model for a second
  opinion on just the judgment-call rejections.
- **Fit-rating was the single largest cost in the pipeline.** It was sending a ~72,000-character
  background doc on every call. Measured, then fixed by building a separate, deliberately short
  career-summary doc for this one use case — cut that call's input tokens by roughly 90% with no
  loss in rating quality.

## Tech stack

Python (Flask, `requests`, BeautifulSoup, `sqlite3`), vanilla JS/HTML/CSS (no build step, no
framework), Anthropic / Google Gemini / OpenAI SDKs, Google Drive & Docs APIs (OAuth), SQLite,
a Manifest V3 Chrome extension, `launchd` for scheduled runs.

## Running it

```bash
pip install -r requirements.txt
python job_checker.py                 # web app at http://localhost:5000
python job_checker.py --dev           # isolated dev data file, for safe local testing
python job_checker.py --get-new-jobs  # run the full ingestion pipeline once and exit
```

Configuration lives in a gitignored `.env` (Anthropic/Gemini/OpenAI API keys, Adzuna credentials,
a Google Drive output folder ID). Google Docs/Drive integration needs a one-time OAuth setup
(`python job_checker.py --google-auth`). See `CLAUDE.md` for the full configuration and
architecture reference this project's own AI-assisted development workflow is built on, and
`PROBLEMS.md` for the running log of design problems worked through along the way.
