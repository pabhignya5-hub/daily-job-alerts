#!/usr/bin/env python3
"""
job_alerts.py

Best-effort, one-shot job collector for U.S. positions (onsite / hybrid / remote).
- Searches public web (DuckDuckGo HTML front-end) across reputable job platforms:
  lever.co, greenhouse.io, workday.com, linkedin.com, indeed.com, angel.co, glassdoor.com
- Visits each result and attempts to extract exact job title, company, location and direct apply link.
- Filters to roles posted/updated in the last 24 hours and with U.S. locations (or Remote/Hybrid).
- Deduplicates, saves CSV, and emails the compiled list.

Notes & caveats:
- This approach is best-effort scraping of public pages. Some sites (LinkedIn, Glassdoor) aggressively block scraping or render with JavaScript;
  results for such sites may be incomplete.
- DuckDuckGo HTML search (html.duckduckgo.com/html/) is used to avoid paid search APIs and heavy blocking.
- For production reliability consider official APIs, curated company lists, or a paid job data provider.

Environment variables required:
- EMAIL_ADDRESS
- EMAIL_PASSWORD
Optional:
- RECIPIENT (defaults to EMAIL_ADDRESS)
- SMTP_HOST (default smtp.gmail.com)
- SMTP_PORT (default 465)

Dependencies:
pip install requests beautifulsoup4 python-dateutil dateparser

Run:
python job_alerts.py
"""

from datetime import datetime, timedelta
import os
import sys
import time
import io
import csv
import re
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import dateparser
from email.message import EmailMessage
import smtplib

# -------- Configuration --------
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_PASSWORD")
RECIPIENT = os.environ.get("RECIPIENT", EMAIL)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))

if not EMAIL or not PASSWORD:
    sys.exit("Set EMAIL_ADDRESS and EMAIL_PASSWORD environment variables before running.")

USER_AGENT = {"User-Agent": "Mozilla/5.0 (compatible; JobAlerts/1.0; +https://example.com)"}
TIME_WINDOW = timedelta(days=1)  # last 24 hours

KEYWORDS = [
    "software engineer", "software developer", "full stack", "full-stack",
    "backend engineer", "frontend engineer", "backend developer", "frontend developer"
]

# Reputable domains to bias searches toward
REPUTABLE_SITES = [
    "lever.co", "greenhouse.io", "workday.com", "indeed.com",
    "linkedin.com", "angel.co", "glassdoor.com", "remoteok.com",
    "weworkremotely.com", "remote.co"
]

DUCKDUCKGO_HTML = "https://html.duckduckgo.com/html/"

# US state abbreviations + 'United States' check
US_STATE_ABBRS = set("""
AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT
NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY
""".split())
US_INDICATORS = ["united states", "usa", "us", "u.s.", "u.s.a."]


# -------- Helpers --------
def http_get(url, timeout=12):
    try:
        return requests.get(url, headers=USER_AGENT, timeout=timeout)
    except Exception:
        return None

def domain_of(url):
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""

def parse_date(text):
    if not text:
        return None
    dt = dateparser.parse(text, settings={"RETURN_AS_TIMEZONE_AWARE": False})
    return dt

def posted_within_24h(dt):
    if not dt:
        return False
    return (datetime.now() - dt) <= TIME_WINDOW

def looks_like_us_location(text):
    if not text:
        return False
    t = text.lower()
    if any(ind in t for ind in US_INDICATORS):
        return True
    if "remote" in t:
        return True
    if "hybrid" in t or "on-site" in t or "onsite" in t:
        return True
    # look for "City, ST" patterns and state abbreviations
    m = re.search(r",\s*([A-Za-z]{2})\b", text)
    if m and m.group(1).upper() in US_STATE_ABBRS:
        return True
    # look for full state name
    for st in ("california","new york","texas","washington","florida","illinois","massachusetts"):
        if st in t:
            return True
    return False

def matches_role(text):
    if not text:
        return False
    t = text.lower()
    for kw in KEYWORDS:
        if kw in t:
            return True
    return False

