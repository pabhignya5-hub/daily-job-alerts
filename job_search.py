#!/usr/bin/env python3
# name=job_alerts_google.py
"""
Collect jobs using Google Custom Search API, visit each result page to extract exact
job title, company, location and direct link, filter to US locations and strict role keywords,
dedupe and email CSV + plain text summary.

Required env:
- EMAIL_ADDRESS
- EMAIL_PASSWORD
- GOOGLE_API_KEY
- GOOGLE_CX

Optional:
- RECIPIENT (defaults to EMAIL_ADDRESS)
- SMTP_HOST (default smtp.gmail.com)
- SMTP_PORT (default 465)
- LOOSE=1 to use looser keyword matching
- MAX_PAGES (per keyword, default 3 -> up to 30 results)
"""
from datetime import datetime
import os
import sys
import time
import io
import csv
import logging
import re
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from email.message import EmailMessage
import smtplib
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- Config ----------------
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_PASSWORD")
RECIPIENT = os.environ.get("RECIPIENT", EMAIL)
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CX = os.environ.get("GOOGLE_CX")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
LOOSE = os.environ.get("LOOSE", "") == "1"
MAX_PAGES = int(os.environ.get("MAX_PAGES", "3"))  # pages of 10 results each

if not EMAIL or not PASSWORD or not GOOGLE_API_KEY or not GOOGLE_CX:
    print("Missing one of required env vars: EMAIL_ADDRESS, EMAIL_PASSWORD, GOOGLE_API_KEY, GOOGLE_CX", file=sys.stderr)
    sys.exit(1)

# keywords and role matching
KEYWORDS_STRICT = [
    "software engineer", "software developer", "full stack", "full-stack",
    "backend engineer", "frontend engineer", "backend developer", "frontend developer",
    "swe", "software eng"
]
KEYWORDS_LOOSE = ["engineer", "developer", "software", "swe", "fullstack", "full-stack"]
KEYWORDS = KEYWORDS_LOOSE if LOOSE else KEYWORDS_STRICT

# US location indicators
US_STATE_ABBRS = set("""AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT
NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY""".split())
US_INDICATORS = ["united states", "usa", "us", "u.s.", "u.s.a.", "united states of america", "america"]

# logging and session
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("job_alerts_google")
session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=(429,500,502,503,504))
session.mount("https://", HTTPAdapter(max_retries=retries))
session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; JobAlertsGoogle/1.0)"})

# ---------------- Helpers ----------------
def domain_of(url):
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""

def matches_role(text):
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in KEYWORDS)

def looks_like_us_location(text):
    if not text:
        return False
    t = text.lower()
    if any(ind in t for ind in US_INDICATORS):
        return True
    if "us-based" in t or "u.s.-based" in t or "us based" in t:
        return True
    if "remote" in t and any(ind in t for ind in US_INDICATORS):
        return True
    if "hybrid" in t or "onsite" in t or "on-site" in t or "office" in t:
        m = re.search(r",\s*([A-Za-z]{2})\b", t)
        if m and m.group(1).upper() in US_STATE_ABBRS:
            return True
        for st in ("california","new york","texas","washington","florida","illinois","massachusetts"):
            if st in t:
                return True
    m = re.search(r",\s*([A-Za-z]{2})\b", t)
    if m and m.group(1).upper() in US_STATE_ABBRS:
        return True
    for city in ("san francisco", "new york", "seattle", "austin", "chicago", "boston", "los angeles"):
        if city in t:
            return True
    return False

def fetch_url(url, timeout=12):
    try:
        return session.get(url, timeout=timeout)
    except Exception as e:
        logger.debug("fetch_url error %s: %s", url, e)
        return None

