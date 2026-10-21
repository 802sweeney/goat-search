#!/usr/bin/env python3
"""
Job Checker - Track job postings across company career pages

Usage:
  python job_checker.py          # Start web app at http://localhost:5000
  python job_checker.py --check  # Run a check and exit (for scheduled tasks)
  python job_checker.py --port 8080  # Use a different port
"""

import json
import os
import hashlib
import threading
import argparse
import tempfile
import re
import uuid
import math
import time
import csv
import io
import sqlite3
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# Load .env from project directory
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except ImportError:
    pass

try:
    import anthropic as anthropic_sdk
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    from google import genai as gemini_sdk
    from google.genai import types as gemini_types
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False

try:
    from openai import OpenAI as openai_sdk
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    HAS_SCHEDULER = True
except ImportError:
    HAS_SCHEDULER = False

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Overridable so dev/test runs never touch the real tracked-job list — see --dev / --data-file.
DATA_FILE = os.environ.get('JOB_DATA_FILE') or os.path.join(BASE_DIR, 'job_data.json')
# Long-term daily metrics (2026-10-07) — see "Metrics database" section below. Dev-isolated the
# same way as DATA_FILE (--dev / --data-file), since the writers below fire from the same sweep/
# generation code paths that are already dev-isolated — without this, a --dev test run would
# write real rows into the production metrics history, same class of mistake the standing
# dev/prod isolation rule exists to prevent for job_data.json.
METRICS_DB_FILE = os.environ.get('JOB_METRICS_DB_FILE') or os.path.join(BASE_DIR, 'metrics.db')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}

# OAuth scopes for live Google Doc generation (see _get_google_creds()). Needs the full 'drive'
# scope, not the narrower 'drive.file' — drive.file only grants access to files the app itself
# creates or that get opened through a Drive file-picker UI, neither of which applies here: the
# template is a pre-existing Doc the user just pastes a URL for, so the app needs to be able to
# read/copy an arbitrary existing file by ID. 'documents' scope is for the actual text-fill via
# the Docs API on the copy once it's created.
GOOGLE_OAUTH_SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/documents',
]

CAREER_DOCS_DIR = os.path.join(BASE_DIR, 'career_docs')

app = Flask(__name__, static_folder=BASE_DIR)
CORS(app)

# Track in-progress check to avoid concurrent runs
_check_lock = threading.Lock()
_check_running = False

# Track in-progress Adzuna sweep to avoid concurrent runs
_sweep_lock = threading.Lock()
_sweep_running = False

_remote_com_sweep_lock = threading.Lock()
_remote_com_sweep_running = False

_jobicy_sweep_lock = threading.Lock()
_jobicy_sweep_running = False

_himalayas_sweep_lock = threading.Lock()
_himalayas_sweep_running = False

_workable_sweep_lock = threading.Lock()
_workable_sweep_running = False

# Scheduler instance (set when server starts)
_scheduler = None
DAILY_CHECK_HOUR = 9   # 9 AM local time

# ─────────────────────────────────────────────
# Data layer
# ─────────────────────────────────────────────

DEFAULT_PROMPT_TEMPLATE = """I want to apply to **{job_title}** at **{company_name}**.

**Job posting:** {job_url}

Please help me prepare my application:

1. Fetch and read the full job description from the URL above
2. Read my reference materials from Google Drive — look in "My Drive / {drive_folder}" for my resume, bio, past cover letters, and any other career docs
3. Write a tailored **cover letter** for this specific role — match my experience to their needs, use my existing voice and style
4. Tailor my **resume** to highlight the most relevant experience for this role
5. Create a **Google Doc** for the cover letter titled "Cover Letter — {job_title} at {company_name}"
6. Create a **Google Doc** for the resume titled "Resume — {job_title} at {company_name}"
7. Share the links to both docs when done"""

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            data = json.load(f)
        # Migrate old single 'keywords' field to split fields
        if 'keywords' in data and 'title_keywords' not in data:
            data['title_keywords'] = data.pop('keywords')
            data['desc_keywords'] = []
        if 'title_exclude_keywords' not in data:
            data['title_exclude_keywords'] = []
        if 'settings' not in data:
            data['settings'] = {}
        if 'last_adzuna_sweep' not in data:
            data['last_adzuna_sweep'] = None
        if 'last_adzuna_sweep_result' not in data:
            data['last_adzuna_sweep_result'] = None
        if 'last_remote_com_sweep' not in data:
            data['last_remote_com_sweep'] = None
        if 'last_remote_com_sweep_result' not in data:
            data['last_remote_com_sweep_result'] = None
        if 'last_get_jobs_at' not in data:
            data['last_get_jobs_at'] = None
        if 'last_get_jobs_result' not in data:
            data['last_get_jobs_result'] = None
        return data
    return {
        'companies': [],
        'title_keywords': [],
        'desc_keywords': [],
        'title_exclude_keywords': [],
        'jobs': [],
        'seen_job_ids': [],
        'last_checked': None,
        'last_check_errors': [],
        'last_adzuna_sweep': None,
        'last_adzuna_sweep_result': None,
        'last_remote_com_sweep': None,
        'last_remote_com_sweep_result': None,
        'last_get_jobs_at': None,
        'last_get_jobs_result': None,
        'settings': {}
    }

def save_data(data):
    """
    Write atomically (2026-09-23, real production bug: two concurrent requests — a generate
    call's own end-of-request save_data() and the near-simultaneous fire-and-forget
    POST /api/jobs/<id>/apply that startGeneration() sends right alongside it — raced on this
    exact file. `open(DATA_FILE, 'w')` truncates to 0 bytes the instant it's called, before any
    content is written; a concurrent load_data() landing in that window read an empty file and
    threw `json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)`, surfacing to
    the user as a real "Generation failed / INTERNAL SERVER ERROR" even though generation itself
    had already succeeded — GET /api/applications, called right after to refresh the list, was
    what actually crashed. Confirmed via two real tracebacks in production, both this exact error,
    both immediately adjacent to a concurrent write.

    Fixed the standard way: write the full content to a temp file in the same directory first,
    then os.replace() it over the real path. os.replace() is atomic at the filesystem level on
    both POSIX and Windows — any concurrent reader now either sees the complete old file or the
    complete new one, never a partial/empty one. A unique temp filename (tempfile.mkstemp, not a
    fixed '.tmp' suffix) means two overlapping save_data() calls can't even clobber each other's
    temp file mid-write; the last os.replace() to run still simply wins, same "no real locking,
    last write wins" behavior this app's read-modify-write pattern already had elsewhere — this
    fix targets the empty-file read specifically, not that broader, already-accepted limitation.
    """
    directory = os.path.dirname(os.path.abspath(DATA_FILE)) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.job_data_', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, DATA_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

# ─────────────────────────────────────────────
# Metrics database (SQLite, 2026-10-07)
# ─────────────────────────────────────────────
# Long-term dashboard metrics (funnel counts, token usage/cost) used to live as ad hoc cumulative
# counters bolted onto job_data.json (data['lifetime_stats'], data['lifetime_token_usage']) —
# a real, growing pain by the second instance: no query structure, no daily granularity (so no
# trend-over-time), and every fix needing hand-written joins against separate JSON sources.
# This is a real, if small, lightweight SQLite database instead — a single file, Python's
# built-in sqlite3 module (no new dependency, consistent with this app's minimal-tooling
# philosophy), one row per (date, metric) rather than one cumulative number — so a trend chart
# ("cost this week vs last") is a GROUP BY away instead of a structural rewrite later.
#
# Deliberately NOT a replacement for job_data.json or the applications/ disk tree — those stay
# exactly as they are (current-state working sets, permanent per-application records). This is
# purely additive: a dedicated home for metrics that are fundamentally about "how much happened,
# over time," which a single mutable JSON blob was never a good fit for.
#
# data['lifetime_stats'] / data['lifetime_token_usage'] are left in place as a frozen,
# pre-2026-10-07 baseline (nothing new is written to them after this change) rather than migrated
# — they have no internal date breakdown to redistribute across days, so backfilling them into
# this schema would mean fabricating dates for real history. GET /api/lifetime-stats and
# GET /api/costs add this baseline on top of the database's own sums instead, so the all-time
# total stays complete without inventing precision the old data never had. Token usage for
# generation/cover_letter_audit/todo_matching/interview_prep, by contrast, *does* have a real
# date for every historical entry (each application's own sections.json/interview_prep.json
# 'generated_at') — see backfill_metrics_db() below, run once on 2026-10-07 to recover real
# multi-week trend history from data that was already sitting on disk.

