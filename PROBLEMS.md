# Problems to Think Through

Running list of open design problems for Goat Search — things that need real thought before
(or instead of) writing code, as opposed to bugs or straightforward features. Add to this as
they come up; check items off (with a one-line note on how it was resolved) rather than
deleting them.

---

## 1. "Remote" detection is too loose

**Status:** open

**Current behavior:** `detect_remote()` in `job_checker.py` regex-matches the job title and
location string against terms like "remote", "anywhere", "distributed", "work from home",
"fully remote". It's a string match on title/location only — it never looks at the job
description.

**The actual problem:** companies use "remote" loosely. A role that expects onsite presence
every Friday, or "remote-friendly" meaning 2-3 days/week in office, matches the current regex
just as well as a role with no onsite expectation at all. These are not the same job to the
user.

**What "truly remote" means here:**
- No expectation of being near an office / no regular onsite days
- Occasional travel (client visits, offsites) — fine
- Occasional required onsite trips, e.g. every few months — fine
- Regular/weekly onsite expectation, or any hybrid schedule — NOT remote, should be excluded

**Why this is hard:** the distinction above can't be reliably determined from the title or a
location string — it requires reading the actual job description body and understanding
phrasing like "hybrid," "X days in office," "occasional travel to HQ," etc. Keyword matching
on the JD body would likely just move the false-positive problem rather than solve it (e.g. a
JD that says "this is not a remote position" would still contain the word "remote").

**Real example (found 2026-07-14, Airbnb — Product Manager, Incubations):** the JD body reads:
> "Due to the nature of this position, the successful applicant will need to be based in San
> Francisco, CA to be able to conduct their work."
No mention of "hybrid" or "onsite" — this reads as a plain relocation/location requirement, not
a schedule note, but it should still disqualify the job from the "remote" tag. This is a good
concrete test case for whatever classification approach gets built: it has none of the obvious
tells (no "hybrid," no "X days/week," no "in-office") and would slip past both the current
title/location regex and any naive keyword-based JD scan.

**Likely direction:** this is the same shape of problem as **Phase 2** in the agent roadmap
(autonomous triage — Claude reads each JD and produces a judgment with reasoning). Remote
classification could be the first real slice of that phase: feed Claude the full job
description and ask it to classify remote-ness against the definition above, with reasoning,
rather than trying to hand-tune a better regex.

**Open questions before building:**
- Full JD text currently is only fetched on-demand during application generation
  (`fetch_job_description()`), not at scrape/sweep time. Classifying remote-ness for every job
  would mean fetching every JD's full text up front — need to think about the added latency/cost
  of doing that for every scrape or Adzuna sweep vs. classifying lazily (e.g. only when the user
  opens a job, or in a batch pass).
- Cost: if this uses a Claude call per job, that's a real per-job cost multiplied across every
  company-board scrape and Adzuna sweep result. Worth thinking about batching, caching the
  judgment once made, or a cheap pre-filter before an LLM call.
- Where does the judgment live — replace `is_remote` in place, or add a separate field
  (`remote_confidence` / `remote_reasoning`) so the user can see *why* something was flagged
  in/out, and override it if the judgment is wrong?

---

## 2. Duplicate listings across cities (Adzuna)

**Status:** open

**The problem:** the same underlying job description is often posted to Adzuna multiple times,
once per city/location — same title, same company, same JD body, but a distinct Adzuna listing
(distinct `id`, distinct `location`, and likely a distinct redirect URL) per city. Today's dedup
in `sweep_adzuna_jobs()` / `api_add_from_search` is URL-based only, so each city variant of the
same job gets added as its own separate tracked job.

**Resolution rule (user-specified):** when a cluster of duplicate listings is found for a job
that looks promising:
1. If any of the duplicate listings' cities are in the Eastern time zone (EST/EDT), keep only
   the one with the **northernmost** such city.
2. If none of the cities are in Eastern time, keep only the one with the **eastern-most** US
   city among all the duplicates.
Discard the other duplicate entries entirely — one tracked job per underlying JD, not one per
city.

**Open questions before building:**
- **Duplicate detection:** what counts as "the same job description"? Likely candidates: exact
  match on `(company, title, description text)`, or company + title with fuzzy/normalized
  description comparison (Adzuna descriptions are truncated/formatted slightly differently
  per listing in some cases). Needs a real look at actual duplicate examples before picking one.
- **"Looks promising":** this rule is scoped to jobs worth keeping, implying dedup happens
  after (or alongside) keyword/title matching — not against every raw Adzuna result. Ties into
  the existing `score_job()` matching and possibly the Phase 2 triage judgment from problem #1.
