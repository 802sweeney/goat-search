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
import re
import uuid
import math
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse
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
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    HAS_SCHEDULER = True
except ImportError:
    HAS_SCHEDULER = False

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, 'job_data.json')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}

CAREER_DOCS_DIR = os.path.join(BASE_DIR, 'career_docs')

app = Flask(__name__, static_folder=BASE_DIR)
CORS(app)

# Track in-progress check to avoid concurrent runs
_check_lock = threading.Lock()
_check_running = False

# Track in-progress Adzuna sweep to avoid concurrent runs
_sweep_lock = threading.Lock()
_sweep_running = False

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
        if 'settings' not in data:
            data['settings'] = {}
        if 'last_adzuna_sweep' not in data:
            data['last_adzuna_sweep'] = None
        if 'last_adzuna_sweep_result' not in data:
            data['last_adzuna_sweep_result'] = None
        return data
    return {
        'companies': [],
        'title_keywords': [],
        'desc_keywords': [],
        'jobs': [],
        'seen_job_ids': [],
        'last_checked': None,
        'last_check_errors': [],
        'last_adzuna_sweep': None,
        'last_adzuna_sweep_result': None,
        'settings': {}
    }

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

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
            jobs.append({
                'id': make_job_id(company_id, title, job_url),
                'company_id': company_id,
                'company_name': company_name,
                'title': title,
                'url': job_url,
                'location': location,
                'is_remote': detect_remote(title, location),
                'source': 'lever',
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
# Main check logic
# ─────────────────────────────────────────────

def check_all_companies() -> tuple[int, int, list]:
    """
    Scrape all companies, update job data.
    Returns (new_jobs_count, total_jobs_count, errors).
    """
    global _check_running

    with _check_lock:
        if _check_running:
            return 0, 0, [{'company': 'system', 'error': 'Check already in progress'}]
        _check_running = True

    try:
        data = load_data()
        existing_jobs = {j['id']: j for j in data.get('jobs', [])}

        new_jobs_found = 0
        fresh_jobs = {}
        errors = []
        now = datetime.now(timezone.utc).isoformat()

        for company in data.get('companies', []):
            print(f"  Checking {company['name']} ({company['url']})...")
            try:
                found = scrape_company(company)
                for job in found:
                    jid = job['id']
                    if jid in existing_jobs:
                        entry = existing_jobs[jid].copy()
                        entry['last_seen'] = now
                        # Keep is_new=True until user dismisses it
                        fresh_jobs[jid] = entry
                    else:
                        job['first_seen'] = now
                        job['last_seen'] = now
                        job['is_new'] = True
                        fresh_jobs[jid] = job
                        new_jobs_found += 1
                print(f"    → {len(found)} matching jobs")
            except Exception as e:
                print(f"    ✗ Error: {e}")
                errors.append({'company': company['name'], 'error': str(e)})

        data['jobs'] = list(fresh_jobs.values())
        data['seen_job_ids'] = list(fresh_jobs.keys())
        data['last_checked'] = now
        data['last_check_errors'] = errors
        save_data(data)

        return new_jobs_found, len(fresh_jobs), errors
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
        'last_checked': data.get('last_checked'),
        'job_count': len(data.get('jobs', [])),
        'new_jobs': sum(1 for j in data.get('jobs', []) if j.get('is_new')),
        'company_count': len(data.get('companies', [])),
        'check_running': _check_running,
        'errors': data.get('last_check_errors', []),
        'last_adzuna_sweep': data.get('last_adzuna_sweep'),
        'last_adzuna_sweep_result': data.get('last_adzuna_sweep_result'),
        'sweep_running': _sweep_running,
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
    })

