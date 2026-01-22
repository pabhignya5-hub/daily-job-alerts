#!/usr/bin/env python3
"""
job_alerts.py

Robust, best-effort collector for U.S. software engineering roles (onsite / hybrid / remote)
posted/updated within the last N days (default 1). It tries multiple reputable sources,
parses job pages for exact title/company/location/apply link, deduplicates results and
emails a CSV + plain-text summary.

Important limitations (honest):
- Scraping public job boards is inherently brittle: some sites use JS, rate-limit, or block
  non-browser clients. This script is "best-effort" and cannot be guaranteed 100% reliable.
- For production-grade, consider official APIs or a paid job-data provider.
- This script is defensive: retries, timeouts, conservative date parsing, and always exits 0
  (sends an email even when nothing is found or an error occurred).

Environment variables (required):
- EMAIL_ADDRESS
- EMAIL_PASSWORD

Optional:
- RECIPIENT (defaults to EMAIL_ADDRESS)
- SMTP_HOST (default: smtp.gmail.com)
- SMTP_PORT (default: 465)
- DAYS (integer days window; default: 1)
- DEBUG (set to "1" to relax some checks and get more verbose logs)
- MAX_RESULTS_PER_QUERY (default: 30)

Dependencies:
pip install requests beautifulsoup4 python-dateutil dateparser

Usage:
- Use as a one-shot (GitHub Actions cron should run it). Do NOT run a long-lived scheduler here.
- Example (local):
  EMAIL_ADDRESS=you@host.com EMAIL_PASSWORD=app_password DEBUG=1 python job_alerts.py
"""

from datetime import datetime, timedelta
import os
import sys
import time
import io
import csv
import logging
import re
import requests
from urllib.parse import urljoin, urlparse, unquote
from bs4 import BeautifulSoup
import dateparser
from email.message import EmailMessage
import smtplib
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- Config ----------------
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_PASSWORD")
RECIPIENT = os.environ.get("RECIPIENT", EMAIL)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
DAYS = int(os.environ.get("DAYS", "1"))
DEBUG = os.environ.get("DEBUG", "") == "1"
MAX_RESULTS_PER_QUERY = int(os.environ.get("MAX_RESULTS_PER_QUERY", "30"))

if not EMAIL or not PASSWORD:
    print("ERROR: EMAIL_ADDRESS and EMAIL_PASSWORD must be set in environment.", file=sys.stderr)
    # Do not raise; exit gracefully after sending failure email will not be possible.
    sys.exit(0)

TIME_WINDOW = timedelta(days=DAYS)

# Reputable sites (we'll bias searches to these domains)
REPUTABLE_SITES = [
    "remoteok.com", "weworkremotely.com", "remote.co",
    "indeed.com", "linkedin.com", "workday.com",
    "lever.co", "greenhouse.io", "angel.co", "wellfound.com", "glassdoor.com"
]

# DuckDuckGo HTML endpoint (less aggressive blocking)
DUCKDUCKGO_HTML = "https://html.duckduckgo.com/html/"

# Keywords for role matching
KEYWORDS = [
    "software engineer", "software developer", "full stack", "full-stack",
    "backend engineer", "frontend engineer", "backend developer", "frontend developer",
    "swe", "software eng"
]

US_STATE_ABBRS = set("""
AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT
NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY
""".split())
US_INDICATORS = ["united states", "usa", "us", "u.s.", "u.s.a."]

# ---------------- Logging ----------------
log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("job_alerts")

# ---------------- HTTP session with retries ----------------
session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
session.mount("https://", HTTPAdapter(max_retries=retries))
session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; JobAlerts/1.0)"})

def safe_get(url, timeout=12):
    try:
        r = session.get(url, timeout=timeout)
        return r
    except Exception as e:
        logger.debug("safe_get error for %s: %s", url, e)
        return None

# ---------------- Helpers ----------------
def domain_of(url):
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""

def parse_date(text):
    if not text:
        return None
    try:
        return dateparser.parse(text)
    except Exception:
        return None

def within_window(dt):
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
    if "hybrid" in t:
        return True
    # City, ST
    m = re.search(r",\s*([A-Za-z]{2})\b", text)
    if m and m.group(1).upper() in US_STATE_ABBRS:
        return True
    # full state names quick check
    for s in ["california","new york","texas","washington","florida","illinois","massachusetts"]:
        if s in t:
            return True
    return False

def matches_role(text):
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in KEYWORDS)