def _metrics_db() -> sqlite3.Connection:
    """
    Open a connection to the metrics database, creating the schema on first use. A fresh
    connection per call rather than a pooled/module-level one — matches load_data()/save_data()'s
    own per-request-open-close pattern, which is the right call at this app's single-user, low-
    concurrency scale (no benefit to pooling, and it avoids ever holding a stale connection across
    a --dev/--data-file path change).
    """
    conn = sqlite3.connect(METRICS_DB_FILE)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS daily_funnel (
            date TEXT NOT NULL,
            metric TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date, metric)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS daily_rejections (
            date TEXT NOT NULL,
            reason TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date, reason)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS daily_token_usage (
            date TEXT NOT NULL,
            part TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            calls INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date, part, model)
        )
    ''')
    return conn


def _record_daily_metric(metric: str, count: int, date: str = None) -> None:
    """Increment today's (or a given date's) count for a simple named funnel metric — currently
    'presented', 'applications_generated', 'interview_rounds_generated'. No-ops on a non-positive
    count so a zero-candidate sweep run doesn't write a pointless row."""
    if count is None or count <= 0:
        return
    date = date or datetime.now().strftime('%Y-%m-%d')
    conn = _metrics_db()
    try:
        conn.execute('''
            INSERT INTO daily_funnel (date, metric, count) VALUES (?, ?, ?)
            ON CONFLICT(date, metric) DO UPDATE SET count = count + excluded.count
        ''', (date, metric, count))
        conn.commit()
    finally:
        conn.close()


def _record_daily_rejections(rejected_by_reason: dict, date: str = None) -> None:
    """Increment today's (or a given date's) per-reason exclusion counts — mirrors
    _excluded_by_reason()'s output shape directly, one row per reason actually present."""
    if not rejected_by_reason:
        return
    date = date or datetime.now().strftime('%Y-%m-%d')
    conn = _metrics_db()
    try:
        for reason, count in rejected_by_reason.items():
            if not count:
                continue
            conn.execute('''
                INSERT INTO daily_rejections (date, reason, count) VALUES (?, ?, ?)
                ON CONFLICT(date, reason) DO UPDATE SET count = count + excluded.count
            ''', (date, reason, count))
        conn.commit()
    finally:
        conn.close()


def _record_daily_token_usage(part: str, model_key: str, usage: dict, date: str = None) -> None:
    """
    Increment today's (or a given date's) token usage for a (part, model) pair. Called from the
    same two choke points that already maintain job['token_usage'] (_add_token_usage()) and
    data['lifetime_token_usage'] (_roll_up_job_token_usage_before_purge()), so every existing
    call site gets daily tracking for free with no changes needed anywhere else. Skips silently
    on a falsy model_key or usage — same "gaps are fine" tolerance _add_token_usage() already
    uses, rather than bucketing under a fake 'unknown' key.
    """
    if not model_key or not usage:
        return
    date = date or datetime.now().strftime('%Y-%m-%d')
    conn = _metrics_db()
    try:
        conn.execute('''
            INSERT INTO daily_token_usage (date, part, model, input_tokens, output_tokens, calls)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, part, model) DO UPDATE SET
                input_tokens  = input_tokens  + excluded.input_tokens,
                output_tokens = output_tokens + excluded.output_tokens,
                calls         = calls         + excluded.calls
        ''', (date, part, model_key, usage.get('input_tokens', 0), usage.get('output_tokens', 0),
              usage.get('calls', 1)))
        conn.commit()
    finally:
        conn.close()


def _metrics_db_summary() -> dict:
    """
    Read back everything the database currently holds, aggregated to the totals GET
    /api/lifetime-stats and GET /api/costs need — callers add data['lifetime_stats'] /
    data['lifetime_token_usage']'s frozen pre-database baseline on top of this themselves (see
    module note above for why that baseline isn't migrated into the schema).
    Returns {'presented', 'applications_generated', 'interview_rounds_generated',
    'rejected_by_reason': {reason: count}, 'token_by_model': {model: {input_tokens,
    output_tokens, calls}}, 'daily': [{'date', 'presented', 'applications_generated',
    'interview_rounds_generated'}, ...]} (daily, oldest first, for a future trend chart).
    """
    conn = _metrics_db()
    try:
        totals = {'presented': 0, 'applications_generated': 0, 'interview_rounds_generated': 0}
        daily = {}
        for date, metric, count in conn.execute('SELECT date, metric, count FROM daily_funnel'):
            if metric in totals:
                totals[metric] += count
            daily.setdefault(date, {'date': date, 'presented': 0, 'applications_generated': 0,
                                     'interview_rounds_generated': 0})
            daily[date][metric] = daily[date].get(metric, 0) + count

        rejected_by_reason = {}
        for date, reason, count in conn.execute('SELECT date, reason, count FROM daily_rejections'):
            rejected_by_reason[reason] = rejected_by_reason.get(reason, 0) + count

        token_by_model = {}
        for date, part, model, in_tok, out_tok, calls in conn.execute(
                'SELECT date, part, model, input_tokens, output_tokens, calls FROM daily_token_usage'):
            bucket = token_by_model.setdefault(model, {'input_tokens': 0, 'output_tokens': 0, 'calls': 0})
            bucket['input_tokens'] += in_tok
            bucket['output_tokens'] += out_tok
            bucket['calls'] += calls

        return {
            **totals,
            'rejected_by_reason': rejected_by_reason,
            'token_by_model': token_by_model,
            'daily': sorted(daily.values(), key=lambda d: d['date']),
        }
    finally:
        conn.close()


# ─────────────────────────────────────────────
# Scraping utilities
# ─────────────────────────────────────────────

REMOTE_TERMS = re.compile(
    r'\b(remote|anywhere|distributed|work from home|wfh|live and work anywhere|fully remote|globally remote)\b',
    re.IGNORECASE
)

def detect_remote(title: str, location: str) -> bool:
    return bool(REMOTE_TERMS.search(title) or REMOTE_TERMS.search(location))

def make_job_id(company_id: str, title: str, url: str) -> str:
    raw = f"{company_id}|{title.lower().strip()}|{url}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]

def score_job(title: str, description: str, title_keywords: list, desc_keywords: list) -> int:
    """
    Return a match score for a job (used at query time, not stored):
      2 = title matches a title keyword (highest weight)
      1 = title or description matches a description keyword
      0 = no keywords set (show everything)
     -1 = keywords set but nothing matched
    """
    has_any_keywords = bool(title_keywords or desc_keywords)
    if not has_any_keywords:
        return 0

    title_lower = title.lower()
    desc_lower = description.lower() if description else ''

    if title_keywords and any(kw.lower() in title_lower for kw in title_keywords):
        return 2

    if desc_keywords:
        searchable = title_lower + ' ' + desc_lower
        if any(kw.lower() in searchable for kw in desc_keywords):
            return 1

    return -1

# ─────────────────────────────────────────────
# Duplicate-location tie-breaking (PROBLEMS.md #2)
#
# Shortcut in place of real geocoding: a single hand-authored state preference
# order (New England first, fanning out) instead of exact lat/long + timezone
# lookups. State-level, not exact timezone — a few border counties (parts of
# MI/IN/KY/TN/FL) will be approximated. Accepted tradeoff.
# ─────────────────────────────────────────────

STATE_RANK = [
    # New England
    'ME', 'NH', 'VT', 'MA', 'RI', 'CT',
    # Rest of Eastern time, roughly north to south
    'NY', 'NJ', 'PA', 'DE', 'MD', 'DC', 'VA', 'WV', 'OH', 'NC', 'SC', 'GA',
    'MI', 'IN', 'KY', 'TN', 'FL',
    # Central time, east to west
    'WI', 'IL', 'MS', 'AL', 'IA', 'MO', 'AR', 'LA', 'MN', 'KS', 'NE', 'SD', 'ND', 'OK', 'TX',
    # Mountain
    'MT', 'WY', 'CO', 'NM', 'UT', 'AZ', 'ID',
    # Pacific + AK/HI
    'WA', 'OR', 'NV', 'CA', 'AK', 'HI',
]
_STATE_RANK_INDEX = {abbr: i for i, abbr in enumerate(STATE_RANK)}

_STATE_NAME_TO_ABBR = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR', 'california': 'CA',
    'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE', 'florida': 'FL', 'georgia': 'GA',
    'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA',
    'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
    'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT', 'vermont': 'VT',
    'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV', 'wisconsin': 'WI',
    'wyoming': 'WY', 'district of columbia': 'DC',
}

def parse_state(location: str) -> str:
    """Extract a US state abbreviation from a free-text location string, or '' if none found."""
    if not location:
        return ''
    for part in reversed([p.strip() for p in location.split(',')]):
        if part.upper() in _STATE_RANK_INDEX:
            return part.upper()
        if part.lower() in _STATE_NAME_TO_ABBR:
            return _STATE_NAME_TO_ABBR[part.lower()]
    return ''

def state_rank(location: str) -> int:
    """Lower is more preferred (New England first). Unparseable locations rank last."""
    return _STATE_RANK_INDEX.get(parse_state(location), len(STATE_RANK))

# ─────────────────────────────────────────────
# Accepted locations (2026-07-22) — settings.accepted_locations is an array of
# "{type}-{STATE}" strings, e.g. ["remote-VT", "hybrid-NH", "hybrid-MA", "hybrid-VT"].
# A "remote-X" entry isn't a new eligibility check — it just tells the classifier where the
# candidate actually lives, so it can correctly reason about location-conditional onsite
# requirements (e.g. "hybrid only for SF Bay Area residents" doesn't apply to someone in VT —
# verified live against a real misclassified Headspace posting). A "hybrid-X" entry means a
# genuinely hybrid (not fully onsite) role based in that state should be accepted, not excluded.
# ─────────────────────────────────────────────

def _parse_accepted_locations(settings: dict) -> tuple:
    """
    Parse settings.accepted_locations into (candidate_location, accepted_hybrid_states).
    candidate_location is the state from the (expected single) 'remote-X' entry, or '' if none
    configured. accepted_hybrid_states is the set of state abbreviations from every 'hybrid-X'
    entry. Malformed entries (no '-') are ignored rather than raising.
    """
    candidate_location = ''
    accepted_hybrid_states = set()
    for entry in (settings or {}).get('accepted_locations', []):
        if '-' not in entry:
            continue
        kind, state = entry.split('-', 1)
        kind = kind.strip().lower()
        state = state.strip().upper()
        if kind == 'remote' and state:
            candidate_location = state
        elif kind == 'hybrid' and state:
            accepted_hybrid_states.add(state)
    return candidate_location, accepted_hybrid_states


def is_hybrid_accepted(location: str, description: str, accepted_hybrid_states: set) -> str:
    """
    MVP deterministic hybrid check (2026-07-22, user-specified) — no AI judgment call: a job
    counts as accepted-hybrid if its location parses to one of the user's accepted hybrid states
    AND its description contains the word 'hybrid' (case-insensitive substring, consistent with
    this app's existing simple-matching style elsewhere, e.g. title_exclude_keywords). Returns
    the matched state abbreviation, or '' if not accepted.
    """
    if not accepted_hybrid_states:
        return ''
    state = parse_state(location)
    if state in accepted_hybrid_states and 'hybrid' in (description or '').lower():
        return state
    return ''


def _decide_remote_status(remote_verdict: str, location: str, description: str, settings: dict) -> tuple:
    """
    Turn classify_job()'s raw 'yes'/'no'/'unclear' remote verdict into the 3-way remote_status
    ('remote'/'hybrid'/'not_remote'/'unknown') plus the matched hybrid_state ('' if not
    applicable). A 'no' verdict gets one more chance via the deterministic is_hybrid_accepted()
    check before being finalized as not_remote. Shared by _apply_classification() and
    evaluate_candidates()'s own inline classification step, so the two never drift apart.
    """
    if remote_verdict == 'yes':
        return 'remote', ''
    if remote_verdict == 'no':
        _, accepted_hybrid_states = _parse_accepted_locations(settings)
        hybrid_state = is_hybrid_accepted(location, description, accepted_hybrid_states)
        if hybrid_state:
            return 'hybrid', hybrid_state
        return 'not_remote', ''
    return 'unknown', ''

# ─────────────────────────────────────────────
# ATS-specific scrapers
# ─────────────────────────────────────────────

def scrape_greenhouse(url: str, company_id: str, company_name: str) -> list:
    """Use the Greenhouse public API to fetch job listings."""
    board_match = re.search(r'greenhouse\.io/([^/?#]+)', url)
    if not board_match:
        return []
    board_token = board_match.group(1)
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    try:
        resp = requests.get(api_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        jobs = []
        for job in resp.json().get('jobs', []):
            title = job.get('title', '')
            job_url = job.get('absolute_url', url)
            location = job.get('location', {}).get('name', '')
            jobs.append({
                'id': make_job_id(company_id, title, job_url),
                'company_id': company_id,
                'company_name': company_name,
                'title': title,
                'url': job_url,
                'location': location,
                'is_remote': detect_remote(title, location),
                'source': 'greenhouse',
                # Greenhouse doesn't expose a distinct "first posted" date, just last-updated —
                # closest available signal, same convention as treating Adzuna's/remote.com's
                # 'created' as "posted" elsewhere in this app.
                'posted': (job.get('updated_at') or '')[:10],
            })
        return jobs
    except Exception as e:
        raise RuntimeError(f"Greenhouse API error: {e}")


def scrape_lever(url: str, company_id: str, company_name: str) -> list:
    """Use the Lever public API to fetch job postings."""
    company_match = re.search(r'lever\.co/([^/?#]+)', url)
    if not company_match:
        return []
    company_token = company_match.group(1)
    api_url = f"https://api.lever.co/v0/postings/{company_token}?mode=json"
    try:
        resp = requests.get(api_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        jobs = []
        for posting in resp.json():
            title = posting.get('text', '')
            job_url = posting.get('hostedUrl', url)
            location = posting.get('categories', {}).get('location', '')
            created_ms = posting.get('createdAt')
            posted = ''
            if created_ms:
                try:
                    posted = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
                except (ValueError, OSError):
                    pass
            jobs.append({
                'id': make_job_id(company_id, title, job_url),
                'company_id': company_id,
                'company_name': company_name,
                'title': title,
                'url': job_url,
                'location': location,
                'is_remote': detect_remote(title, location),
                'source': 'lever',
                'posted': posted,
            })
        return jobs
    except Exception as e:
        raise RuntimeError(f"Lever API error: {e}")


def scrape_ashby(url: str, company_id: str, company_name: str) -> list:
    """Use the Ashby public API to fetch job postings."""
    # jobs.ashbyhq.com/COMPANY -> api.ashbyhq.com/posting-api/job-board/COMPANY
    company_match = re.search(r'ashbyhq\.com/([^/?#]+)', url)
    if not company_match:
        return []
    company_token = company_match.group(1)
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{company_token}"
    try:
        resp = requests.get(api_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        jobs = []
        for job in data.get('jobPostings', []):
            title = job.get('title', '')
            job_url = job.get('jobPostingPath', '')
            if job_url and not job_url.startswith('http'):
                job_url = f"https://jobs.ashbyhq.com/{company_token}/{job_url.lstrip('/')}"
            location = job.get('locationName', '')
            jobs.append({
                'id': make_job_id(company_id, title, job_url),
                'company_id': company_id,
                'company_name': company_name,
                'title': title,
                'url': job_url or url,
                'location': location,
                'is_remote': detect_remote(title, location),
                'source': 'ashby',
                # Best-guess field name (Ashby's documented job-board field) — unverified against
                # a real response, none of the currently-tracked Ashby companies had live public
                # postings to check against. Degrades gracefully to '' if this is wrong, same as
                # not having a posted date at all — not worth blocking on confirming this.
                'posted': (job.get('publishedAt') or '')[:10],
            })
        return jobs
    except Exception as e:
        raise RuntimeError(f"Ashby API error: {e}")


def scrape_generic(url: str, company_id: str, company_name: str) -> list:
    """
    Generic HTML scraper. Looks for anchor tags that appear to be job listings
    based on href patterns and surrounding context.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"HTTP error: {e}")

    soup = BeautifulSoup(resp.text, 'html.parser')
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    JOB_HREF_PATTERNS = re.compile(
        r'/(job|career|position|opening|role|opportunity|posting|vacancy|req|requisition)',
        re.IGNORECASE
    )
    JOB_CONTEXT_PATTERNS = re.compile(
        r'(job|career|position|opening|role|opportunity|listing)',
        re.IGNORECASE
    )

    jobs = []
    seen_urls = set()

    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True)
        href = a['href']

        # Basic quality filters
        if not text or len(text) < 4 or len(text) > 120:
            continue
        skip_phrases = {'apply', 'learn more', 'see more', 'view all', 'read more',
                        'click here', 'here', 'back', 'next', 'previous', 'home'}
        if text.lower() in skip_phrases:
            continue

        href_lower = href.lower()
        is_job_href = bool(JOB_HREF_PATTERNS.search(href_lower))

        if not is_job_href:
            # Check surrounding element classes/ids for job-related terms
            context_els = [a.parent, a.parent.parent if a.parent else None]
            context_str = ' '.join(
                ' '.join(el.get('class', []) if el else []) + ' ' + (el.get('id', '') if el else '')
                for el in context_els if el
            )
            if not JOB_CONTEXT_PATTERNS.search(context_str):
                continue

        full_url = urljoin(base_url, href) if not href.startswith('http') else href
        if full_url in seen_urls or full_url == url:
            continue
        seen_urls.add(full_url)

        jobs.append({
            'id': make_job_id(company_id, text, full_url),
            'company_id': company_id,
            'company_name': company_name,
            'title': text,
            'url': full_url,
            'location': '',
            'is_remote': detect_remote(text, ''),
            'source': 'generic',
        })

    return jobs[:75]  # cap to avoid noise


# ─────────────────────────────────────────────
# remote.com job search — no public API, but search-result pages are server-rendered with real
# job links, so a plain scrape works (see sweep_remote_com_jobs() below for the full picture).
# ─────────────────────────────────────────────

REMOTE_COM_JOB_URL_RE = re.compile(r'^/jobs/([a-z0-9-]+)-c[a-z0-9]+/[a-z0-9-]+-j[a-z0-9]+$')
REMOTE_COM_MAX_PAGES = 5
REMOTE_COM_SCRAPE_DELAY_SECONDS = 3  # politeness pause between search-result page fetches —
                                      # remote.com's tolerance for repeated scripted access is
                                      # unverified beyond light manual testing, so err cautious,
                                      # same instinct as ADZUNA_SCRAPE_DELAY_SECONDS.

# ─────────────────────────────────────────────
# Jobicy — public, unauthenticated remote-jobs API (found 2026-08-03, prompted by a friend
# sending a Workable-hosted job at a small company that this app's existing sources didn't
# have visibility into; researching broader keyword-searchable sources led here). Confirmed
# live: `tag=` performs real server-side filtering (unlike Remotive's `search=`/`category=` and
# Arbeitnow's `tags=`, both tested and found to silently ignore the param and return their
# default unfiltered feed — not worth building against). RemoteOK was also tested and rejected:
# every job on the feed shares one identical, meaningless ~50-term boilerplate tag list rather
# than real per-job tags, so a `tags=` filter there is unusable. Jobicy's own job detail pages
# are behind Cloudflare (a plain fetch 403s, confirmed live) — but the listing API response
# already includes the full `jobDescription` HTML inline, so this doesn't block classification;
# see the `_fallback_jd_text` mechanism in evaluate_candidates(), the same one built for
# Adzuna's own unscrapeable-real-destination case.
# ─────────────────────────────────────────────

JOBICY_API_URL = 'https://jobicy.com/api/v2/remote-jobs'
JOBICY_COUNT_PER_QUERY = 50  # confirmed live: the API honors up to at least count=100; 50 is
                              # comfortably past what one title_keywords phrase actually returns
                              # day to day, without over-fetching on every sweep run.

def _fetch_jobicy_jobs(tag: str) -> list:
    """
    One request to Jobicy's public listing API for a given tag/keyword phrase. Returns raw
    candidates shaped for evaluate_candidates(), each carrying the API's own jobDescription
    (HTML stripped to text) in `_fallback_jd_text` — the only real JD text source for this
    source, since the job's own page 403s behind Cloudflare (see module note above).
    """
    resp = requests.get(JOBICY_API_URL, params={'count': JOBICY_COUNT_PER_QUERY, 'tag': tag},
                         headers=HEADERS, timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    candidates = []
    for j in payload.get('jobs', []):
        url = j.get('url', '')
        if not url:
            continue
        desc_html = j.get('jobDescription', '') or ''
        desc_text = BeautifulSoup(desc_html, 'html.parser').get_text(separator='\n', strip=True)
        candidates.append({
            'title':             j.get('jobTitle', ''),
            'company_name':      j.get('companyName', ''),
            'url':               url,
            'location':          j.get('jobGeo', '') or 'Remote',
            'posted':            j.get('pubDate', ''),
            'source':            'jobicy',
            'is_remote':         True,  # Jobicy is a remote-only board by construction — still
                                          # independently re-verified by classify_job() later,
                                          # no special-casing (see PROBLEMS.md #1).
            '_fallback_jd_text': desc_text,
        })
    return candidates


def sweep_jobicy_jobs() -> dict:
    """
    Query Jobicy's public API once per configured title_keywords phrase and run the results
    through the shared evaluate_candidates() funnel. Like sweep_remote_com_jobs(), there's no
    time-window cursor — Jobicy's API doesn't offer one, so each run re-pulls the current feed
    for each keyword and relies entirely on evaluate_candidates()'s existing URL/recent-repost
    dedup, which is already idempotent and safe to re-run.
    """
    global _jobicy_sweep_running

    with _jobicy_sweep_lock:
        if _jobicy_sweep_running:
            return {'added': 0, 'candidates_seen': 0, 'excluded': [], 'excluded_by_reason': {},
                    'errors': [{'error': 'Jobicy sweep already in progress'}]}
        _jobicy_sweep_running = True

    try:
        data = load_data()
        title_keywords = data.get('title_keywords', [])
        now = datetime.now(timezone.utc)

        result = {
            'added': 0,
            'candidates_seen': 0,
            'excluded': [],
            'excluded_by_reason': {},
            'errors': [],
        }

        if not title_keywords:
            data['last_jobicy_sweep_result'] = {**result, 'at': now.isoformat()}
            save_data(data)
            return result

        raw_candidates = []
        seen_urls_this_run = set()
        for kw in title_keywords:
            try:
                found = _fetch_jobicy_jobs(kw)
            except Exception as e:
                result['errors'].append({'query': kw, 'error': str(e)})
                continue
            for cand in found:
                if cand['url'] in seen_urls_this_run:
                    continue  # same listing matched by more than one keyword phrase
                seen_urls_this_run.add(cand['url'])
                raw_candidates.append(cand)

        result['candidates_seen'] = len(raw_candidates)

        data = load_data()  # reload in case something else wrote to it while we were fetching
        to_save, excluded = evaluate_candidates(raw_candidates, data)
        data.setdefault('jobs', []).extend(to_save)
        result['added'] = len([j for j in to_save if not j.get('exclusion_reason')])
        result['excluded'] = excluded
        result['excluded_by_reason'] = _excluded_by_reason(excluded)
        _record_lifetime_funnel_stats(data, result['added'], result['excluded_by_reason'])

        data['last_jobicy_sweep'] = now.isoformat()
        data['last_jobicy_sweep_result'] = {**result, 'at': now.isoformat()}
        save_data(data)

        return result
    finally:
        with _jobicy_sweep_lock:
            _jobicy_sweep_running = False


# ─────────────────────────────────────────────
# Himalayas.app — public, unauthenticated remote-jobs aggregator API (found 2026-08-03, same
# research pass that found Jobicy). Confirmed live: /jobs/api/search's `q=` param performs real
# free-text search (542 genuine "product manager" matches, real companies like Compassion
# International/Acxiom/Snapsheet/Common App/Kickstarter — a mix of company sizes, not just the
# large tech employers Adzuna already surfaces well), and `country=` genuinely filters too. Same
# Cloudflare situation as Jobicy: the job's own page 403s on a plain fetch (confirmed live), but
# the search API's own JSON already includes the full `description` HTML per job, so this reuses
# the same `_fallback_jd_text` mechanism.
# ─────────────────────────────────────────────

HIMALAYAS_API_URL = 'https://himalayas.app/jobs/api/search'
HIMALAYAS_MAX_PAGES = 3  # 20 results/page (fixed, not configurable via query param) — enough to
                          # cover a normal day's worth of new postings for one keyword without
                          # pulling hundreds of largely-repeat results on every run.

def _fetch_himalayas_jobs(query: str) -> list:
    """
    Paginate Himalayas' public search API for a given keyword phrase, newest first. Returns raw
    candidates shaped for evaluate_candidates(), each carrying the API's own `description` (HTML
    stripped to text) in `_fallback_jd_text` — the only real JD text source for this source, since
    the job's own page 403s behind Cloudflare (see module note above).
    """
    candidates = []
    for page in range(1, HIMALAYAS_MAX_PAGES + 1):
        resp = requests.get(HIMALAYAS_API_URL,
                             params={'q': query, 'country': 'US', 'sort': 'recent', 'page': page},
                             headers=HEADERS, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        jobs = payload.get('jobs', [])
        if not jobs:
            break
        for j in jobs:
            url = j.get('applicationLink') or j.get('guid') or ''
            if not url:
                continue
            desc_html = j.get('description', '') or ''
            desc_text = BeautifulSoup(desc_html, 'html.parser').get_text(separator='\n', strip=True)
            locations = j.get('locationRestrictions') or []
            candidates.append({
                'title':             j.get('title', ''),
                'company_name':      j.get('companyName', ''),
                'url':               url,
                'location':          ', '.join(locations) if locations else 'Remote',
                'posted':            datetime.fromtimestamp(j['pubDate'], tz=timezone.utc).isoformat() if j.get('pubDate') else '',
                'source':            'himalayas',
                'is_remote':         True,  # Himalayas is a remote-only board by construction —
                                              # still independently re-verified by classify_job()
                                              # later, no special-casing (see PROBLEMS.md #1).
                '_fallback_jd_text': desc_text,
            })
        if len(jobs) < 20:
            break  # short page — no more results
    return candidates


def sweep_himalayas_jobs() -> dict:
    """
    Query Himalayas' public search API once (paginated) per configured title_keywords phrase and
    run the results through the shared evaluate_candidates() funnel. Like sweep_jobicy_jobs(),
    there's no time-window cursor — each run re-pulls the current newest-first results for each
    keyword and relies entirely on evaluate_candidates()'s existing URL/recent-repost dedup.
    """
    global _himalayas_sweep_running

    with _himalayas_sweep_lock:
        if _himalayas_sweep_running:
            return {'added': 0, 'candidates_seen': 0, 'excluded': [], 'excluded_by_reason': {},
                    'errors': [{'error': 'Himalayas sweep already in progress'}]}
        _himalayas_sweep_running = True

    try:
        data = load_data()
        title_keywords = data.get('title_keywords', [])
        now = datetime.now(timezone.utc)

        result = {
            'added': 0,
            'candidates_seen': 0,
            'excluded': [],
            'excluded_by_reason': {},
            'errors': [],
        }

        if not title_keywords:
            data['last_himalayas_sweep_result'] = {**result, 'at': now.isoformat()}
            save_data(data)
            return result

        raw_candidates = []
        seen_urls_this_run = set()
        for kw in title_keywords:
            try:
                found = _fetch_himalayas_jobs(kw)
            except Exception as e:
                result['errors'].append({'query': kw, 'error': str(e)})
                continue
            for cand in found:
                if cand['url'] in seen_urls_this_run:
                    continue  # same listing matched by more than one keyword phrase
                seen_urls_this_run.add(cand['url'])
                raw_candidates.append(cand)

        result['candidates_seen'] = len(raw_candidates)

        data = load_data()  # reload in case something else wrote to it while we were fetching
        to_save, excluded = evaluate_candidates(raw_candidates, data)
        data.setdefault('jobs', []).extend(to_save)
        result['added'] = len([j for j in to_save if not j.get('exclusion_reason')])
        result['excluded'] = excluded
        result['excluded_by_reason'] = _excluded_by_reason(excluded)
        _record_lifetime_funnel_stats(data, result['added'], result['excluded_by_reason'])

        data['last_himalayas_sweep'] = now.isoformat()
        data['last_himalayas_sweep_result'] = {**result, 'at': now.isoformat()}
        save_data(data)

        return result
    finally:
        with _himalayas_sweep_lock:
            _himalayas_sweep_running = False


# ─────────────────────────────────────────────
# Workable cross-company job search — jobs.workable.com (found 2026-08-05, prompted directly by
# the Skylight job: a friend-shared Workable posting that none of Adzuna/Jobicy/Himalayas had any
# path to, since none of them crawl arbitrary company career pages and Skylight never separately
# posted to Jobicy/Himalayas). Distinct from the per-company Workable widget API investigated for
# Skylight itself (apply.workable.com/api/v1/widget/accounts/{slug}) — that one only lists a
# single already-known company's jobs with no full description; this one is Workable's own
# candidate-facing search across every company on the platform at once, confirmed live to return
# real, working results (228 total for a plain "product manager" + remote + US query) with the
# full description/requirements/benefits as complete HTML per job — no fallback-fetch mechanism
# needed here at all, unlike Jobicy/Himalayas.
#
# jobs.workable.com/robots.txt disallows the `/search` page's query-string paths (standard SEO
# hygiene — keeps Google from indexing infinite ?query=x&location=y permutations, not a signal
# aimed at excluding this kind of use) but its Content-Signal header explicitly states
# "search=yes, ai-input=yes" — this is squarely the "search" use case that header is naming.
# Same politeness-over-avoidance stance already taken with remote.com/Adzuna elsewhere in this
# app: a low-frequency nightly sweep plus occasional manual runs, not a bulk-indexing bot.
# ─────────────────────────────────────────────

WORKABLE_API_URL = 'https://jobs.workable.com/api/v1/jobs'
WORKABLE_MAX_PAGES = 5   # 20 results/page => up to 100 candidates per keyword per run. This
                          # source is meant to catch a handful of genuinely new postings per day
                          # (user-specified: "10 from the last day", not hundreds) — a bounded
                          # cap keeps a sweep from pulling deep into old, already-seen results
                          # just to filter almost all of them out by date anyway.
WORKABLE_DEFAULT_LOOKBACK_HOURS = 24   # first-ever sweep only looks back 24h (user-specified,
                                        # deliberately narrow) — unlike the Adzuna cursor, which
                                        # is meant to catch everything since forever, this source
                                        # is explicitly supplementary and should start small.

def _fetch_workable_jobs(query: str, since_dt) -> list:
    """
    Paginate jobs.workable.com's public cross-company search API for one keyword phrase, US +
    remote + full-time, newest activity first in practice (no confirmed working date-sort or
    date-filter query param — tried several plausible names live, none changed the result order
    or count, so this filters by 'created' client-side instead, same "over-fetch and filter
    exactly" approach sweep_adzuna_jobs() already uses for its own source's lack of a reliable
    server-side boundary). Stops paging once WORKABLE_MAX_PAGES is hit or a page's results are
    entirely older than since_dt.
    Returns raw candidates shaped for evaluate_candidates(); each carries the API's own
    description + requirementsSection + benefitsSection (HTML, stripped to text) directly — this
    source needs no _fallback_jd_text mechanism, since the search results already include full
    content, unlike Jobicy/Himalayas.
    """
    candidates = []
    next_token = None
    for _ in range(WORKABLE_MAX_PAGES):
        params = {
            'query': query,
            'location': 'United States',
            'workplace': 'remote',
            'employment_type': 'full_time',
        }
        if next_token:
            params['nextPageToken'] = next_token
        resp = requests.get(WORKABLE_API_URL, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        jobs = payload.get('jobs', [])
        if not jobs:
            break

        any_recent_enough = False
        for j in jobs:
            created_raw = j.get('created', '')
            try:
                created_dt = datetime.fromisoformat(created_raw.replace('Z', '+00:00'))
            except ValueError:
                created_dt = None
            if created_dt is not None and created_dt < since_dt:
                continue
            any_recent_enough = True

            url = j.get('url', '')
            if not url:
                continue
            desc_html = '\n\n'.join(p for p in (
                j.get('description', ''), j.get('requirementsSection', ''), j.get('benefitsSection', ''),
            ) if p)
            desc_text = BeautifulSoup(desc_html, 'html.parser').get_text(separator='\n', strip=True)
            company = j.get('company') or {}
            location = j.get('location') or {}
            candidates.append({
                'title':             j.get('title', ''),
                'company_name':      company.get('title', ''),
                'url':               url,
                'location':          location.get('countryName', '') or 'Remote',
                'posted':            created_raw,
                'source':            'workable',
                'is_remote':         True,  # query is scoped to workplace=remote — still
                                              # independently re-verified by classify_job() later,
                                              # no special-casing (see PROBLEMS.md #1).
                '_fallback_jd_text': desc_text,
            })

        next_token = payload.get('nextPageToken')
        if not next_token or not any_recent_enough:
            break
    return candidates


def sweep_workable_jobs() -> dict:
    """
    Query jobs.workable.com's cross-company search once per configured title_keywords phrase,
    from the last successful sweep (or WORKABLE_DEFAULT_LOOKBACK_HOURS on the very first run) up
    to now, and run the results through the shared evaluate_candidates() funnel. Only advances
    'last_workable_sweep' if every keyword query completed without error, same idempotent-retry
    pattern as sweep_adzuna_jobs() — a failed sweep just retries from the same starting point.
    """
    global _workable_sweep_running

    with _workable_sweep_lock:
        if _workable_sweep_running:
            return {'added': 0, 'candidates_seen': 0, 'excluded': [], 'excluded_by_reason': {},
                    'errors': [{'error': 'Workable sweep already in progress'}]}
        _workable_sweep_running = True

    try:
        data = load_data()
        title_keywords = data.get('title_keywords', [])
        now = datetime.now(timezone.utc)

        result = {
            'added': 0,
            'candidates_seen': 0,
            'excluded': [],
            'excluded_by_reason': {},
            'errors': [],
        }

        if not title_keywords:
            data['last_workable_sweep_result'] = {**result, 'at': now.isoformat()}
            save_data(data)
            return result

        since_raw = data.get('last_workable_sweep')
        since_dt = (datetime.fromisoformat(since_raw) if since_raw
                    else now - timedelta(hours=WORKABLE_DEFAULT_LOOKBACK_HOURS))

        raw_candidates = []
        seen_urls_this_run = set()
        for kw in title_keywords:
            try:
                found = _fetch_workable_jobs(kw, since_dt)
            except Exception as e:
                result['errors'].append({'query': kw, 'error': str(e)})
                continue
            for cand in found:
                if cand['url'] in seen_urls_this_run:
                    continue  # same listing matched by more than one keyword phrase
                seen_urls_this_run.add(cand['url'])
                raw_candidates.append(cand)

        result['candidates_seen'] = len(raw_candidates)

        data = load_data()  # reload in case something else wrote to it while we were fetching
        to_save, excluded = evaluate_candidates(raw_candidates, data)
        data.setdefault('jobs', []).extend(to_save)
        result['added'] = len([j for j in to_save if not j.get('exclusion_reason')])
        result['excluded'] = excluded
        result['excluded_by_reason'] = _excluded_by_reason(excluded)
        _record_lifetime_funnel_stats(data, result['added'], result['excluded_by_reason'])

        if not result['errors']:
            data['last_workable_sweep'] = now.isoformat()
        data['last_workable_sweep_result'] = {**result, 'at': now.isoformat()}
        save_data(data)

        return result
    finally:
        with _workable_sweep_lock:
            _workable_sweep_running = False


def _scrape_remote_com_search(search_url: str) -> list:
    """
    Paginate a saved remote.com job-search URL (query/filters already baked into search_url —
    the user pastes a ready-made search, no keyword-construction needed) and return raw
    candidates shaped for evaluate_candidates(). remote.com's search-results page doesn't expose
    company name cleanly outside the job link itself, so company_name here is a rough title-cased
    guess derived from the URL's company slug — good enough for evaluate_candidates()'s cheap
    title-filter/dedup steps; the authoritative name gets filled in from the job's own structured
    data during JD fetch (see fetch_job_description()'s remote.com branch) for survivors only,
    and dedup/repost matching already lowercases company names for comparison, so the
    provisional/final casing difference never causes a mismatch.
    """
    parsed = urlparse(search_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    # Keep as a list of (key, value) pairs, not a dict — this search takes repeated keys
    # (employmentType, seniority can each appear multiple times) and dict(parse_qsl(...)) would
    # silently collapse them down to their last value, corrupting the filters.
    base_query = [(k, v) for k, v in parse_qsl(parsed.query) if k != 'page']

    candidates = []
    seen_urls = set()
    for page in range(1, REMOTE_COM_MAX_PAGES + 1):
        if page > 1:
            time.sleep(REMOTE_COM_SCRAPE_DELAY_SECONDS)
        page_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{urlencode(base_query + [('page', str(page))])}"
        resp = requests.get(page_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        found_this_page = 0
        for a in soup.find_all('a', href=True):
            m = REMOTE_COM_JOB_URL_RE.match(a['href'])
            if not m:
                continue
            full_url = urljoin(base, a['href'])
            if full_url in seen_urls:
                continue
            title = a.get_text(strip=True)
            if not title:
                continue
            seen_urls.add(full_url)
            found_this_page += 1
            candidates.append({
                'title':        title,
                'company_name': m.group(1).replace('-', ' ').title(),
                'url':          full_url,
                'location':     'Remote',
                'posted':       '',
                'source':       'remote_com',
                'is_remote':    True,  # search is scoped to workplaceLocation=remote — still
                                        # independently re-verified by classify_job() later, no
                                        # special-casing (see PROBLEMS.md #1).
            })

        if found_this_page == 0:
            break

    return candidates


def _extract_remote_com_jobposting(html: str) -> dict | None:
    """
    remote.com job pages embed the full schema.org JobPosting object as a JSON string inside a
    Next.js RSC streaming chunk (self.__next_f.push([1, "<json>"])) rather than a literal
    <script type="application/ld+json"> tag (that only materializes client-side after
    hydration) — so a plain HTTP fetch still gets the exact same structured data, just needs
    pulling out of the streaming payload. This format is undocumented and Next.js-internal, so
    treat it as fragile: return None on any structural mismatch rather than raising, so callers
    can fall back to generic text extraction instead of hard-failing.
    """
    idx = html.find('\\"@type\\":\\"JobPosting\\"')
    if idx == -1:
        return None
    start = html.rfind('self.__next_f.push(', 0, idx)
    end = html.find(')</script>', idx)
    if start == -1 or end == -1:
        return None
    try:
        push_arg = json.loads(html[start + len('self.__next_f.push('):end])
        if not (isinstance(push_arg, list) and len(push_arg) > 1 and isinstance(push_arg[1], str)):
            return None
        job_json = json.loads(push_arg[1])
        if isinstance(job_json, dict) and job_json.get('@type') == 'JobPosting':
            return job_json
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def scrape_company(company: dict) -> list:
    """Route to the right scraper based on URL.
    Uses scrape_url if set (ATS endpoint), otherwise falls back to url."""
    url = company.get('scrape_url') or company['url']
    cid = company['id']
    name = company['name']

    if 'greenhouse.io' in url:
        return scrape_greenhouse(url, cid, name)
    elif 'lever.co' in url:
        return scrape_lever(url, cid, name)
    elif 'ashbyhq.com' in url:
        return scrape_ashby(url, cid, name)
    else:
        return scrape_generic(url, cid, name)


# ─────────────────────────────────────────────
# Shared candidate-evaluation funnel — used by BOTH company-board scraping and
# the Adzuna sweep, so every job gets the same checks regardless of source.
# ─────────────────────────────────────────────

RECENT_REPOST_DAYS = 30  # PROBLEMS.md #3 — don't re-evaluate a role we've seen from
                         # this company recently, even under a new URL/listing id
JOB_RETENTION_DAYS = 30  # data['jobs'] entries older than this get cleaned up (see
                          # cleanup_old_jobs()) — generated documents on disk/Drive are never
                          # touched by this, only the JSON job list. Coincides with
                          # RECENT_REPOST_DAYS, which only ever looks back this same window
                          # anyway, so cleanup never removes anything repost-detection still needs.
ADZUNA_SCRAPE_DELAY_SECONDS = 10  # pause between www.adzuna.com real-URL resolutions to
                                   # avoid tripping its bot-protection (PROBLEMS.md #5).
                                   # Generous on purpose — this now runs overnight
                                   # unattended, so wall-clock time doesn't matter.

def _normalize_title(title: str) -> str:
    return re.sub(r'\s+', ' ', title or '').strip().lower()

def evaluate_candidates(candidates: list, data: dict) -> tuple:
    """
    Run a batch of raw candidate jobs (unified shape: title, company_name, url, location,
    description, posted, source — company-scraped candidates also carry company_id/id/is_remote)
    through the shared funnel:
      1. title-keyword pre-filter (cheap, no network): must match a title_keywords phrase (if
         any are set) AND must NOT contain a title_exclude_keywords phrase (e.g. "staff",
         "principal", "director" — seniority levels the user isn't targeting)
      2. resolve Adzuna candidates to the REAL employer/ATS URL (via _resolve_adzuna_real_url,
         following Adzuna's own click-through chain rather than settling for Adzuna's own
         landing page), only for survivors of #1 — avoids a network call per candidate that
         would've been excluded anyway. Falls back to the plain redirect-follow if that fails.
      3. URL dedup against already-tracked jobs
      4. recent-repost check (PROBLEMS.md #3): same (company, title) evaluated in the last
         RECENT_REPOST_DAYS days, found by scanning data['jobs'] — no separate ledger needed,
         since jobs are never deleted from that list.
      5. same-batch location-cluster dedup (PROBLEMS.md #2): (company, title) duplicates across
         cities collapse to the single best-ranked STATE_RANK location.
      6. remote + software classification (PROBLEMS.md #1, #4) via one Claude call per survivor.
         Uses is_real_job_description() (PROBLEMS.md #5) to decide whether the fetched text is
         actually usable — falling back to Adzuna's own details-page text (stashed in step 2)
         if the real destination turned out to be unscrapeable. A failed/unusable fetch or a
         classification call failure does NOT exclude the job — it's kept with 'unknown' status
         and jd_fetch_status: 'failed' so nothing silently disappears; the caller/UI can surface
         these for manual review. Jobs confirmed remote + software-relevant with a real JD then
         get a second, separate Claude call — rate_job_fit() — scoring 1-5 how well the user's
         career docs match the role, so document generation (a much more expensive Sonnet call,
         and always manually triggered — never automatic) can be reserved for strong matches.
    Returns (to_save, excluded). to_save holds every job that should be persisted to
    data['jobs'] — jobs that passed the whole funnel, PLUS judgment-call rejections
    (title_mismatch, title_excluded, not_remote, not_software), which are kept with
    'exclusion_reason'/'exclusion_reasoning' set so they stay reviewable (Excluded tab)
    instead of vanishing. Pure dedup rejections (duplicate_url, recent_repost,
    duplicate_location) are NOT added to to_save — there's nothing to review when an
    equivalent job is already tracked. excluded is a separate, transient list reporting
    ALL seven rejection reasons for the "what just happened in this run" summary.
    """
    title_keywords = data.get('title_keywords', [])
    title_exclude_keywords = data.get('title_exclude_keywords', [])
    existing_jobs = data.get('jobs', [])
    existing_urls = {j.get('url') for j in existing_jobs}
    now = datetime.now(timezone.utc)
    excluded = []
    to_save = []

    def _build_excluded_job(cand, reason, reasoning=''):
        return {
            'id':                  cand.get('id') or str(uuid.uuid4()),
            'title':               cand.get('title', ''),
            'company_name':        cand.get('company_name', ''),
            'company_id':          cand.get('company_id'),
            'url':                 cand.get('url', ''),
            'location':            cand.get('location', ''),
            'source':              cand.get('source', ''),
            'posted':              cand.get('posted', ''),
            'is_new':              True,
            'is_applied':          False,
            'is_remote':           cand.get('is_remote'),
            'match_score':         0,
            'first_seen':          now.isoformat(),
            'last_seen':           now.isoformat(),
            'created_at':          now.isoformat(),
            'remote_status':       'unknown',
            'hybrid_state':        '',
            'remote_reasoning':    '',
            'software_status':     'unknown',
            'software_reasoning':  '',
            'product_summary':     '',
            'industry':            '',
            'evaluated_at':        None,
            'jd_fetch_status':     'failed',
            'fit_rating':          None,
            'fit_reasoning':       '',
            'exclusion_reason':    reason,
            'exclusion_reasoning': reasoning,
        }

    # 1. Title-keyword pre-filter (positive match + exclude-term check)
    survivors = []
    for cand in candidates:
        title = cand.get('title', '')
        title_lower = title.lower()

        excluded_match = next((kw for kw in title_exclude_keywords if kw.lower() in title_lower), None)
        if excluded_match:
            reasoning = f'title contains excluded term "{excluded_match}"'
            excluded.append({**cand, 'reason': 'title_excluded', 'reasoning': reasoning})
            to_save.append(_build_excluded_job(cand, 'title_excluded', reasoning))
            continue

        if title_keywords and not any(kw.lower() in title_lower for kw in title_keywords):
            # Not persisted (2026-07-30, user-specified) — unlike every other exclusion reason,
            # title_mismatch is "everything that isn't what I'm looking for," which in practice
            # is most of what any job board returns and flooded the Excluded tab without being
            # worth reviewing (a title that plainly doesn't contain any of your title_keywords
            # phrases isn't a judgment call the way not_remote/not_software are). Still counted
            # in the transient per-run `excluded` report so "N excluded this run" stays accurate;
            # just never written into data['jobs'].
            excluded.append({**cand, 'reason': 'title_mismatch'})
            continue
        survivors.append(dict(cand))

    # 2. Resolve Adzuna candidates to the REAL employer/ATS URL only for title-match
    #    survivors. Falls back to the old plain-redirect-follow if the real-URL chain fails.
    #    Stashes Adzuna's own details-page text into the generic `_fallback_jd_text` field
    #    (popped off again in step 6, before it's ever persisted) in case the real destination
    #    is unscrapeable. This field isn't Adzuna-specific — sweep_jobicy_jobs() stashes Jobicy's
    #    own API-provided jobDescription here the same way, since Jobicy's job pages are behind
    #    Cloudflare (403 on a plain fetch) even though the listing API itself is open.
    #    A small delay between candidates avoids bursting www.adzuna.com's own bot-protection
    #    (which is separate from and stricter than api.adzuna.com, the real search API — see
    #    PROBLEMS.md #5). Only paid for adzuna candidates that reach this step, not the whole run.
    is_first_adzuna = True
    for cand in survivors:
        if cand.get('source') == 'adzuna':
            if not is_first_adzuna:
                time.sleep(ADZUNA_SCRAPE_DELAY_SECONDS)
            is_first_adzuna = False

            real_url, details_text = _resolve_adzuna_real_url(cand.get('adzuna_id', ''))
            if real_url:
                cand['url'] = real_url
            else:
                cand['url'] = _resolve_redirect_url(cand.get('url', ''))
            if details_text:
                cand['_fallback_jd_text'] = details_text

    def _exclude(cand, reason, **extra):
        clean = {k: v for k, v in cand.items() if k != '_fallback_jd_text'}
        excluded.append({**clean, 'reason': reason, **extra})

    # 3. URL dedup
    after_url_dedup = []
    for cand in survivors:
        url = cand.get('url', '')
        if url in existing_urls:
            _exclude(cand, 'duplicate_url')
            continue
        existing_urls.add(url)
        after_url_dedup.append(cand)

    # 4. Recent-repost check
    after_repost_check = []
    for cand in after_url_dedup:
        norm_title = _normalize_title(cand.get('title', ''))
        norm_company = (cand.get('company_name', '') or '').strip().lower()
        is_recent_repost = False
        for j in existing_jobs:
            if (j.get('company_name', '') or '').strip().lower() != norm_company:
                continue
            if _normalize_title(j.get('title', '')) != norm_title:
                continue
            first_seen = j.get('first_seen')
            if not first_seen:
                continue
            try:
                seen_dt = datetime.fromisoformat(first_seen)
            except ValueError:
                continue
            if (now - seen_dt).days < RECENT_REPOST_DAYS:
                is_recent_repost = True
                break
        if is_recent_repost:
            _exclude(cand, 'recent_repost')
            continue
        after_repost_check.append(cand)

    # 5. Same-batch location-cluster dedup
    groups = {}
    for cand in after_repost_check:
        key = (_normalize_title(cand.get('title', '')), (cand.get('company_name', '') or '').strip().lower())
        groups.setdefault(key, []).append(cand)

    final_candidates = []
    for group in groups.values():
        if len(group) == 1:
            final_candidates.append(group[0])
            continue
        ranked = sorted(group, key=lambda c: state_rank(c.get('location', '')))
        final_candidates.append(ranked[0])
        for loser in ranked[1:]:
            _exclude(loser, 'duplicate_location')

    # 6. Remote + software classification, then fit-rating for confirmed survivors
    career_docs = None  # lazy-loaded only if a job actually reaches the rating step
    candidate_location, _ = _parse_accepted_locations(data.get('settings', {}))
    for cand in final_candidates:
        fallback_jd_text = cand.pop('_fallback_jd_text', '')
        job = {
            **cand,
            'id':                 cand.get('id') or str(uuid.uuid4()),
            'is_new':             True,
            'is_applied':         False,
            'match_score':        0,
            'first_seen':         now.isoformat(),
            'last_seen':          now.isoformat(),
            'created_at':         now.isoformat(),
            'remote_status':      'unknown',
            'hybrid_state':       '',
            'remote_reasoning':   '',
            'software_status':    'unknown',
            'software_reasoning': '',
            'product_summary':    '',
            'industry':           '',
            'evaluated_at':       None,
            'jd_fetch_status':    'failed',
            'fit_rating':         None,
            'fit_reasoning':      '',
            'exclusion_reason':    None,
            'exclusion_reasoning': '',
        }
        if job.get('is_remote') is None:
            job['is_remote'] = detect_remote(job.get('title', ''), job.get('location', ''))

        jd_text = fetch_job_description(job)
        if not is_real_job_description(jd_text) and is_real_job_description(fallback_jd_text):
            jd_text = fallback_jd_text  # the source's own API/details text was usable even if the real destination wasn't

        classified = False
        if is_real_job_description(jd_text):
            # Always persist the full text we actually classified against — previously this was
            # fetched into a local variable and thrown away, leaving only whatever short snippet
            # (if any) the raw candidate started with. Needed for Sonnet re-evaluation to have
            # real text to work with, and worth doing unconditionally regardless ("just in case").
            job['description'] = jd_text[:MAX_JD_CHARS]
            job['jd_fetch_status'] = 'ok'
            try:
                verdict = classify_job(jd_text, job['title'], job['company_name'],
                                        candidate_location=candidate_location)
                _add_token_usage(job, 'classification', verdict.get('usage'), 'haiku')
                job['remote_status'], job['hybrid_state'] = _decide_remote_status(
                    verdict['remote'], job.get('location', ''), jd_text, data.get('settings', {}))
                job['remote_reasoning'] = verdict['remote_reasoning']
                job['software_status'] = {'yes': 'relevant', 'no': 'not_relevant'}.get(verdict['software'], 'unknown')
                job['software_reasoning'] = verdict['software_reasoning']
                job['product_summary'] = verdict.get('summary', '')
                job['industry'] = verdict.get('industry', '')
                job['evaluated_at'] = now.isoformat()
                classified = True
            except Exception as e:
                print(f"    [classify] failed for {job.get('title','')} @ {job.get('company_name','')}: {e}")
        else:
            # No real JD to classify against, so this job is staying in Found regardless — but
            # industry/summary don't need a JD at all, just the company (2026-08-28, user-
            # specified). Best-effort web-search lookup so Found jobs are still rankable by the
            # Total sort's industry term instead of always defaulting to neutral. Soft-fails like
            # everything else non-essential in this funnel — never blocks the job from being saved.
            try:
                industry_verdict = classify_industry_by_search(job['company_name'], job['title'])
                _add_token_usage(job, 'classification', industry_verdict.get('usage'), 'haiku')
                job['product_summary'] = industry_verdict.get('summary', '')
                job['industry'] = industry_verdict.get('industry', '')
            except Exception as e:
                print(f"    [classify] industry-by-search failed for {job.get('title','')} @ {job.get('company_name','')}: {e}")

        if classified and job['remote_status'] == 'not_remote':
            job['exclusion_reason'] = 'not_remote'
            job['exclusion_reasoning'] = job['remote_reasoning']
            # Pass job, not cand, so the transient report's entry carries the same id as the
            # persisted record (cand may never have had an id — it's only assigned above when
            # building job) — needed so "Re-evaluate" on the report has a real job to target.
            _exclude(job, 'not_remote', reasoning=job['remote_reasoning'])
            to_save.append(job)
            continue
        if classified and job['software_status'] == 'not_relevant':
            job['exclusion_reason'] = 'not_software'
            job['exclusion_reasoning'] = job['software_reasoning']
            _exclude(job, 'not_software', reasoning=job['software_reasoning'])
            to_save.append(job)
            continue

        # Fit rating — only for confirmed remote/hybrid + software-relevant jobs with a real JD,
        # so a job that gets excluded above never costs a rating call.
        if job['jd_fetch_status'] == 'ok' and job['remote_status'] in ('remote', 'hybrid') and job['software_status'] == 'relevant':
            if career_docs is None:
                career_docs = load_career_docs(data.get('settings', {}))
            try:
                rating_verdict = rate_job_fit(jd_text, job['title'], job['company_name'], career_docs)
                _add_token_usage(job, 'fit_rating', rating_verdict.get('usage'), 'haiku')
                job['fit_rating'] = rating_verdict['rating']
                job['fit_reasoning'] = rating_verdict['reasoning']
                _apply_cleaned_description(job, rating_verdict.get('cleaned_description', ''))
            except Exception as e:
                print(f"    [rate] failed for {job.get('title','')} @ {job.get('company_name','')}: {e}")

        to_save.append(job)

    return to_save, excluded


def _excluded_by_reason(excluded: list) -> dict:
    return {
        reason: sum(1 for e in excluded if e['reason'] == reason)
        for reason in sorted({e['reason'] for e in excluded})
    }


def _record_lifetime_funnel_stats(data: dict, presented: int, rejected_by_reason: dict) -> None:
    """
    Accumulate permanent, never-purged pipeline funnel counters (2026-09-28, user-specified:
    "how many jobs were reviewed, how many were rejected for each rejection reason, how many
    were presented to me") into data['lifetime_stats'] — a top-level key completely independent
    of data['jobs'], which JOB_RETENTION_DAYS purges after 30 days. Same durability pattern
    data['audit_log'] already uses. Starts accumulating from whenever this was added
    (2026-09-28) — deliberately NOT seeded with a backdated estimate of prior activity, since the
    per-job detail needed to reconstruct real history was only ever stored on job records, and
    those are long gone for anything older than 30 days (this is the direct, concrete finding
    that prompted building this at all — see the real investigation that preceded it). "reviewed"
    itself isn't stored — it's always presented + sum(rejected_by_reason.values()), derived at
    read time (see api_get_lifetime_stats()) so it can never drift out of sync with its own parts.

    Called once per evaluate_candidates() run, from each of the six sweep functions that call it
    (check_all_companies, sweep_adzuna_jobs, sweep_remote_com_jobs, sweep_jobicy_jobs,
    sweep_himalayas_jobs, sweep_workable_jobs) — each of which already computes exactly these two
    numbers for its own per-run result dict, so this just folds the same numbers into the
    permanent running total, no separate computation. Deliberately NOT called from capture_job(),
    add-from-search, generate-adhoc, or any re-evaluation path (reevaluate_job,
    track_job_manually, screen_found_jobs, the manual reclassify route) — those are either
    re-checks of an already-tracked job (would double-count something already counted once) or
    low-volume manual entry points outside evaluate_candidates()'s bulk-funnel shape. The
    overwhelming majority of real review volume goes through the six sweep functions, so this is
    a deliberate, accepted scope cut, not an oversight — the manual paths' true volume is small
    enough that missing it here doesn't meaningfully skew "how many jobs were reviewed" as a
    health metric, which is what this is for.

    (2026-10-07): now writes into the metrics database (_record_daily_metric()/
    _record_daily_rejections()) instead of data['lifetime_stats'], which is left exactly as it
    was at the moment of this change — a frozen pre-database baseline, not touched again — so
    readers add it to the database's own sum rather than the two silently double-counting the
    same ongoing activity.
    """
    _record_daily_metric('presented', presented)
    _record_daily_rejections(rejected_by_reason)


# ─────────────────────────────────────────────
# Main check logic
# ─────────────────────────────────────────────

def check_all_companies() -> dict:
    """
    Scrape all tracked companies' career pages and run genuinely-new listings through the
    shared evaluate_candidates() funnel. Jobs from other sources (Adzuna, ad-hoc) that are
    already tracked are preserved untouched — this function only ever adds/refreshes/drops
    company-scraped jobs.
    Returns {added, candidates_seen, excluded, excluded_by_reason, errors}.
    """
    global _check_running

    with _check_lock:
        if _check_running:
            return {'added': 0, 'candidates_seen': 0, 'excluded': [], 'excluded_by_reason': {},
                    'errors': [{'company': 'system', 'error': 'Check already in progress'}]}
        _check_running = True

    try:
        data = load_data()
        existing_jobs = {j['id']: j for j in data.get('jobs', [])}
        tracked_company_ids = {c['id'] for c in data.get('companies', [])}

        # Preserve jobs that didn't come from a tracked company scrape (Adzuna, ad-hoc, etc.)
        # untouched — only company-scraped jobs get refreshed/dropped by this pass.
        fresh_jobs = {
            jid: job for jid, job in existing_jobs.items()
            if job.get('company_id') not in tracked_company_ids
        }

        new_candidates = []
        errors = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for company in data.get('companies', []):
            print(f"  Checking {company['name']} ({company['url']})...")
            try:
                found = scrape_company(company)
                for job in found:
                    jid = job['id']
                    if jid in existing_jobs:
                        entry = existing_jobs[jid].copy()
                        entry['last_seen'] = now_iso
                        # Keep is_new=True until user dismisses it
                        fresh_jobs[jid] = entry
                    else:
                        new_candidates.append(job)
                print(f"    → {len(found)} jobs found")
            except Exception as e:
                print(f"    ✗ Error: {e}")
                errors.append({'company': company['name'], 'error': str(e)})

        to_save, excluded = evaluate_candidates(new_candidates, data)
        for job in to_save:
            fresh_jobs[job['id']] = job

        added = len([j for j in to_save if not j.get('exclusion_reason')])
        excluded_by_reason = _excluded_by_reason(excluded)
        _record_lifetime_funnel_stats(data, added, excluded_by_reason)

        data['jobs'] = list(fresh_jobs.values())
        data['seen_job_ids'] = list(fresh_jobs.keys())
        data['last_checked'] = now_iso
        data['last_check_errors'] = errors
        save_data(data)

        return {
            'added': added,
            'candidates_seen': len(new_candidates),
            'excluded': excluded,
            'excluded_by_reason': excluded_by_reason,
            'errors': errors,
        }
    finally:
        with _check_lock:
            _check_running = False


# ─────────────────────────────────────────────
# API routes
# ─────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/api/status')
def api_status():
    data = load_data()
    return jsonify({
        'job_count': len(data.get('jobs', [])),
        'new_jobs': sum(1 for j in data.get('jobs', []) if j.get('is_new')),
        'company_count': len(data.get('companies', [])),
        'get_jobs_running': _get_jobs_running,
        'last_get_jobs_at': data.get('last_get_jobs_at'),
        'last_get_jobs_result': data.get('last_get_jobs_result'),
    })


# ─────────────────────────────────────────────
# ATS detection
# ─────────────────────────────────────────────

# Tokens to ignore when extracting company slugs from ATS URLs found in HTML
_ATS_IGNORE = {'embed', 'js', 'css', 'assets', 'api', 'fonts', 'images',
               'static', 'v1', 'v2', 'app', 'widget', 'script', 'track',
               'analytics', 'pixel', 'talent', 'jobs', 'careers'}

def detect_ats_for_url(url: str) -> dict:
    """
    Fetch a URL and scan its HTML for ATS indicators.
    Returns a dict with 'ats', 'suggested_url', and optionally 'warning'.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        html = resp.text
    except Exception as e:
        return {'ats': None, 'error': str(e)}

    # ── Greenhouse ──
    # boards.greenhouse.io/COMPANY or greenhouse.io/embed/job_board?for=COMPANY
    for pattern in [
        r'boards\.greenhouse\.io/([a-zA-Z0-9_-]+)',
        r'greenhouse\.io/embed/job_board\?for=([a-zA-Z0-9_-]+)',
        r'greenhouse\.io/([a-zA-Z0-9_-]+)/jobs',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            token = m.group(1).lower()
            if token not in _ATS_IGNORE:
                return {
                    'ats': 'greenhouse',
                    'suggested_url': f'https://boards.greenhouse.io/{token}',
                }

    # ── Lever ──
    m = re.search(r'jobs\.lever\.co/([a-zA-Z0-9_-]+)', html, re.IGNORECASE)
    if m:
        token = m.group(1).lower()
        if token not in _ATS_IGNORE:
            return {
                'ats': 'lever',
                'suggested_url': f'https://jobs.lever.co/{token}',
            }

    # ── Ashby ──
    for pattern in [
        r'jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)',
        r'ashbyhq\.com/([a-zA-Z0-9_-]+)',
    ]:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            token = m.group(1).lower()
            if token not in _ATS_IGNORE:
                return {
                    'ats': 'ashby',
                    'suggested_url': f'https://jobs.ashbyhq.com/{token}',
                }

    # ── Workday ──
    m = re.search(r'(https?://[a-zA-Z0-9_-]+\.(?:wd\d+\.)?myworkdayjobs\.com[^\s"\'<>]*)', html, re.IGNORECASE)
    if m:
        return {
            'ats': 'workday',
            'suggested_url': m.group(1).rstrip('/'),
            'warning': 'Workday pages are JS-rendered — scraping may be limited.',
        }

    # ── Rippling / Jobvite / SmartRecruiters (flag but no better URL) ──
    for ats_name, pattern in [
        ('rippling',        r'app\.rippling\.com/ats'),
        ('jobvite',         r'jobs\.jobvite\.com'),
        ('smartrecruiters', r'careers\.smartrecruiters\.com'),
        ('icims',           r'careers\.icims\.com'),
        ('bamboohr',        r'[a-z0-9-]+\.bamboohr\.com/careers'),
    ]:
        if re.search(pattern, html, re.IGNORECASE):
            return {
                'ats': ats_name,
                'suggested_url': None,
                'warning': f'This page uses {ats_name.title()}, which requires JavaScript. Scraping may return no results.',
            }

    # ── JS-rendered detection (little parseable text = likely SPA) ──
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()
    visible_text = soup.get_text(separator=' ', strip=True)
    if len(visible_text) < 400:
        return {
            'ats': None,
            'suggested_url': None,
            'warning': 'This page appears to be JavaScript-rendered. The scraper may not find job listings.',
        }

    return {'ats': None}


@app.route('/api/detect-ats', methods=['POST'])
def api_detect_ats():
    body = request.json or {}
    url = (body.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'url is required'}), 400
    if not url.startswith('http'):
        url = 'https://' + url
    result = detect_ats_for_url(url)
    return jsonify(result)


@app.route('/api/companies', methods=['GET'])
def api_get_companies():
    data = load_data()
    return jsonify(data.get('companies', []))

@app.route('/api/companies', methods=['POST'])
def api_add_company():
    body = request.json or {}
    name = (body.get('name') or '').strip()
    url = (body.get('url') or '').strip()
    if not name or not url:
        return jsonify({'error': 'name and url are required'}), 400
    if not url.startswith('http'):
        url = 'https://' + url
    data = load_data()
    # Check for duplicate URL
    if any(c['url'] == url for c in data['companies']):
        return jsonify({'error': 'Company with that URL already exists'}), 409
    company = {
        'id': hashlib.md5(url.encode()).hexdigest()[:8],
        'name': name,
        'url': url,
        'added': datetime.now(timezone.utc).isoformat()
    }
    data['companies'].append(company)
    save_data(data)
    return jsonify(company), 201

@app.route('/api/companies/<company_id>', methods=['PUT'])
def api_update_company(company_id):
    body = request.json or {}
    name = (body.get('name') or '').strip()
    url = (body.get('url') or '').strip()
    if not name or not url:
        return jsonify({'error': 'name and url are required'}), 400
    if not url.startswith('http'):
        url = 'https://' + url
    data = load_data()
    for c in data['companies']:
        if c['id'] == company_id:
            c['name'] = name
            c['url'] = url
            save_data(data)
            return jsonify(c)
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/companies/<company_id>', methods=['DELETE'])
def api_delete_company(company_id):
    data = load_data()
    data['companies'] = [c for c in data['companies'] if c['id'] != company_id]
    data['jobs'] = [j for j in data['jobs'] if j.get('company_id') != company_id]
    save_data(data)
    return jsonify({'ok': True})


@app.route('/api/keywords', methods=['GET'])
def api_get_keywords():
    data = load_data()
    return jsonify({
        'title_keywords': data.get('title_keywords', []),
        'desc_keywords': data.get('desc_keywords', []),
        'title_exclude_keywords': data.get('title_exclude_keywords', []),
    })

@app.route('/api/keywords', methods=['POST'])
def api_update_keywords():
    body = request.json or {}
    title_kw = [k.strip() for k in body.get('title_keywords', []) if k.strip()]
    desc_kw = [k.strip() for k in body.get('desc_keywords', []) if k.strip()]
    title_exclude_kw = [k.strip() for k in body.get('title_exclude_keywords', []) if k.strip()]
    data = load_data()
    data['title_keywords'] = title_kw
    data['desc_keywords'] = desc_kw
    data['title_exclude_keywords'] = title_exclude_kw
    save_data(data)
    return jsonify({
        'title_keywords': title_kw,
        'desc_keywords': desc_kw,
        'title_exclude_keywords': title_exclude_kw,
    })


# Fields index.html's list views (Found/Screened/Excluded cards, global search, the preview-page
# pre-populate before /preview loads) actually read off a job object — audited directly against
# renderJobs()/renderJobList()/renderExcludedJobsList()/fitRatingBadge()/postedInfo()/
# searchAllByCompany() in index.html. Deliberately excludes heavy fields the list views never
# touch (description, cleaned_description, remote_reasoning, software_reasoning, token_usage,
# etc.) — those are only needed on the Job detail page, fetched separately via
# GET /api/jobs/<id>/preview.
JOB_LIST_FIELDS = [
    'id', 'is_new', 'url', 'title', 'company_id', 'company_name', 'location',
    'product_summary', 'industry', 'fit_rating', 'fit_reasoning', 'remote_status',
    'hybrid_state', 'manually_tracked', 'is_applied', 'jd_fetch_status',
    'exclusion_reason', 'exclusion_reasoning', 'reevaluate_failed', 'evaluated_at',
    'software_status', 'posted', 'first_seen', 'source',
]

@app.route('/api/jobs', methods=['GET'])
def api_get_jobs():
    data = load_data()
    title_kw = data.get('title_keywords', [])
    desc_kw = data.get('desc_keywords', [])
    company_filter = request.args.get('company')
    show_new_only = request.args.get('new_only') == 'true'
    matching_only = request.args.get('matching_only') == 'true'

    jobs = data.get('jobs', [])
    if company_filter:
        jobs = [j for j in jobs if j.get('company_id') == company_filter]
    if show_new_only:
        jobs = [j for j in jobs if j.get('is_new')]

    # Score jobs live against current keywords
    linkedin_connections = data.get('linkedin_connections', [])
    scored = []
    for j in jobs:
        s = score_job(j.get('title', ''), j.get('description', ''), title_kw, desc_kw)
        if matching_only and s < 0:
            continue
        # So the Jobs list can show a star by the company name without the user needing to open
        # each job — find_connections_at_company() is the same match already used on the Job
        # detail page's preview step, just run here per job in the list too. Cheap enough at this
        # app's scale (a few hundred connections, low thousands of jobs) to compute on every
        # request rather than needing to cache it. Names are included (not just the count) so the
        # star badge's hover tooltip can name the actual contact(s), not just say "N connections".
        matches = find_connections_at_company(j.get('company_name', ''), linkedin_connections)
        connection_names = [m.get('name', '') for m in matches if m.get('name')]
        trimmed = {field: j.get(field) for field in JOB_LIST_FIELDS}
        scored.append({**trimmed, 'match_score': s, 'linkedin_connection_count': len(matches), 'linkedin_connection_names': connection_names})

    # Sort: new jobs first, then by fit rating (best match on top — unrated jobs sort below
    # any explicit 1-5 rating, since they're unconfirmed rather than known-poor), then by
    # keyword match score, then oldest-first as a final tiebreaker.
    scored.sort(key=lambda j: (
        not j.get('is_new', False),
        -(j.get('fit_rating') or 0),
        -j.get('match_score', 0),
        j.get('first_seen') or '',
    ))
    return jsonify(scored)

@app.route('/api/jobs/<job_id>/dismiss', methods=['POST'])
def api_dismiss_job(job_id):
    """Mark a job as no longer 'new'."""
    data = load_data()
    for job in data['jobs']:
        if job['id'] == job_id:
            job['is_new'] = False
            break
    save_data(data)
    return jsonify({'ok': True})

@app.route('/api/jobs/dismiss-all', methods=['POST'])
def api_dismiss_all():
    """Mark all new jobs as seen."""
    data = load_data()
    for job in data['jobs']:
        job['is_new'] = False
    save_data(data)
    return jsonify({'ok': True})

@app.route('/api/jobs/<job_id>', methods=['DELETE'])
def api_reject_job(job_id):
    """Remove a job from the tracked list entirely — used by the "Reject" button on the
    Found tab."""
    data = load_data()
    data['jobs'] = [j for j in data['jobs'] if j['id'] != job_id]
    save_data(data)
    return jsonify({'ok': True})


@app.route('/api/jobs/<job_id>/restore', methods=['POST'])
def api_restore_job(job_id):
    """
    Undo a judgment-call exclusion (title_mismatch, title_excluded, not_remote, not_software) —
    the "Restore to Found" button on the Excluded tab. Clears exclusion_reason/exclusion_reasoning
    and evaluated_at, putting the job back in Found so it can be given a second look (via the
    Screen retry button, if it already has a real JD) rather than trusting the first verdict
    forever.
    """
    data = load_data()
    job = next((j for j in data['jobs'] if j['id'] == job_id), None)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    job['exclusion_reason'] = None
    job['exclusion_reasoning'] = ''
    job['evaluated_at'] = None
    save_data(data)
    return jsonify({'ok': True, 'job': job})


@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    data = load_data()
    return jsonify(data.get('settings', {}))

@app.route('/api/industries', methods=['GET'])
def api_get_industries():
    # Single source of truth for the closed vocabulary classify_job()/extract_and_classify_job()
    # classify against — Settings' per-industry interest-rating rows are built from this same
    # list, so the two can never drift out of sync.
    return jsonify(INDUSTRIES)

@app.route('/api/settings', methods=['POST'])
def api_save_settings():
    body = request.json or {}
    data = load_data()
    data['settings'].update(body)
    save_data(data)
    return jsonify(data['settings'])


# ─────────────────────────────────────────────
# LinkedIn connections (2026-07-24) — LinkedIn has no usable public API for checking whether the
# user has a connection at a given company (the old Connections API was shut down specifically to
# protect their social graph, and live scraping risks a real account ban). The ToS-safe
# workaround: the user can export their own connections as a CSV from LinkedIn's own data-export
# feature and import it here manually, periodically.
# ─────────────────────────────────────────────

def _parse_linkedin_connections_csv(csv_text: str) -> list:
    """
    Parse a LinkedIn "Connections" data export. The real file has a few 'Notes:' preamble lines
    before the actual header row, so this searches for the first row whose first cell is
    'First Name' rather than assuming a fixed line number — the header row LinkedIn's export has
    consistently used for years. Strips a leading BOM if present (common in this export).
    Skips any row with no company text at all — nothing to ever match a job against.
    Returns [{'name', 'company', 'position', 'url', 'connected_on'}, ...].
    Raises ValueError if no header row is found (e.g. the wrong file was uploaded).
    """
    text = csv_text.lstrip('﻿')
    rows = list(csv.reader(io.StringIO(text)))

    header_idx = next(
        (i for i, row in enumerate(rows) if row and row[0].strip().lower() == 'first name'),
        None,
    )
    if header_idx is None:
        raise ValueError("Could not find the header row — is this a LinkedIn connections export?")

    header = [h.strip().lower() for h in rows[header_idx]]
    col = {name: idx for idx, name in enumerate(header)}

    def get(row, name):
        idx = col.get(name)
        return row[idx].strip() if idx is not None and idx < len(row) else ''

    connections = []
    for row in rows[header_idx + 1:]:
        if not row:
            continue
        company = get(row, 'company')
        if not company:
            continue
        name = f"{get(row, 'first name')} {get(row, 'last name')}".strip()
        connections.append({
            'name':         name,
            'company':      company,
            'position':     get(row, 'position'),
            'url':          get(row, 'url'),
            'connected_on': get(row, 'connected on'),
        })
    return connections


def find_connections_at_company(company_name: str, connections: list) -> list:
    """
    Case-insensitive match in both directions, requiring the shorter name to match as a whole
    word/phrase rather than a raw substring — e.g. "Google" matches a connection's "Google LLC".
    A plain substring check (this app's usual simple-matching style elsewhere, e.g.
    title_exclude_keywords) was tried first but produces real false positives for short company
    names: a connection with company "Alt" matched every job at a company containing that letter
    sequence anywhere, e.g. "Health" (he-ALT-h), "Realty", "Altair" — confirmed for real
    (2026-07-31) via a connection literally named company "Alt" spuriously starring dozens of
    unrelated jobs. Word-boundary regex fixes this while still matching "Google" inside "Google
    LLC" (bounded by the space). Returns the matching connection dicts, or [] if nothing
    configured/matched.
    """
    if not company_name or not connections:
        return []
    needle = company_name.strip().lower()
    matches = []
    for conn in connections:
        hay = (conn.get('company') or '').strip().lower()
        if not hay:
            continue
        if re.search(r'\b' + re.escape(hay) + r'\b', needle) or re.search(r'\b' + re.escape(needle) + r'\b', hay):
            matches.append(conn)
    return matches


@app.route('/api/linkedin-connections/import', methods=['POST'])
def api_import_linkedin_connections():
    body = request.get_json(silent=True) or {}
    csv_text = body.get('csv_text', '')
    if not csv_text.strip():
        return jsonify({'error': 'No CSV content received'}), 400

    try:
        connections = _parse_linkedin_connections_csv(csv_text)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    data = load_data()
    data['linkedin_connections'] = connections
    data['linkedin_connections_imported_at'] = datetime.now(timezone.utc).isoformat()
    save_data(data)
    return jsonify({'ok': True, 'count': len(connections)})


@app.route('/api/linkedin-connections/status', methods=['GET'])
def api_linkedin_connections_status():
    data = load_data()
    return jsonify({
        'count':       len(data.get('linkedin_connections', [])),
        'imported_at': data.get('linkedin_connections_imported_at'),
    })


@app.route('/api/jobs/<job_id>/apply', methods=['POST'])
def api_mark_applied(job_id):
    # 'applied' defaults to True so the existing fire-and-forget call made right after a
    # successful generation (which posts no body at all) keeps working unchanged — this route
    # only gained the ability to un-mark (2026-08-26, real bug: the Applied checkbox always
    # called this route and always forced itself back to checked, so a user who generated docs,
    # read the feedback, and decided NOT to apply after all had no way to uncheck it).
    body = request.get_json(silent=True) or {}
    applied = bool(body.get('applied', True))
    data = load_data()
    for job in data['jobs']:
        if job['id'] == job_id:
            job['is_applied'] = applied
            job['applied_at'] = datetime.now(timezone.utc).isoformat() if applied else None
            break
    save_data(data)
    return jsonify({'ok': True})


@app.route('/api/jobs/<job_id>/reclassify', methods=['POST'])
def api_reclassify_job(job_id):
    """
    Manually re-run remote/software classification using pasted job description text —
    for jobs where automated fetching failed (jd_fetch_status: 'failed'), per PROBLEMS.md #1/#4.
    Also re-runs the fit rating (rate_job_fit) if the job comes out remote + software-relevant,
    or sets exclusion_reason/exclusion_reasoning (via _apply_classification) if it doesn't.
    """
    body = request.get_json(silent=True) or {}
    job_description = (body.get('job_description') or '').strip()
    if not job_description:
        return jsonify({'error': 'job_description is required'}), 400

    data = load_data()
    job = next((j for j in data['jobs'] if j['id'] == job_id), None)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    settings = data.get('settings', {})
    candidate_location, _ = _parse_accepted_locations(settings)
    try:
        verdict = classify_job(job_description, job.get('title', ''), job.get('company_name', ''),
                                candidate_location=candidate_location)
    except Exception as e:
        return jsonify({'error': f'Classification failed: {e}'}), 500

    _apply_classification(job, job_description, verdict, settings, datetime.now(timezone.utc))

    save_data(data)
    return jsonify({'ok': True, 'job': job})


def screen_found_jobs() -> dict:
    """
    Retry-screen any 'Found' job that already has a usable JD (jd_fetch_status 'ok' or 'manual')
    but never got successfully classified (evaluated_at is still None) — e.g. because an earlier
    automatic classify attempt hit a transient API failure. This is a narrow safety net, not the
    primary classification path: most jobs get classified automatically at ingestion
    (evaluate_candidates()) or immediately on JD paste (POST /api/jobs/<id>/reclassify) — this
    should normally find 0 or very few jobs.
    Jobs that fail this retry are removed (same as ingestion-time exclusion behavior) rather than
    left permanently stuck in an unviewable state; jobs that can't even be attempted (missing
    real JD text despite the filter, or a repeated classify failure) are left alone, unchanged,
    to retry again next time.
    Jobs that fail this retry are kept (not deleted) with exclusion_reason/exclusion_reasoning
    set, same as every other rejection path — see _apply_classification().
    Returns {'screened_in': int, 'screened_out': [...with reason/reasoning], 'total': int}.
    """
    data = load_data()
    to_screen = [j for j in data['jobs']
                 if j.get('evaluated_at') is None and j.get('jd_fetch_status') in ('ok', 'manual')]

    result = {'screened_in': 0, 'screened_out': [], 'total': len(to_screen)}
    if not to_screen:
        return result

    now = datetime.now(timezone.utc)
    settings = data.get('settings', {})
    candidate_location, _ = _parse_accepted_locations(settings)

    for job in to_screen:
        jd_text = job.get('description', '')
        if not is_real_job_description(jd_text):
            continue  # leave alone — shouldn't happen given the filter above, but be safe

        try:
            verdict = classify_job(jd_text, job.get('title', ''), job.get('company_name', ''),
                                    candidate_location=candidate_location)
        except Exception as e:
            print(f"    [screen] classify failed for {job.get('title','')}: {e}")
            continue  # leave alone, retry next time

        _apply_classification(job, jd_text, verdict, settings, now)

        if job['exclusion_reason'] is None:
            result['screened_in'] += 1
        else:
            result['screened_out'].append({
                'id': job['id'], 'title': job['title'], 'company_name': job['company_name'],
                'location': job.get('location', ''), 'url': job.get('url', ''),
                'reason': job['exclusion_reason'], 'reasoning': job['exclusion_reasoning'],
            })

    save_data(data)
    return result


@app.route('/api/jobs/screen', methods=['POST'])
def api_screen_found_jobs():
    return jsonify(screen_found_jobs())




def _apply_classification(job: dict, description: str, verdict: dict, settings: dict, now,
                           jd_fetch_status: str = 'manual', model_key: str = 'haiku') -> None:
    """
    Apply a classify_job() / extract_and_classify_job() verdict (both share the same
    remote/remote_reasoning/software/software_reasoning keys) to a job dict in place: sets the
    description, remote/software status + reasoning, and evaluated_at, then either rates fit
    (if it passes) or sets exclusion_reason/exclusion_reasoning (if it doesn't), so a rejection
    stays visible (Excluded tab) instead of just disappearing. Shared by capture_job() (all
    three outcomes: attach / match / create), screen_found_jobs(), the manual reclassify route,
    reevaluate_job(), and the add-from-search/generate-adhoc creation paths — every place a job
    description gets classified outside the main ingestion funnel.
    jd_fetch_status defaults to 'manual' (every pre-existing caller pastes/already-has the text
    by hand); pass 'ok' when the text was fetched automatically (e.g. add-from-search) — purely
    cosmetic, every consumer (e.g. the "needs JD" badge) treats 'ok'/'manual' identically.
    model_key (2026-09-21) records which model actually produced `verdict`, for the Costs area's
    per-model breakdown (see _add_token_usage()) — defaults to 'haiku' since every caller except
    reevaluate_job() (which passes 'sonnet') calls classify_job()/extract_and_classify_job() with
    their own default model_key.
    """
    job['description'] = description[:MAX_JD_CHARS]
    job['jd_fetch_status'] = jd_fetch_status
    _add_token_usage(job, 'classification', verdict.get('usage'), model_key)
    job['remote_status'], job['hybrid_state'] = _decide_remote_status(
        verdict['remote'], job.get('location', ''), description, settings)
    job['remote_reasoning'] = verdict['remote_reasoning']
    job['software_status'] = {'yes': 'relevant', 'no': 'not_relevant'}.get(verdict['software'], 'unknown')
    job['software_reasoning'] = verdict['software_reasoning']
    job['product_summary'] = verdict.get('summary', '')
    job['industry'] = verdict.get('industry', '')
    job['evaluated_at'] = now.isoformat()
    job['fit_rating'] = None
    job['fit_reasoning'] = ''
    job['exclusion_reason'] = None
    job['exclusion_reasoning'] = ''
    if job['remote_status'] in ('remote', 'hybrid') and job['software_status'] == 'relevant':
        try:
            career_docs = load_career_docs(settings)
            rating = rate_job_fit(description, job.get('title', ''), job.get('company_name', ''), career_docs)
            _add_token_usage(job, 'fit_rating', rating.get('usage'), 'haiku')
            job['fit_rating'] = rating['rating']
            job['fit_reasoning'] = rating['reasoning']
            _apply_cleaned_description(job, rating.get('cleaned_description', ''))
        except Exception as e:
            print(f"    [classify] rating failed for {job.get('title','')} @ {job.get('company_name','')}: {e}")
    else:
        job['exclusion_reason'] = 'not_remote' if job['remote_status'] not in ('remote', 'hybrid') else 'not_software'
        job['exclusion_reasoning'] = job['remote_reasoning'] if job['exclusion_reason'] == 'not_remote' else job['software_reasoning']


def capture_job(url: str, page_text: str) -> dict:
    """
    Handle a browser-extension capture. An exact URL match against tracked jobs takes priority
    (cheapest, and covers "the user followed a link out of the app itself"); failing that, a
    combined extract+classify Haiku call (extract_and_classify_job) determines title/company and
    remote/software fit, and those get matched against jobs specifically stuck for lack of a JD
    (evaluated_at is None and jd_fetch_status not in ('ok','manual') — a different, narrower set
    than screen_found_jobs() targets, which is for jobs that already HAVE a JD but got stuck);
    failing that, a brand new job gets created from scratch. No title_keywords/
    title_exclude_keywords gating — this is a deliberate manual action, same as
    add-from-search/generate-adhoc. A captured job that fails remote/software classification is
    still saved, with exclusion_reason/exclusion_reasoning set (Excluded tab), so a rejection
    stays reviewable instead of the click just silently doing nothing.

    Returns one of:
      {'action': 'attached', 'job': {...}}
      {'action': 'matched',  'job': {...}}
      {'action': 'created',  'job': {...}}
      {'action': 'excluded', 'job': {...}, 'reason': str, 'reasoning': str}
      {'action': 'error', 'error': str}
    """
    if not url:
        return {'action': 'error', 'error': 'No URL captured'}
    if not is_real_job_description(page_text):
        return {'action': 'error', 'error': 'Captured text is too short or looks invalid — nothing saved'}

    data = load_data()
    now = datetime.now(timezone.utc)
    settings = data.get('settings', {})
    candidate_location, _ = _parse_accepted_locations(settings)

    def _result(action, job):
        if job['exclusion_reason'] is not None:
            return {'action': 'excluded', 'job': job,
                    'reason': job['exclusion_reason'], 'reasoning': job['exclusion_reasoning']}
        return {'action': action, 'job': job}

    # 1. Exact URL match against every tracked job
    existing = next((j for j in data['jobs'] if j.get('url') == url), None)
    if existing:
        try:
            verdict = classify_job(page_text, existing.get('title', ''), existing.get('company_name', ''),
                                    candidate_location=candidate_location)
        except Exception as e:
            return {'action': 'error', 'error': f'Classification failed: {e}'}
        _apply_classification(existing, page_text, verdict, settings, now)
        save_data(data)
        return _result('attached', existing)

    # 2. No URL match — extract title/company + classify in one call
    try:
        extraction = extract_and_classify_job(page_text, candidate_location=candidate_location)
    except Exception as e:
        return {'action': 'error', 'error': f'Extraction failed: {e}'}

    title = extraction['title']
    company_name = extraction['company_name']
    if not title or not company_name:
        return {'action': 'error', 'error': 'Could not identify a job title/company on this page'}

    # Prefer the cleaned <description> extract_and_classify_job() isolated from the raw page
    # text over the raw text itself — the raw text can be dominated by unrelated content (e.g. a
    # search-results page's other listings, see extract_and_classify_job()'s docstring for the
    # real bug this fixes), and persisting THAT is what let a bad JD reach generation before.
    # Falls back to the raw page text if the extraction came back empty/invalid for any reason —
    # a real value beats losing the capture outright, same "something beats nothing" instinct as
    # every other fallback in this app (_fallback_jd_text, rate_job_fit()'s reasoning fallback).
    cleaned_description = extraction.get('description', '')
    description_for_storage = cleaned_description if is_real_job_description(cleaned_description) else page_text

    # 3. Match against jobs specifically stuck for lack of a JD
    norm_title = _normalize_title(title)
    norm_company = company_name.strip().lower()
    match = next((
        j for j in data['jobs']
        if j.get('evaluated_at') is None
        and j.get('jd_fetch_status') not in ('ok', 'manual')
        and _normalize_title(j.get('title', '')) == norm_title
        and (j.get('company_name', '') or '').strip().lower() == norm_company
    ), None)
    if match:
        match['url'] = url  # prefer the real, reachable URL the user was actually looking at
        _apply_classification(match, description_for_storage, extraction, settings, now)
        save_data(data)
        return _result('matched', match)

    # 4. No match anywhere — a job found entirely outside the app's pipeline
    new_job = {
        'id':           str(uuid.uuid4()),
        'title':        title,
        'company_name': company_name,
        'url':          url,
        'location':     '',
        'source':       'extension',
        'is_new':       True,
        'is_applied':   False,
        'is_remote':    True,
        'match_score':  0,
        'first_seen':   now.isoformat(),
        'last_seen':    now.isoformat(),
        'created_at':   now.isoformat(),
    }
    _apply_classification(new_job, description_for_storage, extraction, settings, now)
    data.setdefault('jobs', []).append(new_job)
    save_data(data)
    return _result('created', new_job)


@app.route('/api/capture', methods=['POST'])
def api_capture_job():
    body = request.get_json(silent=True) or {}
    url = (body.get('url') or '').strip()
    page_text = (body.get('text') or '').strip()
    return jsonify(capture_job(url, page_text))


def reevaluate_job(job: dict, settings: dict) -> dict:
    """
    Re-run classification with Sonnet instead of Haiku for a job excluded on a judgment call
    (not_remote / not_software) — built after a real Haiku misclassification (the Evolve case,
    see PROBLEMS.md) that a side-by-side Sonnet test caught correctly. Only meaningful for the
    two classification-based exclusion reasons; title_mismatch/title_excluded are plain keyword
    matching, not something a stronger model reads any differently.

    Uses the job's persisted description if it's real text; jobs excluded before full JD text
    started getting persisted unconditionally (see evaluate_candidates()) fall back to a fresh
    fetch. Applies the result via the same _apply_classification() every other classification
    path uses — if Sonnet disagrees with the original verdict, the job moves to Found/Screened
    with a real fit rating, same as any other job; if it agrees, the job stays excluded, now
    with Sonnet's reasoning instead of Haiku's.

    Raises ValueError if the job isn't a judgment-call exclusion, or if no usable JD text is
    available at all (nothing to re-evaluate against).
    """
    if job.get('exclusion_reason') not in ('not_remote', 'not_software'):
        raise ValueError('Only jobs excluded for not_remote or not_software can be re-evaluated')

    text = job.get('description', '')
    if not is_real_job_description(text):
        text = fetch_job_description(job)
    if not is_real_job_description(text):
        raise ValueError('No usable job description available to re-evaluate against')

    candidate_location, _ = _parse_accepted_locations(settings)
    verdict = classify_job(text, job.get('title', ''), job.get('company_name', ''), model_key='sonnet',
                            candidate_location=candidate_location)
    _apply_classification(job, text, verdict, settings, datetime.now(timezone.utc), jd_fetch_status='ok',
                           model_key='sonnet')
    # Persisted (not just a transient response flag) so the frontend can swap "Re-evaluate" for
    # "Track" (see track_job_manually()) even after a reload, without the user needing to click
    # Re-evaluate again first — "still excluded after Sonnet's own look" is meaningful, durable
    # information about this specific job, not something to silently forget.
    job['reevaluate_failed'] = job.get('exclusion_reason') is not None
    return job


@app.route('/api/jobs/<job_id>/reevaluate', methods=['POST'])
def api_reevaluate_job(job_id):
    data = load_data()
    job = next((j for j in data['jobs'] if j['id'] == job_id), None)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    try:
        reevaluate_job(job, data.get('settings', {}))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Re-evaluation failed: {e}'}), 500
    save_data(data)
    return jsonify({'ok': True, 'job': job})


def track_job_manually(job: dict, settings: dict) -> None:
    """
    Manual override (2026-07-23) for a job the user has personally verified is worth applying to,
    despite the classifier's verdict — the last resort once Re-evaluate (Sonnet) has already
    looked and still disagrees (see reevaluate_job()'s reevaluate_failed flag, which is what gates
    showing this action in the UI at all). Forces the job into the same trackable state a passing
    classification would have produced — remote_status/software_status set to accepted values,
    exclusion cleared — then rates it exactly like any other job that reaches that state (skipped
    originally because it was excluded before ever reaching the rating step). Deliberately leaves
    remote_reasoning/software_reasoning untouched — the model's original reasoning stays visible
    for reference on the job card, it's just no longer treated as a rejection.
    """
    job['remote_status'] = 'remote'
    job['software_status'] = 'relevant'
    job['exclusion_reason'] = None
    job['exclusion_reasoning'] = ''
    job['reevaluate_failed'] = False
    job['manually_tracked'] = True
    try:
        career_docs = load_career_docs(settings)
        rating = rate_job_fit(job.get('description', ''), job.get('title', ''), job.get('company_name', ''), career_docs)
        _add_token_usage(job, 'fit_rating', rating.get('usage'), 'haiku')
        job['fit_rating'] = rating['rating']
        job['fit_reasoning'] = rating['reasoning']
        _apply_cleaned_description(job, rating.get('cleaned_description', ''))
    except Exception as e:
        print(f"    [track] rating failed for {job.get('title','')} @ {job.get('company_name','')}: {e}")


@app.route('/api/jobs/<job_id>/track', methods=['POST'])
def api_track_job(job_id):
    data = load_data()
    job = next((j for j in data['jobs'] if j['id'] == job_id), None)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    track_job_manually(job, data.get('settings', {}))
    save_data(data)
    return jsonify({'ok': True, 'job': job})


# ─────────────────────────────────────────────
# Application generation (Anthropic API)
# ─────────────────────────────────────────────

_BLOCK_PAGE_RE = re.compile(
    r'access denied|are you a human|just a moment|verify you are|unusual traffic|'
    r'checking your browser|suspicious behaviour|enable javascript and cookies',
    re.IGNORECASE
)
MIN_JD_LENGTH = 300  # shorter than this is almost certainly not a real JD (JS-shell, thin page)
MAX_JD_CHARS = 20000  # cap on persisted/classified JD text — was a bare 8000 in four places
                       # until found (2026-08-06) to be genuinely too tight: 141 real tracked
                       # jobs were sitting at exactly 8000 chars, meaning real content was
                       # already being cut off well before this was traced to a specific capture
                       # bug (a Hospitable job whose JD got cut off mid-heading because a
                       # browser-extension capture had pulled in a whole search-results page's
                       # worth of unrelated listings first). Widened generously — real JD length
                       # is nowhere near this even for verbose postings, and the added cost is
                       # trivial next to what generation calls already spend on career context.

def _extract_readable_text(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'noscript']):
        tag.decompose()
    text = soup.get_text(separator='\n', strip=True)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return '\n'.join(lines)[:MAX_JD_CHARS]

def _content_looks_valid(text: str) -> bool:
    """True if extracted page text looks like a real job description — not empty, not a
    bot-block page, not a JS-rendered shell with no server-side content."""
    if not text or len(text) < MIN_JD_LENGTH:
        return False
    if len(text) < 500 and _BLOCK_PAGE_RE.search(text):
        return False
    return True

def is_real_job_description(text: str) -> bool:
    """Reliable check for 'do we actually have a usable job description' — used anywhere
    that needs to know whether it's safe to classify/generate from this text, not just
    whether fetch_job_description() raised an exception."""
    if not text or text.startswith('(Could not fetch'):
        return False
    return _content_looks_valid(text)


def _apply_cleaned_description(job: dict, cleaned: str) -> None:
    """
    Replace job['description'] with rate_job_fit()'s cleaned extraction (2026-08-06) — every
    rating call now also isolates just the substantive role content out of whatever surrounding
    boilerplate/noise came with the JD, so the *stored* description gets progressively cleaner
    too, not just that one rating's own reasoning. Falls back to leaving the existing description
    untouched if the cleaned text doesn't look like a real, substantive result (same "don't let a
    cleaning miss destroy a perfectly good stored value" instinct as every other fallback in this
    app) — a job keeps whatever it already had rather than losing content to an empty/garbled
    extraction.
    """
    if is_real_job_description(cleaned):
        job['description'] = cleaned[:MAX_JD_CHARS]

def fetch_job_description(job: dict) -> str:
    """Fetch and extract readable text from a job posting URL."""
    url = job.get('url', '')
    if not url:
        return ''
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code >= 400:
            return f'(Could not fetch job description: HTTP {resp.status_code})'

        if 'remote.com/jobs/' in url:
            posting = _extract_remote_com_jobposting(resp.text)
            if posting:
                desc_text = BeautifulSoup(posting.get('description', ''), 'html.parser').get_text(separator='\n', strip=True)
                if _content_looks_valid(desc_text):
                    # Side effect: correct the provisional slug-derived title/company_name
                    # (see _scrape_remote_com_search()) with the authoritative values now that
                    # we have them, for survivors only.
                    if posting.get('title'):
                        job['title'] = posting['title']
                    org_name = (posting.get('hiringOrganization') or {}).get('name')
                    if org_name:
                        job['company_name'] = org_name
                    return desc_text[:MAX_JD_CHARS]
            # Structured extraction failed or was unusable — fall through to generic below.

        result = _extract_readable_text(resp.text)
        # Some pages return HTTP 200 with either a bot-block page or a JS-rendered shell
        # with no real server-side content (seen from Adzuna's own landing pages and from
        # JS-heavy ATS sites like Ashby) — treat both as a fetch failure, not real text.
        if not _content_looks_valid(result):
            return '(Could not fetch job description: page loaded but had no usable text — likely JS-rendered or blocked)'
        return result
    except Exception as e:
        return f'(Could not fetch job description: {e})'


def fetch_gdoc_text(url: str) -> str:
    """
    Fetch plain text from a Google Doc that's shared 'Anyone with link can view'.
    Converts the doc URL to the plain-text export endpoint.
    """
    # Extract doc ID from various Google Doc URL formats
    m = re.search(r'/document/d/([a-zA-Z0-9_-]+)', url)
    if not m:
        raise ValueError(f'Could not extract Google Doc ID from URL: {url}')
    doc_id = m.group(1)
    export_url = f'https://docs.google.com/document/d/{doc_id}/export?format=txt'
    resp = requests.get(export_url, headers=HEADERS, timeout=15)
    if resp.status_code == 403:
        raise ValueError('Google Doc is not publicly shared. Set sharing to "Anyone with the link can view".')
    if resp.status_code != 200:
        raise ValueError(f'Google Doc returned HTTP {resp.status_code}')
    return resp.text.strip()


def strip_excluded_blocks(text: str) -> str:
    """Remove [exclude]...[/exclude] blocks and collapse extra blank lines."""
    cleaned = re.sub(r'\[exclude\].*?\[/exclude\]', '', text, flags=re.DOTALL)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


def load_career_docs(settings: dict = None) -> dict:
    """
    Load career reference documents. Sources (in priority order):
    1. Google Doc URLs stored in settings (gdoc_urls dict)
    2. Local .md files in career_docs/ directory
    Google Doc URLs override local files of the same logical name.
    [exclude]...[/exclude] blocks are stripped from all docs before use.
    """
    docs = {}

    # Load local files first
    if os.path.isdir(CAREER_DOCS_DIR):
        for fname in sorted(os.listdir(CAREER_DOCS_DIR)):
            if fname.endswith('.md'):
                path = os.path.join(CAREER_DOCS_DIR, fname)
                with open(path, encoding='utf-8') as f:
                    docs[fname] = strip_excluded_blocks(f.read())

    # Override/add from Google Doc URLs in settings. 'resume' and 'todo' are skipped here
    # entirely (not just excluded from _build_career_context()'s EXCLUDE set below) — both are
    # accessed via OAuth (see save_application_docs()'s live-fill template lookup, and the
    # future to-do-doc write path) and deliberately aren't publicly shared, so the public
    # fetch_gdoc_text() export would just fail/warn on every load for nothing: neither is
    # actually career reference material, they're output targets (the resume/cover-letter
    # template, and the running to-do list) — bio and instructions are the only two that ever
    # actually reach Claude. 'template' is skipped too, in case a stale value is still sitting
    # in older saved settings from before that field was dropped (2026-07-17) in favor of
    # reusing 'resume' directly.
    gdoc_urls = (settings or {}).get('gdoc_urls', {})
    for doc_name, url in gdoc_urls.items():
        if doc_name in ('template', 'resume', 'todo'):
            continue
        if not url or not url.strip():
            continue
        try:
            text = fetch_gdoc_text(url.strip())
            fname = doc_name if doc_name.endswith('.md') else f'{doc_name}.md'
            docs[fname] = strip_excluded_blocks(text)
            print(f'  [gdoc] Loaded {doc_name} from Google Doc ({len(docs[fname])} chars)')
        except Exception as e:
            print(f'  [gdoc] Warning: could not fetch {doc_name}: {e}')

    return docs


MODELS = {
    'haiku':  'claude-haiku-4-5-20251001',   # ~$0.005/run — use for testing
    'sonnet': 'claude-sonnet-4-6',            # ~$0.05/run  — use for real applications
    'opus':   'claude-opus-5',                # ~$0.15/run  — most capable; used by default for the
                                               # cover-letter audit pass (see audit_cover_letter())
}

# Gemini models (2026-09-22, user-specified: their own Gemini API key, added to the generation
# and audit model pickers specifically — NOT classification/fit-rating/interview-prep, which stay
# Claude-only by deliberate scope decision; see _resolve_claude_model_key()). Kept as a SEPARATE
# dict from MODELS rather than merged into it — MODELS' values are read as raw strings by every
# Claude-only call site in this file (classify_job, rate_job_fit, extract_and_classify_job,
# classify_industry_by_search, _extract_and_match_todo_items, generate_interview_prep); changing
# its shape or mixing in non-Claude entries would risk breaking all of those for a feature that
# was only ever meant to touch two functions (generate_application_docs, audit_cover_letter — see
# _call_gemini()). Real published $/million-token rates confirmed against ai.google.dev's pricing
# and models pages on 2026-09-22 — there is currently no stable (non-"Preview") Gemini Pro-tier
# model; gemini-3.1-pro-preview is the only flagship option Google offers right now.
GEMINI_MODELS = {
    'gemini-flash-lite': 'gemini-3.5-flash-lite',   # $0.30 / $2.50 per MTok — cheapest
    'gemini-flash':      'gemini-3.8-flash',        # $0.75 / $3.75 per MTok (through 2026) — balanced
    'gemini-pro':        'gemini-3.1-pro-preview',  # $2.00 / $12.00 per MTok (≤200K context) — most capable
}

# OpenAI/ChatGPT models (2026-09-22, user-specified — added right after Gemini specifically
# because Gemini's API was hitting real, repeated 503 "high demand" overload during testing; a
# second alternate provider gives a working fallback when one vendor is having a bad day). Same
# scope/shape decisions as GEMINI_MODELS above: a separate dict (never merged into MODELS, for the
# same blast-radius reasons), and the current lineup is a larger family than 3 tiers
# (gpt-6-astra/gpt-5.6-sol/gpt-5.6-terra/gpt-5.6-luna, confirmed against developers.openai.com's
# pricing and models pages on 2026-09-22) — mirroring the same cheap/balanced/top-tier pattern
# already used for Claude and Gemini rather than exposing all four; gpt-5.6-sol ($4/$20, between
# terra and astra) was deliberately left out to keep that pattern, not because it's unavailable.
OPENAI_MODELS = {
    'gpt-luna':  'gpt-5.6-luna',   # $0.20 / $1.20 per MTok — cheapest
    'gpt-terra': 'gpt-5.6-terra',  # $2.00 / $12.00 per MTok — balanced
    'gpt-astra': 'gpt-6-astra',    # $10.00 / $50.00 per MTok — flagship reasoning/coding model
}


def _resolve_generation_model_key(body: dict, settings: dict) -> str:
    """
    Per-generation model override (2026-09-21, extended 2026-09-22 for Gemini and OpenAI) — the
    "Generate Documents" preview page has a dropdown letting the user pick which model writes
    THIS application, separate from the standing Settings → Model default. `body['model']` (if
    present and a real key in MODELS, GEMINI_MODELS, or OPENAI_MODELS) wins; otherwise falls back
    to settings.model exactly as before this feature existed. An unrecognized value (stale
    frontend, hand-crafted request) is silently ignored rather than erroring the whole generation
    — same "don't let a cosmetic mismatch block a real action" instinct as the rest of this app's
    soft-fail conventions.
    """
    requested = body.get('model')
    if requested in MODELS or requested in GEMINI_MODELS or requested in OPENAI_MODELS:
        return requested
    return settings.get('model', 'sonnet')


def _resolve_claude_model_key(model_key: str, default: str = 'sonnet') -> str:
    """
    Clamp a model_key to a real Claude/MODELS key, falling back to `default` for anything else —
    a Gemini or OpenAI key. Used by call sites that stayed Claude-only by deliberate scope
    decision when Gemini (2026-09-22) and OpenAI (same day) were added to the generation/audit
    pickers: interview prep reads the same shared Settings → Model default those pickers use, but
    was never designed or tested against another provider's response shape, so picking one there
    shouldn't silently break it.
    """
    return model_key if model_key in MODELS else default


# Real published $/million-token rates (2026-09-21, Gemini entries added 2026-09-22), keyed the
# same as MODELS/GEMINI_MODELS combined — used to compute actual dollar costs in the Costs area,
# now that per-call model attribution (_add_token_usage()'s model_key) makes that possible. The
# flat "~$0.005/run"-style figures in MODELS above are still just rough per-run estimates for the
# model-picker dropdown copy; this is the real per-token pricing used for the Costs area's dollar
# totals. Update alongside MODELS/GEMINI_MODELS if pricing/models change.
MODEL_PRICING = {
    'haiku':  {'input': 1.00, 'output': 5.00},
    'sonnet': {'input': 3.00, 'output': 15.00},
    'opus':   {'input': 5.00, 'output': 25.00},
    'gemini-flash-lite': {'input': 0.30, 'output': 2.50},
    'gemini-flash':      {'input': 0.75, 'output': 3.75},
    'gemini-pro':        {'input': 2.00, 'output': 12.00},
    'gpt-luna':  {'input': 0.20,  'output': 1.20},
    'gpt-terra': {'input': 2.00,  'output': 12.00},
    'gpt-astra': {'input': 10.00, 'output': 50.00},
}


def _estimate_cost(input_tokens: int, output_tokens: int, model_key: str) -> float:
    """Dollar cost for a token count under a given model's real per-MTok pricing (MODEL_PRICING).
    Returns 0.0 for an unrecognized model_key rather than raising — a cost estimate is a nice-to-
    have display figure, not something worth failing a request over."""
    rates = MODEL_PRICING.get(model_key)
    if not rates:
        return 0.0
    return (input_tokens / 1_000_000) * rates['input'] + (output_tokens / 1_000_000) * rates['output']


def _call_gemini(model_key: str, system_instruction: str, user_content: str, max_output_tokens: int) -> tuple:
    """
    Call the Gemini Developer API (google-genai SDK, the stable client.models.generate_content
    surface — NOT the Beta Interactions API, which Google's own docs say to avoid for production)
    with a system instruction + single user turn, returning (response_text, usage_dict) — the
    exact same shape every Anthropic call site in this file already produces, so
    generate_application_docs()/audit_cover_letter() can dispatch to either provider and parse
    the result identically afterward. Both callers' prompts already ask for plain tagged-XML/JSON
    text back, which is a model-agnostic instruction, not an Anthropic-specific one — no prompt
    changes needed to support a second provider.

    Reads the key from GEMINI_API_KEY, falling back to GOOGLE_API_KEY (matching the SDK's own
    dual-env-var convention) — never hardcoded, same as ANTHROPIC_API_KEY elsewhere in this file.

    Thinking is left at the model's own default rather than force-disabled. This is a deliberate
    difference from the Anthropic call sites (which explicitly pass thinking={'type': 'disabled'}
    after a real bug: Claude Opus 5's default-on thinking put a ThinkingBlock at
    message.content[0], and blindly reading .text off that crashed every audit pass — see
    audit_cover_letter()'s 2026-09-21 fix). That failure mode is specific to Anthropic's
    content-block list and manual indexing; response.text here is an SDK-level accessor
    documented to already exclude thought-marked parts, so there's nothing to work around.
    """
    if not HAS_GEMINI:
        raise RuntimeError('google-genai package not installed. Run: venv/bin/pip install google-genai')
    api_key = os.environ.get('GEMINI_API_KEY', '').strip() or os.environ.get('GOOGLE_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('GEMINI_API_KEY not set. Add it to your .env file.')

    client = gemini_sdk.Client(api_key=api_key)
    response = client.models.generate_content(
        model=GEMINI_MODELS[model_key],
        contents=user_content,
        config=gemini_types.GenerateContentConfig(
            system_instruction=system_instruction,
            max_output_tokens=max_output_tokens,
        ),
    )
    usage = {
        'input_tokens':  (response.usage_metadata.prompt_token_count or 0) if response.usage_metadata else 0,
        'output_tokens': (response.usage_metadata.candidates_token_count or 0) if response.usage_metadata else 0,
    }
    return response.text or '', usage


def _call_openai(model_key: str, system_instruction: str, user_content: str, max_output_tokens: int) -> tuple:
    """
    Call the OpenAI API (openai SDK, the current client.responses.create() surface — NOT the
    older client.chat.completions.create()) with a developer instruction + single user turn,
    returning (response_text, usage_dict) — the exact same shape _call_gemini() and every
    Anthropic call site in this file already produce, so generate_application_docs()/
    audit_cover_letter() can dispatch to any of the three providers and parse the result
    identically afterward.

    Reads the key from OPENAI_API_KEY (the SDK's own standard env var, same convention as
    ANTHROPIC_API_KEY/GEMINI_API_KEY elsewhere in this file) — never hardcoded.

    No thinking/reasoning-disable workaround needed here, unlike the Anthropic call sites (see
    _call_gemini()'s docstring for that bug). response.output_text is an SDK-level property that
    already filters to only `type == "output_text"` message content, explicitly excluding
    reasoning items — confirmed by reading the SDK's own source, not assumed — so there's no
    equivalent failure mode to work around for gpt-astra's reasoning capability.
    """
    if not HAS_OPENAI:
        raise RuntimeError('openai package not installed. Run: venv/bin/pip install openai')
    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('OPENAI_API_KEY not set. Add it to your .env file.')

    client = openai_sdk(api_key=api_key)
    response = client.responses.create(
        model=OPENAI_MODELS[model_key],
        instructions=system_instruction,
        input=user_content,
        max_output_tokens=max_output_tokens,
    )
    usage = {
        'input_tokens':  (response.usage.input_tokens or 0) if response.usage else 0,
        'output_tokens': (response.usage.output_tokens or 0) if response.usage else 0,
    }
    return response.output_text or '', usage


# Closed vocabulary for job['industry'] (2026-08-28, user-specified) — the model must return one
# of these exactly (see INDUSTRY_INSTRUCTION below); anything else gets normalized to 'Other' by
# the caller rather than stored as a rogue free-text string, same closed-vocabulary discipline as
# remote_status/software_status. 'Social Media' is deliberately its own category, separate from
# 'Consumer Media & Entertainment' — the user explicitly didn't want lifestyle/outdoor apps (e.g.
# onX) drifting into a 'social' bucket just because they might have social features; that content
# stays in Consumer Media & Entertainment, Social Media is reserved for actual social platforms
# (Facebook, Twitter/X, Instagram, etc.). Used both for classification and as the fixed row set in
# Settings' per-industry 1-5 interest rating.
INDUSTRIES = [
    'Fintech & Financial Services',
    'Healthcare & Health Tech',
    'Real Estate & PropTech',
    'E-commerce, Retail & Marketplaces',
    'Consumer Media & Entertainment',
    'Enterprise / B2B SaaS',
    'Developer Tools & Infrastructure',
    'Cybersecurity',
    'AI / Machine Learning',
    'Education & EdTech',
    'Travel & Hospitality',
    'Transportation & Logistics',
    'Gaming',
    'Marketing & AdTech',
    'HR Tech & Recruiting',
    'Legal & Compliance Tech',
    'Government, Defense & Public Sector',
    'Energy, Climate & Sustainability',
    'Construction, Industrial & Manufacturing Tech',
    'Consumer Electronics, Hardware & Telecom',
    'Social Media',
    'Other',
]

# Shared between classify_job() and extract_and_classify_job() so the criteria can never drift
# out of sync between the two call sites. Refined 2026-07-17 after a real false negative: Haiku
# misread "we can hire from anywhere in the U.S. except D.C. and Hawaii" (about as broad as
# remote-hiring eligibility gets) as a location restriction, because it didn't distinguish a
# payroll/legal hiring-eligibility list from an actual onsite requirement.
CLASSIFY_CRITERIA = (
    "1. REMOTE: is this role truly remote, with no expectation of regular onsite presence? "
    "Occasional travel (client visits, offsites) is fine. Required onsite presence every so "
    "often (e.g. every few months) is fine. A required regular onsite/hybrid schedule (e.g. "
    "weekly, 'X days in office') or a requirement to be based near a specific office/city is "
    "NOT remote, even if the posting uses the word 'remote' elsewhere. IMPORTANT: a list of "
    "which US states/countries the company can legally hire and pay employees in (e.g. 'we can "
    "hire from anywhere in the US except X, Y' or state-specific compensation/tax notes) is a "
    "standard payroll/legal eligibility notice, NOT an onsite requirement — a broad "
    "hiring-eligibility list like this does not make a role non-remote, even if it also mentions "
    "an office as one optional local arrangement for people who happen to already live near it.\n\n"
    "2. SOFTWARE: does this specific role have a significant software component — does it "
    "involve working/partnering with engineers, building software/applications/data "
    "platforms, or list required technical skills? Judge the role itself, not the company's "
    "industry — a non-software company can have a genuine software PM role, and vice versa.\n\n"
)


SUMMARY_INSTRUCTION = (
    "3. SUMMARY: write a short summary (200 characters max) of the specific product this role "
    "would manage or work on, if the posting makes that clear. If the posting doesn't describe a "
    "specific product clearly enough, summarize what the company does instead. Be concrete and "
    "specific (name the actual product/domain) rather than generic marketing language.\n\n"
)

INDUSTRY_INSTRUCTION = (
    "4. INDUSTRY: classify the company/product into exactly one of the following industries. "
    "Reply with the industry name copied exactly as written below, character for character — do "
    "not paraphrase or shorten it. If nothing fits well, use \"Other\".\n"
    + '\n'.join(f'- {i}' for i in INDUSTRIES) +
    "\n\n"
)


def _normalize_industry(raw: str) -> str:
    """Closed-vocabulary guard for the <industry> tag — a model response that doesn't exactly
    match an entry in INDUSTRIES (paraphrased, wrong case, trailing punctuation) is normalized to
    'Other' rather than stored as a free-text string Settings' fixed per-industry rating rows
    would never match against. Case/whitespace-insensitive match first, exact-string fast path
    first since that's the overwhelmingly common case."""
    raw = raw.strip()
    if raw in INDUSTRIES:
        return raw
    lowered = raw.lower()
    for industry in INDUSTRIES:
        if industry.lower() == lowered:
            return industry
    return 'Other'


def _truncate_summary(text: str, limit: int = 200) -> str:
    """Hard-cap the <summary> field at `limit` chars — a safety net for the rare response that
    overshoots the prompt's own 200-char instruction. Trims back to the last word boundary rather
    than slicing mid-word (a bare [:200] once cut a real response off mid-"insurance")."""
    text = text.strip()
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    last_space = truncated.rfind(' ')
    return (truncated[:last_space] if last_space > 0 else truncated).strip()


def classify_industry_by_search(company_name: str, title: str = '') -> dict:
    """
    Determine industry + a short company summary via Claude's server-side web_search tool,
    instead of from job-posting text (2026-08-28, user-specified). Built for the case
    classify_job() can't cover at all: a Found job with no real JD yet has nothing to classify
    from, but its company can still be looked up directly. Used both going forward
    (evaluate_candidates(), when a candidate's JD fetch fails) and for one-time backfills of
    already-tracked JD-less jobs.

    Shares the same closed-vocabulary INDUSTRIES list and _normalize_industry() guard as
    classify_job() — a job classified this way is indistinguishable in the data from one
    classified off real JD text; only the classification method differs. Does NOT attempt
    remote/software judgments at all (there's no role-specific text to judge those from) — callers
    must leave remote_status/software_status/evaluated_at untouched, this only ever supplies
    'industry' and 'summary'.

    Returns {'industry': str, 'summary': str, 'usage': dict}. Raises on API/config failure — same
    contract as every other Claude-calling function in this file, callers decide how to soft-fail.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key or api_key == 'your-key-here':
        raise RuntimeError('ANTHROPIC_API_KEY not set')
    if not HAS_ANTHROPIC:
        raise RuntimeError('anthropic package not installed')

    client = anthropic_sdk.Anthropic(api_key=api_key)
    role_note = f' A specific open role there is titled "{title}".' if title else ''
    system_prompt = (
        f'Research the company "{company_name}" using web search.{role_note} Well-known companies '
        "you already have reliable knowledge of don't need a search. Then answer two questions.\n\n"
        "1. SUMMARY: write a short summary (200 characters max) of what the company does. Be "
        "concrete and specific (name the actual product/domain) rather than generic marketing "
        "language.\n\n"
        "2. INDUSTRY: classify the company into exactly one of the following industries. Reply "
        "with the industry name copied exactly as written below, character for character — do "
        "not paraphrase or shorten it. If nothing fits well, use \"Other\".\n"
        + '\n'.join(f'- {i}' for i in INDUSTRIES) +
        "\n\nEnd your reply with exactly this format:\n"
        "<summary>...</summary>\n"
        "<industry>...</industry>"
    )

    message = client.messages.create(
        model=MODELS['haiku'],
        max_tokens=1000,
        tools=[{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 3}],
        system=system_prompt,
        messages=[{'role': 'user', 'content': f'Research and classify "{company_name}".'}],
    )
    # Tool-use responses mix server_tool_use/web_search_tool_result/text blocks — the tagged
    # answer only ever lands in text blocks, so concatenate just those rather than assuming
    # message.content[0] is text the way every non-tool-use call in this file safely can.
    text = ''.join(b.text for b in message.content if b.type == 'text')

    def extract(tag):
        matches = re.findall(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL)
        return matches[-1].strip() if matches else ''

    return {
        'summary':  _truncate_summary(extract('summary')),
        'industry': _normalize_industry(extract('industry')),
        'usage':    _usage_dict(message),
    }


def _location_context(candidate_location: str) -> str:
    """Shared prompt fragment (2026-07-22, widened 2026-09-24) — see settings.accepted_locations.
    Empty when no candidate location is configured, so the prompt is byte-identical to before for
    anyone who hasn't set this up.

    Widened 2026-09-24 after a real miss: three Veeva Systems postings requiring "based in or
    within one hour of the Eastern Time zone" were marked not_remote with candidate_location
    correctly unset at the time (settings.accepted_locations had never actually been configured,
    despite the user believing it was — a real config gap, not a code bug; see reevaluate_job()
    below for the same three postings re-run after fixing that). But the ORIGINAL wording here was
    also narrower than it needed to be — "onsite/hybrid requirement conditional on location" only
    describes a residency/hybrid-eligibility condition, not a timezone/working-hours condition
    like this one, even though both are the same underlying judgment (does the candidate's
    location satisfy a location-conditional requirement). Broadened the wording to name timezone
    constraints explicitly so a state-only candidate_location value (e.g. "VT") is more reliably
    connected to a timezone name in the posting (e.g. "Eastern Time") without depending on the
    model happening to make that geography inference unprompted.
    """
    if not candidate_location:
        return ''
    return (
        f"\n\nThe candidate is based in {candidate_location}. If the posting's onsite/hybrid "
        "presence requirement OR any timezone/working-hours requirement (e.g. \"must be based in "
        "or within N hours of [timezone]\", \"core hours aligned with [timezone] teams\") is "
        "conditional on the candidate's own location, judge remote-ness based on what this "
        "specific candidate would actually experience — including whether their location's "
        "timezone satisfies the stated constraint — not the posting's most restrictive case."
    )


def _usage_dict(message) -> dict:
    """Pull {'input_tokens', 'output_tokens'} off an Anthropic SDK response in the one shape
    every Claude-calling function in this file returns it in."""
    return {'input_tokens': message.usage.input_tokens, 'output_tokens': message.usage.output_tokens}


def _add_token_usage(job: dict, part: str, usage: dict, model_key: str = '') -> None:
    """
    Accumulate Claude API token usage onto a job record, broken out by which part of the
    pipeline spent it: 'classification' (classify_job/extract_and_classify_job), 'fit_rating'
    (rate_job_fit), 'generation' (generate_application_docs), 'cover_letter_audit'
    (audit_cover_letter, split out from 'generation' 2026-09-21 specifically so a generation
    model and a different audit model don't get blended into one bucket), 'todo_matching'
    (_extract_and_match_todo_items — a real, separate Haiku call, kept distinct from
    'generation' rather than folded in so a Sonnet-priced and Haiku-priced cost don't get
    blended together).

    model_key (2026-09-21) records which model tier actually spent these tokens, summed
    separately under bucket['by_model'][model_key] alongside the existing part-level totals
    (unchanged shape) — this is what lets the Costs area compute real dollar costs (see
    MODEL_PRICING) and correlate 'generation' cost against cover-letter rule-violation rate per
    model. Falsy model_key is skipped rather than bucketed under a fake 'unknown' key; old
    records simply have no 'by_model' breakdown, same "gaps are fine" tolerance already used
    everywhere else in this app (e.g. jobs from before token_usage itself existed).

    Safe to call multiple times for the same part on the same job (re-evaluate, re-rate,
    regenerate) — sums rather than overwrites, since a job's lifetime token cost is the useful
    number here, not just its most recent call. No-ops on a missing/empty usage dict so every
    call site can pass a Claude call's result straight through without an extra guard.
    """
    if not usage:
        return
    bucket = job.setdefault('token_usage', {}).setdefault(part, {'input_tokens': 0, 'output_tokens': 0, 'calls': 0})
    bucket['input_tokens'] += usage.get('input_tokens', 0) or 0
    bucket['output_tokens'] += usage.get('output_tokens', 0) or 0
    bucket['calls'] += 1
    if model_key:
        mbucket = bucket.setdefault('by_model', {}).setdefault(
            model_key, {'input_tokens': 0, 'output_tokens': 0, 'calls': 0})
        mbucket['input_tokens'] += usage.get('input_tokens', 0) or 0
        mbucket['output_tokens'] += usage.get('output_tokens', 0) or 0
        mbucket['calls'] += 1
    # 2026-10-07: also feed the metrics database, dated today — the one shared choke point
    # behind every classification/fit_rating/generation/todo_matching/cover_letter_audit call
    # site, so all of them get daily tracking for free. See "Metrics database" section above.
    _record_daily_token_usage(part, model_key, usage)


def classify_job(job_description: str, title: str, company_name: str, model_key: str = 'haiku',
                  candidate_location: str = '') -> dict:
    """
    Ask Claude whether a job is truly remote and whether it has a significant software
    component (PROBLEMS.md #1 and #4, combined into one call since both need the same JD text).
    Returns {'remote': 'yes'/'no'/'unclear', 'remote_reasoning': str,
             'software': 'yes'/'no'/'unclear', 'software_reasoning': str}.
    Raises on API/config failure — callers must treat that as 'unknown', not exclude the job.
    model_key defaults to 'haiku' (every existing call site) — pass 'sonnet' for re-evaluation
    (see reevaluate_job()), a deliberate, manually-triggered double-check with a stronger model.
    candidate_location (from settings.accepted_locations' 'remote-X' entry, see
    _parse_accepted_locations()) adds location context to the remote judgment — empty by default.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key or api_key == 'your-key-here':
        raise RuntimeError('ANTHROPIC_API_KEY not set')
    if not HAS_ANTHROPIC:
        raise RuntimeError('anthropic package not installed')

    client = anthropic_sdk.Anthropic(api_key=api_key)

    system_prompt = (
        "You evaluate job postings against four criteria. Read the job description and answer "
        "all four, with brief reasoning (1-2 sentences each) for the first two.\n\n"
        + CLASSIFY_CRITERIA
        + SUMMARY_INSTRUCTION
        + INDUSTRY_INSTRUCTION
        + _location_context(candidate_location) +
        "\n\nRespond in exactly this format:\n"
        "<remote>yes|no|unclear</remote>\n"
        "<remote_reasoning>...</remote_reasoning>\n"
        "<software>yes|no|unclear</software>\n"
        "<software_reasoning>...</software_reasoning>\n"
        "<summary>...</summary>\n"
        "<industry>...</industry>"
    )
    user_message = f"**Title:** {title}\n**Company:** {company_name}\n\n**Job Description:**\n{job_description}"

    message = client.messages.create(
        model=MODELS.get(model_key, MODELS['haiku']),
        # Bumped from 400 (2026-08-28) to give the new <summary>/<industry> fields real headroom
        # alongside the two existing reasoning fields — same "don't cut off mid-response" lesson
        # as every other max_tokens bump in this file.
        max_tokens=600,
        system=system_prompt,
        messages=[{'role': 'user', 'content': user_message}],
    )
    text = message.content[0].text

    def extract(tag):
        # findall + last match, not re.search's first match (2026-07-22): a model occasionally
        # second-guesses itself mid-response on a genuinely ambiguous case (seen live testing
        # candidate-location context against a real location-conditional Headspace posting —
        # Sonnet emitted a draft <remote>no</remote> block, malformed-closed its reasoning with
        # a stray </remote> instead of </remote_reasoning>, then a corrected <remote>yes</remote>
        # block). re.search's first-match picked up the stale draft verdict. Taking the last
        # match prefers whatever the model settled on.
        matches = re.findall(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL)
        return matches[-1].strip() if matches else ''

    return {
        'remote':             extract('remote').lower(),
        'remote_reasoning':   extract('remote_reasoning'),
        'software':           extract('software').lower(),
        'software_reasoning': extract('software_reasoning'),
        'summary':            _truncate_summary(extract('summary')),
        'industry':           _normalize_industry(extract('industry')),
        'usage':              _usage_dict(message),
    }


def extract_and_classify_job(page_text: str, candidate_location: str = '') -> dict:
    """
    Like classify_job(), but for raw captured page text where we don't already know the title
    or company (e.g. the browser-extension capture flow, before any URL/title match has been
    tried). Extracts title + company + a cleaned job description, alongside the same remote/
    software classification, in one call. Returns {'title': str, 'company_name': str,
    'description': str, 'remote': ..., 'remote_reasoning': ..., 'software': ...,
    'software_reasoning': ...} — remote/software share the same value conventions as
    classify_job(). Raises on API/config failure. candidate_location — see classify_job().

    The 'description' extraction (2026-08-06) is the fix for a real bug: the browser extension
    sends whatever the current page's raw visible text is (document.body.innerText — see
    background.js), with no structural cleanup at all. That's fine when the user is looking at an
    actual single-job page, but a real capture caught the user on a Workable *search results*
    page with a job open in a side panel (URL: .../search?...&selectedJobId=...) — the captured
    text was dominated by ~20 unrelated job listings' worth of chrome that happened to appear
    before the real job's content in reading order. capture_job() used to persist that raw text
    directly (truncated to MAX_JD_CHARS), which meant the real job description got cut off
    mid-heading, right as it was starting, and a cover letter got generated against that garbage.
    Every OTHER ingestion path already has some protection against this — a normal URL fetch
    goes through _extract_readable_text(), which strips <nav>/<header>/<footer> before anything
    else — but the extension's raw innerText has no HTML structure left to strip from by the
    time it reaches this function. Since this call already reads the full, untruncated page text
    to find the title/company/classification, it's a small additional ask to have it isolate and
    return just the real job posting's actual content too, rather than storing the raw noisy text
    and truncating blindly afterward.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key or api_key == 'your-key-here':
        raise RuntimeError('ANTHROPIC_API_KEY not set')
    if not HAS_ANTHROPIC:
        raise RuntimeError('anthropic package not installed')

    client = anthropic_sdk.Anthropic(api_key=api_key)

    system_prompt = (
        "You are given raw scraped text from a job posting web page. This text may be noisy — "
        "it can include site navigation, unrelated job listings (e.g. if the page is a search "
        "results list with one job open in a preview panel), footers, or other boilerplate "
        "around the actual posting. Extract the job title and hiring company name, then extract "
        "the ACTUAL job description itself (responsibilities, requirements, qualifications, "
        "benefits, etc. — everything that is genuinely part of THIS ONE job posting's content, "
        "reproduced closely rather than summarized). Exclude anything that belongs to other job "
        "listings, navigation, or page chrome — if the text contains multiple job postings, "
        "extract only the one that matches the title/company you identified. Then evaluate the "
        "posting against four criteria, with brief reasoning (1-2 sentences each) for the "
        "first two.\n\n"
        + CLASSIFY_CRITERIA
        + SUMMARY_INSTRUCTION
        + INDUSTRY_INSTRUCTION
        + _location_context(candidate_location) +
        "\n\nRespond in exactly this format:\n"
        "<title>...</title>\n"
        "<company>...</company>\n"
        "<description>...</description>\n"
        "<remote>yes|no|unclear</remote>\n"
        "<remote_reasoning>...</remote_reasoning>\n"
        "<software>yes|no|unclear</software>\n"
        "<software_reasoning>...</software_reasoning>\n"
        "<summary>...</summary>\n"
        "<industry>...</industry>"
    )
    user_message = f"**Raw page text:**\n{page_text}"

    message = client.messages.create(
        model=MODELS['haiku'],
        # Real headroom for <description> to echo back a full job posting (up to MAX_JD_CHARS
        # worth), not just the short title/classification fields this call used to be limited
        # to (450) — the same "don't cut off mid-response" lesson as rate_job_fit()'s own
        # max_tokens bump, applied here for the same reason: a response that runs out of budget
        # mid-tag is worse than a merely large one that never gets close to the limit.
        max_tokens=6000,
        system=system_prompt,
        messages=[{'role': 'user', 'content': user_message}],
    )
    text = message.content[0].text

    def extract(tag):
        # last match, not first — see classify_job()'s extract() for why.
        matches = re.findall(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL)
        return matches[-1].strip() if matches else ''

    return {
        'title':               extract('title'),
        'company_name':        extract('company'),
        'description':         extract('description'),
        'remote':              extract('remote').lower(),
        'remote_reasoning':    extract('remote_reasoning'),
        'software':            extract('software').lower(),
        'software_reasoning':  extract('software_reasoning'),
        'summary':             _truncate_summary(extract('summary')),
        'industry':            _normalize_industry(extract('industry')),
        'usage':               _usage_dict(message),
    }


def _build_career_context(career_docs: dict) -> str:
    """Format career docs as context for a Claude call, excluding instructions.md/
    interview_instructions.md (writing style, not background), resume.md (the output template,
    not source material), and career_summary.md (2026-10-07 — a short, separate background doc
    built specifically for rate_job_fit()'s own dedicated context, see that function; including
    it here too would just add it alongside the full Bio doc for generation/interview-prep,
    which don't have the token-cost problem this was built to solve)."""
    EXCLUDE = {'instructions.md', 'interview_instructions.md', 'resume.md', 'resume',
               'career_summary.md', 'career_summary'}
    career_context_parts = []
    for fname, content in career_docs.items():
        if fname not in EXCLUDE and content.strip():
            label = fname.replace('.md', '').replace('_', ' ').title()
            career_context_parts.append(f'## {label}\n{content}')
    return '\n\n---\n\n'.join(career_context_parts)


def rate_job_fit(job_description: str, title: str, company_name: str, career_docs: dict) -> dict:
    """
    Rate how well the user's background (career docs) fits a specific job, 1-5, using the
    user's own rubric. Only meant to be called for jobs already confirmed remote + software-
    relevant with a real JD — this is a distinct, second Claude call from classify_job() so a
    job that turns out not-remote/not-relevant never costs a rating call.

    Also extracts a cleaned version of the job description (2026-08-06) — this call already
    reads the full JD text to produce the rating, so it's a small additional ask to have it also
    isolate just the substantive role content (responsibilities/requirements/qualifications) and
    strip out whatever irrelevant material tends to surround it (company boilerplate, generic
    benefits/perks lists, legal/EEO notices, repeated navigation/apply-button text, or — if this
    text came from a noisy capture — other, unrelated job postings). Callers use this to replace
    job['description'] going forward (falling back to the original if the cleaned version looks
    invalid), so the *stored* JD gets progressively cleaner too, not just this one rating call's
    own judgment — the same text later feeds document generation, where leftover garbage matters
    even more than it does for a rating.

    Returns {'rating': int|None, 'reasoning': str, 'cleaned_description': str, 'usage': dict}.
    Raises on API/config failure.

    Uses career_docs['career_summary.md'] instead of the full career context (2026-10-07) — a
    real, measured cost problem: this call was sending the entire ~72K-character Bio doc on
    every job that reaches it, averaging ~5x classify_job()'s own input tokens per call, and at
    520+ calls so far this was the single largest cost in the whole pipeline. A coarse 1-5 fit
    judgment doesn't need the full narrative detail generation/interview-prep do, just the
    concrete facts (roles, skills, domains, scale) to judge gaps/matches against — see
    career_docs/career_summary.md's own content for the intended altitude. Falls back to the
    full _build_career_context(career_docs) when no career_summary doc is configured yet (empty
    string), so an existing install that hasn't set this up still gets a real rating rather than
    an empty-background judgment.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key or api_key == 'your-key-here':
        raise RuntimeError('ANTHROPIC_API_KEY not set')
    if not HAS_ANTHROPIC:
        raise RuntimeError('anthropic package not installed')

    client = anthropic_sdk.Anthropic(api_key=api_key)
    career_summary = career_docs.get('career_summary.md', '').strip()
    career_context = f'## Career Summary\n{career_summary}' if career_summary else _build_career_context(career_docs)

    system_prompt = (
        "You rate how well a candidate's background fits a specific job, on a 1-5 scale:\n"
        "5 = everything matches\n"
        "4 = strong candidate, few gaps\n"
        "3 = gaps, but they are bridgeable\n"
        "2 = significant gaps\n"
        "1 = not qualified\n\n"
        "Base the rating only on what's explicitly in the candidate's background docs below — "
        "don't assume skills or experience that aren't stated. Give brief reasoning (1-3 sentences) "
        "citing the specific gaps or matches that drove the rating.\n\n"
        "The background doc you're given is a short summary, not the candidate's complete "
        "history, so when you cite a gap, phrase it in terms of what the doc does or doesn't "
        "cover (e.g. \"the background doc doesn't mention X\") rather than asserting the "
        "candidate actually lacks that skill outright. This distinction is deliberate: the "
        "person reading this reasoning uses it to tell apart a real experience gap from a gap "
        "in what their own summary doc happens to cover, so they know when to update it.\n\n"
        "The job description text below may include material that isn't really part of this "
        "specific role's own posting — generic company boilerplate, benefits/perks lists, "
        "legal/EEO notices, repeated navigation or apply-button text, or (if this text was "
        "captured from a search-results page) other, unrelated job postings. Also extract just "
        "the substantive content of THIS role's own posting — responsibilities, requirements, "
        "qualifications — reproduced closely rather than summarized, with that irrelevant "
        "surrounding material removed. Put this AFTER your rating and reasoning, since it may be "
        "long.\n\n"
        "Respond in exactly this format:\n"
        "<rating>1|2|3|4|5</rating>\n"
        "<reasoning>...</reasoning>\n"
        "<cleaned_description>...</cleaned_description>"
    )
    user_message = (
        f"## Candidate Background\n{career_context}\n\n---\n\n"
        f"## Job\n**Title:** {title}\n**Company:** {company_name}\n\n**Job Description:**\n{job_description}"
    )

    message = client.messages.create(
        model=MODELS['haiku'],
        # Real headroom for <cleaned_description> to echo back up to a full JD (MAX_JD_CHARS
        # worth), not just the short rating/reasoning fields this call used to be limited to
        # (600) — <rating>/<reasoning> come FIRST in the requested format specifically so they're
        # always complete even if the description runs long enough to hit this ceiling; losing
        # the cleaned description to truncation just means falling back to the original text
        # (see callers), not losing the rating itself. Same "don't cut off mid-response" lesson
        # as this function's own earlier max_tokens bump (300 -> 600, 2026-07-20) and
        # extract_and_classify_job()'s equivalent bump for the same reason.
        max_tokens=6000,
        system=system_prompt,
        messages=[{'role': 'user', 'content': user_message}],
    )
    text = message.content[0].text

    def extract(tag):
        m = re.search(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL)
        return m.group(1).strip() if m else ''

    rating = None
    try:
        parsed = int(extract('rating'))
        if parsed in (1, 2, 3, 4, 5):
            rating = parsed
    except ValueError:
        pass

    reasoning = extract('reasoning')
    if not reasoning:
        # Belt-and-suspenders: if the response got cut off before the closing </reasoning> tag
        # ever appeared (max_tokens hit mid-sentence — the exact failure mode that silently lost
        # reasoning on 35 of 69 rated jobs, 2026-07-20), grab whatever text follows the opening
        # tag instead of losing it outright. Better an unterminated-but-real fragment than nothing.
        # Bounded to stop at a following <cleaned_description> tag if present, so a truncated
        # reasoning fallback doesn't accidentally swallow the next field too.
        m = re.search(r'<reasoning>(.*?)(?:<cleaned_description>|$)', text, re.DOTALL)
        if m:
            reasoning = m.group(1).strip()

    return {
        'rating': rating,
        'reasoning': reasoning,
        'cleaned_description': extract('cleaned_description'),
        'usage': _usage_dict(message),
    }


# Hardcoded style rules — always enforced, even if instructions.md fails to load. Module-level
# (not local to generate_application_docs()) so generate_interview_prep() shares the exact same
# voice guardrails instead of duplicating them — the user's writing-style preferences apply to
# interview-answer outlines just as much as to cover letters/resumes.
MAX_AUDIT_PASSES = 2  # see generate_application_docs()'s audit loop — bounds the real cost of a
                       # rewrite occasionally introducing a new violation, not an expectation that
                       # it usually needs more than one pass.

HARD_RULES = (
    # 2026-09-03: this constant itself used to open with a stylistic em dash ("STRICT RULES —
    # these override..."), directly undermining the very next line's "NEVER use em dashes"
    # instruction — a real, live-instance case of the exact "do as I say, not as I do" problem
    # found the same day in the user's own Agent Instructions doc (<rules> had 8 of its own).
    'STRICT RULES. These override everything else and must never be violated:\n'
    '- NEVER use em dashes (—). They are an AI giveaway. Use commas, periods, or rewrite the sentence.\n'
    '- NEVER use en dashes (–) as punctuation.\n'
    '- NEVER use buzzwords or corporate filler (e.g. "leverage", "synergy", "impactful", "passionate about").\n'
    '- Write in a direct, human voice. If a sentence sounds like it was written by AI, rewrite it.\n'
    '- NEVER fabricate, calculate, or infer numbers. Only use figures explicitly stated in the career reference doc. '
    'If a number is not in the doc, do not include it. This includes years of experience, team sizes, percentages, and dates.\n\n'
)


def _extract_tagged_sections(doc_text: str, tags: tuple) -> str:
    """Pull out just the given tagged sections from a doc, concatenated in the order given. A
    missing tag just contributes nothing, same tolerance load_career_docs() already has for a
    missing doc/file — not an error. Shared by _extract_instructions_sections() (generation call)
    and _extract_audit_sections() (audit call, 2026-08-27) so the underlying tag-extraction regex
    can't drift out of sync between the two."""
    parts = []
    for tag in tags:
        m = re.search(rf'<{tag}>(.*?)</{tag}>', doc_text, re.DOTALL)
        if m:
            parts.append(m.group(1).strip())
    return '\n\n'.join(parts)


def _extract_instructions_sections(doc_text: str) -> str:
    """Pull out just the <rules> and <generation_guidance> tagged sections from the Agent
    Instructions doc (2026-08-27, user-specified) — generate_application_docs() used to send the
    whole doc as system-prompt context; now only these two tagged sections reach the call."""
    return _extract_tagged_sections(doc_text, ('rules', 'generation_guidance'))


def _extract_audit_sections(doc_text: str) -> str:
    """Pull out just the <rules> and <audit_procedure> tagged sections from the same Agent
    Instructions doc, for the separate audit_cover_letter() call (2026-08-27) — deliberately a
    different pair of tags than the generation call above: the audit call needs the same <rules>
    to check against, plus <audit_procedure> (the self-check/reporting-format instructions),
    not <generation_guidance> (which is about producing the letter, not checking one)."""
    return _extract_tagged_sections(doc_text, ('rules', 'audit_procedure'))


def _parse_audit_response(response_text: str) -> tuple:
    """Parse audit_cover_letter()'s JSON response into (rule_results, corrected_cover_letter).

    Per the Agent Instructions doc's own <audit_procedure> spec, the model returns a JSON array —
    one {"rule","violated","offending_sentence","fix"} object per ## heading in <rules>, plus a
    final {"corrected_cover_letter": "..."} entry. Real, observed model behavior isn't consistent
    about WHERE that final entry lands, though: for a short letter it landed as the array's own
    last element (single ```json fence around the whole thing); for a longer real letter (19 rule
    entries) it came back as a SEPARATE object in its OWN ```json fence, after the array's fence
    closed — found via a real end-to-end test (2026-08-27) where rule_results parsed correctly but
    corrected_cover_letter silently stayed empty, because the naive single-fence-strip-then-parse
    approach discarded everything after the first closing fence. Handles both shapes: first parses
    the array (wherever the leading `[` is, regardless of fencing), then — only if the array
    itself didn't contain a corrected_cover_letter entry — separately locates the last
    "corrected_cover_letter" key in the full text and decodes the JSON object starting at its
    nearest enclosing `{`, independent of the array entirely.

    Raises ValueError if no JSON array could be found at all. A parseable array with no
    corrected_cover_letter found anywhere just returns an empty string for it — callers fall back
    to the original cover letter rather than losing it, same "something beats nothing" instinct as
    _fallback_jd_text/rate_job_fit()'s reasoning fallback."""
    text = response_text.strip()

    start = text.find('[')
    if start == -1:
        raise ValueError('audit response contained no JSON array')
    parsed, _ = json.JSONDecoder().raw_decode(text, start)
    if not isinstance(parsed, list):
        raise ValueError('audit response JSON array was not a list')

    rule_results = []
    corrected_cover_letter = ''
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        if 'corrected_cover_letter' in entry:
            corrected_cover_letter = entry.get('corrected_cover_letter') or ''
        elif 'rule' in entry:
            rule_results.append({
                'rule':               entry.get('rule', ''),
                'violated':           bool(entry.get('violated', False)),
                'offending_sentence': entry.get('offending_sentence') or '',
                'fix':                entry.get('fix') or '',
            })

    if not corrected_cover_letter:
        key_idx = text.rfind('"corrected_cover_letter"')
        if key_idx != -1:
            obj_start = text.rfind('{', 0, key_idx)
            if obj_start != -1:
                try:
                    obj_parsed, _ = json.JSONDecoder().raw_decode(text, obj_start)
                    if isinstance(obj_parsed, dict):
                        corrected_cover_letter = obj_parsed.get('corrected_cover_letter') or ''
                except (ValueError, json.JSONDecodeError):
                    pass

    return rule_results, corrected_cover_letter


def _format_audit_report(rule_results: list) -> str:
    """Render the audit call's per-rule results as a readable text block — used both for the
    console printout during a run and as the '## Cover Letter Audit' section merged into this
    job's own feedback text (2026-08-28, user-specified: attach to the job's own output, not a
    separate log). Leads with a 'Total Violations' count above the per-rule breakdown (also
    2026-08-28, user-specified) so the headline number is visible without reading every line.
    One line per rule after that, violated or not, so the full rule set is always visible, not
    just the failures."""
    violated_count = sum(1 for r in rule_results if r['violated'])
    lines = [f'Total Violations: {violated_count} / {len(rule_results)}', '']
    for r in rule_results:
        if r['violated']:
            lines.append(f"- VIOLATED — {r['rule']}\n  Offending: \"{r['offending_sentence']}\"\n  Fix: \"{r['fix']}\"")
        else:
            lines.append(f"- OK — {r['rule']}")
    return '\n'.join(lines)


def audit_cover_letter(cover_letter: str, career_docs: dict, model_key: str = 'opus') -> dict:
    """
    Second Claude call, run immediately after generate_application_docs() parses its response
    into the application object — dedicated to checking (and fixing) the cover letter against the
    Agent Instructions doc's writing rules (2026-08-27, user-specified: the generation call's own
    system prompt already includes <rules>, but compliance wasn't reliable enough on its own, so
    this is a second pass whose only job is auditing that one output).

    Defaults to 'opus' (2026-09-08) — deliberately decoupled from generation's own model_key
    (generate_application_docs() passes its own audit_model_key through explicitly; this default
    only matters for a direct call). The audit step is a stricter, narrower judgment call
    ("did this violate a rule, and if so, fix it") than drafting a full cover letter, so it's
    worth spending on the strongest available tier even when generation itself runs on Sonnet or
    Haiku for cost reasons.

    Deliberately narrow inputs, per spec: this call sees only the <rules> and <audit_procedure>
    tagged sections of the instructions doc as its system prompt, and the cover_letter text as the
    entire user message. No reference doc, no job description, no other application fields
    (resume bullets/contact/feedback) ever reach this call.

    Returns {'rule_results': [...], 'corrected_cover_letter': str, 'usage': {...}}.
    corrected_cover_letter falls back to the original cover_letter (and rule_results to []) if the
    response couldn't be parsed for any reason — a failed audit must never destroy a perfectly
    good draft.

    model_key (2026-09-22) may also be a GEMINI_MODELS or OPENAI_MODELS key, dispatching to
    _call_gemini()/_call_openai() instead of Anthropic — same <rules>/<audit_procedure> prompt
    either way, since the instructions doc's request for plain tagged JSON output isn't
    Anthropic-specific.
    """
    audit_instructions = _extract_audit_sections(career_docs.get('instructions.md', ''))

    if model_key in GEMINI_MODELS:
        response_text, usage = _call_gemini(model_key, audit_instructions, cover_letter, max_output_tokens=6000)
    elif model_key in OPENAI_MODELS:
        response_text, usage = _call_openai(model_key, audit_instructions, cover_letter, max_output_tokens=6000)
    else:
        api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
        if not api_key or api_key == 'your-key-here':
            raise RuntimeError('ANTHROPIC_API_KEY not set')
        if not HAS_ANTHROPIC:
            raise RuntimeError('anthropic package not installed')

        model = MODELS.get(model_key, MODELS['sonnet'])
        client = anthropic_sdk.Anthropic(api_key=api_key)

        # thinking explicitly disabled (2026-09-21, real bug found via a live __TEST__
        # generation): Claude Opus 5 — the default audit model — runs adaptive thinking when
        # `thinking` is omitted, unlike every other model this app calls. That put a
        # ThinkingBlock at message.content[0], and `.text` below crashed with 'ThinkingBlock'
        # object has no attribute 'text' — silently swallowed by this function's own
        # except-and-fall-back-to-original-draft handling, so every audit pass had been failing
        # without any visible error since 'opus' became the audit default. This call was never
        # designed to parse a leading thinking block (its system prompt asks for a fixed JSON
        # shape, not reasoning), so disabling thinking is the correct fix, not just a workaround
        # — it also avoids paying for thinking tokens this call never wanted. Accepted at this
        # call's default effort (high); would 400 at xhigh/max.
        message = client.messages.create(
            model=model,
            max_tokens=6000,
            system=audit_instructions,
            thinking={'type': 'disabled'},
            messages=[{'role': 'user', 'content': cover_letter}],
        )
        usage = _usage_dict(message)
        response_text = message.content[0].text

    try:
        rule_results, corrected = _parse_audit_response(response_text)
    except Exception as e:
        print(f'  [audit] Could not parse audit response: {e}')
        return {'rule_results': [], 'corrected_cover_letter': cover_letter, 'usage': usage}

    return {
        'rule_results': rule_results,
        'corrected_cover_letter': corrected.strip() or cover_letter,
        'usage': usage,
    }


def generate_application_docs(job: dict, job_description: str, career_docs: dict, model_key: str = 'sonnet',
                               additional_instructions: str = '', audit_model_key: str = None) -> dict:
    """
    Generate cover letter and resume via Claude or Gemini, depending on model_key (2026-09-22 —
    model_key may be a MODELS key or a GEMINI_MODELS key; see the dispatch right before the
    draft call below and _call_gemini()).

    audit_model_key (2026-09-08) controls the model used for the audit pass below —
    deliberately a separate knob from model_key (which model drafts the letter), since the
    audit is a narrower, stricter check that benefits from the strongest available tier even
    when generation itself runs cheaper. Defaults to None, which falls back to 'opus' — every
    real call site (api_generate_application, api_generate_adhoc) passes settings['audit_model']
    explicitly instead of relying on this default; the default only matters for a direct/test call.

    additional_instructions (2026-08-07) is free text the user types per-application, in the
    preview step right before clicking "Generate documents" — personal details or context
    specific to just this one job/company (e.g. a connection they have there, why this company
    matters to them), meant mainly to inform the cover letter. Deliberately NOT restricted to
    only affecting the cover letter section — the user explicitly didn't want that enforced yet,
    so it's just added as extra context in the user message alongside the job description, and
    the model decides where (if anywhere else) it's actually relevant. Not a persistent setting —
    it exists only for this one generation, supplementing (not replacing) instructions.md's
    always-sent generic system prompt.
    """
    # System prompt comes from instructions.md — only the <rules> and <generation_guidance>
    # tagged sections, not the whole doc (2026-08-27, user-specified).
    doc_instructions = _extract_instructions_sections(career_docs.get('instructions.md', ''))
    if doc_instructions:
        system_prompt = HARD_RULES + doc_instructions
    else:
        system_prompt = (
            HARD_RULES +
            'You are an expert job application writer. '
            'Write tailored, specific, honest cover letters and resumes. '
            'Use a confident, direct tone. No buzzwords. Be concrete.'
        )

    # Build the career context (exclude instructions.md and resume.md — resume is the output template)
    career_context = _build_career_context(career_docs)

    additional_instructions = additional_instructions.strip()
    additional_instructions_block = (
        f"\n\n---\n\n## Additional Instructions For This Application\n{additional_instructions}"
        if additional_instructions else ''
    )

    user_message = f"""Here are my career reference materials:

{career_context}

---

## Job I'm Applying To

**Title:** {job.get('title', '')}
**Company:** {job.get('company_name', '')}
**URL:** {job.get('url', '')}

**Job Description:**
{job_description}{additional_instructions_block}"""

    # Dispatch by provider (2026-09-22, Gemini and OpenAI added to the generation model picker)
    # — all three branches produce the same (response_text, usage) shape so everything below
    # (extract(), the audit loop, the return dict) is identical regardless of which provider
    # actually wrote the draft.
    if model_key in GEMINI_MODELS:
        response_text, usage = _call_gemini(model_key, system_prompt, user_message, max_output_tokens=4096)
    elif model_key in OPENAI_MODELS:
        response_text, usage = _call_openai(model_key, system_prompt, user_message, max_output_tokens=4096)
    else:
        api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
        if not api_key or api_key == 'your-key-here':
            raise ValueError('ANTHROPIC_API_KEY not set. Add it to your .env file.')
        if not HAS_ANTHROPIC:
            raise ImportError('anthropic package not installed. Run: pip install anthropic')

        model = MODELS.get(model_key, MODELS['sonnet'])
        client = anthropic_sdk.Anthropic(api_key=api_key)

        # thinking explicitly disabled (2026-09-21) — same fix, same reason, as
        # audit_cover_letter(): Claude Opus 5 runs adaptive thinking by default when `thinking`
        # is omitted, which would put a ThinkingBlock at message.content[0] and crash the `.text`
        # read below exactly like it did for the audit call. This call's response format (fixed
        # XML tags) was never designed around a leading thinking block, so disabling it is
        # correct, not a workaround. Accepted at this call's default effort (high); would 400 at
        # xhigh/max.
        message = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            thinking={'type': 'disabled'},
            messages=[{'role': 'user', 'content': user_message}],
        )
        response_text = message.content[0].text
        usage = {'input_tokens': message.usage.input_tokens, 'output_tokens': message.usage.output_tokens}

    def extract(tag):
        m = re.search(rf'<{tag}>(.*?)</{tag}>', response_text, re.DOTALL)
        return m.group(1).strip() if m else ''

    # Resume sub-sections
    intro        = extract('introduction')
    metis        = extract('metis_bullets')
    rentredi     = extract('rentredi_bullets')
    sonos        = extract('sonos_bullets')
    spotify_pm   = extract('spotify_pm_bullets')
    spotify_eng  = extract('spotify_eng_bullets')

    # Log what was parsed so we can debug template mismatches
    print(f'  [parse] cover_letter: {len(extract("cover_letter"))} chars')
    print(f'  [parse] introduction: {len(intro)} chars')
    print(f'  [parse] rentredi_bullets: {len(rentredi)} chars')
    print(f'  [parse] metis_bullets: {len(metis)} chars')
    print(f'  [parse] sonos_bullets: {len(sonos)} chars')
    print(f'  [parse] spotify_pm_bullets: {len(spotify_pm)} chars')
    print(f'  [parse] spotify_eng_bullets: {len(spotify_eng)} chars')

    # Assemble resume text from parts (headers injected here so Claude doesn't need them)
    resume_parts = []
    if intro:
        resume_parts.append(intro)
    if rentredi:
        resume_parts.append('## RentRedi — Growth Product Manager (Oct 2025–Jun 2026)\n' + rentredi)
    if metis:
        resume_parts.append('## Metis Health Technologies — Product Manager & Engineer (Jun 2025–Oct 2025)\n' + metis)
    if sonos:
        resume_parts.append('## Sonos — Senior Technical Product Manager (May 2023–Aug 2024)\n' + sonos)
    if spotify_pm:
        resume_parts.append('## Spotify — Product Manager, Personalization (Mar 2020–Jan 2023)\n' + spotify_pm)
    if spotify_eng:
        resume_parts.append('## Spotify — Backend & Data Engineer (May 2012–Mar 2020)\n' + spotify_eng)
    resume_text = '\n\n'.join(resume_parts)

    # Contact info
    contact_phone   = extract('phone')
    contact_address = extract('address')
    # Company mission (2026-09-15, user-added to Agent Instructions): the model looks up the
    # company's stated mission/goals and returns it as a <mission> tag, grouped with PHONE/
    # ADDRESS under 'contact' per the user's own instruction ("place the information in the
    # 'mission' section of the 'contact' object") rather than as its own top-level field.
    contact_mission = extract('mission')

    # Feedback
    strategy     = extract('strategy')
    missing_info = extract('missing_info')
    assessment   = extract('assessment')
    feedback_parts = []
    if strategy:
        feedback_parts.append('## Strategy\n' + strategy)
    if missing_info:
        feedback_parts.append('## Missing Info / Suggestions\n' + missing_info)
    if assessment:
        feedback_parts.append('## Assessment\n' + assessment)
    feedback_text = '\n\n'.join(feedback_parts)

    # Fallback: if new format not found, try legacy single-tag parse
    cover_letter = extract('cover_letter')
    if not cover_letter and not resume_text:
        cl_match  = re.search(r'<cover_letter>(.*?)</cover_letter>', response_text, re.DOTALL)
        res_match = re.search(r'<resume>(.*?)</resume>', response_text, re.DOTALL)
        cover_letter = cl_match.group(1).strip() if cl_match else response_text
        resume_text  = res_match.group(1).strip() if res_match else ''

    # usage was already set above by whichever provider branch ran (Anthropic or Gemini —
    # see _call_gemini()) — no longer rebuilt here from `message`, which only exists in the
    # Anthropic branch and would NameError for a Gemini-drafted generation.

    # Audit pass (2026-08-27, user-specified): a second, narrower call that checks (and fixes)
    # just the cover letter against the instructions doc's <rules>/<audit_procedure> sections —
    # run here, right after the first call's response is parsed, so the corrected text is what
    # every downstream consumer of this function's return value (local files, Drive, the filled
    # Google Doc, the frontend) ever sees. A failed/unparseable audit call falls back to the
    # original cover_letter (see audit_cover_letter()) rather than blocking generation entirely —
    # same soft-fail instinct as every other non-essential step in this pipeline.
    #
    # Per-rule results (2026-08-28, user-specified correction — this used to write a separate
    # cross-job CSV log, which the user explicitly said NOT to do: "not stored anywhere separate
    # from the job") are printed to console during the run AND returned as their own
    # 'cover_letter_audit' field on this same application object — deliberately kept OUT of
    # 'feedback' itself, since that field also feeds _update_todo_doc()'s gap-mining call
    # (save_application_docs()), and raw rule-violation text ("never use em dash") would get
    # misread as an actionable to-do item there. save_application_docs() is what merges this
    # field into the feedback text actually written to Feedback.txt/sections.json/Drive, after
    # the to-do call has already run on the original, un-merged feedback.
    #
    # Runs in a loop, up to MAX_AUDIT_PASSES (2026-09-03, real bug found via a live Accumulus
    # generation): a single pass audits draft V1 and produces corrected text V2 in the same
    # response, but never re-checks V2 itself — and V2 is a real rewrite, not a diff, so fixing
    # the sentences V1 was flagged for can introduce entirely new violations that were never in V1
    # and are never caught. Confirmed for real: that generation's audit correctly found and fixed
    # 2 em-dash violations, but the rewritten paragraph it produced introduced 4 new em dashes
    # elsewhere that made it straight into the saved file, because nothing ever looked at V2 again.
    # Looping the exact same audit call against its own output catches this — pass 2 audits V2 the
    # same way pass 1 audited V1. Stops early once a pass reports zero violations; capped rather
    # than looped unboundedly since each pass is a real paid call and this is a mitigation for a
    # rewrite occasionally introducing a new issue, not a expectation that it usually will.
    # audit_usage (2026-09-21) is tracked separately from `usage` above, rather than folded in as
    # it used to be — the audit pass can run on a different model than generation itself (see
    # audit_model_key), so blending the two into one bucket would misattribute cost to whichever
    # model happened to draft the letter. Kept alongside `usage` in the return dict so the caller
    # can record 'generation' and 'cover_letter_audit' as two separate _add_token_usage() calls,
    # each tagged with its own real model_key.
    rule_results = []
    audit_usage = {'input_tokens': 0, 'output_tokens': 0}
    effective_audit_model_key = audit_model_key or 'opus'
    try:
        for _ in range(MAX_AUDIT_PASSES):
            audit_result = audit_cover_letter(cover_letter, career_docs, effective_audit_model_key)
            cover_letter = audit_result['corrected_cover_letter']
            rule_results = audit_result['rule_results']
            audit_usage['input_tokens'] += audit_result['usage']['input_tokens']
            audit_usage['output_tokens'] += audit_result['usage']['output_tokens']
            if not any(r['violated'] for r in rule_results):
                break
        if rule_results:
            print('  [audit] ' + _format_audit_report(rule_results).replace('\n', '\n  '))
    except Exception as e:
        print(f'  [audit] Cover letter audit call failed, keeping unaudited draft: {e}')

    # Deterministic last-resort net for the em/en dash rules specifically (2026-09-03) — every
    # other rule in <rules> is a judgment call only a model can make, but "does this exact
    # character appear" is 100% mechanically checkable, so this doesn't have to remain
    # probabilistic like the rest of the audit. Only fires if the multi-pass audit above still
    # missed one — logged loudly when it does, since that means the LLM-based check genuinely
    # failed and this caught what it didn't, not routine behavior.
    if '—' in cover_letter or '–' in cover_letter:
        print(f'  [audit] WARNING: em/en dash survived {MAX_AUDIT_PASSES} audit passes — stripping deterministically')
        cover_letter = cover_letter.replace('—', ', ').replace('–', '-')

    return {
        'cover_letter': cover_letter,
        'resume': resume_text,
        'sections': {
            'COVER_LETTER':       cover_letter,
            'INTRODUCTION':       intro,
            'RENTREDI_BULLETS':   rentredi,
            'METIS_BULLETS':      metis,
            'SONOS_BULLETS':      sonos,
            'SPOTIFY_PM_BULLETS': spotify_pm,
            'SPOTIFY_ENG_BULLETS':spotify_eng,
        },
        'contact': {
            'PHONE':   contact_phone,
            'ADDRESS': contact_address,
            'MISSION': contact_mission,
        },
        'feedback': feedback_text,
        'cover_letter_audit': rule_results,
        'model_key': model_key,
        'audit_model_key': effective_audit_model_key,
        'usage': usage,
        'audit_usage': audit_usage,
    }


def generate_interview_prep(job: dict, job_description: str, career_docs: dict, model_key: str = 'sonnet',
                             interviewer_name: str = '', interviewer_title: str = '',
                             additional_info: str = '') -> tuple:
    """
    Draft likely interview questions for one interview round, each paired with a brief outline of
    what to cover in the answer — modeled directly on generate_application_docs() (same
    HARD_RULES voice guardrails, same _build_career_context() background material, which already
    correctly excludes instructions.md/resume.md/interview_instructions.md: writing-style
    guidance and the output template are no more interview-prep source material than they are
    application source material).

    System prompt comes from career_docs['interview_instructions.md'] if the user has configured
    one (Settings → "Interview Instructions", same gdoc_urls pattern as Agent Instructions),
    exactly like generate_application_docs() defers to instructions.md — a full override, same as
    that field, so it always wins regardless of which round this is. Otherwise:
      - No interviewer_name/title/additional_info given (the initial "Interviewing" click, always
        the first round): built-in **recruiter-screen** default (2026-08-04) — high-level and
        screening-focused (background walkthrough, motivation/fit, logistics) rather than deep
        technical/role-specific, which later rounds are for.
      - Any of interviewer_name/title/additional_info given (the "Add Interview" flow, 2026-08-04):
        a built-in **adaptive** default instead — explicitly told to infer likely depth/focus
        from the interviewer's title (a hiring-manager round probes fit/ownership differently
        than a peer engineer's technical round, which differs again from a final round with an
        exec) rather than assuming any one fixed stage. This is the whole "mechanism for
        additional interviews" — free-text interviewer context the model reasons about, not a
        fixed stage enum, since real interview processes vary too much across companies for a
        fixed set of stages to fit well.
    Either way, interviewer_name/title/additional_info (when given) are also included directly in
    the user message, so the model has them regardless of which system prompt branch applies.

    Response is a variable-length list of questions, so this is the one place this app's usual
    fixed-named-tag extraction doesn't fit — asks for a repeating <qa><question>...</question>
    <outline>...</outline></qa> block instead, parsed with re.findall (same "extract every match"
    approach the classify_job() self-correction fix uses, just applied to a repeating structure
    instead of "keep the last one").

    Returns ([{'question': str, 'outline': str}, ...], usage_dict) — a tuple, not just the
    questions list, so the caller can record token cost the same way every other Claude-calling
    function here does (see _add_token_usage()). Raises on API/config failure, same as
    generate_application_docs().
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key or api_key == 'your-key-here':
        raise ValueError('ANTHROPIC_API_KEY not set. Add it to your .env file.')
    if not HAS_ANTHROPIC:
        raise ImportError('anthropic package not installed. Run: pip install anthropic')

    model = MODELS.get(model_key, MODELS['sonnet'])
    client = anthropic_sdk.Anthropic(api_key=api_key)

    career_context = _build_career_context(career_docs)
    has_interviewer_context = bool(interviewer_name or interviewer_title or additional_info)

    interview_instructions = career_docs.get('interview_instructions.md', '').strip()
    if interview_instructions:
        system_prompt = HARD_RULES + interview_instructions
    elif has_interviewer_context:
        system_prompt = (
            HARD_RULES +
            'You help a candidate prepare for a specific interview round. You are given the job '
            'description, who they will be speaking with (name/title, and any additional context '
            'about this round) below, and the candidate\'s career background. Infer the likely '
            'depth and focus of this round from the interviewer\'s title and the additional '
            'context — a hiring-manager round probes fit and ownership differently than a peer '
            'engineer\'s technical round, which differs again from a final round with an '
            'executive — rather than assuming any one fixed interview stage. Draft a realistic '
            'set of questions for this specific round. For each question, write a brief outline '
            '(not a full scripted answer) of what the candidate should touch on, grounded '
            'specifically in real experience from their career background below — name which '
            'specific story, role, or accomplishment fits, not generic advice.'
        )
    else:
        system_prompt = (
            HARD_RULES +
            'You help a candidate prepare for the initial recruiter screen — the first interview '
            'in the process, before any hiring-manager or technical rounds. Recruiter screens are '
            'high-level and screening-focused: a walk-through of background and career '
            'trajectory, motivation/fit for this specific role and company, and logistics '
            '(compensation expectations, availability, location/work authorization) — not deep '
            'technical drilling or role-specific problem-solving, which is what later rounds are '
            'for. Given the job description and the candidate\'s career background, draft a '
            'realistic set of questions a recruiter would actually ask at this stage, with only '
            'light, high-level treatment of the role\'s core responsibilities. For each question, '
            'write a brief outline (not a full scripted answer) of what the candidate should '
            'touch on, grounded specifically in real experience from their career background '
            'below — name which specific story, role, or accomplishment fits, not generic advice.'
        )

    interviewer_block = ''
    if has_interviewer_context:
        interviewer_block = f"""

## Interviewer
**Name:** {interviewer_name or '(not provided)'}
**Title:** {interviewer_title or '(not provided)'}
**Additional context about this round:** {additional_info or '(none)'}"""

    round_instruction = ('for this specific interview round' if has_interviewer_context
                          else 'for the initial recruiter screen for this role')

    user_message = f"""Here is my career background:

{career_context}

---

## Job I'm Interviewing For

**Title:** {job.get('title', '')}
**Company:** {job.get('company_name', '')}

**Job Description:**
{job_description}
{interviewer_block}

---

Draft 6-8 realistic interview questions {round_instruction}, each with a brief outline of what I
should cover in my answer. Respond in exactly this format, one block per question:

<qa>
<question>...</question>
<outline>...</outline>
</qa>"""

    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{'role': 'user', 'content': user_message}],
    )
    response_text = message.content[0].text

    pairs = re.findall(
        r'<qa>\s*<question>(.*?)</question>\s*<outline>(.*?)</outline>\s*</qa>',
        response_text, re.DOTALL,
    )
    questions = [{'question': q.strip(), 'outline': o.strip()} for q, o in pairs]
    return questions, _usage_dict(message)


# ─────────────────────────────────────────────
# Interview practice (2026-10-13)
# ─────────────────────────────────────────────
# A static bank, not model-generated — these are standard recruiter-screen questions covering
# the same topics generate_interview_prep()'s own recruiter-screen default targets (background/
# trajectory, motivation/fit, logistics), but genuinely generic rather than grounded in any one
# job description, so there's nothing for a Claude call to add over just listing them.
GENERIC_RECRUITER_QUESTIONS = [
    "Tell me about yourself and walk me through your background.",
    "What's motivating your job search right now?",
    "What are you looking for in your next role?",
    "What do you look for in a company before you apply?",
    "Walk me through a time you had a conflict with a coworker or stakeholder, and how you resolved it.",
    "Tell me about a time you made a mistake or something you worked on failed. What did you learn?",
    "What's a professional accomplishment you're most proud of?",
    "How do you prioritize when you have multiple competing deadlines?",
    "What's your biggest strength, and what's an area you're actively working to improve?",
    "What are your compensation expectations?",
    "What's your availability to start, and do you have any notice period?",
    "Do you have any questions for me?",
]


def grade_interview_answer(question: str, answer: str, outline: str, career_docs: dict,
                            model_key: str = 'sonnet') -> dict:
    """
    Feedback on one practice answer to an interview question (2026-10-13) — for the Interview
    Practice area, where the user dictates or types an answer and wants a read on it before the
    real thing. Not voice/audio itself — the answer text already comes from the browser's own
    speech-to-text (or typing); this only ever sees the transcript.

    `outline` is the matching question's own outline from generate_interview_prep() when
    practicing a job-specific question (empty string for a generic recruiter-bank question) — if
    present, feedback can note whether the answer actually covered what was expected, grounded in
    the same real-story framing that outline was built from. `career_docs` is passed through
    exactly like rate_job_fit()/generate_application_docs() (the Career Summary doc if
    configured, else the full Bio doc) so feedback can flag a claim that doesn't match anything
    in the candidate's own documented background, the same "be honest and specific" grounding
    this app already holds generation to — just checked in the opposite direction here (the
    answer against the doc, not a job against the doc).

    Returns {'strengths': str, 'improvements': str, 'delivery': str, 'overall': str,
    'usage': dict}. Raises on API/config failure.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key or api_key == 'your-key-here':
        raise ValueError('ANTHROPIC_API_KEY not set. Add it to your .env file.')
    if not HAS_ANTHROPIC:
        raise ImportError('anthropic package not installed. Run: pip install anthropic')

    model = MODELS.get(model_key, MODELS['sonnet'])
    client = anthropic_sdk.Anthropic(api_key=api_key)
    career_summary = career_docs.get('career_summary.md', '').strip()
    career_context = f'## Career Summary\n{career_summary}' if career_summary else _build_career_context(career_docs)

    system_prompt = (
        "You give direct, specific feedback on a candidate's practice answer to an interview "
        "question. The answer may come from dictated speech-to-text rather than writing, so "
        "judge content and delivery as two separate things.\n\n"
        "Give feedback in four parts:\n"
        "1. Strengths — what the answer does well, specifically (cite the actual phrase or "
        "story, don't just say 'good example').\n"
        "2. Improvements — what's missing, vague, or could be stronger. If the candidate's "
        "background doc below contradicts or doesn't support a claim the answer makes, say so "
        "directly rather than assuming the claim is true. If an outline of what to cover is "
        "provided, note anything the outline expected that the answer didn't touch on.\n"
        "3. Delivery — specifically about how it was said, not what: filler words (um, like, "
        "you know), rambling or run-on structure, pacing, length (too short to be convincing, "
        "or long enough to lose the interviewer). Speech-to-text transcripts can have minor "
        "transcription artifacts (missing punctuation, an odd word substitution) — don't flag "
        "those as delivery problems, they're not real.\n"
        "4. Overall — one or two sentences, direct, on whether this answer would land well in a "
        "real interview.\n\n"
        "Be honest rather than encouraging for its own sake — this is practice specifically "
        "because the answer isn't polished yet.\n\n"
        "Respond in exactly this format:\n"
        "<strengths>...</strengths>\n"
        "<improvements>...</improvements>\n"
        "<delivery>...</delivery>\n"
        "<overall>...</overall>"
    )

    outline_block = f"\n\n## What a Strong Answer Would Cover\n{outline}" if outline.strip() else ''
    user_message = (
        f"## Candidate Background\n{career_context}\n\n---\n\n"
        f"## Question\n{question}{outline_block}\n\n---\n\n"
        f"## Candidate's Answer\n{answer}"
    )

    message = client.messages.create(
        model=model,
        max_tokens=1500,
        system=system_prompt,
        messages=[{'role': 'user', 'content': user_message}],
    )
    text = message.content[0].text

    def extract(tag):
        m = re.search(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL)
        return m.group(1).strip() if m else ''

    return {
        'strengths':    extract('strengths'),
        'improvements': extract('improvements'),
        'delivery':      extract('delivery'),
        'overall':       extract('overall'),
        'usage':         _usage_dict(message),
    }


@app.route('/api/interview-practice/questions', methods=['GET'])
def api_get_interview_practice_questions():
    """
    Generic recruiter-bank questions plus every job-specific question already generated across
    every application's interview_prep.json (via list_interviews(), the same permanent disk
    record the Interviews area itself reads — no separate storage for this feature). Flattened
    into one list per question (not per round) so the frontend can offer a flat picker without
    its own flattening logic duplicating list_interviews()'s shape.
    """
    generic = [{'question': q, 'outline': '', 'source': 'generic'} for q in GENERIC_RECRUITER_QUESTIONS]

    job_specific = []
    for interview in list_interviews():
        for round_ in interview.get('interviews', []):
            for qa in round_.get('questions', []):
                job_specific.append({
                    'question':        qa.get('question', ''),
                    'outline':         qa.get('outline', ''),
                    'source':          'job_specific',
                    'application_id':  interview['id'],
                    'company_name':    interview['company_name'],
                    'title':           interview['title'],
                    'round_heading':   round_.get('heading', ''),
                })

    return jsonify({'generic': generic, 'job_specific': job_specific})


@app.route('/api/interview-practice/feedback', methods=['POST'])
def api_get_interview_practice_feedback():
    """Grade one practice answer — see grade_interview_answer(). Records token usage directly
    into the metrics database (no job record to attach to; this feature is independent of any
    one job's lifecycle and shouldn't need one to exist)."""
    body = request.json or {}
    question = (body.get('question') or '').strip()
    answer = (body.get('answer') or '').strip()
    outline = body.get('outline') or ''
    if not question or not answer:
        return jsonify({'error': 'Both question and answer are required.'}), 400

    data = load_data()
    settings = data.get('settings', {})
    model_key = _resolve_claude_model_key(settings.get('model', 'sonnet'))
    career_docs = load_career_docs(settings)
    if not any(v.strip() for v in career_docs.values()):
        return jsonify({'error': 'No career docs found. Add content to career_docs/ or add Google Doc URLs in settings.'}), 400

    try:
        result = grade_interview_answer(question, answer, outline, career_docs, model_key)
    except (ValueError, ImportError) as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    _record_daily_token_usage('interview_practice_feedback', model_key, result.get('usage'))

    return jsonify({
        'strengths':    result['strengths'],
        'improvements': result['improvements'],
        'delivery':      result['delivery'],
        'overall':       result['overall'],
    })


def _safe_name(s: str, maxlen: int = 50) -> str:
    """Strip characters that are invalid in folder/file names."""
    return re.sub(r'[<>:"/\\|?*]', '', s).strip()[:maxlen]


def get_output_dir(job: dict) -> str:
    """
    Return today's local cache output folder path for a job (may not exist yet). Deliberately
    NOT the Drive-synced OUTPUT_FOLDER (2026-07-21) — Drive Desktop independently syncs anything
    written under a Drive-watched path up to Drive on its own schedule, racing this app's own
    direct Drive API calls (see the Google Drive folder step in save_application_docs()), which
    caused real duplicate folders three times. This is now a purely local cache Drive Desktop
    never sees; the Drive-visible copy is produced entirely by direct API upload instead, so
    there's only ever one writer creating anything in the Drive "Applications" tree.
    """
    company  = _safe_name(job.get('company_name', 'Company'))
    title    = _safe_name(job.get('title', 'Role'))
    date_str = datetime.now().strftime('%Y-%m-%d')
    output_root = os.path.join(BASE_DIR, 'applications')
    return os.path.join(output_root, company, title, date_str)


def _find_existing_jd_file(job: dict) -> str:
    """
    Look for an already-saved Job Description.txt for this job, checking the current local
    cache path first, then falling back to the old Drive-synced OUTPUT_FOLDER path (see
    get_output_dir()'s docstring) for anything generated before 2026-07-21's local-path change.
    Returns the file path if found, else ''.
    """
    candidate = os.path.join(get_output_dir(job), 'Job Description.txt')
    if os.path.isfile(candidate):
        return candidate
    legacy_root = os.environ.get('OUTPUT_FOLDER', '')
    if legacy_root:
        company = _safe_name(job.get('company_name', 'Company'))
        title = _safe_name(job.get('title', 'Role'))
        date_str = datetime.now().strftime('%Y-%m-%d')
        legacy_candidate = os.path.join(legacy_root, company, title, date_str, 'Job Description.txt')
        if os.path.isfile(legacy_candidate):
            return legacy_candidate
    return ''


def _resolve_job_description_for_generation(job: dict) -> str:
    """
    Resolve the job description text for the preview/generate routes, preferring already-known-
    good sources over a fresh network fetch, in order:
      1. An existing saved Job Description.txt from a prior attempt today
         (_find_existing_jd_file), IF its content is actually real (is_real_job_description()) —
         preserves pasted content and avoids re-scraping. A stale file whose content is itself a
         fetch-failure message does NOT count here (see below) — it's treated the same as no file
         existing at all, so a broken first attempt doesn't poison every regenerate for the rest
         of the day.
      2. job['description'] if it's real — the text already fetched/classified at ingestion time.
      3. A fresh fetch_job_description(job) call — last resort, for jobs whose description was
         never populated (e.g. old records from before full JD text was always persisted, or a
         job added without ever going through classification).

    Step 2 is the fix for a real bug (found 2026-08-05, a House Rx / Himalayas job): both routes
    used to skip straight from step 1 to step 3, ignoring job['description'] entirely. That's
    fine for most sources (a fresh fetch usually just re-confirms the same text), but Jobicy and
    Himalayas jobs can NEVER be re-fetched from job['url'] — their real pages are permanently
    Cloudflare-blocked (confirmed live, see sweep_jobicy_jobs()/sweep_himalayas_jobs() module
    notes), which is exactly why evaluate_candidates() stashes their real JD text via the
    _fallback_jd_text mechanism at ingestion time instead of relying on a fetch. Skipping straight
    to step 3 here meant generation always re-triggered that same permanent fetch failure and
    handed Claude a literal "(Could not fetch job description: HTTP 403)" string as the job
    description — which Claude then, correctly and unhelpfully, reported back verbatim in the
    generated cover letter — even though the real, full text was already sitting in
    job['description'] the whole time.

    The "IF its content is real" qualifier on step 1 is a second, related fix (same day, found
    immediately after the first): the first fix alone didn't help a same-day *regenerate*, because
    the original failed attempt had already written that exact fetch-failure string to disk as
    Job Description.txt, and step 1 was unconditionally trusting any existing file — so it kept
    reusing the bad cached file every time, never even reaching step 2. Real user report that
    caught this: regenerating House Rx still produced the same broken output, same day.
    """
    jd_file = _find_existing_jd_file(job)
    if jd_file:
        with open(jd_file, encoding='utf-8') as f:
            existing_text = f.read()
        if is_real_job_description(existing_text):
            return existing_text
    stored = job.get('description', '')
    if is_real_job_description(stored):
        return stored
    return fetch_job_description(job)


def _find_latest_dated_folder(job_dir: str) -> tuple:
    """
    Given a Company/Title-level directory, find the most recent dated subfolder that has a real
    Cover Letter.txt — the "most recent folder that actually has content" check
    api_existing_documents() used to do inline; factored out (2026-07-22) so list_applications()
    can reuse the exact same logic disk-side, without needing a job record to resolve the path
    first. Returns (folder_path, date_str), or (None, None) if nothing usable exists yet.
    """
    if not os.path.isdir(job_dir):
        return None, None
    date_folders = sorted(
        [d for d in os.listdir(job_dir) if os.path.isdir(os.path.join(job_dir, d))],
        reverse=True
    )
    for d in date_folders:
        if os.path.isfile(os.path.join(job_dir, d, 'Cover Letter.txt')):
            return os.path.join(job_dir, d), d
    return None, None


def _application_id(company_name: str, title: str) -> str:
    """
    Stable id for an application, derived only from company+title (via the same _safe_name()
    used for the folder names themselves) — survives regeneration on a new date and doesn't
    depend on data['jobs'] at all, unlike a job's own id. This is what makes an application
    findable even after its underlying job record is purged by cleanup_old_jobs()'s 30-day
    retention window (generated documents are never deleted — see CLAUDE.md).
    """
    return hashlib.md5(f'{_safe_name(company_name)}|{_safe_name(title)}'.encode()).hexdigest()


def _load_application_content(folder: str, date_str: str) -> dict:
    """Load one generated application's content from its dated folder — same shape whether
    reached via a job record (api_existing_documents-style) or purely from disk
    (list_applications()). Also reads the sibling Job Description.txt (always saved into this
    same folder by save_application_docs()) so callers — notably the Interview detail page,
    which otherwise has no way to see what the interview is actually about — can show the
    original JD without needing a live data['jobs'] record."""
    import json as _json
    jd_path = os.path.join(folder, 'Job Description.txt')
    job_description = ''
    if os.path.isfile(jd_path):
        with open(jd_path, encoding='utf-8') as f:
            job_description = f.read()
    sj_path = os.path.join(folder, 'sections.json')
    if os.path.isfile(sj_path):
        with open(sj_path, encoding='utf-8') as f:
            saved = _json.load(f)
        return {
            'cover_letter':   saved.get('cover_letter', ''),
            'sections':       saved.get('sections', {}),
            'feedback':       saved.get('feedback', ''),
            'contact':        saved.get('contact', {}),
            'generated_at':   saved.get('generated_at', date_str),
            'google_doc_url': saved.get('google_doc_url', ''),
            'todo_changes':   saved.get('todo_changes'),
            'generation_model': saved.get('generation_model', ''),
            'audit_model':      saved.get('audit_model', ''),
            'audit_total_rules':    saved.get('audit_total_rules'),
            'audit_violated_count': saved.get('audit_violated_count'),
            'quality_rating':       saved.get('quality_rating'),
            'additional_instructions': saved.get('additional_instructions', ''),
            'job_description': job_description,
        }
    with open(os.path.join(folder, 'Cover Letter.txt'), encoding='utf-8') as f:
        cover_letter = f.read()
    return {
        'cover_letter': cover_letter, 'sections': {}, 'feedback': '', 'contact': {},
        'generated_at': date_str, 'google_doc_url': '', 'todo_changes': None,
        'generation_model': '', 'audit_model': '',
        'audit_total_rules': None, 'audit_violated_count': None, 'quality_rating': None,
        'additional_instructions': '', 'job_description': job_description,
    }


def _walk_application_folders() -> list:
    """
    Shared disk walk behind list_applications()/list_interviews() — every
    <Company>/<Title> pair under BASE_DIR/applications (plus the legacy Drive-synced
    OUTPUT_FOLDER, if set — real, substantial application history from before 2026-07-21's
    local-path fix still lives only there) that has a real generated application (a dated
    folder with Cover Letter.txt), collapsed to the single latest dated folder per pair —
    matching how regeneration already works (in place same-day, new dated folder on a new day).
    When the same (company, title) exists under both roots, the newer-dated one wins.

    Returns the raw per-entry disk facts only — no job-record join, no interview-status
    filtering; list_applications() and list_interviews() each apply their own filter on top of
    this same shared list so the two stay structurally in sync (an entry can only ever appear in
    one of them, by construction, not by two independently-maintained conditions).
    """
    roots = [os.path.join(BASE_DIR, 'applications')]
    legacy_root = os.environ.get('OUTPUT_FOLDER', '')
    if legacy_root:
        roots.append(legacy_root)

    by_key = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for company_dir in sorted(os.listdir(root)):
            company_path = os.path.join(root, company_dir)
            if not os.path.isdir(company_path):
                continue
            for title_dir in sorted(os.listdir(company_path)):
                title_path = os.path.join(company_path, title_dir)
                if not os.path.isdir(title_path):
                    continue
                folder, date_str = _find_latest_dated_folder(title_path)
                if not folder:
                    continue
                key = (company_dir, title_dir)
                existing = by_key.get(key)
                if existing and existing['date'] >= date_str:
                    continue  # already have an equal-or-newer entry for this key from the other root
                by_key[key] = {'company_dir': company_dir, 'title_dir': title_dir, 'folder': folder, 'date': date_str}

    return list(by_key.values())


def _jobs_by_safe_name(data: dict) -> dict:
    """(_safe_name(company_name), _safe_name(title)) -> job record, for reversing the folder
    naming back to a live data['jobs'] entry. Shared by list_applications()/list_interviews()."""
    return {
        (_safe_name(j.get('company_name', '')), _safe_name(j.get('title', ''))): j
        for j in data.get('jobs', [])
    }


def list_applications() -> list:
    """
    Every job that has had documents generated but hasn't moved on to interview prep — scanned
    directly from disk via _walk_application_folders(), not from data['jobs']. Generated
    documents are the permanent record (data['jobs'] entries get purged after JOB_RETENTION_DAYS
    by cleanup_old_jobs() — see CLAUDE.md's 30-day retention section); sourcing this list from
    data['jobs'] instead would silently lose older applications from view once their job record
    aged out, even though the actual generated docs are still sitting on disk.

    An entry with an `interview_prep.json` in its folder has "moved" to Interviews (see
    list_interviews()) — presence of that file, not a stored flag, same pattern as "has generated
    docs at all" already uses to move a job from Jobs to Applications in the first place.

    Best-effort joins back to a live data['jobs'] record (via _jobs_by_safe_name()) for
    job_id/url/is_applied/fit_rating — these come back empty/None once the underlying job record
    has been purged, which this list is specifically designed to survive. Scale: generation is
    manual and deliberate (a real Claude cost), so this is realistically tens of entries, not
    thousands — a full directory walk per request is fine.
    """
    data = load_data()
    jobs_by_safe_name = _jobs_by_safe_name(data)

    results = []
    for entry in _walk_application_folders():
        folder, date_str = entry['folder'], entry['date']
        if os.path.isfile(os.path.join(folder, 'interview_prep.json')):
            continue
        key = (entry['company_dir'], entry['title_dir'])
        content = _load_application_content(folder, date_str)
        matched_job = jobs_by_safe_name.get(key)
        results.append({
            'id':             _application_id(*key),
            'company_name':   matched_job.get('company_name') if matched_job else entry['company_dir'],
            'title':          matched_job.get('title') if matched_job else entry['title_dir'],
            'folder':         folder,
            'date':           date_str,
            'generated_at':   content['generated_at'],
            'google_doc_url': content['google_doc_url'],
            'has_feedback':   bool(content['feedback']),
            'job_id':         matched_job.get('id') if matched_job else None,
            'url':            matched_job.get('url', '') if matched_job else '',
            'is_applied':     matched_job.get('is_applied', False) if matched_job else False,
            'fit_rating':     matched_job.get('fit_rating') if matched_job else None,
            'fit_reasoning':  matched_job.get('fit_reasoning', '') if matched_job else '',
        })

    return sorted(results, key=lambda r: r['generated_at'], reverse=True)


@app.route('/api/applications', methods=['GET'])
def api_list_applications():
    return jsonify(list_applications())


@app.route('/api/applications/<app_id>', methods=['GET'])
def api_get_application(app_id):
    match = next((a for a in list_applications() if a['id'] == app_id), None)
    if not match:
        return jsonify({'error': 'Application not found'}), 404
    content = _load_application_content(match['folder'], match['date'])
    return jsonify({**match, **content})


def _resolve_application_folder(app_id: str) -> dict | None:
    """
    Find an application's folder entry by id, matching _walk_application_folders() facts
    directly — NOT via list_applications()/list_interviews(), which each apply the opposite
    filter on interview_prep.json's presence (see those functions' docstrings) and would 404 on
    exactly the application an interview route needs to find, depending on which side of that
    filter it's currently on. Shared by every interview route below.
    """
    return next(
        (e for e in _walk_application_folders()
         if _application_id(e['company_dir'], e['title_dir']) == app_id),
        None,
    )


def _read_interview_prep(folder: str) -> list:
    """
    Read interview_prep.json as a list of interview-round dicts: {'id', 'heading',
    'interviewer_name', 'interviewer_title', 'additional_info', 'questions', 'generated_at',
    'token_usage'}. Migrates the old single-round flat format (2026-07-24 through 2026-08-04:
    {'questions': [...], 'generated_at': ..., 'token_usage': ...}, no 'interviews' key) into the
    new shape in place — writes the migrated form straight back to disk so the id assigned here
    is stable across reads, not re-randomized every time this runs (a regenerate-by-id action
    needs the same id to still resolve on the next page load). Same ad hoc schema-evolution
    approach load_data() already uses for job_data.json, no separate migration script.
    """
    path = os.path.join(folder, 'interview_prep.json')
    with open(path, encoding='utf-8') as f:
        prep = json.load(f)
    if 'interviews' in prep:
        return prep['interviews']
    interviews = [{
        'id':                str(uuid.uuid4()),
        'heading':           'Recruiter Screen',
        'interviewer_name':  '',
        'interviewer_title': '',
        'additional_info':   '',
        'questions':         prep.get('questions', []),
        'generated_at':      prep.get('generated_at', ''),
        'token_usage':       prep.get('token_usage', {}),
    }]
    _write_interview_prep(folder, interviews)
    return interviews


def _write_interview_prep(folder: str, interviews: list) -> None:
    with open(os.path.join(folder, 'interview_prep.json'), 'w', encoding='utf-8') as f:
        json.dump({'interviews': interviews}, f, indent=2)


def _interview_heading(interviewer_name: str, interviewer_title: str, index: int) -> str:
    """First round (index 0, no interviewer info) is always 'Recruiter Screen' — see
    generate_interview_prep()'s recruiter-screen default, which this heading pairs with. Later
    rounds are labeled from whatever interviewer info was given; a round added with neither name
    nor title falls back to a numbered label rather than an empty heading."""
    if index == 0 and not interviewer_name and not interviewer_title:
        return 'Recruiter Screen'
    if interviewer_name and interviewer_title:
        return f'{interviewer_name} — {interviewer_title}'
    if interviewer_title:
        return interviewer_title
    if interviewer_name:
        return interviewer_name
    return f'Interview {index + 1}'


def _generate_and_append_interview(folder: str, matched_job: dict, data: dict, title: str, company_name: str,
                                    settings: dict, interviewer_name: str = '', interviewer_title: str = '',
                                    additional_info: str = '') -> dict:
    """
    Shared by the "start interviewing" and "add interview" routes: fetches the saved JD, calls
    generate_interview_prep(), builds a new round dict with a fresh id + heading, appends it to
    whatever's already in interview_prep.json (empty list for the first round), and writes it
    back. Regenerating an existing round (a different route, api_regenerate_interview_round)
    replaces one entry in place instead of appending — kept separate since that one re-uses a
    round's already-stored interviewer info rather than accepting new info.
    Raises ValueError (no JD) or whatever generate_interview_prep() raises (API/config failure) —
    callers translate those into the appropriate HTTP error.
    """
    jd_path = os.path.join(folder, 'Job Description.txt')
    if not os.path.isfile(jd_path):
        raise ValueError('No saved job description found for this application')
    with open(jd_path, encoding='utf-8') as f:
        job_description = f.read()

    # Clamped to a real Claude key (2026-09-22) — interview prep stayed Claude-only by deliberate
    # scope decision when Gemini was added to the generation/audit pickers, but shares this same
    # Settings -> Model default with those pickers; see _resolve_claude_model_key().
    model_key = _resolve_claude_model_key(settings.get('model', 'sonnet'))
    career_docs = load_career_docs(settings)
    questions, usage = generate_interview_prep(
        {'title': title, 'company_name': company_name},
        job_description, career_docs, model_key,
        interviewer_name=interviewer_name, interviewer_title=interviewer_title,
        additional_info=additional_info,
    )

    interviews = _read_interview_prep(folder) if os.path.isfile(os.path.join(folder, 'interview_prep.json')) else []
    new_round = {
        'id':                str(uuid.uuid4()),
        'heading':           _interview_heading(interviewer_name, interviewer_title, len(interviews)),
        'interviewer_name':  interviewer_name,
        'interviewer_title': interviewer_title,
        'additional_info':   additional_info,
        'questions':         questions,
        'generated_at':      datetime.now().strftime('%B %d, %Y %I:%M %p'),
        'token_usage':       usage,
    }
    interviews.append(new_round)
    _write_interview_prep(folder, interviews)

    # token_usage lives in interview_prep.json itself, not on a data['jobs'] record — this whole
    # feature is disk-native by design (see list_interviews()'s docstring: resolved via folder
    # walk, not data['jobs']), and matched_job may already be None if the underlying job record
    # was purged. Best-effort mirror onto matched_job too, for jobs where the record still
    # exists, so its lifetime token_usage total (see _add_token_usage()) stays complete.
    # _add_token_usage() also feeds the metrics database — only call it OR the direct database
    # write below, never both, to avoid double-counting this same usage dict.
    if matched_job is not None:
        _add_token_usage(matched_job, 'interview_prep', usage, model_key)
        save_data(data)
    else:
        _record_daily_token_usage('interview_prep', model_key, usage)
    _record_daily_metric('interview_rounds_generated', 1)

    return new_round


@app.route('/api/applications/<app_id>/interview', methods=['POST'])
def api_start_interview_prep(app_id):
    """
    Move an application to Interviewing: draft the initial recruiter-screen questions from its
    saved job description and write interview_prep.json into its folder — always the first round
    (see generate_interview_prep()'s recruiter-screen default, and _interview_heading()'s
    'Recruiter Screen' label for index 0 with no interviewer info).
    """
    data = load_data()
    entry = _resolve_application_folder(app_id)
    if not entry:
        return jsonify({'error': 'Application not found'}), 404
    folder = entry['folder']

    matched_job = _jobs_by_safe_name(data).get((entry['company_dir'], entry['title_dir']))
    title = matched_job.get('title') if matched_job else entry['title_dir']
    company_name = matched_job.get('company_name') if matched_job else entry['company_dir']
    settings = data.get('settings', {})

    try:
        _generate_and_append_interview(folder, matched_job, data, title, company_name, settings)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True, 'id': app_id})


@app.route('/api/applications/<app_id>/interview/add', methods=['POST'])
def api_add_interview_round(app_id):
    """
    Add an additional interview round beyond the initial recruiter screen (2026-08-04) — the
    "+ Add Interview" button's modal (interviewer name/title + free-text additional context)
    posts here. Appends a new round to interview_prep.json rather than replacing anything;
    unlike the recruiter-screen round, this one's prompt adapts to the given interviewer info
    instead of assuming a fixed stage (see generate_interview_prep()'s docstring).
    """
    body = request.get_json(silent=True) or {}
    interviewer_name = (body.get('interviewer_name') or '').strip()
    interviewer_title = (body.get('interviewer_title') or '').strip()
    additional_info = (body.get('additional_info') or '').strip()

    data = load_data()
    entry = _resolve_application_folder(app_id)
    if not entry:
        return jsonify({'error': 'Application not found'}), 404
    folder = entry['folder']

    matched_job = _jobs_by_safe_name(data).get((entry['company_dir'], entry['title_dir']))
    title = matched_job.get('title') if matched_job else entry['title_dir']
    company_name = matched_job.get('company_name') if matched_job else entry['company_dir']
    settings = data.get('settings', {})

    try:
        new_round = _generate_and_append_interview(
            folder, matched_job, data, title, company_name, settings,
            interviewer_name=interviewer_name, interviewer_title=interviewer_title,
            additional_info=additional_info,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True, 'interview': new_round})


@app.route('/api/applications/<app_id>/interview/<round_id>/regenerate', methods=['POST'])
def api_regenerate_interview_round(app_id, round_id):
    """
    Regenerate one existing interview round in place (2026-08-04) — re-uses that round's already-
    stored interviewer_name/interviewer_title/additional_info rather than accepting new ones, so
    "Regenerate Questions" on a specific round produces a fresh draft for the *same* round, not a
    blank recruiter-screen retry. Replaces that one entry's questions/generated_at/token_usage;
    every other round in the file is untouched.
    """
    data = load_data()
    entry = _resolve_application_folder(app_id)
    if not entry:
        return jsonify({'error': 'Application not found'}), 404
    folder = entry['folder']

    if not os.path.isfile(os.path.join(folder, 'interview_prep.json')):
        return jsonify({'error': 'No interview prep found for this application'}), 404
    interviews = _read_interview_prep(folder)
    idx = next((i for i, r in enumerate(interviews) if r['id'] == round_id), None)
    if idx is None:
        return jsonify({'error': 'Interview round not found'}), 404
    round_ = interviews[idx]

    matched_job = _jobs_by_safe_name(data).get((entry['company_dir'], entry['title_dir']))
    title = matched_job.get('title') if matched_job else entry['title_dir']
    company_name = matched_job.get('company_name') if matched_job else entry['company_dir']
    settings = data.get('settings', {})

    try:
        jd_path = os.path.join(folder, 'Job Description.txt')
        if not os.path.isfile(jd_path):
            raise ValueError('No saved job description found for this application')
        with open(jd_path, encoding='utf-8') as f:
            job_description = f.read()

        model_key = _resolve_claude_model_key(settings.get('model', 'sonnet'))
        career_docs = load_career_docs(settings)
        questions, usage = generate_interview_prep(
            {'title': title, 'company_name': company_name},
            job_description, career_docs, model_key,
            interviewer_name=round_.get('interviewer_name', ''),
            interviewer_title=round_.get('interviewer_title', ''),
            additional_info=round_.get('additional_info', ''),
        )
        round_['questions'] = questions
        round_['generated_at'] = datetime.now().strftime('%B %d, %Y %I:%M %p')
        round_['token_usage'] = usage
        interviews[idx] = round_
        _write_interview_prep(folder, interviews)

        # _add_token_usage() also feeds the metrics database — only call it OR the direct
        # database write below, never both, to avoid double-counting this usage dict. No
        # interview_rounds_generated increment here — this is a regenerate of an existing round,
        # not a new one.
        if matched_job is not None:
            _add_token_usage(matched_job, 'interview_prep', usage, model_key)
            save_data(data)
        else:
            _record_daily_token_usage('interview_prep', model_key, usage)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'ok': True, 'interview': round_})


def list_interviews() -> list:
    """
    Every application that has moved into interview prep — same disk-derived, permanent-record
    design as list_applications() (see that function's docstring), sharing the exact same
    _walk_application_folders() facts, just filtered to folders that DO have an
    interview_prep.json rather than don't (the two lists are structurally exclusive by
    construction — an entry can never appear in both). Includes the full original application
    content (cover_letter/sections/contact/feedback) alongside the parsed questions, so a job's
    cover letter and resume don't become unreachable just because it moved here.
    """
    data = load_data()
    jobs_by_safe_name = _jobs_by_safe_name(data)

    results = []
    for entry in _walk_application_folders():
        folder, date_str = entry['folder'], entry['date']
        prep_path = os.path.join(folder, 'interview_prep.json')
        if not os.path.isfile(prep_path):
            continue
        key = (entry['company_dir'], entry['title_dir'])
        content = _load_application_content(folder, date_str)
        interviews = _read_interview_prep(folder)
        matched_job = jobs_by_safe_name.get(key)
        # Most recently generated/regenerated round, across every round in this application —
        # not just the first one — so the list sorts by actual recent activity.
        latest_generated_at = max((r.get('generated_at', '') for r in interviews), default='')
        results.append({
            'id':                        _application_id(*key),
            'company_name':              matched_job.get('company_name') if matched_job else entry['company_dir'],
            'title':                     matched_job.get('title') if matched_job else entry['title_dir'],
            'folder':                    folder,
            'date':                      date_str,
            'generated_at':              content['generated_at'],
            'google_doc_url':            content['google_doc_url'],
            'cover_letter':              content['cover_letter'],
            'sections':                  content['sections'],
            'contact':                   content['contact'],
            'feedback':                  content['feedback'],
            'job_description':           content['job_description'],
            'interviews':                interviews,
            'interview_prep_generated_at': latest_generated_at,
            'job_id':                    matched_job.get('id') if matched_job else None,
            'url':                       matched_job.get('url', '') if matched_job else '',
            'fit_rating':                matched_job.get('fit_rating') if matched_job else None,
        })

    return sorted(results, key=lambda r: r['interview_prep_generated_at'], reverse=True)


@app.route('/api/interviews', methods=['GET'])
def api_list_interviews():
    return jsonify(list_interviews())


@app.route('/api/interviews/<app_id>', methods=['GET'])
def api_get_interview(app_id):
    match = next((i for i in list_interviews() if i['id'] == app_id), None)
    if not match:
        return jsonify({'error': 'Interview not found'}), 404
    return jsonify(match)


class GoogleAuthExpired(RuntimeError):
    """
    Raised specifically when Google's refresh call rejects the stored refresh token itself
    (invalid_grant) — as opposed to any other Drive/Docs API failure (network blip, missing
    OUTPUT_DRIVE_FOLDER_ID, etc.). A distinct type so callers that want to tell "auth needs
    re-running" apart from "something else went wrong" can do so without string-matching error
    text (see _career_doc_metadata(), which surfaces this specific case in the doc-health panel
    instead of silently treating it the same as "no Google Doc configured for this field").

    Confirmed real and recurring (2026-07-24 through 2026-07-27, ~4 days apart) — this app's
    OAuth consent screen is still in Google Cloud Console's "Testing" publish status, which caps
    refresh-token lifetime short regardless of how often the app is used, unlike a normally
    "In production" published app. Moving the consent screen to "In production" (Google Cloud
    Console → APIs & Services → OAuth consent screen → Publish App) is worth trying as an actual
    fix for the recurrence — not guaranteed, since this app's `drive` scope (full Drive access,
    not the narrower `drive.file`) is one Google treats as sensitive/restricted, so an
    unverified production app may still hit friction — but Testing status's flat short expiry
    definitely goes away either way. That console change is the user's to make; re-authorizing
    after it changes anything still needs `python job_checker.py --google-auth`.
    """
    pass


def _get_google_creds():
    """
    Load cached OAuth credentials (token.json), refreshing if expired. Raises a clear error
    directing to the one-time `--google-auth` setup if no token exists yet — same "tell the user
    exactly what to run" spirit as _adzuna_credentials()'s missing-config error. Raises
    GoogleAuthExpired specifically (see that class) if the refresh token itself has been
    rejected — a real, recurring case, not hypothetical.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google.auth.exceptions import RefreshError

    token_path = os.path.join(BASE_DIR, 'token.json')
    if not os.path.exists(token_path):
        raise RuntimeError(
            'Google Docs not authorized yet — run `python job_checker.py --google-auth` once '
            '(needs a real terminal with a browser available) to grant access.'
        )
    creds = Credentials.from_authorized_user_file(token_path, GOOGLE_OAUTH_SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise GoogleAuthExpired(
                'Google OAuth token has expired or been revoked — run '
                '`python job_checker.py --google-auth` again to re-authorize.'
            ) from e
        with open(token_path, 'w') as f:
            f.write(creds.to_json())
    return creds


def _drive_service():
    from googleapiclient.discovery import build
    return build('drive', 'v3', credentials=_get_google_creds())


def _docs_service():
    from googleapiclient.discovery import build
    return build('docs', 'v1', credentials=_get_google_creds())


def _find_or_create_drive_folder(service, name: str, parent_id: str) -> str:
    """
    Idempotent: look up a folder by name under parent_id before creating one, so regenerating
    on the same day doesn't create duplicate folders.

    Used to also defend against a real race with Google Drive Desktop independently syncing
    OUTPUT_FOLDER's local .txt writes up to Drive on its own — that's gone now (2026-07-21):
    local writes moved to a purely local cache path Drive Desktop never watches (see
    get_output_dir()), so this app is the only thing that ever creates folders here, and that
    race is structurally impossible rather than just unlikely. The retry below is kept for a
    narrower, still-real case — this app's own two overlapping calls (e.g. two generate clicks
    for the same job in quick succession) could in principle still both search-then-create
    before either commits — cheap insurance, costs nothing when the first search already
    succeeds, which is the common case now.
    """
    safe_name = name.replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and '{parent_id}' in parents "
        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    existing = service.files().list(q=query, fields='files(id)', spaces='drive').execute()
    files = existing.get('files', [])
    for delay in (3, 4, 5):
        if files:
            break
        time.sleep(delay)
        existing = service.files().list(q=query, fields='files(id)', spaces='drive').execute()
        files = existing.get('files', [])
    if files:
        return files[0]['id']
    created = service.files().create(
        body={'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]},
        fields='id',
    ).execute()
    return created['id']


def _get_or_create_job_drive_folder(job: dict, date_str: str = None) -> str:
    """Walk Company → Title → Date under OUTPUT_DRIVE_FOLDER_ID, mirroring get_output_dir()'s
    local-path structure (job_checker.py) so the Drive layout lines up with what's on disk.
    date_str defaults to today (every existing caller generates same-day) — retry_google_doc_creation()
    passes the application's actual folder date explicitly, since a retry can happen on a later day
    than the original generation and must land in that same original date-folder, not a fresh one."""
    root_id = os.environ.get('OUTPUT_DRIVE_FOLDER_ID', '')
    if not root_id:
        raise RuntimeError('OUTPUT_DRIVE_FOLDER_ID is not set in .env')
    service = _drive_service()
    company_id = _find_or_create_drive_folder(service, _safe_name(job.get('company_name', 'Company')), root_id)
    title_id = _find_or_create_drive_folder(service, _safe_name(job.get('title', 'Role')), company_id)
    date_id = _find_or_create_drive_folder(service, date_str or datetime.now().strftime('%Y-%m-%d'), title_id)
    return date_id


def _upload_file_to_drive(drive, folder_id: str, filename: str, local_path: str, mimetype: str) -> None:
    """
    Upload a local file into a Drive folder, trashing any existing file of the same name there
    first (idempotent regenerate — same reasoning _create_filled_google_doc() already uses for
    the resume template Doc: a fresh copy every time rather than trying to edit one in place).
    Part of this app being the sole writer of the Drive "Applications" tree (2026-07-21) — the
    local file at local_path lives in the non-Drive-synced cache (see get_output_dir()), this is
    what actually makes its contents show up in the user's Drive.
    """
    from googleapiclient.http import MediaFileUpload

    safe_name = filename.replace("'", "\\'")
    existing = drive.files().list(
        q=f"name = '{safe_name}' and '{folder_id}' in parents and trashed = false",
        fields='files(id)', spaces='drive',
    ).execute()
    for f in existing.get('files', []):
        drive.files().update(fileId=f['id'], body={'trashed': True}).execute()

    drive.files().create(
        body={'name': filename, 'parents': [folder_id]},
        media_body=MediaFileUpload(local_path, mimetype=mimetype),
        fields='id',
    ).execute()


def _strip_bullet_markers(text: str) -> str:
    """
    Strip leading markdown-style bullet markers ('- ', '* ', '• ') from each line. The
    placeholder paragraphs in the template are themselves bulleted-list paragraphs — Google
    Docs automatically gives every paragraph created by a newline inside a list item its own
    bullet, so leaving Claude's own literal '- ' markers in (its usual style for bullet
    sections) produces a real bullet plus a literal dash side by side on every line ("bullet
    pairs"). Single-paragraph fields (cover letter, phone, address) pass through unaffected
    unless they happen to start with one of these markers, which is harmless either way.
    """
    lines = [re.sub(r'^\s*[-*•]\s+', '', line) for line in text.splitlines()]
    return '\n'.join(lines)


def _create_filled_google_doc(template_doc_id: str, folder_id: str, replacements: dict) -> str:
    """
    Copy the template Doc into folder_id, fill its {{PLACEHOLDER}}s via the Docs API, return the
    new Doc's webViewLink. Trashes any previous Doc of the same name in that folder first — once
    a copy's placeholders are replaced with real text there's nothing left to re-fill in place,
    so a regenerate needs a fresh copy, not an edit of the old one. Trashing (not permanent
    delete) keeps this reversible.
    """
    drive = _drive_service()
    docs = _docs_service()

    doc_name = 'ksweeney_letter_resume'

    existing = drive.files().list(
        q=f"name = '{doc_name.replace(chr(39), chr(92) + chr(39))}' and '{folder_id}' in parents and trashed = false",
        fields='files(id)', spaces='drive',
    ).execute()
    for f in existing.get('files', []):
        drive.files().update(fileId=f['id'], body={'trashed': True}).execute()

    copy = drive.files().copy(fileId=template_doc_id, body={'name': doc_name, 'parents': [folder_id]}).execute()
    new_doc_id = copy['id']

    requests_body = [
        {'replaceAllText': {
            'containsText': {'text': '{{' + key + '}}', 'matchCase': True},
            'replaceText': _strip_bullet_markers(value) if value else '(not generated)',
        }}
        for key, value in replacements.items()
    ]
    docs.documents().batchUpdate(documentId=new_doc_id, body={'requests': requests_body}).execute()

    meta = drive.files().get(fileId=new_doc_id, fields='webViewLink').execute()
    return meta['webViewLink']


def _read_todo_items(doc_id: str) -> list:
    """
    Read the to-do doc's current items from its 2-column table (Votes | Task) — one data row
    per item, a header row (non-numeric first cell) skipped automatically. Falls back to
    scanning plain paragraphs for '<count> <text>' lines (no leading number defaults to count 1)
    if no table exists yet, so a doc the user seeded by hand with a plain list converts smoothly
    into table format on the first automated update.
    Returns [{'count': int, 'text': str}, ...] in document order.
    """
    docs = _docs_service()
    doc = docs.documents().get(documentId=doc_id).execute()

    def _cell_text(cell):
        parts = []
        for el in cell.get('content', []):
            for run in el.get('paragraph', {}).get('elements', []):
                parts.append(run.get('textRun', {}).get('content', ''))
        return ''.join(parts).strip()

    for el in doc.get('body', {}).get('content', []):
        table = el.get('table')
        if not table:
            continue
        items = []
        for row in table.get('tableRows', []):
            cells = row.get('tableCells', [])
            if len(cells) < 2:
                continue
            count_text = _cell_text(cells[0])
            task_text = _cell_text(cells[1])
            if not task_text:
                continue
            try:
                count = int(count_text)
            except ValueError:
                continue  # non-numeric first cell — this is the header row, skip it
            items.append({'count': count, 'text': task_text})
        return items

    # No table yet — fall back to plain paragraph lines so a hand-seeded plain list still works.
    items = []
    for el in doc.get('body', {}).get('content', []):
        para = el.get('paragraph')
        if not para:
            continue
        text = ''.join(r.get('textRun', {}).get('content', '') for r in para.get('elements', [])).strip()
        if not text:
            continue
        m = re.match(r'^(\d+)\s+(.+)$', text)
        if m:
            items.append({'count': int(m.group(1)), 'text': m.group(2).strip()})
        else:
            items.append({'count': 1, 'text': text})
    return items


def _write_todo_table(doc_id: str, items: list) -> None:
    """
    Replace the to-do doc's entire body with a fresh 2-column table (Votes | Task) — a header
    row plus one row per item, already sorted by the caller. Always a full clear-and-rebuild,
    never an incremental edit, same reasoning as _create_filled_google_doc()'s always-fresh-copy
    approach: reconciling a changed item list against existing table rows in place is far more
    fragile than just rebuilding the whole thing from the current, freshly-read state.
    """
    docs = _docs_service()
    doc = docs.documents().get(documentId=doc_id).execute()
    end_index = doc['body']['content'][-1]['endIndex']

    requests_body = []
    if end_index > 2:
        requests_body.append({'deleteContentRange': {'range': {'startIndex': 1, 'endIndex': end_index - 1}}})
    requests_body.append({'insertTable': {'rows': len(items) + 1, 'columns': 2, 'location': {'index': 1}}})
    docs.documents().batchUpdate(documentId=doc_id, body={'requests': requests_body}).execute()

    # Re-fetch to learn each cell's real insertion index — the Docs API only assigns these once
    # the table actually exists server-side; no reliable way to predict them analytically.
    doc = docs.documents().get(documentId=doc_id).execute()
    table = next(el['table'] for el in doc['body']['content'] if 'table' in el)

    rows_text = [('Votes', 'Task')] + [(str(item['count']), item['text']) for item in items]
    fill_requests = []
    # Last cell to first, so an earlier insertion in this same batch never shifts the index of
    # a not-yet-processed cell later in the request list.
    for row, (count_text, task_text) in reversed(list(zip(table['tableRows'], rows_text))):
        cells = row['tableCells']
        fill_requests.append({'insertText': {'location': {'index': cells[1]['content'][0]['startIndex']}, 'text': task_text}})
        fill_requests.append({'insertText': {'location': {'index': cells[0]['content'][0]['startIndex']}, 'text': count_text}})
    docs.documents().batchUpdate(documentId=doc_id, body={'requests': fill_requests}).execute()


def _extract_and_match_todo_items(feedback_text: str, existing_texts: list) -> tuple:
    """
    One Haiku call: given generation feedback text and the to-do list's current item texts,
    extract the distinct actionable gaps/improvements mentioned and, for each, either echo back
    the exact existing item text verbatim (if it's the same underlying issue) or propose new
    wording. Echoing verbatim on a match, rather than paraphrasing, is what lets the caller match
    by exact string equality afterward without wording drift silently creating near-duplicate
    entries over time.
    Returns ([{'text': str, 'is_new': bool}, ...], usage_dict), deduped by text — a tuple, not
    just the list, so the caller can record token cost via _add_token_usage() like every other
    Claude-calling function here. Raises on API/config failure — caller should treat that as
    "nothing extracted this time," not crash the rest of save_application_docs().
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key or api_key == 'your-key-here':
        raise RuntimeError('ANTHROPIC_API_KEY not set')
    if not HAS_ANTHROPIC:
        raise RuntimeError('anthropic package not installed')
    if not feedback_text.strip():
        return [], {}

    client = anthropic_sdk.Anthropic(api_key=api_key)
    existing_list = '\n'.join(f'- {t}' for t in existing_texts) or '(none yet)'
    system_prompt = (
        "You read feedback from a job application review and extract distinct, actionable gaps "
        "or improvements the candidate could act on to strengthen future applications (skip "
        "generic praise or strategy commentary that isn't an actionable gap).\n\n"
        "For each gap you find, check whether it's the same underlying issue as one of the "
        "EXISTING items below (even if worded differently) — if so, echo that existing item's "
        "text back EXACTLY, verbatim, unchanged. If it's genuinely new, write a short, clear "
        "task description for it (a short actionable phrase, not a full sentence of feedback).\n\n"
        f"EXISTING ITEMS:\n{existing_list}\n\n"
        "Respond with one line per distinct gap found, in exactly this format, no other text:\n"
        "MATCH: <exact existing item text>\n"
        "or\n"
        "NEW: <new item text>\n"
        "If no actionable gaps are mentioned at all, respond with exactly: NONE"
    )
    message = client.messages.create(
        model=MODELS['haiku'],
        max_tokens=500,
        system=system_prompt,
        messages=[{'role': 'user', 'content': feedback_text}],
    )
    text = message.content[0].text.strip()

    results = []
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line == 'NONE':
            continue
        if line.startswith('MATCH:'):
            item_text, is_new = line[len('MATCH:'):].strip(), False
        elif line.startswith('NEW:'):
            item_text, is_new = line[len('NEW:'):].strip(), True
        else:
            continue
        if not item_text or item_text in seen:
            continue
        seen.add(item_text)
        results.append({'text': item_text, 'is_new': is_new})
    return results, _usage_dict(message)


def _todo_doc_id(settings: dict) -> str:
    """Extract the To Do Google Doc's id from settings, or raise ValueError with a message
    that's safe to surface directly to the UI. Shared by _update_todo_doc() and the
    read/complete routes below, rather than each re-deriving it inline."""
    todo_url = (settings.get('gdoc_urls') or {}).get('todo', '')
    if not todo_url:
        raise ValueError('No To Do doc configured in Settings.')
    m = re.search(r'/document/d/([a-zA-Z0-9_-]+)', todo_url)
    if not m:
        raise ValueError(f'Could not extract doc ID from To Do URL: {todo_url}')
    return m.group(1)


def _update_todo_doc(job: dict, feedback_text: str, settings: dict) -> dict:
    """
    Update the to-do doc from this generation's feedback, gated by fit_rating (user-specified
    rule):
      - 4 or 5: gaps get added as new items, or vote up an existing match.
      - 3: never create new items from this feedback, but a match still gets a vote.
      - 1, 2, or unrated (None): skip entirely — no API calls at all, matching the existing
        instinct elsewhere in this app to never spend a call on something already excluded/poor
        fit (e.g. evaluate_candidates() never rates a not-remote job).
    Soft-fails like every other Google-Doc step in save_application_docs() — a missing doc,
    missing auth, or API error is caught and logged, never blocks anything else.

    Returns {'added': [str, ...], 'voted': [str, ...], 'reason': str, 'usage': dict} (2026-07-27,
    'usage' added 2026-08-03) — a summary persisted into this application's sections.json so the
    Application detail page can show exactly what this specific generation changed in the to-do
    doc, not just that the feature exists. 'reason' is only set when nothing changed, explaining
    why (gated by rating, no to-do doc configured, no gaps found, or an error) so the UI always
    has something informative to show rather than just an empty list with no context. 'usage' is
    set whenever the Haiku call actually ran, even if it found no matches — the API cost was
    still spent either way.
    """
    result = {'added': [], 'voted': [], 'reason': '', 'usage': {}}

    # Settings → General → To Do checkbox (2026-10-07, user-specified: disabled because the
    # resulting doc was never actually used for anything, but it was still spending a real Haiku
    # call on every rating-3-or-better generation). Defaults True so pre-existing installs that
    # haven't touched this setting keep their current behavior; only an explicit False turns it
    # off. Checked before the fit_rating gate below so disabling it skips the call entirely rather
    # than just suppressing the result.
    if not settings.get('todo_enabled', True):
        result['reason'] = 'Skipped — To Do tracking is disabled in Settings.'
        return result

    rating = job.get('fit_rating')
    if rating is None or rating <= 2:
        result['reason'] = 'Skipped — fit rating too low (needs 3 or higher).'
        return result

    try:
        doc_id = _todo_doc_id(settings)
    except ValueError as e:
        result['reason'] = str(e)
        return result

    try:
        existing_items = _read_todo_items(doc_id)
        existing_by_text = {item['text']: item for item in existing_items}
        matches, usage = _extract_and_match_todo_items(feedback_text, list(existing_by_text.keys()))
        result['usage'] = usage

        if not matches:
            result['reason'] = "No actionable gaps identified in this generation's feedback."
            return result

        for match in matches:
            if not match['is_new']:
                if match['text'] in existing_by_text:
                    existing_by_text[match['text']]['count'] += 1
                    result['voted'].append(match['text'])
            elif rating >= 4:
                existing_by_text[match['text']] = {'count': 1, 'text': match['text']}
                result['added'].append(match['text'])
            # rating == 3 and is_new: discard, per the gating rule above

        if not result['added'] and not result['voted']:
            result['reason'] = 'Gaps identified, but rating was 3 and none matched an existing item (new items need a rating of 4+).'
            return result

        items = sorted(existing_by_text.values(), key=lambda i: -i['count'])
        _write_todo_table(doc_id, items)
    except Exception as e:
        print(f"  [todo] Could not update to-do doc: {e}")
        result['reason'] = f'Error updating to-do doc: {e}'

    return result


@app.route('/api/todo', methods=['GET'])
def api_get_todo():
    """
    Surface the To Do doc's current items in the app itself (2026-07-28) — previously only
    viewable by opening the actual Google Doc. Read-only mirror of whatever's really in the doc
    (same _read_todo_items() the auto-update path uses), sorted by vote count descending, same
    as the doc's own table. 'error' is only set to explain an empty list (no doc configured, or
    a fetch failure) — still a 200, since "nothing to show, here's why" isn't really a server
    error from the frontend's perspective.
    """
    data = load_data()
    try:
        doc_id = _todo_doc_id(data.get('settings', {}))
        items = _read_todo_items(doc_id)
        items.sort(key=lambda i: -i['count'])
        return jsonify({'items': items})
    except Exception as e:
        return jsonify({'items': [], 'error': str(e)})


@app.route('/api/todo/complete', methods=['POST'])
def api_complete_todo_item():
    """
    Check an item off — removes it from the To Do doc entirely (user-specified: no 'done but
    kept' state, no undo). Matches by exact text, same convention _extract_and_match_todo_items()
    already relies on (its verbatim-echo-on-match requirement is what makes exact-string matching
    reliable here too — text is never silently reworded between when it's read and checked off).
    """
    body = request.get_json(silent=True) or {}
    text = body.get('text', '')
    if not text:
        return jsonify({'error': 'No item text provided'}), 400

    data = load_data()
    try:
        doc_id = _todo_doc_id(data.get('settings', {}))
        items = _read_todo_items(doc_id)
        remaining = [item for item in items if item['text'] != text]
        remaining.sort(key=lambda i: -i['count'])
        _write_todo_table(doc_id, remaining)
        return jsonify({'ok': True})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _parse_application_generated_at(generated_at: str):
    """Parse the two timestamp formats _load_application_content() can hand back —
    '%Y-%m-%d %H:%M' (sections.json-based entries) or '%B %d, %Y %I:%M %p' (legacy fallback) —
    returning an aware UTC datetime, or None if unparseable/blank. Shared by anything that needs
    to bucket applications/interviews by age, e.g. api_get_job_funnel()'s 30-day window."""
    if not generated_at:
        return None
    for fmt in ('%Y-%m-%d %H:%M', '%B %d, %Y %I:%M %p'):
        try:
            return datetime.strptime(generated_at, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@app.route('/api/job-funnel', methods=['GET'])
def api_get_job_funnel():
    """
    Job pipeline funnel for the Data area's Sankey diagram (2026-09-28, user-specified), capped
    to the last 30 days for ALL THREE stages (2026-09-28, real bug found the same day this
    shipped: 'screened' was already implicitly capped to ~30 days by the unrelated
    JOB_RETENTION_DAYS job-record purge, but 'applied'/'interviewed' were all-time totals sourced
    from disk, which never gets purged — over half of real tracked applications turned out to be
    older than 30 days, making the funnel compare a 30-day rolling count against an all-time one
    and inflating the apparent conversion rate). All three now use the same explicit 30-day
    window, computed here rather than relying on the job-purge side effect, so this stays correct
    even if JOB_RETENTION_DAYS ever changes.

    This is NOT lifetime data — it explicitly can't be, since data['jobs'] only ever holds the
    last 30 days once records age out, and the per-job detail needed for a true historical total
    doesn't exist anywhere else. See GET /api/lifetime-stats for the separate, actually-permanent
    counters (data['lifetime_stats'], see _record_lifetime_funnel_stats()) tracking Reviewed/
    Presented/Rejected-by-reason going forward from 2026-09-28.

    'screened' matches the frontend's own Screened-tab filter (renderJobs()) plus the 30-day
    window: evaluated_at set and within the last 30 days, remote_status remote/hybrid,
    software_status relevant, no exclusion_reason.

    'applied' and 'interviewed' are sourced from disk (list_applications() + list_interviews()),
    filtered to generated_at within the last 30 days. 'applied' is deliberately the union of both
    lists (an interviewed job WAS applied to first) — same union pattern renderJobs() already
    uses for its own appliedJobIds exclusion set.
    """
    data = load_data()
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    screened = 0
    for j in data.get('jobs', []):
        evaluated_at = j.get('evaluated_at')
        if not evaluated_at:
            continue
        try:
            evaluated_dt = datetime.fromisoformat(evaluated_at)
        except ValueError:
            continue
        if evaluated_dt < cutoff:
            continue
        if (j.get('remote_status') in ('remote', 'hybrid')
                and j.get('software_status') == 'relevant'
                and not j.get('exclusion_reason')):
            screened += 1

    def _within_30_days(entry):
        dt = _parse_application_generated_at(entry.get('generated_at', ''))
        return dt is not None and dt >= cutoff

    interviews_count = sum(1 for e in list_interviews() if _within_30_days(e))
    applied = sum(1 for e in list_applications() if _within_30_days(e)) + interviews_count
    return jsonify({'screened': screened, 'applied': applied, 'interviewed': interviews_count})


@app.route('/api/lifetime-stats', methods=['GET'])
def api_get_lifetime_stats():
    """
    Permanent, never-purged pipeline funnel counters (2026-09-28, user-specified: "how many jobs
    were reviewed, how many were rejected for each rejection reason, how many were presented to
    me, how many I applied to, and how many turned into interviews") — the actually-lifetime
    counterpart to GET /api/job-funnel's 30-day window. See _record_lifetime_funnel_stats() for
    exactly what feeds 'presented'/'rejected_by_reason' and why (six sweep functions only) and
    why this can't be backfilled before 2026-09-28.

    'applied'/'interviewed' need no separate lifetime counter of their own — list_applications()/
    list_interviews() already give true all-time totals (disk-based, never purged), same numbers
    GET /api/job-funnel uses before that endpoint's own 30-day filter is applied.

    (2026-10-07): 'presented'/'rejected_by_reason' now come from the metrics database
    (_metrics_db_summary()) plus data['lifetime_stats'] — the exact totals this endpoint
    accumulated into before the database existed, frozen in place as a pre-database baseline
    (see "Metrics database" section) rather than migrated, since it has no internal date
    breakdown to preserve. The all-time number is baseline + database sum; the database alone
    only covers 2026-10-07 onward.
    """
    data = load_data()
    baseline = data.get('lifetime_stats', {'presented': 0, 'rejected_by_reason': {}})
    db = _metrics_db_summary()

    presented = baseline.get('presented', 0) + db['presented']
    rejected_by_reason = dict(baseline.get('rejected_by_reason', {}))
    for reason, count in db['rejected_by_reason'].items():
        rejected_by_reason[reason] = rejected_by_reason.get(reason, 0) + count
    reviewed = presented + sum(rejected_by_reason.values())

    interviewed = len(list_interviews())
    applied = len(list_applications()) + interviewed

    return jsonify({
        'reviewed': reviewed,
        'presented': presented,
        'rejected_by_reason': rejected_by_reason,
        'applied': applied,
        'interviewed': interviewed,
    })


@app.route('/api/costs', methods=['GET'])
def api_get_costs():
    """
    Per-job Claude API token usage for the Costs area (2026-08-03) — surfaces job['token_usage']
    (see _add_token_usage()) for every job that's spent at least one real Claude call, sorted by
    total tokens descending (biggest spenders first). This per-job list is necessarily partial —
    data['jobs'] only ever holds the last JOB_RETENTION_DAYS, so it under-represents older
    activity. Browse it as "currently-live jobs' own cost," not as the lifetime total.

    'by_model' (2026-09-21, corrected 2026-10-07) is the actual lifetime total, now read from the
    metrics database (_metrics_db_summary()) instead of re-scanning data['jobs'] and every
    application's disk files on every request — a real audit found the old version silently
    missing most of what was actually spent, because job['token_usage'] alone is lossy
    (cleanup_old_jobs() purges it) and the per-application disk records it should have fallen
    back to were simply never read. A one-time migration (2026-10-07) backfilled the database
    with everything recoverable from both sources before this endpoint was simplified to just
    read it — see the "Metrics database" section near load_data()/save_data() for what that
    covered and what's genuinely unrecoverable (classification/fit_rating cost for jobs purged
    before any of this existed).
    """
    data = load_data()
    jobs = [j for j in data.get('jobs', []) if j.get('token_usage')]

    result = []
    for j in jobs:
        usage = j['token_usage']
        total_input = sum(p.get('input_tokens', 0) for p in usage.values())
        total_output = sum(p.get('output_tokens', 0) for p in usage.values())
        result.append({
            'id':                   j['id'],
            'title':                j.get('title', ''),
            'company_name':         j.get('company_name', ''),
            'token_usage':          usage,
            'total_input_tokens':   total_input,
            'total_output_tokens':  total_output,
        })
    result.sort(key=lambda r: r['total_input_tokens'] + r['total_output_tokens'], reverse=True)

    by_model = _metrics_db_summary()['token_by_model']
    for mk, bucket in by_model.items():
        bucket['estimated_cost'] = round(_estimate_cost(bucket['input_tokens'], bucket['output_tokens'], mk), 4)

    return jsonify({'jobs': result, 'by_model': by_model})


@app.route('/api/metrics/application-tokens', methods=['GET'])
def api_get_application_token_trend():
    """
    One data point per application actually generated, for a "tokens used preparing each
    application, over time" trend chart (2026-10-07) — deliberately per-application resolution,
    not the daily-aggregated totals the metrics database stores (daily_token_usage sums every
    application generated that day into one row, which can't be un-summed back into individual
    points). Read straight from each application's permanent sections.json via
    _walk_application_folders(), the same disk walk list_applications()/list_interviews() and
    the metrics-db backfill already use — there are only a few hundred of these, so a disk scan
    per request is cheap enough not to need its own database table.

    Sums 'generation' + 'todo_matching' + 'cover_letter_audit' — everything spent actually
    preparing that one application (classification/fit_rating are job-level costs incurred
    before an application exists at all; interview_prep is a separate, later lifecycle stage) —
    per the real token_usage snapshot save_application_docs() wrote at generation time, not a
    live job['token_usage'] lookup (which wouldn't exist at all once the job record is purged).

    Returns [{'date', 'company_name', 'title', 'input_tokens', 'output_tokens', 'total_tokens',
    'generation_model'}, ...] sorted oldest first. An application whose sections.json predates
    token tracking entirely (no 'token_usage' key at all) is skipped, not shown as a zero.
    """
    points = []
    for entry in _walk_application_folders():
        sj_path = os.path.join(entry['folder'], 'sections.json')
        if not os.path.isfile(sj_path):
            continue
        try:
            with open(sj_path, encoding='utf-8') as f:
                saved = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        tu = saved.get('token_usage')
        if not tu:
            continue
        input_tokens = sum(v.get('input_tokens', 0) for v in tu.values() if v)
        output_tokens = sum(v.get('output_tokens', 0) for v in tu.values() if v)
        if not input_tokens and not output_tokens:
            continue
        generated_at = saved.get('generated_at', '')
        points.append({
            'date':             generated_at,
            'company_name':     entry['company_dir'],
            'title':            entry['title_dir'],
            'input_tokens':     input_tokens,
            'output_tokens':    output_tokens,
            'total_tokens':     input_tokens + output_tokens,
            'generation_model': saved.get('generation_model') or 'sonnet',
        })
    points.sort(key=lambda p: p['date'])
    return jsonify(points)


@app.route('/api/audit-log', methods=['GET'])
def api_get_audit_log():
    """
    Per-generation cover-letter rule-audit history (2026-09-21, see _log_audit_result()) — a
    standing record of every real "Generate Documents" run's structured audit result, independent
    of data['jobs'] so it survives JOB_RETENTION_DAYS purges and keeps accumulating for as long as
    the user keeps generating (the whole point of "start keeping track of" — a snapshot on the job
    record alone would silently lose history every time a job aged out).

    Returns the raw log (newest first) plus a per-generation-model rollup (generations run,
    total rule checks, violations, violation rate, and — 2026-09-22 — average manual quality
    rating) so the frontend doesn't have to recompute that aggregation itself — directly answers
    "is one model better at sticking to the rules AND does it actually read better than others."
    avg_quality_rating is computed only over entries the user has actually rated (POST
    /api/applications/<id>/rate) — unrated entries (quality_rating: None, the default for every
    new generation) don't drag the average toward 0 or get counted in `rated_count`.
    """
    data = load_data()
    log = data.get('audit_log', [])

    by_model = {}
    for entry in log:
        mk = entry.get('generation_model') or 'unknown'
        bucket = by_model.setdefault(mk, {
            'generations': 0, 'total_rules': 0, 'violated_count': 0,
            'rated_count': 0, 'rating_sum': 0,
        })
        bucket['generations'] += 1
        bucket['total_rules'] += entry.get('total_rules', 0)
        bucket['violated_count'] += entry.get('violated_count', 0)
        rating = entry.get('quality_rating')
        if rating is not None:
            bucket['rated_count'] += 1
            bucket['rating_sum'] += rating
    for bucket in by_model.values():
        bucket['violation_rate'] = round(bucket['violated_count'] / bucket['total_rules'], 4) if bucket['total_rules'] else None
        bucket['avg_quality_rating'] = round(bucket['rating_sum'] / bucket['rated_count'], 2) if bucket['rated_count'] else None
        del bucket['rating_sum']  # internal accumulator only, not meaningful to the frontend

    return jsonify({'log': list(reversed(log)), 'by_model': by_model})


def save_application_docs(job: dict, docs: dict, job_description: str = '', settings: dict = None,
                           additional_instructions: str = '') -> dict:
    """
    Save generated docs as plain text in a local cache folder (see get_output_dir() — not
    Drive-synced, deliberately, as of 2026-07-21), then, if Drive is configured, upload copies
    of everything directly to Drive so the user can browse them there too.
    Folder structure: {output_root}/{Company}/{Job Title}/{YYYY-MM-DD}/
      - Cover Letter.txt
      - Resume Bullets.txt
      - Job Description.txt
      - Feedback.txt
      - sections.json

    additional_instructions (2026-08-07) — whatever per-application free text the user typed in
    the preview step, already baked into `docs` by generate_application_docs() by the time it
    gets here. Persisted into sections.json purely as a record of what was actually asked for on
    this generation (there's no setting or job-record field behind it — it only ever exists for
    this one call), same reasoning as todo_changes/token_usage already being saved there.
    """
    if settings is None:
        settings = {}

    output_dir = get_output_dir(job)
    os.makedirs(output_dir, exist_ok=True)

    results = {'folder': output_dir}

    _add_token_usage(job, 'generation', docs.get('usage'), docs.get('model_key', ''))
    _add_token_usage(job, 'cover_letter_audit', docs.get('audit_usage'), docs.get('audit_model_key', ''))
    # Daily activity metric (2026-10-07) — every real "Generate Documents" call, regenerates
    # included (this intentionally tracks generation activity, not distinct-applications count;
    # that count is still list_applications()'s own live, accurate, non-purged total).
    _record_daily_metric('applications_generated', 1)

    # ── 1. Cover letter ───────────────────────────────────────────────────────
    cover_letter = docs.get('cover_letter', '')
    if cover_letter:
        cl_path = os.path.join(output_dir, 'Cover Letter.txt')
        with open(cl_path, 'w', encoding='utf-8') as f:
            f.write(cover_letter)
        results['cover_letter'] = cl_path

    # ── 2. Resume bullets ─────────────────────────────────────────────────────
    sections = docs.get('sections', {})
    section_labels = [
        ('INTRODUCTION',       'Introduction'),
        ('RENTREDI_BULLETS',   'RentRedi — Growth Product Manager (2025–2026)'),
        ('METIS_BULLETS',      'Metis Health Technologies — Product Manager & Engineer (2025)'),
        ('SONOS_BULLETS',      'Sonos — Senior Technical Product Manager (2023–2024)'),
        ('SPOTIFY_PM_BULLETS', 'Spotify — Product Manager, Personalization (2020–2023)'),
        ('SPOTIFY_ENG_BULLETS','Spotify — Backend & Data Engineer (2012–2020)'),
    ]
    bullets_lines = []
    for key, label in section_labels:
        content = sections.get(key, '').strip()
        if content:
            bullets_lines.append(f'── {label} ──\n{content}')
    if bullets_lines:
        rb_path = os.path.join(output_dir, 'Resume Bullets.txt')
        with open(rb_path, 'w', encoding='utf-8') as f:
            f.write('\n\n'.join(bullets_lines))
        results['resume_bullets'] = rb_path

    # ── 2. Job description text file — written once per day, UNLESS what's already there is a
    # stale fetch-failure message (real bug, found 2026-08-05 via a House Rx regenerate that kept
    # producing the same broken output): "only written once per day" is right for preserving real
    # pasted/fetched content across a same-day regenerate, but a first attempt that failed and
    # left behind a "(Could not fetch job description: ...)" string shouldn't get treated as real
    # content worth protecting — it should be overwritten the moment real text becomes available,
    # same day or not, rather than poisoning every regenerate for the rest of the day.
    jd_path = os.path.join(output_dir, 'Job Description.txt')
    existing_jd_is_real = False
    if os.path.isfile(jd_path):
        with open(jd_path, encoding='utf-8') as f:
            existing_jd_is_real = is_real_job_description(f.read())
    if job_description and not existing_jd_is_real:
        with open(jd_path, 'w', encoding='utf-8') as f:
            f.write(f"{job.get('title', '')} at {job.get('company_name', '')}\n")
            f.write(f"{job.get('url', '')}\n\n")
            f.write(job_description)
    if os.path.exists(jd_path):
        results['job_description'] = jd_path

    # ── 3. AI feedback text file ──────────────────────────────────────────────
    # 'feedback' here is deliberately the ORIGINAL generation feedback only, unmerged with the
    # cover-letter-audit results below — this exact value is what step 5 hands to
    # _update_todo_doc()'s gap-mining call, and raw rule-violation text ("never use em dash")
    # would get misread there as an actionable to-do item. The audit report is merged in only for
    # what actually gets written to disk/Drive/sections.json (feedback_for_output), after the
    # to-do call has already run on the clean version.
    feedback = docs.get('feedback', '')
    audit_report = _format_audit_report(docs.get('cover_letter_audit', []))
    feedback_for_output = f'{feedback}\n\n## Cover Letter Audit\n{audit_report}' if audit_report else feedback
    if feedback_for_output:
        fb_path = os.path.join(output_dir, 'Feedback.txt')
        with open(fb_path, 'w', encoding='utf-8') as f:
            f.write(f"AI Feedback — {job.get('title', '')} at {job.get('company_name', '')}\n")
            f.write(f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
            f.write(feedback_for_output)
        results['feedback'] = fb_path

    # ── 4. Google Drive — this app is the sole writer of the Drive "Applications" tree
    # (2026-07-21; see get_output_dir()) — get/create the job's Drive folder once and upload
    # copies of the local files into it directly, instead of relying on Drive Desktop to sync
    # them up on its own (that's exactly what raced this app's own Doc-fill step and produced
    # three real duplicate folders). Soft-fails throughout: missing OUTPUT_DRIVE_FOLDER_ID,
    # missing auth, or any API error is caught and logged, never blocks the local files above,
    # which already work independently.
    folder_id = None
    if os.environ.get('OUTPUT_DRIVE_FOLDER_ID', ''):
        try:
            folder_id = _get_or_create_job_drive_folder(job)
            drive = _drive_service()
            for key in ('cover_letter', 'resume_bullets', 'job_description', 'feedback'):
                if key not in results:
                    continue
                local_path = results[key]
                filename = os.path.basename(local_path)
                mimetype = 'text/plain'
                _upload_file_to_drive(drive, folder_id, filename, local_path, mimetype)
        except Exception as e:
            print(f"  [gdrive] Could not upload files to Drive: {e}")
            folder_id = None

    # ── 4b. Live Google Doc — copy the user's template and fill it in place ───
    # 'resume' doubles as the live-fill template — the Settings UI field is labeled "Resume
    # Template" for exactly this reason (2026-07-17: a separate 'template' field was removed
    # since it was always meant to be the same doc the user already had saved as their resume).
    google_doc_url = ''
    template_url = (settings.get('gdoc_urls') or {}).get('resume', '')
    if template_url and folder_id:
        try:
            m = re.search(r'/document/d/([a-zA-Z0-9_-]+)', template_url)
            if not m:
                raise ValueError(f'Could not extract doc ID from template URL: {template_url}')
            template_doc_id = m.group(1)
            replacements = {**docs.get('sections', {}), **docs.get('contact', {}),
                             'COVER_LETTER': docs.get('cover_letter', ''),
                             'TODAY': datetime.now().strftime('%B %d, %Y'),
                             'COMPANY': job.get('company_name', '')}
            google_doc_url = _create_filled_google_doc(template_doc_id, folder_id, replacements)
            results['google_doc_url'] = google_doc_url
        except Exception as e:
            print(f"  [gdoc] Could not create filled Google Doc: {e}")

    # ── 5. To Do doc — vote-tracked gaps from this generation's feedback ──────
    todo_changes = _update_todo_doc(job, docs.get('feedback', ''), settings)
    _add_token_usage(job, 'todo_matching', todo_changes.get('usage'), 'haiku')

    # ── 6. sections.json — for reloading in the UI without re-generating ──────
    # token_usage here is a snapshot of THIS generation's own cost (generation + cover_letter_audit
    # + todo_matching), same granularity as todo_changes above — durable even if the underlying
    # data['jobs'] record is later purged (JOB_RETENTION_DAYS). job['token_usage'] (mutated below)
    # is the separate, cumulative lifetime total across every part and every call, for as long as
    # the job record exists — see _add_token_usage(). generation_model/audit_model (2026-09-21)
    # are recorded alongside so a later look at this specific application can tell which model
    # actually wrote it, independent of whatever the Settings default is by the time you look.
    # Rule-violation counts, structured (2026-09-22) — cover_letter_audit already gets formatted
    # into feedback_for_output as readable text above, but that's not machine-readable; these two
    # ints are what the Application detail page's "violations" display and POST
    # /api/applications/<id>/rate's audit_log lookup actually read. Same numbers
    # _log_audit_result() computes for the audit_log entry, just also kept locally with this
    # specific application's own record.
    audit_rule_results = docs.get('cover_letter_audit') or []
    quality_rating = None  # unrated until the user submits one via POST /api/applications/<id>/rate

    import json as _json
    sections_data = {
        'cover_letter':    docs.get('cover_letter', ''),
        'sections':        docs.get('sections', {}),
        'contact':         docs.get('contact', {}),
        'feedback':        feedback_for_output,
        'generated_at':    datetime.now().strftime('%Y-%m-%d %H:%M'),
        'google_doc_url':  google_doc_url,
        'todo_changes':    todo_changes,
        'generation_model': docs.get('model_key', ''),
        'audit_model':      docs.get('audit_model_key', ''),
        'audit_total_rules':    len(audit_rule_results),
        'audit_violated_count': sum(1 for r in audit_rule_results if r.get('violated')),
        'quality_rating':  quality_rating,
        'token_usage':     {
            'generation':         docs.get('usage', {}),
            'cover_letter_audit': docs.get('audit_usage', {}),
            'todo_matching':      todo_changes.get('usage', {}),
        },
        'additional_instructions': additional_instructions.strip(),
    }
    sj_path = os.path.join(output_dir, 'sections.json')
    with open(sj_path, 'w', encoding='utf-8') as f:
        _json.dump(sections_data, f, indent=2)
    results['sections_json'] = sj_path

    if folder_id:
        try:
            drive = _drive_service()
            _upload_file_to_drive(drive, folder_id, 'sections.json', sj_path, 'application/json')
        except Exception as e:
            print(f"  [gdrive] Could not upload sections.json to Drive: {e}")

    return results


def retry_google_doc_creation(app_id: str) -> str:
    """
    Retry ONLY the filled-Google-Doc creation step for an already-generated application, reusing
    the cover letter/resume bullets/contact already saved in its sections.json — no new Claude
    call (2026-09-03, user-specified: a real "Could not create filled Google Doc" failure — a
    transient Drive API rate limit, not an OAuth/config problem — left an application with real,
    good local content but no Doc, and the user shouldn't have to pay for/risk a full regenerate
    just to retry the one Drive API call that failed).

    Mirrors save_application_docs()'s own step 4b rather than calling it directly, since that step
    only ever runs inline during a fresh generation against an in-memory `docs`/`job`, not one
    reloaded from disk after the fact. Does NOT redo the Drive folder / file uploads (step 4) —
    those already succeeded (confirmed via the real failure this was built for: no matching
    "[gdrive]" error alongside the Doc-creation one), and _get_or_create_job_drive_folder() is
    idempotent (find-before-create) regardless, so even a partial-failure case reusing the same
    folder is safe.

    Raises ValueError if the application can't be found or no template/Drive folder is configured.
    Returns the new google_doc_url on success, and persists it into sections.json so future loads
    of this application show the link without needing to retry again.
    """
    entry = _resolve_application_folder(app_id)
    if not entry:
        raise ValueError('Application not found')
    folder, date_str = entry['folder'], entry['date']

    data = load_data()
    settings = data.get('settings', {})
    template_url = (settings.get('gdoc_urls') or {}).get('resume', '')
    if not template_url:
        raise ValueError('No Resume Template Google Doc configured in Settings')
    if not os.environ.get('OUTPUT_DRIVE_FOLDER_ID', ''):
        raise ValueError('OUTPUT_DRIVE_FOLDER_ID is not set in .env')

    content = _load_application_content(folder, date_str)
    matched_job = _jobs_by_safe_name(data).get((entry['company_dir'], entry['title_dir']))
    company_name = matched_job.get('company_name') if matched_job else entry['company_dir']
    title = matched_job.get('title') if matched_job else entry['title_dir']

    # Explicit date_str (the folder's real date, not necessarily today) — a retry can happen on a
    # later day than the original generation and must land in that same original date-folder.
    folder_id = _get_or_create_job_drive_folder({'company_name': company_name, 'title': title}, date_str)

    m = re.search(r'/document/d/([a-zA-Z0-9_-]+)', template_url)
    if not m:
        raise ValueError(f'Could not extract doc ID from template URL: {template_url}')
    template_doc_id = m.group(1)
    replacements = {
        **content.get('sections', {}), **content.get('contact', {}),
        'COVER_LETTER': content.get('cover_letter', ''),
        'TODAY': datetime.strptime(date_str, '%Y-%m-%d').strftime('%B %d, %Y'),
        'COMPANY': company_name,
    }
    google_doc_url = _create_filled_google_doc(template_doc_id, folder_id, replacements)

    sj_path = os.path.join(folder, 'sections.json')
    with open(sj_path, encoding='utf-8') as f:
        sections_data = json.load(f)
    sections_data['google_doc_url'] = google_doc_url
    with open(sj_path, 'w', encoding='utf-8') as f:
        json.dump(sections_data, f, indent=2)

    return google_doc_url


@app.route('/api/applications/<app_id>/retry-google-doc', methods=['POST'])
def api_retry_google_doc(app_id):
    try:
        url = retry_google_doc_creation(app_id)
        return jsonify({'google_doc_url': url})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/applications/<app_id>/rate', methods=['POST'])
def api_rate_application(app_id):
    """
    Manual 1-5 quality rating for a generated application (2026-09-22, user-specified: alongside
    the audit's rule-violation count, as a second, human-judgment signal for comparing which
    model produces the best cover letters — a raw violation count only tells you how many rules
    were broken, not whether the result actually reads well). Rated separately from generation,
    since the user needs to actually read the documents first — this is why quality_rating starts
    as None in save_application_docs() and only gets set here, later, as its own action.

    Written to two places, same "local per-application record + cross-application aggregate log"
    split already used for generation_model/audit_model and the audit_log respectively:
      1. This application's own sections.json — what the Application detail page reads back on
         reload, same read-modify-write pattern retry_google_doc_creation() already uses.
      2. The matching data['audit_log'] entry (same application_id) — this is what makes the
         rating actually comparable ACROSS models/jobs in the Costs area's by-model rollup
         (GET /api/audit-log), not just visible on this one application's own page. If this
         application was regenerated more than once, updates the MOST RECENT entry for this
         application_id — matching "the rating applies to whatever's currently displayed," same
         as every other latest-generation-wins convention in this app (list_applications(),
         _find_latest_dated_folder(), etc.). A pre-audit-log application (generated before
         2026-09-21, or one whose audit call failed so nothing was ever logged) simply has no
         matching entry — the sections.json write still succeeds, only the aggregate is short one
         data point, same "gaps are fine" tolerance as everywhere else in this app.

    Body: {"rating": 1-5} to set, or {"rating": null} to clear a mis-click.
    """
    body = request.get_json(silent=True) or {}
    rating = body.get('rating')
    if rating is not None and rating not in (1, 2, 3, 4, 5):
        return jsonify({'error': 'rating must be an integer 1-5, or null to clear'}), 400

    entry = _resolve_application_folder(app_id)
    if not entry:
        return jsonify({'error': 'Application not found'}), 404
    folder = entry['folder']

    sj_path = os.path.join(folder, 'sections.json')
    if not os.path.isfile(sj_path):
        return jsonify({'error': 'No sections.json for this application — nothing to rate'}), 400
    with open(sj_path, encoding='utf-8') as f:
        sections_data = json.load(f)
    sections_data['quality_rating'] = rating
    with open(sj_path, 'w', encoding='utf-8') as f:
        json.dump(sections_data, f, indent=2)

    data = load_data()
    matching = [e for e in data.get('audit_log', []) if e.get('application_id') == app_id]
    if matching:
        matching[-1]['quality_rating'] = rating
        save_data(data)

    return jsonify({'ok': True, 'quality_rating': rating})


@app.route('/api/jobs/<job_id>/documents', methods=['GET'])
def api_existing_documents(job_id):
    """Return previously generated documents for a job, if they exist."""
    data = load_data()
    job = next((j for j in data['jobs'] if j['id'] == job_id), None)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    company = _safe_name(job.get('company_name', 'Company'))
    title   = _safe_name(job.get('title', 'Role'))

    # Check the current local cache path first, then fall back to the old Drive-synced
    # OUTPUT_FOLDER path (2026-07-21's local-path change moved where *new* generations get
    # cached — see get_output_dir() — but anything generated before that still lives at the old
    # location, and reload needs to keep finding it, not just newly-generated jobs).
    candidate_roots = [os.path.join(BASE_DIR, 'applications')]
    legacy_root = os.environ.get('OUTPUT_FOLDER', '')
    if legacy_root:
        candidate_roots.append(legacy_root)

    folder = date_str = None
    for root in candidate_roots:
        folder, date_str = _find_latest_dated_folder(os.path.join(root, company, title))
        if folder:
            break

    if not folder:
        return jsonify({'exists': False})

    content = _load_application_content(folder, date_str)
    return jsonify({
        'exists': True,
        'folder': folder,
        'date':   date_str,
        **content,
    })


CAREER_DOC_FIELDS = {'bio': 'Bio', 'instructions': 'Agent Instructions', 'resume': 'Resume Template'}

def _career_doc_metadata(settings: dict) -> dict:
    """
    Per-field display metadata for the doc-health panel on the Job detail page's preview step
    (2026-07-23) — the real Google Doc title and last-modified time for each of Bio/Agent
    Instructions/Resume Template, regardless of whether that field's *content* actually reaches
    Claude (instructions.md becomes the system prompt rather than career context; resume.md is
    excluded from the prompt entirely as the output template, not source material — see
    _build_career_context()). This is purely "which real doc is behind this field, and when did
    you last touch it" — unrelated to what gets sent to the model.

    Needs Drive API metadata (`name`, `modifiedTime`), unlike fetch_gdoc_text()'s plain-text
    export (deliberately public, no auth needed). Falls back to the local career_docs/<field>.md
    file's name/mtime when no Google Doc is configured for that field, or when the Drive fetch
    fails — 'resume' in particular is deliberately never Google-Doc-fetched for its *content*
    (load_career_docs() skips it — see that function's docstring) but the Doc may still be
    linked in settings purely for the live-template-fill feature, so its title is still worth
    showing here.

    Distinguishes *why* the fallback happened via the returned `fetch_failed` flag (2026-07-27,
    real incident: this app's OAuth token expired — see GoogleAuthExpired — and every field
    silently fell back to the same stale local-file date with no visual difference from the
    ordinary "no Google Doc configured for this field" case, which is exactly what made the
    problem hard to notice). `fetch_failed: True` means a Google Doc *is* configured here but its
    real title/date couldn't be confirmed (most likely GoogleAuthExpired in practice, but any
    Drive API failure counts) — the local-file date shown is a fallback of unknown accuracy, not
    a confirmed fact — vs. `False` meaning either it fetched fine or there was genuinely nothing
    to fetch.
    """
    gdoc_urls = (settings or {}).get('gdoc_urls', {})
    drive = None
    result = {}
    for field in CAREER_DOC_FIELDS:
        title = ''
        last_updated = ''
        fetch_failed = False
        url = (gdoc_urls.get(field) or '').strip()
        m = re.search(r'/document/d/([a-zA-Z0-9_-]+)', url) if url else None
        if m:
            try:
                if drive is None:
                    drive = _drive_service()
                meta = drive.files().get(fileId=m.group(1), fields='name,modifiedTime').execute()
                title = meta.get('name', '')
                last_updated = meta.get('modifiedTime', '')
            except GoogleAuthExpired as e:
                print(f'  [preview] Google auth expired fetching metadata for {field}: {e}')
                fetch_failed = True
            except Exception as e:
                print(f'  [preview] Could not get Drive metadata for {field}: {e}')
                fetch_failed = True
        if not title:
            local_path = os.path.join(CAREER_DOCS_DIR, f'{field}.md')
            if os.path.isfile(local_path):
                title = f'{field}.md (local file)'
                if not last_updated:
                    last_updated = datetime.fromtimestamp(os.path.getmtime(local_path), tz=timezone.utc).isoformat()
        result[field] = {'title': title, 'last_updated': last_updated, 'fetch_failed': fetch_failed}
    return result


@app.route('/api/jobs/<job_id>/preview', methods=['GET'])
def api_preview_application(job_id):
    """Fetch job description and career doc metadata for the preview step."""
    data = load_data()
    job = next((j for j in data['jobs'] if j['id'] == job_id), None)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    try:
        job_description = _resolve_job_description_for_generation(job)

        doc_meta = _career_doc_metadata(data.get('settings', {}))

        doc_info = {}
        for field, label in CAREER_DOC_FIELDS.items():
            meta = doc_meta.get(field, {})
            doc_info[field] = {
                'label':        label,
                'title':        meta.get('title', ''),
                'last_updated': meta.get('last_updated', ''),
                'fetch_failed': meta.get('fetch_failed', False),
            }

        linkedin_matches = find_connections_at_company(
            job.get('company_name', ''), data.get('linkedin_connections', []))

        return jsonify({
            'job_description':     job_description,
            'docs':                doc_info,
            'linkedin_connections': linkedin_matches,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _log_audit_result(data: dict, job: dict, result: dict) -> None:
    """
    Append one row to data['audit_log'] for this generation's cover-letter rule audit
    (2026-09-21, user-specified: "start keeping track of the writing rule violations to see if
    one model is better at sticking to the rules than others"). A dedicated top-level log,
    separate from job['token_usage'] — that answers "how much did this job cost," this answers
    "which model actually held to the writing rules," which only means anything aggregated
    ACROSS jobs/models over time. Keeping it only on the job record would mean losing the history
    every time JOB_RETENTION_DAYS purges a job, exactly the opposite of "start keeping track of."

    Skips logging when `cover_letter_audit` is empty — that means the audit pass itself never
    completed (the exception path inside generate_application_docs()'s audit loop), not "zero
    rules exist to check" — there's nothing real to record in that case.

    quality_rating starts None — the human judgment half of "which model is best" (2026-09-22),
    set later via POST /api/applications/<id>/rate once the user has actually read the
    documents; see that route's docstring for how it finds and updates this same entry.
    """
    rule_results = result.get('cover_letter_audit') or []
    if not rule_results:
        return
    data.setdefault('audit_log', []).append({
        'timestamp':        datetime.now(timezone.utc).isoformat(),
        'application_id':   _application_id(job.get('company_name', ''), job.get('title', '')),
        'company_name':     job.get('company_name', ''),
        'title':             job.get('title', ''),
        'generation_model': result.get('model_key', ''),
        'audit_model':      result.get('audit_model_key', ''),
        'total_rules':      len(rule_results),
        'violated_count':   sum(1 for r in rule_results if r.get('violated')),
        'violated_rules':   [r['rule'] for r in rule_results if r.get('violated')],
        'quality_rating':   None,
    })


@app.route('/api/jobs/<job_id>/generate', methods=['POST'])
def api_generate_application(job_id):
    """Generate cover letter + resume for a job using the Anthropic API."""
    data = load_data()
    job = next((j for j in data['jobs'] if j['id'] == job_id), None)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    settings = data.get('settings', {})
    body = request.get_json(silent=True) or {}
    model_key = _resolve_generation_model_key(body, settings)

    try:
        # 1. Use pre-fetched JD from preview if given, else resolve the same way preview does
        # (existing file from today > stored job['description'] > fresh fetch as a last resort —
        # see _resolve_job_description_for_generation()'s docstring for the real bug this fixed).
        job_description = body.get('job_description') or _resolve_job_description_for_generation(job)

        # 2. Load career docs (local files + any Google Doc URLs from settings)
        career_docs = load_career_docs(settings)
        if not any(v.strip() for v in career_docs.values()):
            return jsonify({'error': 'No career docs found. Add content to career_docs/ or add Google Doc URLs in settings.'}), 400

        # 3. Generate via Anthropic API
        additional_instructions = body.get('additional_instructions', '')
        audit_model_key = settings.get('audit_model', 'opus')
        result = generate_application_docs(job, job_description, career_docs, model_key,
                                            additional_instructions=additional_instructions,
                                            audit_model_key=audit_model_key)

        # 4. Save files (mutates job['token_usage'] in place — see save_application_docs() —
        # so this needs its own save; nothing else in this route previously persisted job state)
        file_paths = save_application_docs(job, result, job_description, settings,
                                            additional_instructions=additional_instructions)

        # Defaults to applied (2026-10-13, user-specified: "I apply to most after generating
        # documents") — generate-adhoc already defaulted this to True; a tracked job's record
        # is created earlier during ingestion with is_applied: False (correct at that point,
        # nothing's been generated yet), and nothing ever flipped it afterward. The rare case of
        # generating without actually applying is now the one requiring a manual action
        # (unchecking it on the Application detail page), not the common case.
        job['is_applied'] = True

        _log_audit_result(data, job, result)
        save_data(data)

        return jsonify({
            'ok':          True,
            'cover_letter': result['cover_letter'],
            'resume':       result['resume'],
            'sections':     result.get('sections', {}),
            'contact':      result.get('contact', {}),
            'feedback':     result.get('feedback', ''),
            'files':        file_paths,
            'usage':        result.get('usage', {}),
            # So the frontend can navigate straight to #/applications/<id> after a successful
            # generate without an extra round trip — see list_applications().
            'application_id': _application_id(job.get('company_name', ''), job.get('title', '')),
        })

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/generate-adhoc', methods=['POST'])
def api_generate_adhoc():
    """Generate application docs for an ad-hoc job (not in job list)."""
    data     = load_data()
    settings = data.get('settings', {})
    body = request.get_json(silent=True) or {}
    model_key = _resolve_generation_model_key(body, settings)

    company         = body.get('company', 'Company') or 'Company'
    title           = body.get('title', 'Role') or 'Role'
    job_description = body.get('job_description', '').strip()
    url             = body.get('url', '')

    if not job_description:
        return jsonify({'error': 'Job description is required.'}), 400

    try:
        career_docs = load_career_docs(settings)
        if not any(v.strip() for v in career_docs.values()):
            return jsonify({'error': 'No career docs found.'}), 400

        job_id = make_job_id('adhoc', title, url or title)
        now = datetime.now(timezone.utc)
        synthetic_job = {
            'id':           job_id,
            'company_id':   'adhoc',
            'company_name': company,
            'title':        title,
            'url':          url,
            'location':     'Remote',
            'is_remote':    True,
            'is_new':       False,
            'is_applied':   True,
            'source':       'adhoc',
            'first_seen':   now.isoformat(),
        }

        # Classify + rate exactly like every other job — an ad-hoc job shouldn't be treated
        # differently just because of how it entered the system. The JD is already in hand (the
        # user pasted it), so no fetch needed. Generation still proceeds regardless of the
        # verdict below — the user explicitly asked to generate for this specific job;
        # classification here is about giving the resulting record accurate metadata, not
        # gating the action they already took.
        try:
            candidate_location, _ = _parse_accepted_locations(settings)
            verdict = classify_job(job_description, title, company, candidate_location=candidate_location)
            _apply_classification(synthetic_job, job_description, verdict, settings, now)
        except Exception as e:
            print(f"    [adhoc] classify failed for {title} @ {company}: {e}")

        # Save to job list if not already there
        existing_ids = {j['id'] for j in data.get('jobs', [])}
        if job_id not in existing_ids:
            data.setdefault('jobs', []).insert(0, synthetic_job)
            save_data(data)

        additional_instructions = body.get('additional_instructions', '')
        audit_model_key = settings.get('audit_model', 'opus')
        result = generate_application_docs(synthetic_job, job_description, career_docs, model_key,
                                            additional_instructions=additional_instructions,
                                            audit_model_key=audit_model_key)
        file_paths = save_application_docs(synthetic_job, result, job_description, settings,
                                            additional_instructions=additional_instructions)
        # save_application_docs() mutates synthetic_job['token_usage'] in place. Only reaches
        # disk if synthetic_job is the same object already sitting in data['jobs'] (the normal
        # first-generation case, inserted above) — a regenerate of an already-tracked adhoc job
        # rebuilds synthetic_job as a fresh, disconnected dict sharing the same id, so its
        # classification/token_usage updates were already silently discarded before this change
        # too (a pre-existing gap, not introduced here); save_data() here is a no-op harmless
        # extra write in that case, not a regression. _log_audit_result() has no such
        # dependency — it reads straight off `result`, not the job object — so it's unaffected.
        _log_audit_result(data, synthetic_job, result)
        save_data(data)

        return jsonify({
            'ok':          True,
            'job':         synthetic_job,
            'cover_letter': result['cover_letter'],
            'resume':       result['resume'],
            'sections':     result.get('sections', {}),
            'contact':      result.get('contact', {}),
            'feedback':     result.get('feedback', ''),
            'files':        file_paths,
            'usage':        result.get('usage', {}),
            'application_id': _application_id(company, title),
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/career-docs', methods=['GET'])
def api_career_docs_status():
    """Return which career docs exist and their word counts."""
    docs = load_career_docs()
    result = {}
    for fname, content in docs.items():
        words = len(content.split())
        # Check if it still has placeholder text
        has_content = '[ADD' not in content and words > 30
        result[fname] = {'words': words, 'has_content': has_content}
    return jsonify(result)

@app.route('/api/schedule', methods=['GET'])
def api_get_schedule():
    """Return info about the daily auto-check schedule."""
    if not HAS_SCHEDULER or _scheduler is None:
        return jsonify({'enabled': False, 'reason': 'apscheduler not installed'})
    job = _scheduler.get_job('daily_check')
    if job is None:
        return jsonify({'enabled': False})
    next_run = job.next_run_time
    return jsonify({
        'enabled': True,
        'hour': DAILY_CHECK_HOUR,
        'next_run': next_run.isoformat() if next_run else None,
    })


# ─────────────────────────────────────────────
# Job search (Adzuna)
# ─────────────────────────────────────────────

def _adzuna_credentials() -> tuple:
    """Return (app_id, app_key) or raise ValueError if not configured."""
    app_id  = os.environ.get('ADZUNA_APP_ID', '').strip()
    app_key = os.environ.get('ADZUNA_APP_KEY', '').strip()
    if not app_id or app_id == 'your-app-id':
        raise ValueError(
            'Adzuna credentials not set. Add ADZUNA_APP_ID and ADZUNA_APP_KEY to your '
            '.env file. Sign up free at https://developer.adzuna.com'
        )
    return app_id, app_key


def _adzuna_request(query: str, page: int, max_days_old: int, results_per_page: int = 20,
                     where: str = '') -> dict:
    """
    Call the Adzuna search API and return the raw parsed JSON response.
    Adzuna has no "remote or hybrid-in-this-state" combined filter, so 'where' and the default
    'remote_only' behavior are mutually exclusive: when 'where' is set (a state abbreviation or
    name — verified 2026-07-22 against the real API, both work identically), the request is
    scoped to that location instead of remote_only='1' — used for the accepted-hybrid-state
    sweep queries (see sweep_adzuna_jobs()), since a hybrid role wouldn't pass remote_only.
    """
    app_id, app_key = _adzuna_credentials()
    params = {
        'app_id':           app_id,
        'app_key':          app_key,
        'results_per_page': results_per_page,
        'what_phrase':      query,
        'salary_min':       100000,
        'max_days_old':     max_days_old,
        'sort_by':          'date',
        'full_time':        '1',
    }
    if where:
        params['where'] = where
    else:
        params['remote_only'] = '1'
    resp = requests.get(
        f'https://api.adzuna.com/v1/api/jobs/us/search/{page}',
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _resolve_redirect_url(url: str) -> str:
    """Follow an Adzuna redirect URL to the real ATS posting URL. Falls back to the input on failure."""
    if not url:
        return url
    try:
        resp = requests.get(url, allow_redirects=True, timeout=10,
                            headers={'User-Agent': 'Mozilla/5.0'})
        return resp.url
    except Exception:
        return url


def _resolve_adzuna_real_url(adzuna_id: str) -> tuple:
    """
    Follow Adzuna's own click-through chain to the real employer/ATS posting URL, instead of
    settling for Adzuna's own landing page. Adzuna's 'details' page for a job embeds an
    "Apply for this job" link pointing to another Adzuna tracking URL
    (adzuna.com/land/ad/...); requesting that URL bare (no cookies, no Referer) gets a 403,
    but visiting the details page first (to pick up session cookies) and setting a Referer
    header succeeds, landing on a meta-refresh page pointing to the real destination.

    Returns (real_url_or_None, adzuna_details_text). The details-page text is returned too
    since Adzuna's own page usually has a decent JD snippet — a useful fallback if the real
    destination turns out to be unscrapeable (e.g. a JS-rendered ATS). Never raises — returns
    (None, '') on any failure so callers can fall back to the old resolution behavior.
    """
    if not adzuna_id:
        return None, ''
    details_url = f'https://www.adzuna.com/details/{adzuna_id}'
    try:
        session = requests.Session()
        session.headers.update(HEADERS)

        details_resp = session.get(details_url, timeout=15)
        if details_resp.status_code >= 400:
            return None, ''
        details_text = _extract_readable_text(details_resp.text)

        soup = BeautifulSoup(details_resp.text, 'html.parser')
        apply_href = None
        for a in soup.find_all('a', href=True):
            if 'land/ad' in a['href'] and 'apply' in a.get_text(strip=True).lower():
                apply_href = a['href']
                break
        if not apply_href:
            return None, details_text

        apply_url = urljoin(details_url, apply_href)
        session.headers['Referer'] = details_url
        apply_resp = session.get(apply_url, timeout=15)
        if apply_resp.status_code >= 400:
            return None, details_text

        meta_soup = BeautifulSoup(apply_resp.text, 'html.parser')
        meta = meta_soup.find('meta', attrs={'http-equiv': lambda v: v and v.lower() == 'refresh'})
        if meta and meta.get('content'):
            m = re.search(r'url=(.+)', meta['content'], re.IGNORECASE)
            if m:
                return m.group(1).strip(), details_text

        # No meta-refresh found — maybe requests already followed a real HTTP redirect
        if apply_resp.url and apply_resp.url != apply_url:
            return apply_resp.url, details_text

        return None, details_text
    except Exception:
        return None, ''


def _parse_adzuna_created(s: str):
    """Parse Adzuna's 'created' timestamp into an aware datetime, or None if unparseable."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None


@app.route('/api/search', methods=['GET'])
def api_search_jobs():
    """Search for jobs via Adzuna API."""
    query    = request.args.get('q', 'product manager').strip()
    page     = max(1, int(request.args.get('page', 1)))
    max_days = max(1, int(request.args.get('days', 7)))

    try:
        data = _adzuna_request(query, page, max_days)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'Adzuna API error: {e}'}), 500

    # Normalise and filter results — only keep jobs where the query appears in the title
    existing_urls = {j.get('url', '') for j in load_data().get('jobs', [])}
    query_lower = query.lower()

    results = []
    for r in data.get('results', []):
        title = r.get('title', '')
        if query_lower not in title.lower():
            continue  # skip — query phrase not in title
        url = r.get('redirect_url', '')
        results.append({
            'adzuna_id':   r.get('id', ''),
            'title':       title,
            'company':     r.get('company', {}).get('display_name', ''),
            'location':    r.get('location', {}).get('display_name', ''),
            'url':         url,
            'description': r.get('description', '')[:400],
            'posted':      r.get('created', '')[:10],
            'already_tracked': url in existing_urls,
        })

    return jsonify({
        'results':  results,
        'total':    data.get('count', 0),
        'page':     page,
    })


# ─────────────────────────────────────────────
# Adzuna sweep — manually triggered, pulls everything
# posted since the last successful sweep
# ─────────────────────────────────────────────

SWEEP_MAX_PAGES_PER_QUERY = 20      # safety cap (~1000 results per keyword)
SWEEP_RESULTS_PER_PAGE = 50
ADZUNA_RETRY_WAIT_SECONDS = 300      # on a request failure (e.g. the 503s seen two mornings running
                                      # in a row, 2026-07-22), wait 5 minutes and retry once before
                                      # giving up on that request — Adzuna's outages so far have been
                                      # brief, and without this, a single bad morning means the whole
                                      # day's Adzuna results are silently missed (the sweep retries
                                      # from the same point next time, but that's the next scheduled
                                      # run, hours away). Sweep-only, not the shared _adzuna_request()
                                      # helper — a manual, interactive search should fail fast, not
                                      # hang the UI for 5 minutes.
SWEEP_DEFAULT_LOOKBACK_HOURS = 24   # used on the very first sweep, when there's no prior timestamp
SWEEP_LOOKBACK_BUFFER_HOURS = 6     # never ask about anything newer than (now - this) — Adzuna's
                                     # own search index has backfill lag (a listing's 'created'
                                     # timestamp reflects when the employer posted it, not when
                                     # Adzuna's crawler indexed it), so querying right up to "now"
                                     # undercounts and permanently misses late-indexed listings,
                                     # since the cursor never looks backward once it advances.
                                     # Found 2026-07-16 — see PROBLEMS.md #5. Applies to every
                                     # sweep (scheduled or manual), not just the overnight one.

def sweep_adzuna_jobs() -> dict:
    """
    Paginate through Adzuna results for every configured title keyword, from the last successful
    sweep (or SWEEP_DEFAULT_LOOKBACK_HOURS on the first run) up to (now - SWEEP_LOOKBACK_BUFFER_HOURS)
    — never all the way to "now", since Adzuna's own search index has backfill lag and querying
    too close to the present undercounts. Gathers raw candidates and runs them through the shared
    evaluate_candidates() funnel. Only advances 'last_adzuna_sweep' if every keyword query
    completed without error, so a failed sweep is retried from the same starting point next time
    (safe — dedup makes re-fetching idempotent).
    """
    global _sweep_running

    with _sweep_lock:
        if _sweep_running:
            return {'added': 0, 'candidates_seen': 0, 'excluded': [], 'excluded_by_reason': {},
                    'errors': [{'error': 'Sweep already in progress'}]}
        _sweep_running = True

    try:
        data = load_data()
        queries = list(data.get('title_keywords', []))
        now = datetime.now(timezone.utc)
        until_dt = now - timedelta(hours=SWEEP_LOOKBACK_BUFFER_HOURS)

        result = {
            'added': 0,
            'candidates_seen': 0,
            'excluded': [],
            'excluded_by_reason': {},
            'queries': queries,
            'since': None,
            'until': until_dt.isoformat(),
            'errors': [],
        }

        if not queries:
            result['errors'].append({'error': 'No title keywords configured — add some in the Keywords tab.'})
            data['last_adzuna_sweep_result'] = {**result, 'at': now.isoformat()}
            save_data(data)
            return result

        since_raw = data.get('last_adzuna_sweep')
        since_dt = datetime.fromisoformat(since_raw) if since_raw else (now - timedelta(hours=SWEEP_DEFAULT_LOOKBACK_HOURS))
        result['since'] = since_dt.isoformat()

        # Sized off wall-clock "now", not until_dt — this just needs to be large enough that
        # Adzuna's max_days_old returns enough historical depth to reach back to since_dt;
        # the until_dt buffer is enforced separately below, per result.
        days_needed = max(1, math.ceil((now - since_dt).total_seconds() / 86400))

        raw_candidates = []
        seen_ids_this_run = set()

        def _run_adzuna_query(query, where=''):
            """One query's full pagination (remote_only when where='', else location-scoped —
            see _adzuna_request()). Appends survivors to raw_candidates via the enclosing
            closure; returns (nothing) once the boundary is hit, a page comes back short, or
            the request fails twice."""
            page = 1
            while page <= SWEEP_MAX_PAGES_PER_QUERY:
                try:
                    raw = _adzuna_request(query, page, days_needed, results_per_page=SWEEP_RESULTS_PER_PAGE, where=where)
                except Exception as e:
                    label = f"'{query}' (where={where})" if where else f"'{query}'"
                    print(f"    [adzuna] request failed for {label} page {page}: {e} — "
                          f"waiting {ADZUNA_RETRY_WAIT_SECONDS}s and retrying once")
                    time.sleep(ADZUNA_RETRY_WAIT_SECONDS)
                    try:
                        raw = _adzuna_request(query, page, days_needed, results_per_page=SWEEP_RESULTS_PER_PAGE, where=where)
                    except Exception as e2:
                        result['errors'].append({'query': query, 'where': where, 'error': str(e2)})
                        return

                raw_results = raw.get('results', [])
                if not raw_results:
                    return

                hit_boundary = False
                for r in raw_results:
                    created_dt = _parse_adzuna_created(r.get('created', ''))
                    if created_dt is not None and created_dt > until_dt:
                        continue  # too recent — Adzuna may still be backfilling nearby listings;
                                  # leave it for a later sweep instead of risking an undercount
                    if created_dt is not None and created_dt <= since_dt:
                        hit_boundary = True
                        break

                    result['candidates_seen'] += 1
                    adzuna_id = r.get('id', '')
                    if adzuna_id and adzuna_id in seen_ids_this_run:
                        continue  # same listing matched by more than one keyword/location query
                    if adzuna_id:
                        seen_ids_this_run.add(adzuna_id)

                    raw_candidates.append({
                        'adzuna_id':    adzuna_id,
                        'title':        r.get('title', ''),
                        'company_name': r.get('company', {}).get('display_name', ''),
                        'location':     r.get('location', {}).get('display_name', ''),
                        'url':          r.get('redirect_url', ''),  # resolved inside evaluate_candidates()
                        'description':  r.get('description', '')[:400],
                        'posted':       r.get('created', '')[:10],
                        'source':       'adzuna',
                        # Only the remote_only query is pre-filtered remote — a location-scoped
                        # (hybrid-state) query isn't, so leave is_remote unset and let
                        # evaluate_candidates() fall back to the detect_remote() heuristic.
                        'is_remote':    True if not where else None,
                    })

                if hit_boundary or len(raw_results) < SWEEP_RESULTS_PER_PAGE:
                    return
                page += 1

        # Accepted hybrid states (settings.accepted_locations' 'hybrid-X' entries, 2026-07-22) —
        # Adzuna has no native "remote or hybrid in these specific states" filter, so each gets
        # its own additional location-scoped query per keyword, on top of the existing
        # remote_only query. Real added cost (more requests, more candidates through the funnel,
        # longer sweep runtime) — accepted as an MVP tradeoff.
        _, accepted_hybrid_states = _parse_accepted_locations(data.get('settings', {}))
        for query in queries:
            _run_adzuna_query(query)
            for state in sorted(accepted_hybrid_states):
                _run_adzuna_query(query, where=state)

        data = load_data()  # reload in case something else wrote to it while we were paginating
        to_save, excluded = evaluate_candidates(raw_candidates, data)
        data.setdefault('jobs', []).extend(to_save)
        result['added'] = len([j for j in to_save if not j.get('exclusion_reason')])
        result['excluded'] = excluded
        result['excluded_by_reason'] = _excluded_by_reason(excluded)
        _record_lifetime_funnel_stats(data, result['added'], result['excluded_by_reason'])

        if not result['errors']:
            # Advance to until_dt (the boundary we actually covered), NOT wall-clock now —
            # otherwise the buffer above would be pointless, since the next sweep's 'since'
            # would jump straight past the deliberately-not-yet-queried recent window.
            data['last_adzuna_sweep'] = until_dt.isoformat()

        data['last_adzuna_sweep_result'] = {**result, 'at': now.isoformat()}
        save_data(data)

        return result
    finally:
        with _sweep_lock:
            _sweep_running = False


def sweep_remote_com_jobs() -> dict:
    """
    Scan every saved remote.com search URL (data['settings']['remote_com_search_urls'] — ready-
    made search URLs the user pastes in, filters already baked in via query params, unlike
    Adzuna's per-title_keywords query construction) and run the results through the shared
    evaluate_candidates() funnel. Unlike sweep_adzuna_jobs(), there's no time-window cursor —
    each run fully re-scans the configured pages and relies entirely on evaluate_candidates()'s
    existing URL dedup + recent-repost dedup, which is already idempotent and safe to re-run.
    """
    global _remote_com_sweep_running

    with _remote_com_sweep_lock:
        if _remote_com_sweep_running:
            return {'added': 0, 'candidates_seen': 0, 'excluded': [], 'excluded_by_reason': {},
                    'errors': [{'error': 'remote.com sweep already in progress'}]}
        _remote_com_sweep_running = True

    try:
        data = load_data()
        search_urls = list(data.get('settings', {}).get('remote_com_search_urls', []))
        now = datetime.now(timezone.utc)

        result = {
            'added': 0,
            'candidates_seen': 0,
            'excluded': [],
            'excluded_by_reason': {},
            'errors': [],
        }

        if not search_urls:
            data['last_remote_com_sweep_result'] = {**result, 'at': now.isoformat()}
            save_data(data)
            return result

        raw_candidates = []
        seen_urls_this_run = set()
        for search_url in search_urls:
            try:
                found = _scrape_remote_com_search(search_url)
            except Exception as e:
                result['errors'].append({'query': search_url, 'error': str(e)})
                continue
            for cand in found:
                if cand['url'] in seen_urls_this_run:
                    continue  # same listing matched by more than one saved search
                seen_urls_this_run.add(cand['url'])
                raw_candidates.append(cand)

        result['candidates_seen'] = len(raw_candidates)

        data = load_data()  # reload in case something else wrote to it while we were scraping
        to_save, excluded = evaluate_candidates(raw_candidates, data)
        data.setdefault('jobs', []).extend(to_save)
        result['added'] = len([j for j in to_save if not j.get('exclusion_reason')])
        result['excluded'] = excluded
        result['excluded_by_reason'] = _excluded_by_reason(excluded)
        _record_lifetime_funnel_stats(data, result['added'], result['excluded_by_reason'])

        data['last_remote_com_sweep'] = now.isoformat()
        data['last_remote_com_sweep_result'] = {**result, 'at': now.isoformat()}
        save_data(data)

        return result
    finally:
        with _remote_com_sweep_lock:
            _remote_com_sweep_running = False


# ─────────────────────────────────────────────
# Unified "Get New Jobs" — runs the company-board check, the Adzuna sweep, and the remote.com
# sweep in sequence and combines their results into one report. Each source saves before the
# next runs, so later sources' dedup passes naturally see earlier ones' results.
# ─────────────────────────────────────────────

_get_jobs_lock = threading.Lock()
_get_jobs_running = False

# Cross-process lock (separate from the in-process _get_jobs_lock above), used by the
# --get-new-jobs CLI entry point so a scheduled overnight run can never overlap a still-running
# previous one — the in-process lock only protects within a single Python process, which isn't
# enough once this runs as a fresh process every night via launchd.
GET_NEW_JOBS_LOCKFILE = os.path.join(BASE_DIR, '.get_new_jobs.lock')

def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def _acquire_get_new_jobs_lock() -> bool:
    if os.path.exists(GET_NEW_JOBS_LOCKFILE):
        try:
            with open(GET_NEW_JOBS_LOCKFILE) as f:
                old_pid = int(f.read().strip())
            if _pid_is_running(old_pid):
                return False
        except (ValueError, OSError):
            pass  # stale/corrupt lockfile — proceed and overwrite it
    with open(GET_NEW_JOBS_LOCKFILE, 'w') as f:
        f.write(str(os.getpid()))
    return True

def _release_get_new_jobs_lock():
    try:
        os.remove(GET_NEW_JOBS_LOCKFILE)
    except OSError:
        pass


def _parse_job_date(value):
    """Parse a job record's date field robustly — returns None (never raises) on anything
    missing or unparseable, and normalizes naive datetimes (from older records saved before
    every timestamp in this app was written timezone-aware) to UTC so they can be safely
    compared against an aware 'now'."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _roll_up_job_token_usage_before_purge(data: dict, job: dict) -> None:
    """
    classification and fit_rating token usage (see _add_token_usage()) lives ONLY on the job
    record — unlike generation/todo_matching/cover_letter_audit/interview_prep, which also have
    a permanent per-application disk record (sections.json/interview_prep.json) GET /api/costs
    can read directly. Before 2026-10-07, cleanup_old_jobs() silently discarded this cost data
    the instant a job aged out, even though it was real, already-billed spend.

    Called right before a job is removed, folding its classification/fit_rating usage into the
    metrics database (dated today — the purge date, not the original spend date, since a job's
    token_usage is a cumulative total with no per-call date kept) so this stops being lost going
    forward. A part with no by_model breakdown (jobs rated/classified before 2026-09-21) is
    attributed to haiku — classify_job()/rate_job_fit() were Haiku-only for their entire history
    before that, so an unattributed total was never actually anything else; same tolerance
    GET /api/costs already applies. Doesn't recover anything purged before this existed, or
    before 2026-10-07's earlier data['lifetime_token_usage'] version of this same fix — that
    history is genuinely gone, same as the funnel lifetime_stats' own limitation.
    """
    usage = job.get('token_usage') or {}
    for part in ('classification', 'fit_rating'):
        part_usage = usage.get(part)
        if not part_usage:
            continue
        by_model = part_usage.get('by_model')
        if not by_model:
            by_model = {'haiku': {
                'input_tokens': part_usage.get('input_tokens', 0),
                'output_tokens': part_usage.get('output_tokens', 0),
                'calls': part_usage.get('calls', 0),
            }}
        for mk, mv in by_model.items():
            _record_daily_token_usage(part, mk, mv)


def cleanup_old_jobs(data: dict) -> int:
    """
    Remove data['jobs'] entries older than JOB_RETENTION_DAYS (by last_seen, falling back to
    first_seen, then created_at) — a rolling working-set window, not a permanent record. Never
    touches anything under OUTPUT_FOLDER/Drive — generated application documents are the actual
    permanent record of what was applied to, and are never cleaned up by this or anything else.
    A record with no parseable date at all is kept — never delete on missing/unparseable data.
    Rolls each removed job's classification/fit_rating token usage into
    data['lifetime_token_usage'] first (see _roll_up_job_token_usage_before_purge()) so that real
    spend doesn't just vanish from the Costs tab the moment this runs.
    Returns the number of jobs removed.
    """
    now = datetime.now(timezone.utc)
    kept = []
    removed = 0
    for job in data.get('jobs', []):
        age_source = job.get('last_seen') or job.get('first_seen') or job.get('created_at')
        parsed = _parse_job_date(age_source)
        if parsed is not None and (now - parsed).days >= JOB_RETENTION_DAYS:
            removed += 1
            _roll_up_job_token_usage_before_purge(data, job)
            continue
        kept.append(job)
    data['jobs'] = kept
    return removed


def get_new_jobs() -> dict:
    global _get_jobs_running

    with _get_jobs_lock:
        if _get_jobs_running:
            return {'added': 0, 'candidates_seen': 0, 'excluded': [], 'excluded_by_reason': {},
                    'errors': [{'error': 'Already running'}]}
        _get_jobs_running = True

    try:
        check_result = check_all_companies()
        sweep_result = sweep_adzuna_jobs()
        remote_com_result = sweep_remote_com_jobs()
        jobicy_result = sweep_jobicy_jobs()
        himalayas_result = sweep_himalayas_jobs()
        workable_result = sweep_workable_jobs()

        combined_excluded = (check_result.get('excluded', []) + sweep_result.get('excluded', [])
                              + remote_com_result.get('excluded', []) + jobicy_result.get('excluded', [])
                              + himalayas_result.get('excluded', []) + workable_result.get('excluded', []))
        combined = {
            'added': (check_result.get('added', 0) + sweep_result.get('added', 0)
                      + remote_com_result.get('added', 0) + jobicy_result.get('added', 0)
                      + himalayas_result.get('added', 0) + workable_result.get('added', 0)),
            'candidates_seen': (check_result.get('candidates_seen', 0) + sweep_result.get('candidates_seen', 0)
                                 + remote_com_result.get('candidates_seen', 0) + jobicy_result.get('candidates_seen', 0)
                                 + himalayas_result.get('candidates_seen', 0) + workable_result.get('candidates_seen', 0)),
            'excluded': combined_excluded,
            'excluded_by_reason': _excluded_by_reason(combined_excluded),
            'errors': (check_result.get('errors', []) + sweep_result.get('errors', [])
                       + remote_com_result.get('errors', []) + jobicy_result.get('errors', [])
                       + himalayas_result.get('errors', []) + workable_result.get('errors', [])),
        }

        data = load_data()
        now_iso = datetime.now(timezone.utc).isoformat()
        data['last_get_jobs_at'] = now_iso
        combined['jobs_cleaned_up'] = cleanup_old_jobs(data)
        data['last_get_jobs_result'] = combined
        save_data(data)

        return combined
    finally:
        with _get_jobs_lock:
            _get_jobs_running = False


@app.route('/api/get-new-jobs', methods=['POST'])
def api_get_new_jobs():
    if _get_jobs_running:
        return jsonify({'message': 'Already running'}), 409

    def run():
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Starting Get New Jobs...")
        res = get_new_jobs()
        print(f"Done. {res['added']} new jobs added ({res['candidates_seen']} candidates seen). "
              f"{len(res['errors'])} error(s).\n")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'message': 'Started'})


@app.route('/api/open-folder', methods=['POST'])
def api_open_folder():
    """Open a local folder in Finder."""
    import subprocess
    body = request.get_json(silent=True) or {}
    path = body.get('path', '').strip()
    if not path or not os.path.isdir(path):
        return jsonify({'error': 'Invalid path'}), 400
    subprocess.Popen(['open', path])
    return jsonify({'ok': True})


@app.route('/api/jobs/add-from-search', methods=['POST'])
def api_add_from_search():
    """Add a job from search results to the tracked list."""
    body = request.get_json(silent=True) or {}
    data = load_data()

    # Resolve Adzuna redirect URL to the real ATS URL
    url = body.get('url', '')
    resolved_url = _resolve_redirect_url(url)

    # Deduplicate — check both original and resolved URL
    existing_urls = {j.get('url') for j in data['jobs']}
    if resolved_url in existing_urls or url in existing_urls:
        return jsonify({'error': 'Job already in your list'}), 409

    title    = body.get('title', '')
    location = body.get('location', '')
    now = datetime.now(timezone.utc)
    job = {
        'id':           str(uuid.uuid4()),
        'title':        title,
        'company_name': body.get('company_name') or body.get('company', ''),
        'url':          resolved_url,
        'location':     location,
        'description':  body.get('description', ''),
        'source':       body.get('source') or 'adzuna',
        'is_new':       True,
        'is_applied':   False,
        'is_remote':    True,  # Adzuna results are filtered with remote_only=1
        'match_score':  0,
        'posted':       body.get('posted', ''),  # date the job was posted, per Adzuna
        'first_seen':   now.isoformat(),
        'last_seen':    now.isoformat(),
        'created_at':   now.isoformat(),  # when we added it to the tracked list
        'remote_status':      'unknown',
        'hybrid_state':       '',
        'remote_reasoning':   '',
        'software_status':    'unknown',
        'software_reasoning': '',
        'evaluated_at':       None,
        'jd_fetch_status':    'failed',
        'fit_rating':         None,
        'fit_reasoning':      '',
        'exclusion_reason':    None,
        'exclusion_reasoning': '',
    }

    # Classify + rate exactly like every other job — a manually-added search result shouldn't
    # be treated differently just because of how it entered the system. No title_keywords/
    # title_exclude_keywords gating though, same as every other manual-add route (capture,
    # ad-hoc) — this is a deliberate action, not a filtered sweep.
    try:
        settings = data.get('settings', {})
        candidate_location, _ = _parse_accepted_locations(settings)
        jd_text = fetch_job_description(job)
        if is_real_job_description(jd_text):
            verdict = classify_job(jd_text, title, job['company_name'], candidate_location=candidate_location)
            _apply_classification(job, jd_text, verdict, settings, now, jd_fetch_status='ok')
    except Exception as e:
        print(f"    [add-from-search] classify failed for {title} @ {job['company_name']}: {e}")

    data['jobs'].append(job)
    save_data(data)
    return jsonify({'ok': True, 'job': job})


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Job Checker')
    parser.add_argument('--check', action='store_true', help='Run company-board check only, and exit (for cron/scheduled tasks)')
    parser.add_argument('--get-new-jobs', action='store_true',
                         help='Run the full unified pipeline (company boards + Adzuna sweep) and exit — for scheduled/overnight runs')
    parser.add_argument('--port', type=int, default=5000, help='Port for web server (default 5000)')
    parser.add_argument('--data-file', help='Path to the JSON data file (default: job_data.json next to this script)')
    parser.add_argument('--dev', action='store_true',
                         help='Shorthand for --data-file job_data.dev.json — keeps dev/test runs off your real tracked list')
    parser.add_argument('--google-auth', action='store_true',
                         help='One-time interactive OAuth setup for live Google Doc generation — opens a browser, '
                              'caches a refresh token to token.json. Needs credentials.json (OAuth Desktop app '
                              'client from Google Cloud Console) in the repo root first. Run this in a real '
                              'terminal, not through an automated/headless session.')
    args = parser.parse_args()

    if args.google_auth:
        from google_auth_oauthlib.flow import InstalledAppFlow
        creds_path = os.path.join(BASE_DIR, 'credentials.json')
        if not os.path.exists(creds_path):
            print(f"credentials.json not found at {creds_path}")
            print("Create an OAuth 2.0 Client ID (Desktop app) in Google Cloud Console, enable the")
            print("Drive API and Docs API, and download it as credentials.json into the repo root first.")
            raise SystemExit(1)
        flow = InstalledAppFlow.from_client_secrets_file(creds_path, GOOGLE_OAUTH_SCOPES)
        creds = flow.run_local_server(port=0)
        with open(os.path.join(BASE_DIR, 'token.json'), 'w') as f:
            f.write(creds.to_json())
        print("Authorized — token.json saved. Live Google Doc generation is ready to use.")
        raise SystemExit(0)

    if args.data_file:
        DATA_FILE = args.data_file
        METRICS_DB_FILE = args.data_file + '.metrics.db'
    elif args.dev:
        DATA_FILE = os.path.join(BASE_DIR, 'job_data.dev.json')
        METRICS_DB_FILE = os.path.join(BASE_DIR, 'metrics.dev.db')

    print(f"Using data file: {DATA_FILE}")

    if args.check:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Running scheduled job check...")
        result = check_all_companies()
        print(f"Complete. {result['added']} new jobs added ({result['candidates_seen']} candidates seen, "
              f"{len(result['excluded'])} excluded). {len(result['errors'])} error(s).")
        if result['errors']:
            for e in result['errors']:
                print(f"  ✗ {e['company']}: {e['error']}")
    elif args.get_new_jobs:
        if not _acquire_get_new_jobs_lock():
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Skipping — a previous Get New Jobs run "
                  f"is still in progress (lockfile: {GET_NEW_JOBS_LOCKFILE}).")
        else:
            try:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Running scheduled Get New Jobs "
                      f"(company boards + Adzuna sweep)...")
                result = get_new_jobs()
                print(f"Complete. {result['added']} new jobs added ({result['candidates_seen']} candidates seen, "
                      f"{len(result['excluded'])} excluded). {len(result['errors'])} error(s).")
                if result['errors']:
                    for e in result['errors']:
                        print(f"  ✗ {e}")
            finally:
                _release_get_new_jobs_lock()
    else:
        print(f"Job Checker starting at http://localhost:{args.port}")
        print("Press Ctrl+C to stop.")
        app.run(host='127.0.0.1', port=args.port, debug=False)