@app.route('/api/keywords', methods=['POST'])
def api_update_keywords():
    body = request.json or {}
    title_kw = [k.strip() for k in body.get('title_keywords', []) if k.strip()]
    desc_kw = [k.strip() for k in body.get('desc_keywords', []) if k.strip()]
    data = load_data()
    data['title_keywords'] = title_kw
    data['desc_keywords'] = desc_kw
    save_data(data)
    return jsonify({'title_keywords': title_kw, 'desc_keywords': desc_kw})


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
    scored = []
    for j in jobs:
        s = score_job(j.get('title', ''), j.get('location', ''), title_kw, desc_kw)
        if matching_only and s < 0:
            continue
        scored.append({**j, 'match_score': s})

    scored.sort(key=lambda j: (not j.get('is_new', False), -j.get('match_score', 0), j.get('first_seen', '')))
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


@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    data = load_data()
    return jsonify(data.get('settings', {}))

@app.route('/api/settings', methods=['POST'])
def api_save_settings():
    body = request.json or {}
    data = load_data()
    data['settings'].update(body)
    save_data(data)
    return jsonify(data['settings'])

@app.route('/api/jobs/<job_id>/apply', methods=['POST'])
def api_mark_applied(job_id):
    data = load_data()
    for job in data['jobs']:
        if job['id'] == job_id:
            job['is_applied'] = True
            job['applied_at'] = datetime.now(timezone.utc).isoformat()
            break
    save_data(data)
    return jsonify({'ok': True})


# ─────────────────────────────────────────────
# Application generation (Anthropic API)
# ─────────────────────────────────────────────

def fetch_job_description(job: dict) -> str:
    """Fetch and extract readable text from a job posting URL."""
    url = job.get('url', '')
    if not url:
        return ''
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, 'html.parser')
        # Remove nav, header, footer, scripts
        for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'noscript']):
            tag.decompose()
        text = soup.get_text(separator='\n', strip=True)
        # Collapse blank lines
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        return '\n'.join(lines)[:8000]  # cap at 8k chars
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

    # Override/add from Google Doc URLs in settings
    gdoc_urls = (settings or {}).get('gdoc_urls', {})
    for doc_name, url in gdoc_urls.items():
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
}

def generate_application_docs(job: dict, job_description: str, career_docs: dict, model_key: str = 'sonnet') -> dict:
    """Call Anthropic API to generate cover letter and resume."""
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key or api_key == 'your-key-here':
        raise ValueError('ANTHROPIC_API_KEY not set. Add it to your .env file.')

    if not HAS_ANTHROPIC:
        raise ImportError('anthropic package not installed. Run: pip install anthropic')

    model = MODELS.get(model_key, MODELS['sonnet'])
    client = anthropic_sdk.Anthropic(api_key=api_key)

    # Hardcoded style rules — always enforced, even if instructions doc fails to load
    HARD_RULES = (
        'STRICT RULES — these override everything else and must never be violated:\n'
        '- NEVER use em dashes (—). They are an AI giveaway. Use commas, periods, or rewrite the sentence.\n'
        '- NEVER use en dashes (–) as punctuation.\n'
        '- NEVER use buzzwords or corporate filler (e.g. "leverage", "synergy", "impactful", "passionate about").\n'
        '- Write in a direct, human voice. If a sentence sounds like it was written by AI, rewrite it.\n'
        '- NEVER fabricate, calculate, or infer numbers. Only use figures explicitly stated in the career reference doc. '
        'If a number is not in the doc, do not include it. This includes years of experience, team sizes, percentages, and dates.\n\n'
    )

    # System prompt comes from instructions.md
    doc_instructions = career_docs.get('instructions.md', '').strip()
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
    EXCLUDE = {'instructions.md', 'resume.md', 'resume'}
    career_context_parts = []
    for fname, content in career_docs.items():
        if fname not in EXCLUDE and content.strip():
            label = fname.replace('.md', '').replace('_', ' ').title()
            career_context_parts.append(f'## {label}\n{content}')
    career_context = '\n\n---\n\n'.join(career_context_parts)

    user_message = f"""Here are my career reference materials:

{career_context}

---

## Job I'm Applying To

**Title:** {job.get('title', '')}
**Company:** {job.get('company_name', '')}
**URL:** {job.get('url', '')}

**Job Description:**
{job_description}"""

    message = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{'role': 'user', 'content': user_message}],
    )

    response_text = message.content[0].text

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
        },
        'feedback': feedback_text,
        'usage': {
            'input_tokens': message.usage.input_tokens,
            'output_tokens': message.usage.output_tokens,
        }
    }


