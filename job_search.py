#!/usr/bin/env python3
"""
job_alerts.py

One-shot job collector that:
- Does NOT filter by post date (includes older postings).
- Only includes positions that indicate a US location (onsite/hybrid). Remote is included
  only when the posting explicitly indicates US-based / United States / USA.
- Applies strict role matching by default (loose mode optional via LOOSE=1 env).
- Searches multiple reputable job boards and uses site-limited web search (DuckDuckGo HTML).
- Deduplicates results and emails a CSV + plain-text summary.

Environment variables required:
- EMAIL_ADDRESS
- EMAIL_PASSWORD

Optional:
- RECIPIENT (defaults to EMAIL_ADDRESS)
- SMTP_HOST (default: smtp.gmail.com)
- SMTP_PORT (default: 465)
- LOOSE (set to "1" to allow a looser keyword match)
- MAX_RESULTS_PER_QUERY (default: 30)
- DEBUG (set to "1" for verbose logs)

Notes:
- Scraping is best-effort; some sites (LinkedIn/Glassdoor) may block or require JS.
- This script is safe to run in GitHub Actions (one-shot). It exits 0 even if no results.
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
from email.message import EmailMessage
import smtplib
import dateparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- Config & Logging ----------------
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_PASSWORD")
RECIPIENT = os.environ.get("EMAIL_ADDRESS", EMAIL)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
LOOSE = os.environ.get("LOOSE", "") == "1"
DEBUG = os.environ.get("DEBUG", "") == "1"
MAX_RESULTS_PER_QUERY = int(os.environ.get("MAX_RESULTS_PER_QUERY", "30"))

if not EMAIL or not PASSWORD:
    print("Missing EMAIL_ADDRESS or EMAIL_PASSWORD in environment. Exiting.")
    sys.exit(0)

log_level = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("job_alerts")

# ---------------- Globals ----------------
USER_AGENT = {"User-Agent": "Mozilla/5.0 (compatible; JobAlerts/1.0)"}
session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
session.mount("https://", HTTPAdapter(max_retries=retries))
session.headers.update(USER_AGENT)

DUCKDUCKGO_HTML = "https://html.duckduckgo.com/html/"

REPUTABLE_SITES = [
    "lever.co", "greenhouse.io", "workday.com", "indeed.com", "linkedin.com",
    "angel.co", "glassdoor.com", "remoteok.com", "weworkremotely.com", "remote.co", "wellfound.com"
]

KEYWORDS_STRICT = [
    "software engineer", "software developer", "full stack", "full-stack",
    "backend engineer", "frontend engineer", "backend developer", "frontend developer",
    "swe", "software eng"
]
KEYWORDS_LOOSE = [
    "engineer", "developer", "software", "fullstack", "full-stack", "swe"
]

US_STATE_ABBRS = set("""
AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT
NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY
""".split())
US_INDICATORS = ["united states", "usa", "us", "u.s.", "u.s.a.", "united states of america", "america"]

# ---------------- Helpers ----------------
def safe_get(url, timeout=12):
    try:
        return session.get(url, timeout=timeout)
    except Exception as e:
        logger.debug("HTTP GET failed for %s : %s", url, e)
        return None

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

def matches_role(text):
    if not text:
        return False
    t = text.lower()
    keywords = KEYWORDS_LOOSE if LOOSE else KEYWORDS_STRICT
    for kw in keywords:
        if kw in t:
            return True
    return False

def looks_like_us_location(text):
    """
    Return True if text indicates a US location or explicit 'US-based' mention.
    Remote is accepted only if explicitly US-based.
    """
    if not text:
        return False
    t = text.lower()
    # explicit US mentions
    if any(ind in t for ind in US_INDICATORS):
        return True
    # explicit US-based remote
    if "us-based" in t or "us based" in t or "u.s.-based" in t or "u.s. based" in t:
        return True
    # remote without US mention -> reject (we want US positions)
    if "remote" in t and not any(ind in t for ind in US_INDICATORS):
        return False
    # hybrid / onsite indicators
    if "hybrid" in t or "on-site" in t or "onsite" in t or "office" in t:
        # accept if a state abbreviation or full state name present nearby
        # look for "City, ST" patterns
        m = re.search(r",\s*([A-Za-z]{2})\b", text)
        if m and m.group(1).upper() in US_STATE_ABBRS:
            return True
        # check common full state names
        for st in ("california","new york","texas","washington","florida","illinois","massachusetts"):
            if st in t:
                return True
        # check if contains country name
        if any(ind in t for ind in US_INDICATORS):
            return True
    # City, ST patterns anywhere
    m = re.search(r",\s*([A-Za-z]{2})\b", text)
    if m and m.group(1).upper() in US_STATE_ABBRS:
        return True
    # fallback: contains common US city names (limited)
    for city in ("san francisco", "new york", "seattle", "austin", "chicago", "boston", "los angeles"):
        if city in t:
            return True
    return False

# ---------------- Search (DuckDuckGo HTML) ----------------
def ddg_search(query, max_results=MAX_RESULTS_PER_QUERY):
    logger.debug("DDG query: %s", query)
    try:
        resp = session.post(DUCKDUCKGO_HTML, data={"q": query}, timeout=12)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("DDG request failed: %s", e)
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for a in soup.select("a.result__a")[:max_results]:
        href = a.get("href")
        title = a.get_text(" ", strip=True)
        if href and href.startswith("http"):
            href = unquote(href)
            results.append((title, href))
    if not results:
        # fallback selector
        for a in soup.select("a[href]")[:max_results]:
            href = a.get("href")
            title = a.get_text(" ", strip=True)
            if href and href.startswith("http") and 'duckduckgo.com' not in href:
                results.append((title, href))
    logger.debug("DDG returned %d results", len(results))
    return results

def build_queries():
    site_filter = " OR ".join(f"site:{s}" for s in REPUTABLE_SITES)
    queries = []
    keywords = KEYWORDS_LOOSE if LOOSE else KEYWORDS_STRICT
    for kw in keywords:
        q = f'{kw} ({site_filter}) "United States" OR "US" OR "USA"'
        queries.append(q)
    return queries

# ---------------- Generic page extraction ----------------
def extract_job_from_page(url):
    r = safe_get(url)
    if not r or r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    # Title heuristics
    title = None
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        meta_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "title"})
        if meta_title and meta_title.get("content"):
            title = meta_title["content"].strip()
    if not title and soup.title:
        title = soup.title.string.strip()
    if not title:
        return None
    # Company heuristics
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
    # Location heuristics: meta, labels, body patterns
    location = None
    meta_loc = soup.find("meta", attrs={"name": "jobLocation"}) or soup.find("meta", attrs={"property":"jobLocation"})
    if meta_loc and meta_loc.get("content"):
        location = meta_loc["content"].strip()
    if not location:
        # look for 'Location' label text
        for txt in soup.find_all(text=re.compile(r"Location", re.I)):
            parent = txt.parent
            if parent:
                nearby = parent.get_text(" ", strip=True)
                nearby = re.sub(r"(?i)location[:\s]*", "", nearby).strip()
                if nearby:
                    location = nearby
                    break
    if not location:
        body = soup.get_text(" ", strip=True)
        # City, ST
        m = re.search(r"\b[A-Za-z .'-]+,\s*([A-Za-z]{2})\b", body)
        if m:
            # capture fragment for clarity
            start = max(0, m.start()-40)
            location = body[start:m.end()+40].split("\n")[0].strip()
    if not location and re.search(r"\bremote\b", r.text, re.I):
        location = "Remote"
    if not location:
        location = "Unknown"
    # Apply link: use canonical link or page url
    apply_link = url
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        apply_link = canonical["href"]
    # No date filtering per request (we include older postings)
    # Role matching
    combined = " ".join([title, company]).lower()
    if not matches_role(combined):
        return None
    # Location must be US (or Remote with explicit US mention)
    if not looks_like_us_location(location):
        # attempt to check body for US indicators (maybe location parsing missed)
        if not looks_like_us_location(soup.get_text(" ", strip=True)):
            return None
    return {
        "Job Title": title.strip(),
        "Company": company.strip(),
        "Location": location.strip(),
        "Apply Link": apply_link,
        "Source URL": url,
        "Scraped At": datetime.now().isoformat()
    }

# ---------------- Source-specific fetchers (best-effort) ----------------
def fetch_remoteok():
    logger.info("Fetching RemoteOK API...")
    out = []
    try:
        r = safe_get("https://remoteok.com/api")
        if not r or r.status_code != 200:
            return out
        data = r.json()
        for item in data:
            if not isinstance(item, dict):
                continue
            title = item.get("position") or item.get("title") or ""
            company = item.get("company") or ""
            link = item.get("url") or item.get("apply_url") or ""
            if link and link.startswith("/"):
                link = urljoin("https://remoteok.com", link)
            combined = " ".join([title, company, " ".join(item.get("tags", []) if isinstance(item.get("tags", []), list) else [])])
            if not matches_role(combined):
                continue
            # RemoteOK location can be varied; include only if US or US-based present
            loc = item.get("location") or item.get("tags") or ""
            loc_text = loc if isinstance(loc, str) else " ".join(loc) if isinstance(loc, list) else ""
            if not looks_like_us_location(str(loc_text)):
                # check description for US mention
                if not looks_like_us_location(item.get("description", "") or item.get("notes", "") or ""):
                    continue
            out.append({
                "Job Title": title.strip(),
                "Company": company.strip() or domain_of(link),
                "Location": loc_text or "Unknown",
                "Apply Link": link or item.get("link") or "",
                "Source URL": link or "",
                "Scraped At": datetime.now().isoformat()
            })
    except Exception as e:
        logger.debug("RemoteOK fetch error: %s", e)
    logger.info("RemoteOK items: %d", len(out))
    return out

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
        anchors = soup.select("section.jobs a") or soup.select("a[href*='/remote-jobs/']")
        links = []
        for a in anchors:
            href = a.get("href")
            if not href:
                continue
            job_url = urljoin(base, href) if href.startswith("/") else href
            links.append(job_url)
        for job_url in sorted(set(links))[:150]:
            try:
                # extract generically (no date filter)
                job = extract_job_from_page(job_url)
                if job:
                    out.append(job)
                time.sleep(0.25)
            except Exception:
                continue
    except Exception as e:
        logger.debug("WeWorkRemotely error: %s", e)
    logger.info("WeWorkRemotely items: %d", len(out))
    return out

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
        cards = soup.select("a.job-listing") + soup.select("article.job_listing a")
        links = []
        for a in cards:
            href = a.get("href")
            if href:
                links.append(urljoin(base, href))
        for job_url in sorted(set(links))[:120]:
            try:
                job = extract_job_from_page(job_url)
                if job:
                    out.append(job)
                time.sleep(0.25)
            except Exception:
                continue
    except Exception as e:
        logger.debug("Remote.co error: %s", e)
    logger.info("Remote.co items: %d", len(out))
    return out

# ---------------- Collector ----------------
def collect_jobs():
    results = []
    seen = set()
    # 1) Source-specific endpoints
    for fetcher in (fetch_remoteok, fetch_weworkremotely, fetch_remote_co):
        try:
            items = fetcher()
            for it in items:
                key = (it.get("Job Title"), it.get("Company"), it.get("Apply Link") or it.get("Source URL"))
                if key in seen:
                    continue
                seen.add(key)
                results.append(it)
        except Exception as e:
            logger.debug("Fetcher %s failed: %s", fetcher.__name__, e)
    # 2) DuckDuckGo site-limited searches for broader coverage (Indeed/LinkedIn/Workday/Lever/Greenhouse)
    queries = build_queries()
    for q in queries:
        try:
            items = ddg_search(q, MAX_RESULTS_PER_QUERY)
            time.sleep(0.6)
            for title, href in items:
                if not href.startswith("http"):
                    continue
                # prefer reputable domains but allow others
                job = extract_job_from_page(href)
                if not job:
                    continue
                key = (job.get("Job Title"), job.get("Company"), job.get("Apply Link") or job.get("Source URL"))
                if key in seen:
                    continue
                seen.add(key)
                results.append(job)
                time.sleep(0.3)
        except Exception as e:
            logger.debug("Search query failed: %s", e)
    logger.info("Total jobs collected: %d", len(results))
    return results

# ---------------- Email ----------------
def send_email(jobs):
    today = datetime.now().strftime("%Y-%m-%d")
    msg = EmailMessage()
    msg["Subject"] = f"US Software Jobs (relaxed date filter) - {today}"
    msg["From"] = EMAIL
    msg["To"] = RECIPIENT

    if not jobs:
        msg.set_content("No jobs found matching the US-location + role constraints.")
    else:
        lines = []
        for j in jobs:
            lines.append(f"{j.get('Job Title')} | {j.get('Company')} | {j.get('Location')} | {j.get('Apply Link') or j.get('Source URL')}")
        msg.set_content("Jobs (no post-date filtering) matching role + US location:\n\n" + "\n".join(lines))
        # CSV attach
        csv_buf = io.StringIO()
        writer = csv.DictWriter(csv_buf, fieldnames=["Job Title", "Company", "Location", "Apply Link", "Source URL", "Scraped At"])
        writer.writeheader()
        for j in jobs:
            writer.writerow({
                "Job Title": j.get("Job Title",""),
                "Company": j.get("Company",""),
                "Location": j.get("Location",""),
                "Apply Link": j.get("Apply Link",""),
                "Source URL": j.get("Source URL",""),
                "Scraped At": j.get("Scraped At","")
            })
        msg.add_attachment(csv_buf.getvalue().encode("utf-8"), maintype="text", subtype="csv", filename=f"jobs_{today}.csv")

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(EMAIL, PASSWORD)
            s.send_message(msg)
        logger.info("Email sent to %s with %d jobs", RECIPIENT, len(jobs))
    except Exception as e:
        logger.error("Failed to send email: %s", e)

# ---------------- Entrypoint ----------------
def main():
    logger.info("Starting collection (no date filter, US locations only). LOOSE=%s DEBUG=%s", LOOSE, DEBUG)
    try:
        jobs = collect_jobs()
        send_email(jobs)
    except Exception as e:
        logger.exception("Unhandled error during run: %s", e)
    logger.info("Run complete.")
    return 0

if __name__ == "__main__":
    main()