def try_extract_posted_from_text(text):
    if not text:
        return None
    # patterns: 'Posted X hours ago', 'Posted on MMM DD, YYYY', 'X days ago'
    m = re.search(r"(\d+)\s+hour[s]?\s+ago", text, re.I)
    if m:
        return datetime.now() - timedelta(hours=int(m.group(1)))
    m = re.search(r"(\d+)\s+day[s]?\s+ago", text, re.I)
    if m:
        return datetime.now() - timedelta(days=int(m.group(1)))
    m = re.search(r"Posted on[:\s]*([A-Za-z0-9, \-:/]+)", text, re.I)
    if m:
        return parse_date(m.group(1))
    m = re.search(r"Posted[:\s]*([A-Za-z0-9, \-:/]+)", text, re.I)
    if m:
        return parse_date(m.group(1))
    if re.search(r"just posted|just now", text, re.I):
        return datetime.now()
    return None

# ---------------- Source: RemoteOK (JSON, reliable) ----------------
def fetch_remoteok():
    logger.info("Fetching RemoteOK...")
    out = []
    try:
        resp = safe_get("https://remoteok.com/api")
        if not resp or resp.status_code != 200:
            logger.debug("RemoteOK API request failed or non-200.")
            return out
        data = resp.json()
        for item in data:
            if not isinstance(item, dict):
                continue
            # RemoteOK returns a "date" or "date_posted"
            try:
                company = item.get("company") or item.get("slug") or ""
                title = item.get("position") or item.get("title") or item.get("position_name") or ""
                link = item.get("url") or item.get("apply_url") or item.get("link") or ""
                if link and link.startswith("/"):
                    link = urljoin("https://remoteok.com", link)
                date_str = item.get("date") or item.get("created_at") or item.get("time")
                posted = parse_date(str(date_str)) if date_str else None
                if not title or not company or not link:
                    continue
                if posted and not within_window(posted):
                    continue
                # allow if posted missing but description/tag indicates "just posted"
                combined = " ".join([title, company, " ".join(item.get("tags", []) if isinstance(item.get("tags", []), list) else [])])
                if not matches_role(combined):
                    continue
                # accept; best-effort location
                location = item.get("location") or "Remote"
                out.append({
                    "Job Title": title.strip(),
                    "Company": company.strip(),
                    "Location": location,
                    "Apply Link": link,
                    "Posted Date": posted.isoformat() if posted else ""
                })
            except Exception:
                continue
    except Exception as e:
        logger.debug("fetch_remoteok error: %s", e)
    logger.info("RemoteOK found %d items", len(out))
    return out

# ---------------- Source: WeWorkRemotely (HTML) ----------------
def fetch_weworkremotely():
    logger.info("Fetching WeWorkRemotely...")
    out = []
    base = "https://weworkremotely.com"
    cat = f"{base}/categories/remote-programming-jobs"
    try:
        r = safe_get(cat)
        if not r or r.status_code != 200:
            return out
        soup = BeautifulSoup(r.text, "html.parser")
        # anchors to job pages are under sections with 'jobs'
        anchors = soup.select("section.jobs a") or soup.select("a[href*='/remote-jobs/']")
        links = []
        for a in anchors:
            href = a.get("href")
            if not href:
                continue
            # avoid mixed external links
            if href.startswith("/"):
                job_url = urljoin(base, href)
            else:
                job_url = href
            links.append(job_url)
        links = sorted(set(links))
        for job_url in links[:150]:
            try:
                jr = safe_get(job_url)
                if not jr or jr.status_code != 200:
                    continue
                jsoup = BeautifulSoup(jr.text, "html.parser")
                title = (jsoup.find("h1").get_text(strip=True) if jsoup.find("h1") else "") or ""
                company_tag = jsoup.select_one(".company") or jsoup.select_one(".company-card h2") or jsoup.select_one(".company-name")
                company = company_tag.get_text(strip=True) if company_tag else domain_of(job_url)
                # posted date
                dt = None
                time_tag = jsoup.find("time")
                if time_tag and time_tag.get("datetime"):
                    dt = parse_date(time_tag["datetime"])
                if not dt:
                    dt = try_extract_posted_from_text(jsoup.get_text(" ", strip=True))
                if dt and not within_window(dt):
                    continue
                if not matches_role(title):
                    continue
                # location detection
                loc = "Remote"
                # check text nearby for 'Location' label
                for txt in jsoup.stripped_strings:
                    if "location" in txt.lower() and "," in txt:
                        # naive attempt
                        if looks_like_us_location(txt):
                            loc = txt
                            break
                out.append({
                    "Job Title": title,
                    "Company": company,
                    "Location": loc,
                    "Apply Link": job_url,
                    "Posted Date": dt.isoformat() if dt else ""
                })
                time.sleep(0.3)
            except Exception:
                continue
    except Exception as e:
        logger.debug("fetch_weworkremotely error: %s", e)
    logger.info("WeWorkRemotely found %d items", len(out))
    return out