def _safe_name(s: str, maxlen: int = 50) -> str:
    """Strip characters that are invalid in folder/file names."""
    return re.sub(r'[<>:"/\\|?*]', '', s).strip()[:maxlen]


def get_output_dir(job: dict) -> str:
    """Return today's output folder path for a job (may not exist yet)."""
    company     = _safe_name(job.get('company_name', 'Company'))
    title       = _safe_name(job.get('title', 'Role'))
    date_str    = datetime.now().strftime('%Y-%m-%d')
    output_root = os.environ.get('OUTPUT_FOLDER', '') or os.path.join(BASE_DIR, 'applications')
    return os.path.join(output_root, company, title, date_str)


def _markdown_to_docx(document, text: str):
    """
    Write markdown-ish text into a python-docx Document.
    Handles: # headings, **bold**, bullet lines (- / *), blank-line paragraphs.
    """
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    import copy

    for line in text.splitlines():
        stripped = line.strip()

        # Headings
        if stripped.startswith('### '):
            document.add_heading(stripped[4:], level=3)
        elif stripped.startswith('## '):
            document.add_heading(stripped[3:], level=2)
        elif stripped.startswith('# '):
            document.add_heading(stripped[2:], level=1)

        # Bullet points
        elif stripped.startswith(('- ', '* ', '• ')):
            content = stripped[2:]
            p = document.add_paragraph(style='List Bullet')
            _add_runs(p, content)

        # Horizontal rule
        elif stripped in ('---', '***', '___'):
            document.add_paragraph('─' * 40)

        # Empty line → paragraph break
        elif stripped == '':
            document.add_paragraph('')

        # Normal paragraph
        else:
            p = document.add_paragraph()
            _add_runs(p, stripped)


def _add_runs(paragraph, text: str):
    """Split text on **bold** markers and add runs with formatting."""
    parts = re.split(r'\*\*(.+?)\*\*', text)
    for i, part in enumerate(parts):
        if not part:
            continue
        run = paragraph.add_run(part)
        if i % 2 == 1:  # odd indices are inside **...**
            run.bold = True


def _content_lines(text: str) -> list:
    """Split Claude's output into clean lines for template insertion."""
    lines = []
    for line in text.strip().splitlines():
        s = line.strip()
        if not s:
            continue
        # Strip leading bullet markers — template already has list styling
        if s.startswith(('- ', '* ', '• ')):
            s = s[2:].strip()
        if s:
            lines.append(s)
    return lines or ['']


def _replace_paragraph(para, lines: list):
    """
    Replace a template paragraph (containing a {{PLACEHOLDER}}) with one
    or more paragraphs of the same style — one per line.
    Modifies the document XML in place.
    """
    import copy
    from lxml import etree

    WNS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

    # Put first line into the existing paragraph
    for run in para.runs:
        run.text = ''
    if para.runs:
        para.runs[0].text = lines[0]
    else:
        para.add_run(lines[0])

    # Insert additional lines as new paragraphs immediately after, in reverse
    # order so each addnext lands in the right position.
    ref = para._p
    for line in reversed(lines[1:]):
        new_p = copy.deepcopy(para._p)
        # Remove all existing runs from the copy
        for r in new_p.findall(f'{{{WNS}}}r'):
            new_p.remove(r)
        # Add a fresh run with the line text
        r_el = etree.SubElement(new_p, f'{{{WNS}}}r')
        t_el = etree.SubElement(r_el, f'{{{WNS}}}t')
        t_el.text = line
        t_el.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        ref.addnext(new_p)


def _all_paragraphs(doc):
    """Yield every paragraph in the document — body, headers, footers, and table cells."""
    from docx.oxml.ns import qn
    yield from doc.paragraphs
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs


