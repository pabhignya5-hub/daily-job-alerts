import os
import csv
import io
import time
import requests
import schedule
import smtplib
from email.message import EmailMessage
from urllib.parse import urlencode, urlparse
from datetime import datetime
from bs4 import BeautifulSoup
import re

# Config from env
EMAIL = os.environ["EMAIL_ADDRESS"]
PASSWORD = os.environ["EMAIL_PASSWORD"]
BING_KEY = os.environ["BING_API_KEY"]
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
RECIPIENT = os.environ.get("RECIPIENT", EMAIL)

# Search configuration
KEYWORDS = [
    '"software engineer"', '"software developer"', '"full stack"', '"full-stack"',
    '"backend engineer"', '"frontend engineer"'
]
LEVELS = ['entry', 'entry-level', 'junior', 'mid', 'mid-level', 'associate']
SITES = [
    "lever.co", "greenhouse.io", "angel.co", "indeed.com", "linkedin.com",
    "wellfound.com", "glassdoor.com", "remoteok.io", "builtinafrica.org"
]
# domains considered reputable job platforms; results from these are included
REPUTABLE_DOMAINS = set(SITES)

BING_ENDPOINT = "https://api.bing.microsoft.com/v7.0/search"
MAX_RESULTS = 50

def build_query():
    kw_part = " OR ".join(KEYWORDS)
    level_part = " OR ".join(LEVELS)
    site_part = " OR ".join(f"site:{s}" for s in SITES)
    q = f"({kw_part}) ({level_part}) full time {site_part}"
    return q

def bing_search(query):
    headers = {"Ocp-Apim-Subscription-Key": BING_KEY}
    params = {
        "q": query,
        "count": MAX_RESULTS,
        "mkt": "en-US",
        "freshness": "Day",      # only last day
        "textDecorations": False,
        "textFormat": "Raw"
    }
    resp = requests.get(BING_ENDPOINT, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()

def domain_of(url):
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except:
        return ""

def extract_from_bing_item(item):
    # item typically has 'name' and 'url' and 'snippet'
    title = item.get("name", "").strip()
    url = item.get("url", "").strip()
    snippet = item.get("snippet", "") or ""
    domain = domain_of(url)

    # Only include if from reputable domain
    if not any(d in domain for d in REPUTABLE_DOMAINS):
        return None

    # Try to extract company from title: patterns like "Title at Company", "Title - Company", "Company: Title"
    company = None
    title_candidate = title
    # common separators
    m = re.search(r"^(?P<title>.+?)\s+(?:at|@|–|-|—|\|)\s+(?P<company>.+)$", title)
    if m:
        title_candidate = m.group("title").strip()
        company = m.group("company").strip()
    else:
        # fallback: parse from domain (e.g., jobs.lever.co/company => company)
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p]
        if parts:
            company = parts[0] if len(parts) == 1 else parts[0]

    # Location: look for 'remote' or something like "City, ST" in snippet or title
    loc = "Unknown"
    combined = (title + " " + snippet).lower()
    if "remote" in combined:
        loc = "Remote"
    else:
        mloc = re.search(r"([A-Za-z .'-]+,\s*[A-Z]{2}\b)", title + " " + snippet)
        if mloc:
            loc = mloc.group(1).strip()
        else:
            # try patterns like "— City"
            m2 = re.search(r"[—–-]\s*([A-Za-z .'-]+)", title)
            if m2:
                loc = m2.group(1).strip()

    return {
        "Job Title": title_candidate,
        "Company": company or domain,
        "Location": loc,
        "Apply Link": url
    }

def fetch_additional_from_page(url):
    # Best-effort: try to extract a canonical apply link or company/title if bing result is indirect
    try:
        resp = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return {}
        soup = BeautifulSoup(resp.text, "html.parser")
        # meta title
        title_tag = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name":"title"})
        og_title = title_tag["content"].strip() if title_tag and title_tag.get("content") else ""
        company = ""
        # some pages include company in meta
        ctag = soup.find("meta", property="og:site_name") or soup.find("meta", attrs={"name":"application-name"})
        if ctag and ctag.get("content"):
            company = ctag["content"].strip()
        # location: look for address-like strings or 'Remote'
        text = soup.get_text(" ", strip=True)
        loc = "Remote" if "remote" in text.lower() else ""
        return {"page_title": og_title, "page_company": company, "page_location": loc}
    except Exception:
        return {}

def collect_jobs():
    q = build_query()
    data = bing_search(q)
    items = data.get("webPages", {}).get("value", []) if data else []
    seen = set()
    jobs = []
    for it in items:
        parsed = extract_from_bing_item(it)
        if not parsed:
            continue
        key = parsed["Apply Link"]
        if key in seen:
            continue
        seen.add(key)
        # enrich from page if missing useful info
        extra = fetch_additional_from_page(key)
        if extra.get("page_company") and (not parsed["Company"] or parsed["Company"] == domain_of(key)):
            parsed["Company"] = extra["page_company"]
        if extra.get("page_location") and parsed["Location"] in ("Unknown", ""):
            parsed["Location"] = extra["page_location"]
        jobs.append(parsed)
    return jobs

def send_email(jobs):
    if not jobs:
        body = "No new jobs found in the last 24 hours."
    else:
        lines = []
        for j in jobs:
            lines.append(f"{j['Job Title']} | {j['Company']} | {j['Location']} | {j['Apply Link']}")
        body = "New jobs (last 24 hours):\n\n" + "\n".join(lines)

    msg = EmailMessage()
    today = datetime.now().strftime("%Y-%m-%d")
    msg["Subject"] = f"Daily Software Jobs ({today})"
    msg["From"] = EMAIL
    msg["To"] = RECIPIENT
    msg.set_content(body)

    # attach CSV
    csv_buf = io.StringIO()
    writer = csv.DictWriter(csv_buf, fieldnames=["Job Title", "Company", "Location", "Apply Link"])
    writer.writeheader()
    for j in jobs:
        writer.writerow(j)
    csv_bytes = csv_buf.getvalue().encode("utf-8")
    msg.add_attachment(csv_bytes, maintype="text", subtype="csv", filename=f"jobs_{today}.csv")

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(EMAIL, PASSWORD)
        s.send_message(msg)
    print(f"[{datetime.now().isoformat()}] Sent email with {len(jobs)} jobs.")

def job_run():
    print(f"[{datetime.now().isoformat()}] Starting job search...")
    try:
        jobs = collect_jobs()
        send_email(jobs)
    except Exception as e:
        print(f"Error during job run: {e}")

def main():
    # Schedule daily at 08:00
    schedule.every().day.at("08:00").do(job_run)
    print("Scheduled daily job at 08:00. Running scheduler loop...")
    # Optionally run once at startup
    job_run()
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