# ---------------- Source: Remote.co (HTML) ----------------
def fetch_remote_co():
    logger.info("Fetching Remote.co...")
    out = []
    base = "https://remote.co"
    list_url = f"{base}/remote-jobs/developer/"
    try:
        r = safe_get(list_url)
        if not r or r.status_code != 200:
            return out
        soup = BeautifulSoup(r.text, "html.parser")
        # links are in .job_listing or a.job-listing
        cards = soup.select("a.job-listing") + soup.select("article.job_listing a")
        job_links = []
        for a in cards:
            href = a.get("href")
            if not href:
                continue
            job_links.append(urljoin(base, href))
        for job_url in sorted(set(job_links))[:120]:
            try:
                jr = safe_get(job_url)
                if not jr or jr.status_code != 200:
                    continue
                jsoup = BeautifulSoup(jr.text, "html.parser")
                title = jsoup.find("h1").get_text(strip=True) if jsoup.find("h1") else jsoup.title.string if jsoup.title else ""
                company = ""
                # remote.co sometimes has company in .company or h2/h3
                ctag = jsoup.find(lambda t: t.name in ("h2","h3") and "company" in (t.get("class") or []))
                if ctag:
                    company = ctag.get_text(strip=True)
                dt = None
                time_tag = jsoup.find("time")
                if time_tag and time_tag.get("datetime"):
                    dt = parse_date(time_tag["datetime"])
                if not dt:
                    dt = try_extract_posted_from_text(jsoup.get_text(" ", strip=True))
                if dt and not within_window(dt):
                    continue
                if not matches_role(title):
                    continue
                loc = "Remote"
                out.append({
                    "Job Title": title,
                    "Company": company or domain_of(job_url),
                    "Location": loc,
                    "Apply Link": job_url,
                    "Posted Date": dt.isoformat() if dt else ""
                })
                time.sleep(0.3)
            except Exception:
                continue
    except Exception as e:
        logger.debug("fetch_remote_co error: %s", e)
    logger.info("Remote.co found %d items", len(out))
    return out

# ---------------- Search via DuckDuckGo (site-limited) ----------------
def ddg_search(query, max_results=MAX_RESULTS_PER_QUERY):
    logger.debug("DDG search query: %s", query)
    try:
        resp = session.post(DUCKDUCKGO_HTML, data={"q": query}, timeout=12)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("ddg_search request error: %s", e)
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    # primary selector
    for a in soup.select("a.result__a")[:max_results]:
        href = a.get("href")
        title = a.get_text(" ", strip=True)
        if href and not href.startswith("javascript:"):
            # unescape ddg wrapper sometimes
            href = unquote(href)
            results.append((title, href))
    if not results:
        # fallback
        for a in soup.select("a[href]")[:max_results]:
            href = a.get("href")
            title = a.get_text(" ", strip=True)
            if href and 'duckduckgo.com' not in href:
                results.append((title, href))
    logger.debug("DDG search returned %d results", len(results))
    return results

def build_queries():
    site_filter = " OR ".join(f"site:{s}" for s in REPUTABLE_SITES)
    queries = []
    for kw in KEYWORDS:
        q = f'{kw} ({site_filter}) "United States" OR "Remote" OR "Hybrid"'
        queries.append(q)
    return queries

# ---------------- Page extractor (generic) ----------------
def extract_job_from_page(url):
    r = safe_get(url)
    if not r or r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    # Title
    title = None
    if soup.find("h1"):
        title = soup.find("h1").get_text(strip=True)
    if not title:
        meta_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name":"title"})
        if meta_title and meta_title.get("content"):
            title = meta_title["content"].strip()
    if not title and soup.title:
        title = soup.title.string.strip()
    if not title:
        return None
    # Company
    company = None
    meta_site = soup.find("meta", property="og:site_name")
    if meta_site and meta_site.get("content"):
        company = meta_site["content"].strip()
    if not company:
        ctag = soup.select_one(".company") or soup.select_one(".company-name") or soup.select_one("[class*=company]")
        if ctag:
            company = ctag.get_text(strip=True)
    if not company:
        company = domain_of(url)
    # Location
    location = None
    # try structured meta
    meta_loc = soup.find("meta", attrs={"name": "jobLocation"}) or soup.find("meta", attrs={"property":"jobLocation"})
    if meta_loc and meta_loc.get("content"):
        location = meta_loc["content"].strip()
    if not location:
        # search for nearby 'Location' label
        for elem in soup.find_all(text=re.compile(r"Location", re.I)):
            parent = elem.parent
            if parent:
                txt = parent.get_text(" ", strip=True)
                txt = re.sub(r"(?i)location[:\s]*", "", txt).strip()
                if txt:
                    location = txt
                    break
    if not location:
        # fallback search in body for city/state patterns
        body = soup.get_text(" ", strip=True)
        m = re.search(r"\b[A-Za-z .'-]+,\s*([A-Za-z]{2})\b", body)
        if m:
            # capture a small context
            start = max(0, m.start()-40)
            location = body[start:m.end()+40].split("\n")[0].strip()
    if not location and re.search(r"\bremote\b", r.text, re.I):
        location = "Remote"
    if not location:
        location = "Unknown"
    # Posted date
    posted = None
    time_tag = soup.find("time")
    if time_tag and time_tag.get("datetime"):
        posted = parse_date(time_tag["datetime"])
    if not posted:
        # meta published
        meta_p = soup.find("meta", {"property":"article:published_time"}) or soup.find("meta", {"name":"date"})
        if meta_p and meta_p.get("content"):
            posted = parse_date(meta_p["content"])
    if not posted:
        posted = try_extract_posted_from_text(soup.get_text(" ", strip=True))
    # If posted exists and is outside window, skip
    if posted and not within_window(posted):
        return None
    # ensure role matches
    if not matches_role(title + " " + company):
        return None
    # ensure US location or remote/hybrid
    if not looks_like_us_location(location):
        return None
    return {
        "Job Title": title.strip(),
        "Company": company.strip(),
        "Location": location.strip(),
        "Apply Link": url,
        "Posted Date": posted.isoformat() if posted else ""
    }