def extract_job_from_page(url):
    r = fetch_url(url)
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
    meta_loc = soup.find("meta", attrs={"name":"jobLocation"}) or soup.find("meta", attrs={"property":"jobLocation"})
    if meta_loc and meta_loc.get("content"):
        location = meta_loc["content"].strip()
    if not location:
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
        m = re.search(r"\b[A-Za-z .'-]+,\s*([A-Za-z]{2})\b", body)
        if m:
            start = max(0, m.start()-40)
            location = body[start:m.end()+40].split("\n")[0].strip()
    if not location and re.search(r"\bremote\b", r.text, re.I):
        location = "Remote"
    if not location:
        location = "Unknown"
    # Apply link: try canonical
    apply_link = url
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        apply_link = canonical["href"]
    # Role match
    combined = " ".join([title, company]).lower()
    if not matches_role(combined):
        return None
    # Location must be US (or remote with US mention)
    if not looks_like_us_location(location) and not looks_like_us_location(soup.get_text(" ", strip=True)):
        return None
    return {
        "Job Title": title.strip(),
        "Company": company.strip(),
        "Location": location.strip(),
        "Apply Link": apply_link,
        "Source URL": url,
        "Scraped At": datetime.now().isoformat()
    }

# ---------------- Google Custom Search ----------------
def google_search(q, start=1, num=10):
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CX,
        "q": q,
        "start": start,
        "num": num
    }
    try:
        resp = session.get(url, params=params, timeout=12)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug("google_search error: %s", e)
        return {}

def build_queries():
    # include US indicator to bias results
    site_filter = ""  # leave open; CSE can search entire web
    queries = []
    for kw in KEYWORDS:
        q = f'{kw} "United States" OR USA OR "US"'
        queries.append(q)
    return queries

# ---------------- Collector ----------------
def collect_jobs():
    results = []
    seen = set()
    queries = build_queries()
    for q in queries:
        logger.info("Searching Google for: %s", q)
        for page in range(MAX_PAGES):
            start = page*10 + 1
            data = google_search(q, start=start, num=10)
            items = data.get("items", []) or []
            logger.info("Got %d results (page %d) for query", len(items), page+1)
            for it in items:
                link = it.get("link")
                title = it.get("title","")
                snippet = it.get("snippet","")
                if not link:
                    continue
                # quick pre-check: title/snippet role & US mention to avoid fetching irrelevant pages
                pretext = " ".join([title, snippet]).lower()
                if not matches_role(pretext):
                    # skip if doesn't match role at all
                    continue
                if not looks_like_us_location(pretext):
                    # allow fetching page to re-check (some snippets don't show location)
                    pass
                # fetch page and extract canonical job info
                job = extract_job_from_page(link)
                if not job:
                    # fallback: create minimal entry using title/snippet if prechecks pass and snippet indicates US
                    if matches_role(pretext) and looks_like_us_location(pretext):
                        key = (title.strip(), domain_of(link), link)
                        if key in seen:
                            continue
                        seen.add(key)
                        results.append({
                            "Job Title": title.strip(),
                            "Company": domain_of(link),
                            "Location": "Unknown (from snippet)",
                            "Apply Link": link,
                            "Source URL": link,
                            "Scraped At": datetime.now().isoformat()
                        })
                    continue
                key = (job["Job Title"], job["Company"], job["Apply Link"])
                if key in seen:
                    continue
                seen.add(key)
                results.append(job)
                # politeness
                time.sleep(0.4)
            # small delay between pages
            time.sleep(0.6)
    logger.info("Total collected: %d", len(results))
    return results

# ---------------- Email ----------------
def send_email(jobs):
    today = datetime.now().strftime("%Y-%m-%d")
    msg = EmailMessage()
    msg["Subject"] = f"Daily US Software Jobs (Google CSE) - {today}"
    msg["From"] = EMAIL
    msg["To"] = RECIPIENT
    if not jobs:
        msg.set_content("No jobs found.")
    else:
        lines = []
        for j in jobs:
            lines.append(f"{j.get('Job Title')} | {j.get('Company')} | {j.get('Location')} | {j.get('Apply Link')}")
        msg.set_content("Jobs:\n\n" + "\n".join(lines))
        csv_buf = io.StringIO()
        writer = csv.DictWriter(csv_buf, fieldnames=["Job Title","Company","Location","Apply Link","Source URL","Scraped At"])
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

# ---------------- Main ----------------
def main():
    logger.info("Start Google-based job collection")
    jobs = collect_jobs()
    send_email(jobs)
    logger.info("Done. Collected %d jobs", len(jobs))

if __name__ == "__main__":
    main()