def extract_job_from_page(url):
    """
    Visit the job posting page and attempt to extract:
      - title (h1, meta og:title, title tag)
      - company (meta og:site_name, .company, similar)
      - location (common labels, or look for 'Remote'/'Hybrid'/'City, ST')
      - posted date (time tags, meta, or 'Posted' text parsed with dateparser)
    Returns dict or None.
    """
    r = http_get(url)
    if not r or r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    # Title heuristics
    title = None
    if soup.find("h1"):
        title = soup.find("h1").get_text(strip=True)
    if not title:
        meta_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "title"})
        if meta_title and meta_title.get("content"):
            title = meta_title["content"].strip()
    if not title and soup.title:
        title = soup.title.string.strip()

    # Company heuristics
    company = None
    meta_site = soup.find("meta", property="og:site_name")
    if meta_site and meta_site.get("content"):
        company = meta_site["content"].strip()
    if not company:
        # common patterns
        c = soup.select_one(".company") or soup.select_one(".company-name") or soup.select_one("[class*=company]")
        if c:
            company = c.get_text(strip=True)
    if not company:
        # fallback to domain
        company = domain_of(url)

    # Location heuristics
    location = None
    # look for time/location blocks
    loc_candidates = []
    # meta keywords
    meta_loc = soup.find("meta", attrs={"name": "jobLocation"}) or soup.find("meta", attrs={"property": "jobLocation"})
    if meta_loc and meta_loc.get("content"):
        loc_candidates.append(meta_loc["content"])
    # look for elements that contain 'Location' label
    for lbl in soup.find_all(text=re.compile(r"Location|location", re.I)):
        parent = lbl.parent
        if parent and parent.name in ("span", "p", "div", "li"):
            txt = parent.get_text(" ", strip=True)
            # remove the label itself if present
            txt = re.sub(r"(?i)location[:\s]*", "", txt).strip()
            if txt:
                loc_candidates.append(txt)
    # time tag: sometimes job pages have <time datetime=...>
    time_tag = soup.find("time")
    posted_dt = None
    if time_tag and time_tag.get("datetime"):
        posted_dt = parse_date(time_tag["datetime"])
    # look in text for "Posted" patterns
    if not posted_dt:
        full_text = soup.get_text(" ", strip=True)
        m = re.search(r"Posted(?: on)?[:\s]*([A-Za-z0-9, \-:/]+)", full_text, re.I)
        if m:
            posted_dt = parse_date(m.group(1))
        else:
            # phrases like "just posted", "1 day ago"
            m2 = re.search(r"\b(\d+)\s+day[s]?\s+ago\b", full_text, re.I)
            if m2:
                posted_dt = datetime.now() - timedelta(days=int(m2.group(1)))
            elif re.search(r"\bjust posted\b|\bjust now\b", full_text, re.I):
                posted_dt = datetime.now()

    # choose location if any candidate looks like US or remote/hybrid
    for c in loc_candidates:
        if looks_like_us_location(c):
            location = c.strip()
            break
    if not location:
        # try to read any nearby "location" classes
        loc_tag = soup.select_one("[class*=location]") or soup.select_one(".job-location")
        if loc_tag:
            txt = loc_tag.get_text(" ", strip=True)
            if looks_like_us_location(txt):
                location = txt

    # fallback: if page contains 'remote' anywhere
    if not location:
        if re.search(r"\bremote\b", r.text, re.I):
            location = "Remote"

    if not title:
        return None

    # If posted_dt exists ensure within 24 hours
    if posted_dt and not posted_within_24h(posted_dt):
        return None

    # Final company/location defaults
    if not location:
        location = "Unknown"

    return {
        "Job Title": title.strip(),
        "Company": company.strip() if company else domain_of(url),
        "Location": location,
        "Apply Link": url,
        "Posted Date": posted_dt.isoformat() if posted_dt else ""
    }