def _fill_template(template_bytes: bytes, replacements: dict) -> bytes:
    """
    Download a .docx template and replace every {{KEY}} placeholder.
    Multi-line values (bullet lists) expand into multiple same-style paragraphs.
    Returns the filled document as bytes.
    """
    import io
    from docx import Document

    doc = Document(io.BytesIO(template_bytes))

    matched = set()
    for para in list(_all_paragraphs(doc)):
        text = para.text
        for key, value in replacements.items():
            marker = '{{' + key + '}}'
            if marker in text:
                lines = _content_lines(value) if value else ['(not generated)']
                _replace_paragraph(para, lines)
                matched.add(key)
                print(f'  [template] Replaced {{{{{key}}}}} with {len(lines)} line(s)')
                break

    unmatched = set(replacements) - matched
    if unmatched:
        print(f'  [template] WARNING — placeholders not found in template: {unmatched}')

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _download_gdoc_as_docx(url: str) -> bytes:
    """Download a Google Doc as .docx bytes."""
    import re as _re
    m = _re.search(r'/d/([a-zA-Z0-9_-]+)', url)
    if not m:
        raise ValueError(f'Cannot extract doc ID from URL: {url}')
    doc_id = m.group(1)
    export_url = f'https://docs.google.com/document/d/{doc_id}/export?format=docx'
    resp = requests.get(export_url, timeout=15)
    if resp.status_code == 403:
        raise ValueError('Template Google Doc is not publicly shared. Set sharing to "Anyone with the link can view".')
    resp.raise_for_status()
    return resp.content


