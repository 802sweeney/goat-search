# Problems to Think Through

Running list of open design problems for Goat Search — things that need real thought before
(or instead of) writing code, as opposed to bugs or straightforward features. Add to this as
they come up; check items off (with a one-line note on how it was resolved) rather than
deleting them.

---

## 1. "Remote" detection is too loose

**Status:** built (2026-07-15) — see `classify_job()` in `job_checker.py`, called from
`evaluate_candidates()`. One Haiku call per new candidate reads the full JD and returns
`remote: yes|no|unclear` with reasoning, using the exact definition below. Verified against
the real Airbnb example — correctly flagged not-remote despite no "hybrid"/"onsite" keyword.
A failed JD fetch or API call leaves the job with `remote_status: 'unknown'` rather than
excluding it. Cost question resolved by combining this with problem #4's check into one call
(see that item) and by only running it on candidates that already passed the cheap
title-keyword/dedup filters in `evaluate_candidates()` — not on every raw scrape/search result.

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

**Status:** built (2026-07-15) — see `STATE_RANK` / `parse_state` / `state_rank` and the
location-cluster dedup step in `evaluate_candidates()`. The geocoding question below was
resolved with a shortcut: a single hand-authored state preference list (New England first,
then the rest of Eastern time north-to-south, then Central/Mountain/Pacific fanning out) instead
of real lat/long/timezone data — no geocoding dependency needed. Duplicate detection uses exact
normalized `(company, title)` match within a single check/sweep batch (not fuzzy matching — see
open question below, still unresolved). Scope also broadened beyond just Adzuna: this now runs
on candidates from any source, since company boards get the same treatment.

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

**Remaining open question:**
- **Fuzzy title/description matching:** the shipped version uses exact normalized
  `(company, title)` matching only — "Senior Product Manager" and "Sr. PM, Growth" for what's
  arguably the same underlying job won't be caught. Not addressed yet; revisit if this turns
  out to be common in practice.

**Resolved (see "Built" note above):** geography lookup (state-rank shortcut), where this runs
(inside `evaluate_candidates()`, before candidates are ever added — so the user never sees
duplicate city variants land in the tracked list, matching the original goal of not surfacing
the duplication in the first place).

---

## 3. Duplicate postings across time (re-posted roles)

**Status:** built (2026-07-15) — see the recent-repost check in `evaluate_candidates()`
(`RECENT_REPOST_DAYS = 30`). Resolved the "ledger" question by *not* building a separate ledger
— scans existing `data['jobs']` for a normalized `(company, title)` match with `first_seen`
inside the last 30 days, since jobs are never deleted from that list anyway. "Skip" was resolved
as option (a) from the list below: a recognized repost is excluded before ever being added, with
`reason: 'recent_repost'` in the review panel (not silently hidden — visible there, with an
"already tracked" note, same as other dedup reasons).

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

**Remaining open question:**
- **Title matching:** same exact-normalized-match limitation as problem #2 — a genuinely
  reworded repost ("Senior Product Manager" vs. "Sr. PM, Growth") won't be caught. Revisit
  alongside #2 if this comes up in practice.

**Resolved (see "Built" note above):** no separate ledger needed; window is 30 days from the
prior match's `first_seen`; "skip" means excluded-before-adding with a visible reason.

---

## 4. "Product Manager" jobs that aren't software PM roles

**Status:** built (2026-07-15) — combined into the same `classify_job()` call as problem #1
(one JD read, two judgments, as planned below). Verified against the real Barti eyewear example
— correctly flagged not-software-relevant, with reasoning citing the physical-product/supply-chain
focus and absence of technical requirements. Role-content framing (not company-industry) is
exactly what's implemented — the prompt explicitly instructs judging the role, not the company.

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

**Remaining open question:**
- **False negative risk:** a real software PM job might not explicitly say "engineers" or list
  technical skills if the JD is poorly written or very senior/strategic in framing. Currently
  errs toward excluding on a clear "no" verdict; the excluded-review panel with reasoning is the
  mitigation (user can "+ Add anyway" and see *why* it was excluded) rather than a confidence
  threshold. Revisit if false negatives turn out to be common.
- Still not done: Phase 2's actual apply/skip/maybe triage judgment (this and problem #1 cover
  remote/software screening, not the fuller "should I apply" assessment).

---

## 5. Apply/generate flow doesn't check for a failed job-description fetch

**Status:** open

**Found:** 2026-07-15, while investigating a related bug (see below) at the user's prompt —
"I've seen Adzuna returns errors when trying to scrape the text."