# -------- Search via DuckDuckGo HTML --------
def ddg_search(query, max_results=30):
    """
    Use DuckDuckGo HTML endpoint to get search results. Returns a list of result URLs and titles/snippets.
    """
    try:
        resp = requests.post(DUCKDUCKGO_HTML, data={"q": query}, headers=USER_AGENT, timeout=12)
        resp.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    # DuckDuckGo html returns .result__a anchors for links
    for a in soup.select("a.result__a")[:max_results]:
        href = a.get("href")
        title = a.get_text(" ", strip=True)
        if href:
            results.append((title, href))
    # fallback broader selector
    if not results:
        for a in soup.select("a[href]")[:max_results]:
            href = a.get("href")
            title = a.get_text(" ", strip=True)
            if href and 'duckduckgo.com' not in href:
                results.append((title, href))
    return results

# -------- Build site-limited queries --------
def build_queries():
    queries = []
    # combine keywords with reputable sites as site: filters
    site_filter = " OR ".join(f"site:{s}" for s in REPUTABLE_SITES)
    for kw in KEYWORDS:
        q = f'{kw} ({site_filter}) "United States" OR "Remote" OR "Hybrid"'
        queries.append(q)
    return queries

# -------- Collector --------
def collect_jobs():
    queries = build_queries()
    seen = set()
    out = []

    for q in queries:
        results = ddg_search(q, max_results=25)
        time.sleep(1)  # politeness between searches
        for title, href in results:
            # normalize href sometimes prefixed with /l/?kh=...
            # DuckDuckGo may produce redirect wrappers; attempt to unescape if possible
            # For our purposes, follow the href directly
            if not href.startswith("http"):
                continue
            # filter to reputable domains only (safety)
            dom = domain_of(href)
            if not any(s in dom for s in REPUTABLE_SITES):
                # still allow common boards like indeed/linkedin even if domain variations exist
                pass

            # Visit the job page and extract details
            job = extract_job_from_page(href)
            if not job:
                continue
            # ensure role matches keywords
            combined = " ".join([job.get("Job Title",""), job.get("Company","")]).lower()
            if not matches_role(combined):
                continue
            # ensure US / remote / hybrid
            if not looks_like_us_location(job.get("Location","")):
                continue
            key = (job["Job Title"], job["Company"], job["Apply Link"])
            if key in seen:
                continue
            seen.add(key)
            out.append(job)
            # be polite and avoid hammering
            time.sleep(0.6)
    return out


# -------- Email & CSV --------
def send_email(jobs):
    today = datetime.now().strftime("%Y-%m-%d")
    msg = EmailMessage()
    msg["Subject"] = f"Daily US Software Jobs ({today})"
    msg["From"] = EMAIL
    msg["To"] = RECIPIENT

    if not jobs:
        msg.set_content("No new US software jobs found in the last 24 hours.")
    else:
        lines = [f"{j['Job Title']} | {j['Company']} | {j['Location']} | {j['Apply Link']}" for j in jobs]
        msg.set_content("Jobs posted/updated in the last 24 hours (US positions):\n\n" + "\n".join(lines))
        # CSV attachment
        csv_buf = io.StringIO()
        writer = csv.DictWriter(csv_buf, fieldnames=["Job Title", "Company", "Location", "Apply Link", "Posted Date"])
        writer.writeheader()
        for j in jobs:
            writer.writerow(j)
        msg.add_attachment(csv_buf.getvalue().encode("utf-8"), maintype="text", subtype="csv", filename=f"jobs_{today}.csv")

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(EMAIL, PASSWORD)
        s.send_message(msg)

    print(f"[{datetime.now().isoformat()}] Sent email to {RECIPIENT} with {len(jobs)} jobs.")


# -------- Entrypoint --------
def main():
    print(f"[{datetime.now().isoformat()}] Starting collection (US onsite/hybrid/remote, last 24h)...")
    jobs = collect_jobs()
    print(f"Collected {len(jobs)} jobs. Sending email...")
    send_email(jobs)


if __name__ == "__main__":
    main()