def save_application_docs(job: dict, docs: dict, job_description: str = '', settings: dict = None) -> dict:
    """
    Save generated docs as plain text inside Google Drive sync folder.
    Folder structure: {output_root}/{Company}/{Job Title}/{YYYY-MM-DD}/
      - Cover Letter.txt
      - Resume Bullets.txt
      - Job Description.txt
      - Feedback.txt
    """
    if settings is None:
        settings = {}

    output_dir = get_output_dir(job)
    os.makedirs(output_dir, exist_ok=True)

    results = {'folder': output_dir}

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

    # ── 2. Job description text file — only written once per day ─────────────
    jd_path = os.path.join(output_dir, 'Job Description.txt')
    if job_description and not os.path.exists(jd_path):
        with open(jd_path, 'w', encoding='utf-8') as f:
            f.write(f"{job.get('title', '')} at {job.get('company_name', '')}\n")
            f.write(f"{job.get('url', '')}\n\n")
            f.write(job_description)
    if os.path.exists(jd_path):
        results['job_description'] = jd_path

    # ── 3. AI feedback text file ──────────────────────────────────────────────
    feedback = docs.get('feedback', '')
    if feedback:
        fb_path = os.path.join(output_dir, 'Feedback.txt')
        with open(fb_path, 'w', encoding='utf-8') as f:
            f.write(f"AI Feedback — {job.get('title', '')} at {job.get('company_name', '')}\n")
            f.write(f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
            f.write(feedback)
        results['feedback'] = fb_path

    # ── 4. sections.json — for reloading in the UI without re-generating ──────
    import json as _json
    sections_data = {
        'cover_letter': docs.get('cover_letter', ''),
        'sections':     docs.get('sections', {}),
        'contact':      docs.get('contact', {}),
        'feedback':     docs.get('feedback', ''),
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    sj_path = os.path.join(output_dir, 'sections.json')
    with open(sj_path, 'w', encoding='utf-8') as f:
        _json.dump(sections_data, f, indent=2)
    results['sections_json'] = sj_path

    return results


@app.route('/api/jobs/<job_id>/documents', methods=['GET'])
def api_existing_documents(job_id):
    """Return previously generated documents for a job, if they exist."""
    import json as _json
    data = load_data()
    job = next((j for j in data['jobs'] if j['id'] == job_id), None)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    output_root = os.environ.get('OUTPUT_FOLDER', '') or os.path.join(BASE_DIR, 'applications')
    company = _safe_name(job.get('company_name', 'Company'))
    title   = _safe_name(job.get('title', 'Role'))
    job_dir = os.path.join(output_root, company, title)

    if not os.path.isdir(job_dir):
        return jsonify({'exists': False})

    # Find the most recent date folder that has any generated content
    all_date_folders = sorted(
        [d for d in os.listdir(job_dir) if os.path.isdir(os.path.join(job_dir, d))],
        reverse=True
    )
    if not all_date_folders:
        return jsonify({'exists': False})

    # Pick the most recent folder that has at least a Cover Letter.txt
    latest = None
    for d in all_date_folders:
        if os.path.isfile(os.path.join(job_dir, d, 'Cover Letter.txt')):
            latest = os.path.join(job_dir, d)
            latest_date = d
            break
    if not latest:
        return jsonify({'exists': False})

    # Load from sections.json if available, otherwise fall back to Cover Letter.txt only
    sj_path = os.path.join(latest, 'sections.json')
    if os.path.isfile(sj_path):
        with open(sj_path, encoding='utf-8') as f:
            saved = _json.load(f)
        cover_letter = saved.get('cover_letter', '')
        sections     = saved.get('sections', {})
        generated_at = saved.get('generated_at', '')
    else:
        with open(os.path.join(latest, 'Cover Letter.txt'), encoding='utf-8') as f:
            cover_letter = f.read()
        sections     = {}
        generated_at = latest_date  # date only, no time

    feedback = saved.get('feedback', '') if os.path.isfile(sj_path) else ''
    contact  = saved.get('contact', {})  if os.path.isfile(sj_path) else {}

    return jsonify({
        'exists':       True,
        'folder':       latest,
        'date':         latest_date,
        'cover_letter': cover_letter,
        'sections':     sections,
        'contact':      contact,
        'feedback':     feedback,
        'generated_at': generated_at,
    })


@app.route('/api/jobs/<job_id>/preview', methods=['GET'])
def api_preview_application(job_id):
    """Fetch job description and career doc metadata for the preview step."""
    data = load_data()
    job = next((j for j in data['jobs'] if j['id'] == job_id), None)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    try:
        # Use saved JD if it exists — avoids re-scraping and preserves pasted content
        jd_file = os.path.join(get_output_dir(job), 'Job Description.txt')
        if os.path.isfile(jd_file):
            with open(jd_file, encoding='utf-8') as f:
                job_description = f.read()
            print(f'  [preview] Loaded existing Job Description.txt')
        else:
            job_description = fetch_job_description(job)

        career_docs = load_career_docs(data.get('settings', {}))

        doc_info = {}
        for name, content in career_docs.items():
            words = len(content.split()) if content.strip() else 0
            doc_info[name] = {'word_count': words}

        return jsonify({'job_description': job_description, 'docs': doc_info})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/jobs/<job_id>/generate', methods=['POST'])
def api_generate_application(job_id):
    """Generate cover letter + resume for a job using the Anthropic API."""
    data = load_data()
    job = next((j for j in data['jobs'] if j['id'] == job_id), None)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    settings = data.get('settings', {})
    model_key = settings.get('model', 'sonnet')
    body = request.get_json(silent=True) or {}

    try:
        # 1. Use pre-fetched JD from preview, existing file from today, or fetch fresh
        job_description = body.get('job_description')
        if not job_description:
            jd_file = os.path.join(get_output_dir(job), 'Job Description.txt')
            if os.path.exists(jd_file):
                with open(jd_file, encoding='utf-8') as f:
                    job_description = f.read()
                print(f'  [generate] Reusing existing Job Description.txt')
            else:
                job_description = fetch_job_description(job)

        # 2. Load career docs (local files + any Google Doc URLs from settings)
        career_docs = load_career_docs(settings)
        if not any(v.strip() for v in career_docs.values()):
            return jsonify({'error': 'No career docs found. Add content to career_docs/ or add Google Doc URLs in settings.'}), 400

        # 3. Generate via Anthropic API
        result = generate_application_docs(job, job_description, career_docs, model_key)

        # 4. Save files
        file_paths = save_application_docs(job, result, job_description, settings)

        return jsonify({
            'ok':          True,
            'cover_letter': result['cover_letter'],
            'resume':       result['resume'],
            'sections':     result.get('sections', {}),
            'contact':      result.get('contact', {}),
            'feedback':     result.get('feedback', ''),
            'files':        file_paths,
            'usage':        result.get('usage', {}),
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
    model_key = settings.get('model', 'sonnet')
    body = request.get_json(silent=True) or {}

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
            'first_seen':   datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        }

        # Save to job list if not already there
        existing_ids = {j['id'] for j in data.get('jobs', [])}
        if job_id not in existing_ids:
            data.setdefault('jobs', []).insert(0, synthetic_job)
            save_data(data)

        result = generate_application_docs(synthetic_job, job_description, career_docs, model_key)
        file_paths = save_application_docs(synthetic_job, result, job_description, settings)

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

@app.route('/api/check', methods=['POST'])
def api_trigger_check():
    if _check_running:
        return jsonify({'message': 'Check already running'}), 409
    def run():
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Starting job check...")
        new, total, errors = check_all_companies()
        print(f"Done. {new} new jobs found ({total} total). {len(errors)} errors.\n")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'message': 'Check started'})


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


def _adzuna_request(query: str, page: int, max_days_old: int, results_per_page: int = 20) -> dict:
    """Call the Adzuna search API and return the raw parsed JSON response."""
    app_id, app_key = _adzuna_credentials()
    resp = requests.get(
        f'https://api.adzuna.com/v1/api/jobs/us/search/{page}',
        params={
            'app_id':           app_id,
            'app_key':          app_key,
            'results_per_page': results_per_page,
            'what_phrase':      query,
            'remote_only':      '1',
            'salary_min':       100000,
            'max_days_old':     max_days_old,
            'sort_by':          'date',
            'full_time':        '1',
        },
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
SWEEP_DEFAULT_LOOKBACK_HOURS = 24   # used on the very first sweep, when there's no prior timestamp

def sweep_adzuna_jobs() -> dict:
    """
    Paginate through Adzuna results for every configured title keyword, going back to
    the last successful sweep (or SWEEP_DEFAULT_LOOKBACK_HOURS on the first run), and add
    any new matching jobs to the tracked list. Only advances 'last_adzuna_sweep' if every
    keyword query completed without error, so a failed sweep is retried from the same
    starting point next time (safe — dedup makes re-fetching idempotent).
    """
    global _sweep_running

    with _sweep_lock:
        if _sweep_running:
            return {'added': 0, 'candidates_seen': 0, 'errors': [{'error': 'Sweep already in progress'}]}
        _sweep_running = True

    try:
        data = load_data()
        queries = list(data.get('title_keywords', []))
        now = datetime.now(timezone.utc)

        result = {
            'added': 0,
            'candidates_seen': 0,
            'queries': queries,
            'since': None,
            'until': now.isoformat(),
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

        days_needed = max(1, math.ceil((now - since_dt).total_seconds() / 86400))

        existing_urls = {j.get('url') for j in data.get('jobs', [])}
        new_jobs = []
        excluded = []

        for query in queries:
            query_lower = query.lower()
            page = 1
            while page <= SWEEP_MAX_PAGES_PER_QUERY:
                try:
                    raw = _adzuna_request(query, page, days_needed, results_per_page=SWEEP_RESULTS_PER_PAGE)
                except Exception as e:
                    result['errors'].append({'query': query, 'error': str(e)})
                    break

                raw_results = raw.get('results', [])
                if not raw_results:
                    break

                hit_boundary = False
                for r in raw_results:
                    created_dt = _parse_adzuna_created(r.get('created', ''))
                    if created_dt is not None and created_dt <= since_dt:
                        hit_boundary = True
                        break

                    result['candidates_seen'] += 1
                    title = r.get('title', '')

                    # Shape matches api_search_jobs' result objects so excluded entries can be
                    # reviewed and re-added via the same UI/route as manual search results.
                    candidate = {
                        'adzuna_id':   r.get('id', ''),
                        'title':       title,
                        'company':     r.get('company', {}).get('display_name', ''),
                        'location':    r.get('location', {}).get('display_name', ''),
                        'url':         r.get('redirect_url', ''),
                        'description': r.get('description', '')[:400],
                        'posted':      r.get('created', '')[:10],
                    }

                    if query_lower not in title.lower():
                        excluded.append({**candidate, 'reason': 'title_mismatch'})
                        continue

                    redirect_url = r.get('redirect_url', '')
                    resolved_url = _resolve_redirect_url(redirect_url)
                    if resolved_url in existing_urls or redirect_url in existing_urls:
                        excluded.append({**candidate, 'reason': 'duplicate'})
                        continue

                    job = {
                        'id':           str(uuid.uuid4()),
                        'title':        title,
                        'company_name': r.get('company', {}).get('display_name', ''),
                        'url':          resolved_url,
                        'location':     r.get('location', {}).get('display_name', ''),
                        'description':  r.get('description', '')[:400],
                        'source':       'adzuna',
                        'is_new':       True,
                        'is_applied':   False,
                        'is_remote':    True,  # Adzuna results are filtered with remote_only=1
                        'match_score':  0,
                        'posted':       r.get('created', '')[:10],  # date the job was posted, per Adzuna
                        'created_at':   now.isoformat(),            # when we added it to the tracked list
                    }
                    new_jobs.append(job)
                    existing_urls.add(resolved_url)
                    existing_urls.add(redirect_url)

                if hit_boundary or len(raw_results) < SWEEP_RESULTS_PER_PAGE:
                    break
                page += 1

        data = load_data()  # reload in case something else wrote to it while we were paginating/resolving
        data.setdefault('jobs', []).extend(new_jobs)
        result['added'] = len(new_jobs)
        result['excluded'] = excluded
        result['excluded_by_reason'] = {
            reason: sum(1 for e in excluded if e['reason'] == reason)
            for reason in sorted({e['reason'] for e in excluded})
        }

        if not result['errors']:
            data['last_adzuna_sweep'] = now.isoformat()

        data['last_adzuna_sweep_result'] = {**result, 'at': now.isoformat()}
        save_data(data)

        return result
    finally:
        with _sweep_lock:
            _sweep_running = False


@app.route('/api/sweep-adzuna', methods=['POST'])
def api_trigger_sweep():
    if _sweep_running:
        return jsonify({'message': 'Sweep already running'}), 409
    try:
        _adzuna_credentials()
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    def run():
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Starting Adzuna sweep...")
        res = sweep_adzuna_jobs()
        print(f"Sweep done. {res['added']} new jobs added ({res['candidates_seen']} candidates seen). "
              f"{len(res['errors'])} error(s).\n")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'message': 'Sweep started'})


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
    job = {
        'id':           str(uuid.uuid4()),
        'title':        title,
        'company_name': body.get('company', ''),
        'url':          resolved_url,
        'location':     location,
        'description':  body.get('description', ''),
        'source':       'adzuna',
        'is_new':       True,
        'is_applied':   False,
        'is_remote':    True,  # Adzuna results are filtered with remote_only=1
        'match_score':  0,
        'posted':       body.get('posted', ''),  # date the job was posted, per Adzuna
        'created_at':   datetime.now(timezone.utc).isoformat(),  # when we added it to the tracked list
    }
    data['jobs'].append(job)
    save_data(data)
    return jsonify({'ok': True, 'job': job})


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Job Checker')
    parser.add_argument('--check', action='store_true', help='Run check and exit (for cron/scheduled tasks)')
    parser.add_argument('--port', type=int, default=5000, help='Port for web server (default 5000)')
    args = parser.parse_args()

    if args.check:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Running scheduled job check...")
        new, total, errors = check_all_companies()
        print(f"Complete. {new} new jobs found ({total} total). {len(errors)} error(s).")
        if errors:
            for e in errors:
                print(f"  ✗ {e['company']}: {e['error']}")
    else:
        print(f"Job Checker starting at http://localhost:{args.port}")
        print("Press Ctrl+C to stop.")
        app.run(host='127.0.0.1', port=args.port, debug=False)