**The bug that's already fixed:** `fetch_job_description()` never checked the HTTP status code,
so when a job posting URL returned a bot-block page instead of the real posting — confirmed
happening for real: **3 of the first 10 real Adzuna-sourced job URLs tested came back HTTP 403**
("Access Denied") — the function silently treated the block page's text as if it were the real
job description. Fixed in `fetch_job_description()`: now checks `resp.status_code >= 400`, plus
a secondary check (`_BLOCK_PAGE_RE`) for soft-blocks that return HTTP 200 with a short
"just a moment" / "access denied" / "verify you are human" style body instead of real content.
Both cases now return a `'(Could not fetch job description: ...)'` string instead of garbage.

**What's still open:** that fix only helps the *new* `evaluate_candidates()` classification path,
which already checks for the `'(Could not fetch'` prefix before calling Claude. The **existing**
apply/generate flow — `api_preview_application` and `api_generate_application` in
`job_checker.py` — also calls `fetch_job_description()` but feeds the result straight into
`generate_application_docs()` (a real Sonnet call, ~$0.05) with no check at all. Before the fetch
fix, this meant cover letters/resumes were silently being generated from bot-block-page garbage
text. After the fix, it's somewhat better (Claude would get the honest string
`"(Could not fetch job description: HTTP 403)"` instead of garbage) but still wastes a real API
call generating a useless document instead of telling the user upfront that the JD couldn't be
fetched.

**Fix needed (not yet done):** in `api_preview_application` / `api_generate_application`, check
`job_description.startswith('(Could not fetch')` right after the `fetch_job_description()` call
and return a clear error to the user (e.g. "couldn't fetch this job's description — paste it in
manually") instead of proceeding to generate. Given ~30% of the small real sample tested came
back blocked, this isn't a rare edge case for Adzuna-sourced jobs specifically.

---

## 6. 6am scheduled run may not finish before the user's 7am check-in

**Status:** partially resolved — a more fundamental blocker found and fixed 2026-07-16; the
original runtime-risk concern below is still open and unverified.

**Update 2026-07-16:** the first real morning after moving to 06:00, the Screened tab was
completely empty and `launchctl print` showed the job had fired **zero times**. Root cause
wasn't runtime at all — the Mac was asleep at 6am (confirmed via `pmset -g log`: it woke at
10:27 AM, hours after the scheduled trigger). `launchd`'s `StartCalendarInterval` does not wake
a sleeping Mac by default; it just silently never fires. Fixed with
`sudo pmset repeat wake MTWRFSU 05:55:00` (run manually by the user in a real terminal — this
session's shell has no TTY for sudo's password prompt), confirmed via `pmset -g sched` showing
"wake at 5:55AM every day". This should give the job 5 minutes of headroom to be fully awake
before the 06:00 trigger, including when the lid is closed, as long as the Mac has power.
**This means the very first data point on the original runtime-risk question below is still
missing** — tomorrow morning (2026-07-17) will be the first real test of both fixes together
(wake schedule + backfill-lag buffer). Check the Screened tab and `logs/get_new_jobs.log` then.

**Original concern, still open:** the nightly `--get-new-jobs` run was originally scheduled for 00:15, giving
~7 hours of runway before the user checks the app around 7am — comfortable slack even for a
long run. On 2026-07-16, it moved to **06:00** to fix a separate problem (Adzuna's search index
has backfill lag — see `CLAUDE.md`'s "Scheduling" section and `SWEEP_LOOKBACK_BUFFER_HOURS` for
the fix). That reduced the available runway to **~1 hour**.

**The risk:** a real run processes every new candidate sequentially — JD fetch, Claude
classification, and (for confirmed remote+relevant jobs) a fit-rating call, plus a mandatory
10-second delay per Adzuna candidate (`ADZUNA_SCRAPE_DELAY_SECONDS`) to avoid tripping Adzuna's
bot protection. A single night with many qualifying candidates could plausibly still be running
past 7am. This hasn't been stress-tested against real full-scale nightly volume yet — the one
real 6am-adjacent run seen so far (still under the old 00:15 schedule) found 0 qualifying
candidates, so it doesn't tell us anything about worst-case runtime.

**What to watch for:** if the user reports the app showing "checking..." (or stale/incomplete
results) when they sit down in the morning, this is the likely cause.

**Possible fixes, not yet chosen:**
- Push the start time earlier (e.g., 05:00) while keeping the 6h lookback buffer — restores
  more runway without reintroducing the backfill-lag problem, as long as the buffer stays ≥
  whatever Adzuna's actual indexing lag turns out to be.