- **Geography lookup:** "northernmost EST city" and "eastern-most US city" both require actual
  coordinates (latitude for northernmost, longitude for eastern-most) and a timezone per city —
  Adzuna only gives a location display name (e.g. "New York, NY"), not lat/long or timezone.
  Needs either a static US-city → (lat, long, timezone) lookup table, or a geocoding step.
  Note the standard US timezone-by-state approximation has real exceptions (parts of Michigan,
  Indiana, Kentucky, Tennessee, Florida panhandle sit in Central despite being "eastern" states),
  so a naive state-based guess will misclassify some cities.
- **Where this runs:** probably needs to sit in `sweep_adzuna_jobs()` (grouping candidates by
  duplicate cluster before adding), not as a later cleanup pass — otherwise all city variants
  get tracked as separate `is_new` jobs first and the user sees the duplication before any
  cleanup happens.

---

## 3. Duplicate postings across time (re-posted roles)

**Status:** open

**The problem:** the same role can get posted again later under a new listing (new URL / new
req ID / new Adzuna listing id) — e.g. a company closes and re-opens a req, or re-lists on
Adzuna weeks later. Today's identity model doesn't catch this: company-scraped jobs are keyed
by `md5(company_id|title|url)` (`make_job_id`) and Adzuna-added jobs get a fresh `uuid4` — both
keyed in a way that a new URL means a "new" job, even when it's actually the same role we
already looked at. Whatever expensive per-job evaluation gets built (remote classification from
problem #1, Phase 2 triage) would re-run on it, wasting API calls on something we already judged.

**Resolution rule (user-specified):** maintain a "master list" of job opportunities recording
**company, job title, and date posted**. If a company posts a job title matching one we've
already evaluated within the last month, skip re-evaluating it.

**Open questions before building:**
- **This is really about the evaluation ledger, not the job-tracking list.** `job_data.json`'s
  `jobs` array already accumulates every company-scraped job ever seen (never deleted, just
  `is_new` toggled) — but it's keyed by URL-derived id, so it won't recognize a same-role
  repost under a new URL as "the same." The master list here needs identity keyed on
  `(company, normalized title)` instead, separate from (or layered on top of) the existing
  per-listing job records.
- **Title matching:** exact string match, or normalized/fuzzy (e.g. "Senior Product Manager"
  vs. "Sr. PM, Growth" for what's arguably the same role)? Same open question as the
  description-matching question in problem #2 — worth solving both with one approach if
  possible.
- **Window semantics:** "evaluated in the last month" — 30 days rolling from the evaluation
  date of the prior match, presumably, not from the new posting's date. Needs to be explicit
  once built.
- **What "skip" means:** does a recognized repost (a) never get added to the tracked list at
  all, (b) get added but silently inherit the prior evaluation's verdict without re-running it,
  or (c) get added and visibly flagged as "already evaluated N days ago, see prior judgment"?
  (c) seems most consistent with staying human-in-the-loop-friendly, but worth confirming.
- **Directly coupled to Phase 2 (autonomous triage):** this evaluation ledger only has teeth
  once there's an actual expensive per-job evaluation step to guard (the Claude triage judgment
  planned for Phase 2, and/or the remote classification from problem #1). Worth designing the
  ledger schema together with Phase 2 rather than building it standalone first.

---

## 4. "Product Manager" jobs that aren't software PM roles

**Status:** open

**The problem:** title/keyword matching on "product manager" pulls in PM roles for physical or
non-software products — the title alone doesn't distinguish a software PM role from a PM role
at a company that just happens to also need a "product manager." Any product can have a
product manager (toasters, table saws, eyewear); the user is specifically a PM of software
products and doesn't want to spend time reading JDs for roles with no significant software
component, regardless of the company's industry.

**Real example (found 2026-07-14):** the Barti "Product Manager" listing is for eyewear, not
software.

**Framing, confirmed by user:** this is role-content-based, not company-industry-based — the
question is "does this specific role involve managing software with a significant software
component," not "is this a software company." A non-software company can still have a genuine
software PM role (internal tools, a companion app); a "tech" company can have a PM role that
isn't software-facing (e.g. a hardware-only device PM). Judge the role, not the company.

**Signals the user wants used (from the JD body):**
- mentions working/partnering with engineers
- mentions building software / applications / data platforms
- lists required technical skills

**Why this is hard / open questions:**
- **Same shape as problem #1 again:** another JD-content classification problem, same
  mechanics (needs full JD text, same fetch-cost/timing questions, same "where does the
  judgment live" question). This, problem #1 (remote), and the Phase 2 apply/skip/maybe triage
  judgment all boil down to "read the JD once, extract a judgment" — worth designing **one**
  JD-classification pass that returns remote-ness, software-PM-ness, and the Phase 2 judgment
  together, rather than three separate Claude calls per job.
- **False negative risk:** a real software PM job might not explicitly say "engineers" or list
  technical skills if the JD is poorly written or very senior/strategic in framing. Worth
  deciding how conservative to be — err toward excluding borderline cases, or toward keeping
  them with a lower confidence flag for the user to judge?

---