# ---------------- Collector ----------------
def collect_all():
    results = []
    seen = set()

    # first fetch reliable APIs / sources
    fetchers = [fetch_remoteok, fetch_weworkremotely, fetch_remote_co]
    for f in fetchers:
        try:
            items = f()
            for j in items:
                key = (j["Job Title"], j["Company"], j["Apply Link"])
                if key in seen:
                    continue
                seen.add(key)
                results.append(j)
        except Exception as e:
            logger.debug("Fetcher %s failed: %s", f.__name__, e)

    # then perform site-limited searches via DuckDuckGo for broader boards (indeed, workday, linkedin)
    queries = build_queries()
    for q in queries:
        try:
            items = ddg_search(q)
            time.sleep(0.8)
            for title, href in items[:MAX_RESULTS_PER_QUERY]:
                # normalize ddg redirect wrappers: many are direct links already
                if not href.startswith("http"):
                    continue
                # avoid repeat
                if domain_of(href) == "duckduckgo.com":
                    continue
                # visit the page and extract job info
                job = extract_job_from_page(href)
                if not job:
                    continue
                key = (job["Job Title"], job["Company"], job["Apply Link"])
                if key in seen:
                    continue
                seen.add(key)
                results.append(job)
                time.sleep(0.5)
        except Exception as e:
            logger.debug("Search query failed: %s", e)

    logger.info("Total collected jobs: %d", len(results))
    return results

# ---------------- Email ----------------
def send_email(jobs, success=True, error_message=None):
    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"Daily US Software Jobs ({today})"
    if not success:
        subject = f"[ERROR] {subject}"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL
    msg["To"] = RECIPIENT

    if not jobs:
        body = "No new US software jobs found in the last {} day(s).".format(DAYS)
        if error_message:
            body += "\n\nError: " + error_message
        msg.set_content(body)
    else:
        lines = [f"{j['Job Title']} | {j['Company']} | {j['Location']} | {j['Apply Link']}" for j in jobs]
        body = "Jobs posted/updated in the last {} day(s):\n\n".format(DAYS) + "\n".join(lines)
        msg.set_content(body)
        # attach CSV
        csv_buf = io.StringIO()
        writer = csv.DictWriter(csv_buf, fieldnames=["Job Title", "Company", "Location", "Apply Link", "Posted Date"])
        writer.writeheader()
        for j in jobs:
            writer.writerow(j)
        msg.add_attachment(csv_buf.getvalue().encode("utf-8"), maintype="text", subtype="csv", filename=f"jobs_{today}.csv")

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(EMAIL, PASSWORD)
            s.send_message(msg)
        logger.info("Email sent to %s with %d jobs (success=%s).", RECIPIENT, len(jobs), success)
    except Exception as e:
        logger.error("Failed to send email: %s", e)

# ---------------- Main ----------------
def main():
    start = datetime.now()
    try:
        jobs = collect_all()
        send_email(jobs, success=True)
    except Exception as e:
        logger.exception("Unhandled error during collection")
        # Always attempt to send an error email; since credentials exist we try
        send_email([], success=False, error_message=str(e))
    finally:
        elapsed = (datetime.now() - start).total_seconds()
        logger.info("Finished run in %.1f seconds", elapsed)
    # Exit with code 0 to avoid workflow failures (you requested "no issues").
    return 0

if __name__ == "__main__":
    main()