- Increase `SWEEP_LOOKBACK_BUFFER_HOURS` and correspondingly move the schedule even earlier.
- Reduce `ADZUNA_SCRAPE_DELAY_SECONDS` if it turns out to be more conservative than necessary
  (currently untuned — picked as "generous," not measured against Adzuna's actual limits).
- Parallelize candidate classification (currently strictly sequential) — bigger change, only
  worth it if the above tuning isn't enough.

---

## 7. Haiku misclassifies at least one real nationwide-remote-eligibility notice as "not remote"

**Status:** resolved 2026-07-20 — went with the "Sonnet-verify only on rejections" option below,
built as a manually-triggered "Re-evaluate" action rather than an automatic double-check on
every rejection (keeps the added cost proportional to how often the user actually reviews the
Excluded tab, not to total rejection volume). See `reevaluate_job()` in `job_checker.py` and
CLAUDE.md's "Sonnet re-evaluation" section. The underlying Haiku capability gap itself is
unchanged (still a real weakness, per the "What was tried" section below) — this doesn't fix
Haiku, it gives the user a one-click way to catch it when it happens.

**Found:** 2026-07-16, via the Chrome extension. Captured an Evolve listing; the extension badge
showed excluded (yellow), reason `not_remote`. The JD's actual text: "We can hire from anywhere
in the U.S. except D.C. and Hawaii." — no list of approved locations, no onsite/hybrid language
at all. The user's own read agreed this is unambiguously nationwide-remote; Haiku (via
`classify_job()`) flagged it not-remote anyway.

**What was tried:** two prompt-tightening passes on the REMOTE criteria in `CLASSIFY_CRITERIA`
(`job_checker.py`), the second explicitly calling out that a hiring-eligibility exclusion list
("we can hire from anywhere except X, Y") is a standard payroll/legal notice, not an onsite
requirement. Re-tested against the real Evolve text both times — **both attempts still
misclassified it**. A direct side-by-side test using Sonnet instead of Haiku on the identical
text and prompt got it right. Conclusion: this looks like a genuine Haiku capability gap on this
kind of text, not something fixable with prompt wording alone — at least not with what's been
tried so far.

**Why this matters for cost:** `classify_job()` runs automatically, via Haiku, on every new
candidate that survives the cheap pre-filters (`evaluate_candidates()`) — this is the load-bearing
classification step for the entire pipeline, not a rare manual action. Switching it to Sonnet
(or adding a Sonnet double-check specifically on rejections) is a real, ongoing per-job cost
increase, not a one-time fix.

**Mitigation built first, then the actual fix:** the persistent Excluded tab
(`exclusion_reason`/`exclusion_reasoning` on job records — see CLAUDE.md's "Found / Screened /
Excluded sub-tabs" section) gave the user visibility into real rejection volume/patterns first.
Once that showed the two judgment-call reasons (`not_remote`/`not_software`) specifically —
not the two keyword-filter reasons, which don't involve any model judgment to double-check — the
"Re-evaluate" button was built to send exactly those to Sonnet on demand. It reuses the same
`_apply_classification()` every other classification path already uses, so a flipped verdict
gets a real fit rating and moves to Found/Screened like any other job, not a special-cased
"restored" state. "Restore to Found" still exists, but now only for the two keyword-filter
reasons, where there's no model verdict to re-check in the first place.

---

## 8. Adzuna returned 503 during the scheduled sweep two mornings running

**Status:** mitigated 2026-07-22 — a one-retry-after-5-minutes fix built into
`sweep_adzuna_jobs()`; not yet confirmed whether it's actually enough, since it hasn't hit a
third occurrence yet to test against.

**Found:** the user noticed zero new jobs for several days running (a Wednesday check found
nothing all week). Checked `last_get_jobs_result` for the two most recent scheduled 06:00 runs —
both had a `503 Server Error: Service Temporarily Unavailable` from `api.adzuna.com` for every
query, both mornings. Manually re-tested the same Adzuna endpoint immediately after each
discovery — healthy both times (`200`, real results), meaning the outage was brief and specific
to that morning window, not a sustained problem. Because `last_adzuna_sweep` only advances on a
zero-error sweep, each failed morning meant retrying from the *same* starting point at the *next*
scheduled run — 24 hours later — rather than catching up sooner, so two consecutive failures
compounded into most of a week with no new Adzuna-sourced jobs at all.

**Fix:** each per-page Adzuna request in the sweep now retries once after a 5-minute wait before
being recorded as a failure (`ADZUNA_RETRY_WAIT_SECONDS`, job_checker.py). Deliberately
sweep-only, not added to the shared `_adzuna_request()` helper the manual Search tab also uses —
an interactive request should fail fast, not hang the UI for 5 minutes.

**Open question:** two occurrences isn't enough data to know if a single 5-minute retry is
actually sufficient, or if this is symptomatic of something recurring around the 06:00 window
specifically (worth cross-checking against `PROBLEMS.md` #6's Mac-wake-timing concern, though
that one's about *this app* being slow to start, not Adzuna's own service). If it happens a
third time even with the retry in place, that's a real signal the fix isn't enough — consider a
longer wait, more than one retry, or moving the schedule earlier/later to dodge whatever's
happening on Adzuna's side at that time.

---
